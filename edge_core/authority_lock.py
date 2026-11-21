"""Cross-process single-sender lease shared with Node-RED."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

DEFAULT_LEASE_SECONDS = 10.0


def default_lock_dir() -> Path:
    data_dir = Path(os.getenv("QC_DATA_DIR") or Path.cwd() / "data")
    return Path(os.getenv("QC_SENDER_LOCK_DIR") or data_dir / ".sender-authority").expanduser().resolve()


def process_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError, OverflowError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class SenderAuthorityGuard:
    def __init__(
        self,
        *,
        owner: str = "edge-core",
        lock_dir: str | Path | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ):
        self.owner = owner
        self.dir = Path(lock_dir or default_lock_dir()).expanduser().resolve()
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.token = str(uuid.uuid4())
        self.held = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _meta(self) -> dict[str, Any]:
        try:
            value = json.loads((self.dir / "owner.json").read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            return {}

    def _fresh(self, meta: dict[str, Any]) -> bool:
        if not meta.get("token") or not process_alive(meta.get("pid")):
            return False
        try:
            heartbeat = float(meta.get("heartbeat", 0)) / 1000.0
        except (TypeError, ValueError, OverflowError):
            return False
        return time.time() - heartbeat <= self.lease_seconds

    def _write_meta(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        meta = {
            "owner": self.owner,
            "token": self.token,
            "pid": os.getpid(),
            "heartbeat": round(time.time() * 1000),
        }
        temporary = self.dir / f".owner-{os.getpid()}-{time.time_ns()}.tmp"
        temporary.write_text(json.dumps(meta), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.dir / "owner.json")

    def try_acquire(self) -> dict[str, Any]:
        if self.held:
            return {"ok": True, "owner": self.owner}
        try:
            self.dir.mkdir(mode=0o700)
            self._write_meta()
            self.held = True
            return {"ok": True, "owner": self.owner}
        except FileExistsError:
            pass
        current = self._meta()
        if self._fresh(current):
            return {"ok": False, "owner": str(current.get("owner") or "unknown"), "reason": "held"}
        stale = self.dir.with_name(f"{self.dir.name}.stale-{os.getpid()}-{time.time_ns()}")
        try:
            os.rename(self.dir, stale)
            shutil.rmtree(stale, ignore_errors=True)
        except FileNotFoundError:
            pass
        except OSError:
            return {"ok": False, "owner": str(current.get("owner") or "unknown"), "reason": "stale-race"}
        try:
            self.dir.mkdir(mode=0o700)
            self._write_meta()
            self.held = True
            return {"ok": True, "owner": self.owner}
        except OSError as exc:
            winner = self._meta()
            return {"ok": False, "owner": str(winner.get("owner") or "unknown"), "reason": str(exc)}

    def heartbeat(self) -> bool:
        if not self.held:
            return False
        if self._meta().get("token") != self.token:
            self.held = False
            return False
        self._write_meta()
        return True

    def start_heartbeat(self, interval_seconds: float = 2.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(max(0.5, interval_seconds)):
                if not self.heartbeat():
                    return

        self._thread = threading.Thread(target=loop, name="sender-authority-heartbeat", daemon=True)
        self._thread.start()

    def release(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        if not self.held:
            return
        if self._meta().get("token") == self.token:
            try:
                (self.dir / "owner.json").unlink()
            except FileNotFoundError:
                pass
            try:
                self.dir.rmdir()
            except OSError:
                pass
        self.held = False

    def status(self) -> dict[str, Any]:
        current = self._meta()
        return {
            "owner": self.owner,
            "held": self.held,
            "currentOwner": str(current.get("owner") or ""),
            "leaseFresh": self._fresh(current),
            "lockDir": str(self.dir),
        }
