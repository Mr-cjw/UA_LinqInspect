#!/usr/bin/env python3
"""Hardware-free five-minute demo for the Edge Core delivery state machine.

The queue, state transitions, persistence, public API and worker are the real
``edge_core`` implementation. Only the upstream MES and the OPC UA writeback are
local simulators, because this demonstration intentionally needs no hardware.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from edge_core.api import create_api_server
from edge_core.store import STATE_DONE, DeliveryStore
from edge_core.worker import DeliveryWorker

DEMO_STEP_COUNT = 9
MAX_VISION_REQUEST_BYTES = 12 * 1024 * 1024


def _classify_vision_request(body: dict[str, Any]) -> dict[str, Any]:
    """Run the real local vision model against one API request."""
    from vision_demo import classify_base64, classify_sample

    sample = body.get("sample")
    if isinstance(sample, str) and sample.strip():
        return classify_sample(sample.strip())
    encoded = body.get("imageBase64")
    if isinstance(encoded, str) and encoded.strip():
        return classify_base64(encoded)
    raise ValueError("请提供 sample 或 imageBase64")


@dataclass
class DemoStep:
    number: int
    title: str
    detail: str
    state: str = ""
    tone: str = "info"
    facts: dict[str, str] = field(default_factory=dict)


@dataclass
class DemoResult:
    scenario: str
    startedAt: str
    finishedAt: str
    deliveryId: str
    correlationId: str
    businessKey: str
    serialNumber: str
    finalState: str
    attempts: int
    mesReceiptNo: str
    uaWriteConfirmed: bool
    queueOpen: int
    deadLetterHistory: int
    mesSuccessfulReceipts: int
    databasePath: str
    steps: list[dict[str, Any]]
    timeline: list[dict[str, Any]]
    status: dict[str, Any]


class _MesHandler(BaseHTTPRequestHandler):
    server_version = "DemoMES/0.1"

    def log_message(self, _fmt: str, *_args: Any) -> None:
        return

    @property
    def simulator(self) -> "MesSimulator":
        return self.server.simulator  # type: ignore[attr-defined]

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {}
        if not isinstance(body, dict):
            body = {}

        mode = self.simulator.record_request(
            path=self.path,
            idempotency_key=self.headers.get("Idempotency-Key", ""),
            correlation_id=str(body.get("correlationId") or ""),
        )
        if mode == "timeout":
            time.sleep(0.6)
            try:
                self.send_response(504)
                self.send_header("content-length", "0")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if mode == "unavailable":
            self._send_json(
                503,
                {
                    "accepted": False,
                    "errorCode": "MES_UNAVAILABLE",
                    "error": "模拟 MES 维护中（503）",
                },
            )
            return

        receipt = self.simulator.success_receipt(body)
        event_type = str(body.get("eventType") or "")
        self._send_json(
            200,
            {
                "accepted": True,
                "schemaVersion": "1.0",
                "correlationId": body.get("correlationId"),
                "receiptNo": receipt["receiptNo"],
                "mesOrderNo": receipt["receiptNo"] if event_type == "NG_ORDER" else None,
                "duplicated": receipt["duplicated"],
            },
        )

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


class MesSimulator:
    """Small idempotent HTTP MES used only by the demonstration."""

    def __init__(self) -> None:
        self.mode = "ok"
        self.requests: list[dict[str, str]] = []
        self._receipts: dict[str, tuple[str, bool]] = {}
        self._lock = threading.Lock()
        self._sequence = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _MesHandler)
        self.server.daemon_threads = True
        self.server.simulator = self  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, name="demo-mes", daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def set_mode(self, mode: str) -> None:
        with self._lock:
            self.mode = mode

    def record_request(self, *, path: str, idempotency_key: str, correlation_id: str) -> str:
        with self._lock:
            self.requests.append(
                {
                    "path": path,
                    "idempotencyKey": idempotency_key,
                    "correlationId": correlation_id,
                }
            )
            return self.mode

    def success_receipt(self, body: dict[str, Any]) -> dict[str, Any]:
        key = str(body.get("sourceEventId") or body.get("correlationId") or "")
        with self._lock:
            existing = self._receipts.get(key)
            if existing:
                return {"receiptNo": existing[0], "duplicated": True}
            self._sequence += 1
            receipt_no = f"MES-JX-DEMO-{self._sequence:04d}"
            self._receipts[key] = (receipt_no, False)
            return {"receiptNo": receipt_no, "duplicated": False}

    def successful_receipt_count(self) -> int:
        with self._lock:
            return sum(1 for _receipt, duplicated in self._receipts.values() if not duplicated)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class EdgeService:
    """Run the real API server around the real SQLite delivery store."""

    def __init__(self, db_path: Path, mes_base_url: str, api_token: str) -> None:
        self.store = DeliveryStore(db_path)
        self.server = create_api_server("127.0.0.1", 0, self.store, api_token, mes_base_url)
        self.server.daemon_threads = True
        self.server.quiet = True  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, name="demo-edge-api", daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def _http_json(
    method: str,
    url: str,
    *,
    token: str = "",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request_headers = {"accept": "application/json"}
    if body is not None:
        request_headers["content-type"] = "application/json"
    if token:
        request_headers["authorization"] = f"Bearer {token}"
    request_headers.update(headers or {})
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = _decode_json(response.read())
            return response.status, dict(response.headers.items()), payload
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, dict(exc.headers.items()), _decode_json(exc.read())
        finally:
            exc.close()


def _decode_json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _make_event() -> dict[str, Any]:
    correlation_id = f"corr-jx-demo-{uuid.uuid4()}"
    return {
        "schemaVersion": "1.0",
        "sourceEventId": "QC-JX-DEMO-0001",
        "stationId": "JX-LINQIN-STATION-01",
        "eventType": "NG_ORDER",
        "occurredAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "result": "NG",
        "serialNumber": "SN-JX-20261121-0001",
        "workOrderId": "WO-JX-20261121-001",
        "defectType": "SURFACE_SCRATCH",
        "confidence": 0.97,
        "imageId": "IMG-JX-DEMO-0001",
        "correlationId": correlation_id,
        "metadata": {
            "source": "hardware-free-demo",
            "product": "手机中框",
            "line": "江西演示线",
        },
    }


def _worker(store: DeliveryStore, mes_base_url: str) -> DeliveryWorker:
    return DeliveryWorker(
        store,
        mes_base_url=mes_base_url,
        max_attempts=3,
        retry_delays=(0.08, 0.08, 0.08),
        request_timeout=0.2,
        poll_interval=0.05,
        logger=lambda *_args, **_kwargs: None,
    )


def _timeline(db_path: Path, delivery_id: str) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT sequence, from_state, to_state, detail_json, created_at
            FROM delivery_events
            WHERE delivery_id = ?
            ORDER BY sequence ASC
            """,
            (delivery_id,),
        ).fetchall()
    timeline: list[dict[str, Any]] = []
    for row in rows:
        detail = ""
        if row["detail_json"]:
            try:
                parsed = json.loads(row["detail_json"])
                detail = str(parsed.get("detail") or parsed)
            except (json.JSONDecodeError, AttributeError):
                detail = str(row["detail_json"])
        timeline.append(
            {
                "from": row["from_state"] or "新建",
                "to": row["to_state"],
                "detail": detail,
                "at": row["created_at"],
            }
        )
    return timeline


