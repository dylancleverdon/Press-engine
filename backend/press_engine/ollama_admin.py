"""Check on the local Ollama server and download the extraction model from the UI."""

from __future__ import annotations

import logging
import threading

import httpx

log = logging.getLogger(__name__)

_pull_state: dict[str, str | None] = {"model": None, "status": "idle", "error": None}
_pull_lock = threading.Lock()


def status(base_url: str, model: str) -> dict:
    try:
        r = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=3)
        r.raise_for_status()
        names = [m.get("name", "") for m in r.json().get("models", [])]
    except (httpx.HTTPError, ValueError):
        return {"running": False, "models": [], "has_model": False, "pull": dict(_pull_state)}
    wanted = model if ":" in model else f"{model}:latest"
    return {"running": True, "models": names, "has_model": wanted in names or model in names, "pull": dict(_pull_state)}


def start_pull(base_url: str, model: str) -> bool:
    """Download ``model`` in the background (a few GB; can take a while)."""
    with _pull_lock:
        if _pull_state["status"] == "downloading":
            return False
        _pull_state.update({"model": model, "status": "downloading", "error": None})

    def run() -> None:
        try:
            r = httpx.post(f"{base_url.rstrip('/')}/api/pull", json={"model": model, "stream": False}, timeout=3600)
            r.raise_for_status()
            _pull_state["status"] = "done"
        except httpx.HTTPError as e:
            log.warning("ollama pull failed: %s", e)
            _pull_state.update({"status": "error", "error": str(e)})

    threading.Thread(target=run, daemon=True, name="ollama-pull").start()
    return True
