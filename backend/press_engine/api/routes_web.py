"""Server-rendered dashboard (spec §6 "Web dashboard")."""

from __future__ import annotations

import io
from datetime import datetime
from typing import get_args
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..appsettings import PLACEHOLDERS, AppSettings, load_settings, save_settings
from ..calendar import CalendarError, create_meeting
from ..contacts.dedupe import resolve_candidate
from ..contacts.ingest import add_identity
from ..contacts.resolve import resolve_person
from ..discovery.lastfm import add_seed, remove_seed
from ..jobs import JOBS
from ..models import (
    STATUS_RANK,
    Artist,
    ArtistSimilarity,
    DiscoveryLink,
    Identity,
    IdentityType,
    InboundMessage,
    JobRun,
    MergeCandidate,
    Outreach,
    OutreachStatus,
    Person,
    PersonStatus,
    User,
)
from ..outreach import queue as outq
from ..outreach.gmail import GmailError
from ..outreach.google_auth import GoogleAuthError
from ..outreach.replies import send_reply, suggest_reply, thread_messages
from ..util.timeutil import get_tz, to_local, utcnow
from ..util.urls import profile_url
from . import views
from .app import WEB_DIR
from .deps import COOKIE, get_db, get_svc, user_for_token, web_owner, web_user

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=WEB_DIR / "templates")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _local(dt: datetime | None, tz_name: str, fmt: str = "%a %d %b %H:%M") -> str:
    return to_local(dt, get_tz(tz_name)).strftime(fmt) if dt else ""


templates.env.filters["pct"] = lambda v: f"{round((v or 0) * 100)}"


def page(request: Request, name: str, user: User, session: Session, **ctx) -> HTMLResponse:
    from .. import updater

    settings = ctx.pop("settings", None) or load_settings(session)
    ctx.setdefault("msg", request.query_params.get("msg"))
    cfg = request.app.state.svc.cfg
    state = updater.read_state(cfg) if cfg.desktop else {}
    return templates.TemplateResponse(request, name, {
        "user": user, "settings": settings, "local": lambda dt, fmt="%a %d %b %H:%M": _local(dt, settings.timezone, fmt),
        "statuses": views.STATUSES, "profile_url": profile_url, "desktop": cfg.desktop,
        "app_update": state.get("desktop_update"), "app_update_url": state.get("dmg_url"),
        "move_hint": cfg.desktop and cfg.desktop_move_hint, **ctx,
    })


def back(url: str, msg: str | None = None) -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    return RedirectResponse(url, status_code=303)


# --- auth -------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": None})


@router.post("/login")
def login(request: Request, token: str = Form(...), next: str = Form("/"), session: Session = Depends(get_db)):
    from .routes_desktop import login_response

    user = user_for_token(session, token.strip())
    if user is None:
        return templates.TemplateResponse(request, "login.html", {"next": next, "error": "That token isn't valid."}, status_code=401)
    return login_response(request, user, next)


@router.get("/logout")
def logout():
    from ..sessions import COOKIE as SESSION_COOKIE

    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# --- today / queue ----------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def today(request: Request, user: User = Depends(web_user), session: Session = Depends(get_db), svc=Depends(get_svc)):
    settings = load_settings(session)
    summary = views.today_summary(session, settings, svc)
    queued = list(session.scalars(select(Outreach).where(Outreach.channel == "email", Outreach.status == OutreachStatus.QUEUED)
                                  .order_by(Outreach.id)))
    scheduled = list(session.scalars(select(Outreach).where(Outreach.channel == "email", Outreach.status == OutreachStatus.APPROVED)
                                     .order_by(Outreach.scheduled_at.is_(None), Outreach.scheduled_at)))
    recent = list(session.scalars(select(Outreach).where(Outreach.sent_at.is_not(None)).order_by(Outreach.sent_at.desc()).limit(15)))
    replies = list(session.scalars(select(InboundMessage).where(InboundMessage.kind == "reply")
                                   .order_by(InboundMessage.received_at.desc()).limit(5)))
    return page(request, "today.html", user, session, settings=settings, summary=summary, queued=queued,
                scheduled=scheduled, recent=recent, replies=replies, contact_line=views.contact_line)


