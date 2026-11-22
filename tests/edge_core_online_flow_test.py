#!/usr/bin/env python3
"""End-to-end Node-RED -> Edge Core -> MES -> UA writeback cutover drill."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asyncua import Client, ua
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
SCRIPTS_DIR = REPO_ROOT / "scripts"
for path in (REPO_ROOT, TESTS_DIR, SCRIPTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from edge_core.evidence_signing import verify_document  # noqa: E402
from run_regression import (  # noqa: E402
    NODE_RED_BIN,
    free_port,
    prepare_node_red,
    stop_process,
    wait_for_http,
    wait_for_log,
)
from edge_core.store import DeliveryStore  # noqa: E402
from ua_test_client import (  # noqa: E402
    APPLICATION_URI,
    CLIENT_CERT,
    CLIENT_KEY,
    SERVER_CERT,
)

RESULTS: list[dict[str, Any]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append({"name": name, "passed": bool(condition), "detail": detail})
    print(f"{'PASS' if condition else 'FAIL'} | {name} {detail}".rstrip(), flush=True)


def http_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 3.0,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_headers = dict(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("content-type", "application/json")
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(4 * 1024 * 1024)
    value = json.loads(raw.decode("utf-8")) if raw else {}
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object from {url}")
    return value


def wait_until(predicate, timeout: float, description: str):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as exc:
            last_error = exc
        time.sleep(0.2)
    suffix = f"; last error={last_error}" if last_error else ""
    raise TimeoutError(f"timed out waiting for {description}{suffix}")


def start_process(command: list[str], env: dict[str, str], log_path: Path):
    handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return process, handle


async def secure_client(endpoint: str, username: str, password: str) -> Client:
    for path in (CLIENT_CERT, CLIENT_KEY, SERVER_CERT):
        if not path.is_file():
            raise FileNotFoundError(f"missing test certificate: {path}")
    client = Client(endpoint, timeout=5)
    client.application_uri = APPLICATION_URI
    await client.set_security(
        SecurityPolicyBasic256Sha256,
        certificate=str(CLIENT_CERT),
        private_key=str(CLIENT_KEY),
        server_certificate=str(SERVER_CERT),
        mode=ua.MessageSecurityMode.SignAndEncrypt,
    )
    client.set_user(username)
    client.set_password(password)
    await client.connect()
    return client


async def read_receipts(endpoint: str, username: str, password: str) -> dict[str, Any]:
    client = await secure_client(endpoint, username, password)
    try:
        namespace_index = await client.get_namespace_index("urn:fde:linqinspect:qc")
        root = await client.nodes.objects.get_child([f"{namespace_index}:QCStation"])
        values = {}
        for name in (
            "DeviceStatus",
            "LastNgMesOrderNo",
            "LastMesOrderNo",
            "LastCompletionReceiptNo",
        ):
            node = await root.get_child([f"{namespace_index}:{name}"])
            values[name] = await node.read_value()
        return values
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    artifact_dir = Path(
        os.getenv("ONLINE_CUTOVER_ARTIFACT")
        or REPO_ROOT / ".tmp" / f"online-cutover-{stamp}"
    ).expanduser().resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    ua_port = free_port()
    nr_port = free_port()
    edge_port = free_port()
    operator_port = free_port()
    while len({ua_port, nr_port, edge_port, operator_port}) != 4:
        nr_port = free_port()
        edge_port = free_port()
        operator_port = free_port()

    endpoint = f"opc.tcp://127.0.0.1:{ua_port}"
    node_red_base = f"http://127.0.0.1:{nr_port}"
    edge_api_base = f"http://127.0.0.1:{edge_port}"
    operator_base = f"http://127.0.0.1:{operator_port}"
    admin_user = "online-cutover-admin"
    admin_password = secrets.token_urlsafe(24)
    edge_token = secrets.token_urlsafe(24)
    operator_user = "operator"
    operator_password = secrets.token_urlsafe(24)
    http_auth = "Basic " + base64.b64encode(
        f"{admin_user}:{admin_password}".encode("utf-8")
    ).decode("ascii")

    node_red_user_dir, node_red_xdg_dir = prepare_node_red(
        artifact_dir, endpoint, node_red_base, admin_user, admin_password
    )
    source_db = artifact_dir / "qc-queue.sqlite"
    edge_db = artifact_dir / "edge-core.sqlite"
    lock_dir = artifact_dir / "sender-authority"
    base_env = os.environ.copy()
    base_env.update({
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": str(REPO_ROOT),
        "XDG_CONFIG_HOME": str(node_red_xdg_dir),
        "UA_ENDPOINT": endpoint,
        "UA_ENDPOINT_PORT": str(ua_port),
        "UA_ADMIN_USER": admin_user,
        "UA_ADMIN_PASS": admin_password,
        "NODERED_BASE": node_red_base,
        "NODERED_HTTP_USER": admin_user,
        "NODERED_HTTP_PASS": admin_password,
        "NODERED_HTTP_AUTH": http_auth,
        "QC_REPO_ROOT": str(REPO_ROOT),
        "QC_DB_PATH": str(source_db),
        "QC_STATE_PATH": str(artifact_dir / "qc-state.json"),
        "QC_STATE_BACKEND": "sqlite",
        "QC_STATE_DB_PATH": str(source_db),
        "QC_SENDER_LOCK_DIR": str(lock_dir),
        "QC_DELIVERY_AUTHORITY": "node-red",
    })
    server_env = base_env | {
        "UA_BIND_ADDRESS": "127.0.0.1",
        "UA_SERVER_CERT": str(SERVER_CERT),
        "UA_SERVER_PRIVATE_KEY": str(REPO_ROOT / "certs" / "server" / "server_key.pem"),
        "UA_TRUSTED_CLIENT_CERTS_DIR": str(REPO_ROOT / "certs" / "trusted_clients"),
        "UA_AUDIT_LOG": str(artifact_dir / "ua-server-audit.log"),
        "QC_AUTO_INSPECT": "1",
        "QC_INSPECT_INTERVAL": "0.4",
        "QC_NG_PROBABILITY": "1.0",
        "QC_NG_WINDOW": "20",
    }
    edge_env = base_env | {
        "QC_DELIVERY_AUTHORITY": "edge-core",
        "EDGE_API_HOST": "127.0.0.1",
        "EDGE_API_PORT": str(edge_port),
        "EDGE_API_BASE_URL": edge_api_base,
        "EDGE_API_TOKEN": edge_token,
        "EDGE_DB_PATH": str(edge_db),
        "EDGE_EVIDENCE_SIGNING_KEY": "online-evidence-signing-key",
        "EDGE_EVIDENCE_SIGNING_KEY_ID": "online-k1",
        "MES_BASE_URL": node_red_base,
        "EDGE_MES_BASIC_USER": admin_user,
        "EDGE_MES_BASIC_PASS": admin_password,
        "EDGE_MES_TIMEOUT_SECONDS": "4",
        "EDGE_WORKER_INTERVAL_SECONDS": "0.1",
        "EDGE_MAX_ATTEMPTS": "4",
        "EDGE_LEASE_SECONDS": "10",
    }
    node_red_edge_env = edge_env | {
        "QC_DELIVERY_AUTHORITY": "edge-core",
        "EDGE_API_BASE_URL": edge_api_base,
        "EDGE_API_TOKEN": edge_token,
    }
    writeback_env = edge_env | {
        "UA_CLIENT_CERT": str(CLIENT_CERT),
        "UA_CLIENT_KEY": str(CLIENT_KEY),
        "UA_SERVER_CERT": str(SERVER_CERT),
        "UA_CLIENT_APPLICATION_URI": APPLICATION_URI,
        "UA_WRITEBACK_TIMEOUT_SECONDS": "5",
        "UA_WRITEBACK_BATCH_LIMIT": "200",
    }
    operator_env = edge_env | {
        "OPERATOR_CONSOLE_HOST": "127.0.0.1",
        "OPERATOR_CONSOLE_PORT": str(operator_port),
        "OPERATOR_CONSOLE_USER": operator_user,
        "OPERATOR_CONSOLE_PASSWORD": operator_password,
        "UA_CLIENT_CERT": str(CLIENT_CERT),
        "UA_CLIENT_KEY": str(CLIENT_KEY),
        "UA_SERVER_CERT": str(SERVER_CERT),
        "UA_CLIENT_APPLICATION_URI": APPLICATION_URI,
    }
    headers = {"authorization": http_auth}
    api_headers = {"authorization": f"Bearer {edge_token}"}
    operator_auth = "Basic " + base64.b64encode(
        f"{operator_user}:{operator_password}".encode("utf-8")
    ).decode("ascii")
    operator_headers = {"authorization": operator_auth}
    processes: list[tuple[Any, Any]] = []
    work_order = f"WO-ONLINE-CUTOVER-{stamp}"
    summary: dict[str, Any] = {
        "ok": False,
        "artifactDir": str(artifact_dir),
        "uaEndpoint": endpoint,
        "nodeRedBase": node_red_base,
        "edgeApiBase": edge_api_base,
        "operatorBase": operator_base,
        "workOrderId": work_order,
    }

    def launch(command: list[str], env: dict[str, str], name: str):
        process, handle = start_process(command, env, artifact_dir / f"{name}.log")
        processes.append((process, handle))
        return process

    try:
        ua_process = launch(
            [sys.executable, str(REPO_ROOT / "server" / "qc_server.py")],
            server_env,
            "ua-server",
        )
        wait_for_log(artifact_dir / "ua-server.log", "OPC UA Server listening", 25)

        nr_baseline = launch(
            [str(NODE_RED_BIN), "--userDir", str(node_red_user_dir), "--port", str(nr_port), "--no-telemetry"],
            base_env,
            "node-red-baseline",
        )
        wait_for_log(artifact_dir / "node-red-baseline.log", "Started flows", 35)
        wait_for_http(node_red_base + "/qc/status", 20, headers)
        baseline_status = wait_until(
            lambda: (
                status
                if (status := http_json(node_red_base + "/qc/status", headers=headers))
                .get("senderAuthority", {})
                .get("held") is True
                else None
            ),
            12,
            "Node-RED baseline sender authority",
        )
        check(
            "baseline Node-RED owns the sender lease",
            baseline_status["senderAuthority"].get("currentOwner") == "node-red",
            json.dumps(baseline_status["senderAuthority"], ensure_ascii=False),
        )
        stop_process(nr_baseline)
        nr_baseline = None

        edge_process = launch([sys.executable, "-m", "edge_core.main"], edge_env, "edge-core")
        wait_for_http(edge_api_base + "/v1/health", 20)
        lock_meta = wait_until(
            lambda: json.loads((lock_dir / "owner.json").read_text(encoding="utf-8"))
            if (lock_dir / "owner.json").is_file()
            else None,
            10,
            "Edge Core sender lease",
        )
        check(
            "Edge Core acquires sender authority after Node-RED stops",
            lock_meta.get("owner") == "edge-core" and lock_meta.get("pid") == edge_process.pid,
            json.dumps(lock_meta, ensure_ascii=False),
        )

        operator_process = launch(
            [sys.executable, "-m", "operator_console.main"],
            operator_env,
            "operator-console",
        )
        wait_for_http(operator_base + "/healthz", 15)
        operator_status = wait_until(
            lambda: (
                status
                if (status := http_json(operator_base + "/api/status", headers=operator_headers))
                .get("station", {})
                .get("available") is True
                else None
            ),
            12,
            "operator console station status",
        )
        check(
            "operator console reads the real UA station",
            operator_status["station"]["data"].get("deviceStatusText") in ("空闲", "运行中", "故障"),
            json.dumps(operator_status["station"]["data"], ensure_ascii=False)[:1200],
        )

        adapter = launch(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "run_ua_writeback_adapter.py"),
                "--interval",
                "0.5",
                "--limit",
                "200",
            ],
            writeback_env,
            "ua-writeback",
        )

        nr_edge = launch(
            [str(NODE_RED_BIN), "--userDir", str(node_red_user_dir), "--port", str(nr_port), "--no-telemetry"],
            node_red_edge_env,
            "node-red-edge-core",
        )
        wait_for_log(artifact_dir / "node-red-edge-core.log", "Started flows", 35)
        wait_for_http(node_red_base + "/qc/status", 20, headers)
        time.sleep(6)

        nr_status = http_json(node_red_base + "/qc/status", headers=headers)
        check(
            "Node-RED runs in Edge Core authority mode",
            nr_status.get("deliveryAuthority") == "edge-core",
            f"(authority={nr_status.get('deliveryAuthority')})",
        )
        check(
            "Node-RED local send pump is blocked by the shared lease",
            nr_status.get("senderAuthority", {}).get("held") is False
            and nr_status.get("senderAuthority", {}).get("currentOwner") == "edge-core",
            json.dumps(nr_status.get("senderAuthority"), ensure_ascii=False),
        )

        start_response = http_json(
            operator_base + "/api/work-orders/start",
            headers={**operator_headers, "X-Operator-Request": "1"},
            method="POST",
            payload={"workOrderId": work_order, "targetCount": 8},
        )
        check(
            "operator console starts a work order through the real UA Method",
            start_response.get("accepted") is True,
            json.dumps(start_response, ensure_ascii=False),
        )
        drive_status = wait_until(
            lambda: (
                status
                if (status := http_json(operator_base + "/api/status", headers=operator_headers))
                .get("station", {})
                .get("data", {})
                .get("InspectCount", 0) >= 8
                and status["station"]["data"].get("NgCount", 0) >= 1
                else None
            ),
            20,
            "operator console inspection metrics",
        )
        complete_response = http_json(
            operator_base + "/api/work-orders/complete",
            headers={**operator_headers, "X-Operator-Request": "1"},
            method="POST",
            payload={"workOrderId": work_order},
        )
        check(
            "operator console completes the work order through the real UA Method",
            complete_response.get("accepted") is True,
            json.dumps(complete_response, ensure_ascii=False),
        )
        drive = {
            "workOrderId": work_order,
            "inspectCount": drive_status["station"]["data"]["InspectCount"],
            "ngCount": drive_status["station"]["data"]["NgCount"],
            "completionSummary": complete_response.get("message", ""),
        }
        check(
            "virtual inspections and completion ran through the switched chain",
            drive["inspectCount"] >= 8 and drive["ngCount"] >= 1,
            json.dumps(drive, ensure_ascii=False),
        )

        def matching_deliveries() -> list[dict[str, Any]]:
            payload = http_json(edge_api_base + "/v1/deliveries?limit=200", headers=api_headers)
            items = payload.get("items")
            if not isinstance(items, list):
                raise RuntimeError("Edge Core deliveries response is missing items")
            return [item for item in items if item.get("workOrderId") == work_order]

        deliveries = wait_until(
            lambda: (
                items
                if (items := matching_deliveries())
                and any(item.get("eventType") == "NG_ORDER" for item in items)
                and any(item.get("eventType") == "COMPLETION_REPORT" for item in items)
                and all(item.get("state") == "DONE" and item.get("uaWriteConfirmed") for item in items)
                else None
            ),
            35,
            "MES delivery and UA writeback to DONE",
        )
        ng_deliveries = [item for item in deliveries if item["eventType"] == "NG_ORDER"]
        completion_deliveries = [item for item in deliveries if item["eventType"] == "COMPLETION_REPORT"]
        check("at least one NG delivery reached DONE", len(ng_deliveries) >= 1, f"(count={len(ng_deliveries)})")
        check("completion delivery reached DONE once", len(completion_deliveries) == 1,
              f"(count={len(completion_deliveries)})")
        check(
            "MES receipts are present and typed for every delivery",
            all(
                item.get("mesReceipt", {}).get("receiptType")
                == ("MES_ORDER" if item["eventType"] == "NG_ORDER" else "COMPLETION_RECEIPT")
                and item.get("mesReceipt", {}).get("receiptNo")
                for item in deliveries
            ),
            json.dumps([item.get("mesReceipt") for item in deliveries], ensure_ascii=False)[:1500],
        )

        ng_delivery = ng_deliveries[0]
        evidence = http_json(
            edge_api_base + f"/v1/deliveries/{ng_delivery['deliveryId']}/evidence",
            headers=api_headers,
        )
        check(
            "Edge Core evidence bundle reconstructs the NG inspection and audit trail",
            evidence.get("bundleType") == "QC_DELIVERY_EVIDENCE"
            and evidence.get("deliveryId") == ng_delivery["deliveryId"]
            and bool(evidence.get("inspection", {}).get("defectType"))
            and evidence.get("mes", {}).get("receipt", {}).get("receiptNo") == ng_delivery["mesReceipt"]["receiptNo"]
            and evidence.get("ua", {}).get("writeConfirmed") is True
            and evidence.get("completeness", {}).get("complete") is True
            and evidence.get("signatureStatus") == "SIGNED"
            and verify_document(evidence, {"online-k1": "online-evidence-signing-key"})["ok"]
            and len(evidence.get("audit", [])) >= 3
            and len(evidence.get("bundleSha256", "")) == 64,
            json.dumps({
                "deliveryId": evidence.get("deliveryId"),
                "bundleSha256": evidence.get("bundleSha256"),
                "completeness": evidence.get("completeness"),
                "auditEvents": len(evidence.get("audit", [])),
                "signatureStatus": evidence.get("signatureStatus"),
                "signingKeyId": (evidence.get("signature") or {}).get("keyId"),
            }, ensure_ascii=False),
        )
        operator_evidence = http_json(
            operator_base + f"/api/evidence/{ng_delivery['deliveryId']}",
            headers=operator_headers,
        )
        check(
            "operator console exposes the same quality evidence bundle",
            operator_evidence.get("data", {}).get("bundleSha256") == evidence.get("bundleSha256"),
            f"(bundle={operator_evidence.get('data', {}).get('bundleSha256', '')[:16]})",
        )

        image_id = str(evidence["inspection"]["imageId"])
        image_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII="
        )
        image_sha256 = hashlib.sha256(image_bytes).hexdigest()
        upload = http_json(
            edge_api_base + "/v1/evidence/images",
            headers=api_headers,
            method="POST",
            payload={
                "imageId": image_id,
                "imageBase64": base64.b64encode(image_bytes).decode("ascii"),
                "sha256": image_sha256,
            },
        )
        image_bundle = http_json(
            edge_api_base + f"/v1/deliveries/{ng_delivery['deliveryId']}/evidence",
            headers=api_headers,
        )
        check(
            "image evidence is retained and linked into the bundle",
            upload.get("sha256") == image_sha256
            and image_bundle.get("imageEvidence", {}).get("available") is True
            and image_bundle.get("completeness", {}).get("imageBinaryStored") is True,
            json.dumps({
                "imageId": image_id,
                "sha256": upload.get("sha256"),
                "imageEvidence": image_bundle.get("imageEvidence"),
                "completeness": image_bundle.get("completeness"),
            }, ensure_ascii=False),
        )
        image_request = urllib.request.Request(
            operator_base + f"/api/evidence/{ng_delivery['deliveryId']}/image",
            headers=operator_headers,
        )
        with urllib.request.urlopen(image_request, timeout=10) as image_response:
            proxied_image = image_response.read()
            proxied_type = image_response.headers.get_content_type()
        check(
            "operator console proxies the retained evidence image",
            proxied_image == image_bytes and proxied_type == "image/png",
            f"(type={proxied_type}, bytes={len(proxied_image)})",
        )
        receipt_no = ng_delivery["mesReceipt"]["receiptNo"]
        search = http_json(
            edge_api_base + "/v1/evidence/search?" + urllib.parse.urlencode({"q": receipt_no}),
            headers=api_headers,
        )
        check(
            "evidence search finds the delivery by MES receipt number",
            any(item.get("deliveryId") == ng_delivery["deliveryId"] for item in search.get("items", [])),
            f"(query={receipt_no}, results={len(search.get('items', []))})",
        )

        edge_status = http_json(edge_api_base + "/v1/status", headers=api_headers)
        check(
            "Edge Core queue and dead letters are empty",
            edge_status.get("queue", {}).get("total") == 0
            and edge_status.get("deadLetters", {}).get("total") == 0,
            json.dumps(edge_status, ensure_ascii=False)[:1500],
        )

        outbox_status = wait_until(
            lambda: (
                status
                if (status := http_json(node_red_base + "/qc/status", headers=headers))
                .get("outbox", {})
                .get("pendingCount") == 0
                else None
            ),
            15,
            "UA outbox acknowledgement",
        )
        check(
            "Node-RED acknowledged the UA outbox after Edge Core accepted ingress",
            outbox_status["outbox"].get("pendingCount") == 0,
            json.dumps(outbox_status["outbox"], ensure_ascii=False),
        )

        receipts = asyncio.run(read_receipts(endpoint, admin_user, admin_password))
        ng_receipts = {str(item["mesReceipt"]["receiptNo"]) for item in ng_deliveries}
        completion_receipt = str(completion_deliveries[0]["mesReceipt"]["receiptNo"])
        check(
            "UA receipt variables were updated by the real writeback adapter",
            str(receipts.get("LastNgMesOrderNo")) in ng_receipts
            and str(receipts.get("LastCompletionReceiptNo")) == completion_receipt,
            json.dumps({"ngReceipts": sorted(ng_receipts), "completionReceipt": completion_receipt,
                        "receipts": receipts}, ensure_ascii=False),
        )
        check(
            "station returned to Idle after completion",
            receipts.get("DeviceStatus") == 0,
            f"(DeviceStatus={receipts.get('DeviceStatus')})",
        )

        stop_process(operator_process)
        operator_process = None
        stop_process(nr_edge)
        nr_edge = None
        stop_process(adapter)
        adapter = None
        stop_process(edge_process)
        edge_process = None
        wait_until(lambda: not lock_dir.exists(), 10, "Edge Core lease release")

        rollback_env = base_env | {"QC_DELIVERY_AUTHORITY": "node-red"}
        nr_rollback = launch(
            [str(NODE_RED_BIN), "--userDir", str(node_red_user_dir), "--port", str(nr_port), "--no-telemetry"],
            rollback_env,
            "node-red-rollback",
        )
        wait_for_log(artifact_dir / "node-red-rollback.log", "Started flows", 35)
        wait_for_http(node_red_base + "/qc/status", 20, headers)
        rollback_status = wait_until(
            lambda: (
                status
                if (status := http_json(node_red_base + "/qc/status", headers=headers))
                .get("senderAuthority", {})
                .get("held") is True
                else None
            ),
            12,
            "Node-RED rollback sender authority",
        )
        check(
            "Node-RED reacquires sender authority after Edge Core shutdown",
            rollback_status["senderAuthority"].get("currentOwner") == "node-red",
            json.dumps(rollback_status["senderAuthority"], ensure_ascii=False),
        )
        persisted = [
            item for item in DeliveryStore(edge_db).list_deliveries(limit=200)
            if item.get("workOrderId") == work_order
        ]
        check(
            "Edge Core delivery audit remains available for rollback reconciliation",
            len(persisted) == len(deliveries)
            and all(item.get("state") == "DONE" and item.get("uaWriteConfirmed") for item in persisted),
            f"(count={len(persisted)})",
        )
        summary.update({
            "ok": all(item["passed"] for item in RESULTS),
            "deliveries": deliveries,
            "receipts": receipts,
            "checks": RESULTS,
        })
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        check("online cutover drill completed without exception", False, summary["error"])
    finally:
        for process, handle in reversed(processes):
            stop_process(process)
            try:
                handle.close()
            except Exception:
                pass

    summary["checks"] = RESULTS
    summary["ok"] = all(item["passed"] for item in RESULTS)
    (artifact_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    passed = sum(1 for item in RESULTS if item["passed"])
    print(f"\n===== ONLINE CUTOVER: {passed}/{len(RESULTS)} passed =====", flush=True)
    print(f"Artifacts: {artifact_dir / 'summary.json'}", flush=True)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
