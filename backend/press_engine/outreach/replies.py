"""Reply handling (spec §4.9) and replying from the app (spec §4.11)."""

from __future__ import annotations

import html
import logging
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..appsettings import AppSettings
from ..contacts.ingest import find_identity
from ..llm import LLMError
from ..models import STATUS_RANK, Channel, InboundMessage, Outreach, OutreachStatus, Person, PersonStatus
from ..notify import push
from ..util.text import truncate
from .gmail import GmailError, build_message, header, plain_text, strip_quoted
from .queue import add_opt_out

log = logging.getLogger(__name__)

OPT_OUT_RE = re.compile(
    r"\b(unsubscribe|remove me|take me off|opt[- ]?out|stop (?:e-?mailing|contacting|sending)|"
    r"do not (?:e-?mail|contact)|don'?t (?:e-?mail|contact)|no more e-?mails|afmeld|abmelden|uitschrijven|avregistrera)\b",
    re.I,
)
AUTO_SUBJECT_RE = re.compile(
    r"^(auto(?:matic)?[ -]?(?:reply|response|svar)|out of (?:the )?office|abwesenheit|autosvar|automatisch antwoord|"
    r"fraværsmelding|frånvaro|réponse automatique|respuesta automática|ooo\b)",
    re.I,
)
BOUNCE_RE = re.compile(r"^(mailer-daemon|postmaster)@", re.I)
META_HEADERS = ["From", "Reply-To", "Subject", "Date", "Auto-Submitted", "X-Autoreply", "X-Autorespond",
                "Precedence", "X-Failed-Recipients", "Message-ID", "References"]


def is_opt_out(text: str | None) -> bool:
    return bool(text and OPT_OUT_RE.search(text))


def is_auto_reply(meta: dict) -> bool:
    auto = (header(meta, "Auto-Submitted") or "no").lower()
    if auto != "no" or header(meta, "X-Autoreply") or header(meta, "X-Autorespond"):
        return True
    if (header(meta, "Precedence") or "").lower() in ("auto_reply", "auto-reply"):
        return True
    return bool(AUTO_SUBJECT_RE.search((header(meta, "Subject") or "").strip()))


def _received(meta: dict) -> datetime:
    try:
        return datetime.fromtimestamp(int(meta["internalDate"]) / 1000, UTC).replace(tzinfo=None)
    except (KeyError, ValueError):
        return datetime.now(UTC).replace(tzinfo=None)


def _advance(person: Person, status: str) -> None:
    if STATUS_RANK.get(person.status, 0) < STATUS_RANK[status]:
        person.status = status


def handle_inbound(session: Session, svc, gmail, settings: AppSettings, o: Outreach, meta: dict, *, now: datetime,
                   stats: Counter) -> None:
    name, addr = parseaddr(header(meta, "From") or "")
    addr = addr.lower()
    received = _received(meta)
    if o.sent_at and received < o.sent_at:
        return
    snippet = html.unescape(meta.get("snippet") or "")
    person = o.person
    kind = "bounce" if BOUNCE_RE.match(addr) else "auto_reply" if is_auto_reply(meta) else "reply"
    msg = InboundMessage(gmail_message_id=meta["id"], gmail_thread_id=meta.get("threadId"), outreach_id=o.id,
                         person_id=person.id, from_address=addr, from_name=name or None, subject=header(meta, "Subject"),
                         snippet=snippet, kind=kind, received_at=received)
    session.add(msg)
    stats[kind] += 1
    if kind == "bounce":
        o.status = OutreachStatus.BOUNCED
        ident = find_identity(session, "email", o.to_address or "")
        if ident is not None:
            ident.bounced = True
        if person.status == PersonStatus.PITCHED:
            person.status = PersonStatus.NEW  # never reached; may be retried via another address or DM
            person.cooldown_until = None
        return
    if kind == "auto_reply":
        return
    text = snippet
    try:
        full = gmail.get_message(meta["id"], fmt="full")
        text = strip_quoted(plain_text(full.get("payload") or {})) or snippet
    except GmailError:
        pass
    o.status = OutreachStatus.REPLIED
    o.replied_at = received
    o.reply_snippet = truncate(text or snippet, 500)
    _advance(person, PersonStatus.REPLIED)
    if is_opt_out(text):
        msg.opt_out = True
        add_opt_out(session, person, email=addr, reason="Replied asking not to be contacted", source="reply", now=now)
        stats["opt_out"] += 1
    session.flush()
    title = f"{person.name} replied" + (" (opt-out)" if msg.opt_out else "")
    push(session, svc.http, svc.cfg.expo_push_url, title=title, body=truncate(text or snippet, 160),
         data={"outreach_id": o.id, "person_id": person.id})


