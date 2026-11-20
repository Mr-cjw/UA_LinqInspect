"""Short soak: repeated work orders without queue, receipt or health drift."""
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
SOAK_CYCLES = max(1, int(os.getenv("SOAK_CYCLES", "6")))
CYCLE_BUDGET_SEC = float(os.getenv("SOAK_CYCLE_BUDGET", "12"))

results: list[tuple[str, bool]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'} | {name} {detail}")


def http_request(method: str, path: str, timeout: float = 8) -> tuple[int, str]:
    headers = {"Authorization": NODERED_HTTP_AUTH} if NODERED_HTTP_AUTH else {}
    request = urllib.request.Request(NR_BASE + path, headers=headers, method=method)
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


async def read(node):
    try:
        return await node.read_value()
    except Exception:
        return None


async def wait_until(predicate, timeout: float = 20, interval: float = 0.2,
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


async def ensure_idle(nodes) -> bool:
    if await read(nodes["status"]) != 1:
        return True
    current = str(await read(nodes["work_order"]) or "")
    await nodes["root"].call_method(f"{nodes['idx']}:CompleteWorkOrder", current)
    return await wait_until(
        lambda: read(nodes["status"]) == 0,
        timeout=15,
        description="DeviceStatus idle before soak",
    )


async def main() -> int:
    baseline = edge_status()
    dead_before = int(baseline["deadLetters"]["total"])
    check("soak starts with empty queue", baseline["queue"]["total"] == 0,
          f"(queue={baseline['queue']['total']})")
    check("soak starts in MES ok mode", set_mode("ok") and edge_status()["mesMode"] == "ok")

    suffix = datetime.now().strftime("%Y%m%d%H%M%S")
    receipts: set[str] = set()
    cycle_durations: list[float] = []
    client = await create_secure_client()
    async with client:
        idx = await client.get_namespace_index(NS_URI)
        root = await client.nodes.objects.get_child([f"{idx}:QCStation"])
        nodes = {
            "idx": idx,
            "root": root,
            "status": await root.get_child([f"{idx}:DeviceStatus"]),
            "work_order": await root.get_child([f"{idx}:CurrentWorkOrderId"]),
            "completion_receipt": await root.get_child([f"{idx}:LastCompletionReceiptNo"]),
        }
        check("soak prepares idle station", await ensure_idle(nodes))

        for cycle in range(1, SOAK_CYCLES + 1):
            started = time.monotonic()
            work_order = f"WO-SOAK-{suffix}-{cycle:03d}"
            before_receipt = str(await read(nodes["completion_receipt"]) or "")
            ret = await root.call_method(
                f"{idx}:StartInspection", work_order, 1
            )
            check(f"soak cycle {cycle} StartInspection accepted",
                  ret and ret[0] is True,
                  f"({ret[1] if len(ret) > 1 else ''})")

            await asyncio.sleep(0.8)
            ret_done = await root.call_method(f"{idx}:CompleteWorkOrder", work_order)
            queue_drained = await wait_until(
                lambda: edge_status()["queue"]["total"] == 0,
                timeout=20,
                description=f"soak cycle {cycle} queue drain",
            )
            receipt = ""

            async def receipt_changed() -> bool:
                nonlocal receipt
                receipt = str(await read(nodes["completion_receipt"]) or "")
                return receipt.startswith("RC-") and receipt != before_receipt

            receipt_ok = await wait_until(
                receipt_changed,
                timeout=10,
                description=f"soak cycle {cycle} completion receipt",
            )
            elapsed = time.monotonic() - started
            cycle_durations.append(elapsed)
            unique_receipt = receipt_ok and receipt not in receipts
            if unique_receipt:
                receipts.add(receipt)
            check(f"soak cycle {cycle} completes cleanly",
                  bool(ret_done and ret_done[0] is True) and queue_drained and
                  receipt_ok and unique_receipt,
                  f"(receipt={receipt or 'empty'}, elapsed={elapsed:.2f}s)")

    final_drained = await wait_until(
        lambda: edge_status()["queue"]["total"] == 0,
        timeout=20,
        description="final soak queue drain",
    )
    final = edge_status()
    check("soak leaves queue empty", final_drained and final["queue"]["total"] == 0,
          f"(queue={final['queue']['total']})")
    check("soak adds no dead letters",
          int(final["deadLetters"]["total"]) == dead_before,
          f"(dead={final['deadLetters']['total']}, baseline={dead_before})")
    check("soak produced unique completion receipts",
          len(receipts) == SOAK_CYCLES,
          f"({len(receipts)}/{SOAK_CYCLES})")
    check("soak leaves edge healthy", final.get("ok") is True)
    max_cycle = max(cycle_durations) if cycle_durations else 0.0
    check("every soak cycle meets latency budget", max_cycle <= CYCLE_BUDGET_SEC,
          f"(max={max_cycle:.2f}s <= {CYCLE_BUDGET_SEC:.0f}s)")

    passed = sum(1 for _, ok in results if ok)
    print(f"===== SOAK: {passed}/{len(results)} passed =====")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
