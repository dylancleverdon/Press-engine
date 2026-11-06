"""Process-level helpers for running inside the Mac app.

The app (desktop/src-tauri) starts ``press-engine serve`` and restarts it when it
exits with RESTART_EXIT_CODE: after an update, after API keys change, or when
the listening address changes. Settings the app itself needs (port, whether the
phone may connect, launch at login) live in ``desktop.json`` next to the venvs.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

RESTART_EXIT_CODE = 75

_server: Any = None  # the running uvicorn.Server
_scheduler: Any = None  # the background Scheduler, if any
_restart = threading.Event()
_lock = threading.Lock()


def attach_server(server: Any) -> None:
    global _server
    _server = server


def attach_scheduler(scheduler: Any) -> None:
    global _scheduler
    _scheduler = scheduler


def restart_requested() -> bool:
    return _restart.is_set()


def can_restart(cfg) -> bool:
    return bool(cfg.desktop) and _server is not None


def request_restart(reason: str, *, delay: float = 0.8) -> bool:
    """Ask uvicorn to shut down gracefully; the Mac app starts us again.

    Uses ``server.should_exit`` rather than a signal: uvicorn re-raises captured
    signals after shutting down, which would kill the process with SIGTERM
    instead of letting it exit with RESTART_EXIT_CODE.
    """
    with _lock:
        if _server is None:
            log.info("restart requested (%s) but not running under the Mac app", reason)
            return False
        log.info("restarting: %s", reason)
        _restart.set()

    def stop() -> None:
        if _scheduler is not None:
            _scheduler.quiesce()  # never cut off an email halfway through sending
        _server.should_exit = True

    threading.Timer(delay, stop).start()  # let the current HTTP response finish first
    return True


# --- desktop.json -----------------------------------------------------------------

DESKTOP_DEFAULTS: dict[str, Any] = {"host": "127.0.0.1", "port": 8000, "launch_at_login": True}


def desktop_file(cfg) -> Path | None:
    return cfg.app_support_dir / "desktop.json" if cfg.app_support_dir else None


def read_desktop(cfg) -> dict[str, Any]:
    path = desktop_file(cfg)
    data = dict(DESKTOP_DEFAULTS)
    if path and path.exists():
        try:
            data.update(json.loads(path.read_text()))
        except (OSError, ValueError):
            log.warning("could not read %s", path)
    return data


def write_desktop(cfg, **changes: Any) -> dict[str, Any]:
    path = desktop_file(cfg)
    if path is None:
        raise RuntimeError("Not running inside the Mac app")
    data = read_desktop(cfg)
    data.update(changes)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)
    return data


def lan_addresses() -> list[str]:
    """Best guess at this computer's address on the local network / Tailscale."""
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # TEST-NET address: nothing is actually sent
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found and not ip.startswith("127."):
                found.append(ip)
    except OSError:
        pass
    return found
