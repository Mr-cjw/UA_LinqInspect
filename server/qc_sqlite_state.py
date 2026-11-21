"""SQLite-backed durable state for a single QC station.

The complete station snapshot, including pending OPC UA event outbox entries,
is written in one SQLite transaction. Node-RED consumes the replayed events and
acknowledges them through the existing OPC UA methods. The JSON backend remains
available for compatibility and migration.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from .qc_state import QCStateStore, QCStationSnapshot, snapshot_to_value
except ImportError:  # direct execution from the server directory
    from qc_state import QCStateStore, QCStationSnapshot, snapshot_to_value  # type: ignore

_log = logging.getLogger("qc_sqlite_state")
DEFAULT_STATION_ID = "QCStation"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SQLiteQCStateStore:
    """Persist station state and its complete event outbox atomically."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        station_id: str = DEFAULT_STATION_ID,
        window_size: int = 20,
        legacy_json_path: str | Path | None = None,
    ):
        self.db_path = str(Path(db_path).expanduser())
        self.path = Path(self.db_path)
        self.station_id = station_id
        self.window_size = max(1, int(window_size))
        self.legacy_json_path = Path(legacy_json_path).expanduser() if legacy_json_path else None
        self._write_lock = threading.RLock()
        self._validator = QCStateStore(self.path, window_size=self.window_size)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def initialize(self) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS qc_station_state (
                    station_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    status INTEGER NOT NULL,
                    work_order_id TEXT NOT NULL DEFAULT '',
                    target_count INTEGER NOT NULL DEFAULT 0,
                    inspect_count INTEGER NOT NULL DEFAULT 0,
                    ok_count INTEGER NOT NULL DEFAULT 0,
                    ng_count INTEGER NOT NULL DEFAULT 0,
                    window_json TEXT NOT NULL DEFAULT '[]',
                    current_part_sn TEXT NOT NULL DEFAULT '',
                    sn_day TEXT NOT NULL DEFAULT '',
                    sn_seq INTEGER NOT NULL DEFAULT 0,
                    last_result INTEGER NOT NULL DEFAULT 0,
                    last_defect TEXT NOT NULL DEFAULT '',
                    last_confidence REAL NOT NULL DEFAULT 0.0,
                    last_image_id TEXT NOT NULL DEFAULT '',
                    last_timestamp TEXT NOT NULL DEFAULT '',
                    outbox_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS qc_state_meta (
                    meta_key TEXT PRIMARY KEY,
                    meta_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.commit()
        try:
            Path(self.db_path).chmod(0o600)
        except OSError:
            pass

    def _row_to_snapshot(self, row: sqlite3.Row) -> QCStationSnapshot:
        value = {
            "schemaVersion": row["schema_version"],
            "status": row["status"],
            "workOrderId": row["work_order_id"],
            "targetCount": row["target_count"],
            "inspectCount": row["inspect_count"],
            "okCount": row["ok_count"],
            "ngCount": row["ng_count"],
            "window": json.loads(row["window_json"] or "[]"),
            "currentPartSn": row["current_part_sn"],
            "snDay": row["sn_day"],
            "snSeq": row["sn_seq"],
            "lastResult": bool(row["last_result"]),
            "lastDefect": row["last_defect"],
            "lastConfidence": row["last_confidence"],
            "lastImageId": row["last_image_id"],
            "lastTimestamp": row["last_timestamp"],
            "outbox": json.loads(row["outbox_json"] or "[]"),
        }
        return self._validator.validate_value(value)

    def load(self) -> QCStationSnapshot:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM qc_station_state WHERE station_id = ?", (self.station_id,)
            ).fetchone()
            if row:
                return self._row_to_snapshot(row)
        return self._migrate_legacy_json()

    def _migrate_legacy_json(self) -> QCStationSnapshot:
        if self.legacy_json_path is None or not self.legacy_json_path.exists():
            return QCStationSnapshot()
        snapshot = QCStateStore(
            self.legacy_json_path,
            window_size=self.window_size,
        ).load()
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            marker = conn.execute(
                "SELECT meta_value FROM qc_state_meta WHERE meta_key = 'legacy_json_migrated_v1'"
            ).fetchone()
            if marker is None:
                self._upsert_state(conn, snapshot)
                conn.execute(
                    "INSERT INTO qc_state_meta(meta_key, meta_value, updated_at) VALUES (?, ?, ?)",
                    ("legacy_json_migrated_v1", str(self.legacy_json_path), _now()),
                )
            conn.commit()
        _log.info("Migrated legacy JSON state from %s into SQLite %s", self.legacy_json_path, self.db_path)
        return snapshot

    def _upsert_state(self, conn: sqlite3.Connection, snapshot: QCStationSnapshot) -> None:
        value = snapshot_to_value(snapshot)
        conn.execute(
            """
            INSERT INTO qc_station_state (
                station_id, schema_version, status, work_order_id, target_count,
                inspect_count, ok_count, ng_count, window_json, current_part_sn,
                sn_day, sn_seq, last_result, last_defect, last_confidence,
                last_image_id, last_timestamp, outbox_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(station_id) DO UPDATE SET
                schema_version = excluded.schema_version,
                status = excluded.status,
                work_order_id = excluded.work_order_id,
                target_count = excluded.target_count,
                inspect_count = excluded.inspect_count,
                ok_count = excluded.ok_count,
                ng_count = excluded.ng_count,
                window_json = excluded.window_json,
                current_part_sn = excluded.current_part_sn,
                sn_day = excluded.sn_day,
                sn_seq = excluded.sn_seq,
                last_result = excluded.last_result,
                last_defect = excluded.last_defect,
                last_confidence = excluded.last_confidence,
                last_image_id = excluded.last_image_id,
                last_timestamp = excluded.last_timestamp,
                outbox_json = excluded.outbox_json,
                updated_at = excluded.updated_at
            """,
            (
                self.station_id,
                value["schemaVersion"],
                value["status"],
                value["workOrderId"],
                value["targetCount"],
                value["inspectCount"],
                value["okCount"],
                value["ngCount"],
                _canonical_json(value["window"]),
                value["currentPartSn"],
                value["snDay"],
                value["snSeq"],
                int(bool(value["lastResult"])),
                value["lastDefect"],
                value["lastConfidence"],
                value["lastImageId"],
                value["lastTimestamp"],
                _canonical_json(value["outbox"]),
                _now(),
            ),
        )

    def save(self, snapshot: QCStationSnapshot, events: Iterable[dict[str, Any]] = ()) -> None:
        # `events` is accepted for interface compatibility with older callers;
        # current server snapshots carry the authoritative outbox themselves.
        _ = events
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._upsert_state(conn, snapshot)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
