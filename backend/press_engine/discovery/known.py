"""Lookup of seed / similar artists for matching names found in pages."""

from __future__ import annotations

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Artist
from ..util.text import contains_phrase, name_key


class KnownArtists:
    def __init__(self, session: Session):
        rows = session.scalars(
            select(Artist).where(Artist.active.is_(True), (Artist.source == "manual") | (Artist.similarity > 0))
        )
        self.by_key: dict[str, Artist] = {a.name_key: a for a in rows}

    def __len__(self) -> int:
        return len(self.by_key)

    def match(self, name: str | None) -> Artist | None:
        key = name_key(name)
        if not key:
            return None
        if key in self.by_key:
            return self.by_key[key]
        if len(key) >= 6:
            best, best_score = None, 0.0
            for k, a in self.by_key.items():
                score = fuzz.ratio(key, k)
                if score > best_score:
                    best, best_score = a, score
            if best_score >= 92:
                return best
        return None

    def mentioned_in(self, text: str | None, *, limit: int = 20) -> list[Artist]:
        """Known artists whose name appears as a whole phrase in ``text``."""
        if not text:
            return []
        found = [a for a in self.by_key.values() if len(a.name_key) >= 3 and contains_phrase(text, a.name)]
        found.sort(key=lambda a: (not a.is_seed, -a.similarity))
        return found[:limit]
