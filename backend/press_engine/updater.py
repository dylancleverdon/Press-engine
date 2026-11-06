"""Automatic updates for the Mac app.

Releases are published on GitHub by .github/workflows/release-mac.yml with:
* ``press_engine-<version>-py3-none-any.whl`` - the backend (where every feature lives)
* ``Press-Engine-mac.dmg`` - the app shell, rarely changes
* ``desktop-version.txt`` - the shell version inside that DMG

Updating the backend: a new virtualenv is built next to the current one with the
bundled ``uv``, smoke-tested, and recorded in ``<app support>/pending``. The
backend then exits with RESTART_EXIT_CODE; the Mac app starts the pending
version, promotes it if it answers its health check, and otherwise rolls back
and records the version in ``bad_versions`` so it isn't tried again.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import httpx

from . import __version__
from .util.timeutil import utcnow

log = logging.getLogger(__name__)

VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
PYTHON_VERSION = "3.13"


class UpdateError(RuntimeError):
    pass


def parse_version(v: str | None) -> tuple[int, ...]:
    m = VERSION_RE.search(v or "")
    return tuple(int(x) for x in m.groups()) if m else (0,)


def newer(candidate: str | None, current: str | None) -> bool:
    return parse_version(candidate) > parse_version(current)


@dataclass
class Release:
    version: str
    tag: str
    html_url: str
    published_at: str | None
    wheel_url: str | None
    dmg_url: str | None
    desktop_version: str | None


def fetch_latest(http: httpx.Client, cfg) -> Release | None:
    url = f"{cfg.github_api}/repos/{cfg.update_repo}/releases/latest"
    try:
        r = http.get(url, headers={"Accept": "application/vnd.github+json"}, timeout=20)
    except httpx.HTTPError as e:
        raise UpdateError(f"Couldn't reach GitHub: {e}") from e
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise UpdateError(f"GitHub returned {r.status_code}")
    data = r.json()
    assets = {a.get("name", ""): a.get("browser_download_url") for a in data.get("assets", [])}
    wheel = next((u for n, u in assets.items() if n.startswith("press_engine-") and n.endswith(".whl")), None)
    dmg = next((u for n, u in assets.items() if n.endswith(".dmg")), None)
    desktop_version = None
    if assets.get("desktop-version.txt"):
        try:
            v = http.get(assets["desktop-version.txt"], timeout=20, follow_redirects=True)
            if v.status_code == 200:
                desktop_version = v.text.strip()[:32]
        except httpx.HTTPError:
            pass
    tag = data.get("tag_name") or ""
    return Release(version=tag.lstrip("v"), tag=tag, html_url=data.get("html_url") or "",
                   published_at=data.get("published_at"), wheel_url=wheel, dmg_url=dmg, desktop_version=desktop_version)


# --- state shown in the dashboard --------------------------------------------------

def _state_path(cfg) -> Path:
    return cfg.data_dir / "update_state.json"


def read_state(cfg) -> dict:
    try:
        return json.loads(_state_path(cfg).read_text())
    except (OSError, ValueError):
        return {}


def _write_state(cfg, state: dict) -> None:
    _state_path(cfg).write_text(json.dumps(state, indent=2, default=str))


def bad_versions(cfg) -> set[str]:
    if not cfg.app_support_dir:
        return set()
    path = cfg.app_support_dir / "bad_versions"
    return {line.strip() for line in path.read_text().splitlines() if line.strip()} if path.exists() else set()


def check(http: httpx.Client, cfg, *, now: datetime | None = None) -> dict:
    """Look for a newer release and remember what we found."""
    now = now or utcnow()
    state = read_state(cfg)
    state.update({"checked_at": now.isoformat(), "current": __version__, "error": None})
    try:
        rel = fetch_latest(http, cfg)
    except UpdateError as e:
        state["error"] = str(e)
        _write_state(cfg, state)
        return state
    if rel is None:
        state.update({"latest": None, "update_available": False, "desktop_update": None})
    else:
        skip = rel.version in bad_versions(cfg)
        state.update({
            "latest": rel.version,
            "release": asdict(rel),
            "update_available": newer(rel.version, __version__) and bool(rel.wheel_url) and not skip,
            "skipped_bad_version": skip,
            "desktop_update": rel.desktop_version if cfg.desktop_version and newer(rel.desktop_version, cfg.desktop_version) else None,
            "dmg_url": rel.dmg_url,
        })
    _write_state(cfg, state)
    return state


# --- installing ---------------------------------------------------------------------

Runner = Callable[..., subprocess.CompletedProcess]


def uv_env(cfg) -> dict[str, str]:
    env = dict(os.environ)
    env["UV_PYTHON_INSTALL_DIR"] = str(cfg.app_support_dir / "python")
    env.pop("VIRTUAL_ENV", None)
    return env


def install(cfg, rel: Release, *, runner: Runner = subprocess.run) -> Path:
    """Build a fresh venv for ``rel`` and mark it pending. Raises UpdateError on failure."""
    if not (cfg.desktop and cfg.app_support_dir and cfg.uv_path and rel.wheel_url):
        raise UpdateError("Automatic updates only work inside the Mac app")
    target = cfg.app_support_dir / "venvs" / rel.version
    if target.exists():
        shutil.rmtree(target)
    env = uv_env(cfg)
    steps = [
        [str(cfg.uv_path), "venv", "--python", PYTHON_VERSION, "--python-preference", "only-managed", str(target)],
        [str(cfg.uv_path), "pip", "install", "--python", str(target / "bin" / "python"), rel.wheel_url],
        [str(target / "bin" / "python"), "-c", "import press_engine.api.app, press_engine.cli; print(press_engine.__version__)"],
    ]
    for cmd in steps:
        try:
            done = runner(cmd, env=env, capture_output=True, text=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired) as e:
            shutil.rmtree(target, ignore_errors=True)
            raise UpdateError(f"{Path(cmd[0]).name} failed: {e}") from e
        if done.returncode != 0:
            shutil.rmtree(target, ignore_errors=True)
            raise UpdateError(f"{' '.join(cmd[1:3])} failed: {(done.stderr or done.stdout or '').strip()[-500:]}")
    (cfg.app_support_dir / "pending").write_text(rel.version)
    return target


def maybe_update(http: httpx.Client, cfg, *, now: datetime | None = None, runner: Runner = subprocess.run) -> dict:
    """Check, and if there's a newer backend, install it. Returns the state; the
    caller restarts the process when ``installed`` is set."""
    state = check(http, cfg, now=now)
    if not state.get("update_available"):
        return state
    rel = Release(**state["release"])
    try:
        install(cfg, rel, runner=runner)
    except UpdateError as e:
        log.error("update to %s failed: %s", rel.version, e)
        state.update({"error": str(e), "installed": None})
        _write_state(cfg, state)
        return state
    state.update({"installed": rel.version, "installed_at": (now or utcnow()).isoformat()})
    _write_state(cfg, state)
    return state
