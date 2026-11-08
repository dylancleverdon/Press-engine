"""Pipeline jobs and their run log."""

from __future__ import annotations

import logging
import time
import traceback
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .appsettings import AppSettings, load_settings
from .db import Database
from .models import JobRun
from .util.timeutil import utcnow

log = logging.getLogger(__name__)


def job_expand(session: Session, svc, settings: AppSettings, now: datetime, *, force: bool = False) -> dict:
    """Find soundalikes: Last.fm (seeds and a second hop), co-coverage, and fit tracking."""
    from .soundalikes import discover_soundalikes

    return discover_soundalikes(session, svc, settings, now=now, force=force)


def job_discover(session: Session, svc, settings: AppSettings, now: datetime, *, sources: tuple[str, ...] | None = None) -> dict:
    """Daily discovery: expand seeds -> press + creator engines -> resolve -> dedupe -> score."""
    from . import progress

    sources = sources or ("press", "youtube", "tiktok", "playlists")
    progress.start(list(sources))
    try:
        return _discover(session, svc, settings, now, sources)
    finally:
        progress.finish()


def _discover(session: Session, svc, settings: AppSettings, now: datetime, sources: tuple[str, ...]) -> dict:
    from . import progress
    from .contacts.dedupe import find_merge_candidates
    from .contacts.resolve import resolve_pending
    from .discovery.engines import ENGINES, apply_vetting, apply_youtube_rules, max_person_id
    from .discovery.playlists import run_playlist_discovery
    from .discovery.press import run_press_discovery
    from .discovery.tiktok import run_tiktok_discovery
    from .discovery.youtube import run_youtube_discovery
    from .scoring import score_heuristic, score_with_llm

    progress.step("Looking up soundalikes")
    stats: dict = {"soundalikes": job_expand(session, svc, settings, now)}
    session.commit()
    engines: dict[str, Callable] = {
        "press": run_press_discovery, "youtube": run_youtube_discovery,
        "tiktok": run_tiktok_discovery, "playlists": run_playlist_discovery,
    }
    for name in sources:
        progress.step(f"{ENGINES[name].label}: starting", engine=name)
        before = max_person_id(session)
        try:
            stats[name] = engines[name](session, svc, settings, now=now)
            pending = apply_vetting(session, settings, ENGINES[name], after_id=before)
            if pending and isinstance(stats[name], dict):
                stats[name]["waiting_for_review"] = pending
            if name == "youtube" and isinstance(stats[name], dict):
                stats[name]["rules"] = apply_youtube_rules(session, settings)
        except Exception as e:  # keep going with the other engines
            session.rollback()
            log.exception("%s discovery failed", name)
            stats[name] = {"error": str(e)[:300]}
        session.commit()
    progress.step("Scoring new finds", engine="")
    score_heuristic(session, only_stale=True, now=now)
    session.commit()
    progress.step("Reading link pages and websites for contact details")
    stats["resolve"] = resolve_pending(session, svc, now=now)
    progress.step("Merging duplicates", done=1, total=2)
    stats["merge_candidates"] = find_merge_candidates(session)
    progress.step("Scoring", done=1, total=2)
    stats["scored"] = score_heuristic(session, now=now)
    session.commit()
    stats["llm_scoring"] = score_with_llm(session, svc.score_llm, settings, now=now)
    return stats


def job_queue(session: Session, svc, settings: AppSettings, now: datetime) -> dict:
    from .outreach.queue import build_daily_queue

    return build_daily_queue(session, settings, now=now, rng=svc.rng)


def job_send(session: Session, svc, settings: AppSettings, now: datetime) -> dict:
    from .outreach.sender import send_due

    return send_due(session, svc, settings, now=now, rng=svc.rng)


def job_replies(session: Session, svc, settings: AppSettings, now: datetime, *, include_new_threads: bool = False) -> dict:
    from .outreach.replies import poll_replies

    return poll_replies(session, svc, settings, now=now, include_new_threads=include_new_threads)


def job_sheet_sync(session: Session, svc, settings: AppSettings, now: datetime) -> dict:
    from .suggestions import sync

    return sync(session, svc, settings, now=now)


def on_start(db: Database, svc) -> dict:
    """Once per version: settings changes that come with an update, the "Updated to X"
    banner, and re-applying the engines' rules to what's waiting to be vetted."""
    from . import __version__
    from .appsettings import load_settings, update_settings

    with db.session() as s:
        settings = load_settings(s)
        if settings.last_run_version == __version__:
            return {}
        changes: dict = {"last_run_version": __version__}
        if settings.last_run_version:  # an update, not a first start
            changes["announce_version"] = __version__
            if settings.youtube_daily_searches == 30:  # the old default; Google allows ~100 a day
                changes["youtube_daily_searches"] = 60
        update_settings(s, **changes)
    from .api import views

    views.run_in_background(db, svc, "rules")
    return changes


def job_rules(session: Session, svc, settings: AppSettings, now: datetime) -> dict:
    """Apply the engines' current rules to finds waiting to be vetted (no searching)."""
    from .discovery.engines import apply_youtube_rules

    return {"youtube": apply_youtube_rules(session, settings)}


JOBS: dict[str, Callable] = {
    "rules": job_rules,
    "sheet_sync": job_sheet_sync,
    "expand": job_expand,
    "discover": job_discover,
    "queue": job_queue,
    "send": job_send,
    "replies": job_replies,
}

NOTABLE = ("pushed", "pulled", "imported", "sent", "reply", "bounce", "opt_out", "errors", "error", "cancelled", "rescheduled_late")


def run_job(db: Database, svc, name: str, *, now: datetime | None = None, quiet: bool = False, **kwargs) -> dict:
    """Run a job in its own session and log it. ``quiet`` jobs (the every-minute
    ones) are only logged when something notable happened."""
    fn = JOBS[name]
    now = now or utcnow()
    t0 = time.monotonic()
    with db.session() as s:
        settings = load_settings(s)
        try:
            stats = fn(s, svc, settings, now, **kwargs) or {}
            ok, error = True, None
        except Exception as e:
            s.rollback()
            log.exception("job %s failed", name)
            stats, ok, error = {}, False, "".join(traceback.format_exception_only(type(e), e)).strip()
        if not quiet or not ok or any(stats.get(k) for k in NOTABLE):
            finished = now + timedelta(seconds=time.monotonic() - t0)
            s.add(JobRun(job=name, started_at=now, finished_at=finished, ok=ok, stats=_jsonable(stats), error=error))
    return {"ok": ok, "error": error, **stats}


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def last_run(session: Session, job: str) -> JobRun | None:
    return session.scalar(select(JobRun).where(JobRun.job == job).order_by(JobRun.started_at.desc()).limit(1))
