#!/usr/bin/env python3
"""Real mount-level EROFS and ENOSPC drills for state and queue storage."""
from __future__ import annotations

import argparse
import asyncio
import errno
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("QC_MOUNT_DRILL_IMAGE", "alpine:3.20")


class Checks:
    def __init__(self, prefix: str):
        self.prefix = prefix
        self.passed = 0
        self.failures: list[str] = []

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        message = f"{self.prefix}: {name}"
        if condition:
            self.passed += 1
            print(f"PASS | {message}")
        else:
            self.failures.append(f"{message}: {detail}")
            print(f"FAIL | {message} {detail}".rstrip())

    def finish(self) -> int:
        if self.failures:
            for failure in self.failures:
                print(f"ERROR | {failure}")
            print(f"===== {self.prefix}: {self.passed}/{self.passed + len(self.failures)} passed =====")
            return 1
        print(f"===== {self.prefix}: {self.passed}/{self.passed} passed =====")
        return 0


def capture_error(callable_):
    try:
        callable_()
    except Exception as exc:  # noqa: BLE001 - the storage error type is version dependent.
        return exc
    return None


def fill_filesystem(root: Path, chunk_size: int = 64 * 1024) -> Path:
    filler = root / "capacity-filler.bin"
    block = b"x" * chunk_size
    with filler.open("wb", buffering=0) as handle:
        while True:
            try:
                written = handle.write(block)
            except OSError as exc:
                if exc.errno == errno.ENOSPC:
                    return filler
                raise
            if written == 0:
                return filler


