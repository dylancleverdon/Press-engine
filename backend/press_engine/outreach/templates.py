"""Pitch and DM rendering (spec §4.8 personalisation)."""

from __future__ import annotations

import re
from datetime import date

from ..appsettings import AppSettings
from ..models import Coverage, Person
from ..scoring import artist_weight

KIND_NOUN = {
    "review": "review of", "interview": "interview with", "feature": "piece on", "news": "piece on",
    "list": "list featuring", "other": "piece on", "video": "video on", "playlist": "playlist featuring",
    "tiktok": "TikTok featuring",
}


class _Blank(dict):
    def __missing__(self, key):
        return ""


def best_piece(person: Person) -> Coverage | None:
    """The piece to reference: most relevant artist first, then most recent."""
    if not person.coverage:
        return None
    return max(person.coverage, key=lambda c: (artist_weight(c.artist), c.published_at or date.min, c.id))


def template_opener(person: Person, cov: Coverage | None) -> str:
    if cov is None:
        return ""
    where = f" in {cov.outlet.name}" if cov.outlet is not None and not person.is_desk else ""
    if cov.kind == "premiere":
        return f"I caught your premiere of {cov.artist_name}'s new music{where} and loved it."
    return f"I really enjoyed your {KIND_NOUN.get(cov.kind or 'other', 'piece on')} {cov.artist_name}{where}."


def opener_for(person: Person, settings: AppSettings, cov: Coverage | None) -> str:
    if settings.opener_mode == "manual":
        return ""
    if settings.opener_mode == "llm" and person.opener:
        return person.opener
    return template_opener(person, cov)


def context(person: Person, settings: AppSettings) -> dict:
    cov = best_piece(person)
    outlet = person.outlets[0].name if person.outlets else ""
    return {
        "first_name": person.first_name or "there",
        "name": person.name,
        "outlet": outlet,
        "artist": cov.artist_name if cov else "",
        "piece_title": (cov.title or "") if cov else "",
        "piece_url": cov.url if cov else "",
        "opener": opener_for(person, settings, cov),
        "sender_name": settings.sender_name,
        "band_name": settings.band_name,
        "band_blurb": settings.band_blurb,
        "track_url": settings.track_url,
        "ask": settings.ask,
        "signature": settings.signature,
        "opt_out_line": settings.opt_out_line,
    }


def render(template: str, ctx: dict) -> str:
    text = template.format_map(_Blank(ctx))
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return re.sub(r" {2,}", " ", text).strip()


def render_subject(template: str, ctx: dict) -> str:
    subject = render(template, ctx)
    subject = re.sub(r"\s*(for fans of|:|-|–)\s*$", "", subject).strip()  # tidy when {artist} is empty
    return subject or ctx.get("band_name", "")


def render_pitch(person: Person, settings: AppSettings) -> tuple[str, str]:
    ctx = context(person, settings)
    return render_subject(settings.pitch_subject, ctx), render(settings.pitch_body, ctx)


def render_dm(person: Person, settings: AppSettings) -> str:
    ctx = context(person, settings)
    return render(settings.dm_template, ctx)
