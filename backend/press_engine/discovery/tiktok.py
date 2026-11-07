"""TikTok creators via ordinary web search (spec §4.3).

We never fetch tiktok.com ourselves (scraping it is against their terms). Search
results for ``site:tiktok.com "<artist>"`` already carry creator handles in
their URLs; sound pages are saved as links to browse by hand with the Chrome
extension.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..appsettings import AppSettings
from ..contacts.creators import upsert_creator
from ..contacts.ingest import add_coverage
from ..models import DiscoveryLink, PersonKind, SearchRun
from ..util.text import fold, name_key
from ..util.timeutil import utcnow
from ..util.urls import TIKTOK_MUSIC_RE, TIKTOK_VIDEO_RE, SocialRef, normalize_url
from .known import KnownArtists
from .lastfm import searchable_artists
from .press import searches_since
from .search import SearchError

log = logging.getLogger(__name__)


def _is_artist_account(handle: str, artist_keys: set[str]) -> bool:
    h = fold(handle).replace(" ", "")
    return any(h == k.replace(" ", "") or h == k.replace(" ", "") + "official" for k in artist_keys)


def run_tiktok_discovery(session: Session, svc, settings: AppSettings, *, now: datetime | None = None) -> dict:
    now = now or utcnow()
    if svc.search is None:
        return {"skipped": "no web search API key configured"}
    budget = settings.tiktok_daily_searches - searches_since(session, "tiktok", now - timedelta(hours=24))
    if budget <= 0:
        return {"skipped": "daily TikTok search budget used up"}
    known = KnownArtists(session)
    keys = set(known.by_key)
    stats: Counter = Counter()
    for artist in searchable_artists(session, "last_tiktok_search_at", limit=budget):
        query = f'site:tiktok.com "{artist.name}"'
        run = SearchRun(engine="tiktok", provider=svc.search.name, query=query, artist_id=artist.id, run_at=now)
        try:
            results = svc.search.search(query, count=20)
        except SearchError as e:
            run.error = str(e)[:500]
            session.add(run)
            session.commit()
            stats["errors"] += 1
            continue
        run.result_count = len(results)
        session.add(run)
        stats["searches"] += 1
        for r in results:
            music = TIKTOK_MUSIC_RE.search(r.url)
            if music:
                url = normalize_url(r.url)
                if session.scalar(select(DiscoveryLink.id).where(DiscoveryLink.url == url)) is None:
                    session.add(DiscoveryLink(url=url, kind="tiktok_sound", title=r.title[:500], artist_name=artist.name))
                    stats["sound_pages"] += 1
                continue
            video = TIKTOK_VIDEO_RE.search(r.url)
            if not video:
                continue
            handle = video.group(1)
            if _is_artist_account(handle, keys):
                continue
            ref = SocialRef("tiktok", handle.lower(), "@" + handle, f"https://www.tiktok.com/@{handle.lower()}")
            person, created = upsert_creator(session, ref, name=None, kind=PersonKind.CREATOR, source_url=r.url,
                                             found_via="tiktok_search", artist=artist)
            text = f"{r.title} {r.snippet}"
            names = {artist.name} | {a.name for a in known.mentioned_in(text)}
            for name in names:
                if name_key(name) and (name == artist.name or known.match(name)):
                    add_coverage(session, person, url=f"https://www.tiktok.com/@{handle}/video/{video.group(2)}",
                                 artist_name=name, artist=known.match(name), title=r.title[:500] or None,
                                 kind="tiktok", summary=r.snippet[:300] or None, source="tiktok_search")
            stats["new_people"] += int(created)
        artist.last_tiktok_search_at = now
        session.commit()
    return dict(stats)