def run_demo(progress: Callable[[DemoStep], None] | None = None) -> DemoResult:
    """Run the real Edge Core through a deterministic failure/recovery story."""

    started = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = REPO_ROOT / ".tmp" / f"demo-edge-core-{stamp}-{uuid.uuid4().hex[:6]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    db_path = run_dir / "edge-core.sqlite3"
    token = f"demo-{uuid.uuid4()}"
    event = _make_event()
    delivery_key = str(uuid.uuid4())
    steps: list[DemoStep] = []

    def step(
        title: str,
        detail: str,
        *,
        state: str = "",
        tone: str = "info",
        facts: dict[str, str] | None = None,
    ) -> None:
        item = DemoStep(
            number=len(steps) + 1,
            title=title,
            detail=detail,
            state=state,
            tone=tone,
            facts=facts or {},
        )
        steps.append(item)
        if progress:
            progress(item)

    mes = MesSimulator()
    edge: EdgeService | None = None
    try:
        mes.set_mode("timeout")
        edge = EdgeService(db_path, mes.base_url, token)

        step(
            "质检站产生一条 NG",
            "视觉或人工质检发现缺陷，结果必须先可靠进入 MES，而不是只留在本地日志里。",
            facts={
                "SN": event["serialNumber"],
                "工单": event["workOrderId"],
                "缺陷": event["defectType"],
                "correlationId": event["correlationId"],
            },
        )

        status, headers, delivery = _http_json(
            "POST",
            edge.base_url + "/v1/inspection-events",
            token=token,
            body=event,
            headers={"x-correlation-id": event["correlationId"], "idempotency-key": delivery_key},
        )
        if status != 202:
            raise RuntimeError(f"ingest failed: HTTP {status}: {delivery}")
        delivery_id = str(delivery["deliveryId"])
        step(
            "写入边缘队列：先落盘，再发送",
            "API 已把事件事务化写入 SQLite Outbox。此时 MES 是否在线，都不会让这条 NG 消失。",
            state=str(delivery["state"]),
            tone="ok",
            facts={
                "HTTP": str(status),
                "deliveryId": delivery_id,
                "businessKey": str(delivery["businessKey"]),
            },
        )

        status, replay_headers, duplicate = _http_json(
            "POST",
            edge.base_url + "/v1/inspection-events",
            token=token,
            body=event,
            headers={"x-correlation-id": event["correlationId"], "idempotency-key": delivery_key},
        )
        if status != 202 or duplicate["deliveryId"] != delivery_id:
            raise RuntimeError("idempotent replay did not return the original delivery")
        _, _, page = _http_json(
            "GET",
            edge.base_url + "/v1/deliveries?limit=50",
            token=token,
        )
        if len(page.get("items", [])) != 1:
            raise RuntimeError("duplicate event created more than one delivery")
        replay_flag = replay_headers.get("Idempotent-Replay", replay_headers.get("idempotent-replay", ""))
        step(
            "同一 NG 被重复上报，业务队列仍只有 1 条",
            "来源系统重发消息时，幂等键命中已有任务；不会向 MES 重复开单。",
            state="PENDING",
            tone="ok",
            facts={
                "Idempotent-Replay": replay_flag,
                "队列任务数": str(len(page.get("items", []))),
                "deliveryId 未变化": delivery_id == duplicate["deliveryId"] and "是" or "否",
            },
        )

        worker = _worker(edge.store, mes.base_url)
        worker.run_once()
        _, _, current = _http_json(
            "GET", edge.base_url + f"/v1/deliveries/{delivery_id}", token=token
        )
        if current["state"] != "RETRY_WAIT" or current["lastErrorCode"] != "TRANSPORT_ERROR":
            raise RuntimeError(f"unexpected timeout transition: {current}")
        step(
            "MES 超时：任务保留，自动退避",
            "第一次投递遇到传输超时。任务没有丢，也没有被误判为成功，仍保留原业务键等待重试。",
            state=str(current["state"]),
            tone="warn",
            facts={
                "attempts": str(current["attempts"]),
                "lastErrorCode": str(current["lastErrorCode"]),
                "下一次重试": str(current["nextAttemptAt"]),
            },
        )

        edge.close()
        edge = EdgeService(db_path, mes.base_url, token)
        _, _, restored = _http_json(
            "GET", edge.base_url + f"/v1/deliveries/{delivery_id}", token=token
        )
        if restored["state"] != "RETRY_WAIT" or restored["deliveryId"] != delivery_id:
            raise RuntimeError("delivery did not survive service restart")
        step(
            "边缘服务重启：任务从 SQLite 原样恢复",
            "这里真的关闭并重新创建了 Edge Core 服务。恢复的不是内存，而是同一份持久化队列。",
            state=str(restored["state"]),
            tone="ok",
            facts={
                "新 API 端口": edge.base_url.rsplit(":", 1)[-1],
                "attempts": str(restored["attempts"]),
                "businessKey": str(restored["businessKey"]),
            },
        )

        mes.set_mode("unavailable")
        worker = _worker(edge.store, mes.base_url)
        time.sleep(0.12)
        worker.run_once()
        time.sleep(0.12)
        worker.run_once()
        _, _, dead = _http_json(
            "GET", edge.base_url + f"/v1/deliveries/{delivery_id}", token=token
        )
        if dead["state"] != "DEAD":
            raise RuntimeError(f"retry budget did not reach DEAD: {dead}")
        _, _, dead_page = _http_json("GET", edge.base_url + "/v1/dead-letters", token=token)
        if len(dead_page.get("items", [])) != 1:
            raise RuntimeError("dead letter was not recorded")
        step(
            "MES 持续故障：重试耗尽，进入死信",
            "系统没有无限重试，也没有静默丢弃。任务进入 DEAD，保留错误码、尝试次数和人工处置入口。",
            state=str(dead["state"]),
            tone="dead",
            facts={
                "attempts": str(dead["attempts"]),
                "lastErrorCode": str(dead["lastErrorCode"]),
                "死信数": str(len(dead_page.get("items", []))),
            },
        )

        mes.set_mode("ok")
        _, _, replayed = _http_json(
            "POST",
            edge.base_url + f"/v1/dead-letters/{delivery_id}/replay",
            token=token,
            body={"actor": "江西示范线-值班员", "reason": "模拟 MES 已恢复，人工确认重投"},
        )
        if replayed["state"] != "PENDING" or replayed["businessKey"] != dead["businessKey"]:
            raise RuntimeError("dead-letter replay changed the business key")
        step(
            "MES 恢复：人工重放死信，原业务主键不变",
            "重放不是新建订单。operator、原因和时间均进入审计轨迹，避免人工操作变成新的重复业务。",
            state=str(replayed["state"]),
            tone="ok",
            facts={
                "businessKey": str(replayed["businessKey"]),
                "操作人": "江西示范线-值班员",
                "重放原因": "模拟 MES 已恢复，人工确认重投",
            },
        )

        worker.run_once()
        _, _, acked = _http_json(
            "GET", edge.base_url + f"/v1/deliveries/{delivery_id}", token=token
        )
        if acked["state"] != "UA_WRITE_PENDING" or not acked.get("mesReceipt"):
            raise RuntimeError(f"MES receipt was not accepted: {acked}")
        step(
            "MES 受理成功：回执先校验，再等待写回",
            "只有 accepted、schema、correlationId 和 receiptNo 全部匹配，任务才进入 UA 写回阶段。",
            state=str(acked["state"]),
            tone="ok",
            facts={
                "receiptNo": str(acked["mesReceipt"]["receiptNo"]),
                "MES 成功业务回执": str(mes.successful_receipt_count()),
                "UA 写回确认": "尚未确认",
            },
        )

        _, _, done = _http_json(
            "POST",
            edge.base_url + f"/v1/deliveries/{delivery_id}/ua-write-result",
            token=token,
            body={
                "success": True,
                "correlationId": event["correlationId"],
                "actor": "demo-opcua-adapter",
                "note": "模拟 OPC UA 回执写回成功",
            },
        )
        if done["state"] != STATE_DONE or not done["uaWriteConfirmed"]:
            raise RuntimeError(f"UA confirmation did not finish delivery: {done}")
        _, _, status_payload = _http_json("GET", edge.base_url + "/v1/status", token=token)
        step(
            "UA 写回确认：一条 NG 的完整闭环完成",
            "MES 已有唯一回执，OPC UA 已收到写回确认，任务进入终态 DONE。",
            state=str(done["state"]),
            tone="done",
            facts={
                "uaWriteConfirmed": "是",
                "未完成队列": str(status_payload["queue"]["total"]),
                "回执号": str(done["mesReceipt"]["receiptNo"]),
                "累计尝试": str(done["attempts"]),
                "最终状态": str(done["state"]),
            },
        )

        timeline = _timeline(db_path, delivery_id)
        if not timeline:
            raise RuntimeError("delivery audit timeline is empty")
        return DemoResult(
            scenario="NG 质检结果 → 故障恢复 → MES 回执 → UA 写回",
            startedAt=started,
            finishedAt=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            deliveryId=delivery_id,
            correlationId=str(event["correlationId"]),
            businessKey=str(done["businessKey"]),
            serialNumber=str(event["serialNumber"]),
            finalState=str(done["state"]),
            attempts=int(done["attempts"]),
            mesReceiptNo=str(done["mesReceipt"]["receiptNo"]),
            uaWriteConfirmed=bool(done["uaWriteConfirmed"]),
            queueOpen=int(status_payload["queue"]["total"]),
            deadLetterHistory=int(status_payload["deadLetters"]["total"]),
            mesSuccessfulReceipts=mes.successful_receipt_count(),
            databasePath=str(db_path),
            steps=[asdict(item) for item in steps],
            timeline=timeline,
            status=status_payload,
        )
    finally:
        if edge is not None:
            edge.close()
        mes.close()


