"""Tests for durable QC station state snapshots."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from server.qc_state import QCOutboxEvent, QCStateStore, QCStationSnapshot


class StatePersistenceTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
