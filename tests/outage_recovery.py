"""Phase 1 断网/订阅恢复与陈旧事件过滤测试。

模式：
  stall           短时 SIGSTOP/SIGCONT 模拟 UA 停顿，验证原连接续推。
  stale           注入 60 秒以前的事件，验证不入队、不改回执。
  prepare_restart 准备 UA Server 重启测试状态。
  verify_restart  Server 重启后验证订阅重建和新工单闭环。
"""
import asyncio
import json
import os
import signal
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

from ua_test_client import create_secure_client

NS_URI = "urn:fde:linqinspect:qc"
NR_BASE = os.getenv("NODERED_BASE", "http://127.0.0.1:1880").rstrip("/")
STATE_PATH = Path(os.getenv("OUTAGE_STATE", "")).resolve() if os.getenv("OUTAGE_STATE") else None
UA_SERVER_PID = int(os.getenv("UA_SERVER_PID", "0") or 0)

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
                         description="DeviceStatus idle")


async def wait_queue_empty(timeout=30):
    return await wait_until(lambda: edge_status()["queue"]["total"] == 0,
                            timeout=timeout, description="queue drain")


async def exercise_work_order(nodes, label):
    await ensure_idle(nodes)
    check(f"{label} MES ok mode", inject("inj_ok") and edge_status()["mesMode"] == "ok")
    work_order = f"WO-{label}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    before_ng = str(await read(nodes["last_ng"]) or "")
    before_rc = str(await read(nodes["last_rc"]) or "")
    ret = await nodes["root"].call_method(f"{nodes['idx']}:StartInspection", work_order, 5)
    check(f"{label} StartInspection accepted", ret and ret[0] is True,
          f"({ret[1] if len(ret) > 1 else ''})")

    async def ng_changed():
        value = str(await read(nodes["last_ng"]) or "")
        return value.startswith("MES-") and value != before_ng

    ng_ok = await wait_until(ng_changed, timeout=35, description=f"{label} NG receipt")
    check(f"{label} NG event reaches MES after recovery", ng_ok,
          f"(before={before_ng or 'empty'}, now={await read(nodes['last_ng']) or 'empty'})")

    ret_done = await nodes["root"].call_method(f"{nodes['idx']}:CompleteWorkOrder", work_order)
    check(f"{label} CompleteWorkOrder accepted", ret_done and ret_done[0] is True,
          f"({ret_done[1] if len(ret_done) > 1 else ''})")

    async def rc_changed():
        value = str(await read(nodes["last_rc"]) or "")
        return value.startswith("RC-") and value != before_rc

    rc_ok = await wait_until(rc_changed, timeout=35, description=f"{label} completion receipt")
    check(f"{label} completion event reaches MES after recovery", rc_ok,
          f"(before={before_rc or 'empty'}, now={await read(nodes['last_rc']) or 'empty'})")
    check(f"{label} DeviceStatus idle after recovery", await read(nodes["status"]) == 0)


async def stall():
    check("short outage server PID available", UA_SERVER_PID > 0, f"(pid={UA_SERVER_PID or 'missing'})")
    if UA_SERVER_PID <= 0:
        return finish("SHORT OUTAGE")

    client = await create_secure_client()
    async with client:
        nodes = await get_nodes(client)
        await ensure_idle(nodes)
        await wait_queue_empty()
        check("short outage starts with empty queue", edge_status()["queue"]["total"] == 0)

        stopped = False
        try:
            os.kill(UA_SERVER_PID, signal.SIGSTOP)
            stopped = True
            print("INFO | UA Server paused for 5s")
            await asyncio.sleep(5)
        finally:
            if stopped:
                os.kill(UA_SERVER_PID, signal.SIGCONT)
                print("INFO | UA Server resumed")
        check("short outage pause/resume completed", True)

    # asyncua 的客户端会话在停顿后会断开；用新会话验证服务恢复，Node-RED 自己负责重连其订阅。
    await asyncio.sleep(5)
    client = await create_secure_client()
    async with client:
        nodes = await get_nodes(client)
        check("short outage UA accepts a fresh secure session", (await read(nodes["status"])) is not None)
        await exercise_work_order(nodes, "STALL")

    return finish("SHORT OUTAGE")


