"""Small dependency-free HTTP API for the Edge Core Service."""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .contract import ContractError, validate_inspection_event
from .store import (
    ConflictError,
    DeliveryStore,
    StateConflictError,
)

_MAX_BODY = 1024 * 1024
_DELIVERY_PATH = re.compile(r"^/v1/deliveries/([0-9a-fA-F-]{36})(/ua-write-result)?$")
_DEAD_PATH = re.compile(r"^/v1/dead-letters/([0-9a-fA-F-]{36})/(replay|acknowledge)$")


class EdgeApiHandler(BaseHTTPRequestHandler):
    server_version = "EdgeCore/0.2"

    @property
    def store(self) -> DeliveryStore:
        return self.server.store  # type: ignore[attr-defined]

    @property
    def api_token(self) -> str:
        return self.server.api_token  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "quiet", False):
            return
        print(f"edge-core-api {self.address_string()} {fmt % args}")

    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {self.api_token}"

    def _send_json(self, status: int, payload: dict[str, Any], extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        correlation = self.headers.get("X-Correlation-Id", "")
        self._send_json(status, {"code": code, "message": message, "correlationId": correlation, "retryable": False})

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        length = int(raw_length)
        if length <= 0 or length > _MAX_BODY:
            raise ValueError("request body missing or too large")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request body must be an object")
        return value

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/v1/health":
            self._send_json(200, {"status": "ok", "database": "ok", "version": "0.2.0"})
            return
        if not self._authorized():
            self._error(401, "UNAUTHORIZED", "missing or invalid bearer token")
            return
        try:
            if path == "/v1/deliveries":
                query = parse_qs(urlparse(self.path).query)
                state = (query.get("state") or [None])[0]
                limit = min(200, max(1, int((query.get("limit") or ["50"])[0])))
                self._send_json(200, {"items": self.store.list_deliveries(state=state, limit=limit), "nextCursor": None})
                return
            if path == "/v1/dead-letters":
                query = parse_qs(urlparse(self.path).query)
                limit = min(200, max(1, int((query.get("limit") or ["50"])[0])))
                self._send_json(200, {"items": self.store.list_dead_letters(limit=limit), "nextCursor": None})
                return
            if path == "/v1/status":
                self._send_json(200, self._status_payload())
                return
            match = _DELIVERY_PATH.match(path)
            if match and not match.group(2):
                delivery = self.store.get(match.group(1))
                if not delivery:
                    self._error(404, "NOT_FOUND", "delivery not found")
                    return
                self._send_json(200, delivery)
                return
            self._error(404, "NOT_FOUND", "route not found")
        except ValueError as exc:
            self._error(400, "INVALID_REQUEST", str(exc))
        except Exception as exc:
            self._error(500, "INTERNAL_ERROR", str(exc))

    def do_POST(self) -> None:
        if not self._authorized():
            self._error(401, "UNAUTHORIZED", "missing or invalid bearer token")
            return
        path = urlparse(self.path).path
        try:
            body = self._read_json()
            if path == "/v1/inspection-events":
                self._ingest(body)
                return
            match = _DELIVERY_PATH.match(path)
            if match and match.group(2):
                self._ua_write_result(match.group(1), body)
                return
            match = _DEAD_PATH.match(path)
            if match:
                self._dead_action(match.group(1), match.group(2), body)
                return
            self._error(404, "NOT_FOUND", "route not found")
        except ContractError as exc:
            self._error(400, exc.code, str(exc))
        except ValueError as exc:
            self._error(400, "INVALID_REQUEST", str(exc))
        except KeyError as exc:
            self._error(404, "NOT_FOUND", f"delivery not found: {exc.args[0]}")
        except ConflictError as exc:
            self._error(409, "PAYLOAD_CONFLICT", str(exc))
        except StateConflictError as exc:
            self._error(409, "STATE_CONFLICT", str(exc))
        except Exception as exc:
            self._error(500, "INTERNAL_ERROR", str(exc))

    def _ingest(self, body: dict[str, Any]) -> None:
        correlation_id = self.headers.get("X-Correlation-Id", "").strip()
        idempotency_key = validate_inspection_event(
            body,
            correlation_header=correlation_id,
            idempotency_key=self.headers.get("Idempotency-Key", "").strip(),
        )
        endpoint = "/mes/ng-order" if body["eventType"] == "NG_ORDER" else "/mes/complete"
        url = self.server.mes_base_url + endpoint  # type: ignore[attr-defined]
        delivery, duplicate = self.store.enqueue(
            body,
            url=url,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
        )
        self._send_json(202, delivery, {"Idempotent-Replay": "true" if duplicate else "false"})

    def _ua_write_result(self, delivery_id: str, body: dict[str, Any]) -> None:
        if "success" not in body:
            raise ValueError("success is required")
        correlation_id = str(body.get("correlationId") or self.headers.get("X-Correlation-Id") or "")
        if not correlation_id:
            raise ValueError("correlationId is required")
        delivery = self.store.mark_ua_write_result(
            delivery_id,
            success=bool(body["success"]),
            correlation_id=correlation_id,
            error_code=body.get("errorCode"),
            error_message=body.get("errorMessage"),
        )
        self._send_json(200, delivery)

    def _dead_action(self, delivery_id: str, action: str, body: dict[str, Any]) -> None:
        if action == "replay":
            actor = str(body.get("actor") or "")
            reason = str(body.get("reason") or "")
            if not actor or not reason:
                raise ValueError("actor and reason are required")
            self._send_json(202, self.store.replay_dead(delivery_id, actor=actor, reason=reason))
            return
        actor = str(body.get("actor") or "")
        resolution = str(body.get("resolution") or "")
        if not actor or not resolution:
            raise ValueError("actor and resolution are required")
        self._send_json(200, self.store.acknowledge_dead(delivery_id, actor=actor, resolution=resolution))

    def _status_payload(self) -> dict[str, Any]:
        metrics = self.store.metrics_snapshot()
        counts = metrics["counts"]
        return {
            "health": {"status": "ok", "database": "ok", "version": "0.2.0"},
            "queue": {
                "total": metrics["queueTotal"],
                "pending": counts.get("PENDING", 0),
                "sending": counts.get("SENDING", 0),
                "retryWait": counts.get("RETRY_WAIT", 0),
                "oldestAgeSeconds": metrics["oldestQueueAgeSeconds"],
            },
            "deadLetters": {
                "total": metrics["deadTotal"],
                "oldestAgeSeconds": metrics["oldestDeadAgeSeconds"],
            },
            "mes": {
                "successTotal": metrics["mesSuccessTotal"],
                "failureTotal": metrics["mesFailureTotal"],
                "lastSuccessAt": metrics["lastMesSuccessAt"],
                "lastFailureAt": metrics["lastMesFailureAt"],
            },
            "ua": {
                "writeSuccessTotal": metrics["uaWriteSuccessTotal"],
                "writeFailureTotal": metrics["uaWriteFailureTotal"],
                "lastWriteSuccessAt": metrics["lastUaWriteSuccessAt"],
            },
        }


def create_api_server(host: str, port: int, store: DeliveryStore, api_token: str, mes_base_url: str) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), EdgeApiHandler)
    server.store = store  # type: ignore[attr-defined]
    server.api_token = api_token  # type: ignore[attr-defined]
    server.mes_base_url = mes_base_url  # type: ignore[attr-defined]
    server.quiet = False  # type: ignore[attr-defined]
    return server
