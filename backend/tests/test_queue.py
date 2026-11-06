from __future__ import annotations

import random
from datetime import datetime, timedelta

from sqlalchemy import select

from press_engine.appsettings import AppSettings, update_settings
from press_engine.models import Channel, MergeCandidate, Outreach, OutreachStatus, PersonStatus
from press_engine.outreach.queue import add_opt_out, approve, build_daily_queue, skip
from press_engine.outreach.schedule import plan_times, schedule_approved, warmup_cap
from press_engine.outreach.templates import render_pitch
from press_engine.scoring import score_heuristic
from press_engine.util.timeutil import get_tz, to_local

from . import factories as f

# A Monday, 07:00 UTC.
NOW = datetime(2026, 11, 2, 7, 0)


def settings(session, **kw) -> AppSettings:
    base = dict(timezone="Europe/Copenhagen", band_name="Night Swim", track_url="https://song.link/x",
                min_score_to_queue=0.1, require_approval=True, warmup_start=5, daily_email_target=20)
    base.update(kw)
    return update_settings(session, **base)


def make_contacts(session, n: int, *, outlet_per_person: bool = True):
    seed = f.artist(session, "Athletic Progression", seed=True)
    people = []
    for i in range(n):
        outlet = f"zine{i}.dk" if outlet_per_person else "shared.dk"
        people.append(f.person(session, f"Writer {chr(65 + i)}son", outlet=outlet, email=f"w{i}@zine{i}.dk", covers=[seed]))
    score_heuristic(session, now=NOW)
    return people


def test_warmup_ramp():
    s = AppSettings(warmup_start=5, daily_email_target=20, warmup_days=14)
    caps = [warmup_cap(s, d) for d in range(16)]
    assert caps[0] == 5 and caps[13] == 20 and caps[15] == 20
    assert caps == sorted(caps)


def test_plan_times_spread_and_gap():
    start, end = datetime(2026, 11, 2, 8, 30), datetime(2026, 11, 2, 15, 30)
    times = plan_times([(start, end)] * 20, min_gap=timedelta(minutes=8), taken=[], rng=random.Random(1))
    assert all(t is not None and start <= t <= end for t in times)
    ordered = sorted(times)
    assert all((b - a) >= timedelta(minutes=8) for a, b in zip(ordered, ordered[1:]))
    # Stratified: each hour of the window gets some sends.
    assert len({t.hour for t in times}) >= 6
    # Impossible packing leaves the overflow unscheduled instead of breaking the gap.
    tight = plan_times([(start, start + timedelta(minutes=20))] * 5, min_gap=timedelta(minutes=8), taken=[], rng=random.Random(1))
    assert sum(t is not None for t in tight) <= 3


def test_queue_respects_warmup_cap_and_approval(session):
    s = settings(session)
    make_contacts(session, 8)
    stats = build_daily_queue(session, s, now=NOW, rng=random.Random(3))
    assert stats["cap"] == 5 and stats["emails_queued"] == 5
    queued = session.scalars(select(Outreach).where(Outreach.channel == Channel.EMAIL)).all()
    assert all(o.status == OutreachStatus.QUEUED and o.scheduled_at is None for o in queued)
    assert all(o.person.status == PersonStatus.QUEUED for o in queued)
    subject, body = queued[0].subject, queued[0].body
    assert "Night Swim" in subject and "Athletic Progression" in subject
    assert "I really enjoyed your review of Athletic Progression" in body
    assert "unsubscribe" in body

    # Running again the same day does not exceed the cap.
    assert build_daily_queue(session, s, now=NOW + timedelta(hours=1), rng=random.Random(3))["emails_queued"] == 0

    approve(session, queued[0], s, now=NOW, rng=random.Random(3))
    o = queued[0]
    assert o.status == OutreachStatus.APPROVED and o.scheduled_at is not None
    local = to_local(o.scheduled_at, get_tz("Europe/Copenhagen"))
    assert (9, 30) <= (local.hour, local.minute) <= (16, 30) and local.date() == NOW.date()