def remount(path: Path, mode: str) -> None:
    subprocess.run(
        ["mount", "-o", f"remount,{mode}", str(path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def inside_state_drill(root: Path) -> int:
    from server.qc_server import QCStation
    from server.qc_state import QCStateStore, QCStationSnapshot

    checks = Checks("mount-state")
    state_path = root / "state.json"
    store = QCStateStore(state_path)
    store.save(QCStationSnapshot(status=1, work_order_id="WO-MOUNT", inspect_count=2))
    station = QCStation(None, state_store=store)
    committed_bytes = state_path.read_bytes()

    filler = fill_filesystem(root)
    error = capture_error(
        lambda: asyncio.run(
            station.record_inspection(False, "DENT", 0.91, "IMG-MOUNT-FULL", "SN-MOUNT-FULL")
        )
    )
    checks.check(
        "record_inspection surfaces kernel ENOSPC",
        isinstance(error, OSError) and error.errno == errno.ENOSPC,
        repr(error),
    )
    checks.check(
        "record_inspection rolls back counters and outbox",
        station.inspect_count == 2 and station.ng_count == 0 and station.outbox == [],
        f"inspect={station.inspect_count} ng={station.ng_count} outbox={len(station.outbox)}",
    )
    checks.check(
        "record_inspection restores committed work order",
        station.status == 1 and station.work_order_id == "WO-MOUNT",
        f"status={station.status} workOrder={station.work_order_id}",
    )
    checks.check("ENOSPC leaves the committed snapshot intact", state_path.read_bytes() == committed_bytes)
    checks.check(
        "ENOSPC leaves no state temp file",
        list(root.glob(".state.json.tmp-*")) == [],
        repr(list(root.glob(".state.json.tmp-*"))),
    )
    filler.unlink()
    recovered = capture_error(
        lambda: store.save(QCStationSnapshot(status=1, work_order_id="WO-RECOVERED"))
    )
    checks.check("state save recovers after capacity is released", recovered is None, repr(recovered))
    checks.check(
        "state recovery is durable",
        store.load().work_order_id == "WO-RECOVERED",
        store.load().work_order_id,
    )

    store.save(QCStationSnapshot(status=1, work_order_id="WO-READONLY", inspect_count=5))
    station = QCStation(None, state_store=store)
    readonly_bytes = state_path.read_bytes()
    remount(root, "ro")
    try:
        station.work_order_id = "WO-NOT-COMMITTED"
        station.inspect_count = 99
        error = capture_error(lambda: asyncio.run(station._persist()))
        checks.check(
            "persist surfaces kernel EROFS",
            isinstance(error, OSError) and error.errno == errno.EROFS,
            repr(error),
        )
        checks.check(
            "EROFS rolls back in-memory state",
            station.work_order_id == "WO-READONLY" and station.inspect_count == 5,
            f"workOrder={station.work_order_id} inspect={station.inspect_count}",
        )
        checks.check("EROFS leaves the committed snapshot intact", state_path.read_bytes() == readonly_bytes)
        checks.check(
            "EROFS leaves no state temp file",
            list(root.glob(".state.json.tmp-*")) == [],
            repr(list(root.glob(".state.json.tmp-*"))),
        )
    finally:
        remount(root, "rw")

    recovered = capture_error(
        lambda: store.save(QCStationSnapshot(status=0, work_order_id="WO-AFTER-RO"))
    )
    checks.check("state save recovers after remount to read-write", recovered is None, repr(recovered))
    checks.check(
        "post-remount state is durable",
        store.load().work_order_id == "WO-AFTER-RO",
        store.load().work_order_id,
    )
    return checks.finish()


def container_script(host_base: str, inside_base: str) -> str:
    python = shlex.quote(str(Path(sys.executable).absolute()))
    node = shlex.quote(os.environ.get("NODE_BIN") or shutil.which("node") or "node")
    repo = shlex.quote(str(REPO_ROOT))
    state_test = shlex.quote(str(REPO_ROOT / "tests" / "mount_storage_test.py"))
    sqlite_test = shlex.quote(str(REPO_ROOT / "tests" / "mount_sqlite_storage_test.js"))
    state_host = shlex.quote(f"{host_base}/state")
    sqlite_host = shlex.quote(f"{host_base}/sqlite")
    state_inside = shlex.quote(f"{inside_base}/state")
    sqlite_inside = shlex.quote(f"{inside_base}/sqlite")
    return f"""
set -eu
state={state_host}
sqlite={sqlite_host}
cleanup() {{
  for mount_path in "$state" "$sqlite"; do
    mount -o remount,rw "$mount_path" 2>/dev/null || true
    umount "$mount_path" 2>/dev/null || true
  done
}}
trap cleanup EXIT INT TERM
mkdir -p "$state" "$sqlite"
mount -t tmpfs -o size=2m,mode=0700 tmpfs "$state"
mount -t tmpfs -o size=8m,mode=0700 tmpfs "$sqlite"
chroot /host env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={repo} {python} {state_test} --inside-state {state_inside}
chroot /host {node} {sqlite_test} {sqlite_inside}
""".strip()


def run_container_drill() -> int:
    if not shutil.which("docker"):
        print("FAIL | mount-storage: docker is required for the privileged mount drill")
        return 1
    docker_info = subprocess.run(
        ["docker", "info"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if docker_info.returncode != 0:
        print("FAIL | mount-storage: Docker daemon is unavailable")
        print(docker_info.stdout[-4000:])
        return 1

    base_path = Path(tempfile.mkdtemp(prefix="ua-linq-mount-", dir="/tmp"))
    instance = base_path.name
    script = container_script(f"/host/tmp/{instance}", f"/tmp/{instance}")
    try:
        completed = subprocess.run(
            [
                "docker", "run", "--rm", "--privileged", "--network", "none",
                "-v", "/:/host:ro", "-v", f"{base_path}:/host/tmp/{instance}:rw",
                IMAGE, "sh", "-c", script,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    finally:
        shutil.rmtree(base_path, ignore_errors=True)
    print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
    passed = sum(line.startswith("PASS |") for line in completed.stdout.splitlines())
    failed = sum(line.startswith("FAIL |") for line in completed.stdout.splitlines())
    print(f"===== MOUNT STORAGE: {passed}/{passed + failed} passed =====")
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inside-state", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside_state is not None:
        return inside_state_drill(args.inside_state)
    return run_container_drill()


if __name__ == "__main__":
    raise SystemExit(main())
