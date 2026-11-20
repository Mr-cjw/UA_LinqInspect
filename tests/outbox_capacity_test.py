"""Unit tests for outbox backpressure, export and authenticated purge semantics."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from server import qc_server
from server.qc_server import (
    OutboxCapacityError,
    QCStation,
    ST_IDLE,
    ST_RUNNING,
    _val,
)
from server.qc_state import QCOutboxEvent, QCStateStore


class FakeNode:
    def __init__(self):
        self.values = []

    async def write_value(self, value):
        self.values.append(value)


class FakeServer:
    pass


class OutboxCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_max = qc_server.OUTBOX_MAX_PENDING
        self.original_high = qc_server.OUTBOX_HIGH_WATERMARK
        qc_server.OUTBOX_MAX_PENDING = 3
        qc_server.OUTBOX_HIGH_WATERMARK = 2
        self.addCleanup(self._restore_limits)

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_path = Path(self.temp.name) / "state.json"
        self.store = QCStateStore(self.state_path, window_size=20)
        self.station = QCStation(FakeServer(), state_store=self.store)
        self.station.status = ST_RUNNING
        self.station.work_order_id = "WO-CAPACITY-001"
        self.station.target_count = 10
        self.station.nd = {
            name: FakeNode()
            for name in (
                "status", "work_order", "last_result", "last_defect", "last_conf",
                "last_img", "last_ts", "part_sn", "cnt_all", "cnt_ok", "cnt_ng",
                "ng_rate", "outbox_status", "outbox_pending", "outbox_oldest_age",
                "outbox_oldest_at", "outbox_capacity", "outbox_high_watermark",
                "outbox_backpressure",
            )
        }

    def _restore_limits(self):
        qc_server.OUTBOX_MAX_PENDING = self.original_max
        qc_server.OUTBOX_HIGH_WATERMARK = self.original_high

    def _event(self, index: int, event_type: str = "ng") -> QCOutboxEvent:
        event_id = f"evt-capacity-{index}"
        return QCOutboxEvent(
            event_id=event_id,
            event_type=event_type,
            fields={
                "WorkOrderId": "WO-CAPACITY-001",
                "SerialNumber": f"SN-CAPACITY-{index}",
                "CorrelationId": f"corr-capacity-{index}",
                "SourceEventId": event_id,
                "EventVersion": 1,
            },
            severity=3,
            message=f"pending event {index}",
            created_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        )

    def _fill_outbox(self, count: int) -> None:
        self.station.outbox = [self._event(index) for index in range(1, count + 1)]

    async def test_backpressure_blocks_start_without_mutating_state(self):
        self.station.status = ST_IDLE
        self.station.work_order_id = ""
        self.station.target_count = 0
        self._fill_outbox(2)

        result = await self.station._start_inspection(None, "WO-BLOCKED", 5)

        self.assertFalse(_val(result[0]))
        self.assertIn("backpressure", str(_val(result[1])))
        self.assertEqual(self.station.status, ST_IDLE)
        self.assertEqual(self.station.work_order_id, "")
        self.assertEqual(len(self.station.outbox), 2)
        self.assertEqual(self.station.outbox_rejected_total, 1)

    async def test_full_outbox_rejects_inspection_before_counters_change(self):
        self._fill_outbox(3)
        self.station.inspect_count = 7
        self.station.ok_count = 5
        self.station.ng_count = 2

        accepted = await self.station.record_inspection(
            False, "DENT", 0.88, "IMG-CAPACITY-BLOCKED", "SN-CAPACITY-BLOCKED"
        )

        self.assertFalse(accepted)
        self.assertEqual(self.station.inspect_count, 7)
        self.assertEqual(self.station.ok_count, 5)
        self.assertEqual(self.station.ng_count, 2)
        self.assertEqual(len(self.station.outbox), 3)
        self.assertEqual(self.station.outbox_rejected_total, 1)

    async def test_full_outbox_rejects_completion_and_keeps_running(self):
        self._fill_outbox(3)

        result = await self.station._complete_work_order(None, "WO-CAPACITY-001")

        self.assertFalse(_val(result[0]))
        self.assertIn("capacity", str(_val(result[1])))
        self.assertEqual(self.station.status, ST_RUNNING)
        self.assertEqual(self.station.work_order_id, "WO-CAPACITY-001")
        self.assertEqual(len(self.station.outbox), 3)

    async def test_completion_below_capacity_is_persisted_and_publishes_status(self):
        self._fill_outbox(2)

        result = await self.station._complete_work_order(None, "WO-CAPACITY-001")

        self.assertTrue(_val(result[0]))
        self.assertEqual(self.station.status, ST_IDLE)
        self.assertEqual(len(self.station.outbox), 3)
        restored = self.store.load()
        self.assertEqual(len(restored.outbox), 3)
        status = json.loads(self.station.nd["outbox_status"].values[-1])
        self.assertEqual(status["pendingCount"], 3)
        self.assertTrue(status["backpressure"])
        self.assertEqual(_val(self.station.nd["outbox_pending"].values[-1]), 3)
        self.assertTrue(_val(self.station.nd["outbox_backpressure"].values[-1]))

    async def test_export_is_paged_and_purge_requires_matching_token(self):
        self._fill_outbox(3)
        await self.station._persist()

        export, summary = self.station._export_pending_events_impl(1, 1)

        self.assertTrue(_val(export[0]))
        self.assertIn("exported 1 of 3", summary)
        document = json.loads(_val(export[1]))
        self.assertEqual(document["pendingCount"], 3)
        self.assertEqual(document["returned"], 1)
        self.assertTrue(document["hasMore"])
        self.assertEqual(document["events"][0]["eventId"], "evt-capacity-2")

        wrong = await self.station._purge_outbox(None, "purge-3-stale")
        self.assertFalse(_val(wrong[0]))
        self.assertEqual(len(self.station.outbox), 3)
        self.assertEqual(len(self.store.load().outbox), 3)

        purged = await self.station._purge_outbox(None, document["purgeToken"])
        self.assertTrue(_val(purged[0]))
        self.assertEqual(self.station.outbox, [])
        self.assertEqual(self.store.load().outbox, [])
        self.assertEqual(self.station.outbox_purged_total, 3)

        again = await self.station._purge_outbox(None, document["purgeToken"])
        self.assertTrue(_val(again[0]))
        self.assertIn("already empty", str(_val(again[1])))

    async def test_ack_decrements_exported_status_and_keeps_remaining_event(self):
        self._fill_outbox(2)

        result = await self.station._acknowledge_event(None, "evt-capacity-1")

        self.assertTrue(_val(result[0]))
        self.assertEqual(len(self.station.outbox), 1)
        self.assertEqual(len(self.store.load().outbox), 1)
        status = json.loads(self.station.nd["outbox_status"].values[-1])
        self.assertEqual(status["pendingCount"], 1)
        self.assertFalse(status["backpressure"])
        self.assertTrue(status["lastAckAt"])

    async def test_restart_restores_pending_count_and_backpressure(self):
        self._fill_outbox(3)
        await self.station._persist()

        restored_station = QCStation(FakeServer(), state_store=self.store)
        restored_station.nd = {
            "outbox_status": FakeNode(),
            "outbox_pending": FakeNode(),
            "outbox_oldest_age": FakeNode(),
            "outbox_oldest_at": FakeNode(),
            "outbox_capacity": FakeNode(),
            "outbox_high_watermark": FakeNode(),
            "outbox_backpressure": FakeNode(),
        }
        await restored_station._sync_outbox_status()

        status = json.loads(restored_station.nd["outbox_status"].values[-1])
        self.assertEqual(status["pendingCount"], 3)
        self.assertEqual(status["capacity"], 3)
        self.assertTrue(status["backpressure"])

    async def test_append_fails_closed_at_capacity(self):
        self._fill_outbox(3)

        with self.assertRaises(OutboxCapacityError):
            self.station._append_outbox(self._event(99))

        self.assertEqual(len(self.station.outbox), 3)
        self.assertEqual(self.station.outbox_rejected_total, 1)

    async def test_virtual_identity_pauses_at_capacity(self):
        self._fill_outbox(3)

        identity = await self.station._next_virtual_identity()

        self.assertIsNone(identity)
        self.assertEqual(self.station.sn_seq, 0)


if __name__ == "__main__":
    unittest.main()
