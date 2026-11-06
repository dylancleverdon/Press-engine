"""Small helpers to build test data."""

from __future__ import annotations

from datetime import date

from press_engine.contacts.ingest import add_coverage, add_identity, find_or_create_outlet, link_outlet
from press_engine.discovery.lastfm import get_or_create_artist
from press_engine.models import Artist, Person, PersonKind


def artist(session, name: str, *, seed: bool = False, similarity: float = 0.8) -> Artist:
    a, _ = get_or_create_artist(session, name, source="manual" if seed else "expanded")
    if not seed:
        a.similarity = similarity
    session.flush()
    return a


def person(session, name: str, *, outlet: str | None = None, email: str | None = None, verified: bool = True,
           covers: list[Artist] | None = None, kind: str = PersonKind.PRESS, handles: dict | None = None,
           published: date | None = None, outlet_email: str | None = None) -> Person:
    p = Person(name=name, kind=kind)
    session.add(p)
    session.flush()
    o = None
    if outlet:
        o = find_or_create_outlet(session, f"https://{outlet}/", name=outlet.split(".")[0].title())
        link_outlet(session, p, o)
        if outlet_email:
            o.contact_email = outlet_email
    if email:
        p = add_identity(session, p, "email", email, verified=verified, source_url="https://evidence.test/page")
    for typ, value in (handles or {}).items():
        p = add_identity(session, p, typ, value, display="@" + value)
    for i, a in enumerate(covers or []):
        add_coverage(session, p, url=f"https://{outlet or 'site.test'}/{name.split()[0].lower()}/{i}",
                     artist_name=a.name, artist=a, outlet=o, title=f"{a.name} review", kind="review",
                     published_at=published or date(2026, 9, 1))
    session.flush()
    return p
