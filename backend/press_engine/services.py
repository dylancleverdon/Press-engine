"""Wiring of external clients. Tests build a Services with fakes instead."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Config
from .http import Fetcher, make_http_client
from .llm import LLMClient, LLMError, build_client

log = logging.getLogger(__name__)


@dataclass
class Services:
    cfg: Config
    http: httpx.Client
    fetcher: Fetcher
    search: Any = None
    lastfm: Any = None
    youtube: Any = None
    spotify: Any = None
    extract_llm: LLMClient | None = None
    score_llm: LLMClient | None = None
    google: Any = None  # GoogleAuth
    rng: random.Random = field(default_factory=random.Random)
    _gmail: Any = None
    _calendar: Any = None

    @property
    def draft_llm(self) -> LLMClient | None:
        return self.score_llm or self.extract_llm

    def gmail(self):
        """Gmail client, or None when Google isn't connected yet."""
        if self._gmail is None and self.google is not None and self.google.connected():
            from .outreach.gmail import GmailClient

            self._gmail = GmailClient(self.google.access_token, self.http)
        return self._gmail

    def sheets(self):
        """Google Sheets client, or None if Google isn't connected with the Sheets scope."""
        from .outreach.google_auth import SHEETS_SCOPE

        if self.google is None or not self.google.connected() or not self.google.has_scope(SHEETS_SCOPE):
            return None
        from .sheets import SheetsClient

        return SheetsClient(self.google.access_token, self.http)

    def drive(self):
        """Google Drive (only files this app creates), or None without that permission."""
        from .outreach.google_auth import DRIVE_SCOPE

        if self.google is None or not self.google.connected() or not self.google.has_scope(DRIVE_SCOPE):
            return None
        from .sheets import DriveClient

        return DriveClient(self.google.access_token, self.http)

    def youtube_client(self):
        """YouTube via the API key if one is set, else via the Google sign-in (youtube.readonly)."""
        if self.youtube is not None:
            return self.youtube
        from .outreach.google_auth import YOUTUBE_SCOPE

        if self.google is None or not self.google.connected() or not self.google.has_scope(YOUTUBE_SCOPE):
            return None
        from .discovery.youtube import YouTubeClient

        return YouTubeClient(None, self.http, token=self.google.access_token)

    def calendar(self):
        if self._calendar is None and self.google is not None and self.google.connected():
            from .calendar import CalendarClient

            self._calendar = CalendarClient(self.google.access_token, self.http)
        return self._calendar


def build_services(cfg: Config) -> Services:
    from .discovery.lastfm import LastFM
    from .discovery.playlists import SpotifyClient
    from .discovery.search import build_search
    from .discovery.youtube import YouTubeClient
    from .outreach.google_auth import GoogleAuth

    http = make_http_client(cfg)
    fetcher = Fetcher(http, user_agent=cfg.user_agent, respect_robots=cfg.respect_robots,
                      per_host_delay=cfg.per_host_delay_seconds, max_bytes=cfg.max_page_bytes)

    def llm(role: str) -> LLMClient | None:
        try:
            return build_client(cfg, role, http=httpx.Client(timeout=300))
        except LLMError as e:
            log.warning("%s model disabled: %s", role, e)
            return None

    return Services(
        cfg=cfg,
        http=http,
        fetcher=fetcher,
        search=build_search(cfg.search_provider, brave_key=cfg.brave_api_key, serpapi_key=cfg.serpapi_api_key, http=http)
        if cfg.search_provider != "none" else None,
        lastfm=LastFM(cfg.lastfm_api_key, http) if cfg.lastfm_api_key else None,
        youtube=YouTubeClient(cfg.youtube_api_key, http) if cfg.youtube_api_key else None,
        spotify=SpotifyClient(cfg.spotify_client_id, cfg.spotify_client_secret, http)
        if cfg.spotify_client_id and cfg.spotify_client_secret else None,
        extract_llm=llm("extract"),
        score_llm=llm("score"),
        google=GoogleAuth(cfg.client_secrets_path, cfg.token_path),
    )
