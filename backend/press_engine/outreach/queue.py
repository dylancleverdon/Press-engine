"""Daily queue (spec §4.7-4.8): pick today's best contacts for email and DMs."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..appsettings import AppSettings
from ..contacts.dedupe import pending_partner_ids
from ..models import (
    DM_PLATFORMS,
    OPEN_OUTREACH,
    Channel,
    Identity,
    IdentityType,
    OptOut,
    Outlet,
    Outreach,
    OutreachStatus,
    Person,
    PersonStatus,
)
from ..util.emails import normalize_email
from ..util.timeutil import get_tz, to_local
from .schedule import cap_for_day, schedule_approved
from .templates import render_dm, render_pitch

DM_PREFERENCE = [IdentityType.INSTAGRAM, IdentityType.TIKTOK, IdentityType.X, IdentityType.BLUESKY, IdentityType.THREADS]


@dataclass
class EmailTarget:
    address: str
    kind: str  # personal | outlet_inbox
    outlet: Outlet | None = None


def email_target(person: Person, settings: AppSettings) -> EmailTarget | None:
    for ident in person.identities:
        if ident.type == IdentityType.EMAIL and ident.verified and not ident.bounced:
            return EmailTarget(ident.value, "personal")
    if settings.allow_outlet_inbox:
        for outlet in person.outlets:
            if outlet.contact_email:
                return EmailTarget(outlet.contact_email, "outlet_inbox", outlet)
    return None


def dm_target(person: Person) -> Identity | None:
    for platform in DM_PREFERENCE:
        for ident in person.identities:
            if ident.type == platform:
                return ident
    return None


def is_suppressed(session: Session, kind: str, value: str) -> bool:
    value = normalize_email(value) if kind == "email" else value.lower().lstrip("@")
    return session.scalar(select(OptOut.id).where(OptOut.kind == kind, OptOut.value == value)) is not None


def add_opt_out(session: Session, person: Person | None, *, email: str | None = None, reason: str = "",
                source: str = "manual", now: datetime) -> None:
    """Permanent opt-out: the person, all their addresses/handles, and any open outreach."""
    values: list[tuple[str, str]] = []
    if email:
        values.append(("email", normalize_email(email)))
    if person is not None:
        person.opted_out = True
        person.opted_out_at = person.opted_out_at or now
        for ident in person.identities:
            if ident.type == IdentityType.EMAIL:
                values.append(("email", ident.value))
            elif ident.type in DM_PLATFORMS:
                values.append((ident.type, ident.value.lower().lstrip("@")))
        for o in person.outreach:
            if o.status in OPEN_OUTREACH:
                o.status = OutreachStatus.CANCELLED
                o.error = "opted out"
    for kind, value in dict.fromkeys(values):
        if not is_suppressed(session, kind, value):
            session.add(OptOut(kind=kind, value=value, person_id=person.id if person else None, reason=reason, source=source))
    session.flush()


def recently_emailed(session: Session, address: str, since: datetime) -> bool:
    return session.scalar(
        select(Outreach.id).where(Outreach.channel == Channel.EMAIL, Outreach.to_address == normalize_email(address),
                                  Outreach.sent_at.is_not(None), Outreach.sent_at >= since).limit(1)
    ) is not None


def email_block_reason(session: Session, person: Person, target: EmailTarget, settings: AppSettings, now: datetime) -> str | None:
    """Why this email must not go out now (None = OK). Re-checked right before sending."""
    if person.opted_out:
        return "contact opted out"
    if is_suppressed(session, "email", target.address):
        return "address is on the opt-out list"
    if person.cooldown_until and person.cooldown_until > now:
        return "contact is in cooldown"
    outlets = list(person.outlets) + ([target.outlet] if target.outlet else [])
    if settings.outlet_cooldown_days > 0 and any(o.cooldown_until and o.cooldown_until > now for o in outlets):
        return "outlet is in cooldown"
    if recently_emailed(session, target.address, now - timedelta(days=settings.cooldown_days)):
        return "address was emailed recently"
    return None


def open_outreach(session: Session, channel: str) -> int:
    return session.scalar(
        select(func.count(Outreach.id)).where(Outreach.channel == channel, Outreach.status.in_(OPEN_OUTREACH))
    ) or 0


def sent_on(session: Session, day, tz) -> int:
    times = session.scalars(select(Outreach.sent_at).where(Outreach.channel == Channel.EMAIL, Outreach.sent_at.is_not(None)))
    return sum(1 for t in times if to_local(t, tz).date() == day)


def _candidates(session: Session, settings: AppSettings, limit: int = 600) -> list[Person]:
    return list(session.scalars(
        select(Person)
        .where(Person.status == PersonStatus.NEW, Person.opted_out.is_(False), Person.needs_review.is_(False),
               Person.taste_score >= settings.min_score_to_queue)
        .order_by(Person.taste_score.desc(), Person.id)
        .limit(limit)
    ))


def build_daily_queue(session: Session, settings: AppSettings, *, now: datetime, rng: random.Random,
                      user_id: int | None = None) -> dict:
    tz = get_tz(settings.timezone)
    today = to_local(now, tz).date()
    stats = {"emails_queued": 0, "dms_queued": 0, "skipped": {}}

    def skip(reason: str) -> None:
        stats["skipped"][reason] = stats["skipped"].get(reason, 0) + 1

    cap = cap_for_day(session, settings, today, today=today)
    room = cap - open_outreach(session, Channel.EMAIL) - sent_on(session, today, tz)
    stats["cap"] = cap
    partners = pending_partner_ids(session)
    chosen: set[int] = set()
    chosen_outlets: set[int] = set()
    chosen_addresses: set[str] = set()
    has_open = {pid for pid in session.scalars(select(Outreach.person_id).where(Outreach.status.in_(OPEN_OUTREACH)))}

    candidates = _candidates(session, settings)
    if room > 0:
        for person in candidates:
            if stats["emails_queued"] >= room:
                break
            if person.id in has_open:
                continue
            target = email_target(person, settings)
            if target is None:
                continue
            reason = email_block_reason(session, person, target, settings, now)
            if reason:
                skip(reason)
                continue
            outlet_ids = {o.id for o in person.outlets} | ({target.outlet.id} if target.outlet else set())
            if partners.get(person.id, set()) & chosen:
                skip("possible duplicate already queued")
                continue
            if outlet_ids & chosen_outlets or target.address in chosen_addresses:
                skip("one pitch per outlet per day")
                continue
            subject, body = render_pitch(person, settings)
            auto = not settings.require_approval and settings.opener_mode != "manual"
            session.add(Outreach(
                person=person, channel=Channel.EMAIL, to_address=target.address, to_kind=target.kind,
                status=OutreachStatus.APPROVED if auto else OutreachStatus.QUEUED, subject=subject, body=body,
                queue_date=today, created_by_user_id=user_id, approved_at=now if auto else None,
            ))
            person.status = PersonStatus.QUEUED
            chosen.add(person.id)
            chosen_outlets |= outlet_ids
            chosen_addresses.add(target.address)
            stats["emails_queued"] += 1

    dm_room = settings.daily_dm_target - open_outreach(session, Channel.DM)
    for person in candidates:
        if dm_room <= 0:
            break
        if person.id in has_open or person.id in chosen or email_target(person, settings) is not None:
            continue
        ident = dm_target(person)
        if ident is None:
            continue
        if is_suppressed(session, ident.type, ident.value):
            skip("handle is on the opt-out list")
            continue
        if person.cooldown_until and person.cooldown_until > now:
            continue
        session.add(Outreach(
            person=person, channel=Channel.DM, platform=ident.type, to_address=ident.display or ident.value,
            status=OutreachStatus.QUEUED, body=render_dm(person, settings), queue_date=today, created_by_user_id=user_id,
        ))
        person.status = PersonStatus.QUEUED
        chosen.add(person.id)
        dm_room -= 1
        stats["dms_queued"] += 1

    session.flush()
    stats["scheduled"] = schedule_approved(session, settings, now=now, rng=rng)
    return stats


def approve(session: Session, outreach: Outreach, settings: AppSettings, *, now: datetime, rng: random.Random,
            user_id: int | None = None, subject: str | None = None, body: str | None = None) -> None:
    if outreach.channel != Channel.EMAIL or outreach.status != OutreachStatus.QUEUED:
        raise ValueError("Only queued emails can be approved")
    if subject is not None:
        outreach.subject = subject
    if body is not None:
        outreach.body = body
    if not (outreach.body or "").strip():
        raise ValueError("Email body is empty")
    outreach.status = OutreachStatus.APPROVED
    outreach.approved_at = now
    outreach.approved_by_user_id = user_id
    session.flush()
    schedule_approved(session, settings, now=now, rng=rng)


def skip(session: Session, outreach: Outreach, settings: AppSettings, *, now: datetime) -> None:
    """Skip = snooze this contact for the cooldown period."""
    if outreach.status not in OPEN_OUTREACH:
        raise ValueError("Only queued or approved items can be skipped")
    outreach.status = OutreachStatus.SKIPPED
    outreach.scheduled_at = None
    person = outreach.person
    if person.status == PersonStatus.QUEUED and not any(o.status in OPEN_OUTREACH for o in person.outreach if o.id != outreach.id):
        person.status = PersonStatus.NEW
        person.cooldown_until = now + timedelta(days=settings.cooldown_days)
    session.flush()


def mark_dm_sent(session: Session, outreach: Outreach, *, user_id: int | None, now: datetime, settings: AppSettings,
                 body: str | None = None) -> None:
    if outreach.channel != Channel.DM or outreach.status != OutreachStatus.QUEUED:
        raise ValueError("Only queued DMs can be marked as sent")
    if body:
        outreach.body = body
    outreach.status = OutreachStatus.SENT
    outreach.sent_at = now
    outreach.sent_by_user_id = user_id
    mark_pitched(outreach.person, settings, now)
    session.flush()


def mark_pitched(person: Person, settings: AppSettings, now: datetime) -> None:
    from ..models import STATUS_RANK

    if STATUS_RANK.get(person.status, 0) < STATUS_RANK[PersonStatus.PITCHED]:
        person.status = PersonStatus.PITCHED
    person.last_contacted_at = now
    person.cooldown_until = now + timedelta(days=settings.cooldown_days)
    if settings.outlet_cooldown_days > 0:
        for outlet in person.outlets:
            outlet.cooldown_until = now + timedelta(days=settings.outlet_cooldown_days)
