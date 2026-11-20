"""Unit tests for the durable QC event outbox."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from server.qc_server import QCStation, ST_RUNNING
from server.qc_state import QCStateStore


class FakeNode:
    def __init__(self):
        self.values = []

    async def write_value(self, value):
        self.values.append(value)


class FakeServer:
    pass


class EventOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_path = Path(self.temp.name) / "state.json"
        self.store = QCStateStore(self.state_path, window_size=20)
        self.station = QCStation(FakeServer(), state_store=self.store)
        self.station.status = ST_RUNNING
        self.station.work_order_id = "WO-OUTBOX-001"
        self.station.target_count = 10
        self.station.nd = {
            name: FakeNode()
            for name in (
                "last_result", "last_defect", "last_conf", "last_img", "last_ts",
                "part_sn", "cnt_all", "cnt_ok", "cnt_ng", "ng_rate",
            )
        }

    async def test_publish_failure_keeps_state_and_pending_event(self):
        async def fail_publish(event):
            raise RuntimeError("UA subscription unavailable")

        self.station._publish_event = fail_publish
        accepted = await self.station.record_inspection(
            False, "SCRATCH", 0.91, "IMG-OUTBOX-001", "SN-OUTBOX-001"
        )
        self.assertTrue(accepted)

        restored = self.store.load()
        self.assertEqual(restored.inspect_count, 1)
        self.assertEqual(restored.ng_count, 1)
        self.assertEqual(len(restored.outbox), 1)
        event = restored.outbox[0]
        self.assertEqual(event.event_type, "ng")
        self.assertEqual(event.fields["SerialNumber"], "SN-OUTBOX-001")
        self.assertTrue(str(event.fields["SourceEventId"]).startswith("evt-"))
        self.assertTrue(str(event.fields["CorrelationId"]).startswith("corr-"))

    async def test_replay_keeps_stable_ids_until_ack(self):
        captured = []

        async def capture_publish(event):
            captured.append(event)

        self.station._publish_event = capture_publish
        event = self.station._make_event(
            "done",
            {"WorkOrderId": "WO-OUTBOX-001", "InspectCount": 1},
            severity=2,
            message="completed",
            correlation_id="corr-stable-001",
        )
        self.station._append_outbox(event)
        await self.station._persist()

        await self.station.publish_pending_outbox()
        await self.station.publish_pending_outbox()
        self.assertEqual(len(captured), 2)
        self.assertEqual({item.event_id for item in captured}, {event.event_id})
        self.assertEqual({item.fields["CorrelationId"] for item in captured}, {"corr-stable-001"})
        self.assertEqual(len(self.store.load().outbox), 1)

        result, changed = self.station._acknowledge_event_impl(event.event_id)
        self.assertTrue(changed)
        self.assertTrue(result[0])
        await self.station._persist()
        self.assertEqual(self.store.load().outbox, [])

    async def test_ack_is_idempotent_and_accepts_correlation_id(self):
        event = self.station._make_event(
            "ng",
            {"WorkOrderId": "WO-OUTBOX-001", "SerialNumber": "SN-ACK-001"},
            severity=3,
            message="NG",
            correlation_id="corr-ack-001",
        )
        self.station._append_outbox(event)
        result, changed = self.station._acknowledge_event_impl("corr-ack-001")
        self.assertTrue(changed)
        self.assertTrue(result[0])

        result, changed = self.station._acknowledge_event_impl("corr-ack-001")
        self.assertFalse(changed)
        self.assertTrue(result[0])


if __name__ == "__main__":
    unittest.main()
