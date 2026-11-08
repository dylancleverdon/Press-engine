"""Fetch a URL once, parse it, and keep it as evidence (Page rows)."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

import trafilatura
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..http import Fetcher
from ..models import Page
from ..util.emails import find_emails
from ..util.text import truncate
from ..util.timeutil import utcnow
from ..util.urls import absolutize, host_of, is_link_in_bio, normalize_url, parse_social

log = logging.getLogger(__name__)

AUTHOR_PATH_RE = re.compile(
    r"/(?:author|authors|writer|writers|contributor|contributors|profile|profiles|people|person|staff|"
    r"skribent|skribenter|journalist|journalister|autor|autoren|auteur|auteurs|redacteur|redacteuren|"
    r"medewerker|medewerkers|byline|members?)/",
    re.I,
)
CONTACT_RE = re.compile(
    r"(contact|kontakt|impressum|imprint|about|om-os|om_os|/om/?$|over-ons|overons|contatti|contacto|contato|"
    r"a-propos|apropos|qui-sommes|masthead|redaktion|redaktionen|redactie|/team|/staff|submission|submit|"
    r"write-for-us|inquir|お問い合わせ|会社概要)",
    re.I,
)
MAX_TEXT = 60_000
MAX_LINKS = 150
EMBEDDED_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+")
STATIC_ASSET_RE = re.compile(r"\.(?:js|css|png|jpe?g|gif|webp|svg|ico|woff2?|ttf|json|mp4|m3u8)$", re.I)
LANG_RE = re.compile(r"^[a-z]{2,3}")


def _norm_lang(value: str | None) -> str | None:
    if not value:
        return None
    m = LANG_RE.match(value.strip().lower())
    return m.group(0) if m else None


def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    for n in names:
        tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return None


def _jsonld_authors(soup: BeautifulSoup) -> list[str]:
    names: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for key in ("author", "creator"):
                val = node.get(key)
                for item in val if isinstance(val, list) else [val]:
                    if isinstance(item, dict) and isinstance(item.get("name"), str):
                        names.append(item["name"])
                    elif isinstance(item, str) and not item.startswith("http"):
                        names.append(item)
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for script in soup.find_all("script", attrs={"type": "application/ld+json"})[:10]:
        try:
            walk(json.loads(script.string or ""))
        except (ValueError, TypeError):
            continue
    return list(dict.fromkeys(n.strip() for n in names if n and n.strip()))


def parse_html(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    try:
        doc = trafilatura.bare_extraction(html, url=url, with_metadata=True, include_comments=False, favor_recall=True)
        meta = doc.as_dict() if doc is not None and hasattr(doc, "as_dict") else (doc or {})
    except Exception as e:  # trafilatura can choke on odd markup
        log.debug("trafilatura failed on %s: %s", url, e)
        meta = {}

    text = meta.get("text") or soup.get_text(" ", strip=True)
    title = meta.get("title") or _meta(soup, "og:title") or (soup.title.string.strip() if soup.title and soup.title.string else None)
    lang = _norm_lang(soup.html.get("lang") if soup.html else None) or _norm_lang(meta.get("language")) or _norm_lang(_meta(soup, "og:locale"))

    authors = _jsonld_authors(soup)
    meta_author = meta.get("author") or _meta(soup, "author", "article:author", "parsely-author", "sailthru.author")
    if meta_author and not meta_author.startswith("http"):
        authors.insert(0, meta_author)
    byline_bits = []
    for el in soup.select('[rel~="author"], [itemprop="author"], .byline, .by-line, .author, .entry-author, .article-author, .post-author')[:20]:
        t = el.get_text(" ", strip=True)
        if t and len(t) < 200:
            byline_bits.append(t)

    links: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        href = absolutize(url, a["href"])
        if not href or href in links:
            continue
        text_a = a.get_text(" ", strip=True)[:120]
        rel = " ".join(a.get("rel") or [])
        if href.startswith("mailto:"):
            links[href] = {"href": href, "text": text_a, "kind": "mailto"}
            continue
        social = parse_social(href)
        path = href.split("?", 1)[0]
        if social:
            links[href] = {"href": href, "text": text_a, "kind": "social", "type": social.type, "value": social.value}
        elif "author" in rel or AUTHOR_PATH_RE.search(path):
            links[href] = {"href": href, "text": text_a, "kind": "author"}
        elif host_of(href) == host_of(url) and (CONTACT_RE.search(path) or CONTACT_RE.search(text_a)):
            links[href] = {"href": href, "text": text_a, "kind": "contact"}
        if len(links) >= MAX_LINKS:
            break

    if is_link_in_bio(url):
        # Linktree, Beacons and co. often build the page with JavaScript; the links
        # are still in the page's embedded JSON.
        for raw in EMBEDDED_URL_RE.findall(html.replace("\\/", "/").replace("\\u002F", "/")):
            href = raw.rstrip("\\.,;")
            if href in links or len(links) >= MAX_LINKS or host_of(href) == host_of(url):
                continue
            social = parse_social(href)
            if social:
                links[href] = {"href": href, "text": "", "kind": "social", "type": social.type, "value": social.value}
            elif not STATIC_ASSET_RE.search(href.split("?", 1)[0]):
                links[href] = {"href": href, "text": "", "kind": "other"}

    found = find_emails(html, text)
    return {
        "title": truncate(title, 500) or None,
        "site_name": meta.get("sitename") or _meta(soup, "og:site_name"),
        "author": "; ".join(dict.fromkeys(authors))[:500] or None,
        "published": meta.get("date") or _meta(soup, "article:published_time", "datePublished"),
        "language": lang,
        "text": text[:MAX_TEXT],
        "byline": " | ".join(dict.fromkeys(byline_bits))[:2000] or None,
        "emails": found.verbatim,
        "unverified_emails": found.unverified,
        "links": list(links.values()),
    }


def get_page(session: Session, url: str) -> Page | None:
    return session.scalar(select(Page).where(Page.url == normalize_url(url)))


def fetch_page(session: Session, fetcher: Fetcher, url: str, *, max_age_days: int | None = None, now: datetime | None = None) -> Page:
    """Return the cached Page for ``url``, fetching (or refreshing) it if needed."""
    now = now or utcnow()
    key = normalize_url(url)
    page = session.scalar(select(Page).where(Page.url == key))
    if page is not None:
        fresh = max_age_days is None or (now - page.fetched_at).days < max_age_days
        if fresh:
            return page
    result = fetcher.get(url)
    if page is None:
        page = Page(url=key)
        session.add(page)
    page.final_url = result.final_url
    page.status_code = result.status
    page.fetched_at = now
    page.error = result.error
    if result.ok and result.html:
        try:
            parsed = parse_html(result.html, result.final_url)
        except Exception as e:  # never let one bad page kill a run
            log.warning("could not parse %s: %s", url, e)
            page.error = f"parse error: {e}"
        else:
            for k, v in parsed.items():
                setattr(page, k, v)
            page.extracted_at = None
            page.extraction = None
            page.relevant = None
    session.flush()
    return page
