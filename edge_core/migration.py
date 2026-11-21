"""Offline migration from the Node-RED queue to Edge Core delivery storage.

The migration is deliberately not a dual-writer. Stop the Node-RED send pump,
run this importer against a point-in-time database, then start exactly one
sender. Existing delivery IDs are mapped to deterministic UUIDs while the
original Node-RED queue ID is retained as the business key.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import DeliveryStore, STATE_DEAD, STATE_PENDING, STATE_UA_WRITE_PENDING

MIGRATION_NAMESPACE = uuid.UUID("cb3f59c2-7e01-4e11-b8f3-c74740dd8ee4")
REQUIRED_COLUMNS = {
    "queue": {
        "id", "type", "correlation_id", "url", "body_json", "created_at",
        "attempts", "next_at", "status", "sent_at", "last_status",
        "last_error_code", "enqueued_at",
    },
    "dead_letters": {
        "queue_id", "type", "correlation_id", "url", "body_json", "created_at",
        "attempts", "next_at", "status", "sent_at", "last_status",
        "last_error_code", "error_code", "failed_at",
    },
    "ua_writebacks": {
        "queue_id", "status", "receipt", "attempts", "next_at", "started_at",
        "last_error_code", "created_at", "updated_at",
    },
}


class MigrationError(RuntimeError):
    """The source or target cannot be migrated safely."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _iso_from_ms(value: Any, fallback: str | None = None) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback or datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    if number <= 0:
        return fallback or datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return datetime.fromtimestamp(number / 1000, tz=timezone.utc).isoformat(timespec="milliseconds")


def _iso_from_text(value: Any, fallback: str | None = None) -> str:
    text = str(value or "").strip()
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")
        except ValueError:
            pass
    return fallback or datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def inspect_source(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise MigrationError(f"source database does not exist: {path}")
    try:
        connection = _readonly(path)
    except sqlite3.Error as exc:
        raise MigrationError(f"cannot open source database: {exc}") from exc
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        errors: list[str] = []
        for table, required in REQUIRED_COLUMNS.items():
            if table not in tables:
                errors.append(f"missing table {table}")
                continue
            columns = {
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
            missing = sorted(required - columns)
            if missing:
                errors.append(f"missing columns {table}: {', '.join(missing)}")
        if errors:
            raise MigrationError("source schema check failed: " + "; ".join(errors))
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()]
        if integrity != ["ok"]:
            raise MigrationError(f"source integrity_check failed: {integrity}")
        queue_count = int(connection.execute("SELECT COUNT(*) FROM queue").fetchone()[0])
        dead_count = int(connection.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0])
        return {
            "ok": True,
            "source": str(path),
            "integrityCheck": "ok",
            "queueRows": queue_count,
            "deadLetterRows": dead_count,
        }
    finally:
        connection.close()


def _event_type(queue_type: str) -> str:
    if queue_type == "ng":
        return "NG_ORDER"
    if queue_type == "complete":
        return "COMPLETION_REPORT"
    raise MigrationError(f"unsupported Node-RED queue type: {queue_type!r}")


def _legacy_delivery_id(legacy_id: str) -> str:
    return str(uuid.uuid5(MIGRATION_NAMESPACE, f"nodered:{legacy_id}"))


def _normalized_receipt(row: sqlite3.Row | None, event_type: str, correlation_id: str) -> dict[str, Any] | None:
    if row is None:
        return None
    receipt_no = str(row["receipt"] or "").strip()
    if not receipt_no:
        return None
    receipt = {
        "accepted": True,
        "schemaVersion": "1.0",
        "correlationId": correlation_id,
        "receiptType": "MES_ORDER" if event_type == "NG_ORDER" else "COMPLETION_RECEIPT",
        "receiptNo": receipt_no,
        "receivedAt": _iso_from_ms(row["ua_updated_at"]),
    }
    if event_type == "NG_ORDER":
        receipt["mesOrderNo"] = receipt_no
    return receipt


