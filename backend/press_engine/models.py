"""Data model (spec §5)."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .util.timeutil import utcnow


class PersonKind(StrEnum):
    PRESS = "press"
    CREATOR = "creator"
    CURATOR = "curator"


class PersonStatus(StrEnum):
    NEW = "new"
    QUEUED = "queued"
    PITCHED = "pitched"
    REPLIED = "replied"
    IN_CONVERSATION = "in_conversation"
    COVERED = "covered"
    NOT_INTERESTED = "not_interested"


STATUS_RANK = {s: i for i, s in enumerate(PersonStatus)}


class IdentityType(StrEnum):
    EMAIL = "email"
    YOUTUBE = "youtube"
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    X = "x"
    BLUESKY = "bluesky"
    THREADS = "threads"
    FACEBOOK = "facebook"
    SOUNDCLOUD = "soundcloud"
    SPOTIFY = "spotify"
    SITE = "site"
    LINKINBIO = "linkinbio"
    OTHER = "other"


# Platforms where a human can send a DM from the app's DM queue.
DM_PLATFORMS = (IdentityType.INSTAGRAM, IdentityType.TIKTOK, IdentityType.X, IdentityType.BLUESKY, IdentityType.THREADS)


class Channel(StrEnum):
    EMAIL = "email"
    DM = "dm"


class OutreachStatus(StrEnum):
    QUEUED = "queued"  # waiting for D's approval (or, for DMs, waiting to be sent by hand)
    APPROVED = "approved"  # will go out at scheduled_at
    SENT = "sent"
    REPLIED = "replied"
    BOUNCED = "bounced"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


OPEN_OUTREACH = (OutreachStatus.QUEUED, OutreachStatus.APPROVED)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(20), default="member")  # owner | member
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    notify_replies: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    devices: Mapped[list[Device]] = relationship(back_populates="user", cascade="all, delete-orphan")

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expo_push_token: Mapped[str] = mapped_column(String(255), unique=True)
    platform: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="devices")


class Outlet(Base):
    __tablename__ = "outlets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    country: Mapped[str | None] = mapped_column(String(8))
    language: Mapped[str | None] = mapped_column(String(16))
    contact_email: Mapped[str | None] = mapped_column(String(320))  # generic inbox (editor@, redaktion@ ...)
    contact_email_source: Mapped[str | None] = mapped_column(Text)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    person_links: Mapped[list[PersonOutlet]] = relationship(back_populates="outlet", cascade="all, delete-orphan")


class Person(Base):
    __tablename__ = "people"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(20), default=PersonKind.PRESS, index=True)
    status: Mapped[str] = mapped_column(String(30), default=PersonStatus.NEW, index=True)
    # A "desk" is an outlet without a named writer (one-person blogs, staff reviews).
    is_desk: Mapped[bool] = mapped_column(Boolean, default=False)
    language: Mapped[str | None] = mapped_column(String(16))
    country: Mapped[str | None] = mapped_column(String(8))
    bio: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    followers: Mapped[int | None] = mapped_column(Integer)

    heuristic_score: Mapped[float] = mapped_column(Float, default=0.0)
    llm_score: Mapped[float | None] = mapped_column(Float)
    taste_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    score_reason: Mapped[str | None] = mapped_column(Text)
    taste_profile: Mapped[dict | None] = mapped_column(JSON)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime)
    llm_scored_at: Mapped[datetime | None] = mapped_column(DateTime)
    opener: Mapped[str | None] = mapped_column(Text)
    opener_source_url: Mapped[str | None] = mapped_column(Text)

    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    opted_out_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime)

    found_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    found_via: Mapped[str | None] = mapped_column(String(40))
    # The soundalike whose search turned this contact up (for fit tracking).
    discovered_artist_id: Mapped[int | None] = mapped_column(ForeignKey("artists.id", ondelete="SET NULL"), index=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reason: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    identities: Mapped[list[Identity]] = relationship(back_populates="person", cascade="all, delete-orphan")
    coverage: Mapped[list[Coverage]] = relationship(back_populates="person", cascade="all, delete-orphan")
    outlet_links: Mapped[list[PersonOutlet]] = relationship(back_populates="person", cascade="all, delete-orphan")
    outreach: Mapped[list[Outreach]] = relationship(
        back_populates="person", cascade="all, delete-orphan", order_by="Outreach.id"
    )
    meetings: Mapped[list[Meeting]] = relationship(back_populates="person", cascade="all, delete-orphan")
    found_by: Mapped[User | None] = relationship()

    @property
    def outlets(self) -> list[Outlet]:
        return [link.outlet for link in self.outlet_links]

    def identities_of(self, *types: str) -> list[Identity]:
        return [i for i in self.identities if i.type in types]

    @property
    def first_name(self) -> str | None:
        """How to greet them: a writer's first name; a creator's display name or handle."""
        if self.is_desk or not self.name:
            return None
        if self.kind != PersonKind.PRESS:
            return self.name.lstrip("@")
        return self.name.split()[0]


class PersonOutlet(Base):
    __tablename__ = "person_outlets"

    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), primary_key=True)
    outlet_id: Mapped[int] = mapped_column(ForeignKey("outlets.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str | None] = mapped_column(String(80))

    person: Mapped[Person] = relationship(back_populates="outlet_links")
    outlet: Mapped[Outlet] = relationship(back_populates="person_links")


class Identity(Base):
    __tablename__ = "identities"
    __table_args__ = (UniqueConstraint("type", "value", name="uq_identity_type_value"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(20))
    value: Mapped[str] = mapped_column(String(512))  # normalized key
    display: Mapped[str | None] = mapped_column(String(512))  # as found (URL, @Handle ...)
    source_url: Mapped[str | None] = mapped_column(Text)
    # For emails: True only if the address was found verbatim on a fetched page,
    # in API data, or typed in by a human. Unverified emails are never sent to.
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    bounced: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    person: Mapped[Person] = relationship(back_populates="identities")


class Artist(Base):
    """Seed artists (manual) and the similar artists they expand into (spec §4.1)."""

    __tablename__ = "artists"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    name_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # manual | expanded
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    similarity: Mapped[float] = mapped_column(Float, default=1.0)  # seeds = 1.0
    mbid: Mapped[str | None] = mapped_column(String(64))
    lastfm_url: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list | None] = mapped_column(JSON)
    expanded_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_press_search_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_youtube_search_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_tiktok_search_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_playlist_search_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # Soundalike review: "auto" (confident match, used right away), "suggested" (waiting
    # for D), "accepted", "rejected". Only auto/accepted artists are searched.
    review: Mapped[str] = mapped_column(String(20), default="auto")
    suggested_reason: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict | None] = mapped_column(JSON)
    # How well contacts found through this artist respond (see soundalikes.update_fit).
    fit_verdict: Mapped[str] = mapped_column(String(20), default="learning")  # learning | strong | ok | weak
    fit_factor: Mapped[float] = mapped_column(Float, default=1.0)
    fit_stats: Mapped[dict | None] = mapped_column(JSON)

    @property
    def is_seed(self) -> bool:
        return self.source == "manual"

    @property
    def searchable(self) -> bool:
        return self.active and self.review in ("auto", "accepted") and (self.is_seed or self.similarity > 0)


class ArtistSimilarity(Base):
    __tablename__ = "artist_similarity"

    seed_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"), primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"), primary_key=True)
    match: Mapped[float] = mapped_column(Float)


class Coverage(Base):
    __tablename__ = "coverage"
    __table_args__ = (UniqueConstraint("person_id", "url", "artist_name", name="uq_coverage"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    outlet_id: Mapped[int | None] = mapped_column(ForeignKey("outlets.id", ondelete="SET NULL"))
    artist_id: Mapped[int | None] = mapped_column(ForeignKey("artists.id", ondelete="SET NULL"))
    artist_name: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str | None] = mapped_column(String(30))  # review, interview, premiere, feature, video, playlist, tiktok...
    published_at: Mapped[date | None] = mapped_column(Date)
    language: Mapped[str | None] = mapped_column(String(16))
    summary: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    person: Mapped[Person] = relationship(back_populates="coverage")
    outlet: Mapped[Outlet | None] = relationship()
    artist: Mapped[Artist | None] = relationship()


class Page(Base):
    """Cache of every fetched page; also the evidence behind each verified email."""

    __tablename__ = "pages"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(2048), unique=True, index=True)
    final_url: Mapped[str | None] = mapped_column(Text)
    status_code: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    error: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    site_name: Mapped[str | None] = mapped_column(String(255))
    author: Mapped[str | None] = mapped_column(String(512))
    published: Mapped[str | None] = mapped_column(String(32))
    language: Mapped[str | None] = mapped_column(String(16))
    text: Mapped[str | None] = mapped_column(Text)
    byline: Mapped[str | None] = mapped_column(Text)
    emails: Mapped[list | None] = mapped_column(JSON)  # verbatim (incl. Cloudflare-decoded)
    unverified_emails: Mapped[list | None] = mapped_column(JSON)  # "name [at] site [dot] com"
    links: Mapped[list | None] = mapped_column(JSON)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime)
    extraction: Mapped[dict | None] = mapped_column(JSON)
    relevant: Mapped[bool | None] = mapped_column(Boolean)

    @property
    def ok(self) -> bool:
        return self.error is None and self.status_code is not None and self.status_code < 400


class SearchRun(Base):
    __tablename__ = "search_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    engine: Mapped[str] = mapped_column(String(30))  # press | tiktok | youtube | spotify
    provider: Mapped[str] = mapped_column(String(30))
    query: Mapped[str] = mapped_column(Text)
    artist_id: Mapped[int | None] = mapped_column(ForeignKey("artists.id", ondelete="SET NULL"))
    run_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class DiscoveryLink(Base):
    """Things worth a manual look, e.g. TikTok sound pages to browse with the extension."""

    __tablename__ = "discovery_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(2048), unique=True)
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str | None] = mapped_column(Text)
    artist_name: Mapped[str | None] = mapped_column(String(255))
    found_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)


class Outreach(Base):
    __tablename__ = "outreach"
    __table_args__ = (Index("ix_outreach_status_sched", "status", "scheduled_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(10))
    platform: Mapped[str | None] = mapped_column(String(20))  # DM platform
    to_address: Mapped[str | None] = mapped_column(String(320))
    to_kind: Mapped[str | None] = mapped_column(String(20))  # personal | outlet_inbox
    status: Mapped[str] = mapped_column(String(20), default=OutreachStatus.QUEUED)
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    queue_date: Mapped[date | None] = mapped_column(Date, index=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    gmail_message_id: Mapped[str | None] = mapped_column(String(64))
    gmail_thread_id: Mapped[str | None] = mapped_column(String(64), index=True)
    rfc_message_id: Mapped[str | None] = mapped_column(String(255))
    replied_at: Mapped[datetime | None] = mapped_column(DateTime)
    reply_snippet: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    sent_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    person: Mapped[Person] = relationship(back_populates="outreach")
    sent_by: Mapped[User | None] = relationship(foreign_keys=[sent_by_user_id])


class InboundMessage(Base):
    """Replies, auto-replies and bounces detected on pitched threads (spec §4.9)."""

    __tablename__ = "inbound_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    gmail_message_id: Mapped[str] = mapped_column(String(64), unique=True)
    gmail_thread_id: Mapped[str | None] = mapped_column(String(64), index=True)
    outreach_id: Mapped[int | None] = mapped_column(ForeignKey("outreach.id", ondelete="SET NULL"), index=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"), index=True)
    from_address: Mapped[str | None] = mapped_column(String(320))
    from_name: Mapped[str | None] = mapped_column(String(255))
    subject: Mapped[str | None] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20))  # reply | auto_reply | bounce
    opt_out: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    person: Mapped[Person | None] = relationship()
    outreach: Mapped[Outreach | None] = relationship()


class OptOut(Base):
    """Permanent suppression list. Survives person deletion and re-discovery."""

    __tablename__ = "opt_outs"
    __table_args__ = (UniqueConstraint("kind", "value", name="uq_optout"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # email | tiktok | instagram | ...
    value: Mapped[str] = mapped_column(String(512))
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"))
    reason: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(20))  # reply | manual | bounce
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MergeCandidate(Base):
    __tablename__ = "merge_candidates"
    __table_args__ = (UniqueConstraint("person_a_id", "person_b_id", name="uq_merge_pair"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    person_a_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"))
    person_b_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"))
    score: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | merged | rejected
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    person_a: Mapped[Person] = relationship(foreign_keys=[person_a_id])
    person_b: Mapped[Person] = relationship(foreign_keys=[person_b_id])


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    calendar_event_id: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255))
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    location: Mapped[str | None] = mapped_column(Text)
    meet_url: Mapped[str | None] = mapped_column(Text)
    html_link: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    person: Mapped[Person] = relationship(back_populates="meetings")


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (Index("ix_job_runs_job_started", "job", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(40))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    ok: Mapped[bool | None] = mapped_column(Boolean)
    stats: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)


class Suggestion(Base):
    """Suggestions box. Mirrored to the Google Sheet, where they're reviewed."""

    __tablename__ = "suggestions"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    area: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30), default="new")
    notes: Mapped[str | None] = mapped_column(Text)
    adjustments: Mapped[str | None] = mapped_column(Text)
    submitted_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime)  # None = not in the sheet yet / local edits pending
    dirty: Mapped[bool] = mapped_column(Boolean, default=True)

    submitted_by: Mapped[User | None] = relationship()


class SettingRow(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
