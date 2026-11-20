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

from ua_test_client import create_secure_client

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


async def wait_dead_letter_after(previous_total, timeout=35):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        status = edge_status()
        dead = status.get("deadLetters", {})
        if int(dead.get("total") or 0) > previous_total:
            return dead.get("last") or {}
        await asyncio.sleep(0.25)
    return None


async def wait_queue_empty(timeout=25):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        queue = edge_status().get("queue", {})
        if int(queue.get("total") or 0) == 0:
            return True
        await asyncio.sleep(0.25)
    return False


async def wait_for_new_ng_request(previous, timeout=15):
    previous = previous or {}
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        current = (edge_status().get("lastRequestByType") or {}).get("ng") or {}
        if current and (current.get("id") != previous.get("id") or
                        current.get("at") != previous.get("at")):
            return current
        await asyncio.sleep(0.1)
    return None


async def main():
    suffix = datetime.now().strftime("%Y%m%d%H%M%S")
    wo_5xx = f"WO-FAULT-5XX-{suffix}"
    wo_timeout = f"WO-FAULT-TO-{suffix}"
    wo_4xx = f"WO-FAULT-4XX-{suffix}"
    wo_429 = f"WO-FAULT-429-{suffix}"
    wo_invalid = f"WO-FAULT-INVALID-{suffix}"

    client = await create_secure_client()
    async with client:
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

        # ---- 场景 D：MES 4xx -> 业务拒绝，立即死信不重试 ----
        check("drain queue before 4xx scenario", await wait_queue_empty())
        dead_before_4xx = int(edge_status()["deadLetters"]["total"])
        ng_mes_before_4xx = str(await read(last_ng_mes) or "")
        check("inject 4xx mode", inject("inj_4xx"))
        check("edge reports 4xx mode", edge_status()["mesMode"] == "4xx")
        ret4 = await root.call_method(f"{idx}:StartInspection", wo_4xx, 5)
        check("4xx scenario StartInspection accepted", ret4 and ret4[0] is True,
              f"({ret4[1] if len(ret4) > 1 else ''})")
        dead_4xx = await wait_dead_letter_after(dead_before_4xx)
        check("4xx enters dead letter", dead_4xx is not None)
        check("4xx classified as MES_REJECTED",
              bool(dead_4xx) and dead_4xx.get("errorCode") == "MES_REJECTED",
              f"(errorCode={(dead_4xx or {}).get('errorCode') or 'empty'})")
        check("4xx is terminal without retry",
              bool(dead_4xx) and int(dead_4xx.get("attempts") or 0) == 0,
              f"(attempts={(dead_4xx or {}).get('attempts')})")
        check("4xx NG receipt not written", str(await read(last_ng_mes) or "") == ng_mes_before_4xx)
        check("restore MES after 4xx", inject("inj_ok"))
        ret4_done = await root.call_method(f"{idx}:CompleteWorkOrder", wo_4xx)
        check("4xx scenario CompleteWorkOrder accepted", ret4_done and ret4_done[0] is True,
              f"({ret4_done[1] if len(ret4_done) > 1 else ''})")
        check("4xx queue drained after recovery", await wait_queue_empty())

        # ---- 场景 E：MES 429 + Retry-After -> 限速退避后死信 ----
        status_before_429 = edge_status()
        dead_before_429 = int(status_before_429["deadLetters"]["total"])
        request_before_429 = (status_before_429.get("lastRequestByType") or {}).get("ng") or {}
        ng_mes_before_429 = str(await read(last_ng_mes) or "")
        check("inject 429 mode", inject("inj_429"))
        check("edge reports 429 mode", edge_status()["mesMode"] == "429")
        ret429 = await root.call_method(f"{idx}:StartInspection", wo_429, 5)
        check("429 scenario StartInspection accepted", ret429 and ret429[0] is True,
              f"({ret429[1] if len(ret429) > 1 else ''})")
        first_429_request = await wait_for_new_ng_request(request_before_429)
        check("429 first NG request observed", first_429_request is not None)
        started_429 = asyncio.get_running_loop().time()
        dead_429 = await wait_dead_letter_after(dead_before_429)
        elapsed_429 = asyncio.get_running_loop().time() - started_429
        check("429 enters dead letter", dead_429 is not None)
        check("429 classified as MES_BUSY",
              bool(dead_429) and dead_429.get("errorCode") == "MES_BUSY",
              f"(errorCode={(dead_429 or {}).get('errorCode') or 'empty'})")
        check("429 exhausts four attempts",
              bool(dead_429) and int(dead_429.get("attempts") or 0) >= 4,
              f"(attempts={(dead_429 or {}).get('attempts')})")
        check("429 honors Retry-After: 1s backoff window",
              dead_429 is not None and 2.0 <= elapsed_429 <= 9.0,
              f"(elapsed={elapsed_429:.2f}s)")
        check("429 NG receipt not written", str(await read(last_ng_mes) or "") == ng_mes_before_429)
        check("restore MES after 429", inject("inj_ok"))
        ret429_done = await root.call_method(f"{idx}:CompleteWorkOrder", wo_429)
        check("429 scenario CompleteWorkOrder accepted", ret429_done and ret429_done[0] is True,
              f"({ret429_done[1] if len(ret429_done) > 1 else ''})")
        check("429 queue drained after recovery", await wait_queue_empty())

        # ---- 场景 F：非法 2xx -> 契约校验失败，直接死信 ----
        dead_before_invalid = int(edge_status()["deadLetters"]["total"])
        ng_mes_before_invalid = str(await read(last_ng_mes) or "")
        check("inject invalid2xx mode", inject("inj_invalid2xx"))
        check("edge reports invalid2xx mode", edge_status()["mesMode"] == "invalid2xx")
        ret_invalid = await root.call_method(f"{idx}:StartInspection", wo_invalid, 5)
        check("invalid2xx scenario StartInspection accepted", ret_invalid and ret_invalid[0] is True,
              f"({ret_invalid[1] if len(ret_invalid) > 1 else ''})")
        dead_invalid = await wait_dead_letter_after(dead_before_invalid)
        check("invalid2xx enters dead letter", dead_invalid is not None)
        check("invalid2xx classified as INVALID_RESPONSE_SCHEMA",
              bool(dead_invalid) and dead_invalid.get("errorCode") == "INVALID_RESPONSE_SCHEMA",
              f"(errorCode={(dead_invalid or {}).get('errorCode') or 'empty'})")
        check("invalid2xx is terminal without retry",
              bool(dead_invalid) and int(dead_invalid.get("attempts") or 0) == 0,
              f"(attempts={(dead_invalid or {}).get('attempts')})")
        check("invalid2xx NG receipt not written",
              str(await read(last_ng_mes) or "") == ng_mes_before_invalid)
        check("restore MES after invalid2xx", inject("inj_ok"))
        ret_invalid_done = await root.call_method(f"{idx}:CompleteWorkOrder", wo_invalid)
        check("invalid2xx scenario CompleteWorkOrder accepted",
              ret_invalid_done and ret_invalid_done[0] is True,
              f"({ret_invalid_done[1] if len(ret_invalid_done) > 1 else ''})")
        check("invalid2xx queue drained after recovery", await wait_queue_empty())

        check("restore ok mode at end", inject("inj_ok"))
        final_status = edge_status()
        check("edge status remains healthy", final_status.get("ok") is True)
        print("INFO | final edge status:", json.dumps(final_status, ensure_ascii=False))

    failed = [name for name, ok in results if not ok]
    print(f"===== FAULT SCENARIOS: {len(results) - len(failed)}/{len(results)} passed =====")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
