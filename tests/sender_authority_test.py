"""Cross-language tests for the single sender authority lease."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