@router.post("/outreach/{oid}/approve")
def approve(oid: int, subject: str = Form(...), body: str = Form(...), user: User = Depends(web_owner),
            session: Session = Depends(get_db), svc=Depends(get_svc)):
    o = _get(session, Outreach, oid)
    try:
        outq.approve(session, o, load_settings(session), now=utcnow(), rng=svc.rng, user_id=user.id, subject=subject, body=body)
    except ValueError as e:
        return back("/", str(e))
    return back("/", f"Approved: {o.person.name}")


@router.post("/outreach/{oid}/save")
def save_draft(oid: int, subject: str = Form(...), body: str = Form(...), user: User = Depends(web_owner),
               session: Session = Depends(get_db)):
    o = _get(session, Outreach, oid)
    o.subject, o.body = subject, body
    return back("/", "Saved")


@router.post("/outreach/approve-all")
def approve_all(user: User = Depends(web_owner), session: Session = Depends(get_db), svc=Depends(get_svc)):
    settings = load_settings(session)
    n = 0
    for o in session.scalars(select(Outreach).where(Outreach.channel == "email", Outreach.status == OutreachStatus.QUEUED)):
        if (o.body or "").strip():
            outq.approve(session, o, settings, now=utcnow(), rng=svc.rng, user_id=user.id)
            n += 1
    return back("/", f"Approved {n} emails")


@router.post("/outreach/{oid}/skip")
def skip(oid: int, next: str = Form("/"), user: User = Depends(web_owner), session: Session = Depends(get_db)):
    o = _get(session, Outreach, oid)
    try:
        outq.skip(session, o, load_settings(session), now=utcnow())
    except ValueError as e:
        return back(next, str(e))
    return back(next, f"Skipped {o.person.name} (snoozed for the cooldown period)")


@router.post("/outreach/{oid}/dm-sent")
def dm_sent(oid: int, body: str = Form(""), user: User = Depends(web_user), session: Session = Depends(get_db)):
    o = _get(session, Outreach, oid)
    try:
        outq.mark_dm_sent(session, o, user_id=user.id, now=utcnow(), settings=load_settings(session), body=body or None)
    except ValueError as e:
        return back("/dms", str(e))
    return back("/dms", f"Marked as sent: {o.person.name}")


@router.get("/dms", response_class=HTMLResponse)
def dms(request: Request, user: User = Depends(web_user), session: Session = Depends(get_db)):
    sent = list(session.scalars(select(Outreach).where(Outreach.channel == "dm", Outreach.status != OutreachStatus.QUEUED)
                                .order_by(Outreach.sent_at.desc()).limit(20)))
    return page(request, "dms.html", user, session, queue=views.dm_queue(session), sent=sent, sounds=views.sound_pages(session))


@router.post("/links/{lid}/reviewed")
def link_reviewed(lid: int, user: User = Depends(web_user), session: Session = Depends(get_db)):
    _get(session, DiscoveryLink, lid).reviewed = True
    return back("/dms")


# --- contacts ---------------------------------------------------------------------

