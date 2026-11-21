"""Secure OPC UA station gateway used by the operator console."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from asyncua import Client, ua
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256

REPO_ROOT = Path(__file__).resolve().parents[1]


class StationError(RuntimeError):
    """Station control or readback failed."""


class StationGateway(Protocol):
    def read_status(self) -> dict[str, Any]: ...
    def start_work_order(self, work_order_id: str, target_count: int) -> dict[str, Any]: ...
    def complete_work_order(self, work_order_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class StationConfig:
    endpoint: str
    client_cert: Path
    client_key: Path
    server_cert: Path
    admin_user: str
    admin_pass: str
    namespace_uri: str = "urn:fde:linqinspect:qc"
    station_node_name: str = "QCStation"
    request_timeout: float = 5.0
    application_uri: str = "urn:freeopcua:python:client"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "StationConfig":
        values = os.environ if env is None else env
        user = values.get("UA_ADMIN_USER", "").strip()
        password = values.get("UA_ADMIN_PASS", "")
        if not user or not password:
            raise ValueError("UA_ADMIN_USER and UA_ADMIN_PASS are required")
        return cls(
            endpoint=values.get("UA_ENDPOINT", "opc.tcp://127.0.0.1:4840"),
            client_cert=Path(values.get("UA_CLIENT_CERT", REPO_ROOT / "certs/client/client_cert.der")).expanduser(),
            client_key=Path(values.get("UA_CLIENT_KEY", REPO_ROOT / "certs/client/client_key.pem")).expanduser(),
            server_cert=Path(values.get("UA_SERVER_CERT", REPO_ROOT / "certs/server/server_cert.der")).expanduser(),
            admin_user=user,
            admin_pass=password,
            namespace_uri=values.get("QC_NAMESPACE_URI", "urn:fde:linqinspect:qc"),
            station_node_name=values.get("QC_STATION_NODE_NAME", "QCStation"),
            request_timeout=max(0.5, float(values.get("UA_OPERATOR_TIMEOUT_SECONDS", "5"))),
            application_uri=values.get("UA_CLIENT_APPLICATION_URI", "urn:freeopcua:python:client"),
        )


def _unwrap(value: Any) -> Any:
    while isinstance(value, ua.Variant):
        value = value.Value
    return value


def _json_scalar(value: Any) -> Any:
    value = _unwrap(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


class UaStationClient:
    def __init__(self, config: StationConfig):
        self.config = config

    async def _connect(self) -> Client:
        for path in (self.config.client_cert, self.config.client_key, self.config.server_cert):
            if not path.is_file():
                raise StationError(f"missing UA certificate/key: {path}")
        client = Client(self.config.endpoint, timeout=self.config.request_timeout)
        client.application_uri = self.config.application_uri
        await client.set_security(
            SecurityPolicyBasic256Sha256,
            certificate=str(self.config.client_cert),
            private_key=str(self.config.client_key),
            server_certificate=str(self.config.server_cert),
            mode=ua.MessageSecurityMode.SignAndEncrypt,
        )
        client.set_user(self.config.admin_user)
        client.set_password(self.config.admin_pass)
        await client.connect()
        return client

    async def _root(self, client: Client):
        namespace_index = await client.get_namespace_index(self.config.namespace_uri)
        root = await client.nodes.objects.get_child(
            [f"{namespace_index}:{self.config.station_node_name}"]
        )
        return namespace_index, root

    @staticmethod
    async def _read(node) -> Any:
        return _unwrap(await node.read_value())

    async def _read_status_async(self) -> dict[str, Any]:
        client = await self._connect()
        try:
            namespace_index, root = await self._root(client)
            statistics = await root.get_child([f"{namespace_index}:Statistics"])
            inspection = await root.get_child([f"{namespace_index}:Inspection"])
            values = {}
            for name in (
                "DeviceStatus",
                "CurrentWorkOrderId",
                "CurrentPartSN",
                "LastMesOrderNo",
                "LastNgMesOrderNo",
                "LastCompletionReceiptNo",
            ):
                values[name] = _json_scalar(await self._read(await root.get_child([f"{namespace_index}:{name}"])))
            for name in ("InspectCount", "OkCount", "NgCount"):
                values[name] = _json_scalar(await self._read(await statistics.get_child([f"{namespace_index}:{name}"])))
            for name in ("LastResult", "LastDefectType", "LastConfidence", "LastImageId", "LastTimestamp", "NgRate"):
                values[name] = _json_scalar(await self._read(await inspection.get_child([f"{namespace_index}:{name}"])))
            status = int(values.pop("DeviceStatus"))
            if not values.get("LastImageId") and str(values.get("LastTimestamp") or "").startswith("2000-01-01"):
                values["LastTimestamp"] = ""
            return {
                "deviceStatus": status,
                "deviceStatusText": {0: "空闲", 1: "运行中", 2: "故障"}.get(status, f"未知({status})"),
                "currentWorkOrderId": str(values.pop("CurrentWorkOrderId") or ""),
                "currentPartSN": str(values.pop("CurrentPartSN") or ""),
                "lastMesOrderNo": str(values.pop("LastMesOrderNo") or ""),
                "lastNgMesOrderNo": str(values.pop("LastNgMesOrderNo") or ""),
                "lastCompletionReceiptNo": str(values.pop("LastCompletionReceiptNo") or ""),
                **values,
            }
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    def read_status(self) -> dict[str, Any]:
        try:
            return asyncio.run(self._read_status_async())
        except StationError:
            raise
        except Exception as exc:
            raise StationError(f"{type(exc).__name__}: {exc}") from exc

    async def _call_method(self, method: str, *args: Any) -> dict[str, Any]:
        client = await self._connect()
        try:
            namespace_index, root = await self._root(client)
            result = await root.call_method(f"{namespace_index}:{method}", *args)
            accepted = bool(_unwrap(result[0])) if result else False
            message = str(_unwrap(result[1])) if len(result) > 1 else ""
            return {"accepted": accepted, "message": message}
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    def start_work_order(self, work_order_id: str, target_count: int) -> dict[str, Any]:
        try:
            return asyncio.run(self._call_method("StartInspection", work_order_id, target_count))
        except Exception as exc:
            raise StationError(f"{type(exc).__name__}: {exc}") from exc

    def complete_work_order(self, work_order_id: str) -> dict[str, Any]:
        try:
            return asyncio.run(self._call_method("CompleteWorkOrder", work_order_id))
        except Exception as exc:
            raise StationError(f"{type(exc).__name__}: {exc}") from exc
