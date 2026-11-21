#!/usr/bin/env python3
"""Run a safe, reportable MES contract compatibility check.

The default mode is a dry run. Real MES requests require an explicit
--execute and a matching --confirm-write value. The tool uses test-prefixed
business identifiers and never deletes MES records.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from edge_core.contract import (  # noqa: E402
    ContractError,
    EVENT_COMPLETION_REPORT,
    EVENT_NG_ORDER,
    validate_mes_success,
)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def correlation_id(kind: str) -> str:
    return f"corr-doctor-{kind}-{uuid.uuid4().hex}"


def ng_payload(
    *,
    station_id: str,
    work_order: str,
    serial_number: str,
    defect: str = "SCRATCH",
    correlation: str | None = None,
    source_event_id: str | None = None,
) -> dict[str, Any]:
    corr = correlation or correlation_id("ng")
    return {
        "schemaVersion": "1.0",
        "sourceEventId": source_event_id or f"evt-doctor-ng-{uuid.uuid4().hex}",
        "stationId": station_id,
        "eventType": EVENT_NG_ORDER,
        "occurredAt": utc_iso(),
        "result": "NG",
        "serialNumber": serial_number,
        "workOrderId": work_order,
        "defectType": defect,
        "confidence": 0.91,
        "imageId": f"IMG-DOCTOR-{uuid.uuid4().hex[:16]}",
        "correlationId": corr,
        "metadata": {"doctor": True, "purpose": "mes-contract-check"},
    }


def completion_payload(
    *,
    station_id: str,
    work_order: str,
    inspect_count: int = 5,
    correlation: str | None = None,
    source_event_id: str | None = None,
) -> dict[str, Any]:
    corr = correlation or correlation_id("done")
    ok_count = max(0, inspect_count - 1)
    ng_count = 1 if inspect_count else 0
    return {
        "schemaVersion": "1.0",
        "sourceEventId": source_event_id or f"evt-doctor-done-{uuid.uuid4().hex}",
        "stationId": station_id,
        "eventType": EVENT_COMPLETION_REPORT,
        "occurredAt": utc_iso(),
        "workOrderId": work_order,
        "inspectCount": inspect_count,
        "okCount": ok_count,
        "ngCount": ng_count,
        "ngRate": round(ng_count / inspect_count, 3) if inspect_count else -1.0,
        "correlationId": corr,
        "metadata": {"doctor": True, "purpose": "mes-contract-check"},
    }


class MesDoctor:
    def __init__(
        self,
        *,
        base_url: str,
        timeout: float,
        bearer_token: str = "",
        basic_user: str = "",
        basic_pass: str = "",
        ng_path: str = "/mes/ng-order",
        completion_path: str = "/mes/complete",
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = max(0.5, timeout)
        self.bearer_token = bearer_token
        self.basic_user = basic_user
        self.basic_pass = basic_pass
        self.ng_path = "/" + ng_path.lstrip("/")
        self.completion_path = "/" + completion_path.lstrip("/")

    def _headers(self, correlation: str, idempotency_key: str) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": "application/json",
            "x-correlation-id": correlation,
            "idempotency-key": idempotency_key,
        }
        if self.basic_user:
            raw = f"{self.basic_user}:{self.basic_pass}".encode("utf-8")
            headers["authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        elif self.bearer_token:
            headers["authorization"] = f"Bearer {self.bearer_token}"
        return headers

    def post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        correlation = str(payload["correlationId"])
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers=self._headers(correlation, idempotency_key),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                headers = dict(response.headers.items())
                raw = response.read(1024 * 1024)
        except urllib.error.HTTPError as exc:
            try:
                status = exc.code
                headers = dict(exc.headers.items()) if exc.headers else {}
                raw = exc.read(1024 * 1024)
            finally:
                exc.close()
        except (urllib.error.URLError, OSError) as exc:
            return {
                "ok": False,
                "transportError": f"{type(exc).__name__}: {exc}",
                "body": {},
                "raw": "",
                "status": 0,
            }
        text = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError:
            parsed = {}
        return {
            "ok": True,
            "status": status,
            "headers": headers,
            "raw": text[:4000],
            "body": parsed if isinstance(parsed, dict) else {},
        }


def scenario(
    name: str,
    status: str,
    *,
    message: str,
    response: dict[str, Any] | None = None,
    receipt_no: str = "",
    error_code: str = "",
) -> dict[str, Any]:
    body = (response or {}).get("body") or {}
    return {
        "name": name,
        "status": status,
        "message": message,
        "httpStatus": (response or {}).get("status", 0),
        "receiptNo": receipt_no,
        "errorCode": error_code or str(body.get("errorCode") or ""),
        "response": body,
    }


def success_check(
    name: str,
    response: dict[str, Any],
    *,
    correlation: str,
    event_type: str,
) -> tuple[dict[str, Any], str]:
    if response.get("transportError"):
        return scenario(name, "FAIL", message=response["transportError"], response=response), ""
    status = int(response.get("status") or 0)
    if not 200 <= status < 300:
        return scenario(name, "FAIL", message=f"expected 2xx, got HTTP {status}", response=response), ""
    try:
        receipt = validate_mes_success(response.get("body") or {}, correlation_id=correlation, event_type=event_type)
    except ContractError as exc:
        return scenario(name, "FAIL", message=f"{exc.code}: {exc}", response=response), ""
    receipt_no = str(receipt.get("receiptNo") or "")
    return scenario(name, "PASS", message="valid receipt", response=response, receipt_no=receipt_no), receipt_no


def duplicate_check(
    name: str,
    response: dict[str, Any],
    *,
    correlation: str,
    event_type: str,
    first_receipt: str,
) -> dict[str, Any]:
    checked, receipt_no = success_check(
        name, response, correlation=correlation, event_type=event_type
    )
    if checked["status"] == "FAIL":
        return checked
    if not first_receipt or receipt_no != first_receipt:
        checked["status"] = "WARN"
        checked["message"] = f"duplicate returned a different receipt: first={first_receipt!r} duplicate={receipt_no!r}"
        return checked
    duplicated = (response.get("body") or {}).get("duplicated")
    if duplicated is True:
        checked["message"] = "same receipt with duplicated=true"
    else:
        checked["message"] = "same receipt; duplicated flag is absent or false"
    return checked


def conflict_check(name: str, response: dict[str, Any]) -> dict[str, Any]:
    if response.get("transportError"):
        return scenario(name, "FAIL", message=response["transportError"], response=response)
    status = int(response.get("status") or 0)
    body = response.get("body") or {}
    business_effect = body.get("accepted") is True or bool(body.get("mesOrderNo") or body.get("receiptNo"))
    if business_effect:
        return scenario(
            name,
            "WARN",
            message="MES accepted a conflicting payload; business-key idempotency is not enforced",
            response=response,
        )
    if status == 409:
        if body.get("errorCode"):
            return scenario(name, "PASS", message="conflict rejected with stable errorCode", response=response)
        return scenario(name, "WARN", message="conflict rejected without a stable errorCode", response=response)
    if 400 <= status < 500:
        return scenario(name, "WARN", message=f"conflict rejected with HTTP {status}, expected 409", response=response)
    return scenario(name, "FAIL", message=f"expected conflict rejection, got HTTP {status}", response=response)


def invalid_check(name: str, response: dict[str, Any]) -> dict[str, Any]:
    if response.get("transportError"):
        return scenario(name, "FAIL", message=response["transportError"], response=response)
    status = int(response.get("status") or 0)
    body = response.get("body") or {}
    if 200 <= status < 300 or body.get("accepted") is True:
        return scenario(name, "FAIL", message="MES accepted an invalid request", response=response)
    if status in (400, 422):
        return scenario(name, "PASS", message=f"invalid request rejected with HTTP {status}", response=response)
    return scenario(name, "WARN", message=f"invalid request returned HTTP {status}", response=response)


def build_report(
    *,
    doctor: MesDoctor | None,
    execute: bool,
    station_id: str,
    work_order: str,
    serial_number: str,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schemaVersion": "1.0",
        "generatedAt": utc_iso(),
        "mode": "execute" if execute else "dry-run",
        "target": doctor.base_url if doctor else "",
        "auth": (
            "basic" if doctor and doctor.basic_user else "bearer" if doctor and doctor.bearer_token else "none"
        ),
        "stationId": station_id,
        "workOrderId": work_order,
        "serialNumber": serial_number,
        "scenarios": [],
        "notice": "This tool writes test records to MES when --execute is used and never deletes them.",
    }
    if not execute or doctor is None:
        report["overallStatus"] = "DRY_RUN"
        report["scenarios"] = [
            {"name": name, "status": "SKIP", "message": "dry-run"}
            for name in (
                "ng_first",
                "ng_duplicate",
                "ng_conflict",
                "completion_first",
                "completion_duplicate",
                "completion_conflict",
                "invalid_ng",
            )
        ]
        return report

    ng = ng_payload(
        station_id=station_id,
        work_order=work_order,
        serial_number=serial_number,
    )
    ng_key = str(uuid.uuid4())
    first_ng = doctor.post(doctor.ng_path, ng, idempotency_key=ng_key)
    ng_first, ng_receipt = success_check(
        "ng_first",
        first_ng,
        correlation=ng["correlationId"],
        event_type=EVENT_NG_ORDER,
    )
    report["scenarios"].append(ng_first)

    duplicate_ng = doctor.post(doctor.ng_path, ng, idempotency_key=ng_key)
    report["scenarios"].append(
        duplicate_check(
            "ng_duplicate",
            duplicate_ng,
            correlation=ng["correlationId"],
            event_type=EVENT_NG_ORDER,
            first_receipt=ng_receipt,
        )
    )

    conflict_ng = ng_payload(
        station_id=station_id,
        work_order=work_order,
        serial_number=serial_number,
        defect="DENT",
    )
    report["scenarios"].append(
        conflict_check(
            "ng_conflict",
            doctor.post(doctor.ng_path, conflict_ng, idempotency_key=str(uuid.uuid4())),
        )
    )

    completion = completion_payload(station_id=station_id, work_order=work_order)
    completion_key = str(uuid.uuid4())
    first_completion = doctor.post(doctor.completion_path, completion, idempotency_key=completion_key)
    completion_first, completion_receipt = success_check(
        "completion_first",
        first_completion,
        correlation=completion["correlationId"],
        event_type=EVENT_COMPLETION_REPORT,
    )
    report["scenarios"].append(completion_first)

    duplicate_completion = doctor.post(doctor.completion_path, completion, idempotency_key=completion_key)
    report["scenarios"].append(
        duplicate_check(
            "completion_duplicate",
            duplicate_completion,
            correlation=completion["correlationId"],
            event_type=EVENT_COMPLETION_REPORT,
            first_receipt=completion_receipt,
        )
    )

    completion_conflict = completion_payload(
        station_id=station_id,
        work_order=work_order,
        inspect_count=6,
    )
    report["scenarios"].append(
        conflict_check(
            "completion_conflict",
            doctor.post(doctor.completion_path, completion_conflict, idempotency_key=str(uuid.uuid4())),
        )
    )

    invalid = ng_payload(
        station_id=station_id,
        work_order=work_order,
        serial_number=serial_number,
    )
    invalid.pop("serialNumber", None)
    report["scenarios"].append(
        invalid_check(
            "invalid_ng",
            doctor.post(doctor.ng_path, invalid, idempotency_key=str(uuid.uuid4())),
        )
    )

    statuses = [item["status"] for item in report["scenarios"]]
    report["overallStatus"] = "FAIL" if "FAIL" in statuses else "WARN" if "WARN" in statuses else "PASS"
    report["counts"] = {
        status: statuses.count(status) for status in ("PASS", "WARN", "FAIL", "SKIP")
    }
    return report


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# MES 契约体检报告",
        "",
        f"- 生成时间：`{report['generatedAt']}`",
        f"- 模式：`{report['mode']}`",
        f"- MES：`{report.get('target') or '(not configured)'}`",
        f"- 鉴权：`{report.get('auth', 'none')}`",
        f"- 工单：`{report['workOrderId']}`",
        f"- 序列号：`{report['serialNumber']}`",
        f"- 总体结论：**{report['overallStatus']}**",
        "",
        "> 本工具会向 MES 写入测试记录，且不会自动删除。执行前必须确认测试工单和数据清理责任。",
        "",
        "| 场景 | 结论 | HTTP | 回执号 | 说明 |",
        "|---|---:|---:|---|---|",
    ]
    for item in report.get("scenarios", []):
        lines.append(
            "| {name} | {status} | {http} | {receipt} | {message} |".format(
                name=item.get("name", ""),
                status=item.get("status", ""),
                http=item.get("httpStatus", ""),
                receipt=item.get("receiptNo", ""),
                message=str(item.get("message", "")).replace("|", "\\|"),
            )
        )
    lines.extend(
        [
            "",
            "## 结论解释",
            "",
            "- `PASS`：满足当前同步回执和幂等契约。",
            "- `WARN`：MES 可用，但冲突检测或错误码不稳定；需要在合同或适配器中补边界。",
            "- `FAIL`：成功回执不合法，或非法请求被接受，不能直接接入生产。",
            "- `DRY_RUN`：只生成计划，没有向 MES 写入任何数据。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:1880", help="MES base URL")
    parser.add_argument("--ng-path", default="/mes/ng-order")
    parser.add_argument("--completion-path", default="/mes/complete")
    parser.add_argument("--station-id", default="QCStation")
    parser.add_argument("--work-order", default=f"WO-MES-DOCTOR-{utc_stamp()}")
    parser.add_argument("--serial", default=f"SN-MES-DOCTOR-{utc_stamp()}")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--bearer-token", default="")
    parser.add_argument("--basic-user", default="")
    parser.add_argument("--basic-pass", default="")
    parser.add_argument("--execute", action="store_true", help="send real test records to MES")
    parser.add_argument(
        "--confirm-write",
        default="",
        help="must exactly match --work-order when --execute is used",
    )
    parser.add_argument("--report-dir", default="")
    args = parser.parse_args()

    if bool(args.basic_user) != bool(args.basic_pass):
        parser.error("--basic-user and --basic-pass must be provided together")
    if args.bearer_token and args.basic_user:
        parser.error("configure bearer or basic auth, not both")
    if args.execute and args.confirm_write != args.work_order:
        parser.error("--confirm-write must exactly match --work-order before writing to MES")

    base = Path(args.report_dir).expanduser().resolve() if args.report_dir else (
        REPO_ROOT / ".tmp" / f"mes-contract-doctor-{utc_stamp()}"
    )
    base.mkdir(parents=True, exist_ok=True)
    doctor = MesDoctor(
        base_url=args.base_url,
        timeout=args.timeout,
        bearer_token=args.bearer_token.strip(),
        basic_user=args.basic_user.strip(),
        basic_pass=args.basic_pass,
        ng_path=args.ng_path,
        completion_path=args.completion_path,
    )
    report = build_report(
        doctor=doctor,
        execute=args.execute,
        station_id=args.station_id,
        work_order=args.work_order,
        serial_number=args.serial,
    )
    json_path = base / "report.json"
    markdown_path = base / "report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(markdown_report(report), encoding="utf-8")
    print(json.dumps({
        "overallStatus": report["overallStatus"],
        "mode": report["mode"],
        "workOrderId": report["workOrderId"],
        "scenarios": [
            {"name": item["name"], "status": item["status"], "httpStatus": item.get("httpStatus", 0)}
            for item in report["scenarios"]
        ],
        "reportJson": str(json_path),
        "reportMarkdown": str(markdown_path),
    }, ensure_ascii=False, indent=2))
    if report["overallStatus"] == "FAIL":
        return 1
    if report["overallStatus"] == "WARN":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
