"""OPC UA receipt writeback adapter for Edge Core deliveries."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from asyncua import Client, ua
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256

REPO_ROOT = Path(__file__).resolve().parents[1]


class UaWritebackError(RuntimeError):
    """The UA write could not be confirmed."""


class EdgeApiError(RuntimeError):
    """The Edge Core API request failed."""


@dataclass(frozen=True)
class UaWritebackConfig:
    edge_api_base_url: str
    edge_api_token: str
    ua_endpoint: str
    client_cert: Path
    client_key: Path
    server_cert: Path
    admin_user: str
    admin_pass: str
    namespace_uri: str = "urn:fde:linqinspect:qc"
    station_node_name: str = "QCStation"
    receipt_method_name: str = "RecordMesReceipt"
    request_timeout: float = 5.0
    batch_limit: int = 50
    application_uri: str = "urn:freeopcua:python:client"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "UaWritebackConfig":
        values = os.environ if env is None else env
        token = values.get("EDGE_API_TOKEN", "").strip()
        username = values.get("UA_ADMIN_USER", "").strip()
        password = values.get("UA_ADMIN_PASS", "")
        if not token:
            raise ValueError("EDGE_API_TOKEN is required")
        if not username or not password:
            raise ValueError("UA_ADMIN_USER and UA_ADMIN_PASS are required")
        return cls(
            edge_api_base_url=values.get("EDGE_API_BASE_URL", "http://127.0.0.1:8770").rstrip("/"),
            edge_api_token=token,
            ua_endpoint=values.get("UA_ENDPOINT", "opc.tcp://127.0.0.1:4840"),
            client_cert=Path(values.get("UA_CLIENT_CERT", REPO_ROOT / "certs/client/client_cert.der")).expanduser(),
            client_key=Path(values.get("UA_CLIENT_KEY", REPO_ROOT / "certs/client/client_key.pem")).expanduser(),
            server_cert=Path(values.get("UA_SERVER_CERT", REPO_ROOT / "certs/server/server_cert.der")).expanduser(),
            admin_user=username,
            admin_pass=password,
            namespace_uri=values.get("QC_NAMESPACE_URI", "urn:fde:linqinspect:qc"),
            station_node_name=values.get("QC_STATION_NODE_NAME", "QCStation"),
            receipt_method_name=values.get("QC_RECEIPT_METHOD_NAME", "RecordMesReceipt"),
            request_timeout=max(0.2, float(values.get("UA_WRITEBACK_TIMEOUT_SECONDS", "5"))),
            batch_limit=min(200, max(1, int(values.get("UA_WRITEBACK_BATCH_LIMIT", "50")))),
            application_uri=values.get("UA_CLIENT_APPLICATION_URI", "urn:freeopcua:python:client"),
        )


class EdgeCoreApiClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers={
                "authorization": f"Bearer {self.token}",
                "accept": "application/json",
                **({"content-type": "application/json"} if body is not None else {}),
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(2 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(1024 * 1024)
                detail = raw.decode("utf-8", errors="replace")
            finally:
                exc.close()
            raise EdgeApiError(f"Edge API HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise EdgeApiError(f"Edge API request failed: {exc}") from exc
        try:
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EdgeApiError("Edge API returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise EdgeApiError("Edge API response must be an object")
        return value

    def list_ua_pending(self, limit: int = 50) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"state": "UA_WRITE_PENDING", "limit": limit})
        payload = self._request("GET", f"/v1/deliveries?{query}")
        items = payload.get("items")
        if not isinstance(items, list):
            raise EdgeApiError("Edge API delivery page is missing items")
        return [item for item in items if isinstance(item, dict)]

    def report_result(
        self,
        delivery_id: str,
        *,
        success: bool,
        correlation_id: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/deliveries/{urllib.parse.quote(delivery_id, safe='')}/ua-write-result",
            {
                "success": success,
                "correlationId": correlation_id,
                "errorCode": error_code,
                "errorMessage": error_message,
            },
        )


class UaReceiptWriter:
    def __init__(self, config: UaWritebackConfig):
        self.config = config

    async def _connect(self) -> Client:
        for path in (self.config.client_cert, self.config.client_key, self.config.server_cert):
            if not path.is_file():
                raise UaWritebackError(f"missing UA certificate/key: {path}")
        client = Client(self.config.ua_endpoint, timeout=self.config.request_timeout)
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

    @staticmethod
    def _unwrap(value: Any) -> Any:
        while isinstance(value, ua.Variant):
            value = value.Value
        return value

    async def write(self, delivery: dict[str, Any]) -> str:
        receipt = delivery.get("mesReceipt")
        if not isinstance(receipt, dict):
            raise UaWritebackError("MES_RECEIPT_MISSING")
        receipt_type = str(receipt.get("receiptType") or "")
        receipt_no = str(receipt.get("receiptNo") or "").strip()
        correlation_id = str(receipt.get("correlationId") or delivery.get("correlationId") or "").strip()
        work_order_id = str(delivery.get("workOrderId") or "").strip()
        if receipt_type not in ("MES_ORDER", "COMPLETION_RECEIPT"):
            raise UaWritebackError("INVALID_RECEIPT_TYPE")
        if not receipt_no:
            raise UaWritebackError("MISSING_RECEIPT_NO")
        if not correlation_id:
            raise UaWritebackError("MISSING_CORRELATION_ID")
        if not work_order_id:
            raise UaWritebackError("MISSING_WORK_ORDER_ID")
        kind = "ng" if receipt_type == "MES_ORDER" else "complete"
        client = await self._connect()
        try:
            namespace_index = await client.get_namespace_index(self.config.namespace_uri)
            station = await client.nodes.objects.get_child(
                [f"{namespace_index}:{self.config.station_node_name}"]
            )
            result = await station.call_method(
                f"{namespace_index}:{self.config.receipt_method_name}",
                kind,
                receipt_no,
                work_order_id,
                correlation_id,
            )
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
        accepted = self._unwrap(result[0]) if result else None
        summary = self._unwrap(result[1]) if len(result) > 1 else ""
        if accepted is not True:
            raise UaWritebackError(f"UA_RECEIPT_REJECTED: {summary}")
        return str(summary or "accepted")


async def process_pending(
    api: EdgeCoreApiClient,
    writer: UaReceiptWriter,
    *,
    limit: int = 50,
) -> dict[str, int]:
    deliveries = api.list_ua_pending(limit=limit)
    confirmed = 0
    failed = 0
    for delivery in deliveries:
        delivery_id = str(delivery.get("deliveryId") or "")
        correlation_id = str(delivery.get("correlationId") or "")
        if not delivery_id or not correlation_id:
            failed += 1
            continue
        try:
            await writer.write(delivery)
        except Exception as exc:
            failed += 1
            try:
                api.report_result(
                    delivery_id,
                    success=False,
                    correlation_id=correlation_id,
                    error_code="UA_WRITE_FAILED",
                    error_message=str(exc),
                )
            except Exception:
                pass
            continue
        try:
            api.report_result(delivery_id, success=True, correlation_id=correlation_id)
            confirmed += 1
        except Exception:
            failed += 1
    return {"listed": len(deliveries), "confirmed": confirmed, "failed": failed}
