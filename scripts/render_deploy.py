#!/usr/bin/env python3
"""Render repository-specific systemd units and deployment files."""
from __future__ import annotations

import argparse
import getpass
import grp
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = Path("/etc/ua-linqinspect/ua-linqinspect.env")
TOKENS = ("REPO_ROOT", "RUN_USER", "RUN_GROUP", "ENV_FILE")


def current_group() -> str:
    try:
        return grp.getgrgid(os.getgid()).gr_name
    except (KeyError, OSError):
        return getpass.getuser()


def render(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace(f"@{key}@", value)
    leftover = [token for token in TOKENS if f"@{token}@" in text]
    if leftover:
        raise ValueError(f"unrendered deployment tokens: {', '.join(leftover)}")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--user", default=getpass.getuser())
    parser.add_argument("--group", default=current_group())
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    repo_root = Path(args.repo_root).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    required = (
        repo_root / "server" / "qc_server.py",
        repo_root / "edge_core" / "main.py",
        repo_root / "scripts" / "run_ua_writeback_adapter.py",
        repo_root / ".venv" / "bin" / "python",
        repo_root / ".node-red" / "node_modules" / ".bin" / "node-red",
        repo_root / "nodered" / "settings.secure.js",
        repo_root / "scripts" / "setup_node_red_security.py",
        repo_root / "scripts" / "backup_queue.py",
        repo_root / "scripts" / "edge_core_backup.py",
        repo_root / "scripts" / "verify_evidence_signature.py",
        repo_root / "operator_console" / "main.py",
        repo_root / "operator_console" / "static" / "index.html",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit(f"Missing deployment prerequisites: {missing}")

    values = {
        "REPO_ROOT": str(repo_root),
        "RUN_USER": args.user,
        "RUN_GROUP": args.group,
        "ENV_FILE": str(Path(args.env_file).expanduser().resolve()),
    }
    systemd_dir = output_dir / "systemd"
    prometheus_dir = output_dir / "prometheus"
    env_dir = output_dir / "env"
    logrotate_dir = output_dir / "logrotate"
    for directory in (systemd_dir, prometheus_dir, env_dir, logrotate_dir):
        directory.mkdir(parents=True, exist_ok=True)

    rendered = []
    for source in sorted((repo_root / "deploy" / "systemd").glob("*.in")):
        destination = systemd_dir / source.name.removesuffix(".in")
        destination.write_text(render(source.read_text(encoding="utf-8"), values), encoding="utf-8")
        os.chmod(destination, 0o644)
        rendered.append(str(destination))

    alerts_source = repo_root / "deploy" / "prometheus" / "ua-linqinspect-alerts.yml"
    alerts_target = prometheus_dir / alerts_source.name
    alerts_target.write_text(alerts_source.read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(alerts_target, 0o644)

    alertmanager_source = repo_root / "deploy" / "prometheus" / "alertmanager.yml"
    alertmanager_target = prometheus_dir / alertmanager_source.name
    alertmanager_target.write_text(alertmanager_source.read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(alertmanager_target, 0o644)

    logrotate_files = []
    for source in sorted((repo_root / "deploy" / "logrotate").glob("*.in")):
        destination = logrotate_dir / source.name.removesuffix(".in")
        destination.write_text(render(source.read_text(encoding="utf-8"), values), encoding="utf-8")
        os.chmod(destination, 0o644)
        logrotate_files.append(str(destination))

    env_source = repo_root / "deploy" / "env" / "ua-linqinspect.env.example"
    env_target = env_dir / env_source.name
    env_target.write_text(env_source.read_text(encoding="utf-8"), encoding="utf-8")
    os.chmod(env_target, 0o644)

    summary = {
        "ok": True,
        "repoRoot": str(repo_root),
        "user": args.user,
        "group": args.group,
        "envFile": str(Path(args.env_file).expanduser().resolve()),
        "outputDir": str(output_dir),
        "systemdUnits": rendered,
        "prometheusRules": str(alerts_target),
        "alertmanagerConfig": str(alertmanager_target),
        "logrotateConfigs": logrotate_files,
        "envExample": str(env_target),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
