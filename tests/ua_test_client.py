"""Shared OPC UA client configuration for the automated regression suite."""
from __future__ import annotations

import os
from pathlib import Path

from asyncua import Client, ua
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256

REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENT_CERT = REPO_ROOT / "certs" / "client" / "client_cert.der"
CLIENT_KEY = REPO_ROOT / "certs" / "client" / "client_key.pem"
SERVER_CERT = REPO_ROOT / "certs" / "server" / "server_cert.der"
APPLICATION_URI = "urn:freeopcua:python:client"


def endpoint_url() -> str:
    configured = os.getenv("UA_ENDPOINT", "").strip()
    if configured:
        return configured
    port = int(os.getenv("UA_ENDPOINT_PORT", "4840"))
    return f"opc.tcp://127.0.0.1:{port}"


def admin_credentials() -> tuple[str, str]:
    username = os.getenv("UA_ADMIN_USER", "").strip()
    password = os.getenv("UA_ADMIN_PASS", "")
    if not username or not password:
        raise RuntimeError(
            "UA_ADMIN_USER and UA_ADMIN_PASS are required; use scripts/run_regression.py "
            "or export them before running the test directly"
        )
    return username, password


async def create_secure_client(*, admin: bool = True) -> Client:
    """Create a certificate-backed client without connecting it."""
    for path in (CLIENT_CERT, CLIENT_KEY, SERVER_CERT):
        if not path.is_file():
            raise FileNotFoundError(
                f"Missing OPC UA test certificate: {path}. Run scripts/setup_pki.py first."
            )

    client = Client(endpoint_url(), timeout=float(os.getenv("UA_CLIENT_TIMEOUT", "5")))
    client.application_uri = os.getenv("UA_CLIENT_APPLICATION_URI", APPLICATION_URI)
    await client.set_security(
        SecurityPolicyBasic256Sha256,
        certificate=str(CLIENT_CERT),
        private_key=str(CLIENT_KEY),
        server_certificate=str(SERVER_CERT),
        mode=ua.MessageSecurityMode.SignAndEncrypt,
    )
    if admin:
        username, password = admin_credentials()
        client.set_user(username)
        client.set_password(password)
    return client
