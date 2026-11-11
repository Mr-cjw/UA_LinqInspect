"""M1/M2 冒烟测试：以 UA Client 视角验证 qc_server 信息模型与业务校验。

覆盖（正向）：
  1) 连通 + 读 DeviceStatus（期望 0=Idle）
  2) StartInspection 绑定工单 → 状态切 Running
  3) 事件订阅（InspectionFinished / NgDetected）+ SN 字段断言
  4) RO 变量随虚拟检测更新；NgRate 空窗口保护语义
  5) 正确工单号完工 → WorkOrderCompleted 事件 + 状态回 Idle
覆盖（负面，验证 Phase0 §2.6 硬约束第 3/5 条）：
  N1) DeviceStatus=Idle 时 CompleteWorkOrder → 拒绝（no active work order）
  N2) 错误 workOrderId 完工 → accepted=False 且原因含 mismatch
  N3) 向 ReadOnly 变量写入 → BadNotWritable / BadUserAccessDenied
"""
import asyncio
import os
import sys

from asyncua import Client, ua

NS_URI = "urn:fde:linqinspect:qc"
PORT = int(os.getenv("UA_ENDPOINT_PORT", "4840"))
WO = "WO-20261108-001"

results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"{'PASS' if cond else 'FAIL'} | {name} {detail}")


class EvHandler:
    def __init__(self, type_ids=None):
        # type_ids: {NodeId: 分类名}，按事件类型的 NodeId 精确匹配
        self.type_ids = type_ids or {}
        self.insp_events = []
        self.ng_events = []
        self.done_events = []

    def event_notification(self, event):
        etid = getattr(event, "EventType", None)
        name = self.type_ids.get(etid, "")
        if name == "insp":
            self.insp_events.append(event)
        elif name == "ng":
            self.ng_events.append(event)
        elif name == "done":
            self.done_events.append(event)
        print(f"  [EVENT] {name or etid} msg={getattr(event, 'Message', '')}")


