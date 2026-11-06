"""Dedupe (spec §4.6): link-following merges, fuzzy "probably the same" flags."""

from __future__ import annotations

import logging
from collections import defaultdict
from itertools import combinations

from rapidfuzz import fuzz
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ..models import (
    STATUS_RANK,
    InboundMessage,
    MergeCandidate,
    OptOut,
    Person,
)
from ..util.text import name_key
from ..util.timeutil import utcnow

log = logging.getLogger(__name__)

HANDLE_TYPES = ("tiktok", "instagram", "x", "youtube", "bluesky", "threads", "soundcloud", "facebook")


def _join(a: str | None, b: str | None) -> str | None:
    parts = [p for p in (a, b) if p]
    return "\n\n".join(dict.fromkeys(parts)) or None


def merge_people(session: Session, keep: Person, drop: Person) -> Person:
    """Fold ``drop`` into ``keep`` and delete it. Returns ``keep``."""
    if keep.id == drop.id:
        return keep
    session.flush()
    log.info("merging person %s (%s) into %s (%s)", drop.id, drop.name, keep.id, keep.name)

    for ident in list(drop.identities):
        ident.person = keep
    have = {(c.url, c.artist_name) for c in keep.coverage}
    for cov in list(drop.coverage):
        if (cov.url, cov.artist_name) in have:
            drop.coverage.remove(cov)  # orphan -> deleted
        else:
            cov.person = keep
            have.add((cov.url, cov.artist_name))
    keep_outlets = {link.outlet_id for link in keep.outlet_links}
    for link in list(drop.outlet_links):
        if link.outlet_id in keep_outlets:
            outlet = link.outlet
            drop.outlet_links.remove(link)
            if link in outlet.person_links:
                outlet.person_links.remove(link)
        else:
            link.person = keep  # primary-key switch: UPDATE person_outlets SET person_id
            keep_outlets.add(link.outlet_id)
    for o in list(drop.outreach):
        o.person = keep
    for m in list(drop.meetings):
        m.person = keep
    session.execute(update(InboundMessage).where(InboundMessage.person_id == drop.id).values(person_id=keep.id))
    session.execute(update(OptOut).where(OptOut.person_id == drop.id).values(person_id=keep.id))

    for cand in session.scalars(
        select(MergeCandidate).where(or_(MergeCandidate.person_a_id == drop.id, MergeCandidate.person_b_id == drop.id))
    ).all():
        other = cand.person_b_id if cand.person_a_id == drop.id else cand.person_a_id
        a, b = sorted((keep.id, other))
        if other == keep.id or session.scalar(
            select(MergeCandidate).where(MergeCandidate.person_a_id == a, MergeCandidate.person_b_id == b)
        ):
            session.delete(cand)
        else:
            cand.person_a_id, cand.person_b_id = a, b

    if STATUS_RANK.get(drop.status, 0) > STATUS_RANK.get(keep.status, 0):
        keep.status = drop.status
    if drop.opted_out and not keep.opted_out:
        keep.opted_out, keep.opted_out_at = True, drop.opted_out_at
    keep.cooldown_until = max(filter(None, (keep.cooldown_until, drop.cooldown_until)), default=None)
    keep.last_contacted_at = max(filter(None, (keep.last_contacted_at, drop.last_contacted_at)), default=None)
    keep.found_by_user_id = keep.found_by_user_id or drop.found_by_user_id
    keep.found_via = keep.found_via or drop.found_via
    if keep.is_desk and not drop.is_desk:
        keep.name, keep.is_desk = drop.name, False
    elif keep.name.startswith("@") and not drop.name.startswith("@"):
        keep.name = drop.name  # replace a bare handle with a real name
    keep.notes = _join(keep.notes, drop.notes)
    keep.bio = keep.bio or drop.bio
    keep.language = keep.language or drop.language
    keep.country = keep.country or drop.country
    keep.followers = max(filter(None, (keep.followers, drop.followers)), default=None)
    keep.needs_review = keep.needs_review and drop.needs_review
    keep.resolved_at = None  # re-resolve with the combined identities
    keep.scored_at = None
    session.delete(drop)
    session.flush()
    session.expire(keep)
    return keep


