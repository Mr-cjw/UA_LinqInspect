"""Integration test for the two-repository QA + OEE/RCA demonstration.

This test intentionally runs outside the default regression because it starts
the OPC UA server from the sibling ``rpi4-opcua-oee-rca-gateway`` repository.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RPI_ROOT = REPO_ROOT.parent / "rpi4-opcua-oee-rca-gateway"


def load_joint_module():
    path = REPO_ROOT / "scripts" / "demo_joint_oee_rca.py"
    spec = importlib.util.spec_from_file_location("demo_joint_oee_rca", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load joint demo module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(
    (RPI_ROOT / "src" / "index.js").is_file()
    and (RPI_ROOT / "node_modules" / "node-opcua").is_dir(),
    "sibling OEE/RCA gateway repository is unavailable",
)
class JointOeeRcaDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.demo = load_joint_module()
        cls.result = cls.demo.run_joint_demo()

    def test_quality_delivery_still_finishes_after_device_context(self):
        self.assertEqual(self.result.finalState, "DONE")
        self.assertTrue(self.result.mesReceiptNo.startswith("MES-JX-DEMO-"))
        self.assertTrue(self.result.correlationId.startswith("corr-jx-joint-"))

    def test_oee_components_are_bounded_and_explainable(self):
        for value in (
            self.result.oee,
            self.result.availability,
            self.result.performance,
            self.result.quality,
        ):
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)
        self.assertGreater(self.result.observedSeconds, 0)
        self.assertGreaterEqual(self.result.downtimeSeconds, 0)
        self.assertGreater(self.result.ngCount, 0)

    def test_temperature_context_becomes_an_rca_candidate_not_a_causal_claim(self):
        self.assertIn("TEMPERATURE_SPIKE", self.result.alarmCodes)
        self.assertEqual(self.result.rcaCandidate, "TEMPERATURE_SPIKE")
        self.assertIn("非因果证明", self.result.rcaConfidence)
        self.assertGreaterEqual(len(self.result.rcaEvidence), 3)
        self.assertGreater(self.result.temperaturePeakC, 70.0)

    def test_both_project_outputs_are_visible(self):
        self.assertEqual(len(self.result.steps), 8)
        self.assertTrue(self.result.samples)
        self.assertTrue(self.result.timeline)
        self.assertIn("rpi4-opcua-oee-rca-gateway", self.result.rpiRepo)


if __name__ == "__main__":
    unittest.main(verbosity=2)
