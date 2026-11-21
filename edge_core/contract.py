"""Validation rules for the MES-facing connector contract.

The API and delivery worker share these rules so a request cannot be accepted
under one schema and later dispatched under another.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

SCHEMA_VERSION = "1.0"
EVENT_NG_ORDER = "NG_ORDER"
EVENT_COMPLETION_REPORT = "COMPLETION_REPORT"
EVENT_TYPES = frozenset({EVENT_NG_ORDER, EVENT_COMPLETION_REPORT})

CORRELATION_ID_RE = re.compile(r"^corr-[A-Za-z0-9._:-]{8,180}$")
_ALLOWED_FIELDS = frozenset(
    {
        "schemaVersion",
        "sourceEventId",
        "stationId",
        "occurredAt",
        "eventType",
        "result",
        "serialNumber",
        "workOrderId",
        "defectType",
        "confidence",
        "imageId",
        "inspectCount",
        "okCount",
        "ngCount",
        "ngRate",
        "correlationId",
        "metadata",
    }
)
_NG_REQUIRED = ("workOrderId", "serialNumber", "defectType")
_COMPLETION_REQUIRED = ("workOrderId", "inspectCount", "okCount", "ngCount", "ngRate")


class ContractError(ValueError):
    """Stable request or response contract violation."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _text(body: dict[str, Any], name: str, *, max_length: int) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ContractError("INVALID_FIELD", f"{name} must be a non-empty string")
    value = value.strip()
    if len(value) > max_length:
        raise ContractError("INVALID_FIELD", f"{name} exceeds {max_length} characters")
    return value


def _integer(body: dict[str, Any], name: str) -> int:
    value = body.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError("INVALID_COUNT", f"{name} must be a non-negative integer")
    return value


def _number(body: dict[str, Any], name: str, minimum: float, maximum: float) -> float:
    value = body.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError("INVALID_NUMBER", f"{name} must be a number")
    value = float(value)
    if value < minimum or value > maximum:
        raise ContractError("INVALID_NUMBER", f"{name} must be between {minimum} and {maximum}")
    return value


