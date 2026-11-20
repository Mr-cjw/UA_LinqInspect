"""Node-RED SIGKILL recovery test for the SQLite transaction queue.

prepare: queue an NG order while an in-flight MES request is blocked by timeout
         injection, queue the completion event, then leave one item SENDING.
verify:  restart Node-RED with the same SQLite database, restore MES, and assert
         the watchdog resumes the queue without duplicate rows or receipts.
"""
import asyncio
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

from ua_test_client import create_secure_client

NS_URI = "urn:fde:linqinspect:qc"
NR_BASE = os.getenv("NODERED_BASE", "http://127.0.0.1:1880").rstrip("/")
STATE_PATH = Path(os.getenv("CRASH_STATE", "")).resolve() if os.getenv("CRASH_STATE") else None
DB_PATH = Path(os.getenv("QC_DB_PATH", "")).resolve() if os.getenv("QC_DB_PATH") else None

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'} | {name} {detail}")


def http_request(method, path, timeout=5):
    req = urllib.request.Request(NR_BASE + path, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def inject(node_id):
    status, _ = http_request("POST", f"/inject/{node_id}")
    return 200 <= status < 300


def edge_status():
    status, body = http_request("GET", "/qc/status")
    if not 200 <= status < 300:
        raise RuntimeError(f"status endpoint returned HTTP {status}")
    return json.loads(body)


async def read(node):
    try:
        return await node.read_value()
    except Exception:
        return None


async def wait_until(predicate, timeout=30, interval=0.1, description="condition"):
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
        "last_mes": await root.get_child([f"{idx}:LastMesOrderNo"]),
        "last_ng": await root.get_child([f"{idx}:LastNgMesOrderNo"]),
        "last_rc": await root.get_child([f"{idx}:LastCompletionReceiptNo"]),
    }


async def ensure_idle(nodes):
    if await read(nodes["status"]) == 1:
        current = str(await read(nodes["work_order"]) or "")
        await nodes["root"].call_method(f"{nodes['idx']}:CompleteWorkOrder", current)
        await wait_until(
            lambda: read(nodes["status"]) == 0,
            timeout=10,
            description="DeviceStatus idle before crash seed",
        )


def db_counts():
    if DB_PATH is None or not DB_PATH.exists():
        return None
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as connection:
        queue_total = int(connection.execute("SELECT COUNT(*) FROM queue").fetchone()[0])
        queue_sending = int(connection.execute(
            "SELECT COUNT(*) FROM queue WHERE status = 'SENDING'"
        ).fetchone()[0])
        queue_distinct = int(connection.execute(
            "SELECT COUNT(DISTINCT id) FROM queue"
        ).fetchone()[0])
        dead_total = int(connection.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0])
    return {
        "queueTotal": queue_total,
        "queueSending": queue_sending,
        "queueDistinct": queue_distinct,
        "deadTotal": dead_total,
    }


async def prepare():
    if STATE_PATH is None:
        raise SystemExit("CRASH_STATE is required")
    if DB_PATH is None:
        raise SystemExit("QC_DB_PATH is required")
    work_order = "WO-CRASH-" + datetime.now().strftime("%Y%m%d%H%M%S")
    client = await create_secure_client()
    async with client:
        nodes = await get_nodes(client)
        await ensure_idle(nodes)
        check("crash seed restores MES ok", inject("inj_ok") and edge_status()["mesMode"] == "ok")
        drained = await wait_until(
            lambda: edge_status()["queue"]["total"] == 0,
            timeout=35,
            description="queue drained before crash seed",
        )
        check("crash seed starts with empty queue", drained)
        initial = edge_status()
        initial_dedup = initial["dedup"]
        initial_ng = str(await read(nodes["last_ng"]) or "")
        initial_rc = str(await read(nodes["last_rc"]) or "")

        check("crash seed enables timeout mode",
              inject("inj_to") and edge_status()["mesMode"] == "timeout")
        start = await nodes["root"].call_method(f"{nodes['idx']}:StartInspection", work_order, 5)
        check("crash seed StartInspection accepted", start and start[0] is True,
              f"({start[1] if len(start) > 1 else ''})")
        ng_queued = await wait_until(
            lambda: edge_status()["dedup"]["ngSerialNumbers"] > initial_dedup["ngSerialNumbers"],
            timeout=12,
            description="NG dedup key queued before crash",
        )
        check("crash seed queues NG item", ng_queued)

        complete = await nodes["root"].call_method(f"{nodes['idx']}:CompleteWorkOrder", work_order)
        check("crash seed CompleteWorkOrder accepted", complete and complete[0] is True,
              f"({complete[1] if len(complete) > 1 else ''})")
        done_queued = await wait_until(
            lambda: edge_status()["dedup"]["completedWorkOrders"] > initial_dedup["completedWorkOrders"],
            timeout=8,
            description="completion dedup key queued before crash",
        )
        both_queued = done_queued and edge_status()["queue"]["total"] >= 2
        check("crash seed queues NG and completion", both_queued)
        sending = await wait_until(
            lambda: edge_status()["queue"]["sending"] == 1,
            timeout=5,
            description="one queue item is SENDING before crash",
        )
        check("crash seed leaves one request SENDING", sending)

        status = edge_status()
        counts = db_counts() or {}
        state = {
            "workOrderId": work_order,
            "queueTotal": int(status["queue"]["total"]),
            "queueSending": int(status["queue"]["sending"]),
            "deadTotal": int(status["deadLetters"]["total"]),
            "initialDead": int(initial["deadLetters"]["total"]),
            "initialNgReceipt": initial_ng,
            "initialCompletionReceipt": initial_rc,
            "preparedAt": datetime.now().astimezone().isoformat(),
        }
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        check("crash seed state written", STATE_PATH.exists(), f"({STATE_PATH})")
        check("crash seed SQLite queue contains SENDING row",
              counts.get("queueSending") == 1 and counts.get("queueTotal", 0) >= 2,
              f"(db={counts})")
        check("crash seed SQLite queue IDs are unique",
              counts.get("queueDistinct") == counts.get("queueTotal"),
              f"(db={counts})")

    passed = sum(1 for _, ok in results if ok)
    print(f"===== CRASH SEED: {passed}/{len(results)} passed =====")
    return 0 if passed == len(results) else 1


