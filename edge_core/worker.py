"""Synchronous delivery worker for the Edge Core Service."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .store import STATE_DEAD, DeliveryStore


class DeliveryWorker:
    def __init__(
        self,
        store: DeliveryStore,
        *,
        mes_base_url: str,
        bearer_token: str = "",
        max_attempts: int = 4,
        retry_delays: tuple[float, ...] = (1.0, 2.0, 4.0),
        request_timeout: float = 3.0,
        poll_interval: float = 0.5,
        lease_seconds: float = 30.0,
        logger=None,
    ):
        self.store = store
        self.mes_base_url = mes_base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.max_attempts = max_attempts
        self.retry_delays = retry_delays
        self.request_timeout = request_timeout
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self.logger = logger or (lambda level, message, data=None: print(f"[{level}] {message} {data or ''}"))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.run_forever, name="edge-core-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.store.recover_expired(max_attempts=self.max_attempts)
                processed = self.run_once()
                if not processed:
                    self._stop.wait(self.poll_interval)
            except Exception as exc:  # keep the edge service alive; errors are logged
                self.logger("error", "worker loop failed", {"error": str(exc)})
                self._stop.wait(self.poll_interval)

    def run_once(self) -> bool:
        delivery = self.store.claim_due(lease_seconds=self.lease_seconds)
        if not delivery:
            return False
        delivery_id = delivery["deliveryId"]
        payload = self.store.get_payload(delivery_id)
        url = self.store.get_url(delivery_id)
        if payload is None or url is None:
            self.store.mark_dead(delivery_id, error_code="PAYLOAD_MISSING", error_message="stored payload missing")
            return True

        try:
            status, response_headers, body = self._post(url, payload, delivery["correlationId"])
        except (TimeoutError, urllib.error.URLError, OSError, ValueError) as exc:
            self._retry_or_dead(delivery_id, "TRANSPORT_ERROR", str(exc))
            return True

        if 200 <= status < 300:
            try:
                self._validate_success(body, delivery["correlationId"])
            except ValueError as exc:
                self.store.mark_dead(
                    delivery_id,
                    error_code="INVALID_RESPONSE_SCHEMA",
                    error_message=str(exc),
                )
                return True
            self.store.mark_acked(delivery_id, body)
            self.logger("info", "MES receipt accepted; waiting for UA write", {
                "deliveryId": delivery_id,
                "correlationId": delivery["correlationId"],
                "receiptNo": body.get("receiptNo"),
            })
            return True

        if status == 429:
            delay = self._retry_after_seconds(response_headers)
            self._retry_or_dead(
                delivery_id,
                "MES_BUSY",
                f"HTTP 429 MES busy; Retry-After={delay:.1f}s",
                delay=delay,
            )
            return True

        if 400 <= status < 500:
            error_code = str(body.get("errorCode") or "MES_HTTP_4XX")
            error_message = str(body.get("error") or body.get("message") or f"HTTP {status}")
            self.store.mark_dead(delivery_id, error_code=error_code, error_message=error_message)
            return True

        error_code = "MES_UNAVAILABLE" if status >= 500 else "HTTP_ERROR"
        error_message = str(body.get("error") or body.get("message") or f"HTTP {status}")
        self._retry_or_dead(delivery_id, error_code, error_message)
        return True

    def _post(self, url: str, payload: dict[str, Any], correlation_id: str) -> tuple[int, Any, dict[str, Any]]:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "content-type": "application/json",
            "accept": "application/json",
            "x-correlation-id": correlation_id,
            "idempotency-key": str(payload.get("sourceEventId") or correlation_id),
        }
        if self.bearer_token:
            headers["authorization"] = f"Bearer {self.bearer_token}"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.request_timeout) as response:
                raw = response.read(1024 * 1024)
                return response.status, response.headers, self._parse_body(raw)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read(1024 * 1024)
                return exc.code, exc.headers, self._parse_body(raw)
            finally:
                exc.close()

    @staticmethod
    def _parse_body(raw: bytes) -> dict[str, Any]:
        try:
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _validate_success(body: dict[str, Any], correlation_id: str) -> None:
        if body.get("accepted") is not True:
            raise ValueError("accepted must be true")
        if body.get("schemaVersion") != "1.0":
            raise ValueError("schemaVersion must be 1.0")
        if body.get("correlationId") != correlation_id:
            raise ValueError("correlationId does not match request")
        receipt_no = body.get("receiptNo")
        if not isinstance(receipt_no, str) or not receipt_no.strip():
            raise ValueError("receiptNo is required")

    @staticmethod
    def _retry_after_seconds(headers) -> float:
        value = headers.get("Retry-After") if headers else None
        if not value:
            return 1.0
        try:
            return max(0.0, min(30.0, float(value)))
        except ValueError:
            try:
                when = parsedate_to_datetime(str(value))
                return max(0.0, min(30.0, when.timestamp() - time.time()))
            except Exception:
                return 1.0

    def _retry_or_dead(
        self,
        delivery_id: str,
        error_code: str,
        error_message: str,
        *,
        delay: float | None = None,
    ) -> None:
        delivery = self.store.get(delivery_id)
        if not delivery:
            return
        attempt_index = max(0, int(delivery["attempts"]) - 1)
        if delay is None:
            delay = self.retry_delays[min(attempt_index, len(self.retry_delays) - 1)]
        self.store.mark_retry(
            delivery_id,
            error_code=error_code,
            error_message=error_message,
            delay_seconds=delay,
            max_attempts=self.max_attempts,
        )
