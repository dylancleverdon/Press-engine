"""Progress of a running discovery, for the progress bar (one discovery runs at a time)."""

from __future__ import annotations

import threading

_lock = threading.Lock()
_state: dict = {}


def start(engines: list[str]) -> None:
    with _lock:
        _state.clear()
        _state.update({"engines": engines, "engine": None, "step": "Starting", "done": 0, "total": 0})


def step(text: str, *, engine: str | None = None, done: int = 0, total: int = 0) -> None:
    with _lock:
        if not _state:
            return
        if engine is not None:
            _state["engine"] = engine
        _state.update({"step": text, "done": done, "total": total})


def tick(done: int, total: int | None = None) -> None:
    with _lock:
        if _state:
            _state["done"] = done
            if total is not None:
                _state["total"] = total


def finish() -> None:
    with _lock:
        _state.clear()


def current() -> dict:
    with _lock:
        if not _state:
            return {}
        out = dict(_state)
    engines = out.get("engines") or []
    # Overall position: finished engines plus the share of the current one.
    if out.get("engine") in engines:
        i = engines.index(out["engine"])
        part = (out["done"] / out["total"]) if out.get("total") else 0
        out["percent"] = round(100 * (i + min(part, 1)) / (len(engines) + 1))
    elif out.get("step", "").startswith(("Reading", "Merging", "Scoring")):
        out["percent"] = round(100 * len(engines) / (len(engines) + 1) + (100 / (len(engines) + 1)) *
                               ((out["done"] / out["total"]) if out.get("total") else 0.5))
    else:
        out["percent"] = 2
    return out
