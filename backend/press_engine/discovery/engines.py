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
from collections import Counter

from ..models import DM_PLATFORMS, IdentityType, Person, PersonStatus, SearchRun

PENDING, APPROVED, REJECTED, FILTERED = "pending", "approved", "rejected", "filtered"
VETTING = (PENDING, APPROVED, REJECTED, FILTERED)
# How a find can be reached: an email, a DM platform only, or nothing found yet.
REACH = ("email", "dm", "none")


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


DECISIONS = {"approve": APPROVED, "reject": REJECTED, "pending": PENDING, "filtered": FILTERED}


def decide(person: Person, decision: str) -> None:
    state = DECISIONS[decision]
    person.vetting = state
    if state != FILTERED:
        person.vetting_reason = None


def approve(person: Person) -> None:
    decide(person, "approve")


def reject(person: Person) -> None:
    decide(person, "reject")


def unreview(person: Person) -> None:
    decide(person, "pending")


def reach(person: Person) -> str:
    """"email", "dm" (a DM platform but no email) or "none"."""
    types = {i.type for i in person.identities if not (i.type == IdentityType.EMAIL and i.bounced)}
    if IdentityType.EMAIL in types:
        return "email"
    if types & {str(t) for t in DM_PLATFORMS}:
        return "dm"
    return "none"


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


def people(session: Session, engine: Engine, vetting: str, *, limit: int | None = 100, how: str | None = None) -> list[Person]:
    rows = session.scalars(select(Person).where(_found_by(engine), Person.vetting == vetting)
                           .order_by(Person.taste_score.desc(), Person.followers.desc().nulls_last(), Person.id.desc()))
    out = [p for p in rows if how is None or reach(p) == how]
    return out[:limit] if limit else out


def reach_counts(session: Session, engine: Engine, vetting: str) -> dict[str, int]:
    c = Counter(reach(p) for p in people(session, engine, vetting, limit=None))
    return {r: c.get(r, 0) for r in REACH}


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


def youtube_rule_problem(person: Person, settings: AppSettings) -> str | None:
    """Why a YouTube find breaks the current rules (after its videos were checked), or None."""
    subs = person.followers
    if subs is not None and subs < settings.youtube_min_subscribers:
        return f"{subs:,} subscribers, under your minimum of {settings.youtube_min_subscribers:,}"
    if subs is not None and settings.youtube_max_subscribers and subs > settings.youtube_max_subscribers:
        return f"{subs:,} subscribers, over your maximum of {settings.youtube_max_subscribers:,}"
    if not any(c.source == "youtube" for c in person.coverage):
        return "no videos that count"
    return None


def apply_youtube_rules(session: Session, settings: AppSettings) -> dict:
    """Apply the current YouTube rules to finds waiting to be vetted (and to ones the rules
    filtered out before, which come back if they now pass). Uses no searches.

    Videos that don't count are removed; finds left breaking a rule move to the
    "Filtered out" tab with the reason. Nothing you approved or rejected is touched."""
    from .youtube import skip_reason

    yt = ENGINES["youtube"]
    stats: dict = {"checked": 0, "videos_dropped": 0, "filtered": 0, "restored": 0, "merged_flagged": 0}
    reasons: Counter = Counter()
    for p in people(session, yt, PENDING, limit=None) + people(session, yt, FILTERED, limit=None):
        stats["checked"] += 1
        dropped_why = []
        for c in list(p.coverage):
            if c.source != "youtube":
                continue
            why = skip_reason(c.title or "", c.summary or "", c.artist_name, settings)
            if why:
                dropped_why.append(why)
                session.delete(c)
                p.coverage.remove(c)
                stats["videos_dropped"] += 1
        problem = youtube_rule_problem(p, settings)
        if problem == "no videos that count" and dropped_why:
            problem = f"no videos that count ({'; '.join(dict.fromkeys(dropped_why))})"
        if problem:
            if p.vetting != FILTERED:
                stats["filtered"] += 1
                reasons[_reason_group(problem)] += 1
            p.vetting, p.vetting_reason = FILTERED, problem
        elif p.vetting == FILTERED:
            p.vetting, p.vetting_reason = PENDING, None
            stats["restored"] += 1
        if p.vetting == PENDING and merged_channels(p) > 1 and not p.needs_review:
            p.needs_review = True
            p.review_reason = "Holds several YouTube channels, probably merged by mistake. Check before approving."
            stats["merged_flagged"] += 1
    stats["reasons"] = dict(reasons)
    session.flush()
    return stats


def _reason_group(problem: str) -> str:
    if "under your minimum" in problem:
        return "under the subscriber minimum"
    if "over your maximum" in problem:
        return "over the subscriber maximum"
    if "doesn't mention" in problem and "title has" not in problem:
        return "videos don't mention the artist"
    if "title has" in problem:
        return "concert recordings, type beats and similar"
    return "no videos that count"


def describe_rules_result(st: dict) -> str:
    if not st.get("checked"):
        return "Nothing waiting to be vetted."
    parts = [f"Checked {st['checked']} finds"]
    if st["filtered"]:
        why = ", ".join(f"{n} {r}" for r, n in sorted(st["reasons"].items(), key=lambda x: -x[1]))
        parts.append(f"{st['filtered']} moved to Filtered out ({why})")
    if st["restored"]:
        parts.append(f"{st['restored']} back in To vet (they pass the rules now)")
    if st["merged_flagged"]:
        parts.append(f"{st['merged_flagged']} flagged as several channels merged")
    if len(parts) == 1:
        parts.append("everything follows the rules")
    return ": ".join([parts[0], "; ".join(parts[1:])]) + "."
