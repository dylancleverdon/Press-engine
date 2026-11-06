"""Structured extraction from article pages (spec §4.5).

The small local model does the reading; everything it returns is checked
against the page before we keep it:
* author names must appear on the page,
* emails must be in the set found verbatim on the page (anything else is dropped),
* artists must be mentioned in the page text.
Without an LLM configured, a metadata/keyword heuristic is used instead.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from ..llm import LLMClient, LLMError, LLMUnavailable
from ..models import Page
from ..util.emails import normalize_email
from ..util.text import clean_name, contains_phrase, fold, looks_like_person_name, name_key, truncate
from .known import KnownArtists

log = logging.getLogger(__name__)

ContentType = Literal["review", "interview", "premiere", "feature", "news", "list", "other"]
CONTENT_TYPES = ["review", "interview", "premiere", "feature", "news", "list", "other"]

EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "relevant": {"type": "boolean"},
        "content_type": {"type": "string", "enum": CONTENT_TYPES},
        "authors": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": ["string", "null"]},
                    "email": {"type": ["string", "null"]},
                },
                "required": ["name", "role", "email"],
            },
        },
        "outlet_name": {"type": ["string", "null"]},
        "artists_covered": {"type": "array", "items": {"type": "string"}},
        "published_date": {"type": ["string", "null"]},
        "language": {"type": ["string", "null"]},
        "summary": {"type": ["string", "null"]},
    },
    "required": ["relevant", "content_type", "authors", "outlet_name", "artists_covered", "published_date", "language", "summary"],
}

SYSTEM = (
    "You extract facts about music journalism from a single web page. Reply with JSON only. "
    "Use only information explicitly present on the page and never guess: if something is missing, "
    "use null or an empty list. Copy names exactly as they are written on the page."
)

PROMPT = """URL: {url}
Site name: {site}
Page title: {title}
Author(s) in page metadata: {author}
Date in page metadata: {date}
Emails found on the page: {emails}
We found this page while searching for the artist: {artist}

PAGE TEXT:
<<<
{text}
>>>

