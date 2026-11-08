""""Report a problem" on a found contact: everything needed to look into it, as text
that goes into the Details column of the suggestions sheet."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import __version__
from .discovery.engines import engine_for, merged_channels
from .models import Artist, MergeCandidate, Person, SearchRun


def _d(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "-"


def discovery_report(session: Session, person: Person) -> str:
    engine = engine_for(person)
    found_by = session.get(Artist, person.discovered_artist_id) if person.discovered_artist_id else None
    lines = [
        f"Contact #{person.id}: {person.name} ({person.kind})",
        f"Engine: {engine.label if engine else '-'} (found_via={person.found_via or '-'}), vetting={person.vetting}, status={person.status}",
        f"Found {_d(person.created_at)}; first soundalike: {found_by.name if found_by else '-'}",
        f"Followers: {person.followers or '-'}; country: {person.country or '-'}; language: {person.language or '-'}",
        f"Taste score: {round((person.taste_score or 0) * 100)}; resolved: {_d(person.resolved_at)}",
        f"App version: {__version__}",
    ]
    if merged_channels(person) > 1:
        lines.append(f"WARNING: holds {merged_channels(person)} YouTube channels (probably merged by mistake)")
    if person.needs_review:
        lines.append(f"Flagged: {person.review_reason or 'needs review'}")
    lines.append("")
    lines.append("Profiles and emails (type: value [verified] <- where it came from):")
    for i in sorted(person.identities, key=lambda i: (i.type, i.id)):
        lines.append(f"- {i.type}: {i.value}{' [verified]' if i.verified else ''} <- {i.source_url or '-'} (added {_d(i.created_at)})")
    lines.append("")
    lines.append("Coverage (what counts as them covering a soundalike):")
    for c in sorted(person.coverage, key=lambda c: c.id):
        lines.append(f"- [{c.source or '-'}] {c.artist_name}: \"{c.title or ''}\" {c.url} "
                     f"(published {c.published_at or '-'}, added {_d(c.created_at)})")
        if c.summary:
            lines.append(f"    description: {c.summary[:200]}")
    if person.bio:
        lines += ["", f"Bio / channel description: {person.bio[:1500]}"]
    dupes = list(session.scalars(select(MergeCandidate).where(
        (MergeCandidate.person_a_id == person.id) | (MergeCandidate.person_b_id == person.id))))
    if dupes:
        lines += ["", "Possible duplicates:"] + [f"- #{m.person_a_id} / #{m.person_b_id}: {m.reason} ({m.status})" for m in dupes]
    artist_ids = {c.artist_id for c in person.coverage if c.artist_id} | ({found_by.id} if found_by else set())
    if artist_ids and engine and engine.search_engine:
        runs = list(session.scalars(select(SearchRun).where(SearchRun.engine == engine.search_engine,
                                                            SearchRun.artist_id.in_(artist_ids))
                                    .order_by(SearchRun.run_at.desc()).limit(10)))
        if runs:
            lines += ["", "Searches for these soundalikes:"]
            lines += [f"- {_d(r.run_at)} {r.query} -> {r.result_count} results{' ERROR ' + r.error[:100] if r.error else ''}"
                      for r in runs]
    return "\n".join(lines)
