"""M2/M3 故障场景验证：5xx 死信、timeout 退避恢复、Method 拒绝。

前置条件：
  - OPC UA Server 使用 QC_AUTO_INSPECT=1 启动，且 QC_NG_PROBABILITY 足够高
  - Node-RED 已加载最新 nodered/flows.json，并启用本地文件 context
  - Node-RED 暴露 GET /qc/status

运行示例：
  UA_ENDPOINT_PORT=4840 .venv/bin/python tests/fault_scenarios.py
"""
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime

from asyncua import Client

PORT = int(os.getenv("UA_ENDPOINT_PORT", "4840"))
UA_URL = f"opc.tcp://localhost:{PORT}"
NR_BASE = os.getenv("NODERED_BASE", "http://127.0.0.1:1880").rstrip("/")
NS_URI = "urn:fde:linqinspect:qc"

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'} | {name} {detail}")


def http_request(method, path, payload=None, timeout=5):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["content-type"] = "application/json"
    req = urllib.request.Request(NR_BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return resp.status, body


def inject(node_id):
    status, _ = http_request("POST", f"/inject/{node_id}")
    return 200 <= status < 300


def edge_status():
    status, body = http_request("GET", "/qc/status")
    if not 200 <= status < 300:
        raise RuntimeError(f"status endpoint returned HTTP {status}")
    return json.loads(body)


def wait_until(predicate, timeout=35, interval=0.5, description="condition"):
    async def _wait():
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
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

    return _wait()


async def read(node):
    return await node.read_value()


async def main():
    suffix = datetime.now().strftime("%Y%m%d%H%M%S")
    wo_5xx = f"WO-FAULT-5XX-{suffix}"
    wo_timeout = f"WO-FAULT-TO-{suffix}"

    async with Client(UA_URL) as client:
        idx = await client.get_namespace_index(NS_URI)
        root = await client.nodes.objects.get_child([f"{idx}:QCStation"])
        status_node = await root.get_child([f"{idx}:DeviceStatus"])
        current_wo = await root.get_child([f"{idx}:CurrentWorkOrderId"])
        last_mes = await root.get_child([f"{idx}:LastMesOrderNo"])
        last_ng_mes = await root.get_child([f"{idx}:LastNgMesOrderNo"])
        last_rc = await root.get_child([f"{idx}:LastCompletionReceiptNo"])
        stats = await root.get_child([f"{idx}:Statistics"])
        ng_count = await stats.get_child([f"{idx}:NgCount"])

        current_state = await read(status_node)
        if current_state == 1:
            old_wo = str(await read(current_wo) or "")
            print("INFO | server is Running, completing", old_wo)
            await root.call_method(f"{idx}:CompleteWorkOrder", old_wo)
            for _ in range(20):
                if await read(status_node) == 0:
                    break
                await asyncio.sleep(0.25)

        baseline_status = edge_status()
        baseline_dead = int(baseline_status["deadLetters"]["total"])
        initial_ng_mes = str(await read(last_ng_mes) or "")
        initial_rc = str(await read(last_rc) or "")
        print("INFO | edge status:", json.dumps(baseline_status, ensure_ascii=False))

        # ---- 场景 A：MES 5xx -> 重试耗尽 -> 死信 ----
        check("inject 5xx mode", inject("inj_5xx"))
        check("edge reports 5xx mode", edge_status()["mesMode"] == "5xx")
        ret = await root.call_method(f"{idx}:StartInspection", wo_5xx, 5)
        check("5xx scenario StartInspection accepted", ret and ret[0] is True,
              f"({ret[1] if len(ret) > 1 else ''})")

        dead_raised = await wait_until(
            lambda: edge_status()["deadLetters"]["total"] > baseline_dead,
            timeout=35,
            description="5xx dead letter",
        )
        check("5xx request retries then enters dead letter", dead_raised)
        check("5xx NG receipt not written", str(await read(last_ng_mes) or "") == initial_ng_mes)

        # 恢复 MES，然后完工，确认报工链路仍可用
        check("restore MES ok mode", inject("inj_ok"))
        ret_done = await root.call_method(f"{idx}:CompleteWorkOrder", wo_5xx)
        check("5xx scenario CompleteWorkOrder accepted", ret_done and ret_done[0] is True,
              f"({ret_done[1] if len(ret_done) > 1 else ''})")
        receipt_after_5xx = ""
        for _ in range(80):
            receipt_after_5xx = str(await read(last_rc) or "")
            if receipt_after_5xx.startswith("RC-") and receipt_after_5xx != initial_rc:
                break
            await asyncio.sleep(0.5)
        check("completion chain recovers after 5xx", bool(receipt_after_5xx),
              f"(receipt={receipt_after_5xx or 'empty'})")

        # ---- 场景 B：MES timeout -> 10s 后恢复 -> 退避成功 ----
        before_timeout_ng_mes = str(await read(last_ng_mes) or "")
        before_timeout_rc = str(await read(last_rc) or "")
        check("inject timeout mode", inject("inj_to"))
        check("edge reports timeout mode", edge_status()["mesMode"] == "timeout")
        ret2 = await root.call_method(f"{idx}:StartInspection", wo_timeout, 5)
        check("timeout scenario StartInspection accepted", ret2 and ret2[0] is True,
              f"({ret2[1] if len(ret2) > 1 else ''})")

        receipt_seen = False
        for _ in range(90):
            value = str(await read(last_ng_mes) or "")
            if value.startswith("MES-") and value != before_timeout_ng_mes:
                receipt_seen = True
                break
            await asyncio.sleep(0.5)
        check("timeout request eventually succeeds after backoff", receipt_seen)
        check("legacy LastMesOrderNo mirrors current NG receipt",
              str(await read(last_mes) or "") == str(await read(last_ng_mes) or ""))

        # ---- 场景 C：Running 时不同工单开检必须拒绝 ----
        ret3 = await root.call_method(f"{idx}:StartInspection", "WO-CONFLICT-" + suffix, 5)
        check("Method rejects conflicting work order while Running", ret3 and ret3[0] is False,
              f"({ret3[1] if len(ret3) > 1 else ''})")
        check("rejected Method does not overwrite CurrentWorkOrderId",
              str(await read(current_wo) or "") == wo_timeout)

        ret_done2 = await root.call_method(f"{idx}:CompleteWorkOrder", wo_timeout)
        check("timeout scenario CompleteWorkOrder accepted", ret_done2 and ret_done2[0] is True,
              f"({ret_done2[1] if len(ret_done2) > 1 else ''})")

        receipt_after_timeout = ""
        for _ in range(80):
            receipt_after_timeout = str(await read(last_rc) or "")
            if receipt_after_timeout.startswith("RC-") and receipt_after_timeout != before_timeout_rc:
                break
            await asyncio.sleep(0.5)
        check("completion receipt written after timeout scenario",
              bool(receipt_after_timeout) and receipt_after_timeout != before_timeout_rc,
              f"(receipt={receipt_after_timeout or 'empty'})")

        check("restore ok mode at end", inject("inj_ok"))
        final_status = edge_status()
        check("edge status remains healthy", final_status.get("ok") is True)
        print("INFO | final edge status:", json.dumps(final_status, ensure_ascii=False))

    failed = [name for name, ok in results if not ok]
    print(f"===== FAULT SCENARIOS: {len(results) - len(failed)}/{len(results)} passed =====")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
