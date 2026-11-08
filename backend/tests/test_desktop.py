from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text

from press_engine import __version__, envfile, runtime, sessions, updater
from press_engine.api.app import create_app
from press_engine.api.deps import create_user
from press_engine.db import Database, upgrade_schema
from press_engine.models import Artist, User
from press_engine.scheduler import Scheduler

GH = "https://api.github.test"


@pytest.fixture(autouse=True)
def clean_runtime(monkeypatch):
    monkeypatch.setattr(runtime, "_server", None)
    monkeypatch.setattr(runtime, "_scheduler", None)
    monkeypatch.setattr(runtime, "_restart", __import__("threading").Event())


@pytest.fixture
def desktop_cfg(cfg, tmp_path):
    support = tmp_path / "support"
    support.mkdir()
    cfg.desktop = True
    cfg.desktop_version = "1.0.0"
    cfg.desktop_login_secret = "s3cret"
    cfg.app_support_dir = support
    cfg.uv_path = support / "bin" / "uv"
    cfg.github_api = GH
    return cfg


class FakeServer:
    should_exit = False


def test_schema_upgrade_adds_new_columns(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    eng = create_engine(url)
    with eng.begin() as c:  # an "old version" of the users table, missing newer columns
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR(120), role VARCHAR(20), token_hash VARCHAR(64))"))
        c.execute(text("INSERT INTO users (name, role, token_hash) VALUES ('D', 'owner', 'x')"))
    db = Database(url)
    db.create_all()  # runs upgrade_schema
    cols = {c["name"] for c in inspect(db.engine).get_columns("users")}
    assert {"email", "notify_replies", "created_at"} <= cols
    with db.session() as s:
        u = s.scalar(select(User))
        assert u.name == "D" and u.notify_replies is False
    assert upgrade_schema(db.engine) == []  # idempotent


def test_session_cookie_sign_and_verify(tmp_path):
    value = sessions.sign(tmp_path, 7)
    assert sessions.verify(tmp_path, value) == 7
    assert sessions.verify(tmp_path, value.replace(".", "x", 1)) is None
    assert sessions.verify(tmp_path, value[:-1] + ("0" if value[-1] != "0" else "1")) is None
    assert sessions.verify(tmp_path, sessions.sign(tmp_path, 7, now=time.time() - sessions.MAX_AGE - 10)) is None
    assert (tmp_path / "session.key").stat().st_mode & 0o777 == 0o600


