"""First-run setup, Connections (API keys, Google, Ollama, phone/extension) and
app updates - everything needed to run Press Engine from the Mac app without a
terminal."""

from __future__ import annotations

import secrets
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import __version__, envfile, ollama_admin, runtime, sessions, updater
from ..appsettings import load_settings, update_settings
from ..discovery.lastfm import add_seed
from ..models import User
from ..outreach.google_auth import GoogleAuthError
from ..util.timeutil import system_timezone_name
from .deps import create_user, get_db, get_svc, rotate_token, web_owner

router = APIRouter(include_in_schema=False)


def _templates():
    from .routes_web import page, templates

    return templates, page


def login_response(request: Request, user: User, next_url: str = "/") -> RedirectResponse:
    resp = RedirectResponse(next_url if next_url.startswith("/") else "/", status_code=303)
    resp.set_cookie(sessions.COOKIE, sessions.sign(request.app.state.svc.cfg.data_dir, user.id), httponly=True,
                    samesite="lax", max_age=sessions.MAX_AGE)
    return resp


# --- first run ----------------------------------------------------------------------

def _no_users(session: Session) -> bool:
    return not session.scalar(select(func.count(User.id)))


@router.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request, session: Session = Depends(get_db), svc=Depends(get_svc)):
    if not _no_users(session):
        return RedirectResponse("/", status_code=303)
    templates, _ = _templates()
    return templates.TemplateResponse(request, "setup.html", {
        "timezone": system_timezone_name(), "desktop": svc.cfg.desktop, "error": None,
    })


@router.post("/setup")
def setup(request: Request, name: str = Form(...), band: str = Form(...), timezone: str = Form(...),
          seeds: str = Form("Athletic Progression"), launch_at_login: bool = Form(False),
          session: Session = Depends(get_db), svc=Depends(get_svc)):
    if not _no_users(session):
        return RedirectResponse("/", status_code=303)
    user, token = create_user(session, name.strip() or "D", role="owner")
    update_settings(session, band_name=band.strip() or "Our Band", sender_name=user.name,
                    signature=f"{user.name}\n{band.strip()}".strip(), timezone=timezone.strip() or "UTC")
    for seed in seeds.replace(",", "\n").splitlines():
        if seed.strip():
            add_seed(session, seed.strip())
    if svc.cfg.desktop:
        runtime.write_desktop(svc.cfg, launch_at_login=launch_at_login)
    session.commit()
    templates, _ = _templates()
    resp = templates.TemplateResponse(request, "setup_done.html", {"user": user, "token": token, "desktop": svc.cfg.desktop})
    resp.set_cookie(sessions.COOKIE, sessions.sign(svc.cfg.data_dir, user.id), httponly=True, samesite="lax",
                    max_age=sessions.MAX_AGE)
    return resp


@router.get("/desktop-login")
def desktop_login(request: Request, secret: str = "", next: str = "/", session: Session = Depends(get_db), svc=Depends(get_svc)):
    """The Mac app opens this with a per-launch secret so D never has to paste a token."""
    expected = svc.cfg.desktop_login_secret
    if not expected or not secrets.compare_digest(secret, expected):
        return RedirectResponse("/login", status_code=303)
    owner = session.scalar(select(User).where(User.role == "owner").order_by(User.id))
    if owner is None:
        return RedirectResponse("/setup", status_code=303)
    return login_response(request, owner, next)


# --- connections ---------------------------------------------------------------------

