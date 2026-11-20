#!/usr/bin/env python3
"""Verify pending UA events survive SIGKILL, replay with stable IDs, and ACK once."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
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


class EventCollector:
    def __init__(self, event_type):
        self.event_type = event_type
        self.events = []

    def event_notification(self, event):
        if getattr(event, "EventType", None) == self.event_type:
            self.events.append(event)


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


def seed_state(path: Path, work_order: str, event_id: str, correlation_id: str) -> None:
    state = {
        "schemaVersion": 1,
        "status": 1,
        "workOrderId": work_order,
        "targetCount": 5,
        "inspectCount": 1,
        "okCount": 0,
        "ngCount": 1,
        "window": [False],
        "currentPartSn": "SN-OUTBOX-CRASH-001",
        "snDay": "20261121",
        "snSeq": 1,
        "lastResult": False,
        "lastDefect": "SCRATCH",
        "lastConfidence": 0.93,
        "lastImageId": "IMG-OUTBOX-CRASH-001",
        "lastTimestamp": "2026-11-21T00:00:00.000+00:00",
        "outbox": [{
            "eventVersion": 1,
            "eventId": event_id,
            "eventType": "ng",
            "fields": {
                "WorkOrderId": work_order,
                "SerialNumber": "SN-OUTBOX-CRASH-001",
                "DefectType": "SCRATCH",
                "Confidence": 0.93,
                "ImageId": "IMG-OUTBOX-CRASH-001",
                "CorrelationId": correlation_id,
                "SourceEventId": event_id,
                "EventVersion": 1,
            },
            "severity": 3,
            "message": "seeded NG pending event",
            "createdAt": "2026-11-21T00:00:00.000+00:00",
        }],
    }
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def main() -> int:
    for required in (
        REPO_ROOT / "certs/server/server_cert.der",
        REPO_ROOT / "certs/server/server_key.pem",
        REPO_ROOT / "certs/trusted_clients",
    ):
        if not required.exists():
            raise SystemExit(f"Missing test prerequisite: {required}")

    port = free_port()
    work_order = f"WO-OUTBOX-CRASH-{int(time.time())}"
    event_id = "evt-outbox-crash-001"
    correlation_id = "corr-outbox-crash-001"
    with tempfile.TemporaryDirectory(prefix="qc-outbox-recovery-") as temp:
        root = Path(temp)
        state_path = root / "qc-state.json"
        seed_state(state_path, work_order, event_id, correlation_id)

        env = os.environ.copy()
        admin_user = env.get("UA_ADMIN_USER") or "outbox-recovery-admin"
        admin_pass = env.get("UA_ADMIN_PASS") or "outbox-recovery-password"
        env.update({
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
            "QC_STATE_PATH": str(state_path),
            "QC_AUTO_INSPECT": "0",
            "QC_OUTBOX_REPLAY_INTERVAL": "0.2",
        })
        os.environ.update({"UA_ENDPOINT": env["UA_ENDPOINT"], "UA_ADMIN_USER": admin_user, "UA_ADMIN_PASS": admin_pass})

        first: subprocess.Popen | None = None
        second: subprocess.Popen | None = None
        try:
            first = start_server(env, root / "server-first.log")
            time.sleep(0.6)
            before_crash = json.loads(state_path.read_text(encoding="utf-8"))
            check("unacknowledged event stays in snapshot", len(before_crash.get("outbox", [])) == 1)
            check("replay does not invent a new SourceEventId",
                  before_crash["outbox"][0].get("eventId") == event_id)

            stop_server(first, crash=True)
            first = None
            after_crash = json.loads(state_path.read_text(encoding="utf-8"))
            check("SIGKILL preserves pending outbox event", len(after_crash.get("outbox", [])) == 1)

            second = start_server(env, root / "server-second.log")
            client = await create_secure_client()
            async with client:
                idx = await client.get_namespace_index(NS_URI)
                root_node = await client.nodes.objects.get_child([f"{idx}:QCStation"])
                events_node = await root_node.get_child([f"{idx}:Events"])
                ng_type = None
                for node in await client.nodes.base_event_type.get_children():
                    browse_name = await node.read_browse_name()
                    if browse_name.Name == "NgDetectedEventType":
                        ng_type = node.nodeid
                        break
                check("NgDetectedEventType exists", ng_type is not None)

                collector = EventCollector(ng_type)
                subscription = await client.create_subscription(50, collector)
                await subscription.subscribe_events(events_node, [ng_type])

                replayed = await wait_until(lambda: len(collector.events) >= 2, timeout=10,
                                            label="pending event replay after SIGKILL")
                check("pending event replays after SIGKILL", replayed,
                      f"(received={len(collector.events)})")
                if collector.events:
                    first_event = collector.events[0]
                    check("replayed event keeps SourceEventId",
                          str(getattr(first_event, "SourceEventId", "")) == event_id)
                    check("replayed event keeps CorrelationId",
                          str(getattr(first_event, "CorrelationId", "")) == correlation_id)
                    check("replayed event keeps work order and SN",
                          str(getattr(first_event, "WorkOrderId", "")) == work_order and
                          str(getattr(first_event, "SerialNumber", "")) == "SN-OUTBOX-CRASH-001")
                    stable_ids = {str(getattr(event, "SourceEventId", "")) for event in collector.events}
                    check("repeated delivery uses one stable SourceEventId", stable_ids == {event_id},
                          f"(ids={sorted(stable_ids)})")

                ack = await root_node.call_method(f"{idx}:AcknowledgeEvent", event_id)
                check("AcknowledgeEvent accepted", bool(ack and ack[0] is True),
                      f"({ack[1] if ack and len(ack) > 1 else ''})")

                async def acked_from_disk():
                    current = json.loads(state_path.read_text(encoding="utf-8"))
                    return current.get("outbox") == []

                check("ACK durably removes pending event", await wait_until(acked_from_disk, timeout=8,
                                                                         label="ACK persisted"))
                ack_again = await root_node.call_method(f"{idx}:AcknowledgeEvent", event_id)
                check("duplicate ACK is idempotent", bool(ack_again and ack_again[0] is True))
                await asyncio.sleep(0.4)
                collector.events.clear()
                await asyncio.sleep(0.8)
                check("acknowledged event is no longer replayed", collector.events == [],
                      f"(unexpected={len(collector.events)})")
        finally:
            stop_server(second)
            stop_server(first, crash=True)

    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"===== OUTBOX RECOVERY: {passed}/{len(RESULTS)} passed =====")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
