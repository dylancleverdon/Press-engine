"""Command line: setup, one-off pipeline steps, and running the server."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import typer
from sqlalchemy import func, select

from .appsettings import load_settings, update_settings
from .config import get_config
from .db import get_database

app = typer.Typer(help="Press Engine: find, pitch and keep track of music press and creators.", no_args_is_help=True)


def _version(value: bool) -> None:
    if value:
        from . import __version__

        typer.echo(__version__)
        raise typer.Exit()
users_app = typer.Typer(help="Manage D and bandmates.", no_args_is_help=True)
seeds_app = typer.Typer(help="Seed artists.", no_args_is_help=True)
app.add_typer(users_app, name="user")
app.add_typer(seeds_app, name="seed")


def _setup_logging() -> None:
    logging.basicConfig(level=get_config().log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _services():
    from .services import build_services

    return build_services(get_config())


def _print(stats) -> None:
    typer.echo(json.dumps(stats, indent=2, default=str))


@app.command()
def init(name: str = typer.Option("D", help="Your name"), email: str = typer.Option(None, help="Your email"),
         band: str = typer.Option(None, help="Band name"), timezone: str = typer.Option(None, help="IANA time zone, e.g. Europe/Copenhagen"),
         seed: list[str] = typer.Option(["Athletic Progression"], help="Seed artist(s)")):
    """Create the database, the owner account and the first seed artist."""
    from .api.deps import create_user
    from .discovery.lastfm import add_seed
    from .models import User

    db = get_database()
    with db.session() as s:
        if s.scalar(select(func.count(User.id)).where(User.role == "owner")):
            typer.echo("Already initialised. Use `press-engine user token <name>` to get a new token.")
            raise typer.Exit(1)
        user, token = create_user(s, name, role="owner", email=email)
        changes = {}
        if band:
            changes["band_name"] = band
        if timezone:
            changes["timezone"] = timezone
        changes["sender_name"] = name
        changes["signature"] = f"{name}\n{band}" if band else name
        update_settings(s, **changes)
        for artist in seed:
            add_seed(s, artist)
    typer.echo(f"Database: {db.url}")
    typer.echo(f"Owner account created for {name}. Your access token (shown once, keep it safe):\n\n    {token}\n")
    typer.echo("Next: copy backend/.env.example to backend/.env and add API keys, run `press-engine google-auth`, "
               "then `press-engine serve` and open http://localhost:8000")


@users_app.command("add")
def user_add(name: str, owner: bool = typer.Option(False, help="Give full access"), email: str = typer.Option(None)):
    """Add a bandmate and print their token."""
    from .api.deps import create_user

    with get_database().session() as s:
        _, token = create_user(s, name, role="owner" if owner else "member", email=email)
    typer.echo(f"Token for {name} (shown once):\n\n    {token}\n")


@users_app.command("list")
def user_list():
    from .models import User

    with get_database().session() as s:
        for u in s.scalars(select(User).order_by(User.id)):
            typer.echo(f"{u.id:>3}  {u.name:<20} {u.role}")


@users_app.command("token")
def user_token(name: str):
    """Issue a new token for a user (the old one stops working)."""
    from .api.deps import rotate_token
    from .models import User

    with get_database().session() as s:
        user = s.scalar(select(User).where(User.name == name))
        if user is None:
            typer.echo(f"No user named {name}")
            raise typer.Exit(1)
        token = rotate_token(s, user)
    typer.echo(f"New token for {name}:\n\n    {token}\n")


@seeds_app.command("add")
def seed_add(names: list[str]):
    from .discovery.lastfm import add_seed

    with get_database().session() as s:
        for n in names:
            typer.echo(f"Seed: {add_seed(s, n).name}")


@seeds_app.command("list")
def seed_list():
    from .models import Artist

    with get_database().session() as s:
        for a in s.scalars(select(Artist).order_by(Artist.source.desc(), Artist.similarity.desc())):
            flag = "seed" if a.is_seed else f"{a.similarity:.2f}"
            typer.echo(f"{flag:>6}  {'' if a.active else '(excluded) '}{a.name}")


@seeds_app.command("remove")
def seed_remove(name: str):
    from .discovery.lastfm import remove_seed
    from .models import Artist
    from .util.text import name_key

    with get_database().session() as s:
        a = s.scalar(select(Artist).where(Artist.name_key == name_key(name)))
        if a is None:
            typer.echo(f"No artist named {name}")
            raise typer.Exit(1)
        remove_seed(s, a)
    typer.echo(f"Removed seed {name}")


def _run(job: str, **kw):
    from .jobs import run_job

    _setup_logging()
    _print(run_job(get_database(), _services(), job, **kw))


@app.command()
def expand(force: bool = typer.Option(False, help="Re-expand seeds expanded this week")):
    """Expand seed artists into similar artists (Last.fm)."""
    _run("expand", force=force)


@app.command()
def discover(source: list[str] = typer.Option(None, help="press, youtube, tiktok, playlists (default: all)")):
    """Run the daily discovery pipeline now."""
    _run("discover", sources=tuple(source) if source else None)


@app.command()
def queue():
    """Build today's email + DM queue."""
    _run("queue")


