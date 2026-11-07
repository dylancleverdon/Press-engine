"""Soundalikes: finding as many similar artists as possible, and learning which ones
actually lead to press and creators who respond.

Sources of suggestions:
* Last.fm similar artists of each seed (first hop) - strong matches are used right away
* Last.fm similar artists of accepted soundalikes (second hop) - always a suggestion
* co-coverage - artists that several of your discovered contacts also covered

Fit tracking: every contact remembers which soundalike turned it up
(Person.discovered_artist_id). Once enough of those contacts have been pitched, the
soundalike's reply rate is compared with the overall rate. Weak ones count less in
taste scoring and are searched last; D can drop their uncontacted contacts and
exclude them.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .appsettings import AppSettings
from .discovery.lastfm import (
    LastFMError,
    expand_seeds,
    get_or_create_artist,
    link_similar,
    recompute_similarities,
)
from .models import OPEN_OUTREACH, Artist, Coverage, OutreachStatus, Person, PersonStatus
from .util.text import name_key
from .util.timeutil import utcnow

log = logging.getLogger(__name__)

REPLIED = (PersonStatus.REPLIED, PersonStatus.IN_CONVERSATION, PersonStatus.COVERED)


# --- finding soundalikes ------------------------------------------------------------------

def second_degree(session: Session, lastfm, settings: AppSettings, *, now: datetime) -> dict:
    """Look up soundalikes of the best accepted soundalikes (a few per run)."""
    parents = list(session.scalars(
        select(Artist)
        .where(Artist.source == "expanded", Artist.active.is_(True), Artist.review.in_(("auto", "accepted")),
               Artist.similarity > 0, Artist.fit_verdict != "weak")
        .order_by(Artist.expanded_at.is_not(None), Artist.expanded_at, Artist.similarity.desc())
        .limit(settings.second_degree_per_run)
    ))
    stats = {"looked_up": 0, "new_suggestions": 0, "errors": []}
    for parent in parents:
        try:
            similar = lastfm.similar(parent.name, limit=30)
        except LastFMError as e:
            stats["errors"].append(f"{parent.name}: {e}")
            continue
        stats["looked_up"] += 1
        for s in similar:
            artist, created = link_similar(session, parent, s, auto=False,
                                           reason=f"Similar to {parent.name} ({round(s.match * 100)}%), which is similar to your seeds")
            stats["new_suggestions"] += int(bool(artist) and created)
        parent.expanded_at = now
    session.flush()
    return stats


def cocoverage(session: Session, settings: AppSettings) -> dict:
    """Artists that several discovered contacts covered, which aren't soundalikes yet."""
    known = {a.name_key: a for a in session.scalars(select(Artist))}
    writers: dict[str, set[int]] = defaultdict(set)
    names: dict[str, str] = {}
    for cov in session.scalars(select(Coverage).where(Coverage.artist_id.is_(None))):
        key = name_key(cov.artist_name)
        if len(key) < 2:
            continue
        writers[key].add(cov.person_id)
        names.setdefault(key, cov.artist_name)
    stats = {"new_suggestions": 0, "updated": 0}
    for key, people in writers.items():
        count = len(people)
        if count < settings.cocoverage_min_contacts:
            continue
        artist = known.get(key)
        if artist is not None and artist.source != "cocoverage":
            continue  # already a seed / Last.fm soundalike
        if artist is None:
            artist, _ = get_or_create_artist(session, names[key], source="cocoverage")
            artist.review = "suggested"
            stats["new_suggestions"] += 1
        else:
            stats["updated"] += 1
        artist.similarity = round(min(0.7, 0.15 + 0.1 * count), 4)
        artist.suggested_reason = f"{count} of your contacts covered {artist.name} alongside your soundalikes"
        artist.evidence = {**(artist.evidence or {}), "cocoverage_contacts": count}
        # Link the existing coverage rows so taste scoring sees it once accepted.
        for cov in session.scalars(select(Coverage).where(Coverage.artist_id.is_(None))):
            if name_key(cov.artist_name) == key:
                cov.artist = artist
    session.flush()
    return stats


def discover_soundalikes(session: Session, svc, settings: AppSettings, *, now: datetime | None = None,
                         force: bool = False) -> dict:
    """The soundalike part of the nightly run."""
    from datetime import timedelta

    now = now or utcnow()
    stats: dict = {}
    if svc.lastfm is None:
        stats["lastfm"] = {"skipped": "no Last.fm API key (PRESS_LASTFM_API_KEY)"}
    else:
        week_ago = now - timedelta(days=7)
        seeds = [s for s in session.scalars(select(Artist).where(Artist.source == "manual", Artist.active.is_(True)))
                 if force or not s.tags or s.expanded_at is None or s.expanded_at < week_ago]
        if seeds:
            stats["seeds"] = expand_seeds(session, svc.lastfm, per_seed=settings.similar_per_seed, seeds=seeds,
                                          now=now, auto_accept=settings.auto_accept_similarity)
        stats["second_degree"] = second_degree(session, svc.lastfm, settings, now=now)
    stats["cocoverage"] = cocoverage(session, settings)
    recompute_similarities(session, auto_accept=settings.auto_accept_similarity)
    stats["fit"] = update_fit(session, settings)
    return stats


