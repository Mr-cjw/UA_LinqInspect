"""Integration test for persistent queue metrics and Prometheus /metrics."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import urllib.request
from datetime import datetime

from ua_test_client import create_secure_client

NS_URI = "urn:fde:linqinspect:qc"
NR_BASE = os.getenv("NODERED_BASE", "http://127.0.0.1:1880").rstrip("/")
NODERED_HTTP_AUTH = os.getenv("NODERED_HTTP_AUTH", "").strip()

results: list[tuple[str, bool]] = []

SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>.*)\})? (?P<value>[^\s]+)$"
)
LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'} | {name} {detail}")


def http_get(path: str) -> tuple[int, dict[str, str], str]:
    headers = {"Authorization": NODERED_HTTP_AUTH} if NODERED_HTTP_AUTH else {}
    request = urllib.request.Request(NR_BASE + path, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=5) as response:
        return (
            response.status,
            {key.lower(): value for key, value in response.headers.items()},
            response.read().decode("utf-8", errors="replace"),
        )


def inject(node_id: str) -> bool:
    mode = {"inj_to": "timeout"}.get(node_id, node_id.removeprefix("inj_"))
    headers = {"Authorization": NODERED_HTTP_AUTH} if NODERED_HTTP_AUTH else {}
    request = urllib.request.Request(NR_BASE + f"/lab/mode/{mode}", headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        return 200 <= response.status < 300


def edge_status() -> dict:
    status, _, body = http_get("/qc/status")
    if not 200 <= status < 300:
        raise RuntimeError(f"/qc/status returned HTTP {status}")
    return json.loads(body)


def parse_metrics(text: str) -> tuple[dict, dict[str, str], dict[str, str], list[str]]:
    samples: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    helps: dict[str, str] = {}
    types: dict[str, str] = {}
    invalid: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("# HELP "):
            _, _, name, help_text = line.split(" ", 3)
            helps[name] = help_text
            continue
        if line.startswith("# TYPE "):
            _, _, name, metric_type = line.split(" ", 3)
            types[name] = metric_type
            continue
        if line.startswith("#"):
            continue
        match = SAMPLE_RE.match(line)
        if not match:
            invalid.append(line)
            continue
        labels = {
            key: value.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
            for key, value in LABEL_RE.findall(match.group("labels") or "")
        }
        key = (match.group("name"), tuple(sorted(labels.items())))
        samples[key] = float(match.group("value"))
    return samples, helps, types, invalid


def metric_value(samples: dict, name: str, **labels: str) -> float:
    return samples.get((name, tuple(sorted(labels.items()))), 0.0)


def fetch_metrics() -> tuple[dict, dict[str, str], dict[str, str], list[str], dict[str, str]]:
    status, headers, body = http_get("/metrics")
    if not 200 <= status < 300:
        raise RuntimeError(f"/metrics returned HTTP {status}")
    samples, helps, types, invalid = parse_metrics(body)
    return samples, helps, types, invalid, headers


async def read(node):
    try:
        return await node.read_value()
    except Exception:
        return None


async def wait_until(predicate, timeout: float = 35, interval: float = 0.25, description: str = "condition") -> bool:
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


async def ensure_idle(root, idx: int, status_node, work_order_node) -> bool:
    if await read(status_node) != 1:
        return True
    current = str(await read(work_order_node) or "")
    await root.call_method(f"{idx}:CompleteWorkOrder", current)
    return await wait_until(
        lambda: read(status_node) == 0,
        timeout=15,
        description="DeviceStatus idle before metrics baseline",
    )


async def main() -> int:
    samples, helps, metric_types, invalid, headers = fetch_metrics()
    check("/metrics returns HTTP 200", True)
    content_type = headers.get("content-type", "")
    content_type_parts = [part.strip() for part in content_type.split(";")]
    check("Content-Type is Prometheus text format",
          bool(content_type_parts) and content_type_parts[0] == "text/plain" and
          "version=0.0.4" in content_type_parts[1:],
          f"({content_type})")
    check("all metric lines parse", not invalid, f"({invalid[:3]})")
    for family in (
        "qc_queue_depth",
        "qc_queue_oldest_age_seconds",
        "qc_dead_letters",
        "qc_dedup_keys",
        "qc_enqueue_total",
        "qc_delivery_total",
        "qc_delivery_errors_total",
        "qc_watchdog_total",
    ):
        check(f"{family} has HELP", family in helps)
        check(f"{family} has TYPE", family in metric_types)
    check("queue depth is a gauge", metric_types.get("qc_queue_depth") == "gauge")
    check("enqueue total is a counter", metric_types.get("qc_enqueue_total") == "counter")
    check("delivery total is a counter", metric_types.get("qc_delivery_total") == "counter")

    initial_state = edge_status()
    check("queue pending gauge matches /qc/status",
          metric_value(samples, "qc_queue_depth", status="pending") ==
          float(initial_state["queue"]["pending"]))
    check("queue sending gauge matches /qc/status",
          metric_value(samples, "qc_queue_depth", status="sending") ==
          float(initial_state["queue"]["sending"]))
    check("dead-letter gauge matches /qc/status",
          metric_value(samples, "qc_dead_letters") ==
          float(initial_state["deadLetters"]["total"]))
    check("dedup gauge matches /qc/status",
          metric_value(samples, "qc_dedup_keys", kind="ng_fingerprint") ==
          float(initial_state["dedup"]["ngFingerprints"]))

    check("restore MES ok mode", inject("inj_ok") and edge_status()["mesMode"] == "ok")
    client = await create_secure_client()
    async with client:
        idx = await client.get_namespace_index(NS_URI)
        root = await client.nodes.objects.get_child([f"{idx}:QCStation"])
        status_node = await root.get_child([f"{idx}:DeviceStatus"])
        work_order_node = await root.get_child([f"{idx}:CurrentWorkOrderId"])
        check("test starts idle", await ensure_idle(root, idx, status_node, work_order_node))
        check("queue drains before metrics baseline", await wait_until(
            lambda: edge_status()["queue"]["total"] == 0,
            timeout=35,
            description="queue drain before metrics baseline",
        ))

        baseline, _, _, _, _ = fetch_metrics()
        baseline_ng_enqueued = metric_value(baseline, "qc_enqueue_total", type="ng", outcome="accepted")
        baseline_ng_success = metric_value(baseline, "qc_delivery_total", type="ng", outcome="success")
        baseline_done_enqueued = metric_value(
            baseline, "qc_enqueue_total", type="complete", outcome="accepted"
        )
        baseline_done_success = metric_value(
            baseline, "qc_delivery_total", type="complete", outcome="success"
        )

        work_order = "WO-METRICS-" + datetime.now().strftime("%Y%m%d%H%M%S")
        started = await root.call_method(f"{idx}:StartInspection", work_order, 5)
        check("StartInspection accepted", bool(started and started[0] is True),
              f"({started[1] if len(started) > 1 else ''})")
        check("NG enqueue counter advances", await wait_until(
            lambda: metric_value(fetch_metrics()[0], "qc_enqueue_total", type="ng", outcome="accepted")
            > baseline_ng_enqueued,
            timeout=35,
            description="NG enqueue metric",
        ))
        check("NG delivery success counter advances", await wait_until(
            lambda: metric_value(fetch_metrics()[0], "qc_delivery_total", type="ng", outcome="success")
            > baseline_ng_success,
            timeout=35,
            description="NG success metric",
        ))

        completed = await root.call_method(f"{idx}:CompleteWorkOrder", work_order)
        check("CompleteWorkOrder accepted", bool(completed and completed[0] is True),
              f"({completed[1] if len(completed) > 1 else ''})")
        check("completion enqueue counter advances", await wait_until(
            lambda: metric_value(fetch_metrics()[0], "qc_enqueue_total", type="complete", outcome="accepted")
            > baseline_done_enqueued,
            timeout=35,
            description="completion enqueue metric",
        ))
        check("completion delivery success counter advances", await wait_until(
            lambda: metric_value(fetch_metrics()[0], "qc_delivery_total", type="complete", outcome="success")
            > baseline_done_success,
            timeout=35,
            description="completion success metric",
        ))
        check("closed-loop queue drains", await wait_until(
            lambda: edge_status()["queue"]["total"] == 0,
            timeout=35,
            description="queue drain after metrics closure",
        ))

        final_state = edge_status()
        final_samples, _, _, final_invalid, _ = fetch_metrics()
        check("final metrics still parse", not final_invalid)
        check("final pending gauge matches /qc/status",
              metric_value(final_samples, "qc_queue_depth", status="pending") ==
              float(final_state["queue"]["pending"]))
        check("final dead-letter gauge matches /qc/status",
              metric_value(final_samples, "qc_dead_letters") ==
              float(final_state["deadLetters"]["total"]))
        check("final work-order dedup gauge matches /qc/status",
              metric_value(final_samples, "qc_dedup_keys", kind="work_order") ==
              float(final_state["dedup"]["completedWorkOrders"]))
        completion_request = (final_state.get("lastRequestByType") or {}).get("complete") or {}
        check("completion correlationId remains observable",
              str(completion_request.get("correlationId") or "").startswith("corr-"),
              f"({completion_request.get('correlationId') or 'empty'})")
        final_response = final_state.get("lastResponse") or {}
        check("response contract remains observable after metrics scrape",
              final_response.get("status") == "SUCCESS" and
              str(final_response.get("correlationId") or "").startswith("corr-"),
              f"({final_response})")
        check("legacy /qc/status contract still exposes queue/dead/dedup",
              {"queue", "deadLetters", "dedup"}.issubset(final_state))

    passed = sum(1 for _, value in results if value)
    print(f"\n===== METRICS: {passed}/{len(results)} passed =====")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
