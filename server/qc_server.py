"""AI 质检 OPC UA Server — M1 骨架实现

信息模型基准: Phase0_需求拆解与信息模型.md §2（NodeId/变量/Method/Event 全部以该文档为准）
编码硬约束: Phase0 §2.6
  - 自定义 Event 必须继承 BaseEventType（IEC 62541 Part 5 §6.4）
  - 所有自定义节点 NodeId 一律使用 ns=2;s=<可读名> 的 String 形式
  - 服务端口不硬编码，经 UA_ENDPOINT_PORT 环境变量配置（默认 4840）
  - Method 先校验前置条件，拒绝时返回原因（Part 4 §5.10 Call）

环境变量:
  UA_ENDPOINT_PORT    监听端口（默认 4840）
  UA_BIND_ADDRESS     监听地址（默认 127.0.0.1；如需对外服务显式设置为受控网卡）
  UA_ADMIN_USER       可写管理员账号（未配置时仅允许匿名只读）
  UA_ADMIN_PASS       可写管理员密码（未配置时仅允许匿名只读）
  UA_SERVER_CERT      服务器证书路径（必填，.pem/.der；启动时检查 30 天到期警告）
  UA_SERVER_PRIVATE_KEY 服务器私钥路径（必填）
  UA_SERVER_PRIVATE_KEY_PASSWORD 私钥密码（可选）
  UA_TRUSTED_CLIENT_CERTS_DIR 受信客户端证书目录（必填，至少包含一个 .pem/.der）
  UA_AUDIT_LOG        审计日志路径（默认 server_audit.log，自动轮转）
  QC_AUTO_INSPECT     是否启动虚拟质检循环（1=开，默认；0=关，交给真实 YOLOv8/外部写入）
  QC_INSPECT_INTERVAL 检测间隔秒（默认 2.0）
  QC_NG_PROBABILITY   虚拟 NG 概率（默认 0.3）
  QC_NG_WINDOW        NgRate 滑动窗口大小（默认 20）
"""
import asyncio
import hmac
import json
import logging
import os
import random
from collections import deque
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import MethodType

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from asyncua import Server, ua
from asyncua.crypto import uacrypto
from asyncua.crypto.permission_rules import SimpleRoleRuleset, User, UserRole
from asyncua.crypto.truststore import TrustStore
from asyncua.crypto.validator import CertificateValidator, CertificateValidatorOptions
from asyncua.server.internal_session import InternalSession
from asyncua.server.user_managers import UserManager

NS_URI = "urn:fde:linqinspect:qc"
DEFECT_CODES = ("SCRATCH", "DENT", "STAIN", "BURR", "MISSING")

# ---- 可配置项（不硬编码端口） ----
PORT = int(os.getenv("UA_ENDPOINT_PORT", "4840"))
BIND_ADDRESS = os.getenv("UA_BIND_ADDRESS", "").strip() or "127.0.0.1"
ADMIN_USER = os.getenv("UA_ADMIN_USER", "")
ADMIN_PASS = os.getenv("UA_ADMIN_PASS", "")
SERVER_CERT_PATH = os.getenv("UA_SERVER_CERT", "").strip()
SERVER_KEY_PATH = os.getenv("UA_SERVER_PRIVATE_KEY", "").strip()
SERVER_KEY_PASSWORD = os.getenv("UA_SERVER_PRIVATE_KEY_PASSWORD", "")
CLIENT_TRUST_DIR = os.getenv("UA_TRUSTED_CLIENT_CERTS_DIR", "").strip()
AUDIT_LOG_PATH = os.getenv("UA_AUDIT_LOG", "server_audit.log").strip() or "server_audit.log"
AUTO_INSPECT = os.getenv("QC_AUTO_INSPECT", "1") != "0"
INSPECT_INTERVAL = float(os.getenv("QC_INSPECT_INTERVAL", "2.0"))
NG_PROBABILITY = float(os.getenv("QC_NG_PROBABILITY", "0.3"))
WINDOW_SIZE = int(os.getenv("QC_NG_WINDOW", "20"))
CERT_EXPIRY_WARNING_DAYS = 30

