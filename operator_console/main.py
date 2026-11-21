"""Run the local operator console."""

from __future__ import annotations

import os
import signal
import threading

from .edge_api import OperatorEdgeClient
from .server import create_operator_server
from .station import StationConfig, UaStationClient


def main() -> int:
    user = os.getenv("OPERATOR_CONSOLE_USER", "").strip()
    password = os.getenv("OPERATOR_CONSOLE_PASSWORD", "")
    edge_token = os.getenv("EDGE_API_TOKEN", "").strip()
    if not user or not password:
        raise SystemExit("OPERATOR_CONSOLE_USER and OPERATOR_CONSOLE_PASSWORD are required")
    if not edge_token:
        raise SystemExit("EDGE_API_TOKEN is required")
    host = os.getenv("OPERATOR_CONSOLE_HOST", "127.0.0.1").strip()
    port = int(os.getenv("OPERATOR_CONSOLE_PORT", "8780"))
    station = UaStationClient(StationConfig.from_env())
    edge = OperatorEdgeClient(
        os.getenv("EDGE_API_BASE_URL", "http://127.0.0.1:8770"),
        edge_token,
        timeout=float(os.getenv("OPERATOR_EDGE_TIMEOUT_SECONDS", "5")),
    )
    server = create_operator_server(
        host,
        port,
        station,
        edge,
        username=user,
        password=password,
    )
    stop = threading.Event()

    def request_shutdown(_signum=None, _frame=None):
        stop.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
    thread.start()
    print(f"Operator console listening on http://{host}:{port}", flush=True)
    try:
        stop.wait()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