async def verify():
    if STATE_PATH is None or not STATE_PATH.exists():
        raise SystemExit("CRASH_STATE file does not exist")
    if DB_PATH is None or not DB_PATH.exists():
        raise SystemExit("QC_DB_PATH database does not exist")
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    client = await create_secure_client()
    async with client:
        nodes = await get_nodes(client)
        before = edge_status()
        counts_before = db_counts() or {}
        check("crash restart preserves SQLite queue", int(counts_before.get("queueTotal", 0)) >= 2,
              f"(db={counts_before})")
        check("crash restart has no duplicate queue IDs",
              counts_before.get("queueDistinct") == counts_before.get("queueTotal"),
              f"(db={counts_before})")
        check("crash restart status remains healthy", before.get("ok") is True)

        check("crash restart restores MES ok", inject("inj_ok") and edge_status()["mesMode"] == "ok")
        drained = await wait_until(
            lambda: edge_status()["queue"]["total"] == 0,
            timeout=40,
            description="queue drained after SIGKILL restart",
        )
        check("crash restart recovers and drains queue", drained)

        async def ng_recovered():
            value = str(await read(nodes["last_ng"]) or "")
            return value.startswith("MES-") and value != state["initialNgReceipt"]

        ng_ok = await wait_until(
            ng_recovered,
            timeout=20,
            description="NG receipt after SIGKILL restart",
        )
        check("crash restart resumes NG receipt", ng_ok,
              f"(before={state['initialNgReceipt'] or 'empty'}, now={await read(nodes['last_ng']) or 'empty'})")

        async def completion_recovered():
            value = str(await read(nodes["last_rc"]) or "")
            return value.startswith("RC-") and value != state["initialCompletionReceipt"]

        rc_ok = await wait_until(
            completion_recovered,
            timeout=20,
            description="completion receipt after SIGKILL restart",
        )
        check("crash restart resumes completion receipt", rc_ok,
              f"(before={state['initialCompletionReceipt'] or 'empty'}, now={await read(nodes['last_rc']) or 'empty'})")

        final = edge_status()
        counts_after = db_counts() or {}
        check("crash restart does not add dead letters",
              int(final["deadLetters"]["total"]) == int(state["deadTotal"]),
              f"(before={state['deadTotal']}, now={final['deadLetters']['total']})")
        check("crash restart leaves SQLite queue empty", counts_after.get("queueTotal") == 0,
              f"(db={counts_after})")
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as connection:
            dedup_count = int(connection.execute(
                "SELECT COUNT(*) FROM dedup WHERE kind = 'done' AND dedup_key = ?",
                (state["workOrderId"],),
            ).fetchone()[0])
            queue_distinct = int(connection.execute(
                "SELECT COUNT(DISTINCT id) FROM queue"
            ).fetchone()[0])
        check("crash restart keeps single completion dedup row", dedup_count == 1,
              f"(count={dedup_count})")
        check("crash restart leaves no duplicate queue rows", queue_distinct == counts_after.get("queueTotal"),
              f"(db={counts_after})")
        check("crash restart clears CurrentWorkOrderId", str(await read(nodes["work_order"]) or "") == "")

    passed = sum(1 for _, ok in results if ok)
    print(f"===== CRASH VERIFY: {passed}/{len(results)} passed =====")
    return 0 if passed == len(results) else 1


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "prepare":
        return await prepare()
    if mode == "verify":
        return await verify()
    raise SystemExit("usage: crash_recovery.py prepare|verify")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
