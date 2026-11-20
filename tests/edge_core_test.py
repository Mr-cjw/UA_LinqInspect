"""Unit/integration tests for the SQLite Edge Core vertical slice."""

from __future__ import annotations

import json
import tempfile
import time
import threading
import unittest
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from edge_core.api import create_api_server
from edge_core.store import (
    ConflictError,
    DeliveryStore,
    STATE_DEAD,
    STATE_DONE,
    STATE_UA_WRITE_PENDING,
)
from edge_core.worker import DeliveryWorker


def make_event(*, source_event_id: str | None = None, defect: str = "SCRATCH") -> dict:
    correlation_id = f"corr-{uuid.uuid4()}"
    return {
        "schemaVersion": "1.0",
        "sourceEventId": source_event_id or f"evt-{uuid.uuid4()}",
        "stationId": "STATION-01",
        "eventType": "NG_ORDER",
        "occurredAt": "2026-11-20T12:34:56.789Z",
        "result": "NG",
        "serialNumber": f"SN-{uuid.uuid4().hex[:12]}",
        "workOrderId": "WO-20261120-001",
        "defectType": defect,
        "confidence": 0.93,
        "imageId": f"IMG-{uuid.uuid4().hex[:12]}",
        "correlationId": correlation_id,
        "metadata": {"source": "unit-test"},
    }