async def main():
    url = f"opc.tcp://localhost:{PORT}"
    async with Client(url) as client:
        idx = await client.get_namespace_index(NS_URI)
        check("namespace registered", idx == 2, f"(ns={idx})")

        root = await client.nodes.objects.get_child([f"{idx}:QCStation"])
        insp = await root.get_child([f"{idx}:Inspection"])
        stats = await root.get_child([f"{idx}:Statistics"])
        events_node = await root.get_child([f"{idx}:Events"])
        m_start = await root.get_child([f"{idx}:StartInspection"])
        m_done = await root.get_child([f"{idx}:CompleteWorkOrder"])

        # 1) 初始状态
        st0 = await (await root.get_child([f"{idx}:DeviceStatus"])).read_value()
        check("initial DeviceStatus == 0 (Idle)", st0 == 0, f"(got {st0})")

        # N1) 负面用例：Idle 态（无活动工单）完工必须被拒（硬约束 3：先校验后执行）
        ret_idle = await root.call_method(f"{idx}:CompleteWorkOrder", WO)
        acc_idle = ret_idle[0]
        reason_idle = ret_idle[1] if len(ret_idle) > 1 else ""
        check("N1: CompleteWorkOrder while Idle -> rejected", acc_idle is False, f"({reason_idle})")
        check("N1: reject reason mentions no active work order", "no active work order" in reason_idle)

        # 事件订阅：先从 BaseEventType 子类型中按 BrowseName 解析三个自定义事件类型
        type_ids = {}
        for n in await client.nodes.base_event_type.get_children():
            try:
                bn = await n.read_browse_name()
            except Exception:
                continue
            if bn.Name == "InspectionFinishedEventType":
                type_ids[n.nodeid] = "insp"
            elif bn.Name == "NgDetectedEventType":
                type_ids[n.nodeid] = "ng"
            elif bn.Name == "WorkOrderCompletedEventType":
                type_ids[n.nodeid] = "done"
        check("custom event types found under BaseEventType", len(type_ids) == 3, f"(found {len(type_ids)})")

        handler = EvHandler(type_ids)
        sub = await client.create_subscription(200, handler)
        # 关键：必须显式传入自定义事件类型，SelectClauses 才会包含自定义 Property（如 SerialNumber）；
        # 默认 BaseEventType 过滤器只选基类字段，自定义字段会全部为空
        await sub.subscribe_events(events_node, list(type_ids.keys()))

        # 2) 开检
        ret_start = await root.call_method(f"{idx}:StartInspection", WO, 5)
        acc, summ = ret_start[0], ret_start[1] if len(ret_start) > 1 else ""
        check("StartInspection accepted", acc is True, f"({summ})")
        status = await (await root.get_child([f"{idx}:DeviceStatus"])).read_value()
        wo_now = await (await root.get_child([f"{idx}:CurrentWorkOrderId"])).read_value()
        check("DeviceStatus == 1 (Running)", status == 1, f"(got {status})")
        check("CurrentWorkOrderId bound", wo_now == WO, f"(got {wo_now})")

        # 幂等性：Running 态重发同一工单 → 幂等接受（already running），状态不被破坏
        ret_again = await root.call_method(f"{idx}:StartInspection", WO, 5)
        acc_again, summ_again = ret_again[0], ret_again[1] if len(ret_again) > 1 else ""
        check("idempotent re-start same WO -> accepted=True", acc_again is True, f"({summ_again})")
        check("idempotent summary == 'already running'", summ_again == "already running", f"({summ_again})")
        status = await (await root.get_child([f"{idx}:DeviceStatus"])).read_value()
        check("state intact after idempotent call", status == 1, f"(got {status})")
        # Running 态重发不同工单 → 拒绝，当前工单不被覆盖
        ret_diff = await root.call_method(f"{idx}:StartInspection", "WO-OTHER-777", 5)
        acc_diff, summ_diff = ret_diff[0], ret_diff[1] if len(ret_diff) > 1 else ""
        check("re-start with different WO -> accepted=False", acc_diff is False, f"({summ_diff})")
        wo_now = await (await root.get_child([f"{idx}:CurrentWorkOrderId"])).read_value()
        check("current WO not overwritten by rejected call", wo_now == WO, f"(got {wo_now})")

        # 3/4) 等待虚拟检测产生事件与变量更新
        print("  ...waiting for virtual inspections (~8s)")
        await asyncio.sleep(8)
        last_result = await (await insp.get_child([f"{idx}:LastResult"])).read_value()
        last_img = await (await insp.get_child([f"{idx}:LastImageId"])).read_value()
        ng_rate = await (await insp.get_child([f"{idx}:NgRate"])).read_value()
        cnt_all = await (await stats.get_child([f"{idx}:InspectCount"])).read_value()
        check("LastResult updated", isinstance(last_result, bool))
        check("LastImageId updated", bool(last_img), f"({last_img})")
        check("InspectCount > 0", cnt_all > 0, f"(count={cnt_all})")
        check("NgRate in [0,1] (window non-empty)", 0.0 <= ng_rate <= 1.0, f"(rate={ng_rate:.3f})")
        check("InspectionFinished events received", len(handler.insp_events) > 0, f"(n={len(handler.insp_events)})")
        check("NgDetected event observed (p=0.3)", len(handler.ng_events) > 0, f"(n={len(handler.ng_events)})")

        # Part Traceability：SN 已写入且事件携带 SN 字段
        part_sn = await (await root.get_child([f"{idx}:CurrentPartSN"])).read_value()
        ev_sn = str(getattr(handler.insp_events[-1], "SerialNumber", "")) if handler.insp_events else ""
        # 虚拟检测可能恰好在上面的两次读取之间产生新事件；短暂等待变量与事件对齐，避免测试竞态。
        for _ in range(15):
            if ev_sn == part_sn:
                break
            await asyncio.sleep(0.2)
            part_sn = await (await root.get_child([f"{idx}:CurrentPartSN"])).read_value()
            ev_sn = str(getattr(handler.insp_events[-1], "SerialNumber", "")) if handler.insp_events else ""
        check("CurrentPartSN populated", bool(part_sn), f"({part_sn})")
        check("InspectionFinishedEvent carries SerialNumber", bool(ev_sn), f"({ev_sn})")
        check("event SN matches CurrentPartSN", ev_sn == part_sn, f"({ev_sn} vs {part_sn})")

        # MES 回执变量：语义拆分后三个变量都存在且可写
        mes_node = await root.get_child([f"{idx}:LastMesOrderNo"])
        ng_mes_node = await root.get_child([f"{idx}:LastNgMesOrderNo"])
        completion_rc_node = await root.get_child([f"{idx}:LastCompletionReceiptNo"])
        await mes_node.write_value("MES-20261109-0001")
        await ng_mes_node.write_value("MES-20261109-0002")
        await completion_rc_node.write_value("RC-20261109-0003")
        mes_val = await mes_node.read_value()
        check("LastMesOrderNo writable and readable", mes_val == "MES-20261109-0001", f"({mes_val})")
        check("LastNgMesOrderNo writable and readable",
              await ng_mes_node.read_value() == "MES-20261109-0002")
        check("LastCompletionReceiptNo writable and readable",
              await completion_rc_node.read_value() == "RC-20261109-0003")
        wo_now = await (await root.get_child([f"{idx}:CurrentWorkOrderId"])).read_value()
        check("receipt var does not clobber CurrentWorkOrderId", wo_now == WO, f"(got {wo_now})")

        # N3) 负面用例：向 ReadOnly 变量写入必须被拒（硬约束 5）
        ro_node = await insp.get_child([f"{idx}:LastResult"])
        try:
            await ro_node.write_value(True)
            check("N3: write to ReadOnly variable rejected", False, "(no exception raised)")
        except Exception as e:
            msg = str(e)
            check("N3: write to ReadOnly variable rejected",
                  ("BadNotWritable" in msg) or ("BadUserAccessDenied" in msg), f"({type(e).__name__})")

        # 5) 一致性校验：错误工单号（N2 负面用例：accepted=False 且原因含 mismatch）
        ret_bad = await root.call_method(f"{idx}:CompleteWorkOrder", "WO-WRONG-999")
        accepted_bad, reason_bad = ret_bad[0], ret_bad[1] if len(ret_bad) > 1 else ""
        check("N2: CompleteWorkOrder wrong id -> accepted=False", accepted_bad is False, f"({reason_bad})")
        check("N2: reject reason mentions mismatch", "mismatch" in reason_bad)
        status = await (await root.get_child([f"{idx}:DeviceStatus"])).read_value()
        check("still Running after rejected call", status == 1, f"(got {status})")

        # 6) 正确完工
        ret_ok = await root.call_method(f"{idx}:CompleteWorkOrder", WO)
        accepted_ok, summary = ret_ok[0], ret_ok[1] if len(ret_ok) > 1 else ""
        check("CompleteWorkOrder correct id -> accepted=True", accepted_ok is True, f"({summary})")
        status = await (await root.get_child([f"{idx}:DeviceStatus"])).read_value()
        check("DeviceStatus back to 0 (Idle)", status == 0, f"(got {status})")
        await asyncio.sleep(1)  # 收 WorkOrderCompleted 事件
        check("WorkOrderCompleted event received", len(handler.done_events) > 0, f"(n={len(handler.done_events)})")

    failed = [r for r in results if not r[1]]
    print(f"\n===== SMOKE TEST: {len(results) - len(failed)}/{len(results)} passed =====")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
