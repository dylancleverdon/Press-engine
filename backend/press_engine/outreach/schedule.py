"""Sending caps, warm-up ramp and randomised send times (spec §1, §4.8)."""

from __future__ import annotations

import random
from bisect import insort
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..appsettings import AppSettings
from ..models import Channel, Outreach, OutreachStatus
from ..util.timeutil import get_tz, guess_recipient_tz, local_window_utc, to_local


def is_send_day(day: date, settings: AppSettings) -> bool:
    return day.weekday() in settings.send_days


def warmup_cap(settings: AppSettings, send_days_so_far: int) -> int:
    """Linear ramp from warmup_start to daily_email_target over warmup_days send days."""
    target = max(0, settings.daily_email_target)
    start = min(max(1, settings.warmup_start), target) if target else 0
    n = settings.warmup_days
    if n <= 1 or send_days_so_far >= n - 1:
        return target
    return round(start + (target - start) * send_days_so_far / (n - 1))


def sent_email_times(session: Session) -> list[datetime]:
    return list(session.scalars(
        select(Outreach.sent_at).where(Outreach.channel == Channel.EMAIL, Outreach.sent_at.is_not(None))
    ))


def send_days_before(session: Session, day: date, settings: AppSettings) -> int:
    tz = get_tz(settings.timezone)
    return len({to_local(t, tz).date() for t in sent_email_times(session) if to_local(t, tz).date() < day})


def cap_for_day(session: Session, settings: AppSettings, day: date, *, today: date) -> int:
    if settings.sending_paused or not is_send_day(day, settings):
        return 0
    done = send_days_before(session, today, settings)
    # Future days: assume each send day between today and `day` will be used.
    d = today
    while d < day:
        if is_send_day(d, settings):
            done += 1
        d += timedelta(days=1)
    return warmup_cap(settings, done)


def _pick(start: datetime, end: datetime, taken: list[datetime], gap: timedelta, rng: random.Random, tries: int = 40) -> datetime | None:
    span = (end - start).total_seconds()
    if span <= 0:
        return None
    for _ in range(tries):
        t = (start + timedelta(seconds=rng.uniform(0, span))).replace(microsecond=0)
        if all(abs((t - a).total_seconds()) >= gap.total_seconds() for a in taken):
            return t
    return None


def plan_times(windows: list[tuple[datetime, datetime]], *, min_gap: timedelta, taken: list[datetime],
               rng: random.Random) -> list[datetime | None]:
    """Random send times, one per window, at least ``min_gap`` apart from each other
    and from ``taken``. Items sharing a window are stratified across it so the day
    looks evenly spread rather than bunched."""
    assigned = sorted(taken)
    out: list[datetime | None] = [None] * len(windows)
    groups: dict[tuple[datetime, datetime], list[int]] = defaultdict(list)
    for i, w in enumerate(windows):
        groups[w].append(i)
    for (start, end), idxs in groups.items():
        span = (end - start).total_seconds()
        k = len(idxs)
        for slot, i in enumerate(idxs):
            s0 = start + timedelta(seconds=span * slot / k)
            s1 = start + timedelta(seconds=span * (slot + 1) / k)
            t = _pick(s0, s1, assigned, min_gap, rng) or _pick(start, end, assigned, min_gap, rng)
            if t is not None:
                insort(assigned, t)
                out[i] = t
    return out


def send_window(outreach: Outreach, day: date, settings: AppSettings) -> tuple[datetime, datetime]:
    tz_name = settings.timezone
    if settings.timezone_mode == "recipient":
        person = outreach.person
        outlet = person.outlets[0] if person.outlets else None
        tz_name = guess_recipient_tz(person.country or (outlet.country if outlet else None),
                                     person.language or (outlet.language if outlet else None), settings.timezone)
    return local_window_utc(day, settings.send_window_start, settings.send_window_end, get_tz(tz_name))


def schedule_approved(session: Session, settings: AppSettings, *, now: datetime, rng: random.Random, horizon_days: int = 14) -> int:
    """Give every approved, unscheduled email a send time, respecting daily caps."""
    tz = get_tz(settings.timezone)
    today = to_local(now, tz).date()
    approved = list(session.scalars(
        select(Outreach).where(Outreach.channel == Channel.EMAIL, Outreach.status == OutreachStatus.APPROVED,
                               Outreach.sent_at.is_(None)).order_by(Outreach.approved_at, Outreach.id)
    ))
    pending = [o for o in approved if o.scheduled_at is None]
    if not pending or settings.sending_paused:
        return 0
    scheduled = [o for o in approved if o.scheduled_at is not None]
    sent_times = sent_email_times(session)
    earliest = now + timedelta(minutes=2)
    count = 0
    for offset in range(horizon_days):
        if not pending:
            break
        day = today + timedelta(days=offset)
        cap = cap_for_day(session, settings, day, today=today)
        on_day = [o.scheduled_at for o in scheduled if to_local(o.scheduled_at, tz).date() == day]
        sent_on_day = [t for t in sent_times if to_local(t, tz).date() == day]
        free = cap - len(on_day) - len(sent_on_day)
        if free <= 0:
            continue
        batch = pending[:free]
        windows = []
        for o in batch:
            start, end = send_window(o, day, settings)
            windows.append((max(start, earliest), end))
        times = plan_times(windows, min_gap=timedelta(minutes=settings.min_gap_minutes),
                           taken=on_day + sent_on_day, rng=rng)
        for o, t in zip(batch, times):
            if t is not None:
                o.scheduled_at = t
                o.queue_date = day
                scheduled.append(o)
                count += 1
        pending = [o for o in pending if o.scheduled_at is None]
    session.flush()
    return count
