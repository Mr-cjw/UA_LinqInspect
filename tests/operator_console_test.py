"""Tests for the local operator console API and static shell."""

from __future__ import annotations

import base64
import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from operator_console.server import create_operator_server


class FakeStation:
    def __init__(self):
        self.start_calls = []
        self.complete_calls = []
        self.status = {
            "deviceStatus": 0,
            "deviceStatusText": "空闲",
            "currentWorkOrderId": "",
            "currentPartSN": "SN-OPERATOR-001",
            "lastMesOrderNo": "RC-001",
            "lastNgMesOrderNo": "MES-001",
            "lastCompletionReceiptNo": "RC-001",
            "InspectCount": 5,
            "OkCount": 4,
            "NgCount": 1,
            "LastResult": False,
            "LastDefectType": "SCRATCH",
            "LastConfidence": 0.93,
            "LastImageId": "IMG-001",
            "LastTimestamp": "2026-11-21T00:00:00Z",
            "NgRate": 0.2,
        }

    def read_status(self):
        return dict(self.status)

    def start_work_order(self, work_order_id, target_count):
        self.start_calls.append((work_order_id, target_count))
        return {"accepted": True, "message": "started"}

    def complete_work_order(self, work_order_id):
        self.complete_calls.append(work_order_id)
        return {"accepted": True, "message": "completed"}


class FakeEdge:
    def __init__(self):
        self.replayed = []
        self.acknowledged = []

    def status(self):
        return {
            "queue": {"total": 2, "pending": 1, "sending": 1, "retryWait": 0},
            "deadLetters": {"total": 1},
        }

    def deliveries(self, *, limit=30, state=""):
        return {
            "items": [{
                "deliveryId": "11111111-1111-4111-8111-111111111111",
                "eventType": "NG_ORDER",
                "workOrderId": "WO-OPERATOR-001",
                "state": "DONE",
                "mesReceipt": {"receiptNo": "MES-001"},
                "updatedAt": "2026-11-21T00:00:01Z",
            }],
            "nextCursor": None,
        }

    def dead_letters(self, *, limit=30):
        return {
            "items": [{
                "deliveryId": "22222222-2222-4222-8222-222222222222",
                "eventType": "NG_ORDER",
                "workOrderId": "WO-OPERATOR-001",
                "reasonCode": "MES_UNAVAILABLE",
                "reason": "test dead letter",
                "correlationId": "corr-operator-dead-0001",
                "failedAt": "2026-11-21T00:00:02Z",
            }],
            "nextCursor": None,
        }

    def replay_dead_letter(self, delivery_id, *, actor, reason):
        self.replayed.append((delivery_id, actor, reason))
        return {"deliveryId": delivery_id, "state": "PENDING"}

    def acknowledge_dead_letter(self, delivery_id, *, actor, resolution):
        self.acknowledged.append((delivery_id, actor, resolution))
        return {"deliveryId": delivery_id, "resolution": resolution}


class OperatorConsoleTests(unittest.TestCase):
    def setUp(self):
        self.station = FakeStation()
        self.edge = FakeEdge()
        self.user = "operator"
        self.password = "operator-secret"
        self.server = create_operator_server(
            "127.0.0.1",
            0,
            self.station,
            self.edge,
            username=self.user,
            password=self.password,
        )
        self.server.quiet = True  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode("ascii")
        self.auth = {"authorization": f"Basic {token}"}

    def request(self, method, path, *, body=None, auth=True, headers=None):
        request = urllib.request.Request(
            self.base + path,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={
                **({"authorization": self.auth["authorization"]} if auth else {}),
                **({"content-type": "application/json"} if body is not None else {}),
                **(headers or {}),
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, exc.headers, exc.read()
            finally:
                exc.close()

    def json_response(self, method, path, **kwargs):
        status, headers, body = self.request(method, path, **kwargs)
        return status, headers, json.loads(body.decode("utf-8"))

    def test_unauthenticated_request_is_rejected(self):
        status, headers, _ = self.request("GET", "/api/status", auth=False)
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers.get("WWW-Authenticate", ""))

    def test_authenticated_shell_and_status_are_available(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("质检工位操作台", body.decode("utf-8"))
        status, _, payload = self.json_response("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(payload["station"]["available"])
        self.assertEqual(payload["station"]["data"]["currentPartSN"], "SN-OPERATOR-001")
        self.assertEqual(payload["edge"]["data"]["queue"]["total"], 2)

    def test_post_requires_csrf_header(self):
        status, _, payload = self.json_response(
            "POST",
            "/api/work-orders/start",
            body={"workOrderId": "WO-OPERATOR-001", "targetCount": 5},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "CSRF_GUARD")
        self.assertEqual(self.station.start_calls, [])

    def test_start_and_complete_call_station_methods(self):
        status, _, payload = self.json_response(
            "POST",
            "/api/work-orders/start",
            body={"workOrderId": "WO-OPERATOR-001", "targetCount": 8},
            headers={"X-Operator-Request": "1"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.station.start_calls, [("WO-OPERATOR-001", 8)])

        status, _, payload = self.json_response(
            "POST",
            "/api/work-orders/complete",
            body={"workOrderId": "WO-OPERATOR-001"},
            headers={"X-Operator-Request": "1"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.station.complete_calls, ["WO-OPERATOR-001"])

    def test_dead_letter_replay_and_acknowledge_are_proxied(self):
        delivery_id = "22222222-2222-4222-8222-222222222222"
        status, _, _ = self.json_response(
            "POST",
            f"/api/dead-letters/{delivery_id}/replay",
            body={"actor": "operator", "reason": "MES corrected"},
            headers={"X-Operator-Request": "1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.edge.replayed, [(delivery_id, "operator", "MES corrected")])
        status, _, _ = self.json_response(
            "POST",
            f"/api/dead-letters/{delivery_id}/acknowledge",
            body={"actor": "operator", "resolution": "manually reconciled"},
            headers={"X-Operator-Request": "1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.edge.acknowledged, [(delivery_id, "operator", "manually reconciled")])

    def test_report_is_downloadable_json(self):
        status, headers, body = self.request("GET", "/api/report")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        report = json.loads(body.decode("utf-8"))
        self.assertEqual(report["schemaVersion"], "1.0")
        self.assertTrue(report["station"]["available"])
        self.assertEqual(report["deliveries"]["data"]["items"][0]["state"], "DONE")

    def test_invalid_start_payload_is_rejected(self):
        status, _, payload = self.json_response(
            "POST",
            "/api/work-orders/start",
            body={"workOrderId": "", "targetCount": 0},
            headers={"X-Operator-Request": "1"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "INVALID_REQUEST")
        self.assertEqual(self.station.start_calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
