"""Security regression: transport encryption, anonymous read-only, admin write/call."""
from __future__ import annotations

import asyncio
import sys

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

    admin = await create_secure_client(admin=True)
    async with admin:
        idx, root = await station_nodes(admin)
        receipt = await root.get_child([f"{idx}:LastMesOrderNo"])
        previous = str(await receipt.read_value() or "")
        marker = "SECURITY-ADMIN-WRITE"
        await receipt.write_value(marker)
        check("admin write is accepted", str(await receipt.read_value() or "") == marker)
        await receipt.write_value(previous)

        await root.call_method(f"{idx}:ResetCounters")
        check("admin Method call is accepted", True)

    username, _ = admin_credentials()
    print(f"INFO | admin identity used: {username}")

    failed = [name for name, passed, _ in results if not passed]
    print(f"===== SECURITY TEST: {len(results) - len(failed)}/{len(results)} passed =====")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
