"""Manual capture from the Chrome extension and the phone share sheet (spec §4.3)."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Literal

import httpx
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .contacts.creators import add_link, ingest_profile_text, upsert_creator
from .contacts.ingest import add_coverage, add_identity
from .discovery.known import KnownArtists
from .models import IdentityType, Person, PersonKind, User
from .scoring import combined_score, taste_profile
from .util.emails import EMAIL_RE, normalize_email
from .util.urls import HANDLE_RE, TIKTOK_VIDEO_RE, SocialRef, is_short_link, parse_social

log = logging.getLogger(__name__)

PLATFORM_URL = {
    "tiktok": "https://www.tiktok.com/@{h}",
    "instagram": "https://www.instagram.com/{h}/",
    "youtube": "https://www.youtube.com/@{h}",
    "x": "https://x.com/{h}",
}


class CaptureIn(BaseModel):
    url: str = Field(min_length=4, max_length=2048)
    source: Literal["extension", "share", "manual"] = "extension"
    platform: str | None = None
    handle: str | None = None
    display_name: str | None = Field(default=None, max_length=255)
    bio: str | None = Field(default=None, max_length=5000)
    bio_link: str | None = None
    followers: str | int | None = None
    video_url: str | None = None
    caption: str | None = Field(default=None, max_length=5000)
    sound: str | None = Field(default=None, max_length=500)
    email: str | None = None
    note: str | None = Field(default=None, max_length=2000)


class CaptureOut(BaseModel):
    person_id: int
    created: bool
    name: str
    needs_review: bool
    message: str


_COUNT_RE = re.compile(r"([\d.,]+)\s*([KkMmBb]?)")


def parse_count(value: str | int | None) -> int | None:
    if value is None or isinstance(value, int):
        return value
    m = _COUNT_RE.search(value.replace(" ", " "))
    if not m:
        return None
    num = m.group(1).replace(",", "") if m.group(2) else re.sub(r"[.,]", "", m.group(1))
    try:
        n = float(num)
    except ValueError:
        return None
    return int(n * {"k": 1e3, "m": 1e6, "b": 1e9}.get(m.group(2).lower(), 1))


def resolve_redirect(http: httpx.Client, url: str) -> str:
    """Expand share short-links (vm.tiktok.com/...) to the full profile/video URL."""
    try:
        r = http.head(url, follow_redirects=True, timeout=10)
        return str(r.url)
    except httpx.HTTPError:
        return url


def _ref_from_payload(data: CaptureIn, url: str) -> SocialRef | None:
    ref = parse_social(url)
    if ref is None and data.video_url:
        ref = parse_social(data.video_url)
    if ref is None and data.platform and data.handle:
        h = data.handle.strip().lstrip("@")
        if HANDLE_RE.match(h) and data.platform in PLATFORM_URL:
            ref = parse_social(PLATFORM_URL[data.platform].format(h=h))
    return ref


def capture(session: Session, http: httpx.Client, user: User, data: CaptureIn, *, now: datetime) -> tuple[Person, CaptureOut]:
    url = data.url.strip()
    m = EMAIL_RE.search(url)
    if m and "://" not in url:  # someone shared plain text with an address in it
        data.email = data.email or m.group(1)
    if is_short_link(url):
        url = resolve_redirect(http, url)
    ref = _ref_from_payload(data, url)
    via = f"capture_{data.source}"
    if ref is not None:
        person, created = upsert_creator(session, ref, name=data.display_name, kind=PersonKind.CREATOR, source_url=url,
                                         found_via=via, found_by_user_id=user.id, followers=parse_count(data.followers),
                                         bio=data.bio)
        if created and data.display_name:
            person.name = data.display_name[:255]
    else:
        created = True
        person = Person(name=(data.display_name or "Unknown creator")[:255], kind=PersonKind.CREATOR, found_via=via,
                        found_by_user_id=user.id, needs_review=True, bio=data.bio,
                        review_reason=f"Captured without a profile handle - open the link and add one: {url}")
        session.add(person)
        session.flush()

    if data.bio_link:
        person = add_link(session, person, data.bio_link, source_url=url, merge=True)  # their own bio link
    if data.bio:
        person = ingest_profile_text(session, person, data.bio, source_url=url)
    if data.email and EMAIL_RE.fullmatch(data.email.strip()):
        person = add_identity(session, person, IdentityType.EMAIL, normalize_email(data.email),
                              source_url=f"entered by {user.name}", verified=True)

    known = KnownArtists(session)
    text = " ".join(x for x in (data.caption, data.sound, data.bio) if x)
    video = TIKTOK_VIDEO_RE.search(data.video_url or url)
    piece_url = (f"https://www.tiktok.com/@{video.group(1)}/video/{video.group(2)}" if video else (data.video_url or url))
    for artist in known.mentioned_in(text, limit=5):
        add_coverage(session, person, url=piece_url, artist_name=artist.name, artist=artist,
                     title=(data.caption or data.sound or "")[:300] or None, kind="tiktok" if "tiktok" in piece_url else "video",
                     source=via)
    if person.discovered_artist_id is None:
        first = next(iter(known.mentioned_in(text, limit=1)), None)
        if first is not None:
            person.discovered_artist_id = first.id
    if data.note:
        line = f"{now:%Y-%m-%d} {user.name}: {data.note.strip()}"
        person.notes = f"{person.notes}\n{line}" if person.notes else line
    if not created and person.found_by_user_id is None:
        person.found_by_user_id = user.id

    profile, score = taste_profile(person, now.date())
    person.taste_profile, person.heuristic_score = profile, score
    person.taste_score = combined_score(person)
    person.resolved_at = None
    session.flush()
    msg = "Saved new creator" if created else "Already in the CRM - updated"
    if person.needs_review:
        msg += " (needs a handle)"
    return person, CaptureOut(person_id=person.id, created=created, name=person.name, needs_review=person.needs_review, message=msg)
