"""Security regression: transport encryption, anonymous read-only, admin write/call."""
from __future__ import annotations

import asyncio
import os
import sys
import urllib.error
import urllib.request

from asyncua import Client

from ua_test_client import admin_credentials, create_secure_client, endpoint_url

NS_URI = "urn:fde:linqinspect:qc"
results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition), detail))
    print(f"{'PASS' if condition else 'FAIL'} | {name} {detail}")


async def station_nodes(client: Client):
    idx = await client.get_namespace_index(NS_URI)
    root = await client.nodes.objects.get_child([f"{idx}:QCStation"])
    return idx, root


async def main() -> int:
    try:
        plain_client = Client(endpoint_url(), timeout=2)
        async with plain_client:
            await plain_client.get_root_node().read_browse_name()
        check("NoSecurity client is rejected", False, "(unexpectedly connected)")
    except Exception as exc:
        check("NoSecurity client is rejected", True, f"({type(exc).__name__})")

    anonymous = await create_secure_client(admin=False)
    async with anonymous:
        idx, root = await station_nodes(anonymous)
        status = await (await root.get_child([f"{idx}:DeviceStatus"])).read_value()
        check("anonymous secure client can read", status in (0, 1, 2), f"(DeviceStatus={status})")

        receipt = await root.get_child([f"{idx}:LastMesOrderNo"])
        try:
            await receipt.write_value("SECURITY-BYPASS")
            check("anonymous write is rejected", False, "(unexpected success)")
        except Exception as exc:
            check("anonymous write is rejected", True, f"({type(exc).__name__})")

        try:
            await root.call_method(f"{idx}:ResetCounters")
            check("anonymous Method call is rejected", False, "(unexpected success)")
        except Exception as exc:
            check("anonymous Method call is rejected", True, f"({type(exc).__name__})")

        for method, arguments in (
            ("ExportPendingEvents", (0, 1)),
            ("PurgeOutbox", ("not-a-valid-token",)),
        ):
            try:
                await root.call_method(f"{idx}:{method}", *arguments)
                check(f"anonymous {method} is rejected", False, "(unexpected success)")
            except Exception as exc:
                check(f"anonymous {method} is rejected", True, f"({type(exc).__name__})")

    admin = await create_secure_client(admin=True)
    async with admin:
        idx, root = await station_nodes(admin)
        receipt_names = ("LastMesOrderNo", "LastNgMesOrderNo", "LastCompletionReceiptNo")
        direct_writes = []
        for name in receipt_names:
            receipt = await root.get_child([f"{idx}:{name}"])
            original = str(await receipt.read_value() or "")
            try:
                await receipt.write_value("SECURITY-RECEIPT-BYPASS")
                direct_writes.append((name, False, False))
            except Exception:
                direct_writes.append((name, True, str(await receipt.read_value() or "") == original))
        check("admin direct writes to receipt variables are rejected without mutation",
              all(rejected and preserved for _, rejected, preserved in direct_writes),
              f"({direct_writes})")

        original_wo = str(await (await root.get_child([f"{idx}:CurrentWorkOrderId"])).read_value() or "")
        ng_result = await root.call_method(
            f"{idx}:RecordMesReceipt", "ng", "MES-SECURITY-0001", original_wo or "WO-SECURITY", "corr-security-ng"
        )
        completion_result = await root.call_method(
            f"{idx}:RecordMesReceipt", "complete", "RC-SECURITY-0001", original_wo or "WO-SECURITY", "corr-security-rc"
        )
        last_mes = str(await (await root.get_child([f"{idx}:LastMesOrderNo"])).read_value() or "")
        last_ng = str(await (await root.get_child([f"{idx}:LastNgMesOrderNo"])).read_value() or "")
        last_rc = str(await (await root.get_child([f"{idx}:LastCompletionReceiptNo"])).read_value() or "")
        wo_after = str(await (await root.get_child([f"{idx}:CurrentWorkOrderId"])).read_value() or "")
        check("admin RecordMesReceipt updates readonly variables without changing work order",
              ng_result[0] is True and completion_result[0] is True and
              last_ng == "MES-SECURITY-0001" and last_rc == "RC-SECURITY-0001" and
              last_mes == "RC-SECURITY-0001" and wo_after == original_wo,
              f"(ng={last_ng}, rc={last_rc}, mirror={last_mes}, wo={wo_after})")

    admin = await create_secure_client(admin=True)
    async with admin:
        idx, root = await station_nodes(admin)
        for name in ("DeviceStatus", "CurrentWorkOrderId"):
            control = await root.get_child([f"{idx}:{name}"])
            original = await control.read_value()
            try:
                await control.write_value(2 if name == "DeviceStatus" else "BYPASS")
                check(f"direct write to {name} is rejected", False, "(unexpected success)")
            except Exception as exc:
                check(f"direct write to {name} is rejected", True, f"({type(exc).__name__})")
            self_value = await control.read_value()
            check(f"direct write rejection preserves {name}", self_value == original,
                  f"({self_value!r})")

    node_red_base = os.getenv("NODERED_BASE", "").rstrip("/")
    node_red_auth = os.getenv("NODERED_HTTP_AUTH", "").strip()
    if node_red_base:
        try:
            urllib.request.urlopen(node_red_base + "/qc/status", timeout=3)
            check("Node-RED HTTP endpoint rejects unauthenticated access", False, "(unexpected success)")
        except urllib.error.HTTPError as exc:
            check("Node-RED HTTP endpoint rejects unauthenticated access", exc.code == 401, f"(HTTP {exc.code})")
        except Exception as exc:
            check("Node-RED HTTP endpoint rejects unauthenticated access", False, f"({type(exc).__name__})")

        if node_red_auth:
            request = urllib.request.Request(
                node_red_base + "/qc/status",
                headers={"Authorization": node_red_auth},
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                check("Node-RED HTTP endpoint accepts authenticated access", response.status == 200)

            replay_body = b"{}"
            try:
                urllib.request.urlopen(
                    urllib.request.Request(
                        node_red_base + "/qc/dead-letters/replay",
                        data=replay_body,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    ),
                    timeout=3,
                )
                check("Node-RED dead-letter replay rejects unauthenticated access", False,
                      "(unexpected success)")
            except urllib.error.HTTPError as exc:
                check("Node-RED dead-letter replay rejects unauthenticated access",
                      exc.code == 401, f"(HTTP {exc.code})")

            try:
                urllib.request.urlopen(
                    urllib.request.Request(
                        node_red_base + "/qc/dead-letters/replay",
                        data=replay_body,
                        headers={
                            "Authorization": node_red_auth,
                            "Content-Type": "application/json",
                        },
                        method="POST",
                    ),
                    timeout=3,
                )
                check("authenticated dead-letter replay validates request body", False,
                      "(unexpected success)")
            except urllib.error.HTTPError as exc:
                check("authenticated dead-letter replay validates request body",
                      exc.code == 400, f"(HTTP {exc.code})")
        else:
            check("Node-RED authenticated HTTP credential configured", False, "(missing NODERED_HTTP_AUTH)")

    username, _ = admin_credentials()
    print(f"INFO | admin identity used: {username}")

    failed = [name for name, passed, _ in results if not passed]
    print(f"===== SECURITY TEST: {len(results) - len(failed)}/{len(results)} passed =====")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
