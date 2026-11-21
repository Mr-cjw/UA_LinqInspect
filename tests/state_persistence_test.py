"""Tests for durable QC station state snapshots."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.qc_server import QCStation
from server.qc_state import QCOutboxEvent, QCStateStore, QCStationSnapshot
from server.qc_sqlite_state import SQLiteQCStateStore


class StatePersistenceTests(unittest.TestCase):
    @staticmethod
    def snapshot(work_order_id: str = "WO-001", count: int = 1) -> QCStationSnapshot:
        return QCStationSnapshot(
            status=1,
            work_order_id=work_order_id,
            target_count=5,
            inspect_count=count,
            current_part_sn=f"SN-{count:03d}",
        )

    def test_round_trip_preserves_work_order_and_counters(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            store = QCStateStore(path, window_size=3)
            snapshot = QCStationSnapshot(
                status=1,
                work_order_id="WO-001",
                target_count=5,
                inspect_count=4,
                ok_count=3,
                ng_count=1,
                window=[True, True, False, True],
                current_part_sn="SN-004",
                sn_day="20261121",
                sn_seq=4,
                last_result=True,
                last_defect="",
                last_confidence=0.91,
                last_image_id="IMG-004",
                last_timestamp="2026-11-21T00:00:00+00:00",
            )
            store.save(snapshot)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["schemaVersion"], 1)
            self.assertEqual(raw["workOrderId"], "WO-001")
            self.assertNotIn("schema_version", raw)
            restored = store.load()
            self.assertEqual(restored.status, 1)
            self.assertEqual(restored.work_order_id, "WO-001")
            self.assertEqual(restored.inspect_count, 4)
            self.assertEqual(restored.window, [True, False, True])
            self.assertEqual(restored.sn_seq, 4)
            self.assertEqual(restored.current_part_sn, "SN-004")

    def test_round_trip_preserves_pending_outbox(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            store = QCStateStore(path)
            event = QCOutboxEvent(
                event_id="evt-001",
                event_type="ng",
                fields={
                    "WorkOrderId": "WO-001",
                    "SerialNumber": "SN-001",
                    "CorrelationId": "corr-001",
                    "SourceEventId": "evt-001",
                    "EventVersion": 1,
                },
                severity=3,
                message="NG detected",
                created_at="2026-11-21T00:00:00+00:00",
            )
            store.save(QCStationSnapshot(status=1, work_order_id="WO-001", outbox=[event]))
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["outbox"][0]["eventVersion"], 1)
            self.assertEqual(raw["outbox"][0]["eventId"], "evt-001")
            restored = store.load()
            self.assertEqual(len(restored.outbox), 1)
            self.assertEqual(restored.outbox[0].event_type, "ng")
            self.assertEqual(restored.outbox[0].fields["CorrelationId"], "corr-001")

    def test_corrupt_outbox_entry_is_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text(json.dumps({
                "schemaVersion": 1,
                "status": 1,
                "workOrderId": "WO-001",
                "outbox": [
                    {"eventVersion": 1, "eventId": "evt-good", "eventType": "done", "fields": {}},
                    {"eventVersion": 99, "eventId": "evt-bad-version", "eventType": "ng", "fields": {}},
                    {"eventVersion": 1, "eventId": "", "eventType": "ng", "fields": {}},
                    "not-an-object",
                ],
            }), encoding="utf-8")
            restored = QCStateStore(path).load()
            self.assertEqual([event.event_id for event in restored.outbox], ["evt-good"])

    def test_invalid_running_snapshot_falls_back_to_idle(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text(json.dumps({
                "schemaVersion": 1,
                "status": 1,
                "workOrderId": "",
                "inspectCount": 3,
            }), encoding="utf-8")
            restored = QCStateStore(path).load()
            self.assertEqual(restored.status, 0)
            self.assertEqual(restored.inspect_count, 3)

    def test_corrupt_state_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text("not-json", encoding="utf-8")
            restored = QCStateStore(path).load()
            self.assertEqual(restored.status, 0)
            self.assertEqual(restored.inspect_count, 0)
            self.assertEqual(restored.window, [])

    def test_snapshot_is_private_and_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            QCStateStore(path).save(self.snapshot())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(Path(temp).glob(".state.json.tmp-*")), [])

    def test_write_failure_preserves_previous_snapshot_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            store = QCStateStore(path)
            store.save(self.snapshot("WO-OLD", 1))
            previous = path.read_bytes()
            with patch("server.qc_state.os.fsync", side_effect=OSError("simulated disk full")):
                with self.assertRaises(OSError):
                    store.save(self.snapshot("WO-NEW", 2))
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(store.load().work_order_id, "WO-OLD")
            self.assertEqual(list(Path(temp).glob(".state.json.tmp-*")), [])

    def test_replace_failure_preserves_previous_snapshot_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            store = QCStateStore(path)
            store.save(self.snapshot("WO-OLD", 1))
            previous = path.read_bytes()
            with patch("server.qc_state.os.replace", side_effect=OSError("simulated replace failure")):
                with self.assertRaises(OSError):
                    store.save(self.snapshot("WO-NEW", 2))
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(store.load().work_order_id, "WO-OLD")
            self.assertEqual(list(Path(temp).glob(".state.json.tmp-*")), [])

    def test_read_only_directory_preserves_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            path = directory / "state.json"
            store = QCStateStore(path)
            store.save(self.snapshot("WO-OLD", 1))
            previous = path.read_bytes()
            os.chmod(directory, 0o500)
            try:
                with self.assertRaises(PermissionError):
                    store.save(self.snapshot("WO-NEW", 2))
                self.assertEqual(path.read_bytes(), previous)
                self.assertEqual(store.load().work_order_id, "WO-OLD")
                self.assertEqual(list(directory.glob(".state.json.tmp-*")), [])
            finally:
                os.chmod(directory, 0o700)

    def test_directory_fsync_failure_keeps_committed_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            store = QCStateStore(path)
            calls = 0

            def fail_directory_sync(descriptor):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated directory fsync failure")

            with patch("server.qc_state.os.fsync", side_effect=fail_directory_sync):
                store.save(self.snapshot("WO-COMMITTED", 2))
            self.assertEqual(calls, 2)
            self.assertEqual(store.load().work_order_id, "WO-COMMITTED")
            self.assertEqual(list(Path(temp).glob(".state.json.tmp-*")), [])

    def test_sqlite_round_trip_preserves_state_and_pending_outbox(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "qc-edge.sqlite3"
            event = QCOutboxEvent(
                event_id="evt-sqlite-001",
                event_type="ng",
                fields={
                    "WorkOrderId": "WO-SQLITE-001",
                    "SerialNumber": "SN-SQLITE-001",
                    "CorrelationId": "corr-sqlite-001",
                    "SourceEventId": "evt-sqlite-001",
                    "EventVersion": 1,
                },
                severity=3,
                message="NG detected",
                created_at="2026-11-21T00:00:00+00:00",
            )
            snapshot = QCStationSnapshot(
                status=1,
                work_order_id="WO-SQLITE-001",
                target_count=5,
                inspect_count=1,
                ng_count=1,
                current_part_sn="SN-SQLITE-001",
                sn_day="20261121",
                sn_seq=1,
                outbox=[event],
            )
            SQLiteQCStateStore(db_path, window_size=3).save(snapshot)
            restored = SQLiteQCStateStore(db_path, window_size=3).load()
            self.assertEqual(restored.work_order_id, "WO-SQLITE-001")
            self.assertEqual(restored.inspect_count, 1)
            self.assertEqual(len(restored.outbox), 1)
            self.assertEqual(restored.outbox[0].event_id, "evt-sqlite-001")
            self.assertEqual(restored.outbox[0].fields["CorrelationId"], "corr-sqlite-001")

    def test_sqlite_failed_update_preserves_previous_state(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "qc-edge.sqlite3"
            store = SQLiteQCStateStore(db_path, window_size=3)
            store.save(self.snapshot("WO-SQLITE-OLD", 1))
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TRIGGER fail_state_update BEFORE UPDATE ON qc_station_state
                    BEGIN
                        SELECT RAISE(ABORT, 'simulated storage failure');
                    END;
                    """
                )
                conn.commit()
            with self.assertRaises(sqlite3.DatabaseError):
                store.save(self.snapshot("WO-SQLITE-NEW", 2))
            restored = store.load()
            self.assertEqual(restored.work_order_id, "WO-SQLITE-OLD")
            self.assertEqual(restored.inspect_count, 1)

    def test_sqlite_legacy_json_migration_preserves_outbox(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            legacy = root / "state.json"
            event = QCOutboxEvent(
                event_id="evt-migrate-001",
                event_type="done",
                fields={"WorkOrderId": "WO-MIGRATE-001", "CorrelationId": "corr-migrate-001"},
                severity=2,
                message="completed",
                created_at="2026-11-21T00:00:00+00:00",
            )
            QCStateStore(legacy, window_size=3).save(QCStationSnapshot(
                status=1,
                work_order_id="WO-MIGRATE-001",
                inspect_count=2,
                outbox=[event],
            ))
            store = SQLiteQCStateStore(root / "qc-edge.sqlite3", window_size=3, legacy_json_path=legacy)
            restored = store.load()
            self.assertEqual(restored.work_order_id, "WO-MIGRATE-001")
            self.assertEqual(restored.outbox[0].event_id, "evt-migrate-001")
            reopened = SQLiteQCStateStore(root / "qc-edge.sqlite3", window_size=3, legacy_json_path=legacy)
            self.assertEqual(reopened.load().outbox[0].event_id, "evt-migrate-001")


class FakeNode:
    def __init__(self):
        self.value = None

    async def write_value(self, value):
        self.value = value


class StateTransactionTests(unittest.IsolatedAsyncioTestCase):
    def station(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        return QCStation(None, state_store=QCStateStore(root / "state.json")), root

    async def test_failed_persist_rolls_back_durable_fields(self):
        station, _ = self.station()
        await station._persist()
        station.status = 1
        station.work_order_id = "WO-UNCOMMITTED"
        station.inspect_count = 7
        with patch.object(station.state_store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await station._persist()
        self.assertEqual(station.status, 0)
        self.assertEqual(station.work_order_id, "")
        self.assertEqual(station.inspect_count, 0)
        self.assertEqual(station.state_store.load().work_order_id, "")

    async def test_successful_persist_advances_rollback_baseline(self):
        station, _ = self.station()
        station.status = 1
        station.work_order_id = "WO-COMMITTED"
        await station._persist()
        station.work_order_id = "WO-UNCOMMITTED"
        with patch.object(station.state_store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await station._persist()
        self.assertEqual(station.status, 1)
        self.assertEqual(station.work_order_id, "WO-COMMITTED")
        self.assertEqual(station.state_store.load().work_order_id, "WO-COMMITTED")

    async def test_inspection_failure_rolls_back_counters_and_outbox(self):
        station, _ = self.station()
        station.status = 1
        station.work_order_id = "WO-INSPECT"
        await station._persist()
        with patch.object(station.state_store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await station.record_inspection(False, "DENT", 0.9, "IMG-1", "SN-1")
        self.assertEqual(station.inspect_count, 0)
        self.assertEqual(station.ok_count, 0)
        self.assertEqual(station.ng_count, 0)
        self.assertEqual(station.current_part_sn, "")
        self.assertEqual(station.outbox, [])

    async def test_ack_failure_rolls_back_outbox_and_ack_timestamp(self):
        station, _ = self.station()
        event = station._make_event(
            "ng", {"WorkOrderId": "WO-1"}, severity=3, message="NG", correlation_id="corr-1"
        )
        station._append_outbox(event)
        await station._persist()
        with patch.object(station.state_store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await station._acknowledge_event(None, event.event_id)
        self.assertEqual([item.event_id for item in station.outbox], [event.event_id])
        self.assertEqual(station.outbox_last_ack_at, "")

    async def test_purge_failure_rolls_back_outbox_and_purge_total(self):
        station, _ = self.station()
        event = station._make_event(
            "ng", {"WorkOrderId": "WO-1"}, severity=3, message="NG", correlation_id="corr-1"
        )
        station._append_outbox(event)
        await station._persist()
        with patch.object(station.state_store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await station._purge_outbox(None, station._outbox_purge_token())
        self.assertEqual([item.event_id for item in station.outbox], [event.event_id])
        self.assertEqual(station.outbox_purged_total, 0)
        self.assertEqual(station.outbox_last_ack_at, "")

    async def test_failed_persist_resynchronizes_ua_state(self):
        station, _ = self.station()
        names = (
            "status", "work_order", "part_sn", "last_result", "last_defect", "last_conf",
            "last_img", "last_ts", "cnt_all", "cnt_ok", "cnt_ng", "ng_rate",
            "outbox_status", "outbox_pending", "outbox_oldest_age", "outbox_oldest_at",
            "outbox_capacity", "outbox_high_watermark", "outbox_backpressure",
        )
        station.nd = {name: FakeNode() for name in names}
        await station._persist()
        station.status = 1
        station.work_order_id = "WO-UNCOMMITTED"
        with patch.object(station.state_store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await station._persist()
        status_value = station.nd["status"].value
        self.assertEqual(getattr(status_value, "Value", status_value), 0)
        self.assertEqual(station.nd["work_order"].value, "")
        self.assertEqual(station.nd["outbox_pending"].value.Value, 0)


if __name__ == "__main__":
    unittest.main()
