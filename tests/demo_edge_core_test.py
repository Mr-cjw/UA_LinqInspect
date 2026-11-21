"""Regression tests for the hardware-free Edge Core demonstration."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_demo_module():
    path = REPO_ROOT / "scripts" / "demo_edge_core.py"
    spec = importlib.util.spec_from_file_location("demo_edge_core", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load demo module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DemoEdgeCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.demo = load_demo_module()
        cls.result = cls.demo.run_demo()

    def test_delivery_finishes_only_after_mes_receipt_and_ua_write(self):
        self.assertEqual(self.result.finalState, "DONE")
        self.assertTrue(self.result.uaWriteConfirmed)
        self.assertEqual(self.result.mesSuccessfulReceipts, 1)
        self.assertEqual(self.result.queueOpen, 0)
        self.assertEqual(self.result.deadLetterHistory, 1)
        self.assertEqual(self.result.attempts, 4)

    def test_restart_and_dead_letter_are_in_the_audit_timeline(self):
        transitions = [(item["from"], item["to"]) for item in self.result.timeline]
        self.assertIn(("PENDING", "SENDING"), transitions)
        self.assertIn(("SENDING", "RETRY_WAIT"), transitions)
        self.assertIn(("SENDING", "DEAD"), transitions)
        self.assertIn(("DEAD", "PENDING"), transitions)
        self.assertIn(("SENDING", "UA_WRITE_PENDING"), transitions)
        self.assertIn(("UA_WRITE_PENDING", "DONE"), transitions)

    def test_demo_exposes_nine_explainable_steps(self):
        self.assertEqual(len(self.result.steps), self.demo.DEMO_STEP_COUNT)
        self.assertEqual(self.result.steps[-1]["state"], "DONE")
        self.assertIn("correlationId", self.result.steps[0]["facts"])
        self.assertIn("businessKey", self.result.steps[6]["facts"])
        html = self.demo._DASHBOARD
        self.assertIn("Node-RED 原始工作流仍在仓库并通过回归", html)
        self.assertIn("质检交付主线", html)
        self.assertIn("设备上下文", html)
        self.assertIn("OEE / RCA", html)

        from project_overview import PROJECT_OVERVIEW_DASHBOARD

        self.assertIn("项目总览", PROJECT_OVERVIEW_DASHBOARD)
        self.assertIn("不丢、不重、可解释", PROJECT_OVERVIEW_DASHBOARD)
        self.assertIn("今天真实跑通", PROJECT_OVERVIEW_DASHBOARD)
        self.assertIn("还不能对外宣称", PROJECT_OVERVIEW_DASHBOARD)
        self.assertIn("付费试点", PROJECT_OVERVIEW_DASHBOARD)
        self.assertIn("不是生产集成", PROJECT_OVERVIEW_DASHBOARD)
        self.assertIn('href="/overview"', html)
        self.assertEqual(PROJECT_OVERVIEW_DASHBOARD.count('id="run"'), 1)
        self.assertIn('id="run-section"', PROJECT_OVERVIEW_DASHBOARD)
        self.assertNotIn("陈" + "总", PROJECT_OVERVIEW_DASHBOARD)
        self.assertNotIn("投资" + "评审", PROJECT_OVERVIEW_DASHBOARD)
        self.assertNotIn("/api/" + "invest" + "or", PROJECT_OVERVIEW_DASHBOARD)

    def test_same_business_key_and_receipt_are_preserved(self):
        self.assertEqual(self.result.businessKey, "NG_ORDER:JX-LINQIN-STATION-01:QC-JX-DEMO-0001")
        self.assertTrue(self.result.mesReceiptNo.startswith("MES-JX-DEMO-"))
        self.assertTrue(self.result.correlationId.startswith("corr-jx-demo-"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
