"""Read and update KEY=value lines in the data folder's .env (API keys from the UI)."""

from __future__ import annotations

import os
import re
from pathlib import Path

LINE = re.compile(r"^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$")

# Keys the Connections page may edit, with whether they're secret.
EDITABLE: dict[str, bool] = {
    "PRESS_LASTFM_API_KEY": True,
    "PRESS_SEARCH_PROVIDER": False,
    "PRESS_BRAVE_API_KEY": True,
    "PRESS_SERPAPI_API_KEY": True,
    "PRESS_YOUTUBE_API_KEY": True,
    "PRESS_SPOTIFY_CLIENT_ID": False,
    "PRESS_SPOTIFY_CLIENT_SECRET": True,
    "PRESS_EXTRACT_LLM_PROVIDER": False,
    "PRESS_EXTRACT_LLM_MODEL": False,
    "PRESS_EXTRACT_LLM_BASE_URL": False,
    "PRESS_SCORE_LLM_PROVIDER": False,
    "PRESS_SCORE_LLM_MODEL": False,
    "PRESS_SCORE_LLM_BASE_URL": False,
    "PRESS_SCORE_LLM_API_KEY": True,
    "PRESS_SCORE_LLM_EFFORT": False,
}


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def read(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            m = LINE.match(line)
            if m:
                values[m.group(1)] = _unquote(m.group(2))
    return values


def _quote(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"' if re.search(r"[\s#\"']", v) else v


def update(path: Path, changes: dict[str, str | None]) -> None:
    """Set (or, with None/"" values, remove) keys, keeping other lines and comments."""
    lines = path.read_text().splitlines() if path.exists() else []
    pending = dict(changes)
    out: list[str] = []
    for line in lines:
        m = LINE.match(line)
        if m and m.group(1) in pending:
            value = pending.pop(m.group(1))
            if value:
                out.append(f"{m.group(1)}={_quote(value)}")
            continue
        out.append(line)
    for key, value in pending.items():
        if value:
            out.append(f"{key}={_quote(value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def mask(value: str | None) -> str:
    if not value:
        return ""
    return "•" * 6 + value[-4:] if len(value) > 6 else "•" * len(value)
