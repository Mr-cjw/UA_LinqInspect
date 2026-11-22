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
    mes_basic_user: str
    mes_basic_pass: str
    worker_interval: float
    worker_timeout: float
    max_attempts: int
    lease_seconds: float
    delivery_authority: str
    sender_lock_dir: Path
    evidence_image_dir: Path
    evidence_image_max_bytes: int
    evidence_image_max_total_bytes: int
    evidence_image_max_files: int
    evidence_image_min_free_bytes: int


def load_config(env: dict[str, str] | None = None) -> EdgeConfig:
    values = os.environ if env is None else env
    token = values.get("EDGE_API_TOKEN", "").strip()
    if not token:
        raise ValueError("EDGE_API_TOKEN is required")
    mes_bearer = values.get("MES_BEARER_TOKEN", "").strip()
    mes_basic_user = values.get("EDGE_MES_BASIC_USER", "").strip()
    mes_basic_pass = values.get("EDGE_MES_BASIC_PASS", "")
    if bool(mes_basic_user) != bool(mes_basic_pass):
        raise ValueError("EDGE_MES_BASIC_USER and EDGE_MES_BASIC_PASS must be configured together")
    if mes_bearer and mes_basic_user:
        raise ValueError("configure either MES_BEARER_TOKEN or MES basic auth, not both")
    authority = values.get("QC_DELIVERY_AUTHORITY", "node-red").strip().lower()
    if authority not in ("node-red", "edge-core"):
        raise ValueError("QC_DELIVERY_AUTHORITY must be node-red or edge-core")
    return EdgeConfig(
        db_path=Path(values.get("EDGE_DB_PATH", "data/edge_core.sqlite3")).expanduser(),
        api_host=values.get("EDGE_API_HOST", "127.0.0.1"),
        api_port=int(values.get("EDGE_API_PORT", "8770")),
        api_token=token,
        mes_base_url=values.get("MES_BASE_URL", "http://127.0.0.1:1880").rstrip("/"),
        mes_token=mes_bearer,
        mes_basic_user=mes_basic_user,
        mes_basic_pass=mes_basic_pass,
        worker_interval=max(0.05, float(values.get("EDGE_WORKER_INTERVAL_SECONDS", "0.5"))),
        worker_timeout=max(0.2, float(values.get("EDGE_MES_TIMEOUT_SECONDS", "3"))),
        max_attempts=max(1, int(values.get("EDGE_MAX_ATTEMPTS", "4"))),
        lease_seconds=max(5.0, float(values.get("EDGE_LEASE_SECONDS", "30"))),
        delivery_authority=authority,
        sender_lock_dir=Path(
            values.get("QC_SENDER_LOCK_DIR", "data/.sender-authority")
        ).expanduser(),
        evidence_image_dir=Path(
            values.get("EDGE_EVIDENCE_IMAGE_DIR", "data/evidence-images")
        ).expanduser(),
        evidence_image_max_bytes=max(
            1, int(values.get("EDGE_EVIDENCE_IMAGE_MAX_BYTES", str(8 * 1024 * 1024)))
        ),
        evidence_image_max_total_bytes=max(
            1, int(values.get("EDGE_EVIDENCE_IMAGE_MAX_TOTAL_BYTES", str(1024 * 1024 * 1024)))
        ),
        evidence_image_max_files=max(
            1, int(values.get("EDGE_EVIDENCE_IMAGE_MAX_FILES", "10000"))
        ),
        evidence_image_min_free_bytes=max(
            0, int(values.get("EDGE_EVIDENCE_IMAGE_MIN_FREE_BYTES", str(256 * 1024 * 1024)))
        ),
    )
