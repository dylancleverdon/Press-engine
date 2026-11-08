"""Creator discovery on YouTube (spec §4.3): reviewers, reaction channels, mixes."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from ..appsettings import AppSettings
from ..contacts.creators import ingest_profile_text, upsert_creator
from ..contacts.ingest import add_coverage, add_identity
from ..discovery.extract import parse_date
from ..models import PersonKind, SearchRun
from ..util.text import contains_phrase, fold, name_key
from ..util.timeutil import utcnow
from ..util.urls import SocialRef
from .known import KnownArtists
from .lastfm import searchable_artists
from .press import searches_since

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/youtube/v3"
MIN_SUBSCRIBERS = 100
PLAN = [("{artist} review", "video"), ("{artist} reaction", "video"), ("{artist} album review", "video"),
        ("{artist} mix", "video"), ("{artist}", "playlist")]


def skip_reason(title: str, description: str, artist: str, settings: AppSettings) -> str | None:
    """Why a search hit doesn't count as this channel covering the artist, or None.

    YouTube search also returns loosely related videos, so the artist has to be named
    in the title or description; and concert recordings, type beats, lyric re-uploads
    and the like are skipped (their channels aren't people to pitch)."""
    if not (contains_phrase(title, artist) or contains_phrase(description, artist)):
        return "doesn't mention the artist"
    if settings.youtube_skip_videos:
        t = f" {fold(title)} "
        raw = (title or "").lower()
        for word in settings.youtube_skip_words:
            w = word.strip().lower()
            if not w:
                continue
            # Entries with punctuation, like "(live)" or "- live", match as typed;
            # plain words match whole words only ("live in" doesn't hit "olive inn").
            hit = (w in raw) if re.search(r"[^\w\s]", w) else (f" {fold(w)} " in t)
            if hit:
                return f'title has "{word.strip()}"'
    return None


class YouTubeError(RuntimeError):
    pass


class YouTubeClient:
    """YouTube Data API v3, authorised by an API key or by the Google sign-in (OAuth token)."""

    def __init__(self, api_key: str | None, http: httpx.Client, *, token: Callable[[], str] | None = None):
        if not api_key and token is None:
            raise ValueError("YouTubeClient needs an API key or a token provider")
        self.api_key = api_key
        self.token = token
        self.http = http

    @property
    def via(self) -> str:
        return "API key" if self.api_key else "Google sign-in"

    def _get(self, path: str, **params) -> dict:
        headers = {}
        if self.api_key:
            params["key"] = self.api_key
        else:
            try:
                headers["Authorization"] = f"Bearer {self.token()}"
            except Exception as e:  # GoogleAuthError, refresh failures
                raise YouTubeError(f"Google sign-in for YouTube failed: {e}") from e
        try:
            r = self.http.get(f"{API}/{path}", params=params, headers=headers)
        except httpx.HTTPError as e:
            raise YouTubeError(f"YouTube unreachable: {e}") from e
        if r.status_code != 200:
            raise YouTubeError(f"YouTube API {r.status_code}: {r.text[:200]}")
        return r.json()

    def search(self, q: str, *, kind: str = "video", max_results: int = 25, published_after: datetime | None = None) -> list[dict]:
        params = {"part": "snippet", "q": q, "type": kind, "maxResults": max_results}
        if published_after and kind == "video":
            params["publishedAfter"] = published_after.strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._get("search", **params).get("items", [])

    def channels(self, ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 50):
            out += self._get("channels", part="snippet,statistics", id=",".join(ids[i:i + 50]), maxResults=50).get("items", [])
        return out


def is_official(title: str, artist_names: set[str]) -> bool:
    t = fold(title)
    if t.endswith(" topic") or "vevo" in t or any(w in t.split() for w in ("records", "recordings", "official")):
        return True
    squashed = name_key(title).replace(" ", "")
    return any(squashed == a.replace(" ", "") for a in artist_names)


def _item_url(item: dict) -> tuple[str, str] | None:
    ident = item.get("id") or {}
    if ident.get("videoId"):
        return f"https://www.youtube.com/watch?v={ident['videoId']}", "video"
    if ident.get("playlistId"):
        return f"https://www.youtube.com/playlist?list={ident['playlistId']}", "playlist"
    return None


def run_youtube_discovery(session: Session, svc, settings: AppSettings, *, now: datetime | None = None, per_artist: int = 2) -> dict:
    now = now or utcnow()
    yt = svc.youtube_client()
    if yt is None:
        return {"skipped": "YouTube not connected: reconnect Google (allow YouTube) or add a YouTube API key"}
    budget = settings.youtube_daily_searches - searches_since(session, "youtube", now - timedelta(hours=24))
    if budget <= 0:
        return {"skipped": "daily YouTube search budget used up"}
    known = KnownArtists(session)
    artist_keys = set(known.by_key)
    stats = defaultdict(int)
    hits: dict[str, list[tuple[str, dict, str, str]]] = defaultdict(list)  # channel -> (artist, item, url, kind)

    for artist in searchable_artists(session, "last_youtube_search_at", limit=budget // per_artist + 1):
        done = searches_since(session, "youtube", datetime.min)  # rotate templates across runs
        for i in range(per_artist):
            if budget <= 0:
                break
            template, kind = PLAN[(done + i) % len(PLAN)]
            q = template.format(artist=f'"{artist.name}"')
            budget -= 1
            run = SearchRun(engine="youtube", provider="youtube", query=q, artist_id=artist.id, run_at=now)
            try:
                items = yt.search(q, kind=kind, published_after=now - timedelta(days=730))
            except YouTubeError as e:
                run.error = str(e)[:500]
                session.add(run)
                stats["errors"] += 1
                continue
            run.result_count = len(items)
            session.add(run)
            stats["searches"] += 1
            for item in items:
                snip = item.get("snippet") or {}
                ch = snip.get("channelId")
                link = _item_url(item)
                if not (ch and link) or is_official(snip.get("channelTitle", ""), artist_keys):
                    continue
                why = skip_reason(snip.get("title", ""), snip.get("description", ""), artist.name, settings)
                if why:
                    stats["skipped_videos"] += 1
                    continue
                hits[ch].append((artist.name, snip, *link))
        artist.last_youtube_search_at = now
        session.commit()

    if not hits:
        return dict(stats)
    try:
        channels = yt.channels(list(hits))
    except YouTubeError as e:
        return {**stats, "error": str(e)}
    for ch in channels:
        snip = ch.get("snippet") or {}
        st = ch.get("statistics") or {}
        subs = None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount") or 0)
        if subs is not None and (subs < settings.youtube_min_subscribers
                                 or (settings.youtube_max_subscribers and subs > settings.youtube_max_subscribers)):
            stats["too_small"] += 1
            continue
        if is_official(snip.get("title", ""), artist_keys):
            continue
        ref = SocialRef("youtube", ch["id"], ch["id"], f"https://www.youtube.com/channel/{ch['id']}")
        person, created = upsert_creator(session, ref, name=snip.get("title"), kind=PersonKind.CREATOR,
                                         source_url=ref.url, found_via="youtube", followers=subs,
                                         country=snip.get("country"), language=(snip.get("defaultLanguage") or "")[:2] or None,
                                         bio=snip.get("description"), artist=known.match(hits[ch["id"]][0][0]))
        if snip.get("customUrl", "").startswith("@"):
            person = add_identity(session, person, "youtube", snip["customUrl"].lower(), display=f"https://www.youtube.com/{snip['customUrl']}")
        person = ingest_profile_text(session, person, snip.get("description"), source_url=ref.url)
        for artist_name, item, url, kind in hits.get(ch["id"], []):
            names = {artist_name} | {a.name for a in known.mentioned_in(item.get("title"))}
            for name in names:
                add_coverage(session, person, url=url, artist_name=name, artist=known.match(name), title=item.get("title"),
                             kind=kind, published_at=parse_date(item.get("publishedAt")), summary=(item.get("description") or "")[:300] or None,
                             source="youtube")
        stats["new_people"] += int(created)
        stats["channels"] += 1
        session.commit()
    return dict(stats)
