#!/usr/bin/env python3
"""Restore a validated SQLite queue backup into a target database.

Stop Node-RED before replacing an existing database. The script takes a
pre-restore safety snapshot, removes stale WAL/SHM sidecars, and atomically
installs the restored database. If the existing target cannot be opened or
verified as SQLite, it is moved to a same-directory quarantine file for audit
instead of being silently overwritten.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from backup_queue import readonly_connection, verify_backup

REPO_ROOT = Path(__file__).resolve().parents[1]


def copy_database(source: Path, target: Path) -> None:
    source_connection = readonly_connection(source)
    try:
        destination_connection = sqlite3.connect(target)
        try:
            source_connection.backup(destination_connection, pages=256, sleep=0.05)
        finally:
            destination_connection.close()
    finally:
        source_connection.close()


def unique_path(directory: Path, prefix: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    candidate = directory / f"{prefix}-{stamp}.sqlite"
    index = 1
    while candidate.exists():
        candidate = directory / f"{prefix}-{stamp}-{index:02d}.sqlite"
        index += 1
    return candidate


def is_corruption_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "file is not a database",
            "database disk image is malformed",
            "database corrupt",
            "malformed database schema",
            "integrity_check failed",
            "foreign_key_check failed",
        )
    )


def restore(backup: Path, target: Path, force: bool, dry_run: bool) -> dict:
    if not backup.is_file():
        raise FileNotFoundError(f"backup does not exist: {backup}")
    if backup.resolve() == target.resolve():
        raise ValueError("backup and target must be different files")
    if target.exists() and not force:
        raise FileExistsError("target exists; stop Node-RED and pass --force to replace it")

    source_verification = verify_backup(backup)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.restore.tmp")
    if temporary.exists():
        temporary.unlink()
    pre_restore: Path | None = None
    quarantined: Path | None = None
    try:
        copy_database(backup, temporary)
        restored_verification = verify_backup(temporary)
        summary = {
            "ok": True,
            "dryRun": dry_run,
            "backup": str(backup),
            "target": str(target),
            "sourceIntegrityCheck": source_verification["integrityCheck"],
            "restoredIntegrityCheck": restored_verification["integrityCheck"],
            "tables": restored_verification["tables"],
            "preRestore": None,
            "quarantined": None,
        }
        if dry_run:
            summary["restored"] = False
            return summary

        if target.exists():
            safety_dir = target.parent / "backups"
            safety_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            pre_restore = unique_path(safety_dir, "qc-queue-pre-restore")
            try:
                copy_database(target, pre_restore)
                verify_backup(pre_restore)
            except Exception as exc:
                if pre_restore.exists():
                    pre_restore.unlink()
                pre_restore = None
                if not is_corruption_error(exc):
                    raise
                quarantined = unique_path(target.parent, "qc-queue-corrupt")
                os.replace(target, quarantined)
                summary["quarantined"] = str(quarantined)
            else:
                os.chmod(pre_restore, 0o600)
                summary["preRestore"] = str(pre_restore)

        try:
            if quarantined is not None:
                os.chmod(quarantined, 0o600)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(target) + suffix)
                if sidecar.exists():
                    sidecar.unlink()
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        except Exception:
            if quarantined is not None and not target.exists():
                os.replace(quarantined, target)
                summary["quarantined"] = None
            raise
        summary["restored"] = True
        return summary
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", required=True, help="validated SQLite backup to restore")
    parser.add_argument("--target", required=True, help="target database path")
    parser.add_argument("--force", action="store_true", help="replace an existing target; Node-RED must be stopped")
    parser.add_argument("--dry-run", action="store_true", help="validate and copy without replacing the target")
    args = parser.parse_args()

    backup = Path(args.backup).expanduser().resolve()
    target = Path(args.target).expanduser().resolve()
    try:
        summary = restore(backup, target, args.force, args.dry_run)
    except Exception as exc:
        summary = {
            "ok": False,
            "backup": str(backup),
            "target": str(target),
            "error": f"{type(exc).__name__}: {exc}",
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
