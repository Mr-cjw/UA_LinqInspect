#!/usr/bin/env python3
"""Run the Edge Core OPC UA receipt writeback adapter."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from edge_core.ua_writeback import (  # noqa: E402
    EdgeCoreApiClient,
    UaReceiptWriter,
    UaWritebackConfig,
    process_pending,
)


async def run(args) -> int:
    config = UaWritebackConfig.from_env()
    api = EdgeCoreApiClient(config.edge_api_base_url, config.edge_api_token, timeout=config.request_timeout)
    writer = UaReceiptWriter(config)
    while True:
        try:
            summary = await process_pending(api, writer, limit=args.limit or config.batch_limit)
        except Exception as exc:
            summary = {"listed": 0, "confirmed": 0, "failed": 0, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        if args.once:
            return 0 if not summary.get("error") and not summary.get("failed") else 1
        await asyncio.sleep(max(0.5, args.interval))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="process one batch and exit")
    parser.add_argument("--interval", type=float, default=2.0, help="poll interval in loop mode")
    parser.add_argument("--limit", type=int, default=0, help="maximum deliveries per batch")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