@router.get("/contacts", response_class=HTMLResponse)
def contacts(request: Request, q: str | None = None, status: str | None = None, kind: str | None = None, p: int = 1,
             user: User = Depends(web_user), session: Session = Depends(get_db)):
    per = 50
    rows, total = views.search_people(session, q=q, status=status or None, kind=kind or None, limit=per, offset=(p - 1) * per)
    return page(request, "contacts.html", user, session, rows=rows, total=total, q=q or "", status=status or "",
                kind=kind or "", p=p, pages=max(1, (total + per - 1) // per), contact_line=views.contact_line)


@router.get("/contacts/{pid}", response_class=HTMLResponse)
def contact(request: Request, pid: int, user: User = Depends(web_user), session: Session = Depends(get_db)):
    person = _get(session, Person, pid)
    inbound = list(session.scalars(select(InboundMessage).where(InboundMessage.person_id == pid).order_by(InboundMessage.received_at.desc())))
    merges = list(session.scalars(select(MergeCandidate).where(
        MergeCandidate.status == "pending", (MergeCandidate.person_a_id == pid) | (MergeCandidate.person_b_id == pid))))
    return page(request, "contact.html", user, session, p=person, inbound=inbound, merges=merges,
                identity_types=[t.value for t in IdentityType], contact_line=views.contact_line)


@router.post("/contacts/{pid}/update")
def contact_update(pid: int, status: str = Form(...), notes: str = Form(""), user: User = Depends(web_owner),
                   session: Session = Depends(get_db)):
    person = _get(session, Person, pid)
    if status in views.STATUSES:
        person.status = status
    person.notes = notes or None
    if person.needs_review and status != PersonStatus.NEW:
        person.needs_review = False
    return back(f"/contacts/{pid}", "Saved")


@router.post("/contacts/{pid}/reviewed")
def contact_reviewed(pid: int, user: User = Depends(web_owner), session: Session = Depends(get_db)):
    person = _get(session, Person, pid)
    person.needs_review, person.review_reason = False, None
    return back(f"/contacts/{pid}", "Marked as reviewed")


@router.post("/contacts/{pid}/opt-out")
def contact_opt_out(pid: int, user: User = Depends(web_owner), session: Session = Depends(get_db)):
    person = _get(session, Person, pid)
    outq.add_opt_out(session, person, reason=f"marked by {user.name}", source="manual", now=utcnow())
    return back(f"/contacts/{pid}", "Opted out permanently")


@router.post("/contacts/{pid}/identities")
def identity_add(pid: int, type: str = Form(...), value: str = Form(...), user: User = Depends(web_owner),
                 session: Session = Depends(get_db)):
    person = _get(session, Person, pid)
    if type not in {t.value for t in IdentityType} or not value.strip():
        return back(f"/contacts/{pid}", "Pick a type and enter a value")
    value = value.strip()
    if type != IdentityType.EMAIL:
        value = value.lstrip("@").lower()
    survivor = add_identity(session, person, type, value, display=value, source_url=f"added by {user.name}",
                            verified=type == IdentityType.EMAIL)
    return back(f"/contacts/{survivor.id}", "Added" if survivor.id == pid else "Added - merged with an existing contact")


@router.post("/contacts/{pid}/identities/{iid}/verify")
def identity_verify(pid: int, iid: int, user: User = Depends(web_owner), session: Session = Depends(get_db)):
    ident = _get(session, Identity, iid)
    ident.verified, ident.bounced = True, False
    ident.source_url = f"{ident.source_url or ''} (confirmed by {user.name})".strip()
    return back(f"/contacts/{pid}", "Email confirmed")


@router.post("/contacts/{pid}/identities/{iid}/delete")
def identity_delete(pid: int, iid: int, user: User = Depends(web_owner), session: Session = Depends(get_db)):
    session.delete(_get(session, Identity, iid))
    return back(f"/contacts/{pid}", "Removed")


@router.post("/contacts/{pid}/resolve")
def contact_resolve(pid: int, user: User = Depends(web_owner), session: Session = Depends(get_db), svc=Depends(get_svc)):
    person = resolve_person(session, svc, _get(session, Person, pid), max_fetches=6)
    return back(f"/contacts/{person.id}", "Looked for contact details")


@router.post("/contacts/{pid}/meetings")
def contact_meeting(pid: int, title: str = Form(...), start: str = Form(...), duration: int = Form(30), notes: str = Form(""),
                    invite: bool = Form(False), meet: bool = Form(False), user: User = Depends(web_owner),
                    session: Session = Depends(get_db), svc=Depends(get_svc)):
    person = _get(session, Person, pid)
    settings = load_settings(session)
    try:
        when = datetime.fromisoformat(start).replace(tzinfo=get_tz(settings.timezone))
        create_meeting(session, svc, person, title=title, start=when, duration_minutes=duration, tz=settings.timezone,
                       notes=notes, invite=invite, with_meet=meet, user_id=user.id)
    except (ValueError, CalendarError, GoogleAuthError) as e:
        return back(f"/contacts/{pid}", f"Couldn't create the meeting: {e}")
    if STATUS_RANK.get(person.status, 0) < STATUS_RANK[PersonStatus.IN_CONVERSATION]:
        person.status = PersonStatus.IN_CONVERSATION
    return back(f"/contacts/{pid}", "Meeting booked" + ("" if svc.calendar() else " (saved locally; connect Google to sync)"))


@router.get("/merges", response_class=HTMLResponse)
def merges(request: Request, user: User = Depends(web_owner), session: Session = Depends(get_db)):
    rows = list(session.scalars(select(MergeCandidate).where(MergeCandidate.status == "pending").order_by(MergeCandidate.score.desc())))
    return page(request, "merges.html", user, session, rows=rows)


@router.post("/merges/{mid}")
def merge_decide(mid: int, decision: str = Form(...), user: User = Depends(web_owner), session: Session = Depends(get_db)):
    cand = _get(session, MergeCandidate, mid)
    survivor = resolve_candidate(session, cand, same=decision == "same", user_id=user.id)
    return back("/merges", f"Merged into {survivor.name}" if survivor else "Kept separate")


# --- inbox ------------------------------------------------------------------------

@router.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request, user: User = Depends(web_owner), session: Session = Depends(get_db)):
    rows = list(session.scalars(select(InboundMessage).order_by(InboundMessage.received_at.desc()).limit(100)))
    return page(request, "inbox.html", user, session, rows=rows)


def _thread_page(request, user, session, svc, o: Outreach, draft: str = "", error: str | None = None):
    try:
        messages = thread_messages(svc, o)
    except (GmailError, GoogleAuthError) as e:
        messages, error = [], str(e)
    return page(request, "thread.html", user, session, o=o, messages=messages, draft=draft, error=error)


@router.get("/inbox/{oid}", response_class=HTMLResponse)
def thread(request: Request, oid: int, user: User = Depends(web_owner), session: Session = Depends(get_db), svc=Depends(get_svc)):
    o = _get(session, Outreach, oid)
    for m in session.scalars(select(InboundMessage).where(InboundMessage.outreach_id == oid)):
        m.read = True
    return _thread_page(request, user, session, svc, o)


@router.post("/inbox/{oid}/draft", response_class=HTMLResponse)
def thread_draft(request: Request, oid: int, user: User = Depends(web_owner), session: Session = Depends(get_db), svc=Depends(get_svc)):
    o = _get(session, Outreach, oid)
    try:
        messages = thread_messages(svc, o)
        draft = suggest_reply(svc, load_settings(session), o.person, messages)
    except (GmailError, GoogleAuthError) as e:
        return _thread_page(request, user, session, svc, o, error=str(e))
    return _thread_page(request, user, session, svc, o, draft=draft)


@router.post("/inbox/{oid}/reply")
def thread_reply(request: Request, oid: int, body: str = Form(...), user: User = Depends(web_owner),
                 session: Session = Depends(get_db), svc=Depends(get_svc)):
    o = _get(session, Outreach, oid)
    try:
        send_reply(session, svc, load_settings(session), o, body, now=utcnow())
    except (GmailError, GoogleAuthError) as e:
        return _thread_page(request, user, session, svc, o, draft=body, error=str(e))
    return back(f"/inbox/{oid}", "Reply sent")


# --- seeds ------------------------------------------------------------------------

@router.get("/seeds", response_class=HTMLResponse)
def seeds(request: Request, user: User = Depends(web_user), session: Session = Depends(get_db)):
    seed_rows = list(session.scalars(select(Artist).where(Artist.source == "manual").order_by(Artist.active.desc(), Artist.name)))
    similar = list(session.scalars(select(Artist).where(Artist.source == "expanded").order_by(Artist.similarity.desc())))
    parents: dict[int, list[str]] = {}
    names = {a.id: a.name for a in seed_rows}
    for link in session.scalars(select(ArtistSimilarity)):
        if link.seed_id in names:
            parents.setdefault(link.artist_id, []).append(names[link.seed_id])
    return page(request, "seeds.html", user, session, seeds=seed_rows, similar=similar, parents=parents)


@router.post("/seeds")
def seed_add(names: str = Form(...), user: User = Depends(web_owner), session: Session = Depends(get_db)):
    added = [add_seed(session, n).name for n in names.replace(",", "\n").splitlines() if n.strip()]
    return back("/seeds", f"Added {', '.join(added)}. Expand them from the Runs page or wait for tonight's run.")


@router.post("/seeds/{aid}/remove")
def seed_remove(aid: int, user: User = Depends(web_owner), session: Session = Depends(get_db)):
    remove_seed(session, _get(session, Artist, aid))
    return back("/seeds", "Seed removed")


@router.post("/artists/{aid}/toggle")
def artist_toggle(aid: int, user: User = Depends(web_owner), session: Session = Depends(get_db)):
    a = _get(session, Artist, aid)
    a.active = not a.active
    return back("/seeds", f"{a.name}: {'included' if a.active else 'excluded'}")


# --- settings, users, runs, export --------------------------------------------------

SETTINGS_FORM = [
    ("Band", [("band_name", "Band name"), ("band_blurb", "About the band (1-2 sentences)"),
              ("band_sound_profile", "Sound profile used for taste scoring"), ("track_url", "Track link"),
              ("sender_name", "Your name"), ("signature", "Signature")]),
    ("Pitch", [("pitch_subject", "Subject template"), ("pitch_body", "Email template"), ("ask", "The ask"),
               ("opt_out_line", "Opt-out line"), ("opener_mode", "Opening line"), ("dm_template", "DM template")]),
    ("Sending", [("timezone", "Time zone"), ("timezone_mode", "Send in whose time zone"),
                 ("send_window_start", "Window start"), ("send_window_end", "Window end"), ("send_days", "Send days"),
                 ("daily_email_target", "Emails per day (target)"), ("warmup_start", "Warm-up: first day"),
                 ("warmup_days", "Warm-up: days to reach target"), ("min_gap_minutes", "Minimum gap (minutes)"),
                 ("require_approval", "I approve each email before it's sent"),
                 ("allow_outlet_inbox", "Use outlet inboxes when no personal email"),
                 ("daily_dm_target", "DMs per day"), ("sending_paused", "Pause all sending")]),
    ("Cooldowns", [("cooldown_days", "Days before re-pitching a person/address"),
                   ("outlet_cooldown_days", "Days before pitching the same outlet again (0 = off)")]),
    ("Discovery", [("daily_new_contacts_target", "Stop press discovery after this many new contacts a day (0 = no limit)"), ("search_daily_budget", "Web searches per day (press)"),
                   ("youtube_daily_searches", "YouTube searches per day"), ("tiktok_daily_searches", "TikTok web searches per day"),
                   ("playlist_daily_searches", "Spotify playlist searches per day"), ("max_pages_per_run", "Max pages fetched per run"),
                   ("similar_per_seed", "Similar artists per seed"), ("search_freshness", "Search freshness"),
                   ("press_languages", "Languages to search (ISO codes, one per line)"),
                   ("press_queries", "Search templates (lang: template, one per line)"),
                   ("blocked_domains", "Never fetch these domains (one per line)")]),
    ("Scoring and schedule", [("min_score_to_queue", "Minimum taste score to queue (0-1)"),
                              ("llm_score_top_n", "Contacts scored by the larger model per day"),
                              ("discovery_time", "Daily discovery at"), ("queue_time", "Build the daily queue at"),
                              ("reply_poll_minutes", "Check for replies every N minutes")]),
]
TEXTAREAS = {"band_sound_profile", "pitch_body", "dm_template", "signature", "band_blurb", "press_languages",
             "press_queries", "blocked_domains", "ask", "opt_out_line"}


def _field_kind(name: str) -> tuple[str, list[str]]:
    ann = AppSettings.model_fields[name].annotation
    if ann is bool:
        return "bool", []
    if ann in (int, float):
        return "number", []
    if name == "send_days":
        return "days", []
    if getattr(ann, "__origin__", None) is not None and str(ann).startswith("typing.Literal"):
        return "select", list(get_args(ann))
    if name in TEXTAREAS:
        return "textarea", []
    return "text", []


def _form_value(settings: AppSettings, name: str) -> object:
    v = getattr(settings, name)
    if name == "press_queries":
        return "\n".join(f"{lang}: {t}" for lang, ts in v.items() for t in ts)
    if isinstance(v, list) and name != "send_days":
        return "\n".join(v)
    return v


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: User = Depends(web_owner), session: Session = Depends(get_db), svc=Depends(get_svc)):
    settings = load_settings(session)
    groups = [(title, [(name, label, *_field_kind(name), _form_value(settings, name)) for name, label in fields])
              for title, fields in SETTINGS_FORM]
    users = list(session.scalars(select(User).order_by(User.id)))
    return page(request, "settings.html", user, session, settings=settings, groups=groups, users=users, days=DAYS,
                placeholders=PLACEHOLDERS, google=bool(svc.google and svc.google.connected()), error=None, new_token=None)


