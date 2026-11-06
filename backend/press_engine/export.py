"""CSV export of contacts, for checking lead quality by hand (spec §7 phase 1)."""

from __future__ import annotations

import csv
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .appsettings import load_settings
from .models import Person
from .outreach.queue import dm_target, email_target

COLUMNS = ["id", "name", "kind", "status", "taste_score", "llm_score", "score_reason", "outlets", "email", "email_kind",
           "dm", "artists_covered", "latest_piece", "latest_piece_url", "language", "country", "found_via", "found_by",
           "opted_out", "created_at"]


def write_contacts_csv(session: Session, out: TextIO) -> int:
    settings = load_settings(session)
    w = csv.writer(out)
    w.writerow(COLUMNS)
    rows = session.scalars(
        select(Person).options(selectinload(Person.identities), selectinload(Person.coverage), selectinload(Person.outlet_links))
        .order_by(Person.taste_score.desc())
    )
    n = 0
    for p in rows:
        target = email_target(p, settings)
        dm = dm_target(p)
        latest = max(p.coverage, key=lambda c: (c.published_at is not None, c.published_at, c.id), default=None)
        w.writerow([
            p.id, p.name, p.kind, p.status, round(p.taste_score or 0, 3), p.llm_score, p.score_reason or "",
            "; ".join(o.name for o in p.outlets), target.address if target else "", target.kind if target else "",
            f"{dm.type}:{dm.value}" if dm else "", "; ".join(sorted({c.artist_name for c in p.coverage})),
            latest.title if latest else "", latest.url if latest else "", p.language or "", p.country or "",
            p.found_via or "", p.found_by.name if p.found_by else "", p.opted_out, p.created_at.isoformat(),
        ])
        n += 1
    return n
