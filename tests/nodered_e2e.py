"""Node-RED 端到端验证：UA 事件 -> 队列 -> 模拟 MES -> LastMesOrderNo 写回。"""
import asyncio
import os
import re
import sys
from datetime import datetime

from asyncua import Client

PORT = int(os.getenv("UA_ENDPOINT_PORT", "4840"))
URL = f"opc.tcp://localhost:{PORT}"
NS_URI = "urn:fde:linqinspect:qc"
WO = "WO-NRED-" + datetime.now().strftime("%Y%m%d%H%M%S")


def ok(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'} | {name} {detail}")
    return bool(cond)


async def read_val(node):
    try:
        return await node.read_value()
    except Exception:
        return None


async def main():
    results = []
    async with Client(URL) as client:
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

        st = await read_val(status)
        if st == 1:
            wo0 = str(await read_val(cur_wo) or "")
            print("INFO | server is Running with", wo0, "-> completing before test")
            await root.call_method(f"{idx}:CompleteWorkOrder", wo0)
            for _ in range(20):
                if await read_val(status) == 0:
                    break
                await asyncio.sleep(0.25)

        initial_mes = str(await read_val(last_mes) or "")
        print("INFO | initial LastMesOrderNo =", initial_mes or "(empty)")

        ret = await root.call_method(f"{idx}:StartInspection", WO, 5)
        results.append(ok("StartInspection accepted", ret and ret[0] is True, f"({ret[1] if len(ret) > 1 else ''})"))
        results.append(ok("DeviceStatus Running", await read_val(status) == 1))

        mes_no = ""
        ng_before = int(await read_val(stats_ng) or 0)
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            mes_no = str(await read_val(last_mes) or "")
            if mes_no and mes_no != initial_mes and mes_no.startswith("MES-"):
                break
            await asyncio.sleep(0.5)

        results.append(ok("NG order receipt appears in LastMesOrderNo",
                          mes_no != initial_mes and bool(re.match(r"^MES-\d{8}-\d{4}$", mes_no)),
                          f"(got {mes_no or 'empty'}, initial {initial_mes or 'empty'})"))
        results.append(ok("NG order receipt also lands in LastNgMesOrderNo",
                          str(await read_val(last_ng_mes) or "") == mes_no))
        results.append(ok("CurrentWorkOrderId not clobbered", str(await read_val(cur_wo) or "") == WO))
        ng_after = int(await read_val(stats_ng) or 0)
        all_after = int(await read_val(stats_all) or 0)
        results.append(ok("server produced NG samples", ng_after > ng_before, f"(ng={ng_after}, total={all_after})"))

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

    passed = sum(1 for r in results if r)
    print(f"===== NODERED E2E: {passed}/{len(results)} passed =====")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
