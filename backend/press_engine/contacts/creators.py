"""Shared ingest for creators and curators (YouTube, TikTok, playlists, captures)."""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from ..models import IdentityType, Person, PersonKind
from ..util.emails import find_emails
from ..util.urls import SocialRef, parse_social, site_identity
from .ingest import add_identity, attach_email, find_identity

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


def upsert_creator(session: Session, ref: SocialRef, *, name: str | None, kind: str = PersonKind.CREATOR,
                   source_url: str | None = None, found_via: str | None = None, found_by_user_id: int | None = None,
                   followers: int | None = None, country: str | None = None, language: str | None = None,
                   bio: str | None = None, artist=None) -> tuple[Person, bool]:
    """Find the person owning this profile, or create them. Returns (person, created)."""
    existing = find_identity(session, ref.type, ref.value)
    created = existing is None
    if existing is not None:
        person = existing.person
    else:
        person = Person(name=(name or ref.display)[:255], kind=kind, found_via=found_via, found_by_user_id=found_by_user_id)
        session.add(person)
        session.flush()
        person = add_identity(session, person, ref.type, ref.value, display=ref.url, source_url=source_url)
    if artist is not None and person.discovered_artist_id is None:
        person.discovered_artist_id = artist.id  # which soundalike found them (fit tracking)
    if name and person.name.startswith("@") and not name.startswith("@"):
        person.name = name[:255]
    if followers is not None:
        person.followers = max(person.followers or 0, followers)
    person.country = person.country or country
    person.language = person.language or language
    if bio and not person.bio:
        person.bio = bio[:2000]
    return person, created


def ingest_profile_text(session: Session, person: Person, text: str | None, *, source_url: str) -> Person:
    """Emails and profile links a creator wrote about themselves (channel description,
    playlist description, bio). The text came straight from the platform, so emails
    in it count as found verbatim."""
    if not text:
        return person
    found = find_emails(None, text)
    for email in found.verbatim:
        person = attach_email(session, person, None, email, source_url=source_url, page_is_personal=True, merge=False)
    for email in found.unverified:
        person = add_identity(session, person, IdentityType.EMAIL, email, source_url=source_url, verified=False, merge=False)
    for url in URL_RE.findall(text):
        url = url.rstrip(".,;:!?")
        person = add_link(session, person, url, source_url=source_url)
    return person


def add_link(session: Session, person: Person, url: str, *, source_url: str | None, merge: bool = False) -> Person:
    ref = parse_social(url)
    if ref is not None:
        return add_identity(session, person, ref.type, ref.value, display=ref.url, source_url=source_url, merge=merge)
    site = site_identity(url)
    if site is not None:
        return add_identity(session, person, IdentityType.SITE, site.value, display=site.url, source_url=source_url, merge=merge)
    return person
