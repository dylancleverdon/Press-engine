"""Taste scoring (spec §4.7).

Stage 1 (everyone, free): a heuristic built from which seed/similar artists the
contact has covered. Several matching artists rank highest; recency is a small
secondary factor.

Stage 2 (top ~20-50 a day): the larger model reads the contact's coverage and
the band's sound profile, scores taste 0-100, explains why, and writes a
personalised opening line that must reference one of their real pieces.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta

from pydantic import BaseModel, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .appsettings import AppSettings
from .llm import LLMClient, LLMError
from .models import DM_PLATFORMS, Artist, Coverage, IdentityType, Person, PersonStatus
from .util.text import truncate
from .util.timeutil import utcnow

log = logging.getLogger(__name__)

LLM_WEIGHT = 0.6


def artist_weight(artist: Artist | None) -> float:
    if artist is None or not artist.active:
        return 0.0
    if artist.is_seed:
        return 1.0
    if artist.similarity <= 0:
        return 0.0
    return 0.3 + 0.7 * artist.similarity


def taste_profile(person: Person, today: date | None = None) -> tuple[dict, float]:
    today = today or date.today()
    artists: dict[str, float] = {}
    others: set[str] = set()
    latest: date | None = None
    for cov in person.coverage:
        w = artist_weight(cov.artist)
        if w <= 0:
            others.add(cov.artist_name)
            continue
        artists[cov.artist.name] = max(artists.get(cov.artist.name, 0.0), w)
        if cov.published_at and (latest is None or cov.published_at > latest):
            latest = cov.published_at
    total = sum(artists.values())
    taste = 1 - math.exp(-total / 1.5)
    recency = math.exp(-max(0, (today - latest).days) / 365) if latest else 0.5
    score = round(taste * (0.85 + 0.15 * recency), 4)
    profile = {
        "artists": dict(sorted(artists.items(), key=lambda kv: -kv[1])),
        "other_artists": sorted(others)[:30],
        "matched": len(artists),
        "latest": latest.isoformat() if latest else None,
    }
    return profile, score


def combined_score(person: Person) -> float:
    if person.llm_score is None:
        return person.heuristic_score
    return round(LLM_WEIGHT * person.llm_score / 100 + (1 - LLM_WEIGHT) * person.heuristic_score, 4)


def score_heuristic(session: Session, *, only_stale: bool = False, now: datetime | None = None) -> int:
    now = now or utcnow()
    q = select(Person)
    if only_stale:
        q = q.where(Person.scored_at.is_(None))
    n = 0
    for person in session.scalars(q):
        profile, score = taste_profile(person, now.date())
        person.taste_profile = profile
        person.heuristic_score = score
        person.taste_score = combined_score(person)
        person.scored_at = now
        n += 1
    session.flush()
    return n


def has_contact_path(person: Person) -> bool:
    if any(i.type == IdentityType.EMAIL and i.verified and not i.bounced for i in person.identities):
        return True
    if any(o.contact_email for o in person.outlets):
        return True
    return any(i.type in DM_PLATFORMS for i in person.identities)


SCORE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "score": {"type": "integer"},
        "reason": {"type": "string"},
        "opener": {"type": "string"},
        "piece_url": {"type": ["string", "null"]},
    },
    "required": ["score", "reason", "opener", "piece_url"],
}

SCORE_SYSTEM = (
    "You help an independent band find music writers, curators and creators whose taste matches the band's. "
    "Be honest and calibrated: most people are a partial match. Reply with JSON only."
)

SCORE_PROMPT = """Band: {band}
Band's sound: {profile}
Seed artists (the band sounds like these): {seeds}
Similar artists: {similar}

Contact: {name} - {kind}{outlet}
What they have covered, most recent first (some entries may be in other languages; translate as needed):
{coverage}

