"""Tests for the offline Node-RED to Edge Core queue migration."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from edge_core.migration import MigrationError, migrate
from edge_core.store import DeliveryStore


def create_nodered_fixture(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE queue (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                type TEXT NOT NULL,
                correlation_id TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL,
                body_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_at INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'PENDING',
                sent_at INTEGER,
                last_status TEXT NOT NULL DEFAULT '',
                last_error_code TEXT NOT NULL DEFAULT '',
                enqueued_at INTEGER NOT NULL
            );
            CREATE TABLE dead_letters (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_id TEXT NOT NULL,
                type TEXT NOT NULL,
                correlation_id TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL,
                body_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_at INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT '',
                sent_at INTEGER,
                last_status TEXT NOT NULL DEFAULT '',
                last_error_code TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                failed_at TEXT NOT NULL
            );
            CREATE TABLE ua_writebacks (
                queue_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'PENDING',
                receipt TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_at INTEGER NOT NULL DEFAULT 0,
                started_at INTEGER,
                last_error_code TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )
        pending_body = {
            "schemaVersion": "1.0",
            "sourceEventId": "evt-migrate-ng",
            "stationId": "QCStation",
            "correlationId": "corr-migrate-ng-0001",
            "workOrderId": "WO-MIGRATE-1",
            "serialNumber": "SN-MIGRATE-1",
            "defectType": "SCRATCH",
            "occurredAt": "2026-11-21T00:00:00.000Z",
        }
        complete_body = {
            "schemaVersion": "1.0",
            "sourceEventId": "evt-migrate-done",
            "stationId": "QCStation",
            "correlationId": "corr-migrate-done-0001",
            "workOrderId": "WO-MIGRATE-2",
            "inspectCount": 5,
            "okCount": 4,
            "ngCount": 1,
            "ngRate": 0.2,
        }
        dead_body = {
            "schemaVersion": "1.0",
            "sourceEventId": "evt-migrate-dead",
            "stationId": "QCStation",
            "correlationId": "corr-migrate-dead-0001",
            "workOrderId": "WO-MIGRATE-3",
            "serialNumber": "SN-MIGRATE-3",
            "defectType": "DENT",
            "occurredAt": "2026-11-21T00:00:02.000Z",
        }
        conn.execute(
            """
            INSERT INTO queue(
              id, type, correlation_id, url, body_json, created_at, attempts,
              next_at, status, sent_at, last_status, last_error_code, enqueued_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ng|SN-MIGRATE-1|2026-11-21T00:00:00.000Z", "ng",
                "corr-migrate-ng-0001", "http://mes/mes/ng-order",
                json.dumps(pending_body), "2026-11-21T00:00:00.000Z", 1,
                0, "PENDING", None, "HTTP 500", "MES_UNAVAILABLE", 1763683200000,
            ),
        )
        conn.execute(
            """
            INSERT INTO queue(
              id, type, correlation_id, url, body_json, created_at, attempts,
              next_at, status, sent_at, last_status, last_error_code, enqueued_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "done|WO-MIGRATE-2", "complete", "corr-migrate-done-0001",
                "http://mes/mes/complete", json.dumps(complete_body),
                "2026-11-21T00:00:01.000Z", 2, 0, "SENDING", 1763683201000,
                "", "", 1763683201000,
            ),
        )
        conn.execute(
            """
            INSERT INTO ua_writebacks(
              queue_id, status, receipt, attempts, next_at, started_at,
              last_error_code, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("done|WO-MIGRATE-2", "SENDING", "RC-MIGRATE-0001", 1, 0,
             1763683202000, "", 1763683202000, 1763683202000),
        )
        conn.execute(
            """
            INSERT INTO dead_letters(
              queue_id, type, correlation_id, url, body_json, created_at, attempts,
              next_at, status, sent_at, last_status, last_error_code, error_code, failed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ng|SN-MIGRATE-3|2026-11-21T00:00:02.000Z", "ng",
                "corr-migrate-dead-0001", "http://mes/mes/ng-order",
                json.dumps(dead_body), "2026-11-21T00:00:02.000Z", 4, 0,
                "SENDING", 1763683202000, "HTTP 503", "MES_UNAVAILABLE",
                "MES_UNAVAILABLE", "2026-11-21T00:01:00.000Z",
            ),
        )


class EdgeCoreMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "nodered.sqlite"
        self.target = self.root / "edge-core.sqlite"
        create_nodered_fixture(self.source)

    def test_dry_run_does_not_create_target(self):
        summary = migrate(self.source, self.target, apply=False)
        self.assertTrue(summary["ok"])
        self.assertTrue(summary["dryRun"])
        self.assertEqual(summary["queueRows"], 2)
        self.assertEqual(summary["deadLetterRows"], 1)
        self.assertEqual(summary["importable"], 3)
        self.assertFalse(self.target.exists())

    def test_apply_preserves_ids_states_payload_and_dead_letter(self):
        summary = migrate(self.source, self.target, apply=True)
        self.assertTrue(summary["ok"])
        self.assertTrue(summary["applied"])
        self.assertEqual(summary["imported"], 3)
        store = DeliveryStore(self.target)
        pending = store.get_by_business_key("ng|SN-MIGRATE-1|2026-11-21T00:00:00.000Z")
        ua_pending = store.get_by_business_key("done|WO-MIGRATE-2")
        dead = store.get_by_business_key("ng|SN-MIGRATE-3|2026-11-21T00:00:02.000Z")
        self.assertIsNotNone(pending)
        self.assertIsNotNone(ua_pending)
        self.assertIsNotNone(dead)
        self.assertEqual(pending["state"], "PENDING")
        self.assertEqual(pending["attempts"], 1)
        pending_payload = store.get_payload(pending["deliveryId"])
        self.assertEqual(pending_payload["eventType"], "NG_ORDER")
        self.assertEqual(pending_payload["result"], "NG")
        self.assertEqual(pending_payload["sourceEventId"], "evt-migrate-ng")
        self.assertEqual(pending_payload["correlationId"], "corr-migrate-ng-0001")
        self.assertEqual(ua_pending["state"], "UA_WRITE_PENDING")
        self.assertEqual(ua_pending["mesReceipt"]["receiptType"], "COMPLETION_RECEIPT")
        self.assertEqual(ua_pending["mesReceipt"]["receiptNo"], "RC-MIGRATE-0001")
        self.assertEqual(dead["state"], "DEAD")
        self.assertEqual(dead["lastErrorCode"], "MES_UNAVAILABLE")
        self.assertEqual(len(store.list_dead_letters()), 1)
        for delivery in (pending, ua_pending, dead):
            self.assertEqual(str(uuid.UUID(delivery["deliveryId"])), delivery["deliveryId"])
        with sqlite3.connect(self.target) as conn:
            events = conn.execute("SELECT COUNT(*) FROM delivery_events").fetchone()[0]
            self.assertEqual(events, 3)

    def test_reapply_is_idempotent(self):
        first = migrate(self.source, self.target, apply=True)
        second = migrate(self.source, self.target, apply=True)
        self.assertEqual(first["imported"], 3)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["duplicates"], 3)
        with sqlite3.connect(self.target) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 3)

    def test_changed_payload_is_a_conflict_and_leaves_target_unchanged(self):
        migrate(self.source, self.target, apply=True)
        with sqlite3.connect(self.source) as conn:
            body = json.loads(conn.execute(
                "SELECT body_json FROM queue WHERE id = ?",
                ("ng|SN-MIGRATE-1|2026-11-21T00:00:00.000Z",),
            ).fetchone()[0])
            body["defectType"] = "DENT"
            conn.execute(
                "UPDATE queue SET body_json = ? WHERE id = ?",
                (json.dumps(body), "ng|SN-MIGRATE-1|2026-11-21T00:00:00.000Z"),
            )
        summary = migrate(self.source, self.target, apply=True)
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["conflicts"], ["ng|SN-MIGRATE-1|2026-11-21T00:00:00.000Z"])
        with sqlite3.connect(self.target) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 3)

    def test_invalid_source_and_same_file_are_rejected(self):
        incomplete = self.root / "incomplete.sqlite"
        with sqlite3.connect(incomplete) as conn:
            conn.execute("CREATE TABLE queue(id TEXT)")
        with self.assertRaises(MigrationError):
            migrate(incomplete, self.target, apply=False)
        with self.assertRaises(MigrationError):
            migrate(self.source, self.source, apply=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