def find_merge_candidates(session: Session, *, name_threshold: int = 88) -> int:
    """Flag pairs that are probably the same person for one-tap confirmation."""
    people = list(session.scalars(select(Person).where(Person.is_desk.is_(False))))
    info = {}
    for p in people:
        handles = {(i.type, i.value.lstrip("@")) for i in p.identities if i.type in HANDLE_TYPES}
        info[p.id] = (name_key(p.name).lstrip("@"), {link.outlet_id for link in p.outlet_links}, handles)

    existing = {(c.person_a_id, c.person_b_id) for c in session.scalars(select(MergeCandidate))}
    found: dict[tuple[int, int], tuple[float, str]] = {}

    def propose(a: int, b: int, score: float, reason: str) -> None:
        pair = (min(a, b), max(a, b))
        if pair not in existing and (pair not in found or found[pair][0] < score):
            found[pair] = (score, reason)

    # 1) Similar names + overlapping outlet or handle.
    buckets: dict[str, set[int]] = defaultdict(set)
    for pid, (key, _, _) in info.items():
        for tok in key.split():
            if len(tok) >= 3:
                buckets[tok[:4]].add(pid)
    for members in buckets.values():
        if len(members) > 300:
            continue
        for a, b in combinations(sorted(members), 2):
            ka, oa, ha = info[a]
            kb, ob, hb = info[b]
            sim = fuzz.token_sort_ratio(ka, kb)
            if sim < name_threshold:
                continue
            shared_outlet = oa & ob
            handle_overlap = any(
                va == vb or fuzz.ratio(va, vb) >= 90 for _, va in ha for _, vb in hb
            )
            if shared_outlet or handle_overlap:
                why = "same outlet" if shared_outlet else "similar handles"
                propose(a, b, sim / 100, f"Similar names ({sim:.0f}%) and {why}")

    # 2) The same handle on two different platforms (e.g. TikTok and Instagram).
    by_handle: dict[str, set[tuple[int, str]]] = defaultdict(set)
    for pid, (_, _, handles) in info.items():
        for typ, val in handles:
            if len(val) >= 4:
                by_handle[val].add((pid, typ))
    for val, owners in by_handle.items():
        pids = sorted({pid for pid, _ in owners})
        for a, b in combinations(pids, 2):
            ta = {t for p, t in owners if p == a}
            tb = {t for p, t in owners if p == b}
            propose(a, b, 0.85, f"Same handle '{val}' on {', '.join(sorted(ta))} and {', '.join(sorted(tb))}")

    for (a, b), (score, reason) in found.items():
        session.add(MergeCandidate(person_a_id=a, person_b_id=b, score=score, reason=reason))
    session.flush()
    return len(found)


def resolve_candidate(session: Session, cand: MergeCandidate, *, same: bool, user_id: int | None = None) -> Person | None:
    cand.resolved_at = utcnow()
    cand.resolved_by_user_id = user_id
    if not same:
        cand.status = "rejected"
        session.flush()
        return None
    keep, drop = sorted((cand.person_a, cand.person_b), key=lambda p: p.id)
    cand.status = "merged"
    session.flush()
    survivor = merge_people(session, keep, drop)
    return survivor


def pending_partner_ids(session: Session) -> dict[int, set[int]]:
    """person id -> ids it may be a duplicate of (pending candidates)."""
    out: dict[int, set[int]] = defaultdict(set)
    for c in session.scalars(select(MergeCandidate).where(MergeCandidate.status == "pending")):
        out[c.person_a_id].add(c.person_b_id)
        out[c.person_b_id].add(c.person_a_id)
    return out