Return:
- score: 0-100, how close this contact's taste is to the band's sound. This is the main criterion. Covering several of the seed or similar artists should score high. Recency and outlet size matter only a little.
- reason: one sentence in English explaining the score.
- opener: one friendly, specific sentence in English to open a pitch email, referring to one of the pieces listed above about a similar artist. Do not invent details that are not in the titles or summaries, and do not overdo the praise.
- piece_url: the exact URL (from the list) of the piece the opener refers to, or null."""


class ScoreResult(BaseModel):
    score: int
    reason: str
    opener: str = ""
    piece_url: str | None = None

    @field_validator("score")
    @classmethod
    def _clamp(cls, v: int) -> int:
        return max(0, min(100, v))


def _coverage_lines(person: Person, limit: int = 12) -> list[Coverage]:
    return sorted(person.coverage, key=lambda c: (c.published_at or date.min, c.id), reverse=True)[:limit]


def llm_score_person(person: Person, settings: AppSettings, llm: LLMClient, seeds: list[str], similar: list[str]) -> ScoreResult | None:
    covs = _coverage_lines(person)
    if not covs:
        return None
    lines = []
    for c in covs:
        when = c.published_at.isoformat() if c.published_at else "undated"
        summary = f" - {truncate(c.summary, 200)}" if c.summary else ""
        lines.append(f'- [{when}] {c.kind or "piece"} about {c.artist_name}: "{truncate(c.title, 140)}" ({c.url}){summary}')
    outlet = f" at {person.outlets[0].name}" if person.outlets else ""
    prompt = SCORE_PROMPT.format(
        band=settings.band_name,
        profile=settings.sound_profile_text or "Not written yet; use the seed artists as the reference.",
        seeds=", ".join(seeds) or "none",
        similar=", ".join(similar[:20]) or "none",
        name=person.name,
        kind=person.kind,
        outlet=outlet,
        coverage="\n".join(lines),
    )
    try:
        result = ScoreResult.model_validate(llm.json(SCORE_SYSTEM, prompt, SCORE_SCHEMA, max_tokens=600))
    except (LLMError, ValidationError) as e:
        log.info("LLM scoring failed for person %s: %s", person.id, str(e)[:200])
        return None
    urls = {c.url for c in covs}
    if result.piece_url not in urls:
        result.piece_url = None
        result.opener = ""  # can't verify what it refers to
    return result


def score_with_llm(session: Session, llm: LLMClient | None, settings: AppSettings, *, now: datetime | None = None,
                   limit: int | None = None, rescore_after_days: int = 30) -> dict:
    """Run the larger model over today's best heuristic candidates."""
    if llm is None:
        return {"skipped": "no scoring model configured (PRESS_SCORE_LLM_PROVIDER)"}
    now = now or utcnow()
    limit = limit or settings.llm_score_top_n
    stale = now - timedelta(days=rescore_after_days)
    seeds = [a.name for a in session.scalars(select(Artist).where(Artist.source == "manual", Artist.active.is_(True)))]
    similar = [a.name for a in session.scalars(
        select(Artist).where(Artist.source == "expanded", Artist.active.is_(True), Artist.similarity > 0)
        .order_by(Artist.similarity.desc()).limit(20))]
    candidates = session.scalars(
        select(Person)
        .where(Person.status == PersonStatus.NEW, Person.opted_out.is_(False), Person.heuristic_score > 0,
               (Person.llm_scored_at.is_(None)) | (Person.llm_scored_at < stale))
        .order_by(Person.heuristic_score.desc())
        .limit(limit * 4)
    ).all()
    stats = {"scored": 0, "failed": 0}
    for person in candidates:
        if stats["scored"] + stats["failed"] >= limit:
            break
        if not has_contact_path(person):
            continue
        result = llm_score_person(person, settings, llm, seeds, similar)
        person.llm_scored_at = now
        if result is None:
            stats["failed"] += 1
            continue
        person.llm_score = float(result.score)
        person.score_reason = result.reason
        if result.opener:
            person.opener = result.opener
            person.opener_source_url = result.piece_url
        person.taste_score = combined_score(person)
        stats["scored"] += 1
        session.commit()
    return stats
