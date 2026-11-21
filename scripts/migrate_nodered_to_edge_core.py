#!/usr/bin/env python3
"""Dry-run or apply an offline Node-RED queue migration into Edge Core."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from edge_core.migration import MigrationError, migrate  # noqa: E402


def default_source() -> Path:
    return Path(os.getenv("QC_DB_PATH") or REPO_ROOT / "data" / "qc-queue.sqlite")


def default_target() -> Path:
    return Path(os.getenv("EDGE_DB_PATH") or REPO_ROOT / "data" / "edge_core.sqlite3")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=default_source(), help="Node-RED SQLite queue")
    parser.add_argument("--target", type=Path, default=default_target(), help="Edge Core SQLite database")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the migration; without this flag the command is a dry run",
    )
    args = parser.parse_args()
    source = args.source.expanduser().resolve()
    target = args.target.expanduser().resolve()
    try:
        summary = migrate(source, target, apply=args.apply)
    except (MigrationError, OSError, ValueError) as exc:
        summary = {
            "ok": False,
            "dryRun": not args.apply,
            "source": str(source),
            "target": str(target),
            "error": f"{type(exc).__name__}: {exc}",
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