class FakeMesHandler(BaseHTTPRequestHandler):
    mode = "ok"
    requests: list[dict] = []
    receipt_seq = 0

    def log_message(self, _fmt, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        cls = type(self)
        cls.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        if cls.mode == "4xx":
            self._send(400, {"accepted": False, "errorCode": "MES_REJECTED", "error": "rejected by test"})
            return
        if cls.mode == "5xx":
            self._send(500, {"accepted": False, "errorCode": "MES_UNAVAILABLE", "error": "unavailable"})
            return
        if cls.mode == "invalid":
            self._send(200, {"accepted": True, "schemaVersion": "0.9", "correlationId": "wrong", "receiptNo": "BAD"})
            return
        cls.receipt_seq += 1
        self._send(200, {
            "accepted": True,
            "schemaVersion": "1.0",
            "correlationId": body["correlationId"],
            "receiptNo": f"MES-20261120-{cls.receipt_seq:04d}",
        })

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class EdgeCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DeliveryStore(Path(self.temp.name) / "edge.sqlite3")
        FakeMesHandler.mode = "ok"
        FakeMesHandler.requests = []
        self.mes = ThreadingHTTPServer(("127.0.0.1", 0), FakeMesHandler)
        self.mes_thread = threading.Thread(target=self.mes.serve_forever, daemon=True)
        self.mes_thread.start()
        self.addCleanup(self.mes.server_close)
        self.addCleanup(self.mes.shutdown)
        self.mes_url = f"http://127.0.0.1:{self.mes.server_address[1]}"

    def worker(self, max_attempts: int = 4) -> DeliveryWorker:
        return DeliveryWorker(
            self.store,
            mes_base_url=self.mes_url,
            max_attempts=max_attempts,
            request_timeout=2,
            retry_delays=(0.01, 0.01, 0.01),
        )

    def test_duplicate_is_idempotent_and_payload_conflict_is_rejected(self):
        event = make_event(source_event_id="evt-001")
        key = str(uuid.uuid4())
        delivery, duplicate = self.store.enqueue(
            event,
            url=self.mes_url + "/mes/ng-order",
            idempotency_key=key,
            correlation_id=event["correlationId"],
        )
        self.assertFalse(duplicate)
        again, duplicate = self.store.enqueue(
            event,
            url=self.mes_url + "/mes/ng-order",
            idempotency_key=str(uuid.uuid4()),
            correlation_id=event["correlationId"],
        )
        self.assertTrue(duplicate)
        self.assertEqual(delivery["deliveryId"], again["deliveryId"])

        changed = dict(event)
        changed["defectType"] = "DENT"
        with self.assertRaises(ConflictError):
            self.store.enqueue(
                changed,
                url=self.mes_url + "/mes/ng-order",
                idempotency_key=str(uuid.uuid4()),
                correlation_id=changed["correlationId"],
            )

    def test_successful_mes_receipt_then_ua_write_completes_delivery(self):
        event = make_event()
        delivery, _ = self.store.enqueue(
            event,
            url=self.mes_url + "/mes/ng-order",
            idempotency_key=str(uuid.uuid4()),
            correlation_id=event["correlationId"],
        )
        self.assertTrue(self.worker().run_once())
        current = self.store.get(delivery["deliveryId"])
        self.assertEqual(current["state"], STATE_UA_WRITE_PENDING)
        self.assertTrue(current["mesReceipt"]["receiptNo"].startswith("MES-"))
        self.assertEqual(FakeMesHandler.requests[0]["path"], "/mes/ng-order")
        self.assertEqual(FakeMesHandler.requests[0]["headers"]["X-Correlation-Id"], event["correlationId"])

        done = self.store.mark_ua_write_result(
            delivery["deliveryId"],
            success=True,
            correlation_id=event["correlationId"],
        )
        self.assertEqual(done["state"], STATE_DONE)
        self.assertTrue(done["uaWriteConfirmed"])

    def test_4xx_is_terminal_and_replay_preserves_business_key(self):
        event = make_event()
        delivery, _ = self.store.enqueue(
            event,
            url=self.mes_url + "/mes/ng-order",
            idempotency_key=str(uuid.uuid4()),
            correlation_id=event["correlationId"],
        )
        FakeMesHandler.mode = "4xx"
        self.worker().run_once()
        dead = self.store.get(delivery["deliveryId"])
        self.assertEqual(dead["state"], STATE_DEAD)
        self.assertEqual(dead["lastErrorCode"], "MES_REJECTED")
        self.assertEqual(len(FakeMesHandler.requests), 1)
        replay = self.store.replay_dead(delivery["deliveryId"], actor="tester", reason="MES corrected")
        self.assertEqual(replay["state"], "PENDING")
        self.assertEqual(replay["businessKey"], delivery["businessKey"])

    def test_invalid_2xx_enters_dead_without_retry(self):
        event = make_event()
        delivery, _ = self.store.enqueue(
            event,
            url=self.mes_url + "/mes/ng-order",
            idempotency_key=str(uuid.uuid4()),
            correlation_id=event["correlationId"],
        )
        FakeMesHandler.mode = "invalid"
        self.worker().run_once()
        dead = self.store.get(delivery["deliveryId"])
        self.assertEqual(dead["state"], STATE_DEAD)
        self.assertEqual(dead["lastErrorCode"], "INVALID_RESPONSE_SCHEMA")
        self.assertEqual(len(FakeMesHandler.requests), 1)

    def test_5xx_retries_then_dead(self):
        event = make_event()
        delivery, _ = self.store.enqueue(
            event,
            url=self.mes_url + "/mes/ng-order",
            idempotency_key=str(uuid.uuid4()),
            correlation_id=event["correlationId"],
        )
        FakeMesHandler.mode = "5xx"
        worker = self.worker(max_attempts=2)
        worker.run_once()
        waiting = self.store.get(delivery["deliveryId"])
        self.assertEqual(waiting["state"], "RETRY_WAIT")
        time.sleep(0.02)
        worker.run_once()
        dead = self.store.get(delivery["deliveryId"])
        self.assertEqual(dead["state"], STATE_DEAD)
        self.assertEqual(dead["lastErrorCode"], "MES_UNAVAILABLE")
        self.assertEqual(len(FakeMesHandler.requests), 2)

    def test_business_key_survives_store_reopen(self):
        event = make_event(source_event_id="evt-persisted")
        key = str(uuid.uuid4())
        first, _ = self.store.enqueue(
            event,
            url=self.mes_url + "/mes/ng-order",
            idempotency_key=key,
            correlation_id=event["correlationId"],
        )
        reopened = DeliveryStore(self.store.db_path)
        again, duplicate = reopened.enqueue(
            event,
            url=self.mes_url + "/mes/ng-order",
            idempotency_key=str(uuid.uuid4()),
            correlation_id=event["correlationId"],
        )
        self.assertTrue(duplicate)
        self.assertEqual(first["deliveryId"], again["deliveryId"])

    def test_api_ingest_and_query(self):
        token = "test-token"
        api = create_api_server("127.0.0.1", 0, self.store, token, self.mes_url)
        thread = threading.Thread(target=api.serve_forever, daemon=True)
        thread.start()
        try:
            event = make_event()
            key = str(uuid.uuid4())
            body = json.dumps(event).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{api.server_address[1]}/v1/inspection-events",
                data=body,
                headers={
                    "content-type": "application/json",
                    "authorization": f"Bearer {token}",
                    "x-correlation-id": event["correlationId"],
                    "idempotency-key": key,
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 202)
                created = json.loads(response.read())
            self.assertEqual(created["deliveryId"], key)

            query = urllib.request.Request(
                f"http://127.0.0.1:{api.server_address[1]}/v1/deliveries/{key}",
                headers={"authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(query, timeout=5) as response:
                fetched = json.loads(response.read())
            self.assertEqual(fetched["correlationId"], event["correlationId"])
        finally:
            api.shutdown()
            api.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
