"""Control-plane cutover and rollback drill for single-sender ownership.

This is not a factory network test. It exercises the real Edge Core process,
offline SQLite migration, sender lease handoff, MES delivery, UA confirmation,
and rollback readiness against local temporary databases and a fake MES.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from edge_core.authority_lock import SenderAuthorityGuard  # noqa: E402
from edge_core.migration import migrate  # noqa: E402
from edge_core.store import DeliveryStore  # noqa: E402


class FakeMesHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []
    receipts: dict[str, dict] = {}
    lock = threading.Lock()

    def log_message(self, _fmt, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        body = json.loads(raw)
        business_key = str(body.get("serialNumber") or body.get("workOrderId") or "")
        key = f"{self.path}:{business_key}"
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with type(self).lock:
            type(self).calls.append({"path": self.path, "body": body})
            existing = type(self).receipts.get(key)
            if existing and existing["digest"] != digest:
                self._send(409, {
                    "accepted": False,
                    "errorCode": "IDEMPOTENCY_CONFLICT",
                    "error": "payload changed for the same business key",
                })
                return
            if existing is None:
                sequence = len(type(self).receipts) + 1
                if self.path == "/mes/ng-order":
                    receipt = {
                        "accepted": True,
                        "schemaVersion": "1.0",
                        "correlationId": body["correlationId"],
                        "mesOrderNo": f"MES-CUTOVER-{sequence:04d}",
                    }
                else:
                    receipt = {
                        "accepted": True,
                        "schemaVersion": "1.0",
                        "correlationId": body["correlationId"],
                        "receiptNo": f"RC-CUTOVER-{sequence:04d}",
                    }
                existing = {"digest": digest, "receipt": receipt}
                type(self).receipts[key] = existing
            response = dict(existing["receipt"])
            response["correlationId"] = body["correlationId"]
            if existing["receipt"] != response:
                response["duplicated"] = True
        self._send(200, response)

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def create_nodered_source(path: Path, mes_base_url: str) -> None:
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
        ng_body = {
            "schemaVersion": "1.0",
            "sourceEventId": "evt-cutover-ng-001",
            "stationId": "QCStation",
            "correlationId": "corr-cutover-ng-001",
            "workOrderId": "WO-CUTOVER-001",
            "serialNumber": "SN-CUTOVER-001",
            "defectType": "SCRATCH",
            "confidence": 0.96,
            "imageId": "IMG-CUTOVER-001",
            "occurredAt": "2026-11-21T00:00:00.000Z",
        }
        done_body = {
            "schemaVersion": "1.0",
            "sourceEventId": "evt-cutover-done-001",
            "stationId": "QCStation",
            "correlationId": "corr-cutover-done-001",
            "workOrderId": "WO-CUTOVER-001",
            "inspectCount": 5,
            "okCount": 4,
            "ngCount": 1,
            "ngRate": 0.2,
        }
        for queue_id, queue_type, body, url in (
            ("ng|SN-CUTOVER-001|2026-11-21T00:00:00.000Z", "ng", ng_body, mes_base_url + "/mes/ng-order"),
            ("done|WO-CUTOVER-001", "complete", done_body, mes_base_url + "/mes/complete"),
        ):
            conn.execute(
                """
                INSERT INTO queue(
                    id, type, correlation_id, url, body_json, created_at, attempts,
                    next_at, status, sent_at, last_status, last_error_code, enqueued_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 'PENDING', NULL, '', '', ?)
                """,
                (
                    queue_id,
                    queue_type,
                    body["correlationId"],
                    url,
                    json.dumps(body),
                    body.get("occurredAt", "2026-11-21T00:00:01.000Z"),
                    1763683200000,
                ),
            )


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class SenderCutoverTests(unittest.TestCase):
    def test_offline_cutover_keeps_one_sender_and_preserves_rollback_source(self):
        FakeMesHandler.calls = []
        FakeMesHandler.receipts = {}
        mes = ThreadingHTTPServer(("127.0.0.1", 0), FakeMesHandler)
        mes_thread = threading.Thread(target=mes.serve_forever, daemon=True)
        mes_thread.start()
        mes_url = f"http://127.0.0.1:{mes.server_address[1]}"
        self.addCleanup(mes.server_close)
        self.addCleanup(mes.shutdown)

        with tempfile.TemporaryDirectory(prefix="qc-cutover-") as temporary:
            root = Path(temporary)
            source = root / "nodered.sqlite"
            target = root / "edge-core.sqlite"
            lock_dir = root / "sender-authority"
            create_nodered_source(source, mes_url)

            node_guard = SenderAuthorityGuard(owner="node-red", lock_dir=lock_dir)
            self.assertTrue(node_guard.try_acquire()["ok"])

            port = free_port()
            env = os.environ.copy()
            env.update({
                "PYTHONPATH": str(REPO_ROOT),
                "QC_DELIVERY_AUTHORITY": "edge-core",
                "QC_SENDER_LOCK_DIR": str(lock_dir),
                "EDGE_DB_PATH": str(target),
                "EDGE_API_HOST": "127.0.0.1",
                "EDGE_API_PORT": str(port),
                "EDGE_API_TOKEN": "cutover-test-token",
                "EDGE_API_BASE_URL": f"http://127.0.0.1:{port}",
                "MES_BASE_URL": mes_url,
                "EDGE_WORKER_INTERVAL_SECONDS": "0.05",
                "EDGE_MES_TIMEOUT_SECONDS": "2",
                "EDGE_MAX_ATTEMPTS": "2",
                "EDGE_LEASE_SECONDS": "10",
            })
            blocked = subprocess.run(
                [sys.executable, "-m", "edge_core.main"],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=15,
                check=False,
            )
            self.assertEqual(blocked.returncode, 3, blocked.stdout)
            self.assertIn("sender authority is held by node-red", blocked.stdout)

            dry_run = migrate(source, target, apply=False)
            self.assertTrue(dry_run["ok"])
            self.assertEqual(dry_run["importable"], 2)
            applied = migrate(source, target, apply=True)
            self.assertTrue(applied["ok"])
            self.assertEqual(applied["imported"], 2)

            node_guard.release()
            edge_process = subprocess.Popen(
                [sys.executable, "-m", "edge_core.main"],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.addCleanup(lambda: edge_process.poll() is None and edge_process.kill())

            health = None
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if edge_process.poll() is not None:
                    self.fail(f"Edge Core exited early: {edge_process.communicate()[0]}")
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/v1/health", timeout=1
                    ) as response:
                        health = json.loads(response.read())
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.05)
            self.assertIsNotNone(health)
            owner_meta = json.loads((lock_dir / "owner.json").read_text(encoding="utf-8"))
            self.assertEqual(owner_meta["owner"], "edge-core")
            self.assertEqual(owner_meta["pid"], edge_process.pid)

            store = DeliveryStore(target)
            deadline = time.monotonic() + 12
            deliveries = []
            while time.monotonic() < deadline:
                deliveries = store.list_deliveries(limit=10)
                if len(deliveries) == 2 and all(
                    item["state"] == "UA_WRITE_PENDING" for item in deliveries
                ):
                    break
                time.sleep(0.05)
            self.assertEqual(len(deliveries), 2)
            self.assertTrue(all(item["state"] == "UA_WRITE_PENDING" for item in deliveries))
            self.assertEqual(len(FakeMesHandler.calls), 2)
            self.assertTrue(all(item["mesReceipt"] for item in deliveries))

            for item in deliveries:
                body = json.dumps({
                    "success": True,
                    "correlationId": item["correlationId"],
                }).encode("utf-8")
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/deliveries/{item['deliveryId']}/ua-write-result",
                    data=body,
                    headers={
                        "content-type": "application/json",
                        "authorization": "Bearer cutover-test-token",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                deliveries = store.list_deliveries(limit=10)
                if len(deliveries) == 2 and all(item["state"] == "DONE" for item in deliveries):
                    break
                time.sleep(0.05)
            self.assertTrue(all(item["state"] == "DONE" for item in deliveries))
            self.assertTrue(all(item["uaWriteConfirmed"] for item in deliveries))

            edge_process.terminate()
            output, _ = edge_process.communicate(timeout=10)
            self.assertEqual(edge_process.returncode, 0, output)
            self.assertFalse(lock_dir.exists())

            self.assertTrue(node_guard.try_acquire()["ok"])
            node_guard.release()
            with sqlite3.connect(source) as conn:
                source_rows = conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
                source_dead = conn.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0]
            self.assertEqual(source_rows, 2)
            self.assertEqual(source_dead, 0)

            replay = migrate(source, target, apply=True)
            self.assertTrue(replay["ok"])
            self.assertEqual(replay["imported"], 0)
            self.assertEqual(replay["duplicates"], 2)
            self.assertEqual(len(DeliveryStore(target).list_deliveries(limit=10)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
