"""Press discovery engine (spec §4.2): artist -> web search -> fetch -> extract -> ingest."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..appsettings import AppSettings
from ..contacts.ingest import ingest_press_page
from ..models import Artist, SearchRun
from ..util.timeutil import utcnow
from ..util.urls import domain_matches, host_of
from .extract import extract_page, prefilter
from .known import KnownArtists
from .lastfm import searchable_artists
from .pages import fetch_page
from .search import SearchError

log = logging.getLogger(__name__)


def searches_since(session: Session, engine: str, since: datetime) -> int:
    return session.scalar(
        select(func.count(SearchRun.id)).where(SearchRun.engine == engine, SearchRun.run_at >= since)
    ) or 0


def query_plan(session: Session, artist: Artist, settings: AppSettings, per_artist: int) -> list[tuple[str, str]]:
    """Rotate through (language, template) pairs so repeated runs cover new ground."""
    pairs: list[tuple[str, str]] = []
    langs = ["en"] + [lang for lang in settings.press_languages if lang != "en"]
    longest = max((len(settings.press_queries.get(lang, [])) for lang in langs), default=0)
    for i in range(longest):  # interleave: en[0], da[0], de[0], ..., en[1], ...
        for lang in langs:
            templates = settings.press_queries.get(lang, [])
            if i < len(templates):
                pairs.append((lang, templates[i]))
    if not pairs:
        return []
    done = session.scalar(
        select(func.count(SearchRun.id)).where(SearchRun.engine == "press", SearchRun.artist_id == artist.id)
    ) or 0
    start = done % len(pairs)
    picked = (pairs[start:] + pairs[:start])[:per_artist]
    return [(lang, tpl.replace("{artist}", artist.name)) for lang, tpl in picked]


def run_press_discovery(
    session: Session,
    svc,
    settings: AppSettings,
    *,
    now: datetime | None = None,
    max_queries: int | None = None,
    per_artist: int = 4,
    artists: list[Artist] | None = None,
) -> dict:
    now = now or utcnow()
    stats: Counter = Counter()
    if svc.search is None:
        return {"skipped": "no web search API key configured"}
    used_today = searches_since(session, "press", now - timedelta(hours=24))
    budget = max_queries if max_queries is not None else max(0, settings.search_daily_budget - used_today)
    if budget <= 0:
        return {"skipped": "daily search budget used up"}
    known = KnownArtists(session)
    blocked = {d.lower() for d in settings.blocked_domains}
    artists = artists if artists is not None else searchable_artists(session, "last_press_search_at", limit=max(1, budget // per_artist + 1))

    target = settings.daily_new_contacts_target
    for artist in artists:
        if budget <= 0 or stats["pages_fetched"] >= settings.max_pages_per_run:
            break
        if target and stats["new_people"] >= target:
            stats["stopped_at_target"] = 1  # save the rest of the search budget for tomorrow
            break
        for lang, query in query_plan(session, artist, settings, per_artist):
            if budget <= 0 or (target and stats["new_people"] >= target):
                break
            budget -= 1
            run = SearchRun(engine="press", provider=svc.search.name, query=query, artist_id=artist.id, run_at=now)
            try:
                results = svc.search.search(query, count=10, freshness=settings.search_freshness, lang=lang)
            except SearchError as e:
                run.error = str(e)[:500]
                session.add(run)
                session.commit()
                stats["search_errors"] += 1
                log.warning("search failed: %s", e)
                continue
            run.result_count = len(results)
            session.add(run)
            session.commit()
            stats["searches"] += 1
            for result in results:
                if stats["pages_fetched"] >= settings.max_pages_per_run:
                    break
                if domain_matches(host_of(result.url), blocked):
                    stats["blocked"] += 1
                    continue
                process_url(session, svc, result.url, artist.name, known, stats, now=now)
        artist.last_press_search_at = now
        session.commit()
    return dict(stats)


def process_url(session: Session, svc, url: str, artist_name: str | None, known: KnownArtists, stats: Counter,
                *, now: datetime, source: str = "press_search", found_by_user_id: int | None = None) -> None:
    # Commit before every network / model call so SQLite's write lock is never
    # held while we wait (the drip sender runs in another thread).
    page = fetch_page(session, svc.fetcher, url, now=now)
    if page.extracted_at is not None:
        session.commit()
        stats["already_seen"] += 1
        return
    stats["pages_fetched"] += 1
    page.extracted_at = now
    if not page.ok:
        session.commit()
        stats["fetch_errors"] += 1
        return
    if not prefilter(page, artist_name, known):
        page.relevant = False
        session.commit()
        stats["filtered"] += 1
        return
    session.commit()
    extraction = extract_page(page, artist_name, known, svc.extract_llm)
    if extraction is None:
        stats["extraction_discarded"] += 1
        return
    page.extraction = extraction.model_dump(mode="json")
    page.relevant = extraction.relevant
    if not extraction.relevant:
        session.commit()
        stats["not_relevant"] += 1
        return
    people, created = ingest_press_page(session, page, extraction, known, artist_hint=artist_name, source=source,
                                        found_by_user_id=found_by_user_id)
    session.commit()
    stats["relevant_pages"] += 1
    stats["new_people"] += created
