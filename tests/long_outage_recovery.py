"""M3 long MES outage: exhaust retries, preserve backlog, recover and replay."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

from ua_test_client import create_secure_client

NS_URI = "urn:fde:linqinspect:qc"
NR_BASE = os.getenv("NODERED_BASE", "http://127.0.0.1:1880").rstrip("/")
NODERED_HTTP_AUTH = os.getenv("NODERED_HTTP_AUTH", "").strip()
OUTAGE_SECONDS = float(os.getenv("LONG_OUTAGE_SECONDS", "12"))
RTO_BUDGET_SEC = float(os.getenv("LONG_OUTAGE_RTO_BUDGET", "30"))

results: list[tuple[str, bool]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'} | {name} {detail}")


def http_request(
    method: str,
    path: str,
    payload: dict | None = None,
    timeout: float = 8,
) -> tuple[int, str]:
    headers = {"Authorization": NODERED_HTTP_AUTH} if NODERED_HTTP_AUTH else {}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(NR_BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def set_mode(mode: str) -> bool:
    status, _ = http_request("POST", f"/lab/mode/{mode}")
    return 200 <= status < 300


def edge_status() -> dict:
    status, body = http_request("GET", "/qc/status")
    if not 200 <= status < 300:
        raise RuntimeError(f"/qc/status returned HTTP {status}")
    return json.loads(body)


def replay(dead_letter_id: str) -> dict:
    status, body = http_request(
        "POST",
        "/qc/dead-letters/replay",
        {"id": dead_letter_id},
    )
    if status != 200:
        raise RuntimeError(f"dead-letter replay returned HTTP {status}: {body}")
    return json.loads(body)


async def read(node):
    try:
        return await node.read_value()
    except Exception:
        return None


async def wait_until(predicate, timeout: float = 30, interval: float = 0.25,
                     description: str = "condition") -> bool:
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
    print(f"TIMEOUT | {description}")
    return False


async def get_nodes(client):
    idx = await client.get_namespace_index(NS_URI)
    root = await client.nodes.objects.get_child([f"{idx}:QCStation"])
    return {
        "idx": idx,
        "root": root,
        "status": await root.get_child([f"{idx}:DeviceStatus"]),
        "work_order": await root.get_child([f"{idx}:CurrentWorkOrderId"]),
    }


async def ensure_idle(nodes) -> bool:
    if await read(nodes["status"]) != 1:
        return True
    current = str(await read(nodes["work_order"]) or "")
    await nodes["root"].call_method(f"{nodes['idx']}:CompleteWorkOrder", current)
    return await wait_until(
        lambda: read(nodes["status"]) == 0,
        timeout=15,
        description="DeviceStatus idle before long outage",
    )


async def wait_queue_empty(timeout: float = 45) -> bool:
    return await wait_until(
        lambda: edge_status()["queue"]["total"] == 0,
        timeout=timeout,
        description="queue drain",
    )


async def main() -> int:
    baseline = edge_status()
    dead_before = int(baseline["deadLetters"]["total"])
    check("long outage starts with empty queue", await wait_queue_empty())
    check("long outage MES baseline is ok", set_mode("ok") and edge_status()["mesMode"] == "ok")

    suffix = datetime.now().strftime("%Y%m%d%H%M%S")
    work_order = f"WO-LONG-OUTAGE-{suffix}"
    completion_accepted = False
    dead_letter: dict = {}
    recovery_started = 0.0
    client = await create_secure_client()
    async with client:
        nodes = await get_nodes(client)
        check("long outage prepares idle station", await ensure_idle(nodes))
        try:
            check("inject sustained MES 5xx", set_mode("5xx"))
            check("edge reports sustained 5xx mode", edge_status()["mesMode"] == "5xx")
            outage_started = time.monotonic()
            ret = await nodes["root"].call_method(
                f"{nodes['idx']}:StartInspection", work_order, 30
            )
            check("long outage StartInspection accepted", ret and ret[0] is True,
                  f"({ret[1] if len(ret) > 1 else ''})")

            backlog_seen = await wait_until(
                lambda: edge_status()["queue"]["total"] >= 3,
                timeout=8,
                description="outage backlog builds",
            )
            check("outage backlog is retained", backlog_seen,
                  f"(queue={edge_status()['queue']['total']})")

            dead_seen = await wait_until(
                lambda: edge_status()["deadLetters"]["total"] > dead_before,
                timeout=25,
                description="retry exhaustion dead letter",
            )
            outage_snapshot = edge_status()
            dead_letter = outage_snapshot["deadLetters"]["last"] or {}
            check("retry exhaustion creates a dead letter", dead_seen)
            check("dead letter is classified as MES_UNAVAILABLE",
                  dead_letter.get("errorCode") == "MES_UNAVAILABLE",
                  f"(errorCode={dead_letter.get('errorCode') or 'empty'})")
            check("dead letter records exhausted attempts",
                  int(dead_letter.get("attempts") or 0) >= 4,
                  f"(attempts={dead_letter.get('attempts')})")
            check("pending backlog survives retry exhaustion",
                  outage_snapshot["queue"]["total"] >= 1,
                  f"(queue={outage_snapshot['queue']['total']})")

            ret_done = await nodes["root"].call_method(
                f"{nodes['idx']}:CompleteWorkOrder", work_order
            )
            completion_accepted = bool(ret_done and ret_done[0] is True)
            check("long outage CompleteWorkOrder accepted", completion_accepted,
                  f"({ret_done[1] if len(ret_done) > 1 else ''})")

            remaining = OUTAGE_SECONDS - (time.monotonic() - outage_started)
            if remaining > 0:
                await asyncio.sleep(remaining)
            check("configured outage duration elapsed", True,
                  f"({time.monotonic() - outage_started:.1f}s)")
        finally:
            recovery_started = time.monotonic()
            restored = set_mode("ok")
            if not completion_accepted and await read(nodes["status"]) == 1:
                current = str(await read(nodes["work_order"]) or work_order)
                await nodes["root"].call_method(f"{nodes['idx']}:CompleteWorkOrder", current)

    check("MES restored after long outage", restored and edge_status()["mesMode"] == "ok")
    drained = await wait_queue_empty(timeout=45)
    rto = time.monotonic() - recovery_started
    check("backlog drains after MES recovery", drained, f"(RTO={rto:.2f}s)")
    check("measured RTO is within budget", rto <= RTO_BUDGET_SEC,
          f"({rto:.2f}s <= {RTO_BUDGET_SEC:.0f}s)")

    recovered = edge_status()
    check("dead letter remains until explicit replay",
          int(recovered["deadLetters"]["total"]) == dead_before + 1,
          f"(dead={recovered['deadLetters']['total']}, baseline={dead_before})")
    check("replay target has a dead-letter id", bool(dead_letter.get("id")),
          f"(id={dead_letter.get('id') or 'empty'})")

    if dead_letter.get("id"):
        replay_result = replay(str(dead_letter["id"]))
        check("dead-letter replay accepted", replay_result.get("ok") is True)
        check("dead-letter replay requeues exactly one item",
              int(replay_result.get("replayed") or 0) == 1,
              f"(replayed={replay_result.get('replayed')})")
    else:
        check("dead-letter replay accepted", False, "(missing id)")
        check("dead-letter replay requeues exactly one item", False, "(missing id)")

    check("replayed dead letter drains", await wait_queue_empty(timeout=20))
    final = edge_status()
    check("replay returns dead-letter count to baseline",
          int(final["deadLetters"]["total"]) == dead_before,
          f"(dead={final['deadLetters']['total']}, baseline={dead_before})")
    check("long outage leaves queue empty", int(final["queue"]["total"]) == 0)
    check("long outage leaves edge healthy", final.get("ok") is True)

    passed = sum(1 for _, ok in results if ok)
    print(f"===== LONG OUTAGE: {passed}/{len(results)} passed =====")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
