"""Seed expansion via Last.fm artist.getSimilar (spec §4.1)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Artist, ArtistSimilarity
from ..util.text import name_key
from ..util.timeutil import utcnow

log = logging.getLogger(__name__)


class LastFMError(RuntimeError):
    pass


@dataclass
class SimilarArtist:
    name: str
    match: float
    mbid: str | None
    url: str | None


class LastFM:
    BASE = "https://ws.audioscrobbler.com/2.0/"

    def __init__(self, api_key: str, http: httpx.Client):
        self.api_key = api_key
        self.http = http

    def _call(self, method: str, **params) -> dict:
        q = {"method": method, "api_key": self.api_key, "format": "json", "autocorrect": 1, **params}
        try:
            r = self.http.get(self.BASE, params=q)
        except httpx.HTTPError as e:
            raise LastFMError(f"Last.fm unreachable: {e}") from e
        try:
            data = r.json()
        except ValueError as e:
            raise LastFMError(f"Last.fm returned non-JSON (HTTP {r.status_code})") from e
        if "error" in data:
            raise LastFMError(f"Last.fm error {data['error']}: {data.get('message')}")
        return data

    def similar(self, artist: str, limit: int = 25) -> list[SimilarArtist]:
        data = self._call("artist.getsimilar", artist=artist, limit=limit)
        out = []
        for a in (data.get("similarartists") or {}).get("artist", []):
            try:
                match = float(a.get("match") or 0)
            except ValueError:
                match = 0.0
            if a.get("name"):
                out.append(SimilarArtist(a["name"], match, a.get("mbid") or None, a.get("url")))
        return out

    def top_tags(self, artist: str, limit: int = 8) -> list[str]:
        try:
            data = self._call("artist.gettoptags", artist=artist)
        except LastFMError:
            return []
        tags = (data.get("toptags") or {}).get("tag", [])
        return [t["name"].lower() for t in tags[:limit] if t.get("name")]


def get_or_create_artist(session: Session, name: str, *, source: str = "manual") -> tuple[Artist, bool]:
    key = name_key(name)
    artist = session.scalar(select(Artist).where(Artist.name_key == key))
    if artist:
        if source == "manual" and artist.source != "manual":
            artist.source = "manual"  # promoting a similar artist to a seed
            artist.similarity = 1.0
            artist.active = True
        return artist, False
    artist = Artist(name=name.strip(), name_key=key, source=source, similarity=1.0 if source == "manual" else 0.0)
    session.add(artist)
    session.flush()
    return artist, True


def add_seed(session: Session, name: str) -> Artist:
    artist, _ = get_or_create_artist(session, name, source="manual")
    artist.active = True
    return artist


def remove_seed(session: Session, artist: Artist) -> None:
    """Deactivate a seed; its similar artists drop out unless another seed also points at them."""
    artist.active = False
    if artist.source == "manual":
        artist.similarity = 0.0
    recompute_similarities(session)


def expand_seeds(session: Session, lastfm: LastFM, *, per_seed: int = 25, seeds: list[Artist] | None = None,
                 now: datetime | None = None) -> dict:
    now = now or utcnow()
    seeds = seeds if seeds is not None else list(
        session.scalars(select(Artist).where(Artist.source == "manual", Artist.active.is_(True)))
    )
    stats = {"seeds": 0, "similar": 0, "new_artists": 0, "errors": []}
    for seed in seeds:
        try:
            similar = lastfm.similar(seed.name, limit=per_seed)
        except LastFMError as e:
            stats["errors"].append(f"{seed.name}: {e}")
            continue
        if not seed.tags:
            seed.tags = lastfm.top_tags(seed.name)
        stats["seeds"] += 1
        for s in similar:
            artist, created = get_or_create_artist(session, s.name, source="expanded")
            if artist.id == seed.id:
                continue
            stats["new_artists"] += int(created)
            if s.mbid and not artist.mbid:
                artist.mbid = s.mbid
            if s.url and not artist.lastfm_url:
                artist.lastfm_url = s.url
            link = session.get(ArtistSimilarity, (seed.id, artist.id))
            if link:
                link.match = s.match
            else:
                session.add(ArtistSimilarity(seed_id=seed.id, artist_id=artist.id, match=s.match))
            stats["similar"] += 1
        seed.expanded_at = now
    session.flush()
    recompute_similarities(session)
    return stats


def recompute_similarities(session: Session) -> None:
    """similarity(expanded artist) = best Last.fm match against any *active* seed (0 if none)."""
    active_seeds = {a.id for a in session.scalars(select(Artist).where(Artist.source == "manual", Artist.active.is_(True)))}
    best: dict[int, float] = {}
    for link in session.scalars(select(ArtistSimilarity)):
        if link.seed_id in active_seeds:
            best[link.artist_id] = max(best.get(link.artist_id, 0.0), link.match)
    for artist in session.scalars(select(Artist).where(Artist.source == "expanded")):
        artist.similarity = best.get(artist.id, 0.0)
    session.flush()


def searchable_artists(session: Session, order_field: str, limit: int) -> list[Artist]:
    """Active seeds first, then similar artists by similarity; least recently searched first."""
    col = getattr(Artist, order_field)
    rows = session.scalars(
        select(Artist)
        .where(Artist.active.is_(True), (Artist.source == "manual") | (Artist.similarity > 0))
        .order_by(col.is_not(None), col, Artist.similarity.desc())
        .limit(limit)
    )
    return list(rows)
