#!/usr/bin/env python3
"""Verify QC work-order, counters and SN sequence survive a server SIGKILL."""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ua_test_client import create_secure_client

REPO_ROOT = Path(__file__).resolve().parents[1]
NS_URI = "urn:fde:linqinspect:qc"
RESULTS: list[tuple[str, bool]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'} | {name} {detail}")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_log(path: Path, needle: str, timeout: float = 25) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and needle in path.read_text(encoding="utf-8", errors="replace"):
            return
        time.sleep(0.1)
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    raise RuntimeError(f"Timed out waiting for {needle!r} in {path}\n{text[-2000:]}")


def start_server(env: dict[str, str], log_path: Path) -> subprocess.Popen:
    handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "server" / "qc_server.py")],
        cwd=REPO_ROOT,
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    process._log_handle = handle  # type: ignore[attr-defined]
    try:
        wait_for_log(log_path, "OPC UA Server listening")
    except Exception:
        stop_server(process, crash=True)
        raise
    return process


def stop_server(process: subprocess.Popen | None, *, crash: bool = False) -> None:
    if process is None:
        return
    if process.poll() is None:
        if crash:
            os.kill(process.pid, signal.SIGKILL)
        else:
            process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    handle = getattr(process, "_log_handle", None)
    if handle is not None and not handle.closed:
        handle.close()


async def read(node):
    try:
        return await node.read_value()
    except Exception:
        return None


async def wait_until(predicate, timeout: float = 12, interval: float = 0.1, label: str = "condition") -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            value = predicate()
            if asyncio.iscoroutine(value):
                value = await value
            if value:
                return True
        except Exception:
            pass
        await asyncio.sleep(interval)
    print(f"TIMEOUT | {label}")
    return False


async def nodes_for(client):
    idx = await client.get_namespace_index(NS_URI)
    root = await client.nodes.objects.get_child([f"{idx}:QCStation"])
    insp = await root.get_child([f"{idx}:Inspection"])
    stats = await root.get_child([f"{idx}:Statistics"])
    return {
        "idx": idx,
        "root": root,
        "status": await root.get_child([f"{idx}:DeviceStatus"]),
        "work_order": await root.get_child([f"{idx}:CurrentWorkOrderId"]),
        "part_sn": await root.get_child([f"{idx}:CurrentPartSN"]),
        "count": await stats.get_child([f"{idx}:InspectCount"]),
    }


def sn_sequence(value: object) -> int:
    match = re.search(r"-(\d{6})$", str(value or ""))
    return int(match.group(1)) if match else -1


def read_sqlite_state(db_path: Path) -> dict:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM qc_station_state WHERE station_id = 'QCStation'"
        ).fetchone()
    if not row:
        return {}
    value = dict(row)
    value["outbox"] = json.loads(value.get("outbox_json") or "[]")
    return value