# DeviceStatus 数字枚举含义: 0=Idle, 1=Running, 2=Fault
ST_IDLE, ST_RUNNING, ST_FAULT = 0, 1, 2

_log = logging.getLogger("qc_server")
_method_user: ContextVar[User | None] = ContextVar("qc_method_user", default=None)


def _val(v):
    """兼容 asyncua 回调入参为 Variant 或裸值两种情况。"""
    return v.Value if isinstance(v, ua.Variant) else v


def _audit_user_name(user: User | None) -> str:
    if user is None or user.role == UserRole.Anonymous:
        return "Anonymous"
    return user.name or "Authenticated"


def _format_method_result(result) -> str:
    if isinstance(result, (list, tuple)):
        if not result:
            return "accepted"
        accepted = bool(_val(result[0]))
        detail = _val(result[1]) if len(result) > 1 else ""
        verdict = "accepted" if accepted else "rejected"
        detail_text = str(detail).strip()
        prefix = f"{verdict}:"
        if detail_text.lower().startswith(prefix):
            detail_text = detail_text[len(prefix):].strip()
        return f"{verdict}: {detail_text}" if detail_text else verdict
    return str(result)


def _audit_method_call(method_name: str, arguments: dict, result) -> None:
    _log.info(
        "AUDIT ts=%s method=%s user=%s args=%s result=%s",
        datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        method_name,
        _audit_user_name(_method_user.get()),
        json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str),
        _format_method_result(result),
    )


class _AuditedInternalSession(InternalSession):
    """Expose the session user to method callbacks without changing asyncua call signatures."""

    async def call(self, params: list[ua.CallMethodRequest]) -> list[ua.CallMethodResult]:
        token = _method_user.set(self.user)
        try:
            return await super().call(params)
        finally:
            _method_user.reset(token)


def _install_audited_session_factory(server: Server) -> None:
    """Use the audited session subclass for client connections on this server instance."""
    iserver = server.iserver

    def _create_session(iserver_self, name, user=User(role=UserRole.Anonymous), external=False):
        return _AuditedInternalSession(
            iserver_self,
            iserver_self.aspace,
            iserver_self.subscription_service,
            name,
            user=user,
            external=external,
        )

    iserver.create_session = MethodType(_create_session, iserver)


class SecurityConfigError(RuntimeError):
    """证书、私钥或客户端信任列表缺失/无效，服务必须拒绝启动。"""


def _require_config_file(env_name: str, raw_value: str) -> Path:
    if not raw_value:
        raise SecurityConfigError(
            f"{env_name} is required for Basic256Sha256_SignAndEncrypt; "
            "refusing to start without explicit certificate configuration"
        )
    path = Path(raw_value).expanduser()
    try:
        if not path.is_file():
            raise SecurityConfigError(f"{env_name}={raw_value!r} does not point to a readable file")
        with path.open("rb"):
            pass
    except OSError as exc:
        raise SecurityConfigError(f"{env_name}={raw_value!r} is not readable: {exc}") from exc
    return path


def _require_config_dir(env_name: str, raw_value: str) -> Path:
    if not raw_value:
        raise SecurityConfigError(
            f"{env_name} is required for Basic256Sha256_SignAndEncrypt; "
            "refusing to start without a trusted client certificate directory"
        )
    path = Path(raw_value).expanduser()
    try:
        if not path.is_dir():
            raise SecurityConfigError(f"{env_name}={raw_value!r} does not point to a readable directory")
    except OSError as exc:
        raise SecurityConfigError(f"{env_name}={raw_value!r} is not readable: {exc}") from exc
    return path