KEY_GROUPS = [
    ("Discovery sources", [
        ("PRESS_LASTFM_API_KEY", "Last.fm API key", "Free: last.fm/api/account/create"),
        ("PRESS_SEARCH_PROVIDER", "Web search provider", "brave or serpapi"),
        ("PRESS_BRAVE_API_KEY", "Brave Search API key", "api-dashboard.search.brave.com"),
        ("PRESS_SERPAPI_API_KEY", "SerpAPI key", "serpapi.com"),
        ("PRESS_YOUTUBE_API_KEY", "YouTube Data API key", "Google Cloud Console > APIs > YouTube Data API v3"),
        ("PRESS_SPOTIFY_CLIENT_ID", "Spotify client ID", "developer.spotify.com/dashboard"),
        ("PRESS_SPOTIFY_CLIENT_SECRET", "Spotify client secret", ""),
    ]),
    ("Models", [
        ("PRESS_EXTRACT_LLM_PROVIDER", "Extraction model provider", "ollama, openai (compatible server) or none"),
        ("PRESS_EXTRACT_LLM_MODEL", "Extraction model", "qwen2.5:3b"),
        ("PRESS_EXTRACT_LLM_BASE_URL", "Extraction server", "http://localhost:11434"),
        ("PRESS_SCORE_LLM_PROVIDER", "Scoring model provider", "none, ollama, openai or anthropic"),
        ("PRESS_SCORE_LLM_MODEL", "Scoring model", "e.g. qwen2.5:14b or claude-opus-5-5"),
        ("PRESS_SCORE_LLM_BASE_URL", "Scoring server (ollama/openai only)", ""),
        ("PRESS_SCORE_LLM_API_KEY", "Scoring API key (Anthropic / hosted)", ""),
        ("PRESS_SCORE_LLM_EFFORT", "Effort (anthropic only)", "low, medium, high or blank"),
    ]),
]


def _restart_or_message(svc, what: str) -> str:
    if runtime.can_restart(svc.cfg) and runtime.request_restart(what):
        return f"{what}. Press Engine is restarting - this page reloads in a few seconds."
    return f"{what}. Restart press-engine serve to apply."


@router.get("/connections", response_class=HTMLResponse)
def connections(request: Request, user: User = Depends(web_owner), session: Session = Depends(get_db), svc=Depends(get_svc)):
    cfg = svc.cfg
    _, page = _templates()
    current = envfile.read(cfg.env_path)
    groups = []
    for title, keys in KEY_GROUPS:
        rows = []
        for key, label, hint in keys:
            secret = envfile.EDITABLE.get(key, False)
            value = current.get(key, "")
            rows.append({"key": key, "label": label, "hint": hint, "secret": secret,
                         "value": envfile.mask(value) if secret else value, "set": bool(value)})
        groups.append((title, rows))
    sources = {
        "Last.fm": bool(svc.lastfm), "Web search": svc.search.name if svc.search else False,
        "YouTube": bool(svc.youtube), "Spotify": bool(svc.spotify),
        "Extraction model": svc.extract_llm.name if svc.extract_llm else "heuristic only",
        "Scoring model": svc.score_llm.name if svc.score_llm else "heuristic only",
    }
    desktop = runtime.read_desktop(cfg) if cfg.desktop else None
    port = request.url.port or cfg.port
    addresses = [f"http://127.0.0.1:{port}"]
    if desktop and desktop.get("host") == "0.0.0.0":
        addresses += [f"http://{ip}:{port}" for ip in runtime.lan_addresses()]
    google = svc.google
    return page(request, "connections.html", user, session, groups=groups, sources=sources,
                google_connected=bool(google and google.connected()),
                google_has_client=bool(google and google.client_secrets.exists()),
                google_flow=(google.flow_state if google else {"status": "idle"}),
                ollama=ollama_admin.status(cfg.extract_llm_base_url, cfg.extract_llm_model) if cfg.extract_llm_provider == "ollama" else None,
                extract_model=cfg.extract_llm_model, desktop=desktop, addresses=addresses,
                update=updater.read_state(cfg), version=__version__, desktop_version=cfg.desktop_version,
                is_desktop=cfg.desktop)


@router.post("/connections/keys")
async def save_keys(request: Request, user: User = Depends(web_owner), svc=Depends(get_svc)):
    form = await request.form()
    current = envfile.read(svc.cfg.env_path)
    changes: dict[str, str | None] = {}
    for key, secret in envfile.EDITABLE.items():
        if key not in form:
            continue
        value = str(form.get(key, "")).strip()
        if secret and (value.startswith("•") or (not value and form.get(f"clear_{key}") != "on")):
            continue  # untouched secret (shown masked) - keep it
        if value != current.get(key, ""):
            changes[key] = value or None
    if not changes:
        return _back("/connections", "Nothing changed")
    envfile.update(svc.cfg.env_path, changes)
    return _back("/connections", _restart_or_message(svc, f"Saved {len(changes)} setting(s)"))


