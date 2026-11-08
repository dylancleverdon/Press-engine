"""Contact resolution (spec §4.4): look for an email before falling back to DMs.

Order: author bio page / outlet contact page -> link-in-bio pages -> personal
site (+ its contact page). YouTube "About" emails come from the channel
description via the YouTube API (see discovery/youtube.py); Instagram business
emails can only be added by hand through the capture tools.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..discovery.pages import fetch_page, get_page
from ..models import Identity, IdentityType, Person, PersonKind
from ..util.emails import is_generic, matches_name
from ..util.text import contains_phrase, fold, name_tokens
from ..util.timeutil import utcnow
from ..util.urls import host_of, is_social_or_platform_host, parse_social, site_identity
from .ingest import add_identity, attach_email

log = logging.getLogger(__name__)

CONTACT_PRIORITY = ("impressum", "kontakt", "contact", "about", "om-os", "over-ons", "team", "staff", "masthead")
GUESS_PATHS = ("/contact", "/kontakt", "/impressum")


def verified_personal_email(person: Person) -> Identity | None:
    for ident in person.identities:
        if ident.type == IdentityType.EMAIL and ident.verified and not ident.bounced:
            return ident
    return None


def _author_links(page, person: Person) -> list[str]:
    tokens = name_tokens(person.name)
    joined = " ".join(tokens)
    out = []
    for link in page.links or []:
        if link.get("kind") != "author":
            continue
        if contains_phrase(link.get("text"), person.name) or (len(tokens) >= 2 and joined in fold(link["href"])):
            out.append(link["href"])
    return out


def _contact_links(page) -> list[str]:
    links = [link["href"] for link in page.links or [] if link.get("kind") == "contact"]

    def rank(href: str) -> int:
        h = href.lower()
        return next((i for i, word in enumerate(CONTACT_PRIORITY) if word in h), len(CONTACT_PRIORITY))

    return sorted(dict.fromkeys(links), key=rank)


def _candidates(session: Session, person: Person) -> list[tuple[str, str]]:
    cands: list[tuple[str, str]] = []
    if person.kind == PersonKind.PRESS:
        contact_found = False
        for cov in sorted(person.coverage, key=lambda c: c.created_at, reverse=True)[:5]:
            page = get_page(session, cov.url)
            if page is None:
                continue
            if not person.is_desk:
                cands += [(u, "author") for u in _author_links(page, person)[:2]]
            contacts = _contact_links(page)[:2]
            contact_found = contact_found or bool(contacts)
            cands += [(u, "contact") for u in contacts]
        if not contact_found:
            for outlet in person.outlets[:1]:
                cands += [(f"https://{outlet.domain}{p}", "contact") for p in GUESS_PATHS[:1]]
    for ident in person.identities:
        if ident.type == IdentityType.LINKINBIO:
            url = ident.display if (ident.display or "").startswith("http") else f"https://{ident.value}"
            cands.append((url, "bio"))
        elif ident.type == IdentityType.SITE:
            cands.append((f"https://{ident.value}/", "site"))
    seen: set[str] = set()
    out = []
    for url, mode in cands:
        if url not in seen:
            seen.add(url)
            out.append((url, mode))
    return out


def resolve_person(session: Session, svc, person: Person, *, now: datetime | None = None, max_fetches: int = 4) -> Person:
    """Fetch a few likely pages and attach any emails/profiles found. Returns the
    (possibly merged) person."""
    now = now or utcnow()
    queue = _candidates(session, person)
    fetched = 0
    while queue and fetched < max_fetches:
        url, mode = queue[0]
        if verified_personal_email(person) and mode != "bio":
            # An email is enough for pitching, but link pages are still read in full:
            # they list the creator's other profiles.
            queue.pop(0)
            continue
        queue.pop(0)
        session.commit()  # don't hold SQLite's write lock across the network call
        page = fetch_page(session, svc.fetcher, url, max_age_days=30, now=now)
        fetched += 1
        if not page.ok:
            continue
        final = page.final_url or url
        outlet = next((o for o in person.outlets if host_of(final).endswith(o.domain)), None)
        personal_page = mode in ("author", "bio", "site")
        # Link pages and personal sites also link collaborators, so anything there that
        # belongs to someone else goes to Duplicates instead of merging.
        merge = mode not in ("bio", "site", "site_contact")
        emails = page.emails or []
        non_generic = [e for e in emails if not is_generic(e)]
        for email in emails:
            if person.is_desk and mode == "contact" and len(non_generic) == 1 and email == non_generic[0]:
                person = attach_email(session, person, outlet, email, source_url=final, page_is_personal=True, merge=merge)
            else:
                person = attach_email(session, person, outlet, email, source_url=final, page_is_personal=personal_page, merge=merge)
        for email in page.unverified_emails or []:
            if (personal_page or matches_name(email, person.name)) and not is_generic(email):
                person = add_identity(session, person, IdentityType.EMAIL, email, source_url=final, verified=False, merge=merge)
        if personal_page:
            outlet_domains = {o.domain for o in person.outlets}
            for link in page.links or []:
                href = link["href"]
                ref = parse_social(href) if link.get("kind") == "social" else None
                if ref:
                    person = add_identity(session, person, ref.type, ref.value, display=ref.display, source_url=final, merge=merge)
                elif mode == "bio" and href.startswith("http"):
                    site = site_identity(href)
                    if site and site.value not in outlet_domains and not is_social_or_platform_host(host_of(href)):
                        person = add_identity(session, person, IdentityType.SITE, site.value, display=site.url, source_url=final, merge=False)
                        queue.append((site.url, "site"))
            if mode == "site":
                queue += [(u, "site_contact") for u in _contact_links(page)[:1]]
        if mode == "site_contact":
            for email in emails:
                person = attach_email(session, person, None, email, source_url=final, page_is_personal=not is_generic(email), merge=False)
    person.resolved_at = now
    session.flush()
    return person


def resolve_pending(session: Session, svc, *, limit: int = 40, now: datetime | None = None, recheck_days: int = 30) -> dict:
    """Resolve the best-scored people that still lack a verified email."""
    now = now or utcnow()
    stale = now - timedelta(days=recheck_days)
    due = (Person.opted_out.is_(False), (Person.resolved_at.is_(None)) | (Person.resolved_at < stale))
    # Anyone with a link-in-bio page that hasn't been read yet goes first (new finds
    # waiting to be vetted included), then the best-scored rest.
    with_bio = session.scalars(
        select(Person).where(*due, Person.resolved_at.is_(None),
                             Person.identities.any(Identity.type == IdentityType.LINKINBIO))
        .order_by(Person.id.desc()).limit(limit)
    ).all()
    people = list(with_bio) + [p for p in session.scalars(
        select(Person).where(*due).order_by(Person.taste_score.desc(), Person.id).limit(limit * 3)
    ).all() if p not in with_bio]
    stats = {"checked": 0, "found_email": 0}
    seen: set[int] = set()
    for person in people:
        if stats["checked"] >= limit:
            break
        state = sa_inspect(person)
        has_unread_bio = person.resolved_at is None and any(i.type == IdentityType.LINKINBIO for i in person.identities)
        if state.was_deleted or state.deleted or person.id in seen or (verified_personal_email(person) and not has_unread_bio):
            continue  # merged away earlier in this batch, or already done
        try:
            person = resolve_person(session, svc, person, now=now)
        except Exception as e:  # one bad site must not stop the batch
            log.warning("resolution failed for person %s: %s", person.id, e)
            continue
        seen.add(person.id)
        stats["checked"] += 1
        if verified_personal_email(person):
            stats["found_email"] += 1
        session.commit()
    return stats
