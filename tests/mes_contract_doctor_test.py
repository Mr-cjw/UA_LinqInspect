"""Tests for the MES contract doctor CLI."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "mes_contract_doctor.py"


class FakeMesHandler(BaseHTTPRequestHandler):
    mode = "strict"
    calls: list[dict] = []
    receipts: dict[str, dict] = {}
    sequence = 0

    def log_message(self, _fmt, *_args):
        return

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")
        body = json.loads(raw)
        type(self).calls.append({
            "path": self.path,
            "body": body,
            "headers": dict(self.headers),
        })
        if self.path == "/mes/ng-order" and not body.get("serialNumber"):
            self._send(400, {
                "accepted": False,
                "schemaVersion": "1.0",
                "correlationId": body.get("correlationId", ""),
                "errorCode": "INVALID_SERIAL_NUMBER",
                "error": "serialNumber is required",
            })
            return
        if type(self).mode == "invalid_success" and body.get("accepted") is not True:
            self._send(200, {
                "accepted": True,
                "schemaVersion": "0.9",
                "correlationId": "wrong-correlation",
                "mesOrderNo": "MES-INVALID",
            })
            return

        business_key = (
            ("ng", body.get("serialNumber"))
            if self.path == "/mes/ng-order"
            else ("complete", body.get("workOrderId"))
        )
        digest = json.dumps(body, sort_keys=True, separators=(",", ":"))
        existing = type(self).receipts.get(business_key)
        if existing and type(self).mode == "strict":
            if existing["digest"] != digest:
                self._send(409, {
                    "accepted": False,
                    "schemaVersion": "1.0",
                    "correlationId": body["correlationId"],
                    "errorCode": "IDEMPOTENCY_CONFLICT",
                    "error": "business key already has a different payload",
                })
                return
            response = dict(existing["response"])
            response["correlationId"] = body["correlationId"]
            response["duplicated"] = True
            self._send(200, response)
            return

        type(self).sequence += 1
        if self.path == "/mes/ng-order":
            receipt_no = f"MES-DOCTOR-{type(self).sequence:04d}"
            response = {
                "accepted": True,
                "schemaVersion": "1.0",
                "correlationId": body["correlationId"],
                "mesOrderNo": receipt_no,
            }
        else:
            receipt_no = f"RC-DOCTOR-{type(self).sequence:04d}"
            response = {
                "accepted": True,
                "schemaVersion": "1.0",
                "correlationId": body["correlationId"],
                "receiptNo": receipt_no,
            }
        if type(self).mode == "strict" or existing is None:
            type(self).receipts[business_key] = {"digest": digest, "response": response}
        self._send(200, response)

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class MesContractDoctorTests(unittest.TestCase):
    def setUp(self):
        FakeMesHandler.mode = "strict"
        FakeMesHandler.calls = []
        FakeMesHandler.receipts = {}
        FakeMesHandler.sequence = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMesHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_doctor(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--base-url",
                self.base_url,
                "--work-order",
                "WO-MES-DOCTOR-TEST",
                "--serial",
                "SN-MES-DOCTOR-TEST",
                "--timeout",
                "2",
                *extra,
            ],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
        )

    def report(self) -> dict:
        return json.loads((self.root / "report.json").read_text(encoding="utf-8"))

    def test_dry_run_does_not_contact_mes(self):
        completed = self.run_doctor("--report-dir", str(self.root))
        self.assertEqual(completed.returncode, 0, completed.stdout)
        report = self.report()
        self.assertEqual(report["overallStatus"], "DRY_RUN")
        self.assertEqual(FakeMesHandler.calls, [])
        self.assertTrue((self.root / "report.md").is_file())

    def test_strict_contract_passes_and_writes_report(self):
        completed = self.run_doctor(
            "--execute",
            "--confirm-write",
            "WO-MES-DOCTOR-TEST",
            "--report-dir",
            str(self.root),
        )
        self.assertEqual(completed.returncode, 0, completed.stdout)
        report = self.report()
        self.assertEqual(report["overallStatus"], "PASS")
        self.assertEqual(report["counts"]["PASS"], 7)
        self.assertEqual(len(FakeMesHandler.calls), 7)
        self.assertTrue((self.root / "report.json").is_file())
        self.assertTrue((self.root / "report.md").is_file())

    def test_non_idempotent_mes_returns_warning(self):
        FakeMesHandler.mode = "non_idempotent"
        completed = self.run_doctor(
            "--execute",
            "--confirm-write",
            "WO-MES-DOCTOR-TEST",
            "--report-dir",
            str(self.root),
        )
        self.assertEqual(completed.returncode, 2, completed.stdout)
        report = self.report()
        self.assertEqual(report["overallStatus"], "WARN")
        self.assertGreaterEqual(report["counts"]["WARN"], 2)

    def test_invalid_2xx_is_a_failure(self):
        FakeMesHandler.mode = "invalid_success"
        completed = self.run_doctor(
            "--execute",
            "--confirm-write",
            "WO-MES-DOCTOR-TEST",
            "--report-dir",
            str(self.root),
        )
        self.assertEqual(completed.returncode, 1, completed.stdout)
        report = self.report()
        self.assertEqual(report["overallStatus"], "FAIL")
        self.assertGreaterEqual(report["counts"]["FAIL"], 1)

    def test_execute_requires_exact_confirmation(self):
        completed = self.run_doctor(
            "--execute",
            "--confirm-write",
            "WRONG-WORK-ORDER",
            "--report-dir",
            str(self.root),
        )
        self.assertEqual(completed.returncode, 2, completed.stdout)
        self.assertIn("--confirm-write must exactly match", completed.stdout)
        self.assertEqual(FakeMesHandler.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