# --- reviewing ----------------------------------------------------------------------------

def accept(session: Session, artist: Artist) -> None:
    artist.review = "accepted"
    artist.active = True
    recompute_similarities(session)


def reject(session: Session, artist: Artist) -> None:
    artist.review = "rejected"
    artist.active = False
    recompute_similarities(session)


# --- fit tracking ---------------------------------------------------------------------------

@dataclass
class Fit:
    contacts: int = 0
    pitched: int = 0
    replied: int = 0
    covered: int = 0
    declined: int = 0
    person_ids: list[int] = field(default_factory=list)

    @property
    def reply_rate(self) -> float | None:
        return self.replied / self.pitched if self.pitched else None


def _discovery_artist(person: Person) -> int | None:
    if person.discovered_artist_id:
        return person.discovered_artist_id
    linked = [c for c in person.coverage if c.artist_id]
    return min(linked, key=lambda c: c.id).artist_id if linked else None


def performance(session: Session) -> tuple[dict[int, Fit], Fit]:
    """Per-artist outcomes of the contacts each soundalike turned up, plus the totals."""
    per: dict[int, Fit] = defaultdict(Fit)
    total = Fit()
    people = session.scalars(select(Person).options(selectinload(Person.outreach), selectinload(Person.coverage)))
    for p in people:
        artist_id = _discovery_artist(p)
        pitched = any(o.sent_at is not None for o in p.outreach)
        replied = p.status in REPLIED or any(o.status == OutreachStatus.REPLIED for o in p.outreach)
        declined = p.status == PersonStatus.NOT_INTERESTED or p.opted_out
        for fit in ([per[artist_id]] if artist_id else []) + [total]:
            fit.contacts += 1
            fit.pitched += int(pitched)
            fit.replied += int(pitched and replied)
            fit.covered += int(p.status == PersonStatus.COVERED)
            fit.declined += int(declined)
        if artist_id:
            per[artist_id].person_ids.append(p.id)
    return dict(per), total


VERDICT_FACTOR = {"learning": 1.0, "strong": 1.25, "ok": 1.0, "weak": 0.6}


def verdict(fit: Fit, total: Fit, min_pitches: int) -> tuple[str, str]:
    if fit.pitched < min_pitches:
        return "learning", f"{fit.pitched} of {min_pitches} pitches needed to judge"
    rate = fit.reply_rate or 0.0
    if fit.replied == 0 and fit.pitched >= 2 * min_pitches:
        return "weak", f"no replies from {fit.pitched} pitches"
    if total.replied < 3 or not total.pitched:
        return "learning", "not enough replies overall yet to compare"
    overall = total.replied / total.pitched
    pct = f"{round(rate * 100)}% reply rate vs {round(overall * 100)}% overall"
    if rate >= max(overall * 1.25, overall + 0.05):
        return "strong", pct
    if rate <= overall * 0.5:
        return "weak", pct
    return "ok", pct


def update_fit(session: Session, settings: AppSettings) -> dict:
    per, total = performance(session)
    counts: dict[str, int] = defaultdict(int)
    for artist in session.scalars(select(Artist)):
        fit = per.get(artist.id, Fit())
        v, why = verdict(fit, total, settings.fit_min_pitches)
        artist.fit_verdict = v
        artist.fit_factor = VERDICT_FACTOR[v]
        artist.fit_stats = {"contacts": fit.contacts, "pitched": fit.pitched, "replied": fit.replied,
                            "covered": fit.covered, "declined": fit.declined, "why": why,
                            "reply_rate": round(fit.reply_rate, 3) if fit.reply_rate is not None else None}
        counts[v] += 1
    session.flush()
    return {"overall_pitched": total.pitched, "overall_replied": total.replied, **counts}


# --- pivoting away --------------------------------------------------------------------------

def contacts_from(session: Session, artist: Artist) -> list[Person]:
    per, _ = performance(session)
    ids = per.get(artist.id, Fit()).person_ids
    return list(session.scalars(select(Person).where(Person.id.in_(ids)))) if ids else []


def prune(session: Session, artist: Artist, *, now: datetime | None = None) -> dict:
    """Exclude the soundalike and delete the contacts it found that were never contacted.
    Contacts already pitched stay (history, replies, cooldowns). Opt-outs are kept on
    the suppression list regardless."""
    removed = kept = 0
    for person in contacts_from(session, artist):
        if any(o.sent_at is not None for o in person.outreach):
            kept += 1
            continue
        for o in person.outreach:
            if o.status in OPEN_OUTREACH:
                o.status = OutreachStatus.CANCELLED
        session.delete(person)
        removed += 1
    if artist.is_seed:
        artist.active = False
    else:
        artist.review = "rejected"
        artist.active = False
    session.flush()
    recompute_similarities(session)
    log.info("pruned %s: removed %d contacts, kept %d already contacted", artist.name, removed, kept)
    return {"removed": removed, "kept": kept}
