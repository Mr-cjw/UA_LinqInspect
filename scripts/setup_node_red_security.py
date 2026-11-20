#!/usr/bin/env python3
"""Install the secure Node-RED settings template and local development secrets."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shlex
import shutil
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "nodered" / "settings.secure.js"
USER_DIR = ROOT / ".node-red"
TARGET = USER_DIR / "settings.js"
SECRETS = USER_DIR / ".secure-env"
FLOW_CREDENTIALS = USER_DIR / "flows_cred.json"
ENV_KEYS = (
    "NODERED_ADMIN_USER",
    "NODERED_ADMIN_PASSWORD",
    "NODERED_CREDENTIAL_SECRET",
    "NODERED_HTTP_USER",
    "NODERED_HTTP_PASS",
    "NODERED_OPCUA_USER",
    "NODERED_OPCUA_PASSWORD",
    "QC_REPO_ROOT",
    "QC_DB_PATH",
)


def read_existing() -> dict[str, str]:
    values: dict[str, str] = {}
    if not SECRETS.exists():
        return values
    for line in SECRETS.read_text(encoding="utf-8").splitlines():
        if not line.startswith("export ") or "=" not in line:
            continue
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        if len(parts) < 2 or "=" not in parts[1]:
            continue
        key, value = parts[1].split("=", 1)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def main() -> int:
    if not SOURCE.exists():
        raise SystemExit(f"Missing secure settings template: {SOURCE}")
    USER_DIR.mkdir(parents=True, exist_ok=True)
    values = read_existing()
    values.update({key: os.environ[key] for key in ENV_KEYS if os.environ.get(key)})

    values.setdefault("NODERED_ADMIN_USER", "admin")
    values.setdefault("NODERED_ADMIN_PASSWORD", secrets.token_urlsafe(24))
    values.setdefault("NODERED_CREDENTIAL_SECRET", secrets.token_urlsafe(32))
    values.setdefault("NODERED_HTTP_USER", values["NODERED_ADMIN_USER"])
    values.setdefault("NODERED_HTTP_PASS", secrets.token_urlsafe(24))
    values.setdefault("NODERED_OPCUA_USER", os.environ.get("UA_ADMIN_USER") or values["NODERED_ADMIN_USER"])
    values.setdefault(
        "NODERED_OPCUA_PASSWORD",
        os.environ.get("UA_ADMIN_PASS") or secrets.token_urlsafe(24),
    )
    values.setdefault("QC_REPO_ROOT", str(ROOT))
    values.setdefault("QC_DB_PATH", str(ROOT / "data" / "qc-queue.sqlite3"))
    values.setdefault("QC_STATE_PATH", str(ROOT / "data" / "qc-state.json"))

    lines = ["# Generated local-only Node-RED secrets. Never commit this file."]
    for key in (
        "NODERED_ADMIN_USER",
        "NODERED_ADMIN_PASSWORD",
        "NODERED_CREDENTIAL_SECRET",
        "NODERED_HTTP_USER",
        "NODERED_HTTP_PASS",
        "NODERED_OPCUA_USER",
        "NODERED_OPCUA_PASSWORD",
        "QC_REPO_ROOT",
        "QC_DB_PATH",
        "QC_STATE_PATH",
    ):
        value = values[key].replace("'", "'\\''")
        lines.append(f"export {key}='{value}'")
    SECRETS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    SECRETS.chmod(0o600)

    # Node-RED encrypts credentials with the credentialSecret. Provision the
    # OPC UA endpoint credentials without writing plaintext into the flow file.
    plaintext = json.dumps(
        {
            "ep1": {
                "user": values["NODERED_OPCUA_USER"],
                "password": values["NODERED_OPCUA_PASSWORD"],
            }
        },
        separators=(",", ":"),
    ).encode("utf-8")
    key = hashlib.sha256(values["NODERED_CREDENTIAL_SECRET"].encode("utf-8")).digest()
    iv = os.urandom(16)
    encryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    encrypted = {"$": iv.hex() + base64.b64encode(ciphertext).decode("ascii")}
    FLOW_CREDENTIALS.write_text(json.dumps(encrypted) + "\n", encoding="utf-8")
    FLOW_CREDENTIALS.chmod(0o600)

    # Node-RED keeps a generated `_credentialSecret` in its runtime config.
    # When migrating to an explicit credentialSecret, that old key causes a
    # failed migration attempt. Remove only the key; preserve the rest.
    runtime_config = USER_DIR / ".config.runtime.json"
    if runtime_config.exists():
        try:
            config = json.loads(runtime_config.read_text(encoding="utf-8"))
            config.pop("_credentialSecret", None)
            runtime_config.write_text(json.dumps(config) + "\n", encoding="utf-8")
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Cannot update Node-RED runtime config {runtime_config}: {exc}")

    if TARGET.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = USER_DIR / f"settings.js.backup-{stamp}"
        shutil.copy2(TARGET, backup)
        print(f"Backed up existing settings to {backup}")
    shutil.copy2(SOURCE, TARGET)
    print(f"Installed secure settings: {TARGET}")
    print(f"Local secrets: {SECRETS}")
    print(f"Encrypted endpoint credentials: {FLOW_CREDENTIALS}")
    print("Start Node-RED from a shell that has sourced .node-red/.secure-env")
    print("Use NODERED_OPCUA_USER/PASSWORD as UA_ADMIN_USER/UA_ADMIN_PASS for the local demo server")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
