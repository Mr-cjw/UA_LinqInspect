"""Regression tests for Edge Core database + evidence-image backup and restore."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import edge_core_backup  # noqa: E402
from edge_core.store import DeliveryStore  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\nedge-backup-image"


def event() -> dict:
    correlation = f"corr-backup-{uuid.uuid4()}"
    return {
        "schemaVersion": "1.0",
        "sourceEventId": f"evt-backup-{uuid.uuid4()}",
        "stationId": "QCStation",
        "eventType": "NG_ORDER",
        "occurredAt": "2026-11-22T00:00:00.000Z",
        "result": "NG",
        "serialNumber": f"SN-BACKUP-{uuid.uuid4().hex[:10]}",
        "workOrderId": "WO-BACKUP-001",
        "defectType": "SCRATCH",
        "confidence": 0.93,
        "imageId": f"IMG-BACKUP-{uuid.uuid4().hex[:10]}",
        "correlationId": correlation,
        "metadata": {"modelVersion": "backup-test-v1"},
    }


class EdgeCoreBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "data" / "edge.sqlite3"
        self.images = self.root / "data" / "evidence-images"
        self.store = DeliveryStore(self.db, evidence_image_dir=self.images)
        self.event = event()
        self.delivery, _ = self.store.enqueue(
            self.event,
            url="http://mes.invalid/mes/ng-order",
            idempotency_key=str(uuid.uuid4()),
            correlation_id=self.event["correlationId"],
        )
        self.store.store_evidence_image(
            self.event["imageId"],
            PNG,
            expected_sha256=hashlib.sha256(PNG).hexdigest(),
        )

    def backup(self, *, keep: int = 7):
        return edge_core_backup.create_backup(
            source_db=self.db,
            image_dir=self.images,
            output_dir=self.root / "backups",
            keep=keep,
        )

    def test_backup_writes_database_manifest_and_verified_image(self):
        summary = self.backup()
        backup_dir = Path(summary["backupDir"])
        manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["backupType"], "EDGE_CORE_DATABASE_AND_EVIDENCE_IMAGES")
        self.assertEqual(manifest["images"]["count"], 1)
        self.assertEqual(manifest["database"]["counts"]["deliveries"], 1)
        image_file = backup_dir / "images" / manifest["images"]["items"][0]["relativePath"]
        self.assertTrue(image_file.is_file())
        self.assertEqual(hashlib.sha256(image_file.read_bytes()).hexdigest(), hashlib.sha256(PNG).hexdigest())

    def test_restore_recreates_database_and_image(self):
        summary = self.backup()
        target_db = self.root / "restored" / "edge.sqlite3"
        target_images = self.root / "restored" / "evidence-images"
        dry_run = edge_core_backup.restore_backup(
            backup_dir=Path(summary["backupDir"]),
            target_db=target_db,
            target_image_dir=target_images,
            dry_run=True,
        )
        self.assertTrue(dry_run["dryRun"])
        self.assertFalse(target_db.exists())
        restored = edge_core_backup.restore_backup(
            backup_dir=Path(summary["backupDir"]),
            target_db=target_db,
            target_image_dir=target_images,
        )
        self.assertTrue(restored["ok"])
        store = DeliveryStore(target_db, evidence_image_dir=target_images)
        bundle = store.evidence_bundle(self.delivery["deliveryId"])
        self.assertIsNotNone(bundle)
        self.assertTrue(bundle["completeness"]["imageBinaryStored"])
        metadata, content = store.read_evidence_image(self.event["imageId"])
        self.assertEqual(content, PNG)
        self.assertEqual(metadata["sha256"], hashlib.sha256(PNG).hexdigest())

    def test_tampered_backup_image_is_rejected(self):
        summary = self.backup()
        backup_dir = Path(summary["backupDir"])
        manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
        image_file = backup_dir / "images" / manifest["images"]["items"][0]["relativePath"]
        image_file.write_bytes(b"tampered")
        with self.assertRaises(RuntimeError):
            edge_core_backup.restore_backup(
                backup_dir=backup_dir,
                target_db=self.root / "restored" / "edge.sqlite3",
                target_image_dir=self.root / "restored" / "evidence-images",
            )

    def test_missing_source_image_fails_backup_without_final_directory(self):
        metadata, _ = self.store.read_evidence_image(self.event["imageId"])
        (self.images / metadata["relativePath"]).unlink()
        with self.assertRaises(FileNotFoundError):
            self.backup()
        self.assertEqual(list((self.root / "backups").glob("edge-core-*")), [])

    def test_force_restore_creates_pre_restore_snapshot(self):
        first = self.backup()
        target_db = self.root / "target" / "edge.sqlite3"
        target_images = self.root / "target" / "evidence-images"
        edge_core_backup.restore_backup(
            backup_dir=Path(first["backupDir"]),
            target_db=target_db,
            target_image_dir=target_images,
        )
        second_event = event()
        target_store = DeliveryStore(target_db, evidence_image_dir=target_images)
        target_store.enqueue(
            second_event,
            url="http://mes.invalid/mes/ng-order",
            idempotency_key=str(uuid.uuid4()),
            correlation_id=second_event["correlationId"],
        )
        with self.assertRaises(FileExistsError):
            edge_core_backup.restore_backup(
                backup_dir=Path(first["backupDir"]),
                target_db=target_db,
                target_image_dir=target_images,
            )
        restored = edge_core_backup.restore_backup(
            backup_dir=Path(first["backupDir"]),
            target_db=target_db,
            target_image_dir=target_images,
            force=True,
        )
        snapshot = Path(restored["snapshotDir"])
        self.assertTrue((snapshot / "edge-core.sqlite").is_file())
        self.assertTrue((snapshot / "images").is_dir())
        restored_store = DeliveryStore(target_db, evidence_image_dir=target_images)
        self.assertEqual(len(restored_store.list_deliveries(limit=10)), 1)

    def test_cleanup_removes_only_old_unreferenced_images(self):
        unreferenced_id = "IMG-UNREFERENCED-001"
        unreferenced = b"\x89PNG\r\n\x1a\nunreferenced"
        self.store.store_evidence_image(unreferenced_id, unreferenced)
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE evidence_images SET created_at = '2000-01-01T00:00:00.000+00:00'")
        dry_run = self.store.cleanup_unreferenced_evidence_images(retention_days=30, dry_run=True)
        self.assertEqual(dry_run["candidateCount"], 1)
        self.assertIsNotNone(self.store.evidence_image(unreferenced_id))
        applied = self.store.cleanup_unreferenced_evidence_images(retention_days=30, dry_run=False)
        self.assertEqual(applied["removedCount"], 1)
        self.assertIsNone(self.store.evidence_image(unreferenced_id))
        self.assertIsNotNone(self.store.evidence_image(self.event["imageId"]))
        self.assertEqual(self.store.read_evidence_image(self.event["imageId"])[1], PNG)

    def test_retention_keeps_requested_count(self):
        first = self.backup(keep=1)
        second = self.backup(keep=1)
        self.assertNotEqual(first["backupDir"], second["backupDir"])
        backups = [path for path in (self.root / "backups").glob("edge-core-*") if path.is_dir()]
        self.assertEqual(len(backups), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