async def stale():
    client = await create_secure_client()
    async with client:
        nodes = await get_nodes(client)
        await ensure_idle(nodes)
        await wait_queue_empty()
        before = edge_status()
        before_ng = str(await read(nodes["last_ng"]) or "")
        before_mes = str(await read(nodes["last_mes"]) or "")
        check("stale event injection accepted", inject("inj_stale_event"))
        await asyncio.sleep(1.5)
        after = edge_status()
        check("stale event does not enter queue",
              after["queue"]["total"] == before["queue"]["total"],
              f"(before={before['queue']['total']}, after={after['queue']['total']})")
        check("stale event does not reach dead letters",
              after["deadLetters"]["total"] == before["deadLetters"]["total"])
        check("stale event does not change dedup",
              after["dedup"] == before["dedup"],
              f"(before={before['dedup']}, after={after['dedup']})")
        check("stale event does not write NG receipt",
              str(await read(nodes["last_ng"]) or "") == before_ng)
        check("stale event does not change LastMesOrderNo",
              str(await read(nodes["last_mes"]) or "") == before_mes)
        check("stale event does not trigger MES request",
              (after.get("lastRequestByType") or {}) == (before.get("lastRequestByType") or {}))

    return finish("STALE FILTER")


async def prepare_restart():
    if STATE_PATH is None:
        raise SystemExit("OUTAGE_STATE is required")
    client = await create_secure_client()
    async with client:
        nodes = await get_nodes(client)
        await ensure_idle(nodes)
        await wait_queue_empty()
        state = {
            "lastNgReceipt": str(await read(nodes["last_ng"]) or ""),
            "lastCompletionReceipt": str(await read(nodes["last_rc"]) or ""),
            "lastMesOrderNo": str(await read(nodes["last_mes"]) or ""),
            "preparedAt": datetime.now().astimezone().isoformat(),
        }
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        check("UA restart state written", STATE_PATH.exists())
        check("UA restart starts idle", await read(nodes["status"]) == 0)
        check("UA restart starts with empty queue", edge_status()["queue"]["total"] == 0)
    return finish("UA RESTART PREPARE")


async def verify_restart():
    if STATE_PATH is None or not STATE_PATH.exists():
        raise SystemExit("OUTAGE_STATE file does not exist")
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    client = await create_secure_client()
    async with client:
        nodes = await get_nodes(client)
        check("UA Server responds after restart", (await read(nodes["status"])) is not None)
        await exercise_work_order(nodes, "UA-RESTART")
        ng_now = str(await read(nodes["last_ng"]) or "")
        rc_now = str(await read(nodes["last_rc"]) or "")
        check("UA restart NG receipt differs from baseline",
              ng_now.startswith("MES-") and ng_now != state["lastNgReceipt"],
              f"(before={state['lastNgReceipt'] or 'empty'}, now={ng_now or 'empty'})")
        check("UA restart completion receipt differs from baseline",
              rc_now.startswith("RC-") and rc_now != state["lastCompletionReceipt"],
              f"(before={state['lastCompletionReceipt'] or 'empty'}, now={rc_now or 'empty'})")
        check("UA restart leaves queue empty", await wait_queue_empty())
        check("UA restart status healthy", edge_status().get("ok") is True)
    return finish("UA RESTART VERIFY")


def finish(label):
    passed = sum(1 for _, ok in results if ok)
    print(f"===== {label}: {passed}/{len(results)} passed =====")
    return 0 if passed == len(results) else 1


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    handlers = {
        "stall": stall,
        "stale": stale,
        "prepare_restart": prepare_restart,
        "verify_restart": verify_restart,
    }
    handler = handlers.get(mode)
    if handler is None:
        raise SystemExit("usage: outage_recovery.py stall|stale|prepare_restart|verify_restart")
    return await handler()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