def _validate_occurred_at(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ContractError("INVALID_TIMESTAMP", "occurredAt must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("INVALID_TIMESTAMP", "occurredAt must include a UTC offset")


def validate_inspection_event(
    body: dict[str, Any],
    *,
    correlation_header: str,
    idempotency_key: str,
) -> str:
    """Validate an ingest request and return the canonical idempotency key."""
    unknown = sorted(set(body) - _ALLOWED_FIELDS)
    if unknown:
        raise ContractError("UNKNOWN_FIELD", "unsupported fields: " + ", ".join(unknown))

    required = ("schemaVersion", "sourceEventId", "stationId", "eventType", "occurredAt", "correlationId")
    missing = [name for name in required if name not in body]
    if missing:
        raise ContractError("MISSING_FIELD", "missing fields: " + ", ".join(missing))

    if body.get("schemaVersion") != SCHEMA_VERSION:
        raise ContractError("UNSUPPORTED_SCHEMA_VERSION", "schemaVersion must be 1.0")

    correlation_id = _text(body, "correlationId", max_length=200)
    if not CORRELATION_ID_RE.fullmatch(correlation_id):
        raise ContractError("INVALID_CORRELATION_ID", "correlationId has an invalid format")
    if not correlation_header.strip():
        raise ContractError("CORRELATION_MISMATCH", "X-Correlation-Id header is required")
    if correlation_header.strip() != correlation_id:
        raise ContractError("CORRELATION_MISMATCH", "X-Correlation-Id must match body.correlationId")

    try:
        canonical_key = str(uuid.UUID(idempotency_key))
    except (ValueError, AttributeError) as exc:
        raise ContractError("INVALID_IDEMPOTENCY_KEY", "Idempotency-Key must be a UUID") from exc

    _text(body, "sourceEventId", max_length=200)
    _text(body, "stationId", max_length=100)
    _validate_occurred_at(_text(body, "occurredAt", max_length=100))

    event_type = body.get("eventType")
    if event_type not in EVENT_TYPES:
        raise ContractError("INVALID_EVENT_TYPE", "eventType must be NG_ORDER or COMPLETION_REPORT")

    if "result" in body and body["result"] not in ("OK", "NG"):
        raise ContractError("INVALID_RESULT", "result must be OK or NG")
    for name, limit in (("serialNumber", 200), ("workOrderId", 200), ("defectType", 100), ("imageId", 500)):
        if body.get(name) is not None:
            _text(body, name, max_length=limit)
    if body.get("confidence") is not None:
        _number(body, "confidence", 0.0, 1.0)
    if body.get("metadata") is not None and not isinstance(body["metadata"], dict):
        raise ContractError("INVALID_FIELD", "metadata must be an object")

    if event_type == EVENT_NG_ORDER:
        if body.get("result") != "NG":
            raise ContractError("INVALID_RESULT", "NG_ORDER requires result=NG")
        missing = [name for name in _NG_REQUIRED if not body.get(name)]
        if missing:
            raise ContractError("MISSING_FIELD", "missing fields for NG_ORDER: " + ", ".join(missing))
    else:
        missing = [name for name in _COMPLETION_REQUIRED if name not in body]
        if missing:
            raise ContractError(
                "MISSING_FIELD",
                "missing fields for COMPLETION_REPORT: " + ", ".join(missing),
            )
        inspect_count = _integer(body, "inspectCount")
        ok_count = _integer(body, "okCount")
        ng_count = _integer(body, "ngCount")
        ng_rate = _number(body, "ngRate", -1.0, 1.0)
        if ok_count + ng_count > inspect_count:
            raise ContractError("INVALID_COUNT", "okCount + ngCount must not exceed inspectCount")
        if inspect_count == 0 and ng_rate != -1.0:
            raise ContractError("INVALID_NG_RATE", "ngRate must be -1 when inspectCount is zero")
        if inspect_count > 0 and ng_rate < 0:
            raise ContractError("INVALID_NG_RATE", "ngRate must be non-negative when samples exist")

    return canonical_key


def validate_mes_success(
    body: dict[str, Any],
    *,
    correlation_id: str,
    event_type: str,
) -> dict[str, Any]:
    """Validate and normalize a successful MES response."""
    if body.get("schemaVersion") != SCHEMA_VERSION:
        raise ContractError("INVALID_RESPONSE_SCHEMA", "schemaVersion must be 1.0")
    if body.get("correlationId") != correlation_id:
        raise ContractError("CORRELATION_MISMATCH", "correlationId does not match request")
    if body.get("accepted") is not True:
        raise ContractError("MES_REJECTED", "accepted must be true")
    if "duplicated" in body and not isinstance(body["duplicated"], bool):
        raise ContractError("INVALID_RESPONSE_SCHEMA", "duplicated must be boolean")

    if event_type == EVENT_NG_ORDER:
        receipt = body.get("mesOrderNo")
        receipt_type = "MES_ORDER"
        missing_code = "MISSING_MES_ORDER_NO"
    elif event_type == EVENT_COMPLETION_REPORT:
        receipt = body.get("receiptNo")
        receipt_type = "COMPLETION_RECEIPT"
        missing_code = "MISSING_RECEIPT_NO"
    else:
        raise ContractError("INVALID_EVENT_TYPE", f"unsupported eventType: {event_type}")

    receipt_field = "mesOrderNo" if event_type == EVENT_NG_ORDER else "receiptNo"
    if not isinstance(receipt, str) or not receipt.strip():
        raise ContractError(missing_code, f"{receipt_field} is required")
    receipt = receipt.strip()
    if len(receipt) > 200:
        raise ContractError("INVALID_RECEIPT", "MES receipt number exceeds 200 characters")

    normalized = dict(body)
    normalized["receiptType"] = receipt_type
    normalized["receiptNo"] = receipt
    if event_type == EVENT_NG_ORDER:
        normalized["mesOrderNo"] = receipt
    return normalized
