from __future__ import annotations

import random
from datetime import timedelta

import pytest
from sqlalchemy import select

from press_engine.models import (
    Device,
    Identity,
    InboundMessage,
    OptOut,
    Outreach,
    OutreachStatus,
    PersonStatus,
    User,
)
from press_engine.outreach.gmail import GmailClient, strip_quoted
from press_engine.outreach.queue import add_opt_out, build_daily_queue
from press_engine.outreach.replies import is_opt_out, poll_replies, send_reply, suggest_reply, thread_messages
from press_engine.outreach.sender import send_due
from press_engine.scoring import score_heuristic

from . import factories as f
from .conftest import FakeLLM
from .gmail_fake import ME, GmailFake
from .test_queue import NOW, settings


@pytest.fixture
def gmail(services, router):
    fake = GmailFake(router)
    services._gmail = GmailClient(lambda: "test-token", services.http)
    return fake


def queue_and_approve(session, n=3, **kw):
    s = settings(session, require_approval=False, warmup_start=20, **kw)
    seed = f.artist(session, "Athletic Progression", seed=True)
    people = [f.person(session, f"Writer {c}son", outlet=f"zine{c}.dk", email=f"w{c}@zine{c}.dk", covers=[seed])
              for c in "ABC"[:n]]
    score_heuristic(session, now=NOW)
    build_daily_queue(session, s, now=NOW, rng=random.Random(2))
    return s, people


def test_send_due_sends_one_at_a_time(session, services, gmail):
    s, people = queue_and_approve(session)
    first = min(session.scalars(select(Outreach)), key=lambda o: o.scheduled_at)
    at = first.scheduled_at + timedelta(seconds=5)
    stats = send_due(session, services, s, now=at, rng=random.Random(1))
    assert stats["sent"] == 1 and len(gmail.sent) == 1
    msg = gmail.sent[0]["parsed"]
    assert msg["To"] == first.to_address and "Night Swim" in msg["Subject"]
    assert msg["From"] == f"D <{ME}>"
    assert first.status == OutreachStatus.SENT and first.gmail_thread_id and first.rfc_message_id
    p = first.person
    assert p.status == PersonStatus.PITCHED
    assert p.cooldown_until == at + timedelta(days=30)
    assert p.outlets[0].cooldown_until == at + timedelta(days=30)

    # Another one due right away is postponed to keep the minimum gap.
    nxt = sorted((o for o in session.scalars(select(Outreach)) if o.status == OutreachStatus.APPROVED), key=lambda o: o.scheduled_at)[0]
    nxt.scheduled_at = at
    stats = send_due(session, services, s, now=at + timedelta(minutes=1), rng=random.Random(1))
    assert stats.get("sent", 0) == 0 and stats["postponed"] == 1
    assert nxt.scheduled_at >= at + timedelta(minutes=s.min_gap_minutes)


def test_send_due_rechecks_before_sending(session, services, gmail):
    s, people = queue_and_approve(session, n=1)
    o = session.scalars(select(Outreach)).one()
    add_opt_out(session, None, email=o.to_address, reason="test", now=NOW)
    # add_opt_out on an address only (no person) must still block the send.
    o.status = OutreachStatus.APPROVED
    stats = send_due(session, services, s, now=o.scheduled_at + timedelta(seconds=1), rng=random.Random(1))
    assert stats["cancelled"] == 1 and gmail.sent == []
    assert o.status == OutreachStatus.CANCELLED and "opt-out" in o.error


def test_send_due_never_sends_to_unverified_address(session, services, gmail):
    s, people = queue_and_approve(session, n=1)
    o = session.scalars(select(Outreach)).one()
    session.scalar(select(Identity).where(Identity.value == o.to_address)).verified = False
    send_due(session, services, s, now=o.scheduled_at + timedelta(seconds=1), rng=random.Random(1))
    assert gmail.sent == [] and o.status == OutreachStatus.CANCELLED


def test_overdue_sends_are_replanned_not_burst(session, services, gmail):
    s, people = queue_and_approve(session)
    late = max(o.scheduled_at for o in session.scalars(select(Outreach))) + timedelta(hours=2)
    stats = send_due(session, services, s, now=late, rng=random.Random(1))
    assert stats.get("sent", 0) == 0 and stats["rescheduled_late"] == 3
    assert all(o.scheduled_at is None or o.scheduled_at > late for o in session.scalars(select(Outreach)))


def _send_all(session, services, s):
    for o in sorted(session.scalars(select(Outreach)), key=lambda o: o.scheduled_at):
        t = o.scheduled_at + timedelta(seconds=1)
        send_due(session, services, s, now=t, rng=random.Random(1))
    return list(session.scalars(select(Outreach).order_by(Outreach.id)))


