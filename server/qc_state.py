"""Durable local state for the QC OPC UA server.

The state file is intentionally simple: one JSON snapshot written atomically via
fsync + os.replace. It is the recovery baseline for a single edge station, not a
multi-writer database.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
OUTBOX_EVENT_VERSION = 1
OUTBOX_EVENT_TYPES = frozenset({"ng", "done"})
VALID_STATUS = (0, 1, 2)
_SNAPSHOT_KEYS = {
    "schema_version": "schemaVersion",
    "status": "status",
    "work_order_id": "workOrderId",
    "target_count": "targetCount",
    "inspect_count": "inspectCount",
    "ok_count": "okCount",
    "ng_count": "ngCount",
    "window": "window",
    "current_part_sn": "currentPartSn",
    "sn_day": "snDay",
    "sn_seq": "snSeq",
    "last_result": "lastResult",
    "last_defect": "lastDefect",
    "last_confidence": "lastConfidence",
    "last_image_id": "lastImageId",
    "last_timestamp": "lastTimestamp",
    "outbox": "outbox",
}
_log = logging.getLogger("qc_state")


@dataclass
class QCStationSnapshot:
    schema_version: int = SCHEMA_VERSION
    status: int = 0
    work_order_id: str = ""
    target_count: int = 0
    inspect_count: int = 0
    ok_count: int = 0
    ng_count: int = 0
    window: list[bool] = field(default_factory=list)
    current_part_sn: str = ""
    sn_day: str = ""
    sn_seq: int = 0
    last_result: bool = False
    last_defect: str = ""
    last_confidence: float = 0.0
    last_image_id: str = ""
    last_timestamp: str = ""
    outbox: list[QCOutboxEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Keep snapshots constructed with raw dictionaries usable by callers.
        self.outbox = [
            item if isinstance(item, QCOutboxEvent) else QCOutboxEvent(**item)
            for item in (self.outbox or [])
        ]


@dataclass
class QCOutboxEvent:
    """A durable OPC UA event that requires consumer acknowledgement."""

    event_version: int = OUTBOX_EVENT_VERSION
    event_id: str = ""
    event_type: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    severity: int = 0
    message: str = ""
    created_at: str = ""


class QCStateStore:
    def __init__(self, path: str | Path, *, window_size: int = 20):
        self.path = Path(path).expanduser()
        self.window_size = max(1, int(window_size))

    def load(self) -> QCStationSnapshot:
        if not self.path.exists():
            return QCStationSnapshot()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return self._validate(value)
        except Exception as exc:
            _log.warning("Ignoring unreadable QC state %s: %s", self.path, exc)
            return QCStationSnapshot()

    def save(self, snapshot: QCStationSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        value = {
            _SNAPSHOT_KEYS[key]: item
            for key, item in asdict(snapshot).items()
        }
        value["outbox"] = [self._serialize_outbox_event(event) for event in snapshot.outbox]
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        temp = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}")
        try:
            descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
        except Exception:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
            raise
        try:
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass

    @staticmethod
    def _serialize_outbox_event(event: QCOutboxEvent) -> dict[str, Any]:
        return {
            "eventVersion": event.event_version,
            "eventId": event.event_id,
            "eventType": event.event_type,
            "fields": event.fields,
            "severity": event.severity,
            "message": event.message,
            "createdAt": event.created_at,
        }

    def _validate(self, value: Any) -> QCStationSnapshot:
        if not isinstance(value, dict):
            raise ValueError("state root must be an object")
        schema_version = _as_int(value.get("schemaVersion", value.get("schema_version", 0)), 0)
        if schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported state schemaVersion={schema_version}")

        status = _as_int(value.get("status"), 0)
        if status not in VALID_STATUS:
            status = 0
        work_order_id = _as_text(value.get("workOrderId", value.get("work_order_id", ""))).strip()
        if status == 1 and not work_order_id:
            status = 0

        inspect_count = max(0, _as_int(value.get("inspectCount", value.get("inspect_count", 0)), 0))
        ok_count = max(0, _as_int(value.get("okCount", value.get("ok_count", 0)), 0))
        ng_count = max(0, _as_int(value.get("ngCount", value.get("ng_count", 0)), 0))
        if ok_count + ng_count > inspect_count:
            ng_count = max(0, inspect_count - ok_count)

        raw_window = value.get("window", [])
        window = [bool(item) for item in raw_window] if isinstance(raw_window, list) else []
        window = window[-self.window_size:]

        confidence = _as_float(value.get("lastConfidence", value.get("last_confidence", 0.0)), 0.0)
        confidence = min(1.0, max(0.0, confidence))

        return QCStationSnapshot(
            status=status,
            work_order_id=work_order_id,
            target_count=max(0, _as_int(value.get("targetCount", value.get("target_count", 0)), 0)),
            inspect_count=inspect_count,
            ok_count=ok_count,
            ng_count=ng_count,
            window=window,
            current_part_sn=_as_text(value.get("currentPartSn", value.get("current_part_sn", ""))),
            sn_day=_as_text(value.get("snDay", value.get("sn_day", ""))),
            sn_seq=max(0, _as_int(value.get("snSeq", value.get("sn_seq", 0)), 0)),
            last_result=bool(value.get("lastResult", value.get("last_result", False))),
            last_defect=_as_text(value.get("lastDefect", value.get("last_defect", ""))),
            last_confidence=confidence,
            last_image_id=_as_text(value.get("lastImageId", value.get("last_image_id", ""))),
            last_timestamp=_as_text(value.get("lastTimestamp", value.get("last_timestamp", ""))),
            outbox=self._validate_outbox(value.get("outbox", [])),
        )

    def _validate_outbox(self, raw: Any) -> list[QCOutboxEvent]:
        if raw is None:
            return []
        if not isinstance(raw, list):
            _log.warning("Ignoring unreadable QC outbox: expected a list")
            return []

        events: list[QCOutboxEvent] = []
        seen_ids: set[str] = set()
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                _log.warning("Ignoring malformed QC outbox item #%d", index)
                continue
            version = _as_int(item.get("eventVersion", item.get("event_version", 0)), 0)
            event_id = _as_text(item.get("eventId", item.get("event_id", ""))).strip()
            event_type = _as_text(item.get("eventType", item.get("event_type", ""))).strip()
            fields = item.get("fields", {})
            if (
                version != OUTBOX_EVENT_VERSION
                or not event_id
                or event_id in seen_ids
                or event_type not in OUTBOX_EVENT_TYPES
                or not isinstance(fields, dict)
            ):
                _log.warning("Ignoring invalid QC outbox item #%d", index)
                continue
            try:
                normalized_fields = json.loads(json.dumps(fields, ensure_ascii=False))
            except (TypeError, ValueError):
                _log.warning("Ignoring non-serializable QC outbox item #%d", index)
                continue
            seen_ids.add(event_id)
            events.append(
                QCOutboxEvent(
                    event_version=version,
                    event_id=event_id,
                    event_type=event_type,
                    fields=normalized_fields,
                    severity=min(1000, max(0, _as_int(item.get("severity"), 1))),
                    message=_as_text(item.get("message", "")),
                    created_at=_as_text(item.get("createdAt", item.get("created_at", ""))),
                )
            )
        return events


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def last_timestamp_or_default(raw: str) -> datetime:
    if raw:
        try:
            value = datetime.fromisoformat(raw)
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime(2000, 1, 1, tzinfo=timezone.utc)