@router.post("/connections/google/client")
async def google_client(file: UploadFile = File(...), user: User = Depends(web_owner), svc=Depends(get_svc)):
    try:
        svc.google.save_client_secrets(await file.read())
    except GoogleAuthError as e:
        return _back("/connections", str(e))
    return _back("/connections", "Google OAuth client saved. Now click Connect Google.")


@router.post("/connections/google/connect")
def google_connect(user: User = Depends(web_owner), svc=Depends(get_svc)):
    if not svc.google.client_secrets.exists():
        return _back("/connections", "Upload the OAuth client file first.")
    svc.google.start_browser_flow()
    return _back("/connections", "Finish signing in to Google in your browser, then come back here.")


@router.post("/connections/google/disconnect")
def google_disconnect(user: User = Depends(web_owner), svc=Depends(get_svc)):
    svc.google.disconnect()
    svc._gmail = None
    svc._calendar = None
    return _back("/connections", "Google disconnected")


@router.post("/connections/ollama/pull")
def ollama_pull(user: User = Depends(web_owner), svc=Depends(get_svc)):
    started = ollama_admin.start_pull(svc.cfg.extract_llm_base_url, svc.cfg.extract_llm_model)
    return _back("/connections", f"Downloading {svc.cfg.extract_llm_model} - this can take a while" if started else "Already downloading")


@router.post("/connections/token")
def new_token(request: Request, user: User = Depends(web_owner), session: Session = Depends(get_db)):
    owner = session.get(User, user.id)
    token = rotate_token(session, owner)
    _, page = _templates()
    return page(request, "message.html", user, session, title="Your new token",
                body="Paste this into the Chrome extension options and the phone app's Settings. "
                     "The old token stops working now. It's shown only once.", code=token)


@router.post("/connections/desktop")
def desktop_prefs(phone_access: bool = Form(False), launch_at_login: bool = Form(False), user: User = Depends(web_owner),
                  svc=Depends(get_svc)):
    if not svc.cfg.desktop:
        return _back("/connections", "These options are for the Mac app")
    before = runtime.read_desktop(svc.cfg)
    host = "0.0.0.0" if phone_access else "127.0.0.1"
    runtime.write_desktop(svc.cfg, host=host, launch_at_login=launch_at_login)
    if before.get("host") != host:
        return _back("/connections", _restart_or_message(svc, "Network access changed"))
    return _back("/connections", "Saved")


# --- updates ------------------------------------------------------------------------

@router.post("/app/check-updates")
def check_updates(request: Request, user: User = Depends(web_owner), session: Session = Depends(get_db), svc=Depends(get_svc)):
    if not svc.cfg.desktop:
        state = updater.check(svc.http, svc.cfg)
        msg = (f"Version {state['latest']} is available: update with git pull + pip install."
               if state.get("latest") and updater.newer(state.get("latest"), __version__) else "You're up to date.")
        return _back("/connections", state.get("error") or msg)
    state = updater.maybe_update(svc.http, svc.cfg)
    if state.get("installed"):
        return _back("/connections", _restart_or_message(svc, f"Installed version {state['installed']}"))
    if state.get("error"):
        return _back("/connections", f"Update check failed: {state['error']}")
    return _back("/connections", f"You're up to date (version {__version__}).")


@router.post("/app/auto-update")
def auto_update(enabled: bool = Form(False), user: User = Depends(web_owner), session: Session = Depends(get_db)):
    update_settings(session, auto_update=enabled)
    return _back("/connections", "Automatic updates " + ("on" if enabled else "off"))


# --- export straight to Downloads (the app window can't download files) --------------

@router.post("/export/save")
def export_save(user: User = Depends(web_owner), session: Session = Depends(get_db)):
    from ..export import write_contacts_csv

    folder = Path.home() / "Downloads"
    folder.mkdir(exist_ok=True)
    path = folder / f"press-contacts-{date.today().isoformat()}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        n = write_contacts_csv(session, f)
    return _back("/contacts", f"Saved {n} contacts to {path}")


def _back(url: str, msg: str) -> RedirectResponse:
    from urllib.parse import quote

    return RedirectResponse(f"{url}?msg={quote(msg)}", status_code=303)


__all__ = ["router", "login_response", "load_settings"]
