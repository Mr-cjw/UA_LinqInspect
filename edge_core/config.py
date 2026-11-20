"""Environment configuration for the Edge Core Service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EdgeConfig:
    db_path: Path
    api_host: str
    api_port: int
    api_token: str
    mes_base_url: str
    mes_token: str
    worker_interval: float
    worker_timeout: float
    max_attempts: int
    lease_seconds: float


def load_config(env: dict[str, str] | None = None) -> EdgeConfig:
    values = os.environ if env is None else env
    token = values.get("EDGE_API_TOKEN", "").strip()
    if not token:
        raise ValueError("EDGE_API_TOKEN is required")
    return EdgeConfig(
        db_path=Path(values.get("EDGE_DB_PATH", "data/edge_core.sqlite3")).expanduser(),
        api_host=values.get("EDGE_API_HOST", "127.0.0.1"),
        api_port=int(values.get("EDGE_API_PORT", "8770")),
        api_token=token,
        mes_base_url=values.get("MES_BASE_URL", "http://127.0.0.1:1880").rstrip("/"),
        mes_token=values.get("MES_BEARER_TOKEN", ""),
        worker_interval=max(0.05, float(values.get("EDGE_WORKER_INTERVAL_SECONDS", "0.5"))),
        worker_timeout=max(0.2, float(values.get("EDGE_MES_TIMEOUT_SECONDS", "3"))),
        max_attempts=max(1, int(values.get("EDGE_MAX_ATTEMPTS", "4"))),
        lease_seconds=max(5.0, float(values.get("EDGE_LEASE_SECONDS", "30"))),
    )