@app.command("send-due")
def send_due():
    """Send emails that are due (normally done by the scheduler every minute)."""
    _run("send")


@app.command()
def replies(new_threads: bool = typer.Option(True, help="Also look for replies sent in new threads")):
    """Check Gmail for replies, bounces and opt-outs."""
    _run("replies", include_new_threads=new_threads)


@app.command()
def resolve(limit: int = 40):
    """Look for email addresses for the best contacts that don't have one."""
    from .contacts.resolve import resolve_pending

    _setup_logging()
    svc = _services()
    with get_database().session() as s:
        _print(resolve_pending(s, svc, limit=limit))


@app.command()
def score(llm: bool = typer.Option(False, help="Also run the larger model on the top candidates")):
    """Recompute taste scores."""
    from .scoring import score_heuristic, score_with_llm

    _setup_logging()
    with get_database().session() as s:
        stats = {"heuristic": score_heuristic(s)}
        if llm:
            stats["llm"] = score_with_llm(s, _services().score_llm, load_settings(s))
    _print(stats)


@app.command()
def dedupe():
    """Flag probable duplicates for review."""
    from .contacts.dedupe import find_merge_candidates

    with get_database().session() as s:
        typer.echo(f"{find_merge_candidates(s)} new possible duplicates")


@app.command()
def export(path: Path = typer.Argument(Path("press-contacts.csv"))):
    """Export contacts to CSV for checking lead quality by hand."""
    from .export import write_contacts_csv

    with get_database().session() as s, path.open("w", newline="", encoding="utf-8") as f:
        n = write_contacts_csv(s, f)
    typer.echo(f"Wrote {n} contacts to {path}")


@app.command("google-auth")
def google_auth(port: int = typer.Option(0, help="Local port for the OAuth redirect (0 = any free port)")):
    """Connect D's Google account (Gmail send/read + Calendar)."""
    from .outreach.google_auth import GoogleAuth, GoogleAuthError

    cfg = get_config()
    try:
        GoogleAuth(cfg.client_secrets_path, cfg.token_path).run_flow(port=port)
    except GoogleAuthError as e:
        typer.echo(str(e))
        raise typer.Exit(1) from e
    typer.echo(f"Connected. Token saved to {cfg.token_path}")


@app.command()
def status():
    """Show what's configured and how many contacts there are."""
    from .models import Outreach, Person

    cfg = get_config()
    svc = _services()
    with get_database().session() as s:
        settings = load_settings(s)
        people = s.scalar(select(func.count(Person.id))) or 0
        sent = s.scalar(select(func.count(Outreach.id)).where(Outreach.sent_at.is_not(None))) or 0
    checks = {
        "database": cfg.resolved_database_url,
        "last.fm": bool(svc.lastfm), "web search": svc.search.name if svc.search else False,
        "youtube": bool(svc.youtube), "spotify": bool(svc.spotify),
        "extraction model": svc.extract_llm.name if svc.extract_llm else "heuristic only",
        "scoring model": svc.score_llm.name if svc.score_llm else "heuristic only",
        "google connected": bool(svc.google and svc.google.connected()),
        "timezone": settings.timezone, "contacts": people, "messages sent": sent,
    }
    for k, v in checks.items():
        typer.echo(f"{k:>18}: {v}")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, scheduler: bool = typer.Option(True, help="Run background jobs too")):
    """Run the dashboard + API (and the scheduler)."""
    import uvicorn

    from . import runtime
    from .api.app import create_app

    _setup_logging()
    server = uvicorn.Server(uvicorn.Config(create_app(start_scheduler=scheduler), host=host, port=port, log_level="info"))
    runtime.attach_server(server)
    server.run()
    if runtime.restart_requested():
        raise typer.Exit(runtime.RESTART_EXIT_CODE)  # the Mac app starts us again


@app.command()
def worker():
    """Run only the scheduler (no web server)."""
    import time

    from .scheduler import Scheduler

    _setup_logging()
    sched = Scheduler(get_database(), _services())
    sched.start()
    typer.echo("Scheduler running. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        sched.stop()


@app.callback()
def _root(version: bool = typer.Option(False, "--version", callback=_version, is_eager=True, help="Show the version")):
    """Press Engine: find, pitch and keep track of music press and creators."""


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
