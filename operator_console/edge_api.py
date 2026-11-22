"""Small Edge Core API client used by the operator console."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class EdgeApiError(RuntimeError):
    """Edge Core API request failed."""


class OperatorEdgeClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={
                "authorization": f"Bearer {self.token}",
                "accept": "application/json",
                **({"content-type": "application/json"} if data is not None else {}),
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(4 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(1024 * 1024).decode("utf-8", errors="replace")
            finally:
                exc.close()
            raise EdgeApiError(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise EdgeApiError(f"{type(exc).__name__}: {exc}") from exc
        try:
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EdgeApiError("Edge Core returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise EdgeApiError("Edge Core response must be an object")
        return value

    def status(self) -> dict[str, Any]:
        return self.request("GET", "/v1/status")

    def deliveries(self, *, limit: int = 30, state: str = "") -> dict[str, Any]:
        query = {"limit": str(limit)}
        if state:
            query["state"] = state
        return self.request("GET", "/v1/deliveries?" + urllib.parse.urlencode(query))

    def dead_letters(self, *, limit: int = 30) -> dict[str, Any]:
        return self.request("GET", "/v1/dead-letters?" + urllib.parse.urlencode({"limit": limit}))

    def evidence(self, delivery_id: str) -> dict[str, Any]:
        return self.request(
            "GET",
            f"/v1/deliveries/{urllib.parse.quote(delivery_id, safe='')}/evidence",
        )

    def replay_dead_letter(self, delivery_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/v1/dead-letters/{urllib.parse.quote(delivery_id, safe='')}/replay",
            {"actor": actor, "reason": reason},
        )

    def acknowledge_dead_letter(
        self,
        delivery_id: str,
        *,
        actor: str,
        resolution: str,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/v1/dead-letters/{urllib.parse.quote(delivery_id, safe='')}/acknowledge",
            {"actor": actor, "resolution": resolution},
        )
