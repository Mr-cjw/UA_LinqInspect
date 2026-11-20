"""SIGKILL recovery after MES success but before UA write-back confirmation.

prepare: freeze the UA server, inject one NG and one completion event, then wait
         until MES has accepted the NG item and SQLite holds its UA write-back.
verify:  restart Node-RED with the UA server frozen, resume UA, and assert both
         pending events complete their UA writes without dead letters or loss.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from ua_test_client import create_secure_client

NS_URI = "urn:fde:linqinspect:qc"
NR_BASE = os.getenv("NODERED_BASE", "http://127.0.0.1:1880").rstrip("/")
NODERED_HTTP_AUTH = os.getenv("NODERED_HTTP_AUTH", "").strip()
STATE_PATH = (
    Path(os.getenv("UA_WRITE_CRASH_STATE", "")).resolve()
    if os.getenv("UA_WRITE_CRASH_STATE") else None
)
DB_PATH = Path(os.getenv("QC_DB_PATH", "")).resolve() if os.getenv("QC_DB_PATH") else None
UA_SERVER_PID = int(os.getenv("UA_SERVER_PID", "0") or 0)

results: list[tuple[str, bool]] = []


def check(name: str, cond: object, detail: str = "") -> None:
    results.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'} | {name} {detail}")


def http_request(method: str, path: str, timeout: float = 5) -> tuple[int, str]:
    headers = {"Authorization": NODERED_HTTP_AUTH} if NODERED_HTTP_AUTH else {}
    req = urllib.request.Request(NR_BASE + path, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def inject(mode: str) -> bool:
    status, _ = http_request("POST", f"/lab/mode/{mode}")
    return 200 <= status < 300


def inject_fresh_event(kind: str, work_order: str, serial_number: str = "") -> bool:
    query = {"workOrderId": work_order}
    if serial_number:
        query["serialNumber"] = serial_number
    path = f"/lab/fresh-event/{kind}?" + urllib.parse.urlencode(query)
    status, _ = http_request("POST", path)
    return status == 202


def edge_status() -> dict:
    status, body = http_request("GET", "/qc/status")
    if not 200 <= status < 300:
        raise RuntimeError(f"status endpoint returned HTTP {status}")
    return json.loads(body)


async def read(node):
    try:
        return await node.read_value()
    except Exception:
        return None


async def wait_until(predicate, timeout: float = 30, interval: float = 0.1,
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
        "last_mes": await root.get_child([f"{idx}:LastMesOrderNo"]),
        "last_ng": await root.get_child([f"{idx}:LastNgMesOrderNo"]),
        "last_rc": await root.get_child([f"{idx}:LastCompletionReceiptNo"]),
    }


async def ensure_idle(nodes) -> None:
    if await read(nodes["status"]) != 1:
        return
    current = str(await read(nodes["work_order"]) or "")
    await nodes["root"].call_method(f"{nodes['idx']}:CompleteWorkOrder", current)
    await wait_until(
        lambda: read(nodes["status"]) == 0,
        timeout=10,
        description="DeviceStatus idle before UA write crash seed",
    )


def db_snapshot() -> dict:
    if DB_PATH is None or not DB_PATH.exists():
        return {}
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5) as connection:
        return {
            "queueTotal": int(connection.execute("SELECT COUNT(*) FROM queue").fetchone()[0]),
            "queueSending": int(connection.execute(
                "SELECT COUNT(*) FROM queue WHERE status = 'SENDING'"
            ).fetchone()[0]),
            "queueDistinct": int(connection.execute(
                "SELECT COUNT(DISTINCT id) FROM queue"
            ).fetchone()[0]),
            "deadTotal": int(connection.execute(
                "SELECT COUNT(*) FROM dead_letters"
            ).fetchone()[0]),
            "uaTotal": int(connection.execute(
                "SELECT COUNT(*) FROM ua_writebacks"
            ).fetchone()[0]),
            "uaTargets": int(connection.execute(
                "SELECT COUNT(*) FROM ua_write_targets"
            ).fetchone()[0]),
        }


def first_writeback() -> dict | None:
    if DB_PATH is None or not DB_PATH.exists():
        return None
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT w.queue_id, w.status, w.receipt, w.attempts, w.started_at, "
            "q.type, q.correlation_id, q.seq "
            "FROM ua_writebacks w JOIN queue q ON q.id = w.queue_id "
            "ORDER BY q.seq LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def writeback_targets(queue_id: str) -> list[dict]:
    if DB_PATH is None or not DB_PATH.exists():
        return []
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT target, expected_value, confirmed FROM ua_write_targets "
            "WHERE queue_id = ? ORDER BY target",
            (queue_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def metric_total(name: str, metric_type: str, outcome: str) -> int:
    if DB_PATH is None or not DB_PATH.exists():
        return 0
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5) as connection:
        row = connection.execute(
            "SELECT COALESCE(SUM(value), 0) FROM metrics "
            "WHERE name = ? AND type = ? AND outcome = ? AND error_code = ''",
            (name, metric_type, outcome),
        ).fetchone()
        return int(row[0] or 0)


def dedup_count(kind: str, key: str) -> int:
    if DB_PATH is None or not DB_PATH.exists():
        return 0
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM dedup WHERE kind = ? AND dedup_key = ?",
            (kind, key),
        ).fetchone()
        return int(row[0] or 0)


async def prepare() -> int:
    if STATE_PATH is None:
        raise SystemExit("UA_WRITE_CRASH_STATE is required")
    if DB_PATH is None:
        raise SystemExit("QC_DB_PATH is required")
    if UA_SERVER_PID <= 0:
        raise SystemExit("UA_SERVER_PID is required")

    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    work_order = f"WO-UA-WRITE-CRASH-{stamp}"
    serial_number = f"SN-UA-WRITE-CRASH-{stamp}"
    client = await create_secure_client()
    async with client:
        nodes = await get_nodes(client)
        await ensure_idle(nodes)
        check("ua-write crash seed restores MES ok",
              inject("ok") and edge_status().get("mesMode") == "ok")
        check("ua-write crash seed starts with empty queue",
              await wait_until(lambda: edge_status()["queue"]["total"] == 0,
                               timeout=35, description="queue empty before UA write crash"))
        initial = edge_status()
        initial_ng = str(await read(nodes["last_ng"]) or "")
        initial_rc = str(await read(nodes["last_rc"]) or "")
        initial_mes = str(await read(nodes["last_mes"]) or "")
        baseline_metrics = {
            "ngSuccess": metric_total("ua_write_total", "ng", "success"),
            "completeSuccess": metric_total("ua_write_total", "complete", "success"),
        }

    ua_paused = False
    passed = False
    try:
        os.kill(UA_SERVER_PID, signal.SIGSTOP)
        ua_paused = True
        check("ua-write crash seed freezes UA server", True, f"(pid={UA_SERVER_PID})")

        check("ua-write crash seed injects fresh NG event",
              inject_fresh_event("ng", work_order, serial_number))
        check("ua-write crash seed injects fresh completion event",
              inject_fresh_event("done", work_order))

        queued = await wait_until(
            lambda: (db_snapshot() or {}).get("queueTotal") == 2,
            timeout=15,
            description="NG and completion persisted in queue",
        )
        counts = db_snapshot()
        check("ua-write crash seed queues exactly two events", queued, f"(db={counts})")

        writeback_ready = await wait_until(
            lambda: first_writeback() is not None,
            timeout=25,
            description="MES success persisted as UA write-back",
        )
        writeback = first_writeback() or {}
        receipt = str(writeback.get("receipt") or "")
        queue_id = str(writeback.get("queue_id") or "")
        targets = writeback_targets(queue_id) if queue_id else []
        counts = db_snapshot()
        status = edge_status()
        last_ng_request = (status.get("lastRequestByType") or {}).get("ng") or {}
        check("ua-write crash seed records MES success before UA confirmation", writeback_ready,
              f"(queue_id={queue_id or 'empty'}, receipt={receipt or 'empty'})")
        check("ua-write crash seed keeps queue item until UA confirms",
              counts.get("queueTotal") == 2 and counts.get("queueSending") >= 1,
              f"(db={counts})")
        check("ua-write crash seed holds two UA targets",
              len(targets) == 2 and all(row.get("expected_value") == receipt for row in targets),
              f"(targets={targets})")
        check("ua-write crash seed keeps NG receipt pending",
              receipt.startswith("MES-") and receipt != initial_ng,
              f"(receipt={receipt or 'empty'}, before={initial_ng or 'empty'})")
        check("ua-write crash seed does not add dead letters",
              int(status["deadLetters"]["total"]) == int(initial["deadLetters"]["total"]),
              f"(before={initial['deadLetters']['total']}, now={status['deadLetters']['total']})")

        state = {
            "workOrderId": work_order,
            "serialNumber": serial_number,
            "expectedQueueId": queue_id,
            "expectedReceipt": receipt,
            "expectedTargets": targets,
            "initialNgReceipt": initial_ng,
            "initialCompletionReceipt": initial_rc,
            "initialMesOrderNo": initial_mes,
            "deadTotal": int(initial["deadLetters"]["total"]),
            "baselineMetrics": baseline_metrics,
            "ngRequestAtCrash": last_ng_request,
            "queueTotalAtCrash": int(counts.get("queueTotal") or 0),
            "uaTotalAtCrash": int(counts.get("uaTotal") or 0),
            "preparedAt": datetime.now().astimezone().isoformat(),
        }
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        check("ua-write crash seed state written", STATE_PATH.exists(), f"({STATE_PATH})")
        passed = all(ok for _, ok in results)
    finally:
        if ua_paused and not passed:
            os.kill(UA_SERVER_PID, signal.SIGCONT)
            print("INFO | UA server resumed after failed prepare")

    passed_count = sum(1 for _, ok in results if ok)
    print(f"===== UA WRITE CRASH SEED: {passed_count}/{len(results)} passed =====")
    return 0 if passed_count == len(results) else 1


async def verify() -> int:
    if STATE_PATH is None or not STATE_PATH.exists():
        raise SystemExit("UA_WRITE_CRASH_STATE file does not exist")
    if DB_PATH is None or not DB_PATH.exists():
        raise SystemExit("QC_DB_PATH database does not exist")
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    expected_receipt = str(state["expectedReceipt"])

    before = edge_status()
    counts_before = db_snapshot()
    check("ua-write crash restart status remains healthy", before.get("ok") is True)
    check("ua-write crash restart reloads persisted write-back before UA resumes",
          int(counts_before.get("uaTotal") or 0) >= 1
          and int(counts_before.get("queueTotal") or 0) >= 1,
          f"(db={counts_before})")

    if UA_SERVER_PID > 0:
        os.kill(UA_SERVER_PID, signal.SIGCONT)
    check("ua-write crash restart resumes UA server", True, f"(pid={UA_SERVER_PID})")

    client = await create_secure_client()
    async with client:
        nodes = await get_nodes(client)
        check("ua-write crash restart restores MES ok",
              inject("ok") and edge_status().get("mesMode") == "ok")
        drained = await wait_until(
            lambda: edge_status()["queue"]["total"] == 0
                    and edge_status()["uaWriteback"]["pending"] == 0
                    and edge_status()["uaWriteback"]["sending"] == 0,
            timeout=75,
            description="UA write-backs drain after SIGKILL restart",
        )
        check("ua-write crash restart drains queue and write-backs", drained)

        async def ng_written():
            return str(await read(nodes["last_ng"]) or "") == expected_receipt

        ng_ok = await wait_until(
            ng_written,
            timeout=20,
            description="NG receipt confirmed after restart",
        )
        check("ua-write crash restart writes the pre-crash NG receipt", ng_ok,
              f"(expected={expected_receipt or 'empty'}, now={await read(nodes['last_ng']) or 'empty'})")

        completion_value = ""

        async def completion_written():
            nonlocal completion_value
            completion_value = str(await read(nodes["last_rc"]) or "")
            return (completion_value.startswith("RC-")
                    and completion_value != state["initialCompletionReceipt"])

        completion_ok = await wait_until(
            completion_written,
            timeout=20,
            description="completion receipt confirmed after restart",
        )
        check("ua-write crash restart writes a new completion receipt", completion_ok,
              f"(before={state['initialCompletionReceipt'] or 'empty'}, now={completion_value or 'empty'})")

        last_mes = str(await read(nodes["last_mes"]) or "")
        check("ua-write crash restart mirrors completion receipt to LastMesOrderNo",
              bool(completion_value) and last_mes == completion_value,
              f"(lastMes={last_mes or 'empty'}, completion={completion_value or 'empty'})")

        final = edge_status()
        counts = db_snapshot()
        check("ua-write crash restart leaves no dead letters",
              int(final["deadLetters"]["total"]) == int(state["deadTotal"]),
              f"(before={state['deadTotal']}, now={final['deadLetters']['total']})")
        check("ua-write crash restart clears SQLite queue and write-back tables",
              counts.get("queueTotal") == 0 and counts.get("uaTotal") == 0
              and counts.get("uaTargets") == 0,
              f"(db={counts})")
        check("ua-write crash restart keeps one NG serial dedup key",
              dedup_count("ng_sn", state["serialNumber"]) == 1,
              f"(serial={state['serialNumber']})")
        check("ua-write crash restart keeps one completion dedup key",
              dedup_count("done", state["workOrderId"]) == 1,
              f"(workOrder={state['workOrderId']})")

        baseline = state["baselineMetrics"]
        ng_success = metric_total("ua_write_total", "ng", "success")
        complete_success = metric_total("ua_write_total", "complete", "success")
        check("ua-write crash restart confirms NG write-back exactly once",
              ng_success == int(baseline["ngSuccess"]) + 1,
              f"(before={baseline['ngSuccess']}, now={ng_success})")
        check("ua-write crash restart confirms completion write-back exactly once",
              complete_success == int(baseline["completeSuccess"]) + 1,
              f"(before={baseline['completeSuccess']}, now={complete_success})")
        check("ua-write crash restart clears CurrentWorkOrderId",
              str(await read(nodes["work_order"]) or "") == "")

    passed = sum(1 for _, ok in results if ok)
    print(f"===== UA WRITE CRASH VERIFY: {passed}/{len(results)} passed =====")
    return 0 if passed == len(results) else 1


async def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "prepare":
        return await prepare()
    if mode == "verify":
        return await verify()
    raise SystemExit("usage: ua_write_crash_recovery.py prepare|verify")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
