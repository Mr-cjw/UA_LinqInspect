#!/usr/bin/env python3
"""Run the full regression suite against isolated UA Server and Node-RED processes."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE_RED_ROOT = REPO_ROOT / ".node-red"
NODE_RED_BIN = NODE_RED_ROOT / "node_modules" / ".bin" / "node-red"
FLOW_SOURCE = REPO_ROOT / "nodered" / "flows.json"
SETTINGS_SOURCE = NODE_RED_ROOT / "settings.js"
CERT_DIR = REPO_ROOT / "certs"

TESTS = (
    ("security", "tests/security_test.py", 60),
    ("smoke", "tests/smoke_test.py", 90),
    ("nodered_e2e", "tests/nodered_e2e.py", 120),
    ("fault_scenarios", "tests/fault_scenarios.py", 180),
)

RECOVERY_TESTS = (
    ("restart_recovery_seed", "tests/restart_recovery.py", "seed", 70),
    ("restart_recovery_verify", "tests/restart_recovery.py", "verify", 70),
)

OUTAGE_TESTS = (
    ("short_outage", "tests/outage_recovery.py", "stall", 90),
    ("stale_filter", "tests/outage_recovery.py", "stale", 45),
    ("ua_restart_prepare", "tests/outage_recovery.py", "prepare_restart", 60),
    ("ua_restart_verify", "tests/outage_recovery.py", "verify_restart", 120),
)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_log(path: Path, needle: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and needle in path.read_text(encoding="utf-8", errors="replace"):
            return
        time.sleep(0.2)
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else "(no log)"
    raise RuntimeError(f"Timed out waiting for {needle!r} in {path}\n{text[-4000:]}")


def wait_for_http(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if 200 <= response.status < 300:
                    return
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for HTTP endpoint {url}")


def rewrite_flow(source: Path, destination: Path, endpoint: str, node_red_base: str) -> None:
    nodes = json.loads(source.read_text(encoding="utf-8"))
    for node in nodes:
        if node.get("type") == "OpcUa-Endpoint":
            node["endpoint"] = endpoint
        function_code = node.get("func")
        if isinstance(function_code, str):
            node["func"] = (
                function_code.replace("http://127.0.0.1:1880", node_red_base)
                .replace("http://localhost:1880", node_red_base)
            )
    destination.write_text(json.dumps(nodes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare_node_red(
    work_dir: Path,
    endpoint: str,
    node_red_base: str,
    username: str,
    password: str,
) -> Path:
    user_dir = work_dir / "node-red"
    user_dir.mkdir(parents=True)
    shutil.copy2(SETTINGS_SOURCE, user_dir / "settings.js")
    rewrite_flow(FLOW_SOURCE, user_dir / "flows.json", endpoint, node_red_base)
    credentials = {"ep1": {"user": username, "password": password}}
    (user_dir / "flows_cred.json").write_text(
        json.dumps(credentials, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.symlink(NODE_RED_ROOT / "node_modules", user_dir / "node_modules", target_is_directory=True)
    return user_dir


def run_test(
    name: str,
    relative_path: str,
    timeout: int,
    env: dict[str, str],
    log_dir: Path,
    argument: str | None = None,
) -> dict:
    command = [sys.executable, str(REPO_ROOT / relative_path)]
    if argument:
        command.append(argument)
    log_path = log_dir / f"{name}.log"
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        output = completed.stdout
        return_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + f"\nTIMEOUT after {timeout}s\n"
        return_code = 124
    elapsed = round(time.monotonic() - started, 2)
    log_path.write_text(output, encoding="utf-8")
    print(output, end="" if output.endswith("\n") else "\n")
    return {
        "name": name,
        "command": relative_path,
        "returnCode": return_code,
        "elapsedSec": elapsed,
        "passed": return_code == 0,
        "log": str(log_path),
    }


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-going", action="store_true", help="run all tests even after a failure")
    args = parser.parse_args()

    for required in (NODE_RED_BIN, FLOW_SOURCE, SETTINGS_SOURCE):
        if not required.exists():
            raise SystemExit(f"Missing prerequisite: {required}. Reinstall project dependencies first.")
    for required in (CERT_DIR / "server" / "server_cert.der", CERT_DIR / "client" / "client_cert.der"):
        if not required.exists():
            raise SystemExit(f"Missing certificate: {required}. Run `.venv/bin/python scripts/setup_pki.py` first.")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    artifact_dir = REPO_ROOT / ".tmp" / f"regression-{stamp}"
    artifact_dir.mkdir(parents=True)
    ua_port = free_port()
    nr_port = free_port()
    while nr_port == ua_port:
        nr_port = free_port()

    admin_user = "regression-admin"
    admin_password = secrets.token_urlsafe(24)
    endpoint = f"opc.tcp://127.0.0.1:{ua_port}"
    node_red_base = f"http://127.0.0.1:{nr_port}"
    node_red_user_dir = prepare_node_red(
        artifact_dir, endpoint, node_red_base, admin_user, admin_password
    )

    base_env = os.environ.copy()
    base_env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "UA_ENDPOINT": endpoint,
            "UA_ENDPOINT_PORT": str(ua_port),
            "UA_ADMIN_USER": admin_user,
            "UA_ADMIN_PASS": admin_password,
            "NODERED_BASE": node_red_base,
        }
    )
    server_env = base_env | {
        "UA_BIND_ADDRESS": "127.0.0.1",
        "UA_SERVER_CERT": str(CERT_DIR / "server" / "server_cert.der"),
        "UA_SERVER_PRIVATE_KEY": str(CERT_DIR / "server" / "server_key.pem"),
        "UA_TRUSTED_CLIENT_CERTS_DIR": str(CERT_DIR / "trusted_clients"),
        "UA_AUDIT_LOG": str(artifact_dir / "ua-server-audit.log"),
        "QC_AUTO_INSPECT": "1",
        "QC_INSPECT_INTERVAL": "0.5",
        "QC_NG_PROBABILITY": "0.6",
        "QC_NG_WINDOW": "20",
    }

    ua_log = artifact_dir / "ua-server.log"
    nr_log = artifact_dir / "node-red.log"
    ua_process = None
    nr_process = None
    results: list[dict] = []
    summary: dict = {
        "status": "failed",
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "artifactDir": str(artifact_dir),
        "uaEndpoint": endpoint,
        "nodeRedBase": base_env["NODERED_BASE"],
    }

    failure: str | None = None
    try:
        with ua_log.open("w", encoding="utf-8") as handle:
            ua_process = subprocess.Popen(
                [sys.executable, str(REPO_ROOT / "server" / "qc_server.py")],
                cwd=REPO_ROOT,
                env=server_env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
        wait_for_log(ua_log, "OPC UA Server listening", 25)
        print(f"UA Server ready: {endpoint}")

        with nr_log.open("w", encoding="utf-8") as handle:
            nr_process = subprocess.Popen(
                [
                    str(NODE_RED_BIN),
                    "--userDir",
                    str(node_red_user_dir),
                    "--port",
                    str(nr_port),
                    "--no-telemetry",
                ],
                cwd=REPO_ROOT,
                env=base_env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
        wait_for_log(nr_log, "Started flows", 35)
        wait_for_http(base_env["NODERED_BASE"] + "/qc/status", 20)
        print(f"Node-RED ready: {base_env['NODERED_BASE']}")
        time.sleep(6)

        for name, path, timeout in TESTS:
            print(f"\n===== RUN {name} =====")
            result = run_test(name, path, timeout, base_env, artifact_dir)
            results.append(result)
            if not result["passed"] and not args.keep_going:
                break

        recovery_state = artifact_dir / "restart-recovery-state.json"
        recovery_env = base_env | {"RECOVERY_STATE": str(recovery_state)}
        if all(result["passed"] for result in results):
            seed_name, seed_path, seed_arg, seed_timeout = RECOVERY_TESTS[0]
            print(f"\n===== RUN {seed_name} =====")
            seed_result = run_test(seed_name, seed_path, seed_timeout, recovery_env, artifact_dir, seed_arg)
            results.append(seed_result)
            if seed_result["passed"]:
                stop_process(nr_process)
                nr_process = None
                node_red_restart_log = artifact_dir / "node-red-restart.log"
                with node_red_restart_log.open("w", encoding="utf-8") as handle:
                    nr_process = subprocess.Popen(
                        [
                            str(NODE_RED_BIN),
                            "--userDir",
                            str(node_red_user_dir),
                            "--port",
                            str(nr_port),
                            "--no-telemetry",
                        ],
                        cwd=REPO_ROOT,
                        env=base_env,
                        stdout=handle,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                wait_for_log(node_red_restart_log, "Started flows", 35)
                wait_for_http(base_env["NODERED_BASE"] + "/qc/status", 20)
                verify_name, verify_path, verify_arg, verify_timeout = RECOVERY_TESTS[1]
                print(f"\n===== RUN {verify_name} =====")
                verify_result = run_test(
                    verify_name, verify_path, verify_timeout, recovery_env, artifact_dir, verify_arg
                )
                results.append(verify_result)

        if all(result["passed"] for result in results):
            outage_state = artifact_dir / "ua-outage-state.json"
            outage_env = base_env | {
                "OUTAGE_STATE": str(outage_state),
                "UA_SERVER_PID": str(ua_process.pid if ua_process and ua_process.poll() is None else 0),
            }
            for name, path, argument, timeout in OUTAGE_TESTS[:2]:
                print(f"\n===== RUN {name} =====")
                result = run_test(name, path, timeout, outage_env, artifact_dir, argument)
                results.append(result)
                if name == "short_outage" and ua_process and ua_process.poll() is None:
                    try:
                        os.kill(ua_process.pid, signal.SIGCONT)
                    except ProcessLookupError:
                        pass

            prepare_name, prepare_path, prepare_arg, prepare_timeout = OUTAGE_TESTS[2]
            print(f"\n===== RUN {prepare_name} =====")
            prepare_result = run_test(
                prepare_name, prepare_path, prepare_timeout, outage_env, artifact_dir, prepare_arg
            )
            results.append(prepare_result)
            if prepare_result["passed"]:
                stop_process(ua_process)
                ua_process = None
                time.sleep(8)
                restart_log = artifact_dir / "ua-server-restart.log"
                with restart_log.open("w", encoding="utf-8") as handle:
                    ua_process = subprocess.Popen(
                        [sys.executable, str(REPO_ROOT / "server" / "qc_server.py")],
                        cwd=REPO_ROOT,
                        env=server_env,
                        stdout=handle,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                wait_for_log(restart_log, "OPC UA Server listening", 25)
                wait_for_log(node_red_restart_log, "[UA重连]", 45)
                time.sleep(3)
                verify_name, verify_path, verify_arg, verify_timeout = OUTAGE_TESTS[3]
                print(f"\n===== RUN {verify_name} =====")
                verify_result = run_test(
                    verify_name, verify_path, verify_timeout, outage_env, artifact_dir, verify_arg
                )
                results.append(verify_result)
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
        print(f"\nRegression setup/run error: {failure}", file=sys.stderr)
    finally:
        stop_process(nr_process)
        stop_process(ua_process)

    expected_results = len(TESTS) + len(RECOVERY_TESTS) + len(OUTAGE_TESTS)
    passed = failure is None and all(result["passed"] for result in results) and len(results) == expected_results
    summary.update(
        {
            "status": "passed" if passed else "failed",
            "finishedAt": datetime.now(timezone.utc).isoformat(),
            "tests": results,
        }
    )
    if failure is not None:
        summary["error"] = failure
    summary_path = artifact_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nRegression {summary['status']}: {summary_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