def test_reply_detection_optout_autoreply_bounce_and_push(session, services, gmail, router):
    s, people = queue_and_approve(session)
    owner = User(name="D", role="owner", token_hash="x" * 64, notify_replies=True)
    session.add(owner)
    session.flush()
    session.add(Device(user_id=owner.id, expo_push_token="ExponentPushToken[abc]"))
    pushes = []
    router.add("POST", "https://exp.test/push", lambda r: (pushes.append(r), __import__("httpx").Response(200, json={"data": [{"status": "ok"}]}))[1])

    outs = _send_all(session, services, s)
    assert all(o.status == OutreachStatus.SENT for o in outs)
    later = int((max(o.sent_at for o in outs) + timedelta(minutes=5)).timestamp() * 1000) + 3_600_000
    a, b, c = outs
    gmail.add_inbound(thread_id=a.gmail_thread_id, from_addr=a.to_address, subject="Re: hi", internal_ms=later,
                      body="Love it! Can you send a WAV?\n\nOn Mon, D wrote:\n> old text")
    gmail.add_inbound(thread_id=b.gmail_thread_id, from_addr=b.to_address, subject="Automatic reply: away",
                      internal_ms=later, body="I'm away until Monday", headers={"Auto-Submitted": "auto-replied"})
    gmail.add_inbound(thread_id=c.gmail_thread_id, from_addr=c.to_address, subject="Re: hi", internal_ms=later,
                      body="Please unsubscribe me from these.")
    gmail.add_inbound(thread_id="t-unrelated", from_addr="friend@x.com", subject="Dinner?", internal_ms=later, body="hi")

    now = NOW + timedelta(days=1)
    stats = poll_replies(session, services, s, now=now)
    assert stats["reply"] == 2 and stats["auto_reply"] == 1 and stats["opt_out"] == 1
    assert a.status == OutreachStatus.REPLIED and a.person.status == PersonStatus.REPLIED
    assert "WAV" in a.reply_snippet and "old text" not in a.reply_snippet
    assert b.status == OutreachStatus.SENT  # out-of-office isn't a reply
    assert c.person.opted_out and session.scalar(select(OptOut).where(OptOut.value == c.to_address))
    assert len(pushes) == 2
    assert session.scalar(select(InboundMessage).where(InboundMessage.from_address == "friend@x.com")) is None

    # Polling again doesn't double count.
    assert poll_replies(session, services, s, now=now).get("reply", 0) == 0

    # Bounce in a separate thread, matched via X-Failed-Recipients.
    gmail.add_inbound(thread_id="t-bounce", from_addr="mailer-daemon@googlemail.com", subject="Delivery Status Notification",
                      internal_ms=later, body="Address not found", headers={"X-Failed-Recipients": b.to_address})
    stats = poll_replies(session, services, s, now=now)
    assert stats["bounce"] == 1 and b.status == OutreachStatus.BOUNCED
    assert session.scalar(select(Identity).where(Identity.value == b.to_address)).bounced
    assert b.person.status == PersonStatus.NEW


def test_reply_in_new_thread_is_matched_by_sender(session, services, gmail):
    s, people = queue_and_approve(session, n=1)
    (o,) = _send_all(session, services, s)
    later = int((o.sent_at + timedelta(hours=3)).timestamp() * 1000)
    gmail.add_inbound(thread_id="t-fresh", from_addr=o.to_address, subject="Your band", internal_ms=later, body="Hey, got your mail")
    assert poll_replies(session, services, s, now=NOW + timedelta(days=1)).get("reply", 0) == 0
    stats = poll_replies(session, services, s, now=NOW + timedelta(days=1), include_new_threads=True)
    assert stats["reply"] == 1 and o.status == OutreachStatus.REPLIED


def test_thread_draft_and_reply_from_app(session, services, gmail):
    s, people = queue_and_approve(session, n=1)
    (o,) = _send_all(session, services, s)
    later = int((o.sent_at + timedelta(hours=1)).timestamp() * 1000)
    gmail.add_inbound(thread_id=o.gmail_thread_id, from_addr=o.to_address, subject="Re: x", internal_ms=later, body="Send me the EPK?")
    poll_replies(session, services, s, now=NOW + timedelta(days=1))

    msgs = thread_messages(services, o)
    assert [m["is_me"] for m in msgs] == [True, False]
    services.score_llm = FakeLLM(text_fn=lambda system, prompt: "Hi! Here's the EPK: [link]")
    draft = suggest_reply(services, s, o.person, msgs)
    assert draft.startswith("Hi!") and "Send me the EPK?" in services.score_llm.text_calls[0]

    send_reply(session, services, s, o, "Hi! Here it is.", now=NOW + timedelta(days=1))
    reply = gmail.sent[-1]
    assert reply["payload"]["threadId"] == o.gmail_thread_id
    assert reply["parsed"]["In-Reply-To"].startswith("<in") and reply["parsed"]["Subject"].startswith("Re:")
    assert reply["parsed"]["To"].endswith(f"<{o.to_address}>")
    assert o.person.status == PersonStatus.IN_CONVERSATION


def test_opt_out_phrases_and_quote_stripping():
    assert is_opt_out("Please remove me from your list")
    assert is_opt_out("unsubscribe")
    assert not is_opt_out("I'd love to hear more, send it over")
    assert strip_quoted("Sounds good!\n\nOn Tue, 3 Nov 2026, D wrote:\n> pitch") == "Sounds good!"
    assert strip_quoted("Fedt!\n\nDen tir. 3. nov. 2026 skrev D:\n> pitch") == "Fedt!"
