"""Validate deployment rendering, systemd units and Prometheus alert rules."""
from __future__ import annotations

import json
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_SCRIPT = REPO_ROOT / "scripts" / "render_deploy.py"
TOKENS = ("REPO_ROOT", "RUN_USER", "RUN_GROUP", "ENV_FILE")
EXPECTED_UNITS = {
    "ua-linqinspect-server.service",
    "ua-linqinspect-nodered.service",
    "ua-linqinspect-backup.service",
    "ua-linqinspect-backup.timer",
}
EXPECTED_ALERTS = {
    "QcEdgeDown",
    "QcQueueBacklogHigh",
    "QcOldestQueueItemTooOld",
    "QcDeadLettersGrowing",
    "QcDeliveryRetrySpike",
    "QcHeartbeatDegraded",
    "QcBackupStale",
    "QcDatabaseWalGrowing",
    "QcDiskSpaceLow",
}
REQUIRED_ENV = {
    "UA_ADMIN_USER",
    "UA_ADMIN_PASS",
    "UA_SERVER_CERT",
    "UA_SERVER_PRIVATE_KEY",
    "QC_DB_PATH",
    "QC_BACKUP_DIR",
    "QC_BACKUP_KEEP",
    "QC_BACKUP_METRICS_FILE",
    "NODE_RED_PORT",
    "NODERED_ADMIN_USER",
    "NODERED_ADMIN_PASSWORD",
    "NODERED_CREDENTIAL_SECRET",
    "NODERED_HTTP_USER",
    "NODERED_HTTP_PASS",
    "NODERED_OPCUA_USER",
    "NODERED_OPCUA_PASSWORD",
    "QC_REPO_ROOT",
}

