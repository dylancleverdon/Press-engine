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
from .models import Artist, JobRun
from .util.timeutil import utcnow

log = logging.getLogger(__name__)


def job_expand(session: Session, svc, settings: AppSettings, now: datetime, *, force: bool = False) -> dict:
    from .discovery.lastfm import expand_seeds

    if svc.lastfm is None:
        return {"skipped": "no Last.fm API key (PRESS_LASTFM_API_KEY)"}
    week_ago = now - timedelta(days=7)
    seeds = list(session.scalars(select(Artist).where(Artist.source == "manual", Artist.active.is_(True))))
    if not force:
        seeds = [s for s in seeds if s.expanded_at is None or s.expanded_at < week_ago]
    if not seeds:
        return {"skipped": "seeds were expanded this week"}
    return expand_seeds(session, svc.lastfm, per_seed=settings.similar_per_seed, seeds=seeds, now=now)


def job_discover(session: Session, svc, settings: AppSettings, now: datetime, *, sources: tuple[str, ...] | None = None) -> dict:
    """Daily discovery: expand seeds -> press + creator engines -> resolve -> dedupe -> score."""
    from .contacts.dedupe import find_merge_candidates
    from .contacts.resolve import resolve_pending
    from .discovery.playlists import run_playlist_discovery
    from .discovery.press import run_press_discovery
    from .discovery.tiktok import run_tiktok_discovery
    from .discovery.youtube import run_youtube_discovery
    from .scoring import score_heuristic, score_with_llm

    sources = sources or ("press", "youtube", "tiktok", "playlists")
    stats: dict = {"expand": job_expand(session, svc, settings, now)}
    session.commit()
    engines: dict[str, Callable] = {
        "press": run_press_discovery, "youtube": run_youtube_discovery,
        "tiktok": run_tiktok_discovery, "playlists": run_playlist_discovery,
    }
    for name in sources:
        try:
            stats[name] = engines[name](session, svc, settings, now=now)
        except Exception as e:  # keep going with the other engines
            session.rollback()
            log.exception("%s discovery failed", name)
            stats[name] = {"error": str(e)[:300]}
        session.commit()
    score_heuristic(session, only_stale=True, now=now)
    session.commit()
    stats["resolve"] = resolve_pending(session, svc, now=now)
    stats["merge_candidates"] = find_merge_candidates(session)
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


JOBS: dict[str, Callable] = {
    "expand": job_expand,
    "discover": job_discover,
    "queue": job_queue,
    "send": job_send,
    "replies": job_replies,
}

NOTABLE = ("sent", "reply", "bounce", "opt_out", "errors", "error", "cancelled", "rescheduled_late")


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
