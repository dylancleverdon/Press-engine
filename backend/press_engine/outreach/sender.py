"""The drip: send due emails one at a time through D's Gmail (spec §4.8)."""

from __future__ import annotations

import logging
import random
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..appsettings import AppSettings
from ..contacts.ingest import find_identity
from ..models import Channel, Outreach, OutreachStatus, PersonStatus
from ..util.timeutil import get_tz, to_local
from .gmail import GmailError, build_message, header
from .google_auth import GoogleAuthError
from .queue import EmailTarget, email_block_reason, mark_pitched, sent_on
from .schedule import cap_for_day, schedule_approved

log = logging.getLogger(__name__)

# If the machine was asleep and a send is this late, re-plan it instead of
# firing a burst of overdue emails at once.
GRACE = timedelta(minutes=30)
MAX_ATTEMPTS = 3


def final_check(session: Session, o: Outreach, settings: AppSettings, now: datetime) -> str | None:
    person = o.person
    outlet = None
    if o.to_kind == "outlet_inbox":
        if not settings.allow_outlet_inbox:
            return "outlet inboxes are disabled in settings"
        outlet = next((x for x in person.outlets if x.contact_email == o.to_address), None)
        if outlet is None:
            return "outlet inbox address no longer on file"
    else:
        ident = find_identity(session, "email", o.to_address or "")
        if ident is None or ident.person_id != person.id or not ident.verified or ident.bounced:
            return "address is not a verified address of this contact"
    return email_block_reason(session, person, EmailTarget(o.to_address, o.to_kind or "personal", outlet), settings, now)


def send_due(session: Session, svc, settings: AppSettings, *, now: datetime, rng: random.Random) -> dict:
    if settings.sending_paused:
        return {"skipped": "sending is paused"}
    gmail = svc.gmail()
    if gmail is None:
        return {"skipped": "Google is not connected"}
    stats: Counter = Counter()
    due = list(session.scalars(
        select(Outreach)
        .where(Outreach.channel == Channel.EMAIL, Outreach.status == OutreachStatus.APPROVED,
               Outreach.sent_at.is_(None), Outreach.scheduled_at.is_not(None), Outreach.scheduled_at <= now)
        .order_by(Outreach.scheduled_at)
        .limit(10)
    ))
    tz = get_tz(settings.timezone)
    gap = timedelta(minutes=settings.min_gap_minutes)
    last = session.scalar(select(func.max(Outreach.sent_at)).where(Outreach.channel == Channel.EMAIL))
    for o in due:
        if now - o.scheduled_at > GRACE:
            o.scheduled_at = None
            stats["rescheduled_late"] += 1
            continue
        if last is not None and now - last < gap:
            o.scheduled_at = last + gap + timedelta(seconds=rng.randint(30, 240))
            stats["postponed"] += 1
            continue
        today = to_local(now, tz).date()
        if sent_on(session, today, tz) >= cap_for_day(session, settings, today, today=today):
            o.scheduled_at = None
            stats["over_cap"] += 1
            continue
        reason = final_check(session, o, settings, now)
        if reason:
            o.status = OutreachStatus.CANCELLED
            o.error = reason
            if o.person.status == PersonStatus.QUEUED:
                o.person.status = PersonStatus.NEW
            stats["cancelled"] += 1
            continue
        try:
            msg = build_message(from_name=settings.sender_name, from_addr=gmail.me(), to=o.to_address,
                                subject=o.subject or settings.band_name, body=o.body or "")
            result = gmail.send(msg)
        except GoogleAuthError as e:
            log.error("Gmail auth problem, not sending: %s", e)
            return {**stats, "error": str(e)}
        except GmailError as e:
            o.attempts += 1
            o.error = str(e)[:500]
            if not e.retryable or o.attempts >= MAX_ATTEMPTS:
                o.status = OutreachStatus.FAILED
            else:
                o.scheduled_at = now + timedelta(minutes=10 * o.attempts)
            stats["errors"] += 1
            break
        o.status = OutreachStatus.SENT
        o.sent_at = now
        o.error = None
        o.gmail_message_id = result.get("id")
        o.gmail_thread_id = result.get("threadId")
        o.rfc_message_id = msg["Message-ID"]
        try:  # Gmail may assign its own Message-ID; keep the real one for threading replies.
            o.rfc_message_id = header(gmail.get_message(o.gmail_message_id, headers=["Message-ID"]), "Message-ID") or o.rfc_message_id
        except GmailError:
            pass
        mark_pitched(o.person, settings, now)
        stats["sent"] += 1
        break  # one per tick keeps sends naturally spaced
    session.flush()
    stats["scheduled"] = schedule_approved(session, settings, now=now, rng=rng)
    return dict(stats)