def _render_markdown(result: DemoResult) -> str:
    lines = [
        "# UA_LinqInspect 五分钟软件体验记录",
        "",
        f"- 生成时间：{result.finishedAt}",
        f"- 场景：{result.scenario}",
        f"- 最终状态：**{result.finalState}**",
        f"- 唯一业务回执：`{result.mesReceiptNo}`",
        f"- UA 写回确认：{'是' if result.uaWriteConfirmed else '否'}",
        f"- 未完成队列：{result.queueOpen}",
        f"- 历史死信：{result.deadLetterHistory}（已人工重放）",
        f"- correlationId：`{result.correlationId}`",
        f"- deliveryId：`{result.deliveryId}`",
        f"- SQLite：`{result.databasePath}`",
        "",
        "## 发生了什么",
        "",
    ]
    for item in result.steps:
        lines.append(f"{item['number']}. **{item['title']}** ({item['state'] or item['tone']})")
        lines.append(f"   {item['detail']}")
        if item["facts"]:
            facts = "；".join(f"{key}={value}" for key, value in item["facts"].items())
            lines.append(f"   `{facts}`")
    lines.extend(["", "## 状态审计轨迹", "", "| 时间 | 从 | 到 | 说明 |", "|---|---|---|---|"])
    for event in result.timeline:
        detail = str(event["detail"]).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {event['at']} | {event['from']} | {event['to']} | {detail} |")
    lines.extend(
        [
            "",
            "## 说明",
            "",
            "- 队列、状态机、HTTP API、重试、死信和 SQLite 持久化均为项目真实 `edge_core` 代码。",
            "- MES 与 OPC UA 写回为本地模拟，不冒充真实客户系统对接。",
            "- 系统承诺 at-least-once + 业务幂等，不承诺 exactly-once。",
            "",
        ]
    )
    return "\n".join(lines)


