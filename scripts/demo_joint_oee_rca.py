#!/usr/bin/env python3
"""Joint QA delivery and OEE/RCA demonstration across the two project repositories.

The QC delivery path uses the real ``edge_core`` API, SQLite store and worker.
The device context comes from the real OPC UA mock PLC in
``rpi4-opcua-oee-rca-gateway``. Only the downstream MES and UA write-back are
local simulators, matching the hardware-free constraint of the main demo.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from asyncua import Client, ua

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from demo_edge_core import EdgeService, MesSimulator, _http_json, _timeline, _worker
from edge_core.store import STATE_DONE
from vision_demo import DISPLAY_NAMES, classify_sample

NS = "ns=1;s=MockPLC"
NOMINAL_OUTPUT_PER_MIN = 120.0
SAMPLE_INTERVAL_SECONDS = 0.15
RPI_REPO_NAME = "rpi4-opcua-oee-rca-gateway"

NodeLog = Callable[[str], None]


class JointDemoUnavailable(RuntimeError):
    """Raised when the sibling OEE/RCA repository is not runnable."""


@dataclass
class JointStep:
    number: int
    title: str
    detail: str
    tone: str = "info"
    facts: dict[str, str] = field(default_factory=dict)


@dataclass
class JointDemoResult:
    scenario: str
    startedAt: str
    finishedAt: str
    lineId: str
    stationId: str
    workOrderId: str
    serialNumber: str
    correlationId: str
    deliveryId: str
    finalState: str
    mesReceiptNo: str
    attempts: int
    oee: float
    availability: float
    performance: float
    quality: float
    observedSeconds: float
    runningSeconds: float
    downtimeSeconds: float
    goodDelta: int
    ngCount: int
    nominalOutputPerMinute: float
    temperaturePeakC: float
    temperatureAtNgC: float
    alarmCodes: list[str]
    rcaCandidate: str
    rcaConfidence: str
    rcaSummary: str
    rcaEvidence: list[str]
    defectType: str
    defectConfidence: float
    imageId: str
    imageSha256: str
    imageHashScope: str
    visionModelVersion: str
    visionModelSha256: str
    visionDatasetVersion: str
    visionValidationAccuracy: float
    visionValidationScope: str
    visionInferenceMs: float
    steps: list[dict[str, Any]]
    samples: list[dict[str, Any]]
    timeline: list[dict[str, Any]]
    rpiRepo: str


def _rpi_root() -> Path:
    configured = os.environ.get("RPI_GATEWAY_ROOT", "").strip()
    root = Path(configured).expanduser() if configured else REPO_ROOT.parent / RPI_REPO_NAME
    return root.resolve()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class MockPlcProcess:
    """Run the real mock PLC from the sibling repository."""

    def __init__(self, run_dir: Path, logger: NodeLog | None = None) -> None:
        self.root = _rpi_root()
        self.run_dir = run_dir
        self.logger = logger or (lambda _line: None)
        self.port = _free_port()
        self.log_path = run_dir / "mock-plc.log"
        self.process: subprocess.Popen[str] | None = None
        self._log_handle: Any = None

    @property
    def endpoint(self) -> str:
        return f"opc.tcp://127.0.0.1:{self.port}/UA/MockPLC"

    def preflight(self) -> None:
        if not self.root.is_dir():
            raise JointDemoUnavailable(
                f"找不到设备上下文项目：{self.root}。可设置 RPI_GATEWAY_ROOT 指向仓库。"
            )
        if not (self.root / "src" / "index.js").is_file():
            raise JointDemoUnavailable(f"设备上下文项目缺少 src/index.js：{self.root}")
        if not (self.root / "node_modules" / "node-opcua").is_dir():
            raise JointDemoUnavailable(
                f"设备上下文项目尚未安装依赖：{self.root / 'node_modules'}；请先执行 npm install。"
            )
        if shutil.which("node") is None:
            raise JointDemoUnavailable("找不到 node 命令，无法启动模拟 PLC。")

    def start(self) -> None:
        self.preflight()
        env = os.environ.copy()
        env.update(
            {
                "OPCUA_HOST": "127.0.0.1",
                "OPCUA_HOSTNAME": "127.0.0.1",
                "OPCUA_PORT": str(self.port),
                "OPCUA_PKI_ROOT": str(self.run_dir / "pki"),
                "SIM_INTERVAL_MS": "100",
                "SIM_DOWNTIME_RATE_PER_MIN": "0",
                "SIM_TEMP_SPIKE_RATE_PER_MIN": "0",
                "SIM_SEED": "42",
                "SIM_NOMINAL_OUTPUT": str(NOMINAL_OUTPUT_PER_MIN),
                "SIM_YIELD_RATE": "1.0",
            }
        )
        self._log_handle = self.log_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            ["node", "src/index.js"],
            cwd=self.root,
            env=env,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.logger(f"Mock PLC 启动中：{self.endpoint}")

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    def log_tail(self, lines: int = 20) -> str:
        if not self.log_path.exists():
            return ""
        return "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


async def _connect_plc(process: MockPlcProcess, timeout: float = 25.0) -> Client:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if process.process is not None and process.process.poll() is not None:
            raise JointDemoUnavailable(
                "模拟 PLC 提前退出。\n" + process.log_tail()
            )
        client = Client(process.endpoint)
        try:
            await client.connect()
            return client
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            try:
                await client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(0.25)
    raise JointDemoUnavailable(f"无法连接模拟 PLC {process.endpoint}: {last_error}")


async def _read(client: Client, suffix: str) -> Any:
    return await client.get_node(f"{NS}.{suffix}").read_value()


def _alarm_codes(raw: Any) -> list[str]:
    try:
        payload = json.loads(raw) if isinstance(raw, str) else {}
    except json.JSONDecodeError:
        return []
    codes: list[str] = []
    latest = payload.get("latest") if isinstance(payload, dict) else None
    if isinstance(latest, dict) and latest.get("code"):
        codes.append(str(latest["code"]))
    active = payload.get("active") if isinstance(payload, dict) else None
    if isinstance(active, list):
        for item in active:
            if isinstance(item, dict) and item.get("code"):
                codes.append(str(item["code"]))
    return list(dict.fromkeys(codes))


async def _snapshot(client: Client) -> dict[str, Any]:
    raw_alarms = await _read(client, "AlarmEvents")
    return {
        "at": _utc_now(),
        "_mono": time.monotonic(),
        "deviceStatus": str(await _read(client, "DeviceStatus")),
        "currentOutput": round(float(await _read(client, "CurrentOutput")), 2),
        "goodCount": int(await _read(client, "GoodCount")),
        "temperatureC": round(float(await _read(client, "TemperatureSensor")), 2),
        "alarmCodes": _alarm_codes(raw_alarms),
    }


async def _sample_for(
    client: Client,
    seconds: float,
    samples: list[dict[str, Any]],
    *,
    until: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    latest = samples[-1] if samples else await _snapshot(client)
    while time.monotonic() < deadline:
        await asyncio.sleep(SAMPLE_INTERVAL_SECONDS)
        latest = await _snapshot(client)
        samples.append(latest)
        if until is not None and until(latest):
            break
    return latest


async def _call_control(client: Client, method_name: str, *args: ua.Variant) -> None:
    obj = client.get_node(NS)
    method = client.get_node(f"{NS}.{method_name}")
    await obj.call_method(method, *args)


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _public_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in item.items() if key != "_mono"} for item in samples]


def _calculate_oee(
    samples: list[dict[str, Any]],
    *,
    ng_count: int,
) -> dict[str, float]:
    if len(samples) < 2:
        return {"availability": 0.0, "performance": 0.0, "quality": 0.0, "oee": 0.0, "observed": 0.0, "running": 0.0}
    first = samples[0]
    last = samples[-1]
    observed = max(0.001, float(last["_mono"]) - float(first["_mono"]))
    running = 0.0
    for previous, current in zip(samples, samples[1:]):
        if previous["deviceStatus"] == "RUNNING":
            running += max(0.0, float(current["_mono"]) - float(previous["_mono"]))
    good_delta = max(0, int(last["goodCount"]) - int(first["goodCount"]))
    availability = min(1.0, running / observed)
    ideal_runtime_good = NOMINAL_OUTPUT_PER_MIN * running / 60.0
    performance = min(1.0, good_delta / ideal_runtime_good) if ideal_runtime_good > 0 else 0.0
    quality = good_delta / (good_delta + ng_count) if good_delta + ng_count > 0 else 0.0
    oee = availability * performance * quality
    return {
        "availability": availability,
        "performance": performance,
        "quality": quality,
        "oee": oee,
        "observed": observed,
        "running": running,
    }


def _deliver_ng(
    edge: EdgeService,
    mes: MesSimulator,
    token: str,
    event: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    delivery_key = str(uuid.uuid4())
    status, _headers, delivery = _http_json(
        "POST",
        edge.base_url + "/v1/inspection-events",
        token=token,
        body=event,
        headers={"x-correlation-id": event["correlationId"], "idempotency-key": delivery_key},
    )
    if status != 202:
        raise RuntimeError(f"联合场景入队失败：HTTP {status}: {delivery}")
    delivery_id = str(delivery["deliveryId"])
    worker = _worker(edge.store, mes.base_url)
    worker.run_once()
    _, _, acked = _http_json("GET", edge.base_url + f"/v1/deliveries/{delivery_id}", token=token)
    if acked["state"] != "UA_WRITE_PENDING" or not acked.get("mesReceipt"):
        raise RuntimeError(f"联合场景未收到合法 MES 回执：{acked}")
    _, _, done = _http_json(
        "POST",
        edge.base_url + f"/v1/deliveries/{delivery_id}/ua-write-result",
        token=token,
        body={
            "success": True,
            "correlationId": event["correlationId"],
            "actor": "joint-demo-opcua-adapter",
            "note": "联合场景 UA 写回确认",
        },
    )
    if done["state"] != STATE_DONE or not done.get("uaWriteConfirmed"):
        raise RuntimeError(f"联合场景 UA 写回未完成：{done}")
    return acked, done


async def _run_scenario(
    edge: EdgeService,
    mes: MesSimulator,
    token: str,
    plc: MockPlcProcess,
    db_path: Path,
    progress: Callable[[JointStep], None] | None,
    vision_result: dict[str, Any] | None,
) -> JointDemoResult:
    started = _utc_now()
    steps: list[JointStep] = []
    samples: list[dict[str, Any]] = []
    work_order = "WO-JX-JOINT-20261121-001"
    serial = "SN-JX-JOINT-20261121-0001"
    correlation = f"corr-jx-joint-{uuid.uuid4()}"
    vision = vision_result or classify_sample("DENT")
    if vision.get("decision") != "NG":
        raise ValueError("只有 NG 推理结果可以进入联合质量闭环")
    defect_type = str(vision["predictedClass"])
    defect_label = DISPLAY_NAMES.get(defect_type, defect_type)
    defect_confidence = round(float(vision["confidence"]), 6)
    model_card = vision.get("modelCard") or {}
    image_sha256 = str(vision.get("imageSha256") or "")
    if len(image_sha256) != 64:
        raise ValueError("视觉推理结果缺少有效的图像哈希")
    image_id = f"IMG-JX-JOINT-{image_sha256[:8].upper()}"

    def step(
        title: str,
        detail: str,
        *,
        tone: str = "info",
        facts: dict[str, str] | None = None,
    ) -> None:
        item = JointStep(len(steps) + 1, title, detail, tone, facts or {})
        steps.append(item)
        if progress:
            progress(item)

    step(
        "两个项目开始协同",
        "设备上下文来自真实 Mock PLC 仓库；质检交付走真实 Edge Core API、SQLite Outbox 和 Worker。",
        facts={"产线": "JX-LINE-01", "工位": "JX-LINQIN-STATION-01", "工单": work_order},
    )

    client = await _connect_plc(plc)
    try:
        first = await _snapshot(client)
        samples.append(first)
        step(
            "采集设备基线",
            "通过标准 OPC UA 读取设备状态、当前产量、累计良品数和温度，不使用设备私有协议。",
            tone="ok",
            facts={
                "OPC UA": plc.endpoint,
                "设备状态": str(first["deviceStatus"]),
                "当前产量": f"{first['currentOutput']}/min",
                "温度": f"{first['temperatureC']}℃",
            },
        )

        await _sample_for(client, 0.9, samples)
        await _call_control(client, "TriggerDowntime", ua.Variant(2, ua.VariantType.UInt32))
        step(
            "设备停机：OEE 可用率开始受损",
            "模拟设备触发 2 秒停机。设备状态从 RUNNING 变为 DOWN，GoodCount 停止增长。",
            tone="warn",
            facts={"停机时长": "2s", "影响": "Availability"},
        )
        await _sample_for(client, 2.6, samples, until=lambda item: item["deviceStatus"] == "RUNNING")
        await _sample_for(client, 0.5, samples)

        await _call_control(
            client,
            "TriggerTemperatureSpike",
            ua.Variant(4, ua.VariantType.UInt32),
            ua.Variant(92.0, ua.VariantType.Double),
        )
        step(
            "设备温度异常：进入 RCA 观察窗口",
            "温度目标 92℃。这里只建立时间关联，不把温度异常直接等同于 NG 的因果原因。",
            tone="warn",
            facts={"温度目标": "92℃", "窗口": "±5 分钟", "RCA 状态": "观察中"},
        )
        spike_sample = await _sample_for(
            client,
            seconds=3.0,
            samples=samples,
            until=lambda item: item["temperatureC"] >= 70.0,
        )
        temperature_at_ng = float(spike_sample["temperatureC"])
        ng_at = _utc_now()

        event = {
            "schemaVersion": "1.0",
            "sourceEventId": f"QC-JX-JOINT-{uuid.uuid4().hex[:12]}",
            "stationId": "JX-LINQIN-STATION-01",
            "eventType": "NG_ORDER",
            "occurredAt": ng_at,
            "result": "NG",
            "serialNumber": serial,
            "workOrderId": work_order,
            "defectType": defect_type,
            "confidence": defect_confidence,
            "imageId": image_id,
            "correlationId": correlation,
            "metadata": {
                "source": "joint-qa-oee-rca-demo",
                "lineId": "JX-LINE-01",
                "deviceStatus": str(spike_sample["deviceStatus"]),
                "temperatureC": temperature_at_ng,
                "currentOutput": spike_sample["currentOutput"],
                "alarmCodes": spike_sample["alarmCodes"],
                "visionModelVersion": model_card.get("modelVersion"),
                "visionModelSha256": model_card.get("modelSha256"),
                "visionDatasetVersion": model_card.get("datasetVersion"),
                "visionValidationAccuracy": model_card.get("validationAccuracy"),
                "visionValidationMacroRecall": model_card.get("validationMacroRecall"),
                "visionValidationScope": model_card.get("validationScope"),
                "visionInferenceMs": vision.get("inferenceMs"),
                "imageSha256": image_sha256,
                "imageHashScope": vision.get("imageHashScope"),
            },
        }
        step(
            "AI 质检推理产生 NG：附带设备上下文",
            f"缺陷类别与置信度来自本地模型对合成{defect_label}图像的真实推理；合成留出集指标不代表现场精度。NG 事件保持原 MES 业务合同，同时携带设备状态、温度和报警代码。",
            tone="dead",
            facts={
                "SN": serial,
                "缺陷": defect_type,
                "置信度": f"{defect_confidence:.2%}",
                "视觉模型": str(model_card.get("modelVersion") or "unknown"),
                "视觉指标口径": str(model_card.get("validationScope") or "合成数据"),
                "模型哈希": str(model_card.get("modelSha256") or "unknown")[:12],
                "图像哈希": image_sha256[:12],
                "推理耗时": f"{float(vision.get('inferenceMs') or 0):.2f} ms",
                "温度": f"{temperature_at_ng}℃",
                "alarmCodes": ", ".join(spike_sample["alarmCodes"]) or "无",
            },
        )

        acked, done = await asyncio.to_thread(_deliver_ng, edge, mes, token, event)
        step(
            "Edge Core 完成 MES 交付",
            "真实 Outbox、Worker 和回执校验通过；MES 接受后继续等待 UA 写回确认。",
            tone="ok",
            facts={
                "deliveryId": str(done["deliveryId"]),
                "receiptNo": str(acked["mesReceipt"]["receiptNo"]),
                "交付状态": str(done["state"]),
            },
        )

        await _sample_for(client, 1.0, samples)
        await _call_control(client, "ResumeNormal")
        await _sample_for(client, 0.4, samples)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    oee = _calculate_oee(samples, ng_count=1)
    peak = max(float(item["temperatureC"]) for item in samples)
    codes = list(dict.fromkeys(code for item in samples for code in item["alarmCodes"]))
    candidate = "TEMPERATURE_SPIKE" if "TEMPERATURE_SPIKE" in codes and peak >= 70.0 else "INCONCLUSIVE"
    confidence = "中高（时间重合，非因果证明）" if candidate == "TEMPERATURE_SPIKE" else "不足"
    evidence = [
        f"停机窗口内设备状态为 DOWN，GoodCount 停止增长；观测可用率 {_percent(oee['availability'])}。",
        f"温度异常峰值 {peak:.2f}℃，NG 发生时温度 {temperature_at_ng:.2f}℃。",
        f"NG 发生在温度异常窗口内，关联 ID 为 {correlation}。",
        f"MES 已返回唯一回执 {done['mesReceipt']['receiptNo']}，UA 写回确认完成。",
    ]
    summary = (
        "候选根因：温度异常与本次 NG 在时间窗口内重合；同时停机拉低了可用率。"
        if candidate == "TEMPERATURE_SPIKE"
        else "当前证据不足，不能给出候选根因。"
    )
    step(
        "OEE 分解：把损失算清楚",
        "Availability、Performance、Quality 全部由本次真实采样与真实业务结果计算，拒绝用一个漂亮总分掩盖输入。",
        tone="ok",
        facts={
            "OEE": _percent(oee["oee"]),
            "Availability": _percent(oee["availability"]),
            "Performance": _percent(oee["performance"]),
            "Quality": _percent(oee["quality"]),
        },
    )
    step(
        "RCA 候选：给出证据，不给神话",
        summary,
        tone="done" if candidate == "TEMPERATURE_SPIKE" else "warn",
        facts={
            "候选根因": candidate,
            "置信口径": confidence,
            "证据条数": str(len(evidence)),
        },
    )

    delivery_id = str(done["deliveryId"])
    return JointDemoResult(
        scenario="AI 质检 → 设备停机/温度异常 → MES 闭环 → OEE / RCA",
        startedAt=started,
        finishedAt=_utc_now(),
        lineId="JX-LINE-01",
        stationId="JX-LINQIN-STATION-01",
        workOrderId=work_order,
        serialNumber=serial,
        correlationId=correlation,
        deliveryId=delivery_id,
        finalState=str(done["state"]),
        mesReceiptNo=str(done["mesReceipt"]["receiptNo"]),
        attempts=int(done["attempts"]),
        oee=round(oee["oee"], 4),
        availability=round(oee["availability"], 4),
        performance=round(oee["performance"], 4),
        quality=round(oee["quality"], 4),
        observedSeconds=round(oee["observed"], 2),
        runningSeconds=round(oee["running"], 2),
        downtimeSeconds=round(max(0.0, oee["observed"] - oee["running"]), 2),
        goodDelta=max(0, int(samples[-1]["goodCount"]) - int(samples[0]["goodCount"])),
        ngCount=1,
        nominalOutputPerMinute=NOMINAL_OUTPUT_PER_MIN,
        temperaturePeakC=round(peak, 2),
        temperatureAtNgC=round(temperature_at_ng, 2),
        alarmCodes=codes,
        rcaCandidate=candidate,
        rcaConfidence=confidence,
        rcaSummary=summary,
        rcaEvidence=evidence,
        defectType=defect_type,
        defectConfidence=defect_confidence,
        imageId=image_id,
        imageSha256=image_sha256,
        imageHashScope=str(vision.get("imageHashScope") or "unknown"),
        visionModelVersion=str(model_card.get("modelVersion") or "unknown"),
        visionModelSha256=str(model_card.get("modelSha256") or "unknown"),
        visionDatasetVersion=str(model_card.get("datasetVersion") or "unknown"),
        visionValidationAccuracy=round(float(model_card.get("validationAccuracy") or 0.0), 6),
        visionValidationScope=str(model_card.get("validationScope") or "合成数据，不是生产现场数据"),
        visionInferenceMs=round(float(vision.get("inferenceMs") or 0.0), 2),
        steps=[asdict(item) for item in steps],
        samples=_public_samples(samples),
        timeline=_timeline(db_path, delivery_id),
        rpiRepo=str(plc.root),
    )


def run_joint_demo(
    progress: Callable[[JointStep], None] | None = None,
    *,
    vision_result: dict[str, Any] | None = None,
) -> JointDemoResult:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = REPO_ROOT / ".tmp" / f"joint-qa-oee-{stamp}-{uuid.uuid4().hex[:6]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    db_path = run_dir / "joint-edge-core.sqlite3"
    token = f"joint-{uuid.uuid4()}"
    mes = MesSimulator()
    plc = MockPlcProcess(run_dir)
    edge: EdgeService | None = None
    try:
        plc.start()
        edge = EdgeService(db_path, mes.base_url, token)
        return asyncio.run(
            _run_scenario(edge, mes, token, plc, db_path, progress, vision_result)
        )
    finally:
        if edge is not None:
            edge.close()
        mes.close()
        plc.stop()


JOINT_DASHBOARD = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>双项目联合演示 · AI 质检 + OEE/RCA</title>
<style>
:root{--bg:#07111f;--panel:#0d1c30;--panel2:#12243b;--line:#24405f;--text:#e8f1fb;--muted:#8da5bd;--ok:#30d48a;--warn:#f4b740;--dead:#ff6b6b;--blue:#58a6ff;--cyan:#62d9ff}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 85% 0%,#3a2412 0,transparent 34%),radial-gradient(circle at 10% 0%,#143154 0,transparent 40%),var(--bg);color:var(--text);font-family:"Noto Sans SC","Microsoft YaHei",system-ui,sans-serif;min-height:100vh}
.wrap{max-width:1180px;margin:0 auto;padding:36px 24px 64px}.eyebrow{color:var(--cyan);font-size:12px;letter-spacing:.16em;text-transform:uppercase}.hero{display:flex;justify-content:space-between;gap:24px;align-items:flex-end;border-bottom:1px solid var(--line);padding-bottom:22px;margin-bottom:22px}h1{font-size:40px;line-height:1.08;margin:9px 0 11px;letter-spacing:-.03em}.hero p{color:var(--muted);font-size:15px;line-height:1.75;max-width:790px;margin:0}.back{border:1px solid var(--line);color:var(--text);text-decoration:none;border-radius:9px;padding:10px 13px;font-size:12px;white-space:nowrap}
.chain{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:18px}.lane{border:1px solid #21405d;border-radius:13px;padding:14px 16px;background:#091929}.lane.device{border-color:#6b4d1d;background:#1b160d}.lane h3{margin:0 0 10px;font-size:12px;color:var(--cyan)}.lane.device h3{color:var(--warn)}.flow{display:flex;gap:6px;align-items:center;flex-wrap:wrap}.flow span{border:1px solid #28445e;background:#0d2034;border-radius:6px;padding:5px 7px;font-size:10px}.flow i{font-style:normal;color:#547da5}.lane p{color:var(--muted);font-size:10px;line-height:1.55;margin:9px 0 0}
.toolbar{display:flex;gap:13px;align-items:center;margin:20px 0;flex-wrap:wrap}.run{border:0;border-radius:10px;background:linear-gradient(135deg,#f4a83b,#43d8c0);color:#04131e;padding:13px 20px;font-size:15px;font-weight:900;cursor:pointer;box-shadow:0 8px 30px #b9792155}.run:disabled{opacity:.55;cursor:wait}.hint{font-size:12px;color:var(--muted)}
.stats{display:grid;grid-template-columns:1.15fr 1fr 1fr 1.15fr;gap:13px;margin-bottom:18px}.stat{border:1px solid var(--line);border-radius:13px;padding:15px;background:linear-gradient(180deg,var(--panel2),var(--panel))}.stat .label{font-size:11px;color:var(--muted);margin-bottom:7px}.stat .value{font-size:25px;font-weight:900;letter-spacing:-.03em}.stat .sub{font-size:10px;color:var(--muted);margin-top:5px;line-height:1.5}.ok{color:var(--ok)}.warn{color:var(--warn)}.dead{color:var(--dead)}
.layout{display:grid;grid-template-columns:1.45fr .95fr;gap:18px}.panel{border:1px solid var(--line);border-radius:15px;padding:20px;background:#0a1829d9}.panel h2{font-size:17px;margin:0 0 16px}.timeline{position:relative}.timeline:before{content:"";position:absolute;left:15px;top:8px;bottom:8px;width:2px;background:linear-gradient(var(--warn),var(--ok))}.step{position:relative;padding:0 0 17px 46px}.step:last-child{padding-bottom:0}.dot{position:absolute;left:6px;top:2px;width:20px;height:20px;border:2px solid var(--blue);border-radius:50%;background:var(--panel);display:grid;place-items:center;font-size:9px;font-weight:900;z-index:1}.step.ok .dot{border-color:var(--ok);color:var(--ok)}.step.warn .dot{border-color:var(--warn);color:var(--warn)}.step.dead .dot{border-color:var(--dead);color:var(--dead)}.step.done .dot{background:var(--ok);border-color:var(--ok);color:#03131d}.step h3{font-size:14px;margin:0 0 5px}.step p{font-size:12px;line-height:1.6;color:var(--muted);margin:0}.facts{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px}.fact{border:1px solid #233f58;background:#0c1e30;border-radius:5px;padding:3px 6px;font-size:10px;color:#b9cee1}
.side .card{border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:14px}.side .card:last-child{border:0;margin:0;padding:0}.side h3{font-size:13px;margin:0 0 9px}.kv{display:flex;justify-content:space-between;gap:10px;padding:6px 0;border-bottom:1px dashed #1a3149;font-size:10px}.kv:last-child{border:0}.kv span:first-child{color:var(--muted)}.kv span:last-child{text-align:right;word-break:break-all;max-width:68%}.evidence{font-size:11px;line-height:1.6;color:#c4d6e8}.evidence div{padding:7px 0;border-bottom:1px solid #172d43}.evidence div:last-child{border:0}.audit{max-height:230px;overflow:auto;font-size:10px;line-height:1.5}.audit-row{padding:6px 0;border-bottom:1px solid #172d43;color:#b7cce0}.audit-row b{color:var(--text)}.empty{color:var(--muted);font-size:11px;padding:12px 0}.error{color:var(--dead);white-space:pre-wrap;font-size:12px}.foot{color:var(--muted);font-size:10px;line-height:1.65;margin-top:18px}.spin{animation:pulse 1s infinite}@keyframes pulse{50%{opacity:.35}}
@media(max-width:850px){h1{font-size:31px}.hero{display:block}.back{display:inline-block;margin-top:13px}.chain,.stats,.layout{grid-template-columns:1fr}}
</style>
</head>
<body><div class="wrap">
<header class="hero"><div><div class="eyebrow">JOINT DEMO · AI INSPECTION + OEE/RCA</div><h1>AI 质检 × 设备异常</h1><p>本地模型先对合成图像做真实推理，NG 才进入 MES 交付；另一个项目同步采集设备状态、产量、温度和报警。联合后，不只回答“有没有交付”，还回答“这次损失发生在哪里、时间和设备异常是否重合”。</p></div><div style="display:flex;gap:8px;flex-wrap:wrap"><a class="back" href="/vision">AI 质检 →</a><a class="back" href="/overview">项目总览 →</a></div></header>
<section class="chain">
 <div class="lane"><h3>AI 质检与交付主线 · UA_LinqInspect</h3><div class="flow"><span>本地视觉推理</span><i>→</i><span>NG 门禁</span><i>→</i><span>Edge Core Outbox</span><i>→</i><span>MES 回执</span><i>→</i><span>UA 确认</span></div><p>本次真实执行：图像解码、模型推理、置信度门禁、SQLite、API、Worker、回执校验和状态机。</p></div>
 <div class="lane device"><h3>设备上下文 · RPi4 OEE / RCA Gateway</h3><div class="flow"><span>Mock PLC</span><i>→</i><span>停机</span><i>→</i><span>温度 92℃</span><i>→</i><span>报警快照</span></div><p>本次真实执行：另一个仓库的 OPC UA Server、Method 故障注入和变量订阅读取。</p></div>
</section>
<div class="toolbar"><button id="run" class="run">▶ 运行双项目联合场景</button><span id="hint" class="hint">自动触发停机与温度异常，约 12 秒完成</span></div>
<div class="stats">
 <div class="stat"><div class="label">联合 OEE</div><div id="oee" class="value">待运行</div><div id="apq" class="sub">A × P × Q，所有输入可下钻</div></div>
 <div class="stat"><div class="label">设备可用率</div><div id="availability" class="value">-</div><div id="downtime" class="sub">停机损失</div></div>
 <div class="stat"><div class="label">质量闭环</div><div id="state" class="value">-</div><div id="receipt" class="sub">等待 MES 回执</div></div>
 <div class="stat"><div class="label">RCA 候选</div><div id="rca" class="value" style="font-size:18px">-</div><div id="rca-confidence" class="sub">相关不等于因果</div></div>
</div>
<div class="layout">
 <section class="panel"><h2>联合时间线</h2><div id="timeline" class="timeline"><div class="empty">演示尚未开始。</div></div></section>
 <aside class="panel side">
  <div class="card"><h3>业务对象与设备上下文</h3><div id="identity" class="empty">待运行</div></div>
  <div class="card"><h3>RCA 证据</h3><div id="evidence" class="evidence"><div class="empty">完成后显示关联证据。</div></div></div>
  <div class="card"><h3>MES / 投递审计</h3><div id="audit" class="audit"><div class="empty">完成后显示状态轨迹。</div></div></div>
 </aside>
</div>
<p class="foot">真实：本地视觉模型推理、置信度门禁、另一个仓库的 OPC UA Mock PLC、停机/温度 Method、变量读取，以及本项目 Edge Core API、SQLite Outbox、Worker 与回执状态机。模拟：训练/验证图像为程序合成，下游 MES 与 OPC UA 回执写回为本地模拟。合成留出集准确率不代表现场精度；RCA 只展示时间关联和候选原因，不宣称因果证明。</p>
</div>
<script>
const $=id=>document.getElementById(id);
const pct=v=>(Number(v)*100).toFixed(1)+'%';
function esc(s){return String(s??'');}
function setKV(target,obj){const box=$(target);box.innerHTML='';for(const [k,v] of Object.entries(obj)){const row=document.createElement('div');row.className='kv';const a=document.createElement('span');a.textContent=k;const b=document.createElement('span');b.textContent=esc(v);row.append(a,b);box.appendChild(row);}}
function addFact(parent,key,value){const n=document.createElement('span');n.className='fact';n.textContent=key+'='+esc(value);parent.appendChild(n);}
async function run(){
 const btn=$('run');btn.disabled=true;btn.textContent='联合场景运行中…';$('hint').textContent='正在启动另一个仓库的 Mock PLC，并注入停机与温度异常…';$('timeline').innerHTML='<div class="empty spin">正在连接真实 OPC UA 设备上下文…</div>';
 try{
  const res=await fetch('/api/joint',{method:'POST'});const data=await res.json();
  if(!res.ok)throw new Error(data.error||('HTTP '+res.status));
  $('timeline').innerHTML='';
  for(const step of data.steps){
   const card=document.createElement('div');card.className='step '+(step.tone||'info');
   const dot=document.createElement('div');dot.className='dot';dot.textContent=step.number;
   const body=document.createElement('div');const h=document.createElement('h3');h.textContent=step.title;const p=document.createElement('p');p.textContent=step.detail;
   const facts=document.createElement('div');facts.className='facts';for(const [k,v] of Object.entries(step.facts||{}))addFact(facts,k,v);
   body.append(h,p,facts);card.append(dot,body);$('timeline').appendChild(card);
  }
  $('oee').textContent=pct(data.oee);$('oee').className='value ok';$('apq').textContent='A '+pct(data.availability)+' · P '+pct(data.performance)+' · Q '+pct(data.quality);
  $('availability').textContent=pct(data.availability);$('availability').className='value warn';$('downtime').textContent='停机 '+data.downtimeSeconds+'s / 观测 '+data.observedSeconds+'s';
  $('state').textContent=data.finalState;$('state').className='value ok';$('receipt').textContent=data.mesReceiptNo;
  $('rca').textContent=data.rcaCandidate;$('rca').className='value '+(data.rcaCandidate==='TEMPERATURE_SPIKE'?'warn':'dead');$('rca-confidence').textContent=data.rcaConfidence;
  setKV('identity',{'产线':data.lineId,'工位':data.stationId,'工单':data.workOrderId,'SN':data.serialNumber,'视觉判定':data.defectType+' / '+(Number(data.defectConfidence)*100).toFixed(2)+'%','视觉模型':data.visionModelVersion,'模型哈希':String(data.visionModelSha256||'').slice(0,12),'视觉指标口径':data.visionValidationScope,'imageId':data.imageId,'图像哈希':String(data.imageSha256||'').slice(0,12),'哈希口径':data.imageHashScope,'温度峰值':data.temperaturePeakC+'℃','NG 时温度':data.temperatureAtNgC+'℃','alarmCodes':(data.alarmCodes||[]).join(', '),'deliveryId':data.deliveryId,'correlationId':data.correlationId,'另一仓库':data.rpiRepo});
  const ev=$('evidence');ev.innerHTML='';for(const text of data.rcaEvidence){const row=document.createElement('div');row.textContent=text;ev.appendChild(row);}
  const audit=$('audit');audit.innerHTML='';for(const e of data.timeline){const row=document.createElement('div');row.className='audit-row';const b=document.createElement('b');b.textContent=e.from+' → '+e.to;row.append(b,document.createElement('br'),document.createTextNode((e.detail?e.detail+' · ':'')+e.at));audit.appendChild(row);}
  $('hint').textContent='完成：设备损失已量化，NG 已进入 MES，RCA 只给出有证据的候选原因。';btn.textContent='↻ 重新运行联合场景';btn.disabled=false;window.scrollTo({top:0,behavior:'smooth'});
 }catch(e){$('timeline').innerHTML='<div class="error">联合演示失败：'+esc(e.message)+'</div>';$('hint').textContent='请检查另一个项目依赖与路径';btn.textContent='重新运行联合场景';btn.disabled=false;}
}
$('run').addEventListener('click',run);window.addEventListener('load',()=>setTimeout(run,400));
</script></body></html>"""


def _print_result(result: JointDemoResult) -> None:
    line = "=" * 72
    print()
    print(line)
    print("联合场景结论：质量闭环 + 设备上下文")
    print(line)
    print(f"OEE: {_percent(result.oee)}  A: {_percent(result.availability)}  P: {_percent(result.performance)}  Q: {_percent(result.quality)}")
    print(f"质量闭环：{result.finalState} / {result.mesReceiptNo} / {result.deliveryId}")
    print(f"RCA 候选：{result.rcaCandidate}（{result.rcaConfidence}）")
    print(result.rcaSummary)
    for item in result.rcaEvidence:
        print(f"  - {item}")
    print(line)


def main() -> int:
    def report(item: JointStep) -> None:
        print(f"\n[{item.number}] {item.title}")
        print(f"  {item.detail}")
        for key, value in item.facts.items():
            print(f"  {key}: {value}")

    result = run_joint_demo(report)
    _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
