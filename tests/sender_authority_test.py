"""Cross-language tests for the single sender authority lease."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from edge_core.authority_lock import SenderAuthorityGuard
from edge_core.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


class SenderAuthorityTests(unittest.TestCase):
    def test_python_single_owner_and_release(self):
        default_config = load_config({"EDGE_API_TOKEN": "test-token"})
        edge_config = load_config({
            "EDGE_API_TOKEN": "test-token",
            "QC_DELIVERY_AUTHORITY": "edge-core",
        })
        self.assertEqual(default_config.delivery_authority, "node-red")
        self.assertEqual(edge_config.delivery_authority, "edge-core")
        with tempfile.TemporaryDirectory() as temp:
            first = SenderAuthorityGuard(owner="node-red", lock_dir=temp)
            second = SenderAuthorityGuard(owner="edge-core", lock_dir=temp)
            self.assertTrue(first.try_acquire()["ok"])
            blocked = second.try_acquire()
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["owner"], "node-red")
            first.release()
            self.assertTrue(second.try_acquire()["ok"])
            second.release()

    def test_node_and_python_share_the_same_lease(self):
        with tempfile.TemporaryDirectory() as temp:
            env = os.environ.copy()
            env["QC_SENDER_LOCK_DIR"] = temp
            script = r"""
const { SenderAuthorityGuard } = require('./nodered/authority_lock.js');
const guard = new SenderAuthorityGuard({ owner: 'node-red' });
const result = guard.tryAcquire();
process.stdout.write(JSON.stringify(result));
guard.release();
"""
            python_guard = SenderAuthorityGuard(owner="edge-core", lock_dir=temp)
            self.assertTrue(python_guard.try_acquire()["ok"])
            blocked = subprocess.run(
                ["node", "-e", script],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=True,
            )
            self.assertFalse(json.loads(blocked.stdout)["ok"])
            python_guard.release()

            acquired = subprocess.run(
                ["node", "-e", script],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=True,
            )
            self.assertTrue(json.loads(acquired.stdout)["ok"])
            # The Node process exits and its lease becomes stale; Python can recover it.
            recovered = python_guard.try_acquire()
            self.assertTrue(recovered["ok"])
            python_guard.release()

    def test_concurrent_python_and_node_have_exactly_one_winner(self):
        node_script = r"""
const fs = require('node:fs');
const { SenderAuthorityGuard } = require('./nodered/authority_lock.js');
const gate = process.env.TEST_GATE;
while (!fs.existsSync(gate)) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 5);
const guard = new SenderAuthorityGuard({ owner: process.env.TEST_OWNER });
const result = guard.tryAcquire();
process.stdout.write(JSON.stringify(result));
if (result.ok) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 400);
guard.release();
"""
        python_script = r"""
import json, os, time
from edge_core.authority_lock import SenderAuthorityGuard
while not os.path.exists(os.environ['TEST_GATE']):
    time.sleep(0.005)
guard = SenderAuthorityGuard(owner=os.environ['TEST_OWNER'])
result = guard.try_acquire()
print(json.dumps(result), flush=True)
if result.get('ok'):
    time.sleep(0.4)
guard.release()
"""
        for round_index in range(4):
            with tempfile.TemporaryDirectory() as temp:
                gate = Path(temp) / "go"
                env = os.environ.copy()
                env.update({
                    "PYTHONPATH": str(REPO_ROOT),
                    "QC_SENDER_LOCK_DIR": str(Path(temp) / "authority"),
                    "TEST_GATE": str(gate),
                })
                processes = []
                for index in range(4):
                    node_env = dict(env, TEST_OWNER=f"node-red-{index}")
                    processes.append(subprocess.Popen(
                        ["node", "-e", node_script], cwd=REPO_ROOT, env=node_env,
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    ))
                    python_env = dict(env, TEST_OWNER=f"edge-core-{index}")
                    processes.append(subprocess.Popen(
                        [sys.executable, "-c", python_script], cwd=REPO_ROOT, env=python_env,
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    ))
                time.sleep(0.1)
                gate.write_text("go", encoding="utf-8")
                results = []
                for process in processes:
                    stdout, stderr = process.communicate(timeout=20)
                    self.assertEqual(process.returncode, 0, stderr)
                    results.append(json.loads(stdout))
                winners = [item for item in results if item.get("ok")]
                self.assertEqual(len(winners), 1, f"round {round_index}: {results}")
                self.assertFalse((Path(temp) / "authority").exists())
                self.assertEqual(list(Path(temp).glob(".authority.claim-*")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
