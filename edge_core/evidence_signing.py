"""HMAC signing and key rotation for evidence bundles and backup manifests."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

SIGNATURE_ALGORITHM = "HMAC-SHA256"
SIGNATURE_SCOPE = "canonical-json-excluding-signature"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_keyring_from_env(env: dict[str, str] | None = None) -> dict[str, str]:
    values = os.environ if env is None else env
    keyring: dict[str, str] = {}
    previous = values.get("EDGE_EVIDENCE_SIGNING_PREVIOUS_KEYS", "").strip()
    if previous:
        try:
            parsed = json.loads(previous)
        except json.JSONDecodeError as exc:
            raise ValueError("EDGE_EVIDENCE_SIGNING_PREVIOUS_KEYS must be a JSON object") from exc
        if not isinstance(parsed, dict):
            raise ValueError("EDGE_EVIDENCE_SIGNING_PREVIOUS_KEYS must be a JSON object")
        for key_id, key in parsed.items():
            if not str(key_id).strip() or not str(key):
                raise ValueError("previous signing keys require non-empty IDs and values")
            keyring[str(key_id).strip()] = str(key)
    current = values.get("EDGE_EVIDENCE_SIGNING_KEY", "").strip()
    current_id = values.get("EDGE_EVIDENCE_SIGNING_KEY_ID", "primary").strip() or "primary"
    if current:
        keyring[current_id] = current
    return keyring


def sign_document(document: dict[str, Any], *, key: str, key_id: str) -> dict[str, Any]:
    if not key:
        raise ValueError("signing key is required")
    if not key_id:
        raise ValueError("signing key ID is required")
    unsigned = {name: value for name, value in document.items() if name != "signature"}
    value = hmac.new(
        key.encode("utf-8"),
        canonical_json(unsigned).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        **unsigned,
        "signature": {
            "algorithm": SIGNATURE_ALGORITHM,
            "keyId": key_id,
            "value": value,
            "scope": SIGNATURE_SCOPE,
        },
    }


def verify_document(document: dict[str, Any], keyring: dict[str, str]) -> dict[str, Any]:
    signature = document.get("signature")
    if not isinstance(signature, dict):
        return {"ok": False, "error": "MISSING_SIGNATURE", "keyId": ""}
    if signature.get("algorithm") != SIGNATURE_ALGORITHM:
        return {"ok": False, "error": "UNSUPPORTED_ALGORITHM", "keyId": str(signature.get("keyId") or "")}
    if signature.get("scope") != SIGNATURE_SCOPE:
        return {"ok": False, "error": "UNSUPPORTED_SCOPE", "keyId": str(signature.get("keyId") or "")}
    key_id = str(signature.get("keyId") or "")
    key = keyring.get(key_id)
    if not key:
        return {"ok": False, "error": "UNKNOWN_KEY_ID", "keyId": key_id}
    expected = str(signature.get("value") or "")
    unsigned = {name: value for name, value in document.items() if name != "signature"}
    actual = hmac.new(
        key.encode("utf-8"),
        canonical_json(unsigned).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(actual, expected):
        return {"ok": False, "error": "SIGNATURE_MISMATCH", "keyId": key_id}
    return {"ok": True, "error": "", "keyId": key_id}
