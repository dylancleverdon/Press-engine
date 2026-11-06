"""Web search providers (spec §4.2): Brave Search API or SerpAPI."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

log = logging.getLogger(__name__)


class SearchError(RuntimeError):
    pass


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str
    age: str | None = None


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, *, count: int = 10, freshness: str | None = None, lang: str | None = None) -> list[SearchResult]: ...


# Brave's search_lang codes differ from ISO 639-1 for a few languages.
BRAVE_LANG = {"ja": "jp", "no": "nb", "zh": "zh-hans", "pt": "pt-pt"}
BRAVE_FRESHNESS = {"month": "pm", "year": "py"}
SERP_FRESHNESS = {"month": "qdr:m", "year": "qdr:y"}


class BraveSearch:
    name = "brave"
    URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str, http: httpx.Client):
        self.api_key = api_key
        self.http = http

    def search(self, query: str, *, count: int = 10, freshness: str | None = None, lang: str | None = None) -> list[SearchResult]:
        params: dict = {"q": query, "count": min(count, 20), "safesearch": "moderate"}
        if freshness in BRAVE_FRESHNESS:
            params["freshness"] = BRAVE_FRESHNESS[freshness]
        if lang and lang != "en":
            params["search_lang"] = BRAVE_LANG.get(lang, lang)
        headers = {"X-Subscription-Token": self.api_key, "Accept": "application/json"}
        try:
            r = self.http.get(self.URL, params=params, headers=headers)
            if r.status_code == 422 and "search_lang" in params:  # unsupported language code
                params.pop("search_lang")
                r = self.http.get(self.URL, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise SearchError(f"Brave unreachable: {e}") from e
        if r.status_code != 200:
            raise SearchError(f"Brave error {r.status_code}: {r.text[:200]}")
        results = (r.json().get("web") or {}).get("results") or []
        return [
            SearchResult(x["url"], x.get("title") or "", x.get("description") or "", x.get("age"))
            for x in results if x.get("url")
        ]


class SerpApiSearch:
    name = "serpapi"
    URL = "https://serpapi.com/search.json"

    def __init__(self, api_key: str, http: httpx.Client):
        self.api_key = api_key
        self.http = http

    def search(self, query: str, *, count: int = 10, freshness: str | None = None, lang: str | None = None) -> list[SearchResult]:
        params: dict = {"engine": "google", "q": query, "num": min(count, 20), "api_key": self.api_key}
        if freshness in SERP_FRESHNESS:
            params["tbs"] = SERP_FRESHNESS[freshness]
        if lang and lang != "en":
            params["hl"] = lang
        try:
            r = self.http.get(self.URL, params=params)
        except httpx.HTTPError as e:
            raise SearchError(f"SerpAPI unreachable: {e}") from e
        if r.status_code != 200:
            raise SearchError(f"SerpAPI error {r.status_code}: {r.text[:200]}")
        data = r.json()
        if data.get("error"):
            if "hasn't returned any results" in data["error"]:
                return []
            raise SearchError(f"SerpAPI error: {data['error']}")
        return [
            SearchResult(x["link"], x.get("title") or "", x.get("snippet") or "", x.get("date"))
            for x in data.get("organic_results") or [] if x.get("link")
        ]


def build_search(provider: str, *, brave_key: str | None, serpapi_key: str | None, http: httpx.Client) -> SearchProvider | None:
    if provider == "brave" and brave_key:
        return BraveSearch(brave_key, http)
    if provider == "serpapi" and serpapi_key:
        return SerpApiSearch(serpapi_key, http)
    # Fall back to whichever key exists.
    if brave_key:
        return BraveSearch(brave_key, http)
    if serpapi_key:
        return SerpApiSearch(serpapi_key, http)
    return None
