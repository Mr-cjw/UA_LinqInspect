"""SQLite-backed delivery state machine for the Edge Core Service.

The store is intentionally small and dependency-free. Each operation uses its own
short-lived SQLite connection; writes are serialized by SQLite's BEGIN IMMEDIATE
transaction. This is a single-edge-device design, not a distributed queue.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .evidence_signing import sign_document


STATE_PENDING = "PENDING"
STATE_SENDING = "SENDING"
STATE_RETRY_WAIT = "RETRY_WAIT"
STATE_ACKED = "ACKED"
STATE_UA_WRITE_PENDING = "UA_WRITE_PENDING"
STATE_DONE = "DONE"
STATE_DEAD = "DEAD"
STATE_DISCARDED = "DISCARDED"

ALL_STATES = {
    STATE_PENDING,
    STATE_SENDING,
    STATE_RETRY_WAIT,
    STATE_ACKED,
    STATE_UA_WRITE_PENDING,
    STATE_DONE,
    STATE_DEAD,
    STATE_DISCARDED,
}


class ConflictError(RuntimeError):
    """The business key already exists with a different payload."""


class StateConflictError(RuntimeError):
    """The requested transition is not legal for the current state."""


class StorageCapacityError(RuntimeError):
    """Evidence storage cannot accept more data without violating a quota."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def iso_after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class DeliveryStore:
    """Persistent Outbox/deduplication/state-machine storage."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        evidence_image_dir: str | Path | None = None,
        image_max_bytes: int = 8 * 1024 * 1024,
        image_max_total_bytes: int = 1024 * 1024 * 1024,
        image_max_files: int = 10000,
        image_min_free_bytes: int = 256 * 1024 * 1024,
        signing_key: str = "",
        signing_key_id: str = "primary",
    ):
        self.db_path = str(Path(db_path).expanduser())
        self._write_lock = threading.RLock()
        database_dir = Path(self.db_path).parent
        database_dir.mkdir(parents=True, exist_ok=True)
        self.image_dir = Path(evidence_image_dir or database_dir / "evidence-images").expanduser().resolve()
        self.image_max_bytes = max(1, int(image_max_bytes))
        self.image_max_total_bytes = max(1, int(image_max_total_bytes))
        self.image_max_files = max(1, int(image_max_files))
        self.image_min_free_bytes = max(0, int(image_min_free_bytes))
        self.signing_key = str(signing_key or "")
        self.signing_key_id = str(signing_key_id or "primary")
        self.image_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    business_key TEXT NOT NULL UNIQUE,
                    source_event_id TEXT NOT NULL,
                    station_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    lease_until TEXT,
                    url TEXT NOT NULL,
                    receipt_json TEXT,
                    last_error_code TEXT,
                    last_error_message TEXT,
                    ua_write_confirmed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS delivery_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id TEXT NOT NULL REFERENCES deliveries(delivery_id),
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    detail_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS dead_letters (
                    delivery_id TEXT PRIMARY KEY REFERENCES deliveries(delivery_id),
                    reason_code TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    failed_at TEXT NOT NULL,
                    replayed_at TEXT,
                    acknowledged_at TEXT,
                    acknowledged_by TEXT,
                    resolution TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_deliveries_due
                    ON deliveries(state, next_attempt_at);
                CREATE INDEX IF NOT EXISTS idx_deliveries_correlation
                    ON deliveries(correlation_id);

                CREATE TABLE IF NOT EXISTS evidence_images (
                    image_id TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    bytes INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_images_sha256
                    ON evidence_images(sha256);
                """
            )

    @staticmethod
    def business_key(event: dict[str, Any]) -> str:
        event_type = str(event["eventType"])
        station_id = str(event["stationId"])
        source_event_id = str(event["sourceEventId"])
        return f"{event_type}:{station_id}:{source_event_id}"

    @staticmethod
    def _row_to_delivery(row: sqlite3.Row) -> dict[str, Any]:
        receipt = json.loads(row["receipt_json"]) if row["receipt_json"] else None
        payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
        work_order_id = str(payload.get("workOrderId") or payload.get("work_order_id") or "")
        return {
            "schemaVersion": "1.0",
            "deliveryId": row["delivery_id"],
            "businessKey": row["business_key"],
            "sourceEventId": row["source_event_id"],
            "stationId": row["station_id"],
            "eventType": row["event_type"],
            "workOrderId": work_order_id,
            "correlationId": row["correlation_id"],
            "state": row["state"],
            "attempts": row["attempts"],
            "nextAttemptAt": row["next_attempt_at"],
            "leaseUntil": row["lease_until"],
            "lastErrorCode": row["last_error_code"],
            "lastErrorMessage": row["last_error_message"],
            "mesReceipt": receipt,
            "uaWriteConfirmed": bool(row["ua_write_confirmed"]),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _detect_image_type(content: bytes) -> tuple[str, str]:
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png", ".png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg", ".jpg"
        if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            return "image/webp", ".webp"
        raise ValueError("image must be PNG, JPEG or WebP")

    @staticmethod
    def _image_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "imageId": row["image_id"],
            "sha256": row["sha256"],
            "contentType": row["content_type"],
            "bytes": int(row["bytes"]),
            "relativePath": row["relative_path"],
            "createdAt": row["created_at"],
            "available": True,
        }

    def evidence_image_capacity(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(bytes), 0) AS total FROM evidence_images"
            ).fetchone()
        usage = shutil.disk_usage(self.image_dir)
        return {
            "files": int(row["count"]),
            "totalBytes": int(row["total"]),
            "maxFiles": self.image_max_files,
            "maxTotalBytes": self.image_max_total_bytes,
            "maxFileBytes": self.image_max_bytes,
            "freeBytes": int(usage.free),
            "minFreeBytes": self.image_min_free_bytes,
        }

    def store_evidence_image(
        self,
        image_id: str,
        content: bytes,
        *,
        expected_sha256: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        image_id = str(image_id or "").strip()
        if not image_id or len(image_id) > 200:
            raise ValueError("imageId must be a non-empty string up to 200 characters")
        if not content:
            raise ValueError("image content is empty")
        if len(content) > self.image_max_bytes:
            raise ValueError(f"image exceeds {self.image_max_bytes} bytes")
        content_type, extension = self._detect_image_type(content)
        digest = hashlib.sha256(content).hexdigest()
        expected = str(expected_sha256 or "").strip().lower()
        if expected and expected != digest:
            raise ValueError("image SHA-256 does not match metadata")
        now = utc_now()
        with self._write_lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM evidence_images WHERE image_id = ?", (image_id,)
            ).fetchone()
            if existing:
                if existing["sha256"] != digest:
                    raise ConflictError(f"imageId {image_id!r} already exists with different content")
                return self._image_row(existing), True
            usage = conn.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(bytes), 0) AS total FROM evidence_images"
            ).fetchone()
            if int(usage["count"]) + 1 > self.image_max_files:
                raise StorageCapacityError(
                    f"evidence image file quota exceeded ({usage['count']}/{self.image_max_files})"
                )
            if int(usage["total"]) + len(content) > self.image_max_total_bytes:
                raise StorageCapacityError(
                    f"evidence image byte quota exceeded ({int(usage['total']) + len(content)}/{self.image_max_total_bytes})"
                )
            free = shutil.disk_usage(self.image_dir).free
            if free < len(content) + self.image_min_free_bytes:
                raise StorageCapacityError(
                    f"evidence image disk reserve would be violated (free={free}, requested={len(content)}, reserve={self.image_min_free_bytes})"
                )
            relative = Path(digest[:2]) / f"{digest}{extension}"
            destination = self.image_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not destination.is_file():
                temporary = destination.with_name(
                    f".{destination.name}.{os.getpid()}.{time.time_ns()}.tmp"
                )
                try:
                    temporary.write_bytes(content)
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, destination)
                finally:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
            conn.execute(
                "INSERT INTO evidence_images(image_id, sha256, content_type, bytes, relative_path, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (image_id, digest, content_type, len(content), relative.as_posix(), now),
            )
            row = conn.execute(
                "SELECT * FROM evidence_images WHERE image_id = ?", (image_id,)
            ).fetchone()
            return self._image_row(row), False

    def evidence_image(self, image_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evidence_images WHERE image_id = ?", (str(image_id),)
            ).fetchone()
            return self._image_row(row) if row else None

    def read_evidence_image(self, image_id: str) -> tuple[dict[str, Any], bytes] | None:
        metadata = self.evidence_image(image_id)
        if not metadata:
            return None
        path = (self.image_dir / metadata["relativePath"]).resolve()
        if self.image_dir not in path.parents:
            raise ValueError("evidence image path escapes configured root")
        if not path.is_file():
            raise FileNotFoundError(f"evidence image file missing: {metadata['relativePath']}")
        return metadata, path.read_bytes()

    def get(self, delivery_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            return self._row_to_delivery(row) if row else None

    def get_by_business_key(self, business_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM deliveries WHERE business_key = ?", (business_key,)
            ).fetchone()
            return self._row_to_delivery(row) if row else None

    def cleanup_unreferenced_evidence_images(
        self,
        *,
        retention_days: int = 90,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        days = max(0, int(retention_days))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="milliseconds")
        with self._write_lock, self._connect() as conn:
            referenced: set[str] = set()
            for row in conn.execute("SELECT payload_json FROM deliveries").fetchall():
                try:
                    payload = json.loads(row["payload_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                image_id = str(payload.get("imageId") or "").strip()
                if image_id:
                    referenced.add(image_id)
            candidates = [
                row for row in conn.execute(
                    "SELECT * FROM evidence_images WHERE created_at < ? ORDER BY created_at",
                    (cutoff,),
                ).fetchall()
                if row["image_id"] not in referenced
            ]
            total_bytes = sum(int(row["bytes"]) for row in candidates)
            removed_files = []
            if not dry_run and candidates:
                candidate_ids = [row["image_id"] for row in candidates]
                conn.executemany(
                    "DELETE FROM evidence_images WHERE image_id = ?",
                    [(image_id,) for image_id in candidate_ids],
                )
                remaining_paths = {
                    str(row[0]) for row in conn.execute(
                        "SELECT relative_path FROM evidence_images"
                    ).fetchall()
                }
                for row in candidates:
                    if row["relative_path"] in remaining_paths:
                        continue
                    path = safe_path = (self.image_dir / row["relative_path"]).resolve()
                    if self.image_dir not in safe_path.parents:
                        continue
                    try:
                        path.unlink()
                        removed_files.append(str(path))
                    except FileNotFoundError:
                        pass
        return {
            "ok": True,
            "dryRun": dry_run,
            "retentionDays": days,
            "cutoff": cutoff,
            "candidateCount": len(candidates),
            "candidateBytes": total_bytes,
            "removedCount": 0 if dry_run else len(candidates),
            "removedBytes": 0 if dry_run else total_bytes,
            "removedFiles": removed_files,
            "referencedImageCount": len(referenced),
        }

    def search_evidence(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        text = str(query or "").strip()
        if not text:
            return []
        pattern = f"%{text}%"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM deliveries "
                "WHERE delivery_id LIKE ? OR business_key LIKE ? OR correlation_id LIKE ? "
                "OR payload_json LIKE ? OR receipt_json LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (pattern, pattern, pattern, pattern, pattern, max(1, min(200, int(limit)))),
            ).fetchall()
        matches = []
        needle = text.lower()
        for row in rows:
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
            receipt = json.loads(row["receipt_json"]) if row["receipt_json"] else {}
            fields = [
                row["delivery_id"], row["business_key"], row["correlation_id"],
                payload.get("workOrderId"), payload.get("serialNumber"), payload.get("imageId"),
                receipt.get("receiptNo"), receipt.get("mesOrderNo"),
            ]
            if any(needle in str(value).lower() for value in fields if value not in (None, "")):
                matches.append({
                    "deliveryId": row["delivery_id"],
                    "correlationId": row["correlation_id"],
                    "workOrderId": payload.get("workOrderId"),
                    "serialNumber": payload.get("serialNumber"),
                    "eventType": row["event_type"],
                    "state": row["state"],
                    "imageId": payload.get("imageId"),
                    "receiptNo": receipt.get("receiptNo"),
                    "createdAt": row["created_at"],
                })
        return matches

    def evidence_bundle(self, delivery_id: str) -> dict[str, Any] | None:
        """Build a reconstructable quality evidence bundle from durable state."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if not row:
                return None
            event_rows = conn.execute(
                "SELECT sequence, from_state, to_state, correlation_id, detail_json, created_at "
                "FROM delivery_events WHERE delivery_id = ? ORDER BY sequence",
                (delivery_id,),
            ).fetchall()
            dead_row = conn.execute(
                "SELECT * FROM dead_letters WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()

        payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        delivery = self._row_to_delivery(row)
        audit = []
        for event in event_rows:
            detail = event["detail_json"]
            try:
                parsed_detail = json.loads(detail) if detail else None
            except json.JSONDecodeError:
                parsed_detail = detail
            audit.append({
                "sequence": int(event["sequence"]),
                "fromState": event["from_state"],
                "toState": event["to_state"],
                "correlationId": event["correlation_id"],
                "detail": parsed_detail,
                "createdAt": event["created_at"],
            })

        event_type = str(row["event_type"])
        inspection_fields = {
            "sourceEventId": payload.get("sourceEventId"),
            "stationId": payload.get("stationId"),
            "eventType": event_type,
            "occurredAt": payload.get("occurredAt"),
            "result": payload.get("result"),
            "workOrderId": payload.get("workOrderId"),
            "serialNumber": payload.get("serialNumber"),
            "defectType": payload.get("defectType"),
            "confidence": payload.get("confidence"),
            "imageId": payload.get("imageId"),
            "metadata": metadata,
        }
        lineage = {
            "modelVersion": metadata.get("modelVersion") or metadata.get("visionModelVersion"),
            "modelSha256": (
                metadata.get("modelSha256")
                or metadata.get("modelHash")
                or metadata.get("visionModelSha256")
            ),
            "datasetVersion": metadata.get("datasetVersion") or metadata.get("visionDatasetVersion"),
            "imageSha256": metadata.get("imageSha256"),
            "imageHashScope": metadata.get("imageHashScope"),
            "inferenceMs": metadata.get("inferenceMs") or metadata.get("visionInferenceMs"),
        }
        required = ("sourceEventId", "stationId", "workOrderId", "correlationId")
        if event_type == "NG_ORDER":
            required += ("serialNumber", "defectType", "confidence", "imageId")
        missing = [name for name in required if payload.get(name) in (None, "")]
        image_evidence = self.evidence_image(str(payload.get("imageId") or "")) if payload.get("imageId") else None
        dead_letter = None
        if dead_row:
            dead_letter = {
                "reasonCode": dead_row["reason_code"],
                "reason": dead_row["reason"],
                "failedAt": dead_row["failed_at"],
                "replayedAt": dead_row["replayed_at"],
                "acknowledgedAt": dead_row["acknowledged_at"],
                "acknowledgedBy": dead_row["acknowledged_by"],
                "resolution": dead_row["resolution"],
            }

        content = {
            "schemaVersion": "1.0",
            "bundleType": "QC_DELIVERY_EVIDENCE",
            "generatedAt": utc_now(),
            "deliveryId": row["delivery_id"],
            "businessKey": row["business_key"],
            "correlationId": row["correlation_id"],
            "inspection": inspection_fields,
            "visionLineage": lineage,
            "delivery": delivery,
            "mes": {
                "endpoint": row["url"],
                "receipt": delivery.get("mesReceipt"),
                "attempts": int(row["attempts"]),
                "lastErrorCode": row["last_error_code"],
                "lastErrorMessage": row["last_error_message"],
            },
            "ua": {
                "writeConfirmed": bool(row["ua_write_confirmed"]),
                "receiptMirrorsExternal": bool(row["ua_write_confirmed"]),
            },
            "audit": audit,
            "deadLetter": dead_letter,
            "imageEvidence": image_evidence,
            "completeness": {
                "missingFields": missing,
                "complete": not missing,
                "imageBinaryStored": image_evidence is not None,
                "imageReferenceOnly": bool(payload.get("imageId")) and image_evidence is None,
                "modelLineagePresent": all(
                    lineage.get(key) not in (None, "")
                    for key in ("modelVersion", "modelSha256", "datasetVersion")
                ),
            },
            "limitations": [
                "bundleSha256 is content integrity, not a digital signature.",
                "Image binaries are stored only after explicit evidence upload; back up the image directory with the database.",
                "Vision metrics are only valid for the dataset and model version recorded above.",
            ],
        }
        content["signatureStatus"] = "SIGNED" if self.signing_key else "UNSIGNED"
        content["bundleHashScope"] = "canonical-json-excluding-generatedAt-and-bundleSha256"
        hash_input = {
            key: value for key, value in content.items()
            if key not in ("generatedAt", "bundleSha256")
        }
        content["bundleSha256"] = hashlib.sha256(
            _canonical_json(hash_input).encode("utf-8")
        ).hexdigest()
        if self.signing_key:
            return sign_document(content, key=self.signing_key, key_id=self.signing_key_id)
        content["signature"] = None
        return content

    def enqueue(
        self,
        event: dict[str, Any],
        *,
        url: str,
        idempotency_key: str,
        correlation_id: str,
    ) -> tuple[dict[str, Any], bool]:
        """Create a delivery atomically. Returns (delivery, was_duplicate)."""
        business_key = self.business_key(event)
        digest = payload_hash(event)
        now = utc_now()
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM deliveries WHERE business_key = ? OR delivery_id = ?",
                (business_key, idempotency_key),
            ).fetchone()
            if existing:
                if existing["payload_hash"] != digest:
                    raise ConflictError(
                        f"business key {business_key!r} already exists with a different payload"
                    )
                if existing["business_key"] != business_key:
                    raise ConflictError(
                        f"idempotency key {idempotency_key!r} is already used by another business key"
                    )
                conn.commit()
                return self._row_to_delivery(existing), True

            delivery_id = idempotency_key
            conn.execute(
                """
                INSERT INTO deliveries (
                    delivery_id, business_key, source_event_id, station_id,
                    event_type, correlation_id, payload_json, payload_hash,
                    state, attempts, url, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    delivery_id,
                    business_key,
                    event["sourceEventId"],
                    event["stationId"],
                    event["eventType"],
                    correlation_id,
                    _canonical_json(event),
                    digest,
                    STATE_PENDING,
                    url,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO delivery_events
                    (delivery_id, from_state, to_state, correlation_id, detail_json, created_at)
                VALUES (?, NULL, ?, ?, ?, ?)
                """,
                (delivery_id, STATE_PENDING, correlation_id, _canonical_json({"eventType": event["eventType"]}), now),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)).fetchone()
            return self._row_to_delivery(row), False

    def recover_expired(self, *, max_attempts: int, retry_delay_seconds: float = 1.0) -> int:
        now = utc_now()
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM deliveries WHERE state = ? AND lease_until IS NOT NULL AND lease_until < ?",
                (STATE_SENDING, now),
            ).fetchall()
            for row in rows:
                if row["attempts"] >= max_attempts:
                    self._dead_in_conn(conn, row, "LEASE_EXPIRED", "SENDING lease expired after retry budget")
                else:
                    self._transition_in_conn(
                        conn, row, STATE_RETRY_WAIT, "LEASE_EXPIRED",
                        next_attempt_at=iso_after(retry_delay_seconds),
                    )
            conn.commit()
            return len(rows)

    def claim_due(self, *, lease_seconds: float = 30.0) -> dict[str, Any] | None:
        now = utc_now()
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM deliveries
                WHERE state IN (?, ?)
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (STATE_PENDING, STATE_RETRY_WAIT, now),
            ).fetchone()
            if not row:
                conn.commit()
                return None
            updated = conn.execute(
                """
                UPDATE deliveries
                SET state = ?, attempts = attempts + 1, lease_until = ?, updated_at = ?
                WHERE delivery_id = ? AND state = ?
                """,
                (STATE_SENDING, iso_after(lease_seconds), now, row["delivery_id"], row["state"]),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return None
            conn.execute(
                """
                INSERT INTO delivery_events
                    (delivery_id, from_state, to_state, correlation_id, detail_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (row["delivery_id"], row["state"], STATE_SENDING, row["correlation_id"], None, now),
            )
            conn.commit()
            claimed = conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (row["delivery_id"],)
            ).fetchone()
            return self._row_to_delivery(claimed)

    def get_payload(self, delivery_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            return json.loads(row["payload_json"]) if row else None

    def get_url(self, delivery_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT url FROM deliveries WHERE delivery_id = ?", (delivery_id,)).fetchone()
            return row["url"] if row else None

    def _transition_in_conn(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        to_state: str,
        detail: str | None = None,
        *,
        next_attempt_at: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        receipt: dict[str, Any] | None = None,
        clear_lease: bool = True,
    ) -> None:
        now = utc_now()
        receipt_json = _canonical_json(receipt) if receipt is not None else row["receipt_json"]
        conn.execute(
            """
            UPDATE deliveries
            SET state = ?, next_attempt_at = ?, lease_until = CASE WHEN ? THEN NULL ELSE lease_until END,
                receipt_json = ?, last_error_code = ?, last_error_message = ?, updated_at = ?
            WHERE delivery_id = ?
            """,
            (
                to_state,
                next_attempt_at,
                clear_lease,
                receipt_json,
                error_code,
                error_message,
                now,
                row["delivery_id"],
            ),
        )
        conn.execute(
            """
            INSERT INTO delivery_events
                (delivery_id, from_state, to_state, correlation_id, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row["delivery_id"],
                row["state"],
                to_state,
                row["correlation_id"],
                _canonical_json({"detail": detail}) if detail else None,
                now,
            ),
        )

    def _dead_in_conn(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        reason_code: str,
        reason: str,
    ) -> None:
        self._transition_in_conn(
            conn,
            row,
            STATE_DEAD,
            reason,
            error_code=reason_code,
            error_message=reason,
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO dead_letters
                (delivery_id, reason_code, reason, failed_at)
            VALUES (?, ?, ?, ?)
            """,
            (row["delivery_id"], reason_code, reason, utc_now()),
        )

    def mark_acked(self, delivery_id: str, receipt: dict[str, Any]) -> dict[str, Any]:
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if not row:
                raise KeyError(delivery_id)
            if row["state"] not in (STATE_SENDING, STATE_ACKED):
                raise StateConflictError(f"cannot ACK delivery in state {row['state']}")
            self._transition_in_conn(conn, row, STATE_UA_WRITE_PENDING, receipt=receipt)
            conn.commit()
            return self.get(delivery_id)  # type: ignore[return-value]

    def mark_retry(
        self,
        delivery_id: str,
        *,
        error_code: str,
        error_message: str,
        delay_seconds: float,
        max_attempts: int,
    ) -> dict[str, Any]:
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if not row:
                raise KeyError(delivery_id)
            if row["state"] != STATE_SENDING:
                raise StateConflictError(f"cannot retry delivery in state {row['state']}")
            if row["attempts"] >= max_attempts:
                self._dead_in_conn(conn, row, error_code, error_message)
            else:
                self._transition_in_conn(
                    conn,
                    row,
                    STATE_RETRY_WAIT,
                    error_message,
                    next_attempt_at=iso_after(delay_seconds),
                    error_code=error_code,
                    error_message=error_message,
                )
            conn.commit()
            return self.get(delivery_id)  # type: ignore[return-value]

    def mark_dead(self, delivery_id: str, *, error_code: str, error_message: str) -> dict[str, Any]:
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if not row:
                raise KeyError(delivery_id)
            if row["state"] in (STATE_DONE, STATE_DEAD, STATE_DISCARDED):
                raise StateConflictError(f"cannot move terminal delivery {row['state']}")
            self._dead_in_conn(conn, row, error_code, error_message)
            conn.commit()
            return self.get(delivery_id)  # type: ignore[return-value]

    def mark_ua_write_result(
        self,
        delivery_id: str,
        *,
        success: bool,
        correlation_id: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if not row:
                raise KeyError(delivery_id)
            if row["correlation_id"] != correlation_id:
                raise StateConflictError("correlationId does not match the delivery")
            if row["state"] not in (STATE_UA_WRITE_PENDING, STATE_ACKED):
                raise StateConflictError(f"cannot write UA result in state {row['state']}")
            if success:
                self._transition_in_conn(conn, row, STATE_DONE)
                conn.execute(
                    "UPDATE deliveries SET ua_write_confirmed = 1 WHERE delivery_id = ?",
                    (delivery_id,),
                )
            else:
                self._transition_in_conn(
                    conn,
                    row,
                    STATE_UA_WRITE_PENDING,
                    "UA write failed; retry required",
                    error_code=error_code or "UA_WRITE_FAILED",
                    error_message=error_message or "OPC UA write failed",
                )
            conn.commit()
            return self.get(delivery_id)  # type: ignore[return-value]

    def list_deliveries(self, *, state: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if state and state not in ALL_STATES:
            raise ValueError(f"unknown state: {state}")
        with self._connect() as conn:
            if state:
                rows = conn.execute(
                    "SELECT * FROM deliveries WHERE state = ? ORDER BY created_at ASC LIMIT ?",
                    (state, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM deliveries ORDER BY created_at ASC LIMIT ?", (limit,)
                ).fetchall()
            return [self._row_to_delivery(row) for row in rows]

    def list_dead_letters(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT d.*, x.reason_code AS dead_reason_code, x.reason AS dead_reason,
                       x.failed_at AS dead_failed_at, x.replayed_at, x.acknowledged_at,
                       x.acknowledged_by, x.resolution
                FROM dead_letters x JOIN deliveries d USING (delivery_id)
                ORDER BY x.failed_at ASC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                {
                    **self._row_to_delivery(row),
                    "reasonCode": row["dead_reason_code"],
                    "reason": row["dead_reason"],
                    "failedAt": row["dead_failed_at"],
                    "replayedAt": row["replayed_at"],
                    "acknowledgedAt": row["acknowledged_at"],
                    "acknowledgedBy": row["acknowledged_by"],
                    "resolution": row["resolution"],
                }
                for row in rows
            ]

    def replay_dead(self, delivery_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if not row:
                raise KeyError(delivery_id)
            if row["state"] != STATE_DEAD:
                raise StateConflictError(f"cannot replay delivery in state {row['state']}")
            self._transition_in_conn(conn, row, STATE_PENDING)
            conn.execute(
                """
                UPDATE dead_letters
                SET replayed_at = ?, acknowledged_at = NULL, acknowledged_by = NULL, resolution = NULL
                WHERE delivery_id = ?
                """,
                (utc_now(), delivery_id),
            )
            conn.execute(
                """
                INSERT INTO delivery_events
                    (delivery_id, from_state, to_state, correlation_id, detail_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (delivery_id, STATE_DEAD, STATE_PENDING, row["correlation_id"],
                 _canonical_json({"actor": actor, "reason": reason}), utc_now()),
            )
            conn.commit()
            return self.get(delivery_id)  # type: ignore[return-value]

    def acknowledge_dead(self, delivery_id: str, *, actor: str, resolution: str) -> dict[str, Any]:
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if not row:
                raise KeyError(delivery_id)
            if row["state"] != STATE_DEAD:
                raise StateConflictError(f"cannot acknowledge delivery in state {row['state']}")
            conn.execute(
                """
                UPDATE dead_letters
                SET acknowledged_at = ?, acknowledged_by = ?, resolution = ?
                WHERE delivery_id = ?
                """,
                (utc_now(), actor, resolution, delivery_id),
            )
            conn.commit()
            dead = self.list_dead_letters(limit=1000)
            return next(item for item in dead if item["deliveryId"] == delivery_id)

    def counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS count FROM deliveries GROUP BY state"
            ).fetchall()
            counts = {state: 0 for state in ALL_STATES}
            for row in rows:
                counts[row["state"]] = row["count"]
            return counts

    def metrics_snapshot(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)

        def age_seconds(value: str | None) -> float:
            if not value:
                return 0.0
            try:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return max(0.0, round((now - parsed).total_seconds(), 3))
            except ValueError:
                return 0.0

        with self._connect() as conn:
            state_rows = conn.execute(
                "SELECT state, COUNT(*) AS count FROM deliveries GROUP BY state"
            ).fetchall()
            counts = {state: 0 for state in ALL_STATES}
            for row in state_rows:
                counts[row["state"]] = row["count"]
            oldest = conn.execute(
                """
                SELECT MIN(created_at) AS value FROM deliveries
                WHERE state IN ('PENDING', 'SENDING', 'RETRY_WAIT')
                """
            ).fetchone()["value"]
            dead = conn.execute(
                "SELECT COUNT(*) AS count, MIN(failed_at) AS oldest FROM dead_letters"
            ).fetchone()
            last_success = conn.execute(
                "SELECT MAX(updated_at) AS value FROM deliveries WHERE state = 'DONE'"
            ).fetchone()["value"]
            last_failure = conn.execute(
                "SELECT MAX(failed_at) AS value FROM dead_letters"
            ).fetchone()["value"]
            ua_success = conn.execute(
                "SELECT COUNT(*) AS count, MAX(updated_at) AS last FROM deliveries WHERE state = 'DONE' AND ua_write_confirmed = 1"
            ).fetchone()
            ua_failure = conn.execute(
                "SELECT COUNT(*) AS count FROM deliveries WHERE state = 'UA_WRITE_PENDING' AND last_error_code LIKE 'UA_%'"
            ).fetchone()["count"]
            done_total = conn.execute(
                "SELECT COUNT(*) AS count FROM deliveries WHERE state = 'DONE'"
            ).fetchone()["count"]
            dead_total = int(dead["count"])
        return {
            "counts": counts,
            "queueTotal": counts["PENDING"] + counts["SENDING"] + counts["RETRY_WAIT"],
            "oldestQueueAgeSeconds": age_seconds(oldest),
            "deadTotal": dead_total,
            "oldestDeadAgeSeconds": age_seconds(dead["oldest"]),
            "mesSuccessTotal": int(done_total),
            "mesFailureTotal": dead_total,
            "lastMesSuccessAt": last_success,
            "lastMesFailureAt": last_failure,
            "uaWriteSuccessTotal": int(ua_success["count"]),
            "uaWriteFailureTotal": int(ua_failure),
            "lastUaWriteSuccessAt": ua_success["last"],
        }