async def _load_trust_store(raw_dir: str) -> TrustStore:
    trust_dir = _require_config_dir("UA_TRUSTED_CLIENT_CERTS_DIR", raw_dir)
    try:
        cert_files = sorted(
            path for path in trust_dir.iterdir()
            if path.is_file() and path.suffix.lower() in (".pem", ".der")
        )
    except OSError as exc:
        raise SecurityConfigError(
            f"Failed to read UA_TRUSTED_CLIENT_CERTS_DIR={str(trust_dir)!r}: {exc}"
        ) from exc
    if not cert_files:
        raise SecurityConfigError(
            f"UA_TRUSTED_CLIENT_CERTS_DIR={str(trust_dir)!r} contains no .pem/.der client certificates; "
            "refusing to start with an empty trust list"
        )

    for cert_file in cert_files:
        try:
            await uacrypto.load_certificate(cert_file)
        except Exception as exc:
            raise SecurityConfigError(f"Invalid trusted client certificate {cert_file}: {exc}") from exc

    trust_store = TrustStore([trust_dir], [])
    try:
        await trust_store.load()
    except Exception as exc:
        raise SecurityConfigError(
            f"Failed to load trusted client certificates from {trust_dir}: {exc}"
        ) from exc

    _log.info("Loaded %d trusted client certificate file(s) from %s", len(cert_files), trust_dir)
    return trust_store


def _verify_server_key_pair(certificate, private_key) -> None:
    def public_key_der(key) -> bytes:
        return key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    try:
        certificate_key = public_key_der(certificate.public_key())
        private_key_public = public_key_der(private_key.public_key())
    except Exception as exc:
        raise SecurityConfigError(f"Failed to inspect server certificate/private key: {exc}") from exc
    if not hmac.compare_digest(certificate_key, private_key_public):
        raise SecurityConfigError(
            "UA_SERVER_CERT and UA_SERVER_PRIVATE_KEY do not form a matching key pair; refusing to start"
        )


def _warn_if_server_cert_expiring(certificate, cert_path: Path) -> None:
    not_after = getattr(certificate, "not_valid_after_utc", None)
    if not_after is None:
        not_after = certificate.not_valid_after.replace(tzinfo=timezone.utc)
    remaining = not_after - datetime.now(timezone.utc)
    days = remaining.total_seconds() / 86400
    if days < 0:
        _log.warning(
            "Server certificate %s expired %.1f days ago (notAfter=%s)",
            cert_path, abs(days), not_after.isoformat(),
        )
    elif days <= CERT_EXPIRY_WARNING_DAYS:
        _log.warning(
            "Server certificate %s expires in %.1f days (notAfter=%s); renew it before expiry",
            cert_path, days, not_after.isoformat(),
        )
    else:
        _log.info(
            "Server certificate %s expires in %.1f days (notAfter=%s)",
            cert_path, days, not_after.isoformat(),
        )


async def _load_server_identity(server: Server, cert_path: Path, key_path: Path) -> None:
    try:
        await server.load_certificate(cert_path)
    except Exception as exc:
        raise SecurityConfigError(f"Failed to load UA_SERVER_CERT from {cert_path}: {exc}") from exc
    try:
        await server.load_private_key(key_path, SERVER_KEY_PASSWORD or None)
    except Exception as exc:
        raise SecurityConfigError(f"Failed to load UA_SERVER_PRIVATE_KEY from {key_path}: {exc}") from exc

    certificate = server.iserver.certificate
    private_key = server.iserver.private_key
    if certificate is None or private_key is None:
        raise SecurityConfigError("Server certificate/private key failed to load; refusing to start")
    _verify_server_key_pair(certificate, private_key)
    _warn_if_server_cert_expiring(certificate, cert_path)