def _queue_item(row: sqlite3.Row) -> dict[str, Any]:
    try:
        body = json.loads(row["body_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"invalid body_json for queue {row['id']}") from exc
    if not isinstance(body, dict):
        raise MigrationError(f"queue body must be an object: {row['id']}")
    legacy_id = str(row["id"])
    event_type = _event_type(str(row["type"]))
    correlation_id = str(row["correlation_id"] or body.get("correlationId") or "")
    source_event_id = str(body.get("sourceEventId") or body.get("SourceEventId") or correlation_id or legacy_id)
    station_id = str(body.get("stationId") or body.get("StationId") or os.getenv("QC_STATION_ID") or "QCStation")
    has_writeback = row["ua_status"] is not None
    state = STATE_UA_WRITE_PENDING if has_writeback else STATE_PENDING
    receipt = _normalized_receipt(row, event_type, correlation_id)
    next_attempt_at = None
    if not has_writeback and int(row["next_at"] or 0) > 0:
        next_attempt_at = _iso_from_ms(row["next_at"])
    created_at = _iso_from_text(row["created_at"], _iso_from_ms(row["enqueued_at"]))
    updated_at = _iso_from_ms(row["ua_updated_at"] or row["sent_at"] or row["enqueued_at"], created_at)
    return {
        "action": "pending",
        "deliveryId": _legacy_delivery_id(legacy_id),
        "legacyDeliveryId": legacy_id,
        "businessKey": legacy_id,
        "sourceEventId": source_event_id[:200],
        "stationId": station_id[:100],
        "eventType": event_type,
        "correlationId": correlation_id,
        "payload": body,
        "payloadJson": _canonical_json(body),
        "payloadHash": _payload_hash(body),
        "state": state,
        "attempts": int(row["attempts"] or 0),
        "nextAttemptAt": next_attempt_at,
        "url": str(row["url"]),
        "receipt": receipt,
        "lastErrorCode": str(row["last_error_code"] or "") or None,
        "lastErrorMessage": str(row["last_status"] or "") or None,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "deadLetter": None,
    }


def _dead_item(row: sqlite3.Row) -> dict[str, Any]:
    try:
        body = json.loads(row["body_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"invalid body_json for dead letter {row['queue_id']}") from exc
    if not isinstance(body, dict):
        raise MigrationError(f"dead-letter body must be an object: {row['queue_id']}")
    legacy_id = str(row["queue_id"])
    event_type = _event_type(str(row["type"]))
    correlation_id = str(row["correlation_id"] or body.get("correlationId") or "")
    source_event_id = str(body.get("sourceEventId") or body.get("SourceEventId") or correlation_id or legacy_id)
    station_id = str(body.get("stationId") or body.get("StationId") or os.getenv("QC_STATION_ID") or "QCStation")
    created_at = _iso_from_text(row["created_at"], _iso_from_ms(row["next_at"]))
    failed_at = _iso_from_text(row["failed_at"], created_at)
    error_code = str(row["error_code"] or row["last_error_code"] or "MES_UNKNOWN")
    return {
        "action": "dead",
        "deliveryId": _legacy_delivery_id(legacy_id),
        "legacyDeliveryId": legacy_id,
        "businessKey": legacy_id,
        "sourceEventId": source_event_id[:200],
        "stationId": station_id[:100],
        "eventType": event_type,
        "correlationId": correlation_id,
        "payload": body,
        "payloadJson": _canonical_json(body),
        "payloadHash": _payload_hash(body),
        "state": STATE_DEAD,
        "attempts": int(row["attempts"] or 0),
        "nextAttemptAt": None,
        "url": str(row["url"]),
        "receipt": None,
        "lastErrorCode": error_code,
        "lastErrorMessage": str(row["last_status"] or "") or None,
        "createdAt": created_at,
        "updatedAt": failed_at,
        "deadLetter": {
            "reasonCode": error_code,
            "reason": str(row["last_status"] or "imported from Node-RED"),
            "failedAt": failed_at,
        },
    }


def _target_rows(target: Path) -> dict[str, sqlite3.Row]:
    if not target.exists():
        return {}
    try:
        connection = _readonly(target)
    except sqlite3.Error as exc:
        raise MigrationError(f"cannot open target database: {exc}") from exc
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "deliveries" not in tables:
            return {}
        rows = connection.execute(
            "SELECT delivery_id, business_key, payload_hash FROM deliveries"
        ).fetchall()
        return {str(row["business_key"]): row for row in rows}
    finally:
        connection.close()


def build_plan(source: Path, target: Path) -> dict[str, Any]:
    if source.resolve() == target.resolve():
        raise MigrationError("source and target must be different files")
    source_info = inspect_source(source)
    existing = _target_rows(target)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    connection = _readonly(source)
    try:
        queue_rows = connection.execute(
            """
            SELECT q.*, w.status AS ua_status, w.receipt AS receipt,
                   w.updated_at AS ua_updated_at
            FROM queue q
            LEFT JOIN ua_writebacks w ON w.queue_id = q.id
            ORDER BY q.seq
            """
        ).fetchall()
        for row in queue_rows:
            item = _queue_item(row)
            if item["businessKey"] in seen:
                raise MigrationError(f"duplicate source delivery id: {item['businessKey']}")
            seen.add(item["businessKey"])
            items.append(item)
        dead_rows = connection.execute("SELECT * FROM dead_letters ORDER BY seq").fetchall()
        for row in dead_rows:
            item = _dead_item(row)
            if item["businessKey"] in seen:
                raise MigrationError(f"delivery appears in both queue and dead letters: {item['businessKey']}")
            seen.add(item["businessKey"])
            items.append(item)
    finally:
        connection.close()

    imported = 0
    duplicates = 0
    conflicts: list[str] = []
    for item in items:
        current = existing.get(item["businessKey"])
        if current is None:
            item["importAction"] = "import"
            imported += 1
        elif str(current["payload_hash"]) == item["payloadHash"]:
            item["importAction"] = "duplicate"
            duplicates += 1
        else:
            item["importAction"] = "conflict"
            conflicts.append(item["businessKey"])
    return {
        "ok": not conflicts,
        "source": source_info,
        "target": str(target),
        "items": items,
        "queueRows": source_info["queueRows"],
        "deadLetterRows": source_info["deadLetterRows"],
        "importable": imported,
        "duplicates": duplicates,
        "conflicts": conflicts,
    }


def apply_plan(plan: dict[str, Any], target: Path) -> dict[str, Any]:
    if not plan.get("ok"):
        raise MigrationError("migration plan contains conflicts")
    imported = [
        item for item in plan["items"] if item["importAction"] == "import"
    ]
    if not imported:
        return {
            "ok": True,
            "applied": False,
            "imported": 0,
            "duplicates": plan["duplicates"],
            "conflicts": [],
        }
    DeliveryStore(target)
    connection = sqlite3.connect(target, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        connection.execute("BEGIN IMMEDIATE")
        for item in imported:
            current = connection.execute(
                "SELECT payload_hash FROM deliveries WHERE business_key = ?",
                (item["businessKey"],),
            ).fetchone()
            if current is not None:
                if str(current["payload_hash"]) != item["payloadHash"]:
                    raise MigrationError(f"target conflict changed during migration: {item['businessKey']}")
                continue
            receipt_json = _canonical_json(item["receipt"]) if item["receipt"] is not None else None
            connection.execute(
                """
                INSERT INTO deliveries (
                    delivery_id, business_key, source_event_id, station_id, event_type,
                    correlation_id, payload_json, payload_hash, state, attempts,
                    next_attempt_at, lease_until, url, receipt_json, last_error_code,
                    last_error_message, ua_write_confirmed, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    item["deliveryId"], item["businessKey"], item["sourceEventId"],
                    item["stationId"], item["eventType"], item["correlationId"],
                    item["payloadJson"], item["payloadHash"], item["state"],
                    item["attempts"], item["nextAttemptAt"], item["url"],
                    receipt_json, item["lastErrorCode"], item["lastErrorMessage"],
                    item["createdAt"], item["updatedAt"],
                ),
            )
            connection.execute(
                """
                INSERT INTO delivery_events(
                    delivery_id, from_state, to_state, correlation_id, detail_json, created_at
                ) VALUES (?, NULL, ?, ?, ?, ?)
                """,
                (
                    item["deliveryId"], item["state"], item["correlationId"],
                    _canonical_json({"importedFrom": "node-red", "legacyDeliveryId": item["legacyDeliveryId"]}),
                    item["createdAt"],
                ),
            )
            if item["deadLetter"]:
                dead = item["deadLetter"]
                connection.execute(
                    """
                    INSERT INTO dead_letters(
                        delivery_id, reason_code, reason, failed_at,
                        replayed_at, acknowledged_at, acknowledged_by, resolution
                    ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL)
                    """,
                    (item["deliveryId"], dead["reasonCode"], dead["reason"], dead["failedAt"]),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "ok": True,
        "applied": True,
        "imported": len(imported),
        "duplicates": plan["duplicates"],
        "conflicts": [],
    }


def migrate(source: Path, target: Path, *, apply: bool = False) -> dict[str, Any]:
    plan = build_plan(source, target)
    summary = {
        "ok": plan["ok"],
        "dryRun": not apply,
        "source": str(source),
        "target": str(target),
        "queueRows": plan["queueRows"],
        "deadLetterRows": plan["deadLetterRows"],
        "importable": plan["importable"],
        "duplicates": plan["duplicates"],
        "conflicts": plan["conflicts"],
    }
    if not apply or not plan["ok"]:
        return summary
    summary.update(apply_plan(plan, target))
    return summary