@router.post("/settings")
async def settings_save(request: Request, user: User = Depends(web_owner), session: Session = Depends(get_db)):
    form = await request.form()
    data = load_settings(session).model_dump()
    for _, fields in SETTINGS_FORM:
        for name, _label in fields:
            kind, _ = _field_kind(name)
            if kind == "bool":
                data[name] = form.get(name) == "on"
            elif kind == "days":
                data[name] = [int(d) for d in form.getlist(name)]
            elif name == "press_queries":
                queries: dict[str, list[str]] = {}
                for line in str(form.get(name, "")).splitlines():
                    if ":" in line:
                        lang, tpl = line.split(":", 1)
                        if tpl.strip():
                            queries.setdefault(lang.strip().lower(), []).append(tpl.strip())
                data[name] = queries
            elif name in ("press_languages", "blocked_domains"):
                data[name] = [x.strip() for x in str(form.get(name, "")).splitlines() if x.strip()]
            elif name in form:
                data[name] = form.get(name)
    try:
        save_settings(session, AppSettings.model_validate(data))
    except ValidationError as e:
        errors = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
        return back("/settings", f"Not saved - {errors}")
    return back("/settings", "Settings saved")


@router.post("/users")
def user_add(request: Request, name: str = Form(...), role: str = Form("member"), user: User = Depends(web_owner),
             session: Session = Depends(get_db), svc=Depends(get_svc)):
    from .deps import create_user

    new_user, token = create_user(session, name.strip(), role="owner" if role == "owner" else "member")
    return page(request, "message.html", user, session, title=f"Token for {new_user.name}",
                body=f"Give this to {new_user.name}. It's shown only once. They paste it into the Chrome extension "
                     "options and the phone app settings.", code=token)


