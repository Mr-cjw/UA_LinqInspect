"""Unit/integration tests for the SQLite Edge Core vertical slice."""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import time
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from edge_core.api import create_api_server
from edge_core.config import load_config
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


def make_completion(*, source_event_id: str | None = None, inspect_count: int = 5) -> dict:
    correlation_id = f"corr-{uuid.uuid4()}"
    return {
        "schemaVersion": "1.0",
        "sourceEventId": source_event_id or f"evt-{uuid.uuid4()}",
        "stationId": "STATION-01",
        "eventType": "COMPLETION_REPORT",
        "occurredAt": "2026-11-20T12:35:56.789Z",
        "workOrderId": "WO-20261120-001",
        "inspectCount": inspect_count,
        "okCount": max(0, inspect_count - 1),
        "ngCount": 1 if inspect_count else 0,
        "ngRate": round(1 / inspect_count, 3) if inspect_count else -1.0,
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
        if cls.mode == "missing-ng-receipt":
            self._send(200, {
                "accepted": True,
                "schemaVersion": "1.0",
                "correlationId": body["correlationId"],
                "receiptNo": "MES-WRONG-FIELD",
            })
            return
        if cls.mode == "missing-completion-receipt":
            self._send(200, {
                "accepted": True,
                "schemaVersion": "1.0",
                "correlationId": body["correlationId"],
                "mesOrderNo": "RC-WRONG-FIELD",
            })
            return
        cls.receipt_seq += 1
        receipt_field = "mesOrderNo" if self.path == "/mes/ng-order" else "receiptNo"
        prefix = "MES" if receipt_field == "mesOrderNo" else "RC"
        self._send(200, {
            "accepted": True,
            "schemaVersion": "1.0",
            "correlationId": body["correlationId"],
            receipt_field: f"{prefix}-20261120-{cls.receipt_seq:04d}",
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

    @staticmethod
    def api_post(api, token: str, event: dict, key: str, correlation_id: str | None = None):
        body = json.dumps(event).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{api.server_address[1]}/v1/inspection-events",
            data=body,
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {token}",
                "x-correlation-id": correlation_id or event["correlationId"],
                "idempotency-key": key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read())
            finally:
                exc.close()

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

    def test_mes_auth_configuration_rejects_ambiguous_credentials(self):
        with self.assertRaises(ValueError):
            load_config({"EDGE_API_TOKEN": "test", "EDGE_MES_BASIC_USER": "mes"})
        with self.assertRaises(ValueError):
            load_config({
                "EDGE_API_TOKEN": "test",
                "MES_BEARER_TOKEN": "bearer",
                "EDGE_MES_BASIC_USER": "mes",
                "EDGE_MES_BASIC_PASS": "secret",
            })
        config = load_config({
            "EDGE_API_TOKEN": "test",
            "EDGE_MES_BASIC_USER": "mes",
            "EDGE_MES_BASIC_PASS": "secret",
        })
        self.assertEqual(config.mes_basic_user, "mes")
        self.assertEqual(config.mes_basic_pass, "secret")

    def test_mes_basic_auth_is_sent_to_mes(self):
        event = make_event()
        delivery, _ = self.store.enqueue(
            event,
            url=self.mes_url + "/mes/ng-order",
            idempotency_key=str(uuid.uuid4()),
            correlation_id=event["correlationId"],
        )
        worker = DeliveryWorker(
            self.store,
            mes_base_url=self.mes_url,
            basic_user="mes-user",
            basic_pass="mes-secret",
            request_timeout=2,
        )
        self.assertTrue(worker.run_once())
        expected = "Basic " + base64.b64encode(b"mes-user:mes-secret").decode("ascii")
        self.assertEqual(FakeMesHandler.requests[0]["headers"]["Authorization"], expected)
        self.assertEqual(self.store.get(delivery["deliveryId"])["state"], STATE_UA_WRITE_PENDING)

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
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "contracts" / "delivery-record.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(set(current), set(schema["required"]))
        self.assertEqual(current["schemaVersion"], "1.0")
        self.assertEqual(current["workOrderId"], "WO-20261120-001")
        self.assertEqual(current["state"], STATE_UA_WRITE_PENDING)
        self.assertTrue(current["mesReceipt"]["receiptNo"].startswith("MES-"))
        self.assertEqual(current["mesReceipt"]["receiptType"], "MES_ORDER")
        self.assertIn("receivedAt", current["mesReceipt"])
        self.assertEqual(FakeMesHandler.requests[0]["path"], "/mes/ng-order")
        self.assertEqual(FakeMesHandler.requests[0]["headers"]["X-Correlation-Id"], event["correlationId"])
        self.assertEqual(
            FakeMesHandler.requests[0]["headers"]["Idempotency-Key"],
            event["sourceEventId"],
        )

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

    def test_evidence_bundle_reconstructs_inspection_delivery_and_audit(self):
        event = make_event()
        event["metadata"] = {
            "modelVersion": "synthetic-vision-mlp-v1",
            "modelSha256": "a" * 64,
            "datasetVersion": "procedural-synthetic-v1",
            "imageSha256": "b" * 64,
            "imageHashScope": "canonical-png-bytes",
            "inferenceMs": 4.2,
        }
        delivery, _ = self.store.enqueue(
            event,
            url=self.mes_url + "/mes/ng-order",
            idempotency_key=str(uuid.uuid4()),
            correlation_id=event["correlationId"],
        )
        self.worker().run_once()
        self.store.mark_ua_write_result(
            delivery["deliveryId"],
            success=True,
            correlation_id=event["correlationId"],
        )
        api = create_api_server("127.0.0.1", 0, self.store, "test-token", self.mes_url)
        api.quiet = True  # type: ignore[attr-defined]
        thread = threading.Thread(target=api.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{api.server_address[1]}/v1/deliveries/{delivery['deliveryId']}/evidence",
                headers={"authorization": "Bearer test-token"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                bundle = json.loads(response.read().decode("utf-8"))
        finally:
            api.shutdown()
            api.server_close()
            thread.join(timeout=5)

        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "contracts" / "evidence-bundle.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(set(schema["required"]) <= set(bundle))
        self.assertEqual(bundle["bundleType"], "QC_DELIVERY_EVIDENCE")
        self.assertEqual(bundle["deliveryId"], delivery["deliveryId"])
        self.assertEqual(bundle["inspection"]["serialNumber"], event["serialNumber"])
        self.assertEqual(bundle["inspection"]["defectType"], event["defectType"])
        self.assertEqual(bundle["visionLineage"]["modelVersion"], "synthetic-vision-mlp-v1")
        self.assertEqual(bundle["visionLineage"]["imageSha256"], "b" * 64)
        self.assertEqual(bundle["mes"]["receipt"]["receiptType"], "MES_ORDER")
        self.assertTrue(bundle["ua"]["writeConfirmed"])
        self.assertTrue(bundle["completeness"]["complete"])
        self.assertTrue(bundle["completeness"]["modelLineagePresent"])
        self.assertGreaterEqual(len(bundle["audit"]), 4)
        transitions = [(item["fromState"], item["toState"]) for item in bundle["audit"]]
        self.assertIn(("PENDING", "SENDING"), transitions)
        self.assertIn(("SENDING", "UA_WRITE_PENDING"), transitions)
        self.assertIn(("UA_WRITE_PENDING", "DONE"), transitions)
        self.assertEqual(len(bundle["bundleSha256"]), 64)

    def test_evidence_image_upload_search_and_binary_readback(self):
        event = make_event()
        delivery, _ = self.store.enqueue(
            event,
            url=self.mes_url + "/mes/ng-order",
            idempotency_key=str(uuid.uuid4()),
            correlation_id=event["correlationId"],
        )
        image = b"\x89PNG\r\n\x1a\n" + b"evidence-image-content"
        digest = hashlib.sha256(image).hexdigest()
        api = create_api_server("127.0.0.1", 0, self.store, "test-token", self.mes_url)
        api.quiet = True  # type: ignore[attr-defined]
        thread = threading.Thread(target=api.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{api.server_address[1]}"
        headers = {"authorization": "Bearer test-token", "content-type": "application/json"}
        try:
            upload = urllib.request.Request(
                base + "/v1/evidence/images",
                data=json.dumps({
                    "imageId": event["imageId"],
                    "imageBase64": base64.b64encode(image).decode("ascii"),
                    "sha256": digest,
                }).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(upload, timeout=5) as response:
                self.assertEqual(response.status, 201)
                uploaded = json.loads(response.read().decode("utf-8"))
            image_request = urllib.request.Request(
                base + "/v1/evidence/images/" + event["imageId"],
                headers={"authorization": "Bearer test-token"},
            )
            with urllib.request.urlopen(image_request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), "image/png")
                self.assertEqual(response.read(), image)
            search_url = base + "/v1/evidence/search?" + urllib.parse.urlencode({"q": event["sourceEventId"]})
            with urllib.request.urlopen(
                urllib.request.Request(search_url, headers={"authorization": "Bearer test-token"}), timeout=5
            ) as response:
                found = json.loads(response.read().decode("utf-8"))["items"]
            evidence_url = base + f"/v1/deliveries/{delivery['deliveryId']}/evidence"
            with urllib.request.urlopen(
                urllib.request.Request(evidence_url, headers={"authorization": "Bearer test-token"}), timeout=5
            ) as response:
                bundle = json.loads(response.read().decode("utf-8"))
        finally:
            api.shutdown()
            api.server_close()
            thread.join(timeout=5)

        self.assertEqual(uploaded["sha256"], digest)
        self.assertEqual(uploaded["bytes"], len(image))
        self.assertEqual(found[0]["deliveryId"], delivery["deliveryId"])
        self.assertEqual(bundle["inspection"]["imageId"], event["imageId"])
        self.assertTrue(bundle["imageEvidence"]["available"])
        self.assertTrue(bundle["completeness"]["imageBinaryStored"])
        self.assertFalse(bundle["completeness"]["imageReferenceOnly"])

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

    def test_api_rejects_contract_violations_with_stable_codes(self):
        token = "test-token"
        api = create_api_server("127.0.0.1", 0, self.store, token, self.mes_url)
        thread = threading.Thread(target=api.serve_forever, daemon=True)
        thread.start()
        try:
            ng = make_event()
            bad_schema = dict(ng, schemaVersion="0.9")
            unknown = dict(ng, unexpectedField=True)
            bad_corr = dict(make_event())
            completion = make_completion(inspect_count=5)
            bad_counts = dict(completion, okCount=4, ngCount=2)
            bad_rate = dict(make_completion(inspect_count=0), ngRate=0.0)
            cases = [
                (bad_schema, "UNSUPPORTED_SCHEMA_VERSION"),
                (unknown, "UNKNOWN_FIELD"),
                (bad_corr, "CORRELATION_MISMATCH"),
                (bad_counts, "INVALID_COUNT"),
                (bad_rate, "INVALID_NG_RATE"),
            ]
            for event, expected_code in cases:
                with self.subTest(code=expected_code):
                    status, payload = self.api_post(
                        api,
                        token,
                        event,
                        str(uuid.uuid4()),
                        "corr-other-client-0001" if expected_code == "CORRELATION_MISMATCH" else None,
                    )
                    self.assertEqual(status, 400)
                    self.assertEqual(payload["code"], expected_code)
        finally:
            api.shutdown()
            api.server_close()
            thread.join(timeout=5)

    def test_completion_receipt_is_validated_and_normalized(self):
        token = "test-token"
        api = create_api_server("127.0.0.1", 0, self.store, token, self.mes_url)
        thread = threading.Thread(target=api.serve_forever, daemon=True)
        thread.start()
        try:
            event = make_completion()
            key = str(uuid.uuid4())
            status, created = self.api_post(api, token, event, key)
            self.assertEqual(status, 202)
            self.worker().run_once()
            current = self.store.get(created["deliveryId"])
            self.assertEqual(current["state"], STATE_UA_WRITE_PENDING)
            self.assertEqual(current["mesReceipt"]["receiptType"], "COMPLETION_RECEIPT")
            self.assertTrue(current["mesReceipt"]["receiptNo"].startswith("RC-"))
            self.assertEqual(FakeMesHandler.requests[-1]["path"], "/mes/complete")
        finally:
            api.shutdown()
            api.server_close()
            thread.join(timeout=5)

    def test_ng_success_without_mes_order_no_is_a_contract_dead_letter(self):
        event = make_event()
        delivery, _ = self.store.enqueue(
            event,
            url=self.mes_url + "/mes/ng-order",
            idempotency_key=str(uuid.uuid4()),
            correlation_id=event["correlationId"],
        )
        FakeMesHandler.mode = "missing-ng-receipt"
        self.worker().run_once()
        dead = self.store.get(delivery["deliveryId"])
        self.assertEqual(dead["state"], STATE_DEAD)
        self.assertEqual(dead["lastErrorCode"], "MISSING_MES_ORDER_NO")

    def test_completion_success_without_receipt_no_is_a_contract_dead_letter(self):
        event = make_completion()
        delivery, _ = self.store.enqueue(
            event,
            url=self.mes_url + "/mes/complete",
            idempotency_key=str(uuid.uuid4()),
            correlation_id=event["correlationId"],
        )
        FakeMesHandler.mode = "missing-completion-receipt"
        self.worker().run_once()
        dead = self.store.get(delivery["deliveryId"])
        self.assertEqual(dead["state"], STATE_DEAD)
        self.assertEqual(dead["lastErrorCode"], "MISSING_RECEIPT_NO")

    def test_retry_after_is_clamped_to_contract_minimum(self):
        self.assertEqual(DeliveryWorker._retry_after_seconds({"Retry-After": "0"}), 0.1)
        self.assertEqual(DeliveryWorker._retry_after_seconds({"Retry-After": "999"}), 30.0)

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