async def main() -> int:
    for required in (
        REPO_ROOT / "certs/server/server_cert.der",
        REPO_ROOT / "certs/server/server_key.pem",
        REPO_ROOT / "certs/trusted_clients",
    ):
        if not required.exists():
            raise SystemExit(f"Missing test prerequisite: {required}")

    port = free_port()
    work_order = f"WO-STATE-RESTART-{int(time.time())}"
    with tempfile.TemporaryDirectory(prefix="qc-state-restart-") as temp:
        root = Path(temp)
        legacy_state_path = root / "qc-state.json"
        state_db_path = root / "qc-edge.sqlite3"
        env = os.environ.copy()
        admin_user = env.get("UA_ADMIN_USER") or "state-restart-admin"
        admin_pass = env.get("UA_ADMIN_PASS") or "state-restart-password"
        env.update(
            {
                "PYTHONUNBUFFERED": "1",
                "PYTHONPATH": str(REPO_ROOT),
                "UA_ENDPOINT": f"opc.tcp://127.0.0.1:{port}",
                "UA_ENDPOINT_PORT": str(port),
                "UA_BIND_ADDRESS": "127.0.0.1",
                "UA_ADMIN_USER": admin_user,
                "UA_ADMIN_PASS": admin_pass,
                "UA_SERVER_CERT": str(REPO_ROOT / "certs/server/server_cert.der"),
                "UA_SERVER_PRIVATE_KEY": str(REPO_ROOT / "certs/server/server_key.pem"),
                "UA_TRUSTED_CLIENT_CERTS_DIR": str(REPO_ROOT / "certs/trusted_clients"),
                "UA_AUDIT_LOG": str(root / "ua-audit.log"),
                "QC_STATE_PATH": str(legacy_state_path),
                "QC_STATE_BACKEND": "sqlite",
                "QC_STATE_DB_PATH": str(state_db_path),
                "QC_AUTO_INSPECT": "1",
                "QC_INSPECT_INTERVAL": "0.15",
                "QC_NG_PROBABILITY": "1.0",
                "QC_NG_WINDOW": "5",
            }
        )
        os.environ.update({"UA_ENDPOINT": env["UA_ENDPOINT"], "UA_ADMIN_USER": admin_user, "UA_ADMIN_PASS": admin_pass})

        first: subprocess.Popen | None = None
        second: subprocess.Popen | None = None
        try:
            first = start_server(env, root / "server-first.log")
            client = await create_secure_client()
            async with client:
                nodes = await nodes_for(client)
                ret = await nodes["root"].call_method(f"{nodes['idx']}:StartInspection", work_order, 5)
                check("first server accepts StartInspection", ret and ret[0] is True,
                      f"({ret[1] if len(ret) > 1 else ''})")

                async def persisted_and_running():
                    if not state_db_path.exists():
                        return False
                    state = read_sqlite_state(state_db_path)
                    return (
                        state.get("status") == 1
                        and state.get("work_order_id") == work_order
                        and int(state.get("inspect_count") or 0) >= 2
                        and bool(state.get("current_part_sn"))
                    )

                persisted = await wait_until(
                    persisted_and_running, timeout=15, label="durable running state before crash"
                )
                check("durable state records running work order and inspections", persisted)
                outbox_before_crash = read_sqlite_state(state_db_path).get("outbox", [])
                check("SQLite transaction persists NG outbox events",
                      len(outbox_before_crash) >= 2 and
                      all(str(event.get("eventId", "")).startswith("evt-") for event in outbox_before_crash),
                      f"(events={len(outbox_before_crash)})")
                before_count = int(await read(nodes["count"]) or 0)
                before_sn = str(await read(nodes["part_sn"]) or "")
                check("first server produced inspection and SN", before_count >= 2 and sn_sequence(before_sn) > 0,
                      f"(count={before_count}, sn={before_sn or 'empty'})")

            stop_server(first, crash=True)
            first = None
            persisted_state = read_sqlite_state(state_db_path)
            persisted_count = int(persisted_state.get("inspect_count") or 0)
            persisted_sn = str(persisted_state.get("current_part_sn") or "")
            check("SIGKILL leaves recoverable schema-v1 state",
                  persisted_state.get("schema_version") == 1 and
                  persisted_state.get("status") == 1 and persisted_count >= 2,
                  f"(count={persisted_count}, sn={persisted_sn or 'empty'})")
            outbox_after_crash = persisted_state.get("outbox", [])
            check("outbox survives SIGKILL without duplicate event ids",
                  len(outbox_after_crash) >= len(outbox_before_crash) and
                  len({event.get("eventId") for event in outbox_after_crash}) == len(outbox_after_crash),
                  f"(events={len(outbox_after_crash)})")

            second = start_server(env, root / "server-second.log")
            client = await create_secure_client()
            async with client:
                nodes = await nodes_for(client)
                check("restart restores running status", await read(nodes["status"]) == 1)
                check("restart restores active work order", str(await read(nodes["work_order"]) or "") == work_order)
                restored_count = int(await read(nodes["count"]) or 0)
                check("restart preserves inspection counter", restored_count >= persisted_count,
                      f"(before={persisted_count}, after={restored_count})")

                async def next_inspection():
                    count = int(await read(nodes["count"]) or 0)
                    sn = str(await read(nodes["part_sn"]) or "")
                    return count > restored_count and sn_sequence(sn) > sn_sequence(persisted_sn)

                continued = await wait_until(next_inspection, timeout=10, label="post-restart SN continuation")
                after_count = int(await read(nodes["count"]) or 0)
                after_sn = str(await read(nodes["part_sn"]) or "")
                check("generated SN sequence continues after restart", continued,
                      f"(before={persisted_sn or 'empty'}, after={after_sn or 'empty'})")
                check("inspection counter continues after restart", after_count > restored_count,
                      f"(before={restored_count}, after={after_count})")

                ret_done = await nodes["root"].call_method(f"{nodes['idx']}:CompleteWorkOrder", work_order)
                check("restored work order can be completed", ret_done and ret_done[0] is True,
                      f"({ret_done[1] if len(ret_done) > 1 else ''})")
                check("complete transition returns station to idle", await read(nodes["status"]) == 0)
        finally:
            stop_server(second)
            stop_server(first, crash=True)

    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"===== QC STATE RESTART: {passed}/{len(RESULTS)} passed =====")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