class QCUserManager(UserManager):
    """匿名用户只读；仅管理员用户名/密码匹配时授予读写与 Method 调用权限。

    客户端证书只用于 SecureChannel 与证书校验，不参与用户身份授权。
    """

    def __init__(self, admin_user: str = "", admin_pass: str = "", trust_store: TrustStore | None = None):
        self._admin_user = admin_user or ""
        self._admin_pass = admin_pass or ""
        self._trust_store = trust_store

    def get_user(self, iserver, username=None, password=None, certificate=None):
        # OPC UA 匿名会话仍会携带 SecureChannel 的对端证书（certificate 参数）；
        # 该证书只证明通道身份，不能把 Anonymous 身份提升为可写用户。
        if certificate and not self._is_trusted_client_certificate(certificate):
            return None
        if not username:
            return User(role=UserRole.Anonymous)

        supplied_username = self._as_text(username)
        supplied_password = self._as_text(password)
        user_ok = bool(self._admin_user) and hmac.compare_digest(supplied_username, self._admin_user)
        password_ok = bool(self._admin_pass) and hmac.compare_digest(supplied_password, self._admin_pass)
        if user_ok and password_ok:
            return User(role=UserRole.User, name=supplied_username)
        return None

    def _is_trusted_client_certificate(self, certificate) -> bool:
        if self._trust_store is None:
            return False
        try:
            cert = certificate if isinstance(certificate, x509.Certificate) else uacrypto.x509_from_der(certificate)
            return cert is not None and bool(self._trust_store.is_trusted(cert))
        except Exception:
            _log.warning("Rejected client certificate because it could not be validated", exc_info=True)
            return False

    @staticmethod
    def _as_text(value) -> str:
        if isinstance(value, (bytes, bytearray)):
            return value.decode("utf-8", errors="replace")
        return "" if value is None else str(value)


class QCAnonymousReadOnlyRuleset(SimpleRoleRuleset):
    """匿名角色可读/浏览/订阅，但禁止 Write 与 Call；普通用户保留读写权限。"""

    _PRIVILEGED_SERVICES = {
        ua.NodeId(ua.ObjectIds.WriteRequest_Encoding_DefaultBinary),
        ua.NodeId(ua.ObjectIds.CallRequest_Encoding_DefaultBinary),
    }

    def __init__(self):
        super().__init__()
        self._permission_dict[UserRole.Anonymous] = (
            set(self._permission_dict[UserRole.User]) - self._PRIVILEGED_SERVICES
        )


