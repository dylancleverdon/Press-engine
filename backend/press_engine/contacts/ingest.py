"""Turn extractions / API results into People, Identities, Outlets and Coverage."""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..discovery.extract import Extraction
from ..discovery.known import KnownArtists
from ..models import (
    Artist,
    Coverage,
    Identity,
    IdentityType,
    Outlet,
    Page,
    Person,
    PersonKind,
    PersonOutlet,
)
from ..util.emails import is_generic, matches_name, normalize_email
from ..util.text import name_key
from ..util.urls import host_of, parse_social
from .dedupe import merge_people

log = logging.getLogger(__name__)


def find_or_create_outlet(session: Session, url: str, *, name: str | None = None, language: str | None = None) -> Outlet:
    domain = host_of(url)
    outlet = session.scalar(select(Outlet).where(Outlet.domain == domain))
    if outlet is None:
        outlet = Outlet(domain=domain, name=(name or domain)[:255], language=language)
        session.add(outlet)
        session.flush()
    else:
        if name and outlet.name == outlet.domain:
            outlet.name = name[:255]
        outlet.language = outlet.language or language
    return outlet


def link_outlet(session: Session, person: Person, outlet: Outlet, role: str | None = None) -> None:
    if any(link.outlet_id == outlet.id for link in person.outlet_links):
        return
    session.add(PersonOutlet(person=person, outlet=outlet, role=role))
    session.flush()


def find_identity(session: Session, type_: str, value: str) -> Identity | None:
    return session.scalar(select(Identity).where(Identity.type == type_, Identity.value == value))


def add_identity(
    session: Session,
    person: Person,
    type_: str,
    value: str,
    *,
    display: str | None = None,
    source_url: str | None = None,
    verified: bool = False,
) -> Person:
    """Attach an identity. If another person already owns it, the two are merged
    (link following, spec §4.6). Returns the surviving person."""
    if type_ == IdentityType.EMAIL:
        value = normalize_email(value)
    existing = find_identity(session, type_, value)
    if existing is None:
        session.add(Identity(person=person, type=type_, value=value, display=display or value,
                             source_url=source_url, verified=verified))
        session.flush()
        return person
    if existing.person_id == person.id:
        if verified and not existing.verified:
            existing.verified = True
            existing.source_url = source_url or existing.source_url
        return person
    other = existing.person
    keep, drop = (other, person) if other.id < person.id else (person, other)
    survivor = merge_people(session, keep, drop)
    if verified and not existing.verified:
        existing.verified = True
        existing.source_url = source_url or existing.source_url
    return survivor


def add_coverage(
    session: Session,
    person: Person,
    *,
    url: str,
    artist_name: str,
    artist: Artist | None = None,
    outlet: Outlet | None = None,
    title: str | None = None,
    kind: str | None = None,
    published_at: date | None = None,
    language: str | None = None,
    summary: str | None = None,
    source: str | None = None,
) -> Coverage:
    for c in person.coverage:
        if c.url == url and name_key(c.artist_name) == name_key(artist_name):
            if artist and not c.artist_id:
                c.artist = artist
            return c
    cov = Coverage(person=person, url=url, artist_name=artist_name, artist=artist, outlet=outlet, title=title,
                   kind=kind, published_at=published_at, language=language, summary=summary, source=source)
    session.add(cov)
    session.flush()
    return cov


def find_person_at_outlet(outlet: Outlet, name: str) -> Person | None:
    key = name_key(name)
    for link in outlet.person_links:
        if name_key(link.person.name) == key:
            return link.person
    return None


def find_desk(outlet: Outlet) -> Person | None:
    for link in outlet.person_links:
        if link.person.is_desk:
            return link.person
    return None


def attach_email(session: Session, person: Person, outlet: Outlet | None, email: str, *, source_url: str,
                 verified: bool = True, page_is_personal: bool = False) -> Person:
    """Personal-looking address -> person identity; generic inbox -> outlet contact."""
    email = normalize_email(email)
    personal = (matches_name(email, person.name) or page_is_personal) and not is_generic(email)
    if personal:
        return add_identity(session, person, IdentityType.EMAIL, email, source_url=source_url, verified=verified)
    if outlet is not None and verified and is_generic(email):
        if not outlet.contact_email:
            outlet.contact_email = email
            outlet.contact_email_source = source_url
    return person


def ingest_press_page(
    session: Session,
    page: Page,
    extraction: Extraction,
    known: KnownArtists,
    *,
    artist_hint: str | None = None,
    source: str = "press_search",
    found_by_user_id: int | None = None,
) -> tuple[list[Person], int]:
    """Create/update the writers of a relevant article. Returns (people, number newly created)."""
    url = page.final_url or page.url
    outlet = find_or_create_outlet(session, url, name=extraction.outlet_name, language=extraction.language)

    artists: list[tuple[str, Artist | None]] = []
    for name in extraction.artists_covered or ([artist_hint] if artist_hint else []):
        artists.append((name, known.match(name)))
    if artist_hint and not any(name_key(n) == name_key(artist_hint) for n, _ in artists):
        if known.match(artist_hint):
            artists.append((artist_hint, known.match(artist_hint)))

    people: list[Person] = []
    created = 0
    authors = extraction.authors
    if not authors:
        desk = find_desk(outlet)
        if desk is None:
            desk = Person(name=outlet.name, kind=PersonKind.PRESS, is_desk=True, language=extraction.language,
                          found_via=source, found_by_user_id=found_by_user_id)
            session.add(desk)
            session.flush()
            link_outlet(session, desk, outlet)
            created += 1
        people.append(desk)
    else:
        for author in authors:
            person = find_person_at_outlet(outlet, author.name)
            if person is None:
                person = Person(name=author.name, kind=PersonKind.PRESS, language=extraction.language,
                                found_via=source, found_by_user_id=found_by_user_id)
                session.add(person)
                session.flush()
                link_outlet(session, person, outlet, author.role)
                created += 1
            if author.email:
                person = attach_email(session, person, outlet, author.email, source_url=url)
            people.append(person)

    for person in people:
        for name, artist in artists:
            add_coverage(session, person, url=url, artist_name=artist.name if artist else name, artist=artist,
                         outlet=outlet, title=page.title, kind=extraction.content_type,
                         published_at=extraction.published, language=extraction.language,
                         summary=extraction.summary, source=source)

    # Emails on the article itself: personal ones for a named writer, generic ones for the outlet.
    for email in page.emails or []:
        owner = next((p for p in people if matches_name(email, p.name)), None)
        if owner is not None:
            attach_email(session, owner, outlet, email, source_url=url)
        elif is_generic(email):
            attach_email(session, people[0], outlet, email, source_url=url)
    return people, created


def social_refs_from_links(links: list[dict] | None) -> list:
    out = []
    for link in links or []:
        if link.get("kind") == "social":
            ref = parse_social(link["href"])
            if ref:
                out.append(ref)
    return out
