"""HTTP server for the local operator console."""

from __future__ import annotations

import base64
import json
import re
import secrets
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .edge_api import EdgeApiError, OperatorEdgeClient
from .station import StationError, StationGateway

STATIC_DIR = Path(__file__).resolve().parent / "static"
_DEAD_ACTION = re.compile(r"^/api/dead-letters/([0-9a-fA-F-]{36})/(replay|acknowledge)$")
_EVIDENCE_PATH = re.compile(r"^/api/evidence/([0-9a-fA-F-]{36})$")
_EVIDENCE_IMAGE_PATH = re.compile(r"^/api/evidence/([0-9a-fA-F-]{36})/image$")
_MAX_BODY = 64 * 1024


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class OperatorHandler(BaseHTTPRequestHandler):
    server_version = "QCOperator/0.1"

    @property
    def station(self) -> StationGateway:
        return self.server.station  # type: ignore[attr-defined]

    @property
    def edge(self) -> OperatorEdgeClient:
        return self.server.edge  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "quiet", False):
            return
        print(f"operator-console {self.address_string()} {fmt % args}")

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        user, separator, password = decoded.partition(":")
        if not separator:
            return False
        expected_user = str(self.server.operator_user)  # type: ignore[attr-defined]
        expected_password = str(self.server.operator_password)  # type: ignore[attr-defined]
        return secrets.compare_digest(user.encode("utf-8"), expected_user.encode("utf-8")) and secrets.compare_digest(
            password.encode("utf-8"), expected_password.encode("utf-8")
        )

    def _send(
        self,
        status: int,
        body: bytes,
        *,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("x-frame-options", "DENY")
        self.send_header("referrer-policy", "no-referrer")
        self.send_header(
            "content-security-policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            content_type="application/json; charset=utf-8",
            headers=headers,
        )

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"ok": False, "code": code, "message": message})

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > _MAX_BODY:
            raise ValueError("request body missing or too large")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request body must be an object")
        return value

    def _serve_asset(self, name: str, content_type: str) -> None:
        path = STATIC_DIR / name
        if not path.is_file():
            self._error(404, "NOT_FOUND", "asset not found")
            return
        self._send(200, path.read_bytes(), content_type=content_type)

    def _station_snapshot(self) -> dict[str, Any]:
        try:
            return {"available": True, "data": self.station.read_status()}
        except Exception as exc:
            return {"available": False, "error": f"{type(exc).__name__}: {exc}"}

    def _edge_call(self, operation) -> dict[str, Any]:
        try:
            return {"available": True, "data": operation()}
        except Exception as exc:
            return {"available": False, "error": f"{type(exc).__name__}: {exc}"}

    def _status_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "generatedAt": now_utc(),
            "station": self._station_snapshot(),
            "edge": self._edge_call(self.edge.status),
        }

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._json(200, {"ok": True, "service": "qc-operator-console", "at": now_utc()})
            return
        if not self._authorized():
            self._json(
                401,
                {"ok": False, "code": "UNAUTHORIZED", "message": "HTTP Basic authentication required"},
                {"www-authenticate": 'Basic realm="QC Operator Console"'},
            )
            return
        try:
            if parsed.path in ("/", "/index.html"):
                self._serve_asset("index.html", "text/html; charset=utf-8")
                return
            if parsed.path == "/app.js":
                self._serve_asset("app.js", "text/javascript; charset=utf-8")
                return
            if parsed.path == "/styles.css":
                self._serve_asset("styles.css", "text/css; charset=utf-8")
                return
            if parsed.path == "/api/status":
                self._json(200, self._status_payload())
                return
            if parsed.path == "/api/deliveries":
                query = parse_qs(parsed.query)
                limit = min(200, max(1, int((query.get("limit") or ["30"])[0])))
                state = str((query.get("state") or [""])[0])
                result = self.edge.deliveries(limit=limit, state=state)
                self._json(200, {"ok": True, "data": result})
                return
            if parsed.path == "/api/dead-letters":
                query = parse_qs(parsed.query)
                limit = min(200, max(1, int((query.get("limit") or ["30"])[0])))
                result = self.edge.dead_letters(limit=limit)
                self._json(200, {"ok": True, "data": result})
                return
            if parsed.path == "/api/evidence/search":
                query = parse_qs(parsed.query)
                search = str((query.get("q") or [""])[0]).strip()
                limit = min(200, max(1, int((query.get("limit") or ["20"])[0])))
                result = self.edge.search_evidence(search, limit=limit)
                self._json(200, {"ok": True, "data": result})
                return
            image_match = _EVIDENCE_IMAGE_PATH.match(parsed.path)
            if image_match:
                evidence = self.edge.evidence(image_match.group(1))
                image_id = str(evidence.get("inspection", {}).get("imageId") or "")
                if not image_id or not evidence.get("completeness", {}).get("imageBinaryStored"):
                    self._error(404, "IMAGE_NOT_FOUND", "delivery has no stored evidence image")
                    return
                content_type, content = self.edge.evidence_image(image_id)
                self._send(
                    200,
                    content,
                    content_type=content_type,
                    headers={"content-disposition": 'inline; filename="quality-evidence-image"'},
                )
                return
            match = _EVIDENCE_PATH.match(parsed.path)
            if match:
                result = self.edge.evidence(match.group(1))
                self._json(200, {"ok": True, "data": result})
                return
            if parsed.path == "/api/report":
                report = {
                    "schemaVersion": "1.0",
                    "generatedAt": now_utc(),
                    "station": self._station_snapshot(),
                    "edge": self._edge_call(self.edge.status),
                    "deliveries": self._edge_call(lambda: self.edge.deliveries(limit=200)),
                    "deadLetters": self._edge_call(lambda: self.edge.dead_letters(limit=200)),
                }
                body = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                self._send(
                    200,
                    body,
                    content_type="application/json; charset=utf-8",
                    headers={"content-disposition": f'attachment; filename="qc-operator-{stamp}.json"'},
                )
                return
            self._error(404, "NOT_FOUND", "route not found")
        except ValueError as exc:
            self._error(400, "INVALID_REQUEST", str(exc))
        except (EdgeApiError, StationError) as exc:
            self._error(502, "UPSTREAM_ERROR", str(exc))
        except Exception as exc:
            self._error(500, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")

    def do_POST(self) -> None:
        if not self._authorized():
            self._json(
                401,
                {"ok": False, "code": "UNAUTHORIZED", "message": "HTTP Basic authentication required"},
                {"www-authenticate": 'Basic realm="QC Operator Console"'},
            )
            return
        if self.headers.get("X-Operator-Request") != "1":
            self._error(403, "CSRF_GUARD", "X-Operator-Request: 1 header is required")
            return
        path = urlparse(self.path).path
        try:
            body = self._read_json()
            if path == "/api/work-orders/start":
                work_order = str(body.get("workOrderId") or "").strip()
                target = body.get("targetCount")
                if not work_order or len(work_order) > 200:
                    raise ValueError("workOrderId must be a non-empty string up to 200 characters")
                if isinstance(target, bool) or not isinstance(target, int) or not 1 <= target <= 100000:
                    raise ValueError("targetCount must be an integer between 1 and 100000")
                result = self.station.start_work_order(work_order, target)
                self._json(200 if result.get("accepted") else 409, {"ok": bool(result.get("accepted")), **result})
                return
            if path == "/api/work-orders/complete":
                work_order = str(body.get("workOrderId") or "").strip()
                if not work_order or len(work_order) > 200:
                    raise ValueError("workOrderId must be a non-empty string up to 200 characters")
                result = self.station.complete_work_order(work_order)
                self._json(200 if result.get("accepted") else 409, {"ok": bool(result.get("accepted")), **result})
                return
            match = _DEAD_ACTION.match(path)
            if match:
                delivery_id, action = match.groups()
                actor = str(body.get("actor") or "").strip()
                reason = str(body.get("reason") or body.get("resolution") or "").strip()
                if not actor or not reason:
                    raise ValueError("actor and reason/resolution are required")
                if action == "replay":
                    result = self.edge.replay_dead_letter(delivery_id, actor=actor, reason=reason)
                else:
                    result = self.edge.acknowledge_dead_letter(
                        delivery_id, actor=actor, resolution=reason
                    )
                self._json(200, {"ok": True, "data": result})
                return
            self._error(404, "NOT_FOUND", "route not found")
        except json.JSONDecodeError as exc:
            self._error(400, "INVALID_JSON", str(exc))
        except ValueError as exc:
            self._error(400, "INVALID_REQUEST", str(exc))
        except (EdgeApiError, StationError) as exc:
            self._error(502, "UPSTREAM_ERROR", str(exc))
        except Exception as exc:
            self._error(500, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")


def create_operator_server(
    host: str,
    port: int,
    station: StationGateway,
    edge: OperatorEdgeClient,
    *,
    username: str,
    password: str,
) -> ThreadingHTTPServer:
    if not username or not password:
        raise ValueError("operator console username and password are required")
    server = ThreadingHTTPServer((host, port), OperatorHandler)
    server.station = station  # type: ignore[attr-defined]
    server.edge = edge  # type: ignore[attr-defined]
    server.operator_user = username  # type: ignore[attr-defined]
    server.operator_password = password  # type: ignore[attr-defined]
    server.quiet = False  # type: ignore[attr-defined]
    return server
