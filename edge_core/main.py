"""Run the Edge Core HTTP API and delivery worker."""

from __future__ import annotations

import signal
import threading

from .api import create_api_server
from .authority_lock import SenderAuthorityGuard
from .config import load_config
from .store import DeliveryStore
from .worker import DeliveryWorker


def main() -> int:
    config = load_config()
    store = DeliveryStore(config.db_path)
    authority = None
    worker = None
    if config.delivery_authority == "edge-core":
        authority = SenderAuthorityGuard(lock_dir=config.sender_lock_dir)
        lease = authority.try_acquire()
        if not lease.get("ok"):
            print(
                "Edge Core worker cannot start: sender authority is held by "
                + str(lease.get("owner") or "unknown"),
                flush=True,
            )
            return 3
        authority.start_heartbeat()
        worker = DeliveryWorker(
            store,
            mes_base_url=config.mes_base_url,
            bearer_token=config.mes_token,
            max_attempts=config.max_attempts,
            request_timeout=config.worker_timeout,
            poll_interval=config.worker_interval,
            lease_seconds=config.lease_seconds,
        )
    api = create_api_server(config.api_host, config.api_port, store, config.api_token, config.mes_base_url)
    api_thread = threading.Thread(
        target=api.serve_forever,
        kwargs={"poll_interval": 0.5},
        name="edge-core-api",
        daemon=True,
    )
    if worker is not None:
        worker.start()
    else:
        print("QC_DELIVERY_AUTHORITY=node-red: Edge Core worker disabled", flush=True)
    stop = threading.Event()

    def request_shutdown(_signum=None, _frame=None):
        stop.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    print(f"Edge Core listening on http://{config.api_host}:{config.api_port}", flush=True)
    api_thread.start()
    try:
        stop.wait()
    finally:
        api.shutdown()
        api_thread.join(timeout=5)
        if worker is not None:
            worker.stop()
        if authority is not None:
            authority.release()
        api.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
