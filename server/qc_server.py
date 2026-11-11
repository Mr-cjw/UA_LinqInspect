"""AI 质检 OPC UA Server — M1 骨架实现

信息模型基准: Phase0_需求拆解与信息模型.md §2（NodeId/变量/Method/Event 全部以该文档为准）
编码硬约束: Phase0 §2.6
  - 自定义 Event 必须继承 BaseEventType（IEC 62541 Part 5 §6.4）
  - 所有自定义节点 NodeId 一律使用 ns=2;s=<可读名> 的 String 形式
  - 服务端口不硬编码，经 UA_ENDPOINT_PORT 环境变量配置（默认 4840）
  - Method 先校验前置条件，拒绝时返回原因（Part 4 §5.10 Call）

环境变量:
  UA_ENDPOINT_PORT    监听端口（默认 4840）
  QC_AUTO_INSPECT     是否启动虚拟质检循环（1=开，默认；0=关，交给真实 YOLOv8/外部写入）
  QC_INSPECT_INTERVAL 检测间隔秒（默认 2.0）
  QC_NG_PROBABILITY   虚拟 NG 概率（默认 0.3）
  QC_NG_WINDOW        NgRate 滑动窗口大小（默认 20）
"""
import asyncio
import logging
import os
import random
from collections import deque
from datetime import datetime, timezone

from asyncua import Server, ua

NS_URI = "urn:fde:linqinspect:qc"
DEFECT_CODES = ("SCRATCH", "DENT", "STAIN", "BURR", "MISSING")

# ---- 可配置项（不硬编码端口） ----
PORT = int(os.getenv("UA_ENDPOINT_PORT", "4840"))
AUTO_INSPECT = os.getenv("QC_AUTO_INSPECT", "1") != "0"
INSPECT_INTERVAL = float(os.getenv("QC_INSPECT_INTERVAL", "2.0"))
NG_PROBABILITY = float(os.getenv("QC_NG_PROBABILITY", "0.3"))
WINDOW_SIZE = int(os.getenv("QC_NG_WINDOW", "20"))

# DeviceStatus 数字枚举含义: 0=Idle, 1=Running, 2=Fault
ST_IDLE, ST_RUNNING, ST_FAULT = 0, 1, 2

_log = logging.getLogger("qc_server")


def _val(v):
    """兼容 asyncua 回调入参为 Variant 或裸值两种情况。"""
    return v.Value if isinstance(v, ua.Variant) else v


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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    server = Server()
    await server.init()
    server.set_endpoint(f"opc.tcp://0.0.0.0:{PORT}")
    # 教学环境：放开安全策略，便于 UaExpert 直连（生产必须启用证书，Part 6 §6.4）
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])

    station = QCStation(server)
    await station.build()

    _log.info("OPC UA Server listening at opc.tcp://localhost:%d (ns=%d)", PORT, station.idx)
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
    except KeyboardInterrupt:
        pass