Fill in:
- relevant: true only if the page is a review, interview, premiere or feature mainly about a music artist or release. False for shops, ticket/tour listings, lyrics, streaming pages, artist biographies, label press releases, or pages that only mention the artist in passing.
- content_type: one of review, interview, premiere, feature, news, list, other.
- authors: the people who WROTE this piece (not the musicians, not people quoted). email only if it is in the emails list above and clearly belongs to that author, else null.
- outlet_name: the publication's name.
- artists_covered: music artists the piece is about, spelled as on the page.
- published_date: YYYY-MM-DD if the page states it, else null.
- language: ISO 639-1 code of the page text.
- summary: one short sentence in English describing the piece."""


class ExtractedAuthor(BaseModel):
    name: str
    role: str | None = None
    email: str | None = None


class Extraction(BaseModel):
    relevant: bool
    content_type: ContentType = "other"
    authors: list[ExtractedAuthor] = Field(default_factory=list)
    outlet_name: str | None = None
    artists_covered: list[str] = Field(default_factory=list)
    published_date: str | None = None
    language: str | None = None
    summary: str | None = None
    method: str = "llm"

    @field_validator("content_type", mode="before")
    @classmethod
    def _ctype(cls, v):
        v = (v or "other").lower()
        return v if v in CONTENT_TYPES else "other"

    @property
    def published(self) -> date | None:
        return parse_date(self.published_date)


DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    m = DATE_RE.search(value)
    if not m:
        return None
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return d if 1990 <= d.year <= date.today().year + 1 else None


def prefilter(page: Page, artist_name: str | None, known: KnownArtists) -> bool:
    """Cheap check before spending an LLM call: enough text and an artist we care about."""
    if not page.ok or not page.text or len(page.text) < 300:
        return False
    hay = f"{page.title or ''} {page.text[:20000]}"
    if artist_name and contains_phrase(hay, artist_name):
        return True
    return bool(known.mentioned_in(page.title)) or len(known.mentioned_in(page.text[:20000], limit=3)) >= 2


def _haystack(page: Page) -> str:
    return " ".join(x for x in (page.title, page.author, page.byline, page.text) if x)


def _split_authors(value: str | None) -> list[str]:
    if not value:
        return []
    parts = re.split(r";|,|\s+(?:and|og|und|en|et|e|y|och|&)\s+", value)
    return [clean_name(p) for p in parts if looks_like_person_name(clean_name(p))]


def clean(raw: Extraction, page: Page, known: KnownArtists) -> Extraction:
    hay = _haystack(page)
    verbatim = {normalize_email(e) for e in (page.emails or [])}
    authors: dict[str, ExtractedAuthor] = {}
    for a in raw.authors:
        name = clean_name(a.name or "")
        if not looks_like_person_name(name) or not contains_phrase(hay, name):
            continue  # likely hallucinated or not a person
        email = normalize_email(a.email) if a.email else None
        if email and email not in verbatim:
            log.debug("dropping model-invented email %s for %s", email, page.url)
            email = None
        authors.setdefault(name_key(name), ExtractedAuthor(name=name, role=(a.role or None), email=email))
    if not authors:
        for name in _split_authors(page.author):
            authors.setdefault(name_key(name), ExtractedAuthor(name=name))

    artists: dict[str, str] = {}
    for name in raw.artists_covered:
        name = (name or "").strip()
        if name and contains_phrase(hay, name):
            artists.setdefault(name_key(name), name)

    published = parse_date(raw.published_date) or parse_date(page.published)
    lang = (raw.language or page.language or "").lower()[:2] or None
    return raw.model_copy(update={
        "authors": list(authors.values()),
        "artists_covered": list(artists.values()),
        "published_date": published.isoformat() if published else None,
        "language": lang,
        "outlet_name": (raw.outlet_name or page.site_name or None),
        "summary": truncate(raw.summary, 300) or None,
    })


REVIEW_WORDS = ("review", "anmeldelse", "rezension", "kritik", "recensie", "recension", "chronique", "critique",
                "recensione", "reseña", "resena", "critica", "arvostelu", "レビュー", "評")
INTERVIEW_WORDS = ("interview", "intervju", "entrevista", "intervista", "haastattelu", "インタビュー")
PREMIERE_WORDS = ("premiere", "première", "premiär", "premiär", "exclusive", "debut", "first listen", "eksklusiv")
FEATURE_WORDS = ("feature", "portrait", "porträt", "portræt", "in focus", "profile")


def _detect_type(title: str, url: str) -> str:
    t = f"{fold(title)} {url.lower()}"
    for words, kind in ((REVIEW_WORDS, "review"), (INTERVIEW_WORDS, "interview"),
                        (PREMIERE_WORDS, "premiere"), (FEATURE_WORDS, "feature")):
        if any(w in t for w in words):
            return kind
    return "other"


def heuristic_extract(page: Page, artist_name: str | None, known: KnownArtists) -> Extraction:
    title = page.title or ""
    ctype = _detect_type(title, page.final_url or page.url)
    mentioned = known.mentioned_in(f"{title} {(page.text or '')[:20000]}")
    names = [a.name for a in mentioned]
    if artist_name and contains_phrase(f"{title} {page.text or ''}", artist_name) and name_key(artist_name) not in {name_key(n) for n in names}:
        names.insert(0, artist_name)
    in_title = artist_name and contains_phrase(title, artist_name) or any(contains_phrase(title, n) for n in names)
    relevant = bool(names) and ctype != "other" and bool(in_title)
    raw = Extraction(
        relevant=relevant,
        content_type=ctype,
        authors=[ExtractedAuthor(name=n) for n in _split_authors(page.author)],
        outlet_name=page.site_name,
        artists_covered=names[:10],
        published_date=page.published,
        language=page.language,
        summary=None,
        method="heuristic",
    )
    return clean(raw, page, known)


def extract_page(page: Page, artist_name: str | None, known: KnownArtists, llm: LLMClient | None) -> Extraction | None:
    """Return a validated, cleaned Extraction, or None if the model output was unusable."""
    if llm is None:
        return heuristic_extract(page, artist_name, known)
    prompt = PROMPT.format(
        url=page.final_url or page.url,
        site=page.site_name or "unknown",
        title=page.title or "",
        author=page.author or "none",
        date=page.published or "none",
        emails=", ".join(page.emails or []) or "none",
        artist=artist_name or "unknown",
        text=(page.text or "")[:12000],
    )
    try:
        data = llm.json(SYSTEM, prompt, EXTRACTION_SCHEMA, max_tokens=800)
        raw = Extraction.model_validate({**data, "method": "llm"})
    except LLMUnavailable as e:
        # No model running (e.g. Ollama not installed yet): use the keyword reader
        # rather than losing the page.
        log.info("extraction model unavailable (%s); using the heuristic for %s", str(e)[:120], page.url)
        return heuristic_extract(page, artist_name, known)
    except (LLMError, ValidationError) as e:
        log.info("extraction discarded for %s: %s", page.url, str(e)[:200])
        return None
    return clean(raw, page, known)