class _Color:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    BLUE = "\033[34m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    CYAN = "\033[36m"


def _terminal_reporter(use_color: bool) -> Callable[[DemoStep], None]:
    color = _Color if use_color else None

    def paint(text: str, code: str) -> str:
        return f"{code}{text}{_Color.RESET}" if color else text

    def report(item: DemoStep) -> None:
        tone_code = {
            "ok": _Color.GREEN,
            "warn": _Color.YELLOW,
            "dead": _Color.RED,
            "done": _Color.GREEN,
        }.get(item.tone, _Color.CYAN)
        print()
        print(paint(f"[{item.number}/{DEMO_STEP_COUNT}] {item.title}", _Color.BOLD))
        print(f"  {item.detail}")
        if item.state:
            print(f"  状态：{paint(item.state, tone_code)}")
        for key, value in item.facts.items():
            print(f"  {paint(key + ':', _Color.DIM)} {value}")

    return report


def _print_summary(result: DemoResult) -> None:
    line = "=" * 72
    print()
    print(line)
    print("演示结论：硬件还没到，最值钱的可靠性已经可以先验证。")
    print(line)
    print(f"一条 NG：{result.serialNumber}")
    print("经历：MES 超时 → 服务重启 → MES 503 → 3 次失败 → 死信 → 人工重放 → 第 4 次成功")
    print(f"累计尝试：{result.attempts}")
    print(f"MES 唯一业务回执：{result.mesReceiptNo}")
    print(f"UA 写回确认：{'是' if result.uaWriteConfirmed else '否'}")
    print(f"未完成任务：{result.queueOpen}    历史死信：{result.deadLetterHistory}（已重放）")
    print(f"correlationId：{result.correlationId}")
    print(f"SQLite：{result.databasePath}")
    print()
    print("这不是再做一套 AI 视觉，而是让视觉/人工质检的结果可靠进入 MES：")
    print("不丢、不重、可对账；出故障能看见，人工处理后还能回到原业务键继续。")
    print(line)


_DASHBOARD = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UA_LinqInspect · 五分钟软件体验</title>
<style>
:root{--bg:#07111f;--panel:#0d1c30;--panel2:#12243b;--line:#24405f;--text:#e8f1fb;--muted:#8da5bd;--ok:#30d48a;--warn:#f4b740;--dead:#ff6b6b;--blue:#58a6ff;--cyan:#62d9ff}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 15% 0%,#143154 0,transparent 38%),var(--bg);color:var(--text);font-family:"Noto Sans SC","Microsoft YaHei",system-ui,sans-serif;min-height:100vh}
.wrap{max-width:1180px;margin:0 auto;padding:38px 24px 64px}.eyebrow{color:var(--cyan);font-size:13px;letter-spacing:.16em;text-transform:uppercase}.hero{display:flex;justify-content:space-between;gap:24px;align-items:flex-end;border-bottom:1px solid var(--line);padding-bottom:24px;margin-bottom:24px}
h1{font-size:42px;line-height:1.08;margin:10px 0 12px;letter-spacing:-.03em}.hero p{color:var(--muted);font-size:16px;line-height:1.75;max-width:760px;margin:0}.badge{border:1px solid #2b5d85;background:#0b2740;color:var(--cyan);border-radius:999px;padding:8px 13px;font-size:13px;white-space:nowrap}
.workflow{margin:0 0 24px;border:1px solid var(--line);border-radius:16px;padding:20px;background:linear-gradient(180deg,#0d2035cc,#091727cc)}.workflow-head{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:15px}.workflow-head h2{margin:5px 0 0;font-size:20px;letter-spacing:-.02em}.workflow-state{border:1px solid #28664d;background:#0b2a20;color:#7cf0ba;border-radius:999px;padding:6px 10px;font-size:11px;white-space:nowrap}.lanes{display:grid;grid-template-columns:1fr 1fr;gap:13px}.lane{border:1px solid #1d3a58;border-radius:12px;padding:14px;background:#091a2b}.lane.primary{border-color:#2b5d85;box-shadow:inset 0 0 0 1px #163b5c}.lane.context{border-style:dashed}.lane-tag{font-size:11px;font-weight:800;color:var(--cyan);letter-spacing:.05em;margin-bottom:11px}.lane.context .lane-tag{color:var(--warn)}.chain{display:flex;gap:6px;align-items:center;flex-wrap:wrap}.chain span{border:1px solid #27445f;background:#0d2137;border-radius:7px;padding:6px 8px;font-size:11px;font-weight:700}.chain small{display:block;color:var(--muted);font-size:9px;font-weight:400;margin-top:2px}.chain i{color:#4d7da8;font-style:normal;font-size:12px}.lane p{color:var(--muted);font-size:11px;line-height:1.65;margin:11px 0 0}.join{display:flex;align-items:center;gap:10px;margin-top:13px;border-top:1px solid #1b3550;padding-top:12px;color:var(--muted);font-size:11px;line-height:1.6}.join b{color:var(--text);white-space:nowrap}.join span{color:var(--cyan)}
.toolbar{display:flex;align-items:center;gap:14px;margin-bottom:22px;flex-wrap:wrap}.run{border:0;border-radius:10px;background:linear-gradient(135deg,#2d8cff,#43d8c0);color:#03111e;padding:13px 20px;font-weight:800;font-size:15px;cursor:pointer;box-shadow:0 8px 30px #1973c855}.run:disabled{opacity:.55;cursor:wait}.jointlink{border:1px solid #336a96;background:#0a2137;color:var(--cyan);border-radius:10px;padding:12px 16px;text-decoration:none;font-size:13px;font-weight:700}.hint{color:var(--muted);font-size:13px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:24px 0}.stat{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:14px;padding:16px 18px}.stat .label{color:var(--muted);font-size:12px;margin-bottom:8px}.stat .value{font-size:26px;font-weight:800;letter-spacing:-.02em}.stat .sub{color:var(--muted);font-size:11px;margin-top:5px}
.layout{display:grid;grid-template-columns:1.55fr .9fr;gap:20px}.panel{background:#0a1829cc;border:1px solid var(--line);border-radius:16px;padding:22px;backdrop-filter:blur(8px)}.panel h2{margin:0 0 18px;font-size:18px}.timeline{position:relative}.timeline:before{content:"";position:absolute;left:16px;top:8px;bottom:8px;width:2px;background:linear-gradient(var(--blue),var(--ok))}.step{position:relative;padding:0 0 20px 48px}.step:last-child{padding-bottom:0}.dot{position:absolute;left:7px;top:4px;width:20px;height:20px;border-radius:50%;background:var(--panel);border:2px solid var(--blue);display:grid;place-items:center;font-size:10px;font-weight:800;z-index:1}.step.ok .dot{border-color:var(--ok);color:var(--ok)}.step.warn .dot{border-color:var(--warn);color:var(--warn)}.step.dead .dot{border-color:var(--dead);color:var(--dead)}.step.done .dot{border-color:var(--ok);background:var(--ok);color:#04121c}.step h3{margin:0 0 6px;font-size:15px}.step p{margin:0;color:var(--muted);line-height:1.65;font-size:13px}.facts{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}.fact{font-size:11px;border:1px solid var(--line);background:#0c1d2f;border-radius:6px;padding:4px 7px;color:#bcd0e4}.pill{display:inline-block;border-radius:999px;padding:3px 8px;font-size:10px;font-weight:800;margin-bottom:7px;background:#102f48;color:var(--cyan)}
.side .card{border-bottom:1px solid var(--line);padding:0 0 16px;margin-bottom:16px}.side .card:last-child{border-bottom:0;margin-bottom:0;padding-bottom:0}.side h3{margin:0 0 9px;font-size:14px}.side p{color:var(--muted);font-size:12px;line-height:1.7;margin:0}.kv{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-bottom:1px dashed #1a3149;font-size:12px}.kv:last-child{border-bottom:0}.kv span:first-child{color:var(--muted)}.kv span:last-child{text-align:right;word-break:break-all}.audit{max-height:330px;overflow:auto}.audit-row{font-size:11px;line-height:1.55;padding:7px 0;border-bottom:1px solid #172b41;color:#b7cce0}.audit-row b{color:var(--text)}.empty{color:var(--muted);font-size:12px;padding:18px 0}.value{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:20px}.value div{border:1px solid var(--line);border-radius:12px;padding:16px;background:linear-gradient(180deg,#0c2035,#0a1727)}.value b{display:block;margin-bottom:7px;font-size:14px}.value span{color:var(--muted);font-size:12px;line-height:1.6}.foot{color:var(--muted);font-size:11px;line-height:1.7;margin-top:22px}.error{color:var(--dead);white-space:pre-wrap}.spin{animation:pulse 1s infinite}@keyframes pulse{50%{opacity:.35}}
@media(max-width:820px){h1{font-size:32px}.hero{display:block}.badge{display:inline-block;margin-top:14px}.workflow-head{display:block}.workflow-state{display:inline-block;margin-top:10px}.lanes{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}.layout{grid-template-columns:1fr}.value{grid-template-columns:1fr}}
</style>
</head>
<body><div class="wrap">
<div class="hero"><div><div class="eyebrow">UA_LinqInspect · Edge Core Demo</div><h1>一条 NG 的最后一公里</h1><p>没有视觉硬件，也能先验证最值钱的部分：质检结果从产生开始，经历 MES 超时、边缘服务重启、持续故障、死信人工重放，最后仍然只有一次业务回执，并且每一步都能用 correlationId 查清楚。</p></div><div class="badge">PoC+ / 软件验证</div></div>
<section class="workflow">
 <div class="workflow-head"><div><div class="eyebrow">PRODUCT WORKFLOW</div><h2>不是一个仪表盘，而是两条可以合并的工作流</h2></div><div class="workflow-state">Node-RED 原始工作流仍在仓库并通过回归</div></div>
 <div class="lanes">
  <div class="lane primary"><div class="lane-tag">质检交付主线 · UA_LinqInspect</div><div class="chain"><span>QC 事件<small>OK / NG / SN</small></span><i>→</i><span>OPC UA<small>语义与权限</small></span><i>→</i><span>Node-RED<small>去重与削峰</small></span><i>→</i><span>Edge Core<small>Outbox / 重试 / 死信</small></span><i>→</i><span>MES 回执<small>唯一业务效果</small></span></div><p>本页按钮跑的就是这条真实链路：真实 API、SQLite、Worker、状态机；MES 和 UA 写回为本地模拟。</p></div>
  <div class="lane context"><div class="lane-tag">设备上下文 · RPi4 OEE / RCA Gateway</div><div class="chain"><span>模拟 PLC<small>运行 / 停机</small></span><i>→</i><span>OPC UA 采集<small>产量 / 温度 / 报警</small></span><i>→</i><span>Gateway<small>结构化事件</small></span><i>→</i><span>OEE / RCA<small>损失分解与候选根因</small></span></div><p>另一个仓库已经能独立产出设备状态、GoodCount、CurrentOutput、温度与报警；当前尚未与本页左侧共享事件库和时间轴。</p></div>
 </div>
 <div class="join"><b>联合价值</b><span>质量事件 + 设备上下文 → 一次对账说清“这批 NG 与哪次停机/温度异常同时发生，MES 是否已收到”。</span></div>
</section>
<div class="toolbar"><button id="run" class="run">▶ 运行五分钟演示</button><a class="jointlink" href="/vision" style="border-color:#2a6952;color:#8ce9c2">AI 质检真实推理 →</a><a class="jointlink" href="/joint">运行双项目联合演示 →</a><a class="jointlink" href="/overview" style="border-color:#7c622f;color:#f1c56f">项目总览与现场演示 →</a><span id="hint" class="hint">点击后自动跑完 9 个真实状态迁移</span></div>
<div class="stats">
 <div class="stat"><div class="label">最终业务状态</div><div id="final" class="value">待运行</div><div class="sub">MES 回执 + UA 写回都完成才 DONE</div></div>
 <div class="stat"><div class="label">未完成任务</div><div id="queue" class="value">-</div><div class="sub">PENDING / SENDING / RETRY_WAIT</div></div>
 <div class="stat"><div class="label">历史死信</div><div id="dead" class="value">-</div><div class="sub">保留审计记录，可人工重放</div></div>
 <div class="stat"><div class="label">MES 唯一回执</div><div id="receipt" class="value" style="font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">-</div><div class="sub">同一业务键只产生一次业务效果</div></div>
</div>
<div class="layout">
 <section class="panel"><h2>故障注入与状态流转</h2><div id="timeline" class="timeline"><div class="empty">演示尚未开始。</div></div><div class="value"><div><b>不丢</b><span>先写 SQLite 再投递，服务重启后继续。</span></div><div><b>不重</b><span>业务幂等键 + payload 哈希；只承诺 at-least-once，不吹 exactly-once。</span></div><div><b>可对账</b><span>correlationId、尝试次数、错误码、MES 回执和审计轨迹全部保留。</span></div></div></section>
 <aside class="panel side">
  <div class="card"><h3>本次业务对象</h3><div id="identity" class="empty">待运行</div></div>
  <div class="card"><h3>运行环境</h3><div class="kv"><span>队列 / 状态机</span><span>真实 edge_core</span></div><div class="kv"><span>MES</span><span>本地模拟</span></div><div class="kv"><span>OPC UA 写回</span><span>本地模拟</span></div><div class="kv"><span>硬件</span><span>不需要</span></div></div>
  <div class="card"><h3>审计轨迹</h3><div id="audit" class="audit"><div class="empty">DONE 后显示完整状态历史。</div></div></div>
 </aside>
</div>
<p class="foot">说明：本演示使用项目真实 Edge Core API、SQLite Outbox、Worker、重试与死信逻辑；MES 与 OPC UA 写回为本地模拟，不冒充客户系统。当前定位为 PoC+/付费试点前验证，不宣称生产级或 exactly-once。浏览器关闭后服务可按 Ctrl+C 停止。</p>
</div>
<script>
const $=id=>document.getElementById(id);
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
function esc(s){return String(s??'');}
function addFact(parent,key,value){const n=document.createElement('span');n.className='fact';n.textContent=key+'='+esc(value);parent.appendChild(n);}
function setKV(target,obj){const box=$(target);box.innerHTML='';for(const [k,v] of Object.entries(obj)){const row=document.createElement('div');row.className='kv';const a=document.createElement('span');a.textContent=k;const b=document.createElement('span');b.textContent=esc(v);row.append(a,b);box.appendChild(row);}}
async function run(){
 const btn=$('run');btn.disabled=true;btn.textContent='演示运行中…';$('hint').textContent='正在注入 MES 故障并重启边缘服务，请稍候';$('timeline').innerHTML='<div class="empty spin">正在启动真实 Edge Core 服务…</div>';
 try{
  const res=await fetch('/api/run',{method:'POST'});const data=await res.json();
  if(!res.ok)throw new Error(data.error||('HTTP '+res.status));
  $('timeline').innerHTML='';
  for(const step of data.steps){
   const card=document.createElement('div');card.className='step '+(step.tone||'info');
   const dot=document.createElement('div');dot.className='dot';dot.textContent=step.number;
   const body=document.createElement('div');const pill=document.createElement('div');pill.className='pill';pill.textContent=(step.state||step.tone).toUpperCase();
   const h=document.createElement('h3');h.textContent=step.title;const p=document.createElement('p');p.textContent=step.detail;
   const facts=document.createElement('div');facts.className='facts';for(const [k,v] of Object.entries(step.facts||{}))addFact(facts,k,v);
   body.append(pill,h,p,facts);card.append(dot,body);$('timeline').appendChild(card);await sleep(260);
  }
  $('final').textContent=data.finalState;$('final').style.color='var(--ok)';$('queue').textContent=data.queueOpen;$('dead').textContent=data.deadLetterHistory;$('receipt').textContent=data.mesReceiptNo;
  setKV('identity',{'SN':data.serialNumber,'工单':'WO-JX-20261121-001','deliveryId':data.deliveryId,'correlationId':data.correlationId,'businessKey':data.businessKey,'SQLite':data.databasePath});
  const audit=$('audit');audit.innerHTML='';for(const e of data.timeline){const row=document.createElement('div');row.className='audit-row';const b=document.createElement('b');b.textContent=e.from+' → '+e.to;row.append(b,document.createElement('br'),document.createTextNode((e.detail?e.detail+' · ':'')+e.at));audit.appendChild(row);}
  $('hint').textContent='演示完成：1 条 NG，3 次失败后进入死信，服务重启后恢复，人工重放后第 4 次成功。';btn.textContent='↻ 重新运行演示';btn.disabled=false;window.scrollTo({top:0,behavior:'smooth'});
 }catch(e){$('timeline').innerHTML='<div class="error">演示失败：'+esc(e.message)+'</div>';$('hint').textContent='演示失败，请查看终端日志';btn.textContent='重新运行演示';btn.disabled=false;}
}
$('run').addEventListener('click',run);window.addEventListener('load',()=>setTimeout(run,350));
</script></body></html>"""


class _DashboardHandler(BaseHTTPRequestHandler):
    server_version = "UA_LinqDemoUI/0.1"

    def log_message(self, _fmt: str, *_args: Any) -> None:
        return

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            body = _DASHBOARD.encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/joint":
            from demo_joint_oee_rca import JOINT_DASHBOARD

            body = JOINT_DASHBOARD.encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/overview":
            from project_overview import PROJECT_OVERVIEW_DASHBOARD

            body = PROJECT_OVERVIEW_DASHBOARD.encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/vision":
            from vision_demo import VISION_DASHBOARD

            body = VISION_DASHBOARD.encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in {"/api/joint", "/api/overview"}:
            lock: threading.Lock = self.server.joint_lock  # type: ignore[attr-defined]
            if not lock.acquire(blocking=False):
                self._json(409, {"error": "已有联合演示正在运行"})
                return
            try:
                from demo_joint_oee_rca import JointDemoUnavailable, run_joint_demo

                result = run_joint_demo()
                self._json(200, asdict(result))
            except JointDemoUnavailable as exc:
                self._json(503, {"error": str(exc)})
            except Exception as exc:
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            finally:
                lock.release()
            return
        if path == "/api/vision/classify":
            lock = self.server.vision_lock  # type: ignore[attr-defined]
            if not lock.acquire(blocking=False):
                self._json(409, {"error": "已有视觉推理正在运行"})
                return
            try:
                vision = _classify_vision_request(self._read_json_body())
                self._json(200, vision)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            finally:
                lock.release()
            return
        if path == "/api/vision/run":
            lock = self.server.joint_lock  # type: ignore[attr-defined]
            if not lock.acquire(blocking=False):
                self._json(409, {"error": "已有联合演示正在运行"})
                return
            try:
                vision = _classify_vision_request(self._read_json_body())
                if vision["decision"] != "NG":
                    if vision.get("qualityGate", {}).get("status") != "PASS":
                        message = "本次图像质量门禁未通过，已转人工复核，未生成 MES 工单。"
                    elif vision["decision"] == "OK":
                        message = "本次推理为 OK，未生成 MES 工单。"
                    else:
                        message = "本次置信度低于自动开单阈值，已转人工复核，未生成 MES 工单。"
                    self._json(200, {"vision": vision, "delivery": None, "message": message})
                    return
                from demo_joint_oee_rca import JointDemoUnavailable, run_joint_demo

                result = run_joint_demo(vision_result=vision)
                self._json(200, {"vision": vision, "delivery": asdict(result)})
            except JointDemoUnavailable as exc:
                self._json(503, {"error": str(exc)})
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except Exception as exc:
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            finally:
                lock.release()
            return
        if path != "/api/run":
            self._json(404, {"error": "not found"})
            return
        lock: threading.Lock = self.server.demo_lock  # type: ignore[attr-defined]
        if not lock.acquire(blocking=False):
            self._json(409, {"error": "已有演示正在运行"})
            return
        try:
            result = run_demo()
            self._json(200, asdict(result))
        except Exception as exc:
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            lock.release()

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("请求体长度无效") from exc
        if length <= 0:
            raise ValueError("请求体不能为空")
        if length > MAX_VISION_REQUEST_BYTES:
            raise ValueError("请求体超过 12 MB 限制")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体必须是有效的 JSON") from exc
        if not isinstance(body, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return body

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def run_ui(port: int, open_browser: bool) -> int:
    server = ThreadingHTTPServer(("127.0.0.1", port), _DashboardHandler)
    server.daemon_threads = True
    server.demo_lock = threading.Lock()  # type: ignore[attr-defined]
    server.joint_lock = threading.Lock()  # type: ignore[attr-defined]
    server.vision_lock = threading.Lock()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, name="demo-dashboard", daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print("UA_LinqInspect 五分钟软件体验已启动")
    print(f"浏览器打开：{url}")
    print("按 Ctrl+C 停止。")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        thread.join()
    except KeyboardInterrupt:
        print("\n正在停止演示服务…")
    finally:
        server.shutdown()
        server.server_close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the hardware-free Edge Core demonstration")
    parser.add_argument("--ui", action="store_true", help="open the interactive browser dashboard")
    parser.add_argument("--port", type=int, default=8899, help="dashboard port for --ui")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser automatically")
    parser.add_argument("--report", type=Path, help="write a Markdown evidence report")
    args = parser.parse_args()

    if args.ui:
        return run_ui(args.port, open_browser=not args.no_open)

    if not args.json:
        print("=" * 72)
        print(" UA_LinqInspect · 五分钟软件体验")
        print(" 不接硬件，验证“质检结果可靠进入 MES”的最后一公里")
        print("=" * 72)
        print("核心队列、状态机、HTTP API、重试、死信和持久化均使用项目真实代码。")
        print("MES 与 OPC UA 写回为本地模拟。")

    use_color = sys.stdout.isatty() and not args.no_color
    result = run_demo(None if args.json else _terminal_reporter(use_color))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(_render_markdown(result), encoding="utf-8")
        if not args.json:
            print(f"\n证据报告：{args.report}")
    if args.json:
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    else:
        _print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
