"""Node-RED 端到端验证：UA 事件 -> 队列 -> 模拟 MES -> LastMesOrderNo 写回。"""
import asyncio
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime

from ua_test_client import create_secure_client

NS_URI = "urn:fde:linqinspect:qc"
WO = "WO-NRED-" + datetime.now().strftime("%Y%m%d%H%M%S")
NODERED_BASE = os.getenv("NODERED_BASE", "http://127.0.0.1:1880").rstrip("/")
NODERED_HTTP_AUTH = os.getenv("NODERED_HTTP_AUTH", "").strip()


def ok(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'} | {name} {detail}")
    return bool(cond)


class EventCollector:
    def __init__(self, type_ids=None):
        self.type_ids = type_ids or {}
        self.ng_events = []
        self.done_events = []

    def event_notification(self, event):
        event_type = getattr(event, "EventType", None)
        kind = self.type_ids.get(event_type, "")
        if kind == "ng":
            self.ng_events.append(event)
        elif kind == "done":
            self.done_events.append(event)


async def read_val(node):
    try:
        return await node.read_value()
    except Exception:
        return None


def edge_status():
    headers = {"Authorization": NODERED_HTTP_AUTH} if NODERED_HTTP_AUTH else {}
    request = urllib.request.Request(NODERED_BASE + "/qc/status", headers=headers)
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def mes_post(path, payload):
    headers = {
        "Content-Type": "application/json",
        "X-Correlation-Id": payload["correlationId"],
    }
    if NODERED_HTTP_AUTH:
        headers["Authorization"] = NODERED_HTTP_AUTH
    request = urllib.request.Request(
        NODERED_BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        data = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(data)
        except json.JSONDecodeError:
            body = {"raw": data}
        return exc.code, body


async def wait_for_drained_edge(timeout=30):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            status = edge_status()
            queue = status.get("queue", {})
            if queue.get("total", 0) == 0 and queue.get("sending", 0) == 0:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    print("WARN | Node-RED queue did not drain before E2E baseline")
    return False


async def main():
    results = []
    client = await create_secure_client()
    async with client:
        idx = await client.get_namespace_index(NS_URI)
        root = await client.nodes.objects.get_child([f"{idx}:QCStation"])
        status = await root.get_child([f"{idx}:DeviceStatus"])
        cur_wo = await root.get_child([f"{idx}:CurrentWorkOrderId"])
        last_mes = await root.get_child([f"{idx}:LastMesOrderNo"])
        last_ng_mes = await root.get_child([f"{idx}:LastNgMesOrderNo"])
        last_completion_rc = await root.get_child([f"{idx}:LastCompletionReceiptNo"])
        stats = await root.get_child([f"{idx}:Statistics"])
        stats_ng = await stats.get_child([f"{idx}:NgCount"])
        stats_all = await stats.get_child([f"{idx}:InspectCount"])
        events_node = await root.get_child([f"{idx}:Events"])

        st = await read_val(status)
        if st == 1:
            wo0 = str(await read_val(cur_wo) or "")
            print("INFO | server is Running with", wo0, "-> completing before test")
            await root.call_method(f"{idx}:CompleteWorkOrder", wo0)
            for _ in range(20):
                if await read_val(status) == 0:
                    break
                await asyncio.sleep(0.25)

        await wait_for_drained_edge()
        initial_mes = str(await read_val(last_mes) or "")
        print("INFO | initial LastMesOrderNo =", initial_mes or "(empty)")

        type_ids = {}
        for node in await client.nodes.base_event_type.get_children():
            try:
                browse_name = await node.read_browse_name()
            except Exception:
                continue
            if browse_name.Name == "NgDetectedEventType":
                type_ids[node.nodeid] = "ng"
            elif browse_name.Name == "WorkOrderCompletedEventType":
                type_ids[node.nodeid] = "done"
        collector = EventCollector(type_ids)
        subscription = await client.create_subscription(200, collector)
        await subscription.subscribe_events(events_node, list(type_ids.keys()))
        results.append(ok("correlation event filters found", len(type_ids) == 2, f"(found {len(type_ids)})"))

        ret = await root.call_method(f"{idx}:StartInspection", WO, 5)
        results.append(ok("StartInspection accepted", ret and ret[0] is True, f"({ret[1] if len(ret) > 1 else ''})"))
        results.append(ok("DeviceStatus Running", await read_val(status) == 1))

        mes_no = ""
        ng_before = int(await read_val(stats_ng) or 0)
        ng_after = ng_before
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            mes_no = str(await read_val(last_mes) or "")
            ng_after = int(await read_val(stats_ng) or 0)
            if (
                mes_no
                and mes_no != initial_mes
                and mes_no.startswith("MES-")
                and ng_after > ng_before
            ):
                break
            await asyncio.sleep(0.5)

        results.append(ok("NG order receipt appears in LastMesOrderNo",
                          mes_no != initial_mes and bool(re.match(r"^MES-\d{8}-\d{4}$", mes_no)),
                          f"(got {mes_no or 'empty'}, initial {initial_mes or 'empty'})"))
        results.append(ok("NG order receipt also lands in LastNgMesOrderNo",
                          str(await read_val(last_ng_mes) or "") == mes_no))
        results.append(ok("CurrentWorkOrderId not clobbered", str(await read_val(cur_wo) or "") == WO))
        all_after = int(await read_val(stats_all) or 0)
        results.append(ok("server produced NG samples", ng_after > ng_before, f"(ng={ng_after}, total={all_after})"))
        edge = edge_status()
        last_request = edge.get("lastRequest") or {}
        ng_correlations = {str(getattr(event, "CorrelationId", "")) for event in collector.ng_events}
        results.append(ok("MES request uses schemaVersion 1.0", last_request.get("schemaVersion") == "1.0",
                          f"(got {last_request.get('schemaVersion') or 'empty'})"))
        results.append(ok("UA NG CorrelationId reaches Node-RED MES request",
                          bool(last_request.get("correlationId")) and last_request.get("correlationId") in ng_correlations,
                          f"(request={last_request.get('correlationId') or 'empty'}, events={len(ng_correlations)})"))
        last_response = edge.get("lastResponse") or {}
        results.append(ok("MES response preserves CorrelationId",
                          last_response.get("status") == "SUCCESS" and
                          last_response.get("correlationId") == last_request.get("correlationId"),
                          f"(response={last_response.get('correlationId') or 'empty'})"))

        ret2 = await root.call_method(f"{idx}:CompleteWorkOrder", WO)
        results.append(ok("CompleteWorkOrder accepted", ret2 and ret2[0] is True, f"({ret2[1] if len(ret2) > 1 else ''})"))
        results.append(ok("DeviceStatus Idle", await read_val(status) == 0))

        receipt = ""
        after_ng = mes_no
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            receipt = str(await read_val(last_mes) or "")
            if receipt.startswith("RC-") and receipt != after_ng:
                break
            await asyncio.sleep(0.5)
        results.append(ok("completion receipt appears in LastMesOrderNo",
                          receipt != after_ng and bool(re.match(r"^RC-\d{8}-\d{4}$", receipt)),
                          f"(got {receipt or 'empty'}, after NG {after_ng or 'empty'})"))
        results.append(ok("completion receipt also lands in LastCompletionReceiptNo",
                          str(await read_val(last_completion_rc) or "") == receipt))
        edge = edge_status()
        done_correlations = {str(getattr(event, "CorrelationId", "")) for event in collector.done_events}
        completion_request = (edge.get("lastRequestByType") or {}).get("complete") or {}
        results.append(ok("UA completion CorrelationId reaches Node-RED MES request",
                          bool(completion_request.get("correlationId")) and
                          completion_request.get("correlationId") in done_correlations,
                          f"(request={completion_request.get('correlationId') or 'empty'}, events={len(done_correlations)})"))

        suffix = datetime.now().strftime("%Y%m%d%H%M%S%f")
        ng_body = {
            "schemaVersion": "1.0",
            "correlationId": f"corr-mes-idem-ng-{suffix}",
            "workOrderId": f"WO-MES-IDEM-{suffix}",
            "serialNumber": f"SN-MES-IDEM-{suffix}",
            "defectType": "SCRATCH",
            "confidence": 0.88,
            "imageId": f"IMG-MES-IDEM-{suffix}",
            "occurredAt": "2026-11-21T01:10:00.000Z",
        }
        ng_first_status, ng_first = mes_post("/mes/ng-order", ng_body)
        ng_duplicate_status, ng_duplicate = mes_post(
            "/mes/ng-order", {**ng_body, "correlationId": f"corr-mes-idem-ng-dup-{suffix}"}
        )
        ng_conflict_status, ng_conflict = mes_post(
            "/mes/ng-order",
            {**ng_body, "correlationId": f"corr-mes-idem-ng-conflict-{suffix}", "defectType": "DENT"},
        )
        results.append(ok("mock MES NG first request accepted",
                          ng_first_status == 200 and ng_first.get("accepted") is True and
                          bool(str(ng_first.get("mesOrderNo") or "").startswith("MES-")),
                          f"(status={ng_first_status})"))
        results.append(ok("mock MES NG duplicate returns original receipt",
                          ng_duplicate_status == 200 and ng_duplicate.get("duplicated") is True and
                          ng_duplicate.get("mesOrderNo") == ng_first.get("mesOrderNo"),
                          f"(status={ng_duplicate_status})"))
        results.append(ok("mock MES NG payload conflict returns 409",
                          ng_conflict_status == 409 and
                          ng_conflict.get("errorCode") == "IDEMPOTENCY_CONFLICT",
                          f"(status={ng_conflict_status}, code={ng_conflict.get('errorCode') or 'empty'})"))

        done_body = {
            "schemaVersion": "1.0",
            "correlationId": f"corr-mes-idem-done-{suffix}",
            "workOrderId": f"WO-MES-IDEM-DONE-{suffix}",
            "inspectCount": 12,
            "okCount": 9,
            "ngCount": 3,
            "ngRate": 0.25,
        }
        done_first_status, done_first = mes_post("/mes/complete", done_body)
        done_duplicate_status, done_duplicate = mes_post(
            "/mes/complete", {**done_body, "correlationId": f"corr-mes-idem-done-dup-{suffix}"}
        )
        done_conflict_status, done_conflict = mes_post(
            "/mes/complete",
            {**done_body, "correlationId": f"corr-mes-idem-done-conflict-{suffix}", "inspectCount": 13},
        )
        results.append(ok("mock MES completion first request accepted",
                          done_first_status == 200 and done_first.get("accepted") is True and
                          bool(str(done_first.get("receiptNo") or "").startswith("RC-")),
                          f"(status={done_first_status})"))
        results.append(ok("mock MES completion duplicate returns original receipt",
                          done_duplicate_status == 200 and done_duplicate.get("duplicated") is True and
                          done_duplicate.get("receiptNo") == done_first.get("receiptNo"),
                          f"(status={done_duplicate_status})"))
        results.append(ok("mock MES completion payload conflict returns 409",
                          done_conflict_status == 409 and
                          done_conflict.get("errorCode") == "IDEMPOTENCY_CONFLICT",
                          f"(status={done_conflict_status}, code={done_conflict.get('errorCode') or 'empty'})"))

    passed = sum(1 for r in results if r)
    print(f"===== NODERED E2E: {passed}/{len(results)} passed =====")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
