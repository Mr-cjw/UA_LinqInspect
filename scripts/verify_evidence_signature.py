#!/usr/bin/env python3
"""Verify an exported evidence bundle or Edge Core backup manifest signature."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
for path in (REPO_ROOT, SCRIPTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from edge_core.evidence_signing import load_keyring_from_env, verify_document  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", type=Path, help="evidence bundle or manifest JSON file")
    group.add_argument("--backup-dir", type=Path, help="backup directory containing manifest.json")
    args = parser.parse_args()
    try:
        keyring = load_keyring_from_env()
        if not keyring:
            raise ValueError("configure EDGE_EVIDENCE_SIGNING_KEY or previous keys")
        if args.backup_dir:
            from edge_core_backup import validate_backup

            manifest, _database, _images = validate_backup(args.backup_dir, keyring=keyring)
            document = manifest
        else:
            document = json.loads(args.file.expanduser().resolve().read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError("signature document must be a JSON object")
            verification = verify_document(document, keyring)
            if not verification["ok"]:
                raise ValueError(f"signature verification failed: {verification['error']}")
        signature = document.get("signature") or {}
        result = {
            "ok": True,
            "signatureStatus": document.get("signatureStatus"),
            "algorithm": signature.get("algorithm"),
            "keyId": signature.get("keyId"),
            "scope": signature.get("scope"),
        }
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
