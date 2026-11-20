"""Validate SQLite online backup, integrity checks and retention policy."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = REPO_ROOT / "scripts" / "backup_queue.py"

results: list[tuple[str, bool]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'} | {name} {detail}")


def run_backup(
    source: Path, output_dir: Path, keep: int = 7, metrics_file: Path | None = None
) -> tuple[subprocess.CompletedProcess, dict]:
    command = [
        sys.executable,
        str(BACKUP_SCRIPT),
        "--source",
        str(source),
        "--output-dir",
        str(output_dir),
        "--keep",
        str(keep),
    ]
    if metrics_file is not None:
        command.extend(["--metrics-file", str(metrics_file)])
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    try:
        summary = json.loads(completed.stdout)
    except json.JSONDecodeError:
        summary = {"parseError": completed.stdout, "stderr": completed.stderr}
    return completed, summary


def db_counts(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        return {
            "queue": int(connection.execute("SELECT COUNT(*) FROM queue").fetchone()[0]),
            "dead_letters": int(connection.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0]),
            "dedup": int(connection.execute("SELECT COUNT(*) FROM dedup").fetchone()[0]),
            "metrics": int(connection.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]),
        }
    finally:
        connection.close()


def create_source(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.executescript(
        """
        CREATE TABLE queue (id TEXT PRIMARY KEY, status TEXT NOT NULL, body_json TEXT NOT NULL);
        CREATE TABLE dead_letters (queue_id TEXT PRIMARY KEY, error_code TEXT NOT NULL);
        CREATE TABLE dedup (kind TEXT NOT NULL, dedup_key TEXT NOT NULL, PRIMARY KEY(kind, dedup_key));
        CREATE TABLE metrics (name TEXT NOT NULL, value INTEGER NOT NULL);
        """
    )
    connection.executemany(
        "INSERT INTO queue(id, status, body_json) VALUES (?, ?, ?)",
        [
            ("ng|SN-1", "PENDING", '{"serialNumber":"SN-1"}'),
            ("ng|SN-2", "SENDING", '{"serialNumber":"SN-2"}'),
            ("done|WO-1", "PENDING", '{"workOrderId":"WO-1"}'),
        ],
    )
    connection.executemany(
        "INSERT INTO dead_letters(queue_id, error_code) VALUES (?, ?)",
        [("ng|DEAD-1", "MES_REJECTED"), ("done|DEAD-2", "MES_UNAVAILABLE")],
    )
    connection.executemany(
        "INSERT INTO dedup(kind, dedup_key) VALUES (?, ?)",
        [("ng_fp", "SN-1|2026-11-20T12:00:00Z"), ("ng_sn", "SN-1"), ("done", "WO-1")],
    )
    connection.executemany(
        "INSERT INTO metrics(name, value) VALUES (?, ?)",
        [("enqueue_total", 3), ("delivery_total", 2), ("watchdog_total", 1)],
    )
    connection.commit()
    return connection


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="qc-backup-test-") as temporary:
        root = Path(temporary)
        source = root / "qc-queue.sqlite"
        output_dir = root / "backups"
        source_connection = create_source(source)
        try:
            expected_counts = db_counts(source)
            metrics_file = root / "node-exporter" / "qc-backup.prom"
            completed, summary = run_backup(source, output_dir, keep=2, metrics_file=metrics_file)
            check("backup script exits successfully", completed.returncode == 0,
                  completed.stderr.strip())
            check("backup summary reports ok", summary.get("ok") is True)
            backup_path = Path(str(summary.get("backup") or ""))
            check("backup file exists", backup_path.is_file(), str(backup_path))
            check("backup reports integrity_check=ok", summary.get("integrityCheck") == "ok")
            check("backup reports empty foreign_key_check", summary.get("foreignKeyCheck") == "ok")
            check("backup includes committed WAL rows", backup_path.is_file() and
                  db_counts(backup_path) == expected_counts,
                  f"({db_counts(backup_path) if backup_path.is_file() else {}})")
            copied = sqlite3.connect(backup_path)
            try:
                check("backup passes SQLite integrity_check",
                      copied.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
            finally:
                copied.close()
            metrics_text = metrics_file.read_text(encoding="utf-8") if metrics_file.is_file() else ""
            check("backup writes Prometheus textfile", summary.get("metricsFile") == str(metrics_file))
            check("backup metrics expose success timestamp",
                  "qc_backup_last_success_timestamp_seconds " in metrics_text)
            check("backup metrics expose integrity and row counts",
                  "qc_backup_integrity_ok 1" in metrics_text and
                  'qc_backup_table_rows{table="queue"} 3' in metrics_text)

            latest = summary
            for _ in range(4):
                completed, latest = run_backup(source, output_dir, keep=2)
                check("retention run exits successfully", completed.returncode == 0)
            retained = sorted(
                path.name for path in output_dir.glob("qc-queue-*.sqlite") if path.is_file()
            )
            check("retention keeps exactly two backups", len(retained) == 2, f"({retained})")
            check("newest backup is retained", Path(str(latest.get("backup") or "")).is_file())
            check("retention summary reports removed files",
                  len(latest.get("removed") or []) >= 1)
        finally:
            source_connection.close()

        missing = root / "missing.sqlite"
        completed, summary = run_backup(missing, root / "missing-backups")
        check("missing source fails", completed.returncode != 0)
        check("missing source returns machine-readable error",
              summary.get("ok") is False and "does not exist" in str(summary.get("error")))

        live_source = os.getenv("QC_DB_PATH")
        if live_source and Path(live_source).is_file():
            completed, summary = run_backup(
                Path(live_source).resolve(), root / "live-backups", keep=2
            )
            check("active Node-RED database can be backed up online",
                  completed.returncode == 0 and summary.get("integrityCheck") == "ok",
                  str(summary.get("error") or ""))

    passed = sum(1 for _, value in results if value)
    print(f"\n===== BACKUP QUEUE: {passed}/{len(results)} passed =====")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