class QCStation:
    """QCStation 对象树的状态与行为承载（对应信息模型 §2.1~§2.4）。"""

    def __init__(self, server: Server):
        self.server = server
        self.idx = None
        self.status = ST_IDLE
        self.work_order_id = ""
        self.target_count = 0
        self.inspect_count = 0
        self.ok_count = 0
        self.ng_count = 0
        self.window = deque(maxlen=WINDOW_SIZE)  # True=OK, False=NG
        self.nd = {}

    # ---------- 地址空间构建 ----------
    async def build(self):
        idx = await self.server.register_namespace(NS_URI)
        self.idx = idx
        sv = self.server

        def nid(s: str) -> ua.NodeId:
            """按 Phase0 §2.6-4 生成 ns=2;s=<名> 的 String NodeId。"""
            return ua.NodeId(s, idx, ua.NodeIdType.String)

        root = await sv.nodes.objects.add_object(nid("QCStation"), "2:QCStation")
        insp = await root.add_object(nid("QCStation.Inspection"), "2:Inspection")
        stats = await root.add_object(nid("QCStation.Statistics"), "2:Statistics")
        events = await root.add_object(nid("QCStation.Events"), "2:Events")
        # 事件 Notifier 必须置 SubscribeToEvents 位，否则客户端无法在该对象上订阅事件
        await events.set_event_notifier([ua.EventNotifier.SubscribeToEvents])
        self.nd["events_source"] = events

        # ---------- Variables（权限按 Phase0 §2.2） ----------
        async def _ro(node):
            # asyncua 2.1.0 的 add_variable 默认 AccessLevel=1（只读），这里显式确认收敛为只读（Part 4 §5.8）
            await node.set_read_only()
            return node

        async def _rw(node):
            # ReadWrite 变量必须显式 set_writable，否则客户端（Node-RED）写入被 BadUserAccessDenied 拒绝
            await node.set_writable()
            return node

        self.nd["status"] = await _rw(await root.add_variable(nid("QCStation.DeviceStatus"), "2:DeviceStatus", ua.Variant(ST_IDLE, ua.VariantType.Int32)))
        self.nd["work_order"] = await _rw(await root.add_variable(nid("QCStation.CurrentWorkOrderId"), "2:CurrentWorkOrderId", ""))
        # Part Traceability：单件序列号（ReadWrite，由数据源/Node-RED 写入，追溯基石）
        self.nd["part_sn"] = await _rw(await root.add_variable(nid("QCStation.CurrentPartSN"), "2:CurrentPartSN", ""))
        # MES 回执落地：按业务语义拆分；LastMesOrderNo 仅作为最近回执的兼容镜像
        self.nd["mes_order"] = await _rw(await root.add_variable(nid("QCStation.LastMesOrderNo"), "2:LastMesOrderNo", ""))
        self.nd["ng_mes_order"] = await _rw(await root.add_variable(nid("QCStation.LastNgMesOrderNo"), "2:LastNgMesOrderNo", ""))
        self.nd["completion_receipt"] = await _rw(await root.add_variable(nid("QCStation.LastCompletionReceiptNo"), "2:LastCompletionReceiptNo", ""))

        self.nd["last_result"] = await _ro(await insp.add_variable(nid("QCStation.Inspection.LastResult"), "2:LastResult", False))
        self.nd["last_defect"] = await _ro(await insp.add_variable(nid("QCStation.Inspection.LastDefectType"), "2:LastDefectType", ""))
        self.nd["last_conf"] = await _ro(await insp.add_variable(nid("QCStation.Inspection.LastConfidence"), "2:LastConfidence", 0.0))
        self.nd["last_img"] = await _ro(await insp.add_variable(nid("QCStation.Inspection.LastImageId"), "2:LastImageId", ""))
        self.nd["last_ts"] = await _ro(await insp.add_variable(nid("QCStation.Inspection.LastTimestamp"), "2:LastTimestamp", datetime(2000, 1, 1, tzinfo=timezone.utc)))
        self.nd["ng_rate"] = await _ro(await insp.add_variable(nid("QCStation.Inspection.NgRate"), "2:NgRate", -1.0))  # -1.0 = 空窗口保护

        self.nd["cnt_all"] = await _ro(await stats.add_variable(nid("QCStation.Statistics.InspectCount"), "2:InspectCount", ua.Variant(0, ua.VariantType.UInt32)))
        self.nd["cnt_ok"] = await _ro(await stats.add_variable(nid("QCStation.Statistics.OkCount"), "2:OkCount", ua.Variant(0, ua.VariantType.UInt32)))
        self.nd["cnt_ng"] = await _ro(await stats.add_variable(nid("QCStation.Statistics.NgCount"), "2:NgCount", ua.Variant(0, ua.VariantType.UInt32)))

        # ---------- 自定义 EventType（必须继承 BaseEventType，Phase0 §2.6-1） ----------
        base = sv.nodes.base_event_type
        et_insp = await base.add_object_type(nid("InspectionFinishedEventType"), "2:InspectionFinishedEventType")
        for name, v in (("WorkOrderId", ""), ("SerialNumber", ""), ("Result", ""), ("DefectType", ""),
                        ("Confidence", 0.0), ("ImageId", "")):
            await et_insp.add_property(nid(f"InspectionFinishedEventType.{name}"), f"2:{name}", v)

        et_ng = await base.add_object_type(nid("NgDetectedEventType"), "2:NgDetectedEventType")
        for name, v in (("WorkOrderId", ""), ("SerialNumber", ""), ("DefectType", ""),
                        ("Confidence", 0.0), ("ImageId", "")):
            await et_ng.add_property(nid(f"NgDetectedEventType.{name}"), f"2:{name}", v)

        et_done = await base.add_object_type(nid("WorkOrderCompletedEventType"), "2:WorkOrderCompletedEventType")
        for name, v in (("WorkOrderId", ""), ("InspectCount", 0), ("OkCount", 0),
                        ("NgCount", 0), ("NgRate", 0.0)):
            await et_done.add_property(nid(f"WorkOrderCompletedEventType.{name}"), f"2:{name}", v)

        self.et = {"insp": et_insp, "ng": et_ng, "done": et_done}

        # ---------- Methods（先校验后执行，Phase0 §2.6-3） ----------
        await root.add_method(nid("QCStation.StartInspection"), "2:StartInspection", self._start_inspection,
                              [ua.VariantType.String, ua.VariantType.UInt32],
                              [ua.VariantType.Boolean, ua.VariantType.String])
        await root.add_method(nid("QCStation.CompleteWorkOrder"), "2:CompleteWorkOrder", self._complete_work_order,
                              [ua.VariantType.String],
                              [ua.VariantType.Boolean, ua.VariantType.String])
        await root.add_method(nid("QCStation.ResetCounters"), "2:ResetCounters", self._reset_counters, [], [])

        _log.info("AddressSpace built: ns=%d uri=%s", idx, NS_URI)

    # ---------- 事件触发 ----------
    async def _fire(self, etype_key: str, fields: dict, severity: int, message: str):
        gen = await self.server.get_event_generator(self.et[etype_key], self.nd["events_source"])
        for k, v in fields.items():
            setattr(gen.event, k, v)
        gen.event.Severity = severity
        gen.event.Message = ua.LocalizedText(message)
        await gen.trigger()

    # ---------- Method 实现 ----------
    async def _start_inspection(self, parent, work_order_id, target_count):
        audit_args = {"workOrderId": _val(work_order_id), "targetCount": _val(target_count)}
        try:
            result = await self._start_inspection_impl(work_order_id, target_count)
        except Exception as exc:
            _audit_method_call("StartInspection", audit_args, f"error: {type(exc).__name__}: {exc}")
            raise
        _audit_method_call("StartInspection", audit_args, result)
        return result

    async def _start_inspection_impl(self, work_order_id, target_count):
        wo, tc = _val(work_order_id), _val(target_count)
        if not wo or not str(wo).strip():
            return [ua.Variant(False, ua.VariantType.Boolean), ua.Variant("rejected: empty workOrderId", ua.VariantType.String)]
        if self.status == ST_RUNNING:
            # 幂等/状态校验：同工单重发 = 幂等接受；不同工单 = 拒绝（防重复动作类故障）
            if str(wo) == self.work_order_id:
                return [ua.Variant(True, ua.VariantType.Boolean), ua.Variant("already running", ua.VariantType.String)]
            return [ua.Variant(False, ua.VariantType.Boolean), ua.Variant(f"rejected: already running with different work order {self.work_order_id}", ua.VariantType.String)]
        if self.status == ST_FAULT:
            return [ua.Variant(False, ua.VariantType.Boolean), ua.Variant("rejected: DeviceStatus=Fault", ua.VariantType.String)]
        if not tc or int(tc) <= 0:
            return [ua.Variant(False, ua.VariantType.Boolean), ua.Variant("rejected: targetCount must be > 0", ua.VariantType.String)]
        self.status = ST_RUNNING
        self.work_order_id = str(wo)
        self.target_count = int(tc)
        await self.nd["status"].write_value(ua.Variant(ST_RUNNING, ua.VariantType.Int32))
        await self.nd["work_order"].write_value(self.work_order_id)
        _log.info("StartInspection: %s target=%d", self.work_order_id, self.target_count)
        return [ua.Variant(True, ua.VariantType.Boolean), ua.Variant(f"started {self.work_order_id}, target={self.target_count}", ua.VariantType.String)]

    async def _complete_work_order(self, parent, work_order_id):
        audit_args = {"workOrderId": _val(work_order_id)}
        try:
            result = await self._complete_work_order_impl(work_order_id)
        except Exception as exc:
            _audit_method_call("CompleteWorkOrder", audit_args, f"error: {type(exc).__name__}: {exc}")
            raise
        _audit_method_call("CompleteWorkOrder", audit_args, result)
        return result

    async def _complete_work_order_impl(self, work_order_id):
        wo = str(_val(work_order_id) or "")
        # 校验链：一致性（Phase0 评审意见 #1）→ 状态前置
        if not self.work_order_id:
            return [ua.Variant(False, ua.VariantType.Boolean), ua.Variant("rejected: no active work order", ua.VariantType.String)]
        if wo != self.work_order_id:
            return [ua.Variant(False, ua.VariantType.Boolean), ua.Variant(f"rejected: workOrderId mismatch, expected {self.work_order_id}", ua.VariantType.String)]
        if self.status != ST_RUNNING:
            return [ua.Variant(False, ua.VariantType.Boolean), ua.Variant(f"rejected: DeviceStatus={self.status} is not Running", ua.VariantType.String)]
        # 汇总 + 事件
        ng_rate = self._ng_rate()
        summary = (f"workOrder={self.work_order_id} inspect={self.inspect_count} "
                   f"ok={self.ok_count} ng={self.ng_count} ngRate={ng_rate:.3f}")
        await self._fire("done", {
            "WorkOrderId": self.work_order_id,
            "InspectCount": self.inspect_count,
            "OkCount": self.ok_count,
            "NgCount": self.ng_count,
            # 空窗口保持 -1.0 哨兵语义，MES 侧据此显示“样本不足”，不得误报为 0 不良率
            "NgRate": ng_rate,
        }, severity=2, message=f"WorkOrder {self.work_order_id} completed")
        self.status = ST_IDLE
        self.work_order_id = ""
        self.target_count = 0
        await self.nd["status"].write_value(ua.Variant(ST_IDLE, ua.VariantType.Int32))
        await self.nd["work_order"].write_value("")
        _log.info("CompleteWorkOrder: %s", summary)
        return [ua.Variant(True, ua.VariantType.Boolean), ua.Variant(summary, ua.VariantType.String)]

    async def _reset_counters(self, parent):
        try:
            result = await self._reset_counters_impl()
        except Exception as exc:
            _audit_method_call("ResetCounters", {}, f"error: {type(exc).__name__}: {exc}")
            raise
        _audit_method_call("ResetCounters", {}, result)
        return result

    async def _reset_counters_impl(self):
        self.inspect_count = self.ok_count = self.ng_count = 0
        self.window.clear()
        await self._write_stats()
        await self.nd["ng_rate"].write_value(-1.0)  # 回到空窗口语义
        return []

    # ---------- 检测结果落地 ----------
    def _ng_rate(self) -> float:
        """滑动窗口 NG 率；空窗口保护：无样本时返回 -1.0（见 docs/M1 契约 §2）。"""
        if not self.window:
            return -1.0
        return sum(1 for ok in self.window if not ok) / len(self.window)

    async def _write_stats(self):
        await self.nd["cnt_all"].write_value(ua.Variant(self.inspect_count, ua.VariantType.UInt32))
        await self.nd["cnt_ok"].write_value(ua.Variant(self.ok_count, ua.VariantType.UInt32))
        await self.nd["cnt_ng"].write_value(ua.Variant(self.ng_count, ua.VariantType.UInt32))

    async def record_inspection(self, is_ok: bool, defect_type: str, confidence: float,
                                image_id: str, serial_number: str = ""):
        self.inspect_count += 1
        self.window.append(is_ok)
        if is_ok:
            self.ok_count += 1
        else:
            self.ng_count += 1

        now = datetime.now(timezone.utc)
        await self.nd["last_result"].write_value(is_ok)
        await self.nd["last_defect"].write_value(defect_type)
        await self.nd["last_conf"].write_value(confidence)
        await self.nd["last_img"].write_value(image_id)
        await self.nd["last_ts"].write_value(now)
        if serial_number:
            await self.nd["part_sn"].write_value(serial_number)
        await self._write_stats()
        await self.nd["ng_rate"].write_value(self._ng_rate())

        await self._fire("insp", {
            "WorkOrderId": self.work_order_id,
            "SerialNumber": serial_number,
            "Result": "OK" if is_ok else "NG",
            "DefectType": defect_type,
            "Confidence": confidence,
            "ImageId": image_id,
        }, severity=1, message=f"Inspection {image_id} SN={serial_number}: {'OK' if is_ok else 'NG ' + defect_type}")

        if not is_ok:
            await self._fire("ng", {
                "WorkOrderId": self.work_order_id,
                "SerialNumber": serial_number,
                "DefectType": defect_type,
                "Confidence": confidence,
                "ImageId": image_id,
            }, severity=3, message=f"NG detected on {image_id} SN={serial_number}: {defect_type}")
            _log.warning("NG: %s SN=%s %s conf=%.3f", image_id, serial_number, defect_type, confidence)

    # ---------- 虚拟 AI 质检循环（阶段一数据源，预留 YOLOv8 切换点） ----------
    async def inspection_loop(self):
        seq = 0
        while True:
            await asyncio.sleep(INSPECT_INTERVAL)
            if self.status != ST_RUNNING:
                continue
            seq += 1
            is_ok = random.random() > NG_PROBABILITY
            defect = "" if is_ok else random.choice(DEFECT_CODES)
            confidence = round(random.uniform(0.85, 0.99) if is_ok else random.uniform(0.55, 0.95), 3)
            image_id = f"IMG-{datetime.now(timezone.utc):%Y%m%d}-{seq:06d}"
            serial_number = f"SN-{datetime.now(timezone.utc):%Y%m%d}-{seq:06d}"
            # TODO(YOLOv8 切换点): 用真实推理结果+产线 SN 替换上面的 random 分支后调用 record_inspection
            await self.record_inspection(is_ok, defect, confidence, image_id, serial_number)
            if self.target_count and self.inspect_count >= self.target_count:
                _log.info("Target count reached (%d); wait for CompleteWorkOrder call", self.target_count)
                self.target_count = 0  # 仅提示一次


