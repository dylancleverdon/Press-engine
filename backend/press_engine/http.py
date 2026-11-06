"""HTTP client factory and a polite page fetcher (robots.txt + per-host delay)."""

from __future__ import annotations

import logging
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .config import Config

log = logging.getLogger(__name__)


def make_http_client(cfg: Config, *, timeout: float | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=timeout or cfg.http_timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": cfg.user_agent, "Accept-Language": "en,*;q=0.5"},
    )


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None
    content_type: str
    html: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and self.status < 400


class Fetcher:
    def __init__(
        self,
        http: httpx.Client,
        *,
        user_agent: str = "PressEngine",
        respect_robots: bool = True,
        per_host_delay: float = 2.0,
        max_bytes: int = 3_000_000,
    ):
        self.http = http
        self.user_agent = user_agent
        self.respect_robots = respect_robots
        self.per_host_delay = per_host_delay
        self.max_bytes = max_bytes
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()

    def _robots_for(self, scheme: str, host: str) -> urllib.robotparser.RobotFileParser | None:
        key = f"{scheme}://{host}"
        if key in self._robots:
            return self._robots[key]
        rp: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
        try:
            r = self.http.get(f"{key}/robots.txt", timeout=10)
            if r.status_code in (401, 403):
                rp.disallow_all = True
            elif r.status_code >= 400:
                rp = None  # no robots.txt: everything allowed
            else:
                rp.parse(r.text.splitlines())
        except httpx.HTTPError:
            rp = None
        self._robots[key] = rp
        return rp

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        rp = self._robots_for(parts.scheme or "https", parts.netloc)
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    def _wait_turn(self, host: str) -> None:
        if self.per_host_delay <= 0:
            return
        with self._lock:
            last = self._last_hit.get(host, 0.0)
            wait = last + self.per_host_delay - time.monotonic()
            self._last_hit[host] = max(time.monotonic(), last + self.per_host_delay)
        if wait > 0:
            time.sleep(wait)

    def get(self, url: str) -> FetchResult:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return FetchResult(url, url, None, "", "", error="unsupported scheme")
        if not self.allowed(url):
            return FetchResult(url, url, None, "", "", error="blocked by robots.txt")
        self._wait_turn(parts.netloc)
        try:
            with self.http.stream("GET", url) as r:
                ctype = r.headers.get("content-type", "")
                if "html" not in ctype and "xml" not in ctype and "text/plain" not in ctype:
                    return FetchResult(url, str(r.url), r.status_code, ctype, "", error=f"not html ({ctype or 'unknown'})")
                chunks, size = [], 0
                for chunk in r.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > self.max_bytes:
                        break
                body = b"".join(chunks)
                encoding = r.charset_encoding or "utf-8"
                try:
                    html = body.decode(encoding, errors="replace")
                except LookupError:
                    html = body.decode("utf-8", errors="replace")
                err = None if r.status_code < 400 else f"HTTP {r.status_code}"
                return FetchResult(url, str(r.url), r.status_code, ctype, html, error=err)
        except httpx.HTTPError as e:
            log.info("fetch failed %s: %s", url, e)
            return FetchResult(url, url, None, "", "", error=f"{type(e).__name__}: {e}")