def test_envfile_update_keeps_other_lines(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# my keys\nPRESS_LASTFM_API_KEY=old\nOTHER=1\n")
    envfile.update(path, {"PRESS_LASTFM_API_KEY": "new key", "PRESS_BRAVE_API_KEY": "b123", "OTHER": None})
    text_ = path.read_text()
    assert text_.startswith("# my keys\n") and "OTHER" not in text_
    assert envfile.read(path) == {"PRESS_LASTFM_API_KEY": "new key", "PRESS_BRAVE_API_KEY": "b123"}
    assert path.stat().st_mode & 0o777 == 0o600
    assert envfile.mask("abcdefghij") == "••••••ghij"


@pytest.fixture
def desktop_client(db, services, desktop_cfg):
    services.cfg = desktop_cfg
    return TestClient(create_app(db=db, svc=services, start_scheduler=False))


def test_first_run_setup_flow(desktop_client, db, desktop_cfg):
    r = desktop_client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/setup"
    page = desktop_client.get("/setup")
    assert "Open Press Engine when I log in" in page.text
    r = desktop_client.post("/setup", data={"name": "Dylan", "band": "Night Swim", "timezone": "Europe/Copenhagen",
                                            "seeds": "Athletic Progression\nMammal Hands", "launch_at_login": "true"})
    assert r.status_code == 200 and "pe_" in r.text
    assert desktop_client.get("/").status_code == 200  # logged in via the session cookie
    with db.session() as s:
        assert s.scalar(select(User)).role == "owner"
        assert {a.name for a in s.scalars(select(Artist))} == {"Athletic Progression", "Mammal Hands"}
        from press_engine.appsettings import load_settings

        assert load_settings(s).band_name == "Night Swim"
    assert runtime.read_desktop(desktop_cfg)["launch_at_login"] is True
    assert desktop_client.get("/setup", follow_redirects=False).status_code == 303  # only once


def test_desktop_login_with_secret(desktop_client, db):
    with db.session() as s:
        create_user(s, "D", role="owner")
    assert desktop_client.get("/desktop-login?secret=wrong", follow_redirects=False).headers["location"] == "/login"
    r = desktop_client.get("/desktop-login?secret=s3cret&next=/contacts", follow_redirects=False)
    assert r.headers["location"] == "/contacts" and sessions.COOKIE in r.cookies
    assert desktop_client.get("/contacts").status_code == 200


def test_connections_save_keys_and_restart(desktop_client, db, desktop_cfg):
    with db.session() as s:
        create_user(s, "D", role="owner")
    desktop_client.get("/desktop-login?secret=s3cret")
    page = desktop_client.get("/connections")
    assert page.status_code == 200 and "Connect Google" not in page.text and "Upload" in page.text
    fake = FakeServer()
    runtime.attach_server(fake)
    r = desktop_client.post("/connections/keys", data={"PRESS_LASTFM_API_KEY": "lfm-123456", "PRESS_SEARCH_PROVIDER": "brave"},
                            follow_redirects=False)
    assert "restarting" in r.headers["location"]
    assert envfile.read(desktop_cfg.env_path) == {"PRESS_LASTFM_API_KEY": "lfm-123456", "PRESS_SEARCH_PROVIDER": "brave"}
    assert runtime.restart_requested()
    time.sleep(1.0)
    assert fake.should_exit
    # A masked secret posted back unchanged is kept.
    page = desktop_client.get("/connections")
    assert "••••••3456" in page.text
    desktop_client.post("/connections/keys", data={"PRESS_LASTFM_API_KEY": "••••••3456"})
    assert envfile.read(desktop_cfg.env_path)["PRESS_LASTFM_API_KEY"] == "lfm-123456"
    # Google client upload is validated.
    bad = desktop_client.post("/connections/google/client", files={"file": ("x.json", b'{"web": {}}', "application/json")},
                              follow_redirects=False)
    assert "Desktop+app" in bad.headers["location"] or "Desktop%20app" in bad.headers["location"]


def test_export_saves_to_downloads(desktop_client, db, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with db.session() as s:
        create_user(s, "D", role="owner")
    desktop_client.get("/desktop-login?secret=s3cret")
    assert "Export CSV to Downloads" in desktop_client.get("/contacts").text
    r = desktop_client.post("/export/save", follow_redirects=False)
    assert "Downloads" in r.headers["location"]
    assert list((tmp_path / "Downloads").glob("press-contacts-*.csv"))


def _release(router, version="9.0.0", desktop="1.1.0"):
    router.json("GET", f"{GH}/repos/dylancleverdon/Press-engine/releases/latest", {
        "tag_name": f"v{version}", "html_url": "https://github.test/r", "published_at": "2026-11-06T10:00:00Z",
        "assets": [
            {"name": f"press_engine-{version}-py3-none-any.whl", "browser_download_url": f"https://dl.test/press_engine-{version}-py3-none-any.whl"},
            {"name": "Press-Engine-mac.dmg", "browser_download_url": "https://dl.test/Press-Engine-mac.dmg"},
            {"name": "desktop-version.txt", "browser_download_url": "https://dl.test/desktop-version.txt"},
        ]})
    router.add("GET", "https://dl.test/desktop-version.txt", lambda r: httpx.Response(200, text=desktop + "\n"))


def test_updater_check_install_and_bad_versions(services, router, desktop_cfg):
    _release(router)
    state = updater.check(services.http, desktop_cfg)
    assert state["update_available"] and state["latest"] == "9.0.0" and state["desktop_update"] == "1.1.0"
    assert updater.read_state(desktop_cfg)["latest"] == "9.0.0"

    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        if cmd[1] == "venv":
            (Path(cmd[-1]) / "bin").mkdir(parents=True)
        assert kw["env"]["UV_PYTHON_INSTALL_DIR"].endswith("support/python")
        return subprocess.CompletedProcess(cmd, 0, stdout="9.0.0", stderr="")

    state = updater.maybe_update(services.http, desktop_cfg, runner=runner)
    assert state["installed"] == "9.0.0"
    assert (desktop_cfg.app_support_dir / "pending").read_text() == "9.0.0"
    assert calls[0][1:3] == ["venv", "--python"] and "only-managed" in calls[0]
    assert calls[1][-1].endswith("press_engine-9.0.0-py3-none-any.whl")

    # A version the app rolled back is never offered again.
    (desktop_cfg.app_support_dir / "bad_versions").write_text("9.0.0\n")
    assert updater.check(services.http, desktop_cfg)["update_available"] is False


def test_updater_failure_cleans_up(services, router, desktop_cfg):
    _release(router)

    def runner(cmd, **kw):
        if cmd[1] == "venv":
            Path(cmd[-1]).mkdir(parents=True)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 1, "", "No solution found when resolving dependencies")

    state = updater.maybe_update(services.http, desktop_cfg, runner=runner)
    assert state.get("installed") is None and "No solution found" in state["error"]
    assert not (desktop_cfg.app_support_dir / "venvs" / "9.0.0").exists()
    assert not (desktop_cfg.app_support_dir / "pending").exists()


def test_no_update_when_current_or_outside_app(services, router, cfg):
    _release(router, version=__version__)
    cfg.github_api = GH
    state = updater.check(services.http, cfg)
    assert state["update_available"] is False and state["desktop_update"] is None
    assert updater.newer("0.10.0", "0.9.9") and not updater.newer("v0.2.0", "0.2.0")


def test_scheduler_update_tick_installs_and_restarts(db, services, router, desktop_cfg, monkeypatch):
    services.cfg = desktop_cfg
    _release(router)
    restarts = []
    monkeypatch.setattr(runtime, "request_restart", lambda reason, delay=0.8: restarts.append(reason) or True)
    monkeypatch.setattr(updater, "install", lambda cfg, rel, runner=None: (cfg.app_support_dir / "pending").write_text(rel.version))
    clock = {"now": datetime(2026, 11, 6, 10, 0)}
    sched = Scheduler(db, services, clock=lambda: clock["now"])
    from press_engine.appsettings import AppSettings

    sched.update_tick(clock["now"], AppSettings())  # primes: first check ~2 minutes after start
    assert restarts == []
    clock["now"] += timedelta(minutes=3)
    sched.update_tick(clock["now"], AppSettings())
    assert restarts == []  # installed, but the banner counts down first
    banner = updater.banner(desktop_cfg, clock["now"])
    assert banner["phase"] == "ready" and banner["version"] == "9.0.0" and banner["restart_in"] == 60
    from press_engine.jobs import last_run

    with db.session() as s:
        assert last_run(s, "update").stats["installed"] == "9.0.0"
    from press_engine.api import views

    clock["now"] += timedelta(seconds=61)
    views.RUNNING["discover"] = 1  # never restarts in the middle of a discovery run
    try:
        sched.restart_tick(clock["now"], AppSettings())
        assert restarts == []
    finally:
        views.RUNNING.clear()
    sched.restart_tick(clock["now"], AppSettings())
    assert restarts == ["update to 9.0.0"]

    # "Later" waits for the night.
    restarts.clear()
    updater.set_phase(desktop_cfg, "ready", postponed=True)
    sched.restart_tick(datetime(2026, 11, 6, 14, 0), AppSettings(timezone="UTC"))
    assert restarts == []
    sched.restart_tick(datetime(2026, 11, 7, 2, 0), AppSettings(timezone="UTC"))
    assert restarts == ["update to 9.0.0"]
    restarts.clear()
    sched.update_tick(clock["now"] + timedelta(hours=1), AppSettings(auto_update=False))
    assert restarts == []


def test_desktop_json_round_trip(desktop_cfg):
    assert runtime.read_desktop(desktop_cfg) == runtime.DESKTOP_DEFAULTS
    runtime.write_desktop(desktop_cfg, host="0.0.0.0")
    assert json.loads((desktop_cfg.app_support_dir / "desktop.json").read_text())["host"] == "0.0.0.0"