async def main():
    audit_log_path = Path(AUDIT_LOG_PATH).expanduser()
    if not audit_log_path.is_absolute():
        audit_log_path = Path.cwd() / audit_log_path
    audit_log_path.parent.mkdir(parents=True, exist_ok=True)
    log_format = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_format)
    audit_handler = RotatingFileHandler(
        audit_log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    audit_handler.setFormatter(log_format)
    logging.basicConfig(level=logging.WARNING, handlers=[console_handler, audit_handler], force=True)
    _log.setLevel(logging.INFO)
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    _log.info("Audit log rotating file: %s", audit_log_path)

    cert_path = _require_config_file("UA_SERVER_CERT", SERVER_CERT_PATH)
    key_path = _require_config_file("UA_SERVER_PRIVATE_KEY", SERVER_KEY_PATH)
    trust_store = await _load_trust_store(CLIENT_TRUST_DIR)

    server = Server(user_manager=QCUserManager(ADMIN_USER, ADMIN_PASS, trust_store))
    await server.init()
    _install_audited_session_factory(server)
    server.set_endpoint(f"opc.tcp://{BIND_ADDRESS}:{PORT}")
    await _load_server_identity(server, cert_path, key_path)
    server.set_certificate_validator(
        CertificateValidator(
            CertificateValidatorOptions.TRUSTED_VALIDATION | CertificateValidatorOptions.PEER_CLIENT,
            trust_store=trust_store,
        )
    )
    # 仅暴露签名并加密的端点；证书/私钥/信任列表任一缺失都会在启动前失败，禁止回退 NoSecurity。
    server.set_security_policy(
        [ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt],
        permission_ruleset=QCAnonymousReadOnlyRuleset(),
    )

    if not ADMIN_USER or not ADMIN_PASS:
        _log.warning(
            "UA_ADMIN_USER/UA_ADMIN_PASS not configured; clients are read-only"
        )

    station = QCStation(server)
    await station.build()

    _log.info(
        "OPC UA Server listening at opc.tcp://%s:%d (ns=%d, policy=Basic256Sha256_SignAndEncrypt)",
        BIND_ADDRESS, PORT, station.idx,
    )
    async with server:
        if AUTO_INSPECT:
            task = asyncio.create_task(station.inspection_loop())
            try:
                await asyncio.Future()  # 运行至被取消
            finally:
                task.cancel()
        else:
            await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SecurityConfigError as exc:
        _log.critical("Startup aborted: %s", exc)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        pass
