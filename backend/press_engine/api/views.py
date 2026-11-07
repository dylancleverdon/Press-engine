"""Queries and serializers shared by the JSON API and the dashboard."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..appsettings import AppSettings
from ..contacts.resolve import verified_personal_email
from ..models import (
    OPEN_OUTREACH,
    Channel,
    DiscoveryLink,
    InboundMessage,
    MergeCandidate,
    Outreach,
    OutreachStatus,
    Person,
    PersonStatus,
)
from ..outreach.queue import email_target, sent_on
from ..outreach.schedule import cap_for_day
from ..util.timeutil import get_tz, to_local, utcnow
from ..util.urls import profile_url


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def person_brief(p: Person) -> dict:
    email = verified_personal_email(p)
    return {
        "id": p.id,
        "name": p.name,
        "kind": p.kind,
        "status": p.status,
        "taste_score": round(p.taste_score or 0, 3),
        "outlet": p.outlets[0].name if p.outlets else None,
        "email": email.value if email else None,
        "found_by": p.found_by.name if p.found_by else None,
        "opted_out": p.opted_out,
        "needs_review": p.needs_review,
    }


def person_detail(p: Person) -> dict:
    return {
        **person_brief(p),
        "bio": p.bio,
        "notes": p.notes,
        "language": p.language,
        "country": p.country,
        "followers": p.followers,
        "score_reason": p.score_reason,
        "taste_profile": p.taste_profile,
        "identities": [{"id": i.id, "type": i.type, "value": i.value, "display": i.display, "verified": i.verified,
                        "bounced": i.bounced, "source_url": i.source_url} for i in p.identities],
        "coverage": [{"artist": c.artist_name, "url": c.url, "title": c.title, "kind": c.kind,
                      "published_at": c.published_at.isoformat() if c.published_at else None} for c in p.coverage],
        "outreach": [outreach_dict(o, with_person=False) for o in p.outreach],
        "meetings": [{"id": m.id, "title": m.title, "starts_at": iso(m.starts_at), "ends_at": iso(m.ends_at),
                      "meet_url": m.meet_url, "html_link": m.html_link, "notes": m.notes} for m in p.meetings],
    }


def outreach_dict(o: Outreach, *, with_person: bool = True) -> dict:
    d = {
        "id": o.id,
        "channel": o.channel,
        "platform": o.platform,
        "to": o.to_address,
        "to_kind": o.to_kind,
        "status": o.status,
        "subject": o.subject,
        "body": o.body,
        "queue_date": o.queue_date.isoformat() if o.queue_date else None,
        "scheduled_at": iso(o.scheduled_at),
        "sent_at": iso(o.sent_at),
        "replied_at": iso(o.replied_at),
        "reply_snippet": o.reply_snippet,
        "sent_by": o.sent_by.name if o.sent_by else None,
        "error": o.error,
    }
    if with_person:
        d["person"] = person_brief(o.person)
        ident = next((i for i in o.person.identities if i.type == o.platform), None)
        d["profile_url"] = profile_url(ident.type, ident.value) if ident else None
    return d


def inbound_dict(m: InboundMessage) -> dict:
    return {
        "id": m.id,
        "kind": m.kind,
        "from": m.from_address,
        "from_name": m.from_name,
        "subject": m.subject,
        "snippet": m.snippet,
        "received_at": iso(m.received_at),
        "read": m.read,
        "opt_out": m.opt_out,
        "outreach_id": m.outreach_id,
        "person": person_brief(m.person) if m.person else None,
    }


def today_summary(session: Session, settings: AppSettings, svc, *, now: datetime | None = None) -> dict:
    now = now or utcnow()
    tz = get_tz(settings.timezone)
    today = to_local(now, tz).date()
    start_utc = now - timedelta(hours=24)
    emails = list(session.scalars(
        select(Outreach).options(selectinload(Outreach.person))
        .where(Outreach.channel == Channel.EMAIL,
               or_(Outreach.status.in_(OPEN_OUTREACH), Outreach.sent_at >= start_utc))
        .order_by(Outreach.scheduled_at.is_(None), Outreach.scheduled_at, Outreach.id)
    ))
    google = svc.google is not None and svc.google.connected()
    warnings = []
    if not google:
        warnings.append("Google isn't connected yet, so nothing can be sent. Run: press-engine google-auth")
    if svc.search is None:
        warnings.append("No web search API key, so press discovery is off (PRESS_BRAVE_API_KEY or PRESS_SERPAPI_API_KEY).")
    if svc.lastfm is None:
        warnings.append("No Last.fm key, so seed artists can't be expanded (PRESS_LASTFM_API_KEY).")
    if not settings.track_url:
        warnings.append("No track link set yet (Settings > Pitch).")
    if not settings.sound_profile_text:
        warnings.append("The band's sound profile is empty (Settings > Band). Scoring falls back to seed artists only.")
    if settings.sending_paused:
        warnings.append("Sending is paused.")
    return {
        "date": today.isoformat(),
        "timezone": settings.timezone,
        "cap": cap_for_day(session, settings, today, today=today),
        "sent_today": sent_on(session, today, tz),
        "awaiting_approval": [outreach_dict(o) for o in emails if o.status == OutreachStatus.QUEUED],
        "scheduled": [outreach_dict(o) for o in emails if o.status == OutreachStatus.APPROVED],
        "sent": [outreach_dict(o) for o in emails if o.sent_at and o.sent_at >= start_utc],
        "dm_queue": session.scalar(select(func.count(Outreach.id)).where(
            Outreach.channel == Channel.DM, Outreach.status == OutreachStatus.QUEUED)) or 0,
        "new_leads_24h": session.scalar(select(func.count(Person.id)).where(Person.created_at >= start_utc)) or 0,
        "waiting_for_review": session.scalar(select(func.count(Person.id)).where(Person.vetting == "pending")) or 0,
        "unread_replies": session.scalar(select(func.count(InboundMessage.id)).where(
            InboundMessage.kind == "reply", InboundMessage.read.is_(False))) or 0,
        "pending_merges": session.scalar(select(func.count(MergeCandidate.id)).where(MergeCandidate.status == "pending")) or 0,
        "google_connected": google,
        "sending_paused": settings.sending_paused,
        "warnings": warnings,
    }


def search_people(session: Session, *, q: str | None = None, status: str | None = None, kind: str | None = None,
                  needs_email: bool | None = None, limit: int = 50, offset: int = 0,
                  vetting: str | None = "approved") -> tuple[list[Person], int]:
    stmt = select(Person)
    if vetting:
        stmt = stmt.where(Person.vetting == vetting)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Person.name.ilike(like), Person.notes.ilike(like), Person.bio.ilike(like)))
    if status:
        stmt = stmt.where(Person.status == status)
    if kind:
        stmt = stmt.where(Person.kind == kind)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(session.scalars(
        stmt.options(selectinload(Person.identities), selectinload(Person.outlet_links))
        .order_by(Person.taste_score.desc(), Person.id.desc()).limit(limit).offset(offset)
    ))
    return rows, total


def dm_queue(session: Session) -> list[Outreach]:
    return list(session.scalars(
        select(Outreach).where(Outreach.channel == Channel.DM, Outreach.status == OutreachStatus.QUEUED)
        .order_by(Outreach.queue_date, Outreach.id)
    ))


def sound_pages(session: Session, limit: int = 30) -> list[DiscoveryLink]:
    return list(session.scalars(
        select(DiscoveryLink).where(DiscoveryLink.reviewed.is_(False)).order_by(DiscoveryLink.found_at.desc()).limit(limit)
    ))


RUNNING: dict[str, int] = {}  # jobs started from the dashboard that haven't finished yet
_running_lock = threading.Lock()


def run_in_background(db, svc, job: str, **kwargs) -> None:
    from ..jobs import run_job

    with _running_lock:
        RUNNING[job] = RUNNING.get(job, 0) + 1

    def go() -> None:
        try:
            run_job(db, svc, job, **kwargs)
        finally:
            with _running_lock:
                RUNNING[job] -= 1
                if RUNNING[job] <= 0:
                    del RUNNING[job]

    threading.Thread(target=go, daemon=True, name=f"job-{job}").start()


def running_jobs() -> list[str]:
    with _running_lock:
        return sorted(RUNNING)


def contact_line(person: Person, settings: AppSettings) -> str:
    target = email_target(person, settings)
    if target:
        return f"{target.address}" + (" (outlet inbox)" if target.kind == "outlet_inbox" else "")
    return "no email: DM queue" if any(i.type in ("instagram", "tiktok", "x") for i in person.identities) else "no contact yet"


STATUSES = [s.value for s in PersonStatus]
