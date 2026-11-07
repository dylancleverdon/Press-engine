"""Runtime settings D edits from the dashboard, stored as one JSON row.

Defaults below are deliberately conservative; the open questions in the spec
(§8: sound profile, pitch template, sending hours...) are all settings here.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from .models import SettingRow
from .util.timeutil import parse_hhmm, system_timezone_name

SETTINGS_KEY = "app"

DEFAULT_SUBJECT = "{band_name}: new music for fans of {artist}"

DEFAULT_BODY = """Hi {first_name},

{opener}

I'm {sender_name} from {band_name}. {band_blurb}

Here's our new track: {track_url}

{ask}

Thanks for listening,
{signature}

{opt_out_line}"""

DEFAULT_DM = (
    "Hey {first_name}! {opener} I'm {sender_name} from {band_name}. {band_blurb} "
    "Would you be up for giving our new track a listen? {track_url}"
)

PLACEHOLDERS = {
    "first_name": "Contact's first name, or 'there'",
    "name": "Contact's full name (or outlet name for desks)",
    "outlet": "Outlet name, if any",
    "artist": "The most relevant seed/similar artist they covered",
    "piece_title": "Title of that piece",
    "piece_url": "URL of that piece",
    "opener": "Personalised opening line",
    "sender_name": "Your name",
    "band_name": "Band name",
    "band_blurb": "One or two sentences about the band",
    "track_url": "Link to the track",
    "ask": "The one simple ask",
    "signature": "Signature block",
    "opt_out_line": "Opt-out line",
}

DEFAULT_PRESS_QUERIES: dict[str, list[str]] = {
    "en": ['"{artist}" review', '"{artist}" interview', '"{artist}" premiere', '"{artist}" album review'],
    "da": ['"{artist}" anmeldelse', '"{artist}" interview'],
    "de": ['"{artist}" Rezension', '"{artist}" Kritik', '"{artist}" Interview'],
    "nl": ['"{artist}" recensie'],
    "sv": ['"{artist}" recension'],
    "no": ['"{artist}" anmeldelse'],
    "fr": ['"{artist}" chronique'],
    "it": ['"{artist}" recensione'],
    "es": ['"{artist}" reseña'],
    "ja": ['"{artist}" レビュー', '"{artist}" インタビュー'],
}

DEFAULT_BLOCKED_DOMAINS = [
    "wikipedia.org", "discogs.com", "allmusic.com", "last.fm", "musicbrainz.org", "rateyourmusic.com",
    "spotify.com", "apple.com", "music.apple.com", "deezer.com", "tidal.com", "bandcamp.com",
    "soundcloud.com", "youtube.com", "youtu.be", "tiktok.com", "instagram.com", "facebook.com",
    "twitter.com", "x.com", "reddit.com", "pinterest.com", "threads.net", "bsky.app",
    "amazon.com", "amazon.de", "amazon.co.uk", "ebay.com", "songkick.com", "bandsintown.com",
    "ticketmaster.com", "eventbrite.com", "genius.com", "azlyrics.com", "shazam.com",
    "linktr.ee", "setlist.fm", "imdb.com", "qobuz.com", "napster.com", "jpc.de", "cdon.com",
]

LANG_CODE = re.compile(r"^[a-z]{2,3}$")


class AppSettings(BaseModel):
    # --- Band / pitch (spec §4.8, §8) ---
    band_name: str = "Our Band"
    band_blurb: str = "We make music for fans of the artists you write about."
    band_sound_profile: str = ""
    sender_name: str = "D"
    signature: str = "D"
    track_url: str = ""
    ask: str = "Would you be up for a listen, and maybe a review or a feature if it's your thing?"
    opt_out_line: str = 'P.S. Not your thing? Just reply "unsubscribe" and I won\'t email you again.'
    pitch_subject: str = DEFAULT_SUBJECT
    pitch_body: str = DEFAULT_BODY
    dm_template: str = DEFAULT_DM
    # template | llm | manual (manual = leave blank, D must write it before approving)
    opener_mode: Literal["template", "llm", "manual"] = "template"

    # --- Sending (spec §1, §4.8) ---
    timezone: str = Field(default_factory=system_timezone_name)
    timezone_mode: Literal["sender", "recipient"] = "sender"
    send_window_start: str = "09:30"
    send_window_end: str = "16:30"
    send_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])  # Mon=0
    daily_email_target: int = 20
    warmup_start: int = 5
    warmup_days: int = 14
    min_gap_minutes: int = 8
    require_approval: bool = True
    allow_outlet_inbox: bool = True
    daily_dm_target: int = 10
    sending_paused: bool = False
    auto_update: bool = True  # Mac app: install new releases automatically

    # --- Dedupe / cooldowns (spec §4.6) ---
    cooldown_days: int = 30
    outlet_cooldown_days: int = 30

    # --- Discovery (spec §1, §4.2-4.3) ---
    daily_new_contacts_target: int = 20
    search_daily_budget: int = 60
    youtube_daily_searches: int = 30
    tiktok_daily_searches: int = 10
    playlist_daily_searches: int = 10
    max_pages_per_run: int = 150
    press_languages: list[str] = Field(default_factory=lambda: ["en", "da", "de", "nl", "sv", "ja"])
    press_queries: dict[str, list[str]] = Field(default_factory=lambda: dict(DEFAULT_PRESS_QUERIES))
    blocked_domains: list[str] = Field(default_factory=lambda: list(DEFAULT_BLOCKED_DOMAINS))
    similar_per_seed: int = 50
    # Soundalikes (Seed artists page)
    auto_accept_similarity: float = 0.5  # Last.fm match at/above which a soundalike is used without review
    second_degree_per_run: int = 5  # soundalikes of soundalikes looked up per discovery run
    cocoverage_min_contacts: int = 2  # suggest artists that this many of your contacts also covered
    fit_min_pitches: int = 8  # pitches before judging a soundalike by its reply rate
    # Google Sheet the suggestions box (and a soundalike report) syncs to
    suggestions_sheet_id: str = "1C-Y3u0ixkD8KX19ns70GC3XA4tpZ1YvdF-HPskqIOTM"
    search_freshness: Literal["month", "year", "any"] = "year"

    # --- Scoring (spec §4.7) ---
    min_score_to_queue: float = 0.25
    llm_score_top_n: int = 40

    # --- Schedule (local time in `timezone`) ---
    discovery_time: str = "03:00"
    queue_time: str = "07:30"
    reply_poll_minutes: int = 5

    @field_validator("send_window_start", "send_window_end", "discovery_time", "queue_time")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        parse_hhmm(v)
        return v

    @field_validator("send_days")
    @classmethod
    def _days(cls, v: list[int]) -> list[int]:
        return sorted({d for d in v if 0 <= d <= 6})

    @field_validator("press_languages")
    @classmethod
    def _langs(cls, v: list[str]) -> list[str]:
        return [x.strip().lower() for x in v if LANG_CODE.match(x.strip().lower())]

    @field_validator("pitch_subject", "pitch_body", "dm_template")
    @classmethod
    def _placeholders(cls, v: str) -> str:
        unknown = set(re.findall(r"{(\w+)}", v)) - set(PLACEHOLDERS)
        if unknown:
            raise ValueError(f"Unknown placeholder(s): {', '.join(sorted(unknown))}")
        return v

    @property
    def sound_profile_text(self) -> str:
        return self.band_sound_profile.strip()


def load_settings(session: Session) -> AppSettings:
    row = session.get(SettingRow, SETTINGS_KEY)
    if row is None:
        return AppSettings()
    return AppSettings.model_validate(row.value)


def save_settings(session: Session, settings: AppSettings) -> AppSettings:
    settings = AppSettings.model_validate(settings.model_dump())  # re-validate
    row = session.get(SettingRow, SETTINGS_KEY)
    data = settings.model_dump(mode="json")
    if row is None:
        session.add(SettingRow(key=SETTINGS_KEY, value=data))
    else:
        row.value = data
    session.flush()
    return settings


def update_settings(session: Session, **changes) -> AppSettings:
    current = load_settings(session).model_dump()
    current.update(changes)
    return save_settings(session, AppSettings.model_validate(current))
