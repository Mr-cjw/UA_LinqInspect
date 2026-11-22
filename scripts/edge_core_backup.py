#!/usr/bin/env python3
"""Create and restore consistent Edge Core database + evidence-image backups."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from backup_queue import copy_database, ensure_writable_directory, readonly_connection  # noqa: E402
from edge_core.evidence_signing import (  # noqa: E402
    load_keyring_from_env,
    sign_document,
    verify_document,
)

MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "edge-core.sqlite"
IMAGES_NAME = "images"
REQUIRED_TABLES = ("deliveries", "delivery_events", "dead_letters", "evidence_images")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def database_info(path: Path) -> dict[str, Any]:
    connection = readonly_connection(path)
    connection.row_factory = sqlite3.Row
    try:
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()]
        if integrity != ["ok"]:
            raise RuntimeError(f"database integrity_check failed: {integrity}")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise RuntimeError(f"database foreign_key_check failed: {foreign_keys}")
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        missing = [name for name in REQUIRED_TABLES if name not in tables]
        if missing:
            raise RuntimeError(f"database missing required tables: {missing}")
        counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in REQUIRED_TABLES
        }
        image_rows = [
            dict(row) for row in connection.execute(
                "SELECT image_id, sha256, content_type, bytes, relative_path, created_at "
                "FROM evidence_images ORDER BY image_id"
            ).fetchall()
        ]
        return {
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
            "integrityCheck": "ok",
            "foreignKeyCheck": "ok",
            "counts": counts,
            "images": image_rows,
        }
    finally:
        connection.close()


def safe_image_path(root: Path, relative: str) -> Path:
    root = root.resolve()
    path = (root / relative).resolve()
    if root not in path.parents:
        raise RuntimeError(f"image path escapes image root: {relative}")
    return path


def copy_image_tree(source_root: Path, destination_root: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    copied = []
    for row in rows:
        relative = str(row["relative_path"])
        source = safe_image_path(source_root, relative)
        if not source.is_file():
            raise FileNotFoundError(f"referenced evidence image is missing: {relative}")
        actual = sha256_file(source)
        if actual != row["sha256"]:
            raise RuntimeError(f"evidence image hash mismatch: {relative}")
        destination = safe_image_path(destination_root, relative)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(source, destination)
        os.chmod(destination, 0o600)
        if sha256_file(destination) != actual:
            raise RuntimeError(f"backup image hash mismatch after copy: {relative}")
        copied.append({
            "imageId": row["image_id"],
            "sha256": actual,
            "contentType": row["content_type"],
            "bytes": int(row["bytes"]),
            "relativePath": relative,
            "createdAt": row["created_at"],
        })
    return copied


def create_backup(
    *,
    source_db: Path,
    image_dir: Path,
    output_dir: Path,
    keep: int = 7,
    signing_key: str = "",
    signing_key_id: str = "primary",
) -> dict[str, Any]:
    source_db = source_db.expanduser().resolve()
    image_dir = image_dir.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not source_db.is_file():
        raise FileNotFoundError(f"source database does not exist: {source_db}")
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    ensure_writable_directory(output_dir, "edge backup output")
    stamp = utc_stamp()
    target = output_dir / f"edge-core-{stamp}"
    index = 1
    while target.exists():
        target = output_dir / f"edge-core-{stamp}-{index:02d}"
        index += 1
    staging = output_dir / f".{target.name}.{os.getpid()}.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(mode=0o700)
    try:
        database_target = staging / DATABASE_NAME
        copy_database(source_db, database_target)
        info = database_info(database_target)
        image_target = staging / IMAGES_NAME
        image_target.mkdir(mode=0o700)
        images = copy_image_tree(image_dir, image_target, info["images"])
        manifest = {
            "schemaVersion": "1.0",
            "signatureStatus": "SIGNED" if signing_key else "UNSIGNED",
            "backupType": "EDGE_CORE_DATABASE_AND_EVIDENCE_IMAGES",
            "createdAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "database": {
                "file": DATABASE_NAME,
                "sha256": info["sha256"],
                "bytes": info["bytes"],
                "integrityCheck": info["integrityCheck"],
                "foreignKeyCheck": info["foreignKeyCheck"],
                "counts": info["counts"],
            },
            "images": {
                "directory": IMAGES_NAME,
                "count": len(images),
                "totalBytes": sum(item["bytes"] for item in images),
                "items": images,
            },
        }
        if signing_key:
            manifest = sign_document(manifest, key=signing_key, key_id=signing_key_id)
        else:
            manifest["signature"] = None
        manifest_temp = staging / f".{MANIFEST_NAME}.{os.getpid()}.tmp"
        manifest_temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(manifest_temp, 0o600)
        os.replace(manifest_temp, staging / MANIFEST_NAME)
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    candidates = sorted(
        [path for path in output_dir.glob("edge-core-*") if path.is_dir() and path != target],
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    removed = []
    retained = [target] + candidates[: max(0, keep - 1)]
    for old in candidates[max(0, keep - 1):]:
        shutil.rmtree(old)
        removed.append(str(old))
    return {
        "ok": True,
        "backupDir": str(target),
        "manifest": str(target / MANIFEST_NAME),
        "databaseSha256": manifest["database"]["sha256"],
        "databaseCounts": manifest["database"]["counts"],
        "imageCount": manifest["images"]["count"],
        "imageBytes": manifest["images"]["totalBytes"],
        "signatureStatus": manifest["signatureStatus"],
        "signingKeyId": (manifest.get("signature") or {}).get("keyId", ""),
        "retained": [str(path) for path in retained],
        "removed": removed,
    }


def validate_backup(
    backup_dir: Path,
    *,
    keyring: dict[str, str] | None = None,
) -> tuple[dict[str, Any], Path, Path]:
    backup_dir = backup_dir.expanduser().resolve()
    manifest_path = backup_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise FileNotFoundError(f"backup manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schemaVersion") != "1.0" or manifest.get("backupType") != "EDGE_CORE_DATABASE_AND_EVIDENCE_IMAGES":
        raise RuntimeError("unsupported Edge Core backup manifest")
    keys = keyring or {}
    if manifest.get("signatureStatus") == "SIGNED":
        verification = verify_document(manifest, keys)
        if not verification["ok"]:
            raise RuntimeError(f"backup manifest signature verification failed: {verification['error']}")
    elif keys:
        raise RuntimeError("unsigned backup rejected because signing keys are configured")
    database = backup_dir / str(manifest["database"]["file"])
    if not database.is_file():
        raise FileNotFoundError(f"backup database missing: {database}")
    if sha256_file(database) != manifest["database"]["sha256"]:
        raise RuntimeError("backup database SHA-256 mismatch")
    info = database_info(database)
    if info["sha256"] != manifest["database"]["sha256"]:
        raise RuntimeError("backup database metadata mismatch")
    image_root = backup_dir / str(manifest["images"]["directory"])
    for item in manifest["images"]["items"]:
        path = safe_image_path(image_root, str(item["relativePath"]))
        if not path.is_file():
            raise FileNotFoundError(f"backup image missing: {item['relativePath']}")
        if sha256_file(path) != item["sha256"]:
            raise RuntimeError(f"backup image SHA-256 mismatch: {item['relativePath']}")
    return manifest, database, image_root


def restore_backup(
    *,
    backup_dir: Path,
    target_db: Path,
    target_image_dir: Path,
    force: bool = False,
    dry_run: bool = False,
    keyring: dict[str, str] | None = None,
) -> dict[str, Any]:
    backup_dir = backup_dir.expanduser().resolve()
    target_db = target_db.expanduser().resolve()
    target_image_dir = target_image_dir.expanduser().resolve()
    manifest, source_database, source_images = validate_backup(backup_dir, keyring=keyring)
    target_exists = target_db.exists() or (target_image_dir.exists() and any(target_image_dir.iterdir()))
    if target_exists and not force:
        raise FileExistsError("target already exists; use --force to replace it")
    if dry_run:
        return {
            "ok": True,
            "dryRun": True,
            "backupDir": str(backup_dir),
            "targetDatabase": str(target_db),
            "targetImageDir": str(target_image_dir),
            "databaseCounts": manifest["database"]["counts"],
            "imageCount": manifest["images"]["count"],
        }

    target_db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=".edge-restore-", dir=target_db.parent))
    snapshot_dir = target_db.parent / f"edge-core-pre-restore-{utc_stamp()}"
    snapshot_created = False
    try:
        staged_db = staging / DATABASE_NAME
        shutil.copy2(source_database, staged_db)
        staged_images = staging / IMAGES_NAME
        shutil.copytree(source_images, staged_images)
        database_info(staged_db)
        for item in manifest["images"]["items"]:
            path = safe_image_path(staged_images, str(item["relativePath"]))
            if sha256_file(path) != item["sha256"]:
                raise RuntimeError(f"staged image hash mismatch: {item['relativePath']}")

        if target_exists:
            snapshot_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            if target_db.exists():
                copy_database(target_db, snapshot_dir / DATABASE_NAME)
            if target_image_dir.exists():
                shutil.copytree(target_image_dir, snapshot_dir / IMAGES_NAME, dirs_exist_ok=True)
            snapshot_created = True

        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(target_db) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        os.replace(staged_db, target_db)
        if target_image_dir.exists():
            shutil.rmtree(target_image_dir)
        shutil.copytree(staged_images, target_image_dir)
        database_info(target_db)
        for item in manifest["images"]["items"]:
            path = safe_image_path(target_image_dir, str(item["relativePath"]))
            if sha256_file(path) != item["sha256"]:
                raise RuntimeError(f"restored image hash mismatch: {item['relativePath']}")
    except Exception:
        if snapshot_created:
            try:
                if (snapshot_dir / DATABASE_NAME).is_file():
                    if target_db.exists():
                        target_db.unlink()
                    shutil.copy2(snapshot_dir / DATABASE_NAME, target_db)
                if (snapshot_dir / IMAGES_NAME).is_dir():
                    if target_image_dir.exists():
                        shutil.rmtree(target_image_dir)
                    shutil.copytree(snapshot_dir / IMAGES_NAME, target_image_dir)
            except Exception:
                pass
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {
        "ok": True,
        "dryRun": False,
        "backupDir": str(backup_dir),
        "targetDatabase": str(target_db),
        "targetImageDir": str(target_image_dir),
        "databaseCounts": manifest["database"]["counts"],
        "imageCount": manifest["images"]["count"],
        "snapshotDir": str(snapshot_dir) if snapshot_created else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    keyring = load_keyring_from_env()
    signing_key = os.getenv("EDGE_EVIDENCE_SIGNING_KEY", "").strip()
    signing_key_id = os.getenv("EDGE_EVIDENCE_SIGNING_KEY_ID", "primary").strip() or "primary"
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup")
    backup.add_argument("--source", type=Path, default=Path(os.getenv("EDGE_DB_PATH", "data/edge_core.sqlite3")))
    backup.add_argument("--images", type=Path, default=Path(os.getenv("EDGE_EVIDENCE_IMAGE_DIR", "data/evidence-images")))
    backup.add_argument("--output-dir", type=Path, default=Path(os.getenv("EDGE_BACKUP_DIR", "data/edge-backups")))
    backup.add_argument("--keep", type=int, default=int(os.getenv("EDGE_BACKUP_KEEP", "7")))

    verify = sub.add_parser("verify")
    verify.add_argument("--backup", type=Path, required=True)

    restore = sub.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--target", type=Path, default=Path(os.getenv("EDGE_DB_PATH", "data/edge_core.sqlite3")))
    restore.add_argument("--target-images", type=Path, default=Path(os.getenv("EDGE_EVIDENCE_IMAGE_DIR", "data/evidence-images")))
    restore.add_argument("--force", action="store_true")
    restore.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    try:
        if args.command == "backup":
            summary = create_backup(
                source_db=args.source,
                image_dir=args.images,
                output_dir=args.output_dir,
                keep=args.keep,
                signing_key=signing_key,
                signing_key_id=signing_key_id,
            )
        elif args.command == "verify":
            manifest, _database, _images = validate_backup(args.backup, keyring=keyring)
            summary = {
                "ok": True,
                "backupDir": str(args.backup.expanduser().resolve()),
                "signatureStatus": manifest.get("signatureStatus"),
                "signingKeyId": (manifest.get("signature") or {}).get("keyId", ""),
                "databaseCounts": manifest["database"]["counts"],
                "imageCount": manifest["images"]["count"],
            }
        else:
            summary = restore_backup(
                backup_dir=args.backup,
                target_db=args.target,
                target_image_dir=args.target_images,
                force=args.force,
                dry_run=args.dry_run,
                keyring=keyring,
            )
    except Exception as exc:
        summary = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
