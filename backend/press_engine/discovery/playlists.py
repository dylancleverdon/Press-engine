"""Playlist curators (spec §4.3): public Spotify playlists containing similar artists.

Uses the client-credentials flow (no user login). Only user-made playlists are
considered, and only curators who left a way to reach them (email or profile
link) in the playlist description are stored.
"""

from __future__ import annotations

import html
import logging
import time
from collections import Counter
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from ..appsettings import AppSettings
from ..contacts.creators import URL_RE, ingest_profile_text, upsert_creator
from ..contacts.ingest import add_coverage
from ..models import PersonKind, SearchRun
from ..util.emails import find_emails
from ..util.text import name_key
from ..util.timeutil import utcnow
from ..util.urls import SocialRef
from .known import KnownArtists
from .lastfm import searchable_artists
from .press import searches_since

log = logging.getLogger(__name__)

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"


class SpotifyError(RuntimeError):
    pass


class SpotifyClient:
    def __init__(self, client_id: str, client_secret: str, http: httpx.Client):
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = http
        self._token: tuple[str, float] | None = None

    def _auth(self) -> str:
        if self._token and self._token[1] > time.time() + 30:
            return self._token[0]
        try:
            r = self.http.post(TOKEN_URL, data={"grant_type": "client_credentials"}, auth=(self.client_id, self.client_secret))
        except httpx.HTTPError as e:
            raise SpotifyError(f"Spotify unreachable: {e}") from e
        if r.status_code != 200:
            raise SpotifyError(f"Spotify auth failed {r.status_code}: {r.text[:200]}")
        data = r.json()
        self._token = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
        return self._token[0]

    def _get(self, path: str, **params) -> dict:
        try:
            r = self.http.get(f"{API}{path}", params=params, headers={"Authorization": f"Bearer {self._auth()}"})
        except httpx.HTTPError as e:
            raise SpotifyError(f"Spotify unreachable: {e}") from e
        if r.status_code != 200:
            raise SpotifyError(f"Spotify API {r.status_code}: {r.text[:200]}")
        return r.json()

    def search_playlists(self, q: str, limit: int = 20) -> list[dict]:
        items = (self._get("/search", q=q, type="playlist", limit=limit).get("playlists") or {}).get("items") or []
        return [i for i in items if i]  # Spotify sometimes returns null entries

    def playlist_artists(self, playlist_id: str) -> set[str]:
        data = self._get(f"/playlists/{playlist_id}/tracks", limit=100, fields="items(track(artists(name)))")
        names = set()
        for item in data.get("items") or []:
            for a in ((item or {}).get("track") or {}).get("artists") or []:
                if a.get("name"):
                    names.add(a["name"])
        return names


def run_playlist_discovery(session: Session, svc, settings: AppSettings, *, now: datetime | None = None) -> dict:
    now = now or utcnow()
    if svc.spotify is None:
        return {"skipped": "no Spotify credentials (PRESS_SPOTIFY_CLIENT_ID/SECRET)"}
    budget = settings.playlist_daily_searches - searches_since(session, "spotify", now - timedelta(hours=24))
    if budget <= 0:
        return {"skipped": "daily playlist search budget used up"}
    known = KnownArtists(session)
    stats: Counter = Counter()
    for artist in searchable_artists(session, "last_playlist_search_at", limit=budget):
        q = f'"{artist.name}"'
        run = SearchRun(engine="spotify", provider="spotify", query=q, artist_id=artist.id, run_at=now)
        try:
            playlists = svc.spotify.search_playlists(q)
        except SpotifyError as e:
            run.error = str(e)[:500]
            session.add(run)
            session.commit()
            stats["errors"] += 1
            continue
        run.result_count = len(playlists)
        session.add(run)
        stats["searches"] += 1
        for pl in playlists:
            owner = pl.get("owner") or {}
            if not owner.get("id") or owner["id"] == "spotify":
                continue
            desc = html.unescape(pl.get("description") or "")
            if not find_emails(None, desc).verbatim and not find_emails(None, desc).unverified and not URL_RE.search(desc):
                stats["no_contact"] += 1
                continue
            try:
                in_playlist = svc.spotify.playlist_artists(pl["id"])
            except SpotifyError:
                stats["errors"] += 1
                continue
            matched = [known.match(n) for n in in_playlist]
            matched = [a for a in matched if a is not None]
            if not any(name_key(a.name) == artist.name_key for a in matched):
                stats["artist_not_in_playlist"] += 1
                continue
            url = (pl.get("external_urls") or {}).get("spotify") or f"https://open.spotify.com/playlist/{pl['id']}"
            ref = SocialRef("spotify", owner["id"], owner.get("display_name") or owner["id"], f"https://open.spotify.com/user/{owner['id']}")
            person, created = upsert_creator(session, ref, name=owner.get("display_name") or pl.get("name"),
                                             kind=PersonKind.CURATOR, source_url=url, found_via="spotify_playlist",
                                             artist=artist)
            person = ingest_profile_text(session, person, desc, source_url=url)
            for a in {a.id: a for a in matched}.values():
                add_coverage(session, person, url=url, artist_name=a.name, artist=a, title=pl.get("name"), kind="playlist",
                             summary=desc[:300] or None, source="spotify")
            stats["new_people"] += int(created)
            session.commit()
        artist.last_playlist_search_at = now
        session.commit()
    return dict(stats)
