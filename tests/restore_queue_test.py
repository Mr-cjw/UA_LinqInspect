"""Validate the offline SQLite restore workflow and application compatibility."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = REPO_ROOT / "scripts" / "backup_queue.py"
RESTORE_SCRIPT = REPO_ROOT / "scripts" / "restore_queue.py"
NODE_BIN = os.environ.get("NODE_BIN", "node")

results: list[tuple[str, bool]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'} | {name} {detail}")


def run_json(command: list[str]) -> tuple[subprocess.CompletedProcess, dict]:
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
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = {"parseError": completed.stdout, "stderr": completed.stderr}
    return completed, payload


def run_backup(source: Path, output_dir: Path) -> tuple[subprocess.CompletedProcess, dict]:
    return run_json(
        [
            sys.executable,
            str(BACKUP_SCRIPT),
            "--source",
            str(source),
            "--output-dir",
            str(output_dir),
            "--keep",
            "3",
        ]
    )


def run_restore(
    backup: Path, target: Path, force: bool = False, dry_run: bool = False
) -> tuple[subprocess.CompletedProcess, dict]:
    command = [
        sys.executable,
        str(RESTORE_SCRIPT),
        "--backup",
        str(backup),
        "--target",
        str(target),
    ]
    if force:
        command.append("--force")
    if dry_run:
        command.append("--dry-run")
    return run_json(command)


def node_eval(script: str, db_path: Path) -> dict:
    completed = subprocess.run(
        [NODE_BIN, "-e", script],
        cwd=REPO_ROOT,
        env=os.environ | {"DB_PATH": str(db_path)},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout.strip().splitlines()[-1])


SEED_SOURCE = r"""
const { createStore } = require('./nodered/sqlite_store.js');
const store = createStore({ dbPath: process.env.DB_PATH });
store.enqueue({
  id: 'ng|SN-RESTORE-1|2026-11-20T12:00:00.000Z',
  type: 'ng',
  correlationId: 'corr-restore-1',
  url: 'http://127.0.0.1/mes/ng-order',
  body: { schemaVersion: '1.0', serialNumber: 'SN-RESTORE-1' },
  createdAt: '2026-11-20T12:00:00.000Z',
  dedupKeys: [
    { kind: 'ng_fp', key: 'SN-RESTORE-1|2026-11-20T12:00:00.000Z' },
    { kind: 'ng_sn', key: 'SN-RESTORE-1' },
  ],
  now: 1000,
});
const sent = store.claimNext(2000);
store.deadLetter({
  id: sent.item.id,
  correlationId: sent.item.correlationId,
  errorCode: 'MES_REJECTED',
  reason: 'HTTP 400',
  now: 3000,
});
store.enqueue({
  id: 'ng|SN-RESTORE-2|2026-11-20T12:00:01.000Z',
  type: 'ng',
  correlationId: 'corr-restore-2',
  url: 'http://127.0.0.1/mes/ng-order',
  body: { schemaVersion: '1.0', serialNumber: 'SN-RESTORE-2' },
  createdAt: '2026-11-20T12:00:01.000Z',
  dedupKeys: [
    { kind: 'ng_fp', key: 'SN-RESTORE-2|2026-11-20T12:00:01.000Z' },
    { kind: 'ng_sn', key: 'SN-RESTORE-2' },
  ],
  now: 4000,
});
store.enqueue({
  id: 'done|WO-RESTORE-1',
  type: 'complete',
  correlationId: 'corr-restore-3',
  url: 'http://127.0.0.1/mes/complete',
  body: { schemaVersion: '1.0', workOrderId: 'WO-RESTORE-1' },
  createdAt: '2026-11-20T12:00:02.000Z',
  dedupKeys: [{ kind: 'done', key: 'WO-RESTORE-1' }],
  now: 5000,
});
console.log(JSON.stringify({ status: store.status(), metrics: store.metricSnapshot() }));
store.close();
"""

SEED_TARGET = r"""
const { createStore } = require('./nodered/sqlite_store.js');
const store = createStore({ dbPath: process.env.DB_PATH });
store.enqueue({
  id: 'ng|SN-OLD|2026-11-19T12:00:00.000Z',
  type: 'ng',
  correlationId: 'corr-old',
  url: 'http://127.0.0.1/mes/ng-order',
  body: { schemaVersion: '1.0', serialNumber: 'SN-OLD' },
  createdAt: '2026-11-19T12:00:00.000Z',
  dedupKeys: [
    { kind: 'ng_fp', key: 'SN-OLD|2026-11-19T12:00:00.000Z' },
    { kind: 'ng_sn', key: 'SN-OLD' },
  ],
  now: 1000,
});
console.log(JSON.stringify(store.status()));
store.close();
"""

VERIFY_RESTORED = r"""
const { createStore } = require('./nodered/sqlite_store.js');
const store = createStore({ dbPath: process.env.DB_PATH });
const before = store.status();
const metrics = store.metricSnapshot();
const claimed = store.claimNext(Date.now());
let completed = null;
if (claimed.state === 'send') {
  const targets = [
    { topic: 'ns=2;s=QCStation.LastNgMesOrderNo', value: 'MES-RESTORE-0001' },
    { topic: 'ns=2;s=QCStation.LastMesOrderNo', value: 'MES-RESTORE-0001' },
  ];
  completed = store.complete({
    id: claimed.item.id,
    correlationId: claimed.item.correlationId,
    receipt: 'MES-RESTORE-0001',
    targets,
  });
  for (const target of targets) {
    store.confirmUaWriteTarget({
      id: claimed.item.id,
      correlationId: claimed.item.correlationId,
      target: target.topic,
      value: target.value,
    });
  }
}
const after = store.status();
console.log(JSON.stringify({ before, after, metrics, claimed: claimed.state, completed: !!completed }));
store.close();
"""


def queue_rows(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM queue").fetchone()[0])
    finally:
        connection.close()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="qc-restore-test-") as temporary:
        root = Path(temporary)
        source = root / "source" / "qc-queue.sqlite"
        target = root / "target" / "qc-queue.sqlite"
        source.parent.mkdir(parents=True)
        target.parent.mkdir(parents=True)
        source_state = node_eval(SEED_SOURCE, source)
        node_eval(SEED_TARGET, target)
        check("restore fixture has source queue and dead letter",
              source_state["status"]["queue"]["total"] == 2 and
              source_state["status"]["deadLetters"]["total"] == 1)

        completed, backup_summary = run_backup(source, root / "backups")
        check("backup for restore succeeds", completed.returncode == 0 and backup_summary.get("ok"))
        backup = Path(str(backup_summary.get("backup") or ""))
        check("backup file exists", backup.is_file(), str(backup))

        old_rows = queue_rows(target)
        completed, refused = run_restore(backup, target)
        check("existing target requires --force", completed.returncode != 0 and
              "--force" in str(refused.get("error")))
        check("refused restore leaves target unchanged", queue_rows(target) == old_rows)

        completed, dry = run_restore(backup, target, force=True, dry_run=True)
        check("dry-run validates backup", completed.returncode == 0 and dry.get("ok") is True and
              dry.get("restored") is False)
        check("dry-run leaves target unchanged", queue_rows(target) == old_rows)
        check("dry-run does not create pre-restore snapshot", not any((target.parent / "backups").glob("*.sqlite")))

        completed, restored = run_restore(backup, target, force=True)
        check("restore succeeds", completed.returncode == 0 and restored.get("ok") is True,
              str(restored.get("error") or ""))
        check("restore reports integrity checks",
              restored.get("sourceIntegrityCheck") == "ok" and
              restored.get("restoredIntegrityCheck") == "ok")
        check("restore reports schema checks",
              restored.get("sourceSchemaCheck") == "ok" and
              restored.get("restoredSchemaCheck") == "ok")
        pre_restore = Path(str(restored.get("preRestore") or ""))
        check("pre-restore safety snapshot exists", pre_restore.is_file(), str(pre_restore))
        check("pre-restore snapshot preserves previous target", pre_restore.is_file() and
              queue_rows(pre_restore) == old_rows)
        check("stale WAL/SHM sidecars are removed",
              not Path(str(target) + "-wal").exists() and
              not Path(str(target) + "-shm").exists())

        verified = node_eval(VERIFY_RESTORED, target)
        check("restored queue counts match backup",
              verified["before"]["queue"]["total"] == 2,
              f"({verified['before']['queue']})")
        check("restored dead letters match backup",
              verified["before"]["deadLetters"]["total"] == 1 and
              verified["before"]["deadLetters"]["last"]["errorCode"] == "MES_REJECTED")
        check("restored dedup keys match backup",
              verified["before"]["dedup"] == {
                  "ngFingerprints": 2,
                  "ngSerialNumbers": 2,
                  "completedWorkOrders": 1,
              }, f"({verified['before']['dedup']})")
        check("restored metrics remain queryable",
              any(item["name"] == "delivery_total" and item["type"] == "ng" and
                  item["outcome"] == "dead" and item["value"] == 1
                  for item in verified["metrics"]))
        check("restored queue continues to dispatch",
              verified["claimed"] == "send" and verified["completed"] is True)
        check("restored queue drains after one dispatch",
              verified["after"]["queue"]["total"] == 1)

        read_only_backup_dir = root / "read-only-backup-source"
        read_only_backup_dir.mkdir()
        read_only_backup = read_only_backup_dir / "qc-queue.sqlite"
        shutil.copy2(backup, read_only_backup)
        os.chmod(read_only_backup, 0o444)
        os.chmod(read_only_backup_dir, 0o500)
        try:
            completed, from_read_only = run_restore(
                read_only_backup, root / "read-only-backup-target.sqlite"
            )
            check("read-only backup file can be restored",
                  completed.returncode == 0 and from_read_only.get("ok") is True and
                  queue_rows(root / "read-only-backup-target.sqlite") == 2,
                  str(from_read_only.get("error") or ""))
        finally:
            os.chmod(read_only_backup_dir, 0o700)

        read_only_target_dir = root / "read-only-target"
        read_only_target_dir.mkdir()
        read_only_target = read_only_target_dir / "qc-queue.sqlite"
        node_eval(SEED_TARGET, read_only_target)
        target_bytes = read_only_target.read_bytes()
        os.chmod(read_only_target_dir, 0o500)
        try:
            completed, blocked = run_restore(backup, read_only_target, force=True)
            check("read-only restore target directory is rejected",
                  completed.returncode != 0 and
                  "restore target directory is not writable" in str(blocked.get("error")),
                  str(blocked.get("error") or ""))
            check("read-only target directory leaves database bytes unchanged",
                  read_only_target.read_bytes() == target_bytes)
            check("read-only target directory creates no pre-restore snapshot",
                  not (read_only_target_dir / "backups").exists())
            check("read-only target directory mode is preserved",
                  stat.S_IMODE(read_only_target_dir.stat().st_mode) == 0o500)
        finally:
            os.chmod(read_only_target_dir, 0o700)

        read_only_empty_dir = root / "read-only-empty-target"
        read_only_empty_dir.mkdir()
        os.chmod(read_only_empty_dir, 0o500)
        missing_target = read_only_empty_dir / "qc-queue.sqlite"
        try:
            completed, blocked = run_restore(backup, missing_target)
            check("restore cannot create a target in a read-only directory",
                  completed.returncode != 0 and
                  "restore target directory is not writable" in str(blocked.get("error")),
                  str(blocked.get("error") or ""))
            check("read-only directory remains empty after rejected restore",
                  not missing_target.exists() and not any(read_only_empty_dir.iterdir()))
        finally:
            os.chmod(read_only_empty_dir, 0o700)

        corrupt = root / "corrupt.sqlite"
        corrupt.write_text("not a sqlite database\n", encoding="utf-8")
        completed, invalid = run_restore(corrupt, root / "invalid-target.sqlite")
        check("corrupt backup is rejected", completed.returncode != 0 and
              invalid.get("ok") is False)

        schema_target = root / "schema-target" / "qc-queue.sqlite"
        schema_target.parent.mkdir(parents=True)
        node_eval(SEED_TARGET, schema_target)
        schema_target_rows = queue_rows(schema_target)
        schema_snapshots_before = set((schema_target.parent / "backups").glob("*.sqlite"))
        empty_backup = root / "schema-less.sqlite"
        empty_connection = sqlite3.connect(empty_backup)
        empty_connection.close()
        completed, incompatible = run_restore(empty_backup, schema_target, force=True)
        check("valid SQLite without queue schema is rejected as a backup",
              completed.returncode != 0 and
              "queue schema check failed" in str(incompatible.get("error")),
              str(incompatible.get("error") or ""))
        check("schema-invalid backup leaves target unchanged",
              queue_rows(schema_target) == schema_target_rows)
        check("schema-invalid backup creates no pre-restore snapshot",
              set((schema_target.parent / "backups").glob("*.sqlite")) == schema_snapshots_before)

        corrupt_target = root / "corrupt-target" / "qc-queue.sqlite"
        corrupt_target.parent.mkdir(parents=True)
        corrupt_bytes = (b"not a sqlite database\n" * 200)
        corrupt_target.write_bytes(corrupt_bytes)
        for suffix in ("-wal", "-shm"):
            Path(str(corrupt_target) + suffix).write_bytes(b"stale sidecar")

        completed, recovered = run_restore(backup, corrupt_target, force=True)
        check("corrupt target can be force-restored", completed.returncode == 0 and
              recovered.get("ok") is True, str(recovered.get("error") or ""))
        check("corrupt target has no misleading pre-restore snapshot",
              recovered.get("preRestore") is None)
        quarantined = Path(str(recovered.get("quarantined") or ""))
        check("corrupt target is quarantined beside the target",
              quarantined.is_file() and quarantined.parent == corrupt_target.parent and
              quarantined.name.startswith("qc-queue-corrupt-"), str(quarantined))
        check("quarantine preserves the corrupt original bytes",
              quarantined.is_file() and quarantined.read_bytes() == corrupt_bytes)
        check("corrupt target is replaced by the validated backup",
              queue_rows(corrupt_target) == 2)
        check("corrupt target keeps no stale WAL/SHM sidecars",
              not Path(str(corrupt_target) + "-wal").exists() and
              not Path(str(corrupt_target) + "-shm").exists())

        recovered_verified = node_eval(VERIFY_RESTORED, corrupt_target)
        check("restored corrupt target continues to dispatch",
              recovered_verified["claimed"] == "send" and
              recovered_verified["completed"] is True)
        check("restored corrupt target still drains",
              recovered_verified["after"]["queue"]["total"] == 1)

    passed = sum(1 for _, value in results if value)
    print(f"\n===== RESTORE QUEUE: {passed}/{len(results)} passed =====")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
