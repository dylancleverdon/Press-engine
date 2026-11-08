"""The discovery engines, as the Discovery pages show them, and vetting of what they find.

Each engine turns soundalikes into contacts. New contacts an engine finds wait as
"pending" until D approves them (or the engine's auto-approve is on). Pending and
rejected contacts are never scored into the queue or pitched; rejected ones stay
in the database, hidden, so a later run recognises them and doesn't add them again.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..appsettings import AppSettings
from ..models import Person, PersonStatus, SearchRun

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"
VETTING = (PENDING, APPROVED, REJECTED)


@dataclass(frozen=True)
class Engine:
    key: str
    label: str
    blurb: str
    found_via: tuple[str, ...]  # Person.found_via values this engine writes
    search_engine: str | None = None  # SearchRun.engine
    runnable: bool = True  # has its own "Run now" (captures don't)
    settings: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # (AppSettings field, label)
    coming_soon: str | None = None  # placeholder engine: what it will do

    @property
    def auto_field(self) -> str | None:
        return f"{self.key}_auto_approve" if self.runnable and not self.coming_soon else None

    def auto_approve(self, settings: AppSettings) -> bool:
        return bool(self.auto_field and getattr(settings, self.auto_field))


ENGINES: dict[str, Engine] = {e.key: e for e in [
    Engine("youtube", "YouTube", "Reviewers, reaction channels and mixes that feature your soundalikes.",
           ("youtube",), "youtube",
           settings=(("youtube_daily_searches", "Searches per day (each uses 100 of the 10,000 free daily units)"),
                     ("youtube_min_subscribers", "Skip channels with fewer subscribers than"),
                     ("youtube_max_subscribers", "Skip channels with more subscribers than (0 = no limit)"),
                     ("youtube_skip_videos", "Skip concert recordings, type beats, lyric videos and the like"),
                     ("youtube_skip_words", "Skip videos whose titles contain (one per line)"))),
    Engine("tiktok", "TikTok", "Creators posting about your soundalikes, found through web search (TikTok itself is never scraped).",
           ("tiktok_search",), "tiktok",
           settings=(("tiktok_daily_searches", "Web searches per day"),)),
    Engine("press", "Press", "Music journalists and blogs who review or interview your soundalikes.",
           ("press_search",), "press",
           settings=(("search_daily_budget", "Web searches per day"),
                     ("daily_new_contacts_target", "Stop after this many new contacts a day (0 = no limit)"),
                     ("max_pages_per_run", "Max pages fetched per run"),
                     ("search_freshness", "Search freshness"),
                     ("press_languages", "Languages to search (ISO codes, one per line)"),
                     ("press_queries", "Search templates (lang: template, one per line)"))),
    Engine("playlists", "Spotify playlists", "Curators of playlists that include your soundalikes.",
           ("spotify_playlist",), "spotify",
           settings=(("playlist_daily_searches", "Playlist searches per day"),)),
    Engine("captures", "Instagram & captures", "Profiles you saved with the Chrome extension or the phone share sheet. "
           "Instagram can't be searched automatically, so this is how Instagram creators get in.",
           ("capture_extension", "capture_share", "capture_mobile", "capture_manual"), runnable=False),
    Engine("twitch", "Twitch", "Streamers who play, react to or talk about music like yours.", ("twitch",), runnable=False,
           coming_soon="Twitch can't be searched like YouTube: its search only matches channel names, and only "
           "live streams have titles. The plan is to combine (1) checking who's live in the Music and "
           "Just Chatting categories every couple of hours and keeping streams whose titles or tags mention "
           "your soundalikes or genres, (2) web search for Twitch channels that mention your soundalikes, and "
           "(3) Twitch links found on creators' YouTube channels and link pages, which Press Engine already "
           "saves. It will need a free Twitch developer app (Client ID and Secret) on Connections."),
]}


def engine_for(person: Person) -> Engine | None:
    via = person.found_via or ""
    for e in ENGINES.values():
        if via in e.found_via or (e.key == "captures" and via.startswith("capture_")):
            return e
    return None


def max_person_id(session: Session) -> int:
    return session.scalar(select(func.max(Person.id))) or 0


def apply_vetting(session: Session, settings: AppSettings, engine: Engine, *, after_id: int) -> int:
    """New contacts this engine just created wait for review unless its auto-approve is on."""
    if engine.auto_approve(settings):
        return 0
    rows = list(session.scalars(select(Person).where(
        Person.id > after_id, Person.found_via.in_(engine.found_via), Person.vetting == APPROVED,
        Person.status == PersonStatus.NEW)))
    for p in rows:
        p.vetting = PENDING
    return len(rows)


def approve(person: Person) -> None:
    person.vetting = APPROVED


def reject(person: Person) -> None:
    person.vetting = REJECTED


def unreview(person: Person) -> None:
    person.vetting = PENDING


def counts(session: Session, engine: Engine) -> dict[str, int]:
    out = {v: 0 for v in VETTING}
    for vetting, n in session.execute(select(Person.vetting, func.count(Person.id))
                                      .where(_found_by(engine)).group_by(Person.vetting)):
        out[vetting] = n
    out["total"] = sum(out[v] for v in VETTING)
    return out


def _found_by(engine: Engine):
    if engine.key == "captures":
        return Person.found_via.like("capture\\_%", escape="\\")
    return Person.found_via.in_(engine.found_via)


def people(session: Session, engine: Engine, vetting: str, *, limit: int = 100) -> list[Person]:
    return list(session.scalars(select(Person).where(_found_by(engine), Person.vetting == vetting)
                                .order_by(Person.taste_score.desc(), Person.followers.desc().nulls_last(), Person.id.desc())
                                .limit(limit)))


def recent_searches(session: Session, engine: Engine, *, limit: int = 30) -> list[SearchRun]:
    if not engine.search_engine:
        return []
    return list(session.scalars(select(SearchRun).where(SearchRun.engine == engine.search_engine)
                                .order_by(SearchRun.run_at.desc(), SearchRun.id.desc()).limit(limit)))


# --- YouTube clean-up -----------------------------------------------------------------------

def merged_channels(person: Person) -> int:
    """How many distinct YouTube channel IDs this contact holds (more than one usually
    means separate channels were merged by mistake)."""
    return sum(1 for i in person.identities if i.type == "youtube" and i.value.startswith("UC"))


def recheck_youtube(session: Session, settings: AppSettings) -> dict:
    """Apply the current video rules to YouTube finds still waiting to be vetted:
    drop videos that don't count, and move finds with nothing left to Rejected."""
    from .youtube import skip_reason

    stats = {"checked": 0, "videos_dropped": 0, "rejected": 0, "merged_flagged": 0}
    for p in people(session, ENGINES["youtube"], PENDING, limit=10_000):
        stats["checked"] += 1
        for c in list(p.coverage):
            if c.source == "youtube" and skip_reason(c.title or "", c.summary or "", c.artist_name, settings):
                session.delete(c)
                p.coverage.remove(c)
                stats["videos_dropped"] += 1
        if not any(c.source == "youtube" for c in p.coverage):
            p.vetting = REJECTED
            stats["rejected"] += 1
        elif merged_channels(p) > 1 and not p.needs_review:
            p.needs_review = True
            p.review_reason = "Holds several YouTube channels, probably merged by mistake. Check before approving."
            stats["merged_flagged"] += 1
    session.flush()
    return stats