results: list[tuple[str, bool]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'} | {name} {detail}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="qc-deploy-test-") as temporary:
        output_dir = Path(temporary) / "rendered"
        command = [
            sys.executable,
            str(RENDER_SCRIPT),
            "--repo-root",
            str(REPO_ROOT),
            "--user",
            "qc-deploy-test",
            "--group",
            "qc-deploy-test",
            "--env-file",
            "/etc/ua-linqinspect/ua-linqinspect.env",
            "--output-dir",
            str(output_dir),
        ]
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        check("render command exits successfully", completed.returncode == 0,
              completed.stderr.strip())
        try:
            summary = json.loads(completed.stdout)
        except json.JSONDecodeError:
            summary = {}
        check("render summary is machine-readable", summary.get("ok") is True)

        systemd_dir = output_dir / "systemd"
        actual_units = {path.name for path in systemd_dir.glob("*")} if systemd_dir.is_dir() else set()
        check("all expected systemd units are rendered", actual_units == EXPECTED_UNITS,
              f"({sorted(actual_units)})")
        rendered_files = [path for path in output_dir.rglob("*") if path.is_file()]
        leftovers = [
            f"{path.name}:{token}"
            for path in rendered_files
            for token in TOKENS
            if f"@{token}@" in path.read_text(encoding="utf-8")
        ]
        check("rendered files contain no template tokens", not leftovers, f"({leftovers})")
        check("rendered files are read-only to group and others",
              all(stat.S_IMODE(path.stat().st_mode) & 0o022 == 0 for path in rendered_files))

        analyzer = shutil.which("systemd-analyze")
        check("systemd-analyze is available", analyzer is not None)
        if analyzer is not None:
            verify = subprocess.run(
                [analyzer, "verify", *sorted(str(path) for path in systemd_dir.glob("*"))],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            verify_output = (verify.stdout + verify.stderr).strip()
            check("systemd units verify without warnings",
                  verify.returncode == 0 and not verify_output, f"({verify_output})")

        backup_unit = (systemd_dir / "ua-linqinspect-backup.service").read_text(encoding="utf-8")
        timer_unit = (systemd_dir / "ua-linqinspect-backup.timer").read_text(encoding="utf-8")
        server_unit = (systemd_dir / "ua-linqinspect-server.service").read_text(encoding="utf-8")
        nodered_unit = (systemd_dir / "ua-linqinspect-nodered.service").read_text(encoding="utf-8")
        check("backup service runs the backup script with metrics",
              "scripts/backup_queue.py" in backup_unit and "--metrics-file" in backup_unit)
        check("backup timer targets the backup service",
              "Unit=ua-linqinspect-backup.service" in timer_unit and
              "Persistent=true" in timer_unit)
        check("application services use the expected entry points",
              "server/qc_server.py" in server_unit and
              ".node-red/node_modules/.bin/node-red" in nodered_unit and
              "Restart=on-failure" in server_unit and
              "Restart=on-failure" in nodered_unit)
        check("Node-RED unit loads deployment credentials",
              "EnvironmentFile=-/etc/ua-linqinspect/ua-linqinspect.env" in nodered_unit)

        alerts_path = output_dir / "prometheus" / "ua-linqinspect-alerts.yml"
        alerts_text = alerts_path.read_text(encoding="utf-8") if alerts_path.is_file() else ""
        alerts = set(re.findall(r"^\s*-\s+alert:\s*(\S+)\s*$", alerts_text, flags=re.MULTILINE))
        check("Prometheus rules contain every expected alert", alerts == EXPECTED_ALERTS,
              f"({sorted(alerts)})")
        check("heartbeat and backup alerts encode operational thresholds",
              "max_over_time(qc_heartbeat_failures[5m]) >= 3" in alerts_text and
              "129600" in alerts_text)

        alertmanager_text = (output_dir / "prometheus" / "alertmanager.yml").read_text(encoding="utf-8")
        check("Alertmanager config routes critical and warning alerts",
              'severity="critical"' in alertmanager_text and
              'severity="warning"' in alertmanager_text and
              "ua-linqinspect-critical" in alertmanager_text and
              "ua-linqinspect-warning" in alertmanager_text)
        check("Alertmanager config sends resolved notifications and inhibits edge fan-out",
              alertmanager_text.count("send_resolved: true") >= 3 and
              'alertname="QcEdgeDown"' in alertmanager_text)

        check("WAL and disk alerts encode operational thresholds",
              'qc_database_bytes{kind="wal"} > 268435456' in alerts_text and
              "node_filesystem_avail_bytes" in alerts_text and
              "< 0.10" in alerts_text)

        logrotate_path = output_dir / "logrotate" / "ua-linqinspect"
        logrotate_text = logrotate_path.read_text(encoding="utf-8") if logrotate_path.is_file() else ""
        check("logrotate config covers state logs with bounded retention",
              "/var/lib/ua-linqinspect/*.log" in logrotate_text and
              "rotate 7" in logrotate_text and
              "maxsize 100M" in logrotate_text and
              "copytruncate" in logrotate_text and
              "su qc-deploy-test qc-deploy-test" in logrotate_text)

        env_path = output_dir / "env" / "ua-linqinspect.env.example"
        env_text = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
        env_keys = {
            line.split("=", 1)[0]
            for line in env_text.splitlines()
            if line and not line.startswith("#") and "=" in line
        }
        check("environment template covers required deployment settings",
              REQUIRED_ENV <= env_keys, f"(missing={sorted(REQUIRED_ENV - env_keys)})")
        check("environment template requires secret replacement",
              "UA_ADMIN_PASS=CHANGE_ME" in env_text and
              "NODERED_ADMIN_PASSWORD=CHANGE_ME" in env_text and
              "NODERED_CREDENTIAL_SECRET=CHANGE_ME" in env_text and
              "NODERED_HTTP_PASS=CHANGE_ME" in env_text and
              "NODERED_OPCUA_PASSWORD=CHANGE_ME" in env_text)
        check("render summary points at generated artifacts",
              Path(str(summary.get("prometheusRules") or "")).is_file() and
              Path(str(summary.get("alertmanagerConfig") or "")).is_file() and
              bool(summary.get("logrotateConfigs")) and
              all(Path(str(path)).is_file() for path in summary.get("logrotateConfigs") or []) and
              Path(str(summary.get("envExample") or "")).is_file())

    passed = sum(1 for _, value in results if value)
    print(f"\n===== DEPLOY TEMPLATES: {passed}/{len(results)} passed =====")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
