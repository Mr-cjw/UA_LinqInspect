"""Tests for HMAC evidence signing, key rotation and signed backups."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
import sys
for path in (REPO_ROOT, SCRIPTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import edge_core_backup  # noqa: E402
from edge_core.evidence_signing import (  # noqa: E402
    load_keyring_from_env,
    sign_document,
    verify_document,
)
from edge_core.store import DeliveryStore  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\nsigned-evidence"


def event() -> dict:
    correlation = f"corr-sign-{uuid.uuid4()}"
    return {
        "schemaVersion": "1.0",
        "sourceEventId": f"evt-sign-{uuid.uuid4()}",
        "stationId": "QCStation",
        "eventType": "NG_ORDER",
        "occurredAt": "2026-11-22T00:00:00.000Z",
        "result": "NG",
        "serialNumber": f"SN-SIGN-{uuid.uuid4().hex[:10]}",
        "workOrderId": "WO-SIGN-001",
        "defectType": "SCRATCH",
        "confidence": 0.91,
        "imageId": f"IMG-SIGN-{uuid.uuid4().hex[:10]}",
        "correlationId": correlation,
        "metadata": {
            "modelVersion": "sign-test-v1",
            "modelSha256": "a" * 64,
            "datasetVersion": "sign-test-data",
            "imageSha256": "b" * 64,
        },
    }


class EvidenceSigningTests(unittest.TestCase):
    def test_sign_and_verify_round_trip(self):
        signed = sign_document({"schemaVersion": "1.0", "value": 42}, key="secret-1", key_id="k1")
        self.assertEqual(signed["signature"]["algorithm"], "HMAC-SHA256")
        verification = verify_document(signed, {"k1": "secret-1"})
        self.assertTrue(verification["ok"], verification)

    def test_tampering_is_detected(self):
        signed = sign_document({"schemaVersion": "1.0", "value": 42}, key="secret-1", key_id="k1")
        tampered = copy.deepcopy(signed)
        tampered["value"] = 43
        verification = verify_document(tampered, {"k1": "secret-1"})
        self.assertFalse(verification["ok"])
        self.assertEqual(verification["error"], "SIGNATURE_MISMATCH")

    def test_rotated_keyring_verifies_old_signature(self):
        signed = sign_document({"value": 42}, key="old-secret", key_id="old")
        keyring = load_keyring_from_env({
            "EDGE_EVIDENCE_SIGNING_KEY": "new-secret",
            "EDGE_EVIDENCE_SIGNING_KEY_ID": "new",
            "EDGE_EVIDENCE_SIGNING_PREVIOUS_KEYS": json.dumps({"old": "old-secret"}),
        })
        self.assertEqual(set(keyring), {"new", "old"})
        self.assertTrue(verify_document(signed, keyring)["ok"])

    def test_unknown_key_is_rejected(self):
        signed = sign_document({"value": 42}, key="secret-1", key_id="missing")
        verification = verify_document(signed, {"other": "secret"})
        self.assertFalse(verification["ok"])
        self.assertEqual(verification["error"], "UNKNOWN_KEY_ID")

    def test_evidence_bundle_is_hmac_signed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = DeliveryStore(root / "edge.sqlite3", signing_key="evidence-secret", signing_key_id="evidence-k1")
            item = event()
            delivery, _ = store.enqueue(
                item,
                url="http://mes.invalid/mes/ng-order",
                idempotency_key=str(uuid.uuid4()),
                correlation_id=item["correlationId"],
            )
            bundle = store.evidence_bundle(delivery["deliveryId"])
            self.assertEqual(bundle["signatureStatus"], "SIGNED")
            self.assertEqual(bundle["signature"]["keyId"], "evidence-k1")
            self.assertTrue(verify_document(bundle, {"evidence-k1": "evidence-secret"})["ok"])

    def test_signed_backup_manifest_and_restore_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = DeliveryStore(root / "data" / "edge.sqlite3", signing_key="backup-secret", signing_key_id="backup-k1")
            item = event()
            store.enqueue(
                item,
                url="http://mes.invalid/mes/ng-order",
                idempotency_key=str(uuid.uuid4()),
                correlation_id=item["correlationId"],
            )
            store.store_evidence_image(item["imageId"], PNG)
            summary = edge_core_backup.create_backup(
                source_db=root / "data" / "edge.sqlite3",
                image_dir=root / "data" / "evidence-images",
                output_dir=root / "backups",
                signing_key="backup-secret",
                signing_key_id="backup-k1",
            )
            manifest_path = Path(summary["backupDir"]) / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["signatureStatus"], "SIGNED")
            edge_core_backup.validate_backup(Path(summary["backupDir"]), keyring={"backup-k1": "backup-secret"})

            tampered = copy.deepcopy(manifest)
            tampered["images"]["count"] += 1
            manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                edge_core_backup.validate_backup(Path(summary["backupDir"]), keyring={"backup-k1": "backup-secret"})

    def test_unsigned_backup_is_rejected_when_keys_are_configured(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = DeliveryStore(root / "data" / "edge.sqlite3")
            item = event()
            store.enqueue(
                item,
                url="http://mes.invalid/mes/ng-order",
                idempotency_key=str(uuid.uuid4()),
                correlation_id=item["correlationId"],
            )
            summary = edge_core_backup.create_backup(
                source_db=root / "data" / "edge.sqlite3",
                image_dir=root / "data" / "evidence-images",
                output_dir=root / "backups",
            )
            with self.assertRaisesRegex(RuntimeError, "unsigned backup rejected"):
                edge_core_backup.validate_backup(Path(summary["backupDir"]), keyring={"k1": "secret"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
