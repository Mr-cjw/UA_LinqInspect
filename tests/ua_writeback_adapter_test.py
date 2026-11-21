"""Tests for the Edge Core OPC UA receipt writeback adapter."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import unittest
import uuid
from pathlib import Path

from edge_core.api import create_api_server
from edge_core.store import DeliveryStore, STATE_DONE
from edge_core.ua_writeback import (
    UaReceiptWriter,
    UaWritebackConfig,
    process_pending,
)

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from ua_test_client import (  # noqa: E402
    CLIENT_CERT,
    CLIENT_KEY,
    SERVER_CERT,
    admin_credentials,
    endpoint_url,
)


class FakeApi:
    def __init__(self, items):
        self.items = items
        self.reports = []

    def list_ua_pending(self, limit=50):
        return self.items[:limit]

    def report_result(self, delivery_id, *, success, correlation_id, error_code=None, error_message=None):
        self.reports.append({
            "deliveryId": delivery_id,
            "success": success,
            "correlationId": correlation_id,
            "errorCode": error_code,
            "errorMessage": error_message,
        })
        return {}


class FakeWriter:
    async def write(self, delivery):
        if delivery.get("reject"):
            raise RuntimeError("simulated UA rejection")
        return "recorded"


class UaWritebackAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_pending_reports_success_and_failure(self):
        api = FakeApi([
            {"deliveryId": str(uuid.uuid4()), "correlationId": "corr-side-ok-0001"},
            {"deliveryId": str(uuid.uuid4()), "correlationId": "corr-side-fail-0001", "reject": True},
        ])
        summary = await process_pending(api, FakeWriter(), limit=10)
        self.assertEqual(summary, {"listed": 2, "confirmed": 1, "failed": 1})
        self.assertTrue(api.reports[0]["success"])
        self.assertFalse(api.reports[1]["success"])
        self.assertEqual(api.reports[1]["errorCode"], "UA_WRITE_FAILED")

    async def test_missing_receipt_fails_without_reporting_success(self):
        item = {
            "deliveryId": str(uuid.uuid4()),
            "correlationId": "corr-side-missing-0001",
            "mesReceipt": None,
            "workOrderId": "WO-MISSING",
        }
        api = FakeApi([item])
        config = UaWritebackConfig(
            edge_api_base_url="http://127.0.0.1:1",
            edge_api_token="unused",
            ua_endpoint="opc.tcp://127.0.0.1:1",
            client_cert=CLIENT_CERT,
            client_key=CLIENT_KEY,
            server_cert=SERVER_CERT,
            admin_user="unused",
            admin_pass="unused",
        )
        summary = await process_pending(api, UaReceiptWriter(config), limit=10)
        self.assertEqual(summary["failed"], 1)
        self.assertFalse(api.reports[0]["success"])
        self.assertEqual(api.reports[0]["errorCode"], "UA_WRITE_FAILED")

    @unittest.skipUnless(
        all(path.is_file() for path in (CLIENT_CERT, CLIENT_KEY, SERVER_CERT)) and os.getenv("UA_ADMIN_USER"),
        "requires secure UA server, test certificates and admin credentials",
    )
    async def test_real_ua_receipt_moves_delivery_to_done(self):
        import tempfile

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        store = DeliveryStore(root / "edge.sqlite3")
        correlation_id = "corr-ua-adapter-real-0001"
        event = {
            "schemaVersion": "1.0",
            "sourceEventId": "evt-ua-adapter-real-0001",
            "stationId": "QCStation",
            "eventType": "NG_ORDER",
            "occurredAt": "2026-11-21T00:00:00.000Z",
            "result": "NG",
            "serialNumber": "SN-UA-ADAPTER-0001",
            "workOrderId": "WO-UA-ADAPTER-0001",
            "defectType": "SCRATCH",
            "confidence": 0.9,
            "imageId": "IMG-UA-ADAPTER-0001",
            "correlationId": correlation_id,
        }
        token = "adapter-test-token"
        delivery, _ = store.enqueue(
            event,
            url="http://unused/mes/ng-order",
            idempotency_key=str(uuid.uuid4()),
            correlation_id=correlation_id,
        )
        claimed = store.claim_due(lease_seconds=30)
        self.assertEqual(claimed["deliveryId"], delivery["deliveryId"])
        receipt_no = "MES-UA-ADAPTER-0001"
        store.mark_acked(delivery["deliveryId"], {
            "accepted": True,
            "schemaVersion": "1.0",
            "correlationId": correlation_id,
            "receiptType": "MES_ORDER",
            "receiptNo": receipt_no,
            "mesOrderNo": receipt_no,
            "receivedAt": "2026-11-21T00:00:01.000Z",
        })
        api = create_api_server("127.0.0.1", 0, store, token, "http://unused")
        api.quiet = True  # type: ignore[attr-defined]
        thread = threading.Thread(target=api.serve_forever, daemon=True)
        thread.start()
        try:
            from edge_core.ua_writeback import EdgeCoreApiClient

            client = EdgeCoreApiClient(
                f"http://127.0.0.1:{api.server_address[1]}",
                token,
                timeout=5,
            )
            username, password = admin_credentials()
            config = UaWritebackConfig(
                edge_api_base_url=f"http://127.0.0.1:{api.server_address[1]}",
                edge_api_token=token,
                ua_endpoint=endpoint_url(),
                client_cert=CLIENT_CERT,
                client_key=CLIENT_KEY,
                server_cert=SERVER_CERT,
                admin_user=username,
                admin_pass=password,
            )
            summary = await process_pending(client, UaReceiptWriter(config), limit=10)
            final = store.get(delivery["deliveryId"])
            self.assertEqual(
                summary["confirmed"], 1,
                f"state={final['state']} error={final['lastErrorCode']} message={final['lastErrorMessage']}",
            )
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(final["state"], STATE_DONE)
            self.assertTrue(final["uaWriteConfirmed"])
        finally:
            api.shutdown()
            api.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