@router.post("/users/{uid}/delete")
def user_delete(uid: int, user: User = Depends(web_owner), session: Session = Depends(get_db)):
    target = _get(session, User, uid)
    if target.id == user.id:
        return back("/settings", "You can't remove yourself")
    session.delete(target)
    return back("/settings", f"Removed {target.name}")


@router.get("/runs", response_class=HTMLResponse)
def runs(request: Request, user: User = Depends(web_user), session: Session = Depends(get_db)):
    rows = list(session.scalars(select(JobRun).order_by(JobRun.started_at.desc()).limit(60)))
    return page(request, "runs.html", user, session, rows=rows, jobs=list(JOBS))


@router.post("/runs/{job}")
def runs_start(request: Request, job: str, user: User = Depends(web_owner)):
    if job not in JOBS:
        raise HTTPException(404)
    views.run_in_background(request.app.state.db, request.app.state.svc, job)
    return back("/runs", f"Started {job}. Refresh in a bit to see the result.")


@router.get("/export.csv")
def export(user: User = Depends(web_owner), session: Session = Depends(get_db)):
    from ..export import write_contacts_csv

    buf = io.StringIO()
    write_contacts_csv(session, buf)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=press-contacts.csv"})


def _get(session: Session, model, pk: int):
    obj = session.get(model, pk)
    if obj is None:
        raise HTTPException(404, "Not found")
    return obj

