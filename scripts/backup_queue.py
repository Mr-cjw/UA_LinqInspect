#!/usr/bin/env python3
"""Create a consistent online backup of the SQLite edge queue.

The script uses SQLite's backup API so committed rows in WAL are included even
while Node-RED is running. The temporary backup is integrity-checked before it
is atomically renamed into place.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KEEP = 7
COUNTED_TABLES = ("queue", "dead_letters", "dedup", "metrics")


def resolve_source(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    configured = os.getenv("QC_DB_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return (REPO_ROOT / "data" / "qc-queue.sqlite").resolve()


def readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10.0)
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    existing = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    counts: dict[str, int] = {}
    for table in COUNTED_TABLES:
        if table in existing:
            counts[table] = int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        else:
            counts[table] = 0
    return counts


def verify_backup(path: Path) -> dict:
    connection = sqlite3.connect(path)
    try:
        integrity_rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()]
        if integrity_rows != ["ok"]:
            raise RuntimeError(f"integrity_check failed: {integrity_rows}")
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise RuntimeError(f"foreign_key_check failed: {foreign_key_errors}")
        return {
            "integrityCheck": "ok",
            "foreignKeyCheck": "ok",
            "tables": table_counts(connection),
        }
    finally:
        connection.close()


def choose_target(output_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = output_dir / f"qc-queue-{stamp}.sqlite"
    index = 1
    while target.exists():
        target = output_dir / f"qc-queue-{stamp}-{index:02d}.sqlite"
        index += 1
    return target


def prune_backups(
    output_dir: Path, keep: int, current: Path, source: Path
) -> tuple[list[str], list[str], list[str]]:
    candidates = [
        path
        for path in output_dir.glob("qc-queue-*.sqlite")
        if path.is_file() and path != current and path.resolve() != source
    ]
    candidates.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    removed: list[str] = []
    errors: list[str] = []
    for path in candidates[keep - 1:]:
        try:
            path.unlink()
            removed.append(str(path))
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    retained = [str(current)] + [str(path) for path in candidates[:keep - 1]]
    return retained, removed, errors


def write_metrics_file(path: Path, summary: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tables = summary.get("tables") or {}
    lines = [
        '# HELP qc_backup_last_success_timestamp_seconds Unix timestamp of the last successful backup.',
        '# TYPE qc_backup_last_success_timestamp_seconds gauge',
        f'qc_backup_last_success_timestamp_seconds {int(time.time())}',
        '# HELP qc_backup_size_bytes Size of the last successful backup file.',
        '# TYPE qc_backup_size_bytes gauge',
        f'qc_backup_size_bytes {int(summary.get("sizeBytes") or 0)}',
        '# HELP qc_backup_integrity_ok Whether the last backup passed integrity checks.',
        '# TYPE qc_backup_integrity_ok gauge',
        'qc_backup_integrity_ok 1',
        '# HELP qc_backup_table_rows Rows copied per key table.',
        '# TYPE qc_backup_table_rows gauge',
    ]
    for table in COUNTED_TABLES:
        lines.append(f'qc_backup_table_rows{{table="{table}"}} {int(tables.get(table) or 0)}')
    try:
        temporary.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return str(path)


def create_backup(source: Path, output_dir: Path, keep: int, metrics_file: Path | None = None) -> dict:
    if not source.is_file():
        raise FileNotFoundError(f"source database does not exist: {source}")
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(output_dir, 0o700)
    except OSError:
        pass

    target = choose_target(output_dir)
    temporary = output_dir / f".{target.name}.{os.getpid()}.tmp"
    if temporary.exists():
        temporary.unlink()

    try:
        source_connection = readonly_connection(source)
        try:
            destination_connection = sqlite3.connect(temporary)
            try:
                source_connection.backup(destination_connection, pages=256, sleep=0.05)
            finally:
                destination_connection.close()
        finally:
            source_connection.close()

        verification = verify_backup(temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise

    retained, removed, retention_errors = prune_backups(output_dir, keep, target, source)
    summary = {
        "ok": True,
        "source": str(source),
        "backup": str(target),
        "sizeBytes": target.stat().st_size,
        "integrityCheck": verification["integrityCheck"],
        "foreignKeyCheck": verification["foreignKeyCheck"],
        "tables": verification["tables"],
        "retained": retained,
        "removed": removed,
        "retentionErrors": retention_errors,
    }
    if metrics_file is not None:
        try:
            summary["metricsFile"] = write_metrics_file(metrics_file, summary)
        except Exception as exc:
            summary["metricsError"] = f"{type(exc).__name__}: {exc}"
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="SQLite source path; defaults to QC_DB_PATH or data/qc-queue.sqlite")
    parser.add_argument("--output-dir", help="backup directory; defaults to <source-parent>/backups")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="number of newest backups to retain")
    parser.add_argument("--metrics-file", help="optional Prometheus textfile describing the last successful backup")
    args = parser.parse_args()
    if args.keep < 1:
        parser.error("--keep must be at least 1")

    source = resolve_source(args.source)
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else source.parent / "backups"
    )
    try:
        metrics_file = Path(args.metrics_file).expanduser().resolve() if args.metrics_file else None
        summary = create_backup(source, output_dir, args.keep, metrics_file)
    except Exception as exc:
        summary = {
            "ok": False,
            "source": str(source),
            "outputDir": str(output_dir),
            "error": f"{type(exc).__name__}: {exc}",
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