def test_auto_approval_schedules_everything_spaced(session):
    s = settings(session, require_approval=False, warmup_start=20)
    make_contacts(session, 12)
    stats = build_daily_queue(session, s, now=NOW, rng=random.Random(5))
    assert stats["emails_queued"] == 12 and stats["scheduled"] == 12
    times = sorted(o.scheduled_at for o in session.scalars(select(Outreach)))
    assert all((b - a) >= timedelta(minutes=s.min_gap_minutes) for a, b in zip(times, times[1:]))


def test_one_pitch_per_outlet_and_cooldowns(session):
    s = settings(session, warmup_start=20)
    people = make_contacts(session, 3, outlet_per_person=False)
    stats = build_daily_queue(session, s, now=NOW, rng=random.Random(1))
    assert stats["emails_queued"] == 1
    assert stats["skipped"]["one pitch per outlet per day"] == 2

    # Outlet cooldown after a pitch blocks the others for 30 days.
    people[0].outlets[0].cooldown_until = NOW + timedelta(days=30)
    for o in session.scalars(select(Outreach)):
        o.status = OutreachStatus.SENT
        o.sent_at = NOW
    next_week = NOW + timedelta(days=7)
    stats = build_daily_queue(session, s, now=next_week, rng=random.Random(1))
    assert stats["emails_queued"] == 0 and stats["skipped"]["outlet is in cooldown"] == 2


def test_opt_out_and_probable_duplicates_are_respected(session):
    s = settings(session, warmup_start=20)
    a, b, c = make_contacts(session, 3)
    add_opt_out(session, a, reason="asked", now=NOW)
    session.add(MergeCandidate(person_a_id=b.id, person_b_id=c.id, score=0.9, reason="test"))
    session.flush()
    stats = build_daily_queue(session, s, now=NOW, rng=random.Random(1))
    assert stats["emails_queued"] == 1
    assert stats["skipped"]["possible duplicate already queued"] == 1
    queued_people = {o.person_id for o in session.scalars(select(Outreach))}
    assert a.id not in queued_people


def test_skip_snoozes_contact(session):
    s = settings(session)
    make_contacts(session, 1)
    build_daily_queue(session, s, now=NOW, rng=random.Random(1))
    o = session.scalars(select(Outreach)).one()
    skip(session, o, s, now=NOW)
    assert o.status == OutreachStatus.SKIPPED and o.person.status == PersonStatus.NEW
    assert o.person.cooldown_until > NOW + timedelta(days=29)
    assert build_daily_queue(session, s, now=NOW + timedelta(days=1), rng=random.Random(1))["emails_queued"] == 0


def test_dm_queue_for_contacts_without_email(session):
    s = settings(session)
    seed = f.artist(session, "Athletic Progression", seed=True)
    f.person(session, "Tok Creator", kind="creator", covers=[seed], handles={"tiktok": "tokcreator", "instagram": "tok.creator"})
    score_heuristic(session, now=NOW)
    stats = build_daily_queue(session, s, now=NOW, rng=random.Random(1))
    assert stats["dms_queued"] == 1
    dm = session.scalars(select(Outreach).where(Outreach.channel == Channel.DM)).one()
    assert dm.platform == "instagram" and "Night Swim" in dm.body


def test_weekend_and_overflow_roll_to_next_send_day(session):
    s = settings(session, require_approval=False, warmup_start=3, daily_email_target=3)
    make_contacts(session, 5)
    saturday = datetime(2026, 11, 7, 7, 0)
    build_daily_queue(session, s, now=saturday, rng=random.Random(1))
    assert session.scalars(select(Outreach)).all() == []  # cap 0 at weekends

    friday_afternoon = datetime(2026, 11, 6, 15, 22)  # 16:22 Copenhagen, minutes before the window closes
    build_daily_queue(session, s, now=friday_afternoon, rng=random.Random(1))
    days = sorted(to_local(o.scheduled_at, get_tz(s.timezone)).date() for o in session.scalars(select(Outreach)))
    assert len(days) == 3
    assert days[0].weekday() == 4 and days[-1].weekday() == 0  # what doesn't fit on Friday goes to Monday
    assert schedule_approved(session, s, now=friday_afternoon, rng=random.Random(1)) == 0


def test_render_without_coverage(session):
    s = settings(session)
    p = f.person(session, "No Coverage", email="n@c.dk")
    subject, body = render_pitch(p, s)
    assert subject == "Night Swim: new music for fans of" or subject == "Night Swim: new music"
    assert body.startswith("Hi No,")
