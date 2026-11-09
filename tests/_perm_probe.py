"""探针：查 LastMesOrderNo / CurrentWorkOrderId / DeviceStatus 的 AccessLevel 与 UserAccessLevel，并尝试写入。"""
import asyncio

from asyncua import Client, ua

NS_URI = "urn:fde:linqinspect:qc"


async def main():
    async with Client("opc.tcp://localhost:4840") as client:
        idx = await client.get_namespace_index(NS_URI)
        root = await client.nodes.objects.get_child([f"{idx}:QCStation"])
        out = []
        for name in ("LastMesOrderNo", "CurrentWorkOrderId", "DeviceStatus", "CurrentPartSN"):
            node = await root.get_child([f"{idx}:{name}"])
            al = await node.read_attribute(ua.AttributeIds.AccessLevel)
            ual = await node.read_attribute(ua.AttributeIds.UserAccessLevel)
            out.append(f"{name}: AccessLevel={al.Value.Value if al.Value else None}, UserAccessLevel={ual.Value.Value if ual.Value else None}")
            try:
                await node.write_value("TEST" if "Status" not in name else 1)
                out.append(f"  write {name}: OK")
            except Exception as e:
                out.append(f"  write {name}: {type(e).__name__}")
        open("D:/UA_LinqInspect/_perm_probe.txt", "w", encoding="utf-8").write("\n".join(out))


asyncio.run(main())
