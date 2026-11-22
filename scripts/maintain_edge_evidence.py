#!/usr/bin/env python3
"""Inspect evidence-image capacity and optionally remove old unreferenced images."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from edge_core.store import DeliveryStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show image count, bytes, quotas and free space")
    cleanup = sub.add_parser("cleanup", help="delete old images that no delivery references")
    cleanup.add_argument("--retention-days", type=int, default=int(os.getenv("EDGE_EVIDENCE_RETENTION_DAYS", "90")))
    cleanup.add_argument("--apply", action="store_true", help="delete candidates; default is dry-run")

    args = parser.parse_args()
    db = Path(os.getenv("EDGE_DB_PATH", "data/edge_core.sqlite3")).expanduser()
    image_dir = Path(os.getenv("EDGE_EVIDENCE_IMAGE_DIR", "data/evidence-images")).expanduser()
    store = DeliveryStore(
        db,
        evidence_image_dir=image_dir,
        image_max_bytes=int(os.getenv("EDGE_EVIDENCE_IMAGE_MAX_BYTES", str(8 * 1024 * 1024))),
        image_max_total_bytes=int(os.getenv("EDGE_EVIDENCE_IMAGE_MAX_TOTAL_BYTES", str(1024 * 1024 * 1024))),
        image_max_files=int(os.getenv("EDGE_EVIDENCE_IMAGE_MAX_FILES", "10000")),
        image_min_free_bytes=int(os.getenv("EDGE_EVIDENCE_IMAGE_MIN_FREE_BYTES", str(256 * 1024 * 1024))),
    )
    if args.command == "status":
        result = {"ok": True, **store.evidence_image_capacity()}
    else:
        result = store.cleanup_unreferenced_evidence_images(
            retention_days=args.retention_days,
            dry_run=not args.apply,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