def poll_replies(session: Session, svc, settings: AppSettings, *, now: datetime, lookback_days: int = 14,
                 include_new_threads: bool = False) -> dict:
    gmail = svc.gmail()
    if gmail is None:
        return {"skipped": "Google is not connected"}
    stats: Counter = Counter()
    sent = list(session.scalars(
        select(Outreach).where(Outreach.channel == Channel.EMAIL, Outreach.gmail_thread_id.is_not(None),
                               Outreach.sent_at >= now - timedelta(days=90))
    ))
    if not sent:
        return {"checked": 0}
    by_thread = {o.gmail_thread_id: o for o in sent}
    seen = set(session.scalars(select(InboundMessage.gmail_message_id)))

    def consider(ref: dict, outreach: Outreach | None) -> None:
        if ref["id"] in seen or outreach is None:
            return
        seen.add(ref["id"])
        meta = gmail.get_message(ref["id"], headers=META_HEADERS)
        handle_inbound(session, svc, gmail, settings, outreach, meta, now=now, stats=stats)

    for ref in gmail.list_messages(f"newer_than:{lookback_days}d -from:me -in:chats"):
        consider(ref, by_thread.get(ref.get("threadId")))

    # Bounces that Gmail filed in their own thread.
    by_address: dict[str, Outreach] = {}
    for o in sorted(sent, key=lambda x: x.sent_at):
        if o.to_address:
            by_address[o.to_address.lower()] = o
    for ref in gmail.list_messages(f"newer_than:{lookback_days}d from:(mailer-daemon OR postmaster)", pages=1):
        if ref["id"] in seen or ref.get("threadId") in by_thread:
            continue
        meta = gmail.get_message(ref["id"], headers=META_HEADERS)
        failed = (header(meta, "X-Failed-Recipients") or "").split(",")[0].strip().lower()
        seen.add(ref["id"])
        if failed in by_address:
            handle_inbound(session, svc, gmail, settings, by_address[failed], meta, now=now, stats=stats)

    # Contacts who answered in a brand-new thread instead of replying.
    if include_new_threads:
        waiting = [a for a, o in by_address.items() if o.status == OutreachStatus.SENT and o.sent_at >= now - timedelta(days=21)]
        for i in range(0, len(waiting), 25):
            chunk = waiting[i:i + 25]
            q = f"newer_than:{lookback_days}d from:(" + " OR ".join(chunk) + ")"
            for ref in gmail.list_messages(q, pages=1):
                if ref.get("threadId") in by_thread:
                    continue
                meta_from = None
                if ref["id"] not in seen:
                    meta_from = gmail.get_message(ref["id"], headers=META_HEADERS)
                    seen.add(ref["id"])
                if meta_from:
                    _, addr = parseaddr(header(meta_from, "From") or "")
                    target = by_address.get(addr.lower())
                    if target:
                        handle_inbound(session, svc, gmail, settings, target, meta_from, now=now, stats=stats)
    session.flush()
    return dict(stats)


# --- Thread view, drafts and replying -------------------------------------------------

def thread_messages(svc, o: Outreach) -> list[dict]:
    gmail = svc.gmail()
    if gmail is None or not o.gmail_thread_id:
        return []
    me = gmail.me()
    out = []
    for m in gmail.get_thread(o.gmail_thread_id).get("messages", []):
        name, addr = parseaddr(header(m, "From") or "")
        out.append({
            "id": m["id"],
            "from_name": name,
            "from_address": addr.lower(),
            "is_me": addr.lower() == me,
            "date": _received(m).isoformat(),
            "subject": header(m, "Subject"),
            "text": strip_quoted(plain_text(m.get("payload") or {})) or html.unescape(m.get("snippet") or ""),
        })
    return out


DRAFT_SYSTEM = (
    "You draft short, warm email replies for an independent musician to send to music press and creators. "
    "Write in plain text, first person, friendly and concise. Never invent facts such as dates, prices, links or "
    "attachments: use [placeholders] for anything you don't know. The musician will edit your draft before sending."
)


def suggest_reply(svc, settings: AppSettings, person: Person, messages: list[dict]) -> str:
    first = person.first_name or "there"
    fallback = (f"Hi {first},\n\nThanks so much for getting back to me! [Answer their question / suggest next step]\n\n"
                f"Best,\n{settings.signature}")
    llm = svc.draft_llm
    if llm is None or not messages:
        return fallback
    convo = "\n\n".join(
        f"{'ME' if m['is_me'] else (m['from_name'] or m['from_address'])} ({m['date'][:10]}):\n{truncate(m['text'], 1500)}"
        for m in messages[-6:]
    )
    prompt = (
        f"Band: {settings.band_name}. About the band: {settings.band_blurb}\n"
        f"My name: {settings.sender_name}. Sign off as:\n{settings.signature}\n"
        f"Track link: {settings.track_url or '[track link]'}\n"
        f"Contact: {person.name} ({person.kind})\n\n"
        f"Conversation so far (oldest first):\n{convo}\n\n"
        "Write my reply to their latest message. Body text only, no subject line."
    )
    try:
        return llm.text(DRAFT_SYSTEM, prompt, max_tokens=700) or fallback
    except LLMError as e:
        log.info("draft failed: %s", e)
        return fallback


def send_reply(session: Session, svc, settings: AppSettings, o: Outreach, body: str, *, now: datetime) -> dict:
    gmail = svc.gmail()
    if gmail is None:
        raise GmailError("Google is not connected")
    if not o.gmail_thread_id:
        raise GmailError("This outreach has no Gmail thread")
    thread = gmail.get_thread(o.gmail_thread_id, fmt="metadata")
    msgs = thread.get("messages", [])
    if not msgs:
        raise GmailError("Thread is empty")
    me = gmail.me()
    theirs = [m for m in msgs if parseaddr(header(m, "From") or "")[1].lower() != me]
    last = msgs[-1]
    reply_to = header(theirs[-1], "Reply-To") or header(theirs[-1], "From") if theirs else o.to_address
    subject = header(msgs[0], "Subject") or o.subject or ""
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    msg = build_message(from_name=settings.sender_name, from_addr=me, to=reply_to, subject=subject, body=body,
                        in_reply_to=header(last, "Message-ID"), references=header(last, "References"))
    result = gmail.send(msg, thread_id=o.gmail_thread_id)
    _advance(o.person, PersonStatus.IN_CONVERSATION)
    for m in session.scalars(select(InboundMessage).where(InboundMessage.gmail_thread_id == o.gmail_thread_id)):
        m.read = True
    session.flush()
    return result
