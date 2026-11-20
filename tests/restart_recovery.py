"""Node-RED 非空队列重启恢复测试（seed / verify 两阶段）。

seed：制造非空队列并切到 5xx，等待 context 落盘后退出。
verify：Node-RED 用同一 userDir 重启后恢复 MES、检查队列排空和 UA 回执。
"""
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from ua_test_client import create_secure_client

NS_URI = "urn:fde:linqinspect:qc"
NR_BASE = os.getenv("NODERED_BASE", "http://127.0.0.1:1880").rstrip("/")
STATE_PATH = Path(os.getenv("RECOVERY_STATE", "")).resolve() if os.getenv("RECOVERY_STATE") else None

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


async def wait_until(predicate, timeout=30, interval=0.25, description="condition"):
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
        await wait_until(lambda: read(nodes["status"]) == 0, timeout=10,
                         description="DeviceStatus idle before recovery seed")


async def seed():
    if STATE_PATH is None:
        raise SystemExit("RECOVERY_STATE is required")
    work_order = "WO-RESTART-" + datetime.now().strftime("%Y%m%d%H%M%S")
    client = await create_secure_client()
    async with client:
        nodes = await get_nodes(client)
        await ensure_idle(nodes)
        initial_ng = str(await read(nodes["last_ng"]) or "")
        initial_rc = str(await read(nodes["last_rc"]) or "")
        initial_dead = int(edge_status()["deadLetters"]["total"])

        check("seed sets MES 5xx", inject("inj_5xx"))
        check("seed confirms 5xx mode", edge_status()["mesMode"] == "5xx")
        ret = await nodes["root"].call_method(f"{nodes['idx']}:StartInspection", work_order, 5)
        check("seed StartInspection accepted", ret and ret[0] is True,
              f"({ret[1] if len(ret) > 1 else ''})")
        queue_raised = await wait_until(
            lambda: edge_status()["queue"]["total"] > 0,
            timeout=20,
            description="non-empty queue before restart",
        )
        check("seed queue becomes non-empty", queue_raised)

        ret_done = await nodes["root"].call_method(f"{nodes['idx']}:CompleteWorkOrder", work_order)
        check("seed CompleteWorkOrder accepted", ret_done and ret_done[0] is True,
              f"({ret_done[1] if len(ret_done) > 1 else ''})")

        await asyncio.sleep(2.5)
        status = edge_status()
        state = {
            "workOrderId": work_order,
            "queueTotal": int(status["queue"]["total"]),
            "deadTotal": int(status["deadLetters"]["total"]),
            "initialDead": initial_dead,
            "initialNgReceipt": initial_ng,
            "initialCompletionReceipt": initial_rc,
            "seededAt": datetime.now().astimezone().isoformat(),
        }
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        check("seed persisted a non-empty queue", state["queueTotal"] > 0,
              f"(queue={state['queueTotal']}, dead={state['deadTotal']})")
        check("seed state written", STATE_PATH.exists(), f"({STATE_PATH})")

    passed = sum(1 for _, ok in results if ok)
    print(f"===== RESTART SEED: {passed}/{len(results)} passed =====")
    return 0 if passed == len(results) else 1


async def verify():
    if STATE_PATH is None or not STATE_PATH.exists():
        raise SystemExit("RECOVERY_STATE file does not exist")
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    client = await create_secure_client()
    async with client:
        nodes = await get_nodes(client)
        check("verify restores MES ok", inject("inj_ok"))
        check("verify confirms ok mode", edge_status()["mesMode"] == "ok")

        drained = await wait_until(
            lambda: edge_status()["queue"]["total"] == 0,
            timeout=35,
            description="queue drained after Node-RED restart",
        )
        check("verify restores and drains queue", drained)

        async def ng_recovered():
            value = str(await read(nodes["last_ng"]) or "")
            return value.startswith("MES-") and value != state["initialNgReceipt"]

        ng_ok = await wait_until(ng_recovered, timeout=20,
                                 description="NG receipt after restart recovery")
        check("verify NG receipt resumes after restart", ng_ok,
              f"(before={state['initialNgReceipt'] or 'empty'}, now={await read(nodes['last_ng']) or 'empty'})")

        async def completion_recovered():
            value = str(await read(nodes["last_rc"]) or "")
            return value.startswith("RC-") and value != state["initialCompletionReceipt"]

        rc_ok = await wait_until(completion_recovered, timeout=20,
                                 description="completion receipt after restart recovery")
        check("verify completion receipt resumes after restart", rc_ok,
              f"(before={state['initialCompletionReceipt'] or 'empty'}, now={await read(nodes['last_rc']) or 'empty'})")

        final = edge_status()
        check("verify does not add dead letters during recovery",
              int(final["deadLetters"]["total"]) == int(state["deadTotal"]),
              f"(before={state['deadTotal']}, now={final['deadLetters']['total']})")
        check("verify clears CurrentWorkOrderId", str(await read(nodes["work_order"]) or "") == "")
        check("verify status remains healthy", final.get("ok") is True)

    passed = sum(1 for _, ok in results if ok)
    print(f"===== RESTART VERIFY: {passed}/{len(results)} passed =====")
    return 0 if passed == len(results) else 1


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "seed":
        return await seed()
    if mode == "verify":
        return await verify()
    raise SystemExit("usage: restart_recovery.py seed|verify")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
