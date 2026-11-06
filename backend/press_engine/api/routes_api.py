"""JSON API used by the Chrome extension and the mobile app."""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import __version__
from ..appsettings import load_settings
from ..calendar import CalendarError, create_meeting
from ..capture import CaptureIn, CaptureOut, capture
from ..contacts.resolve import resolve_person
from ..discovery.lastfm import add_seed, remove_seed
from ..jobs import JOBS
from ..llm import LLMError
from ..models import STATUS_RANK, Artist, Device, InboundMessage, Outreach, Person, PersonStatus, User
from ..outreach import queue as outq
from ..outreach.gmail import GmailError
from ..outreach.google_auth import GoogleAuthError
from ..outreach.replies import send_reply, suggest_reply, thread_messages
from ..util.timeutil import get_tz, utcnow
from . import views
from .deps import api_owner, api_user, get_db, get_svc

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


@router.get("/health")
def health():
    return {"ok": True, "version": __version__}


@router.get("/me")
def me(user: User = Depends(api_user), svc=Depends(get_svc)):
    return {"id": user.id, "name": user.name, "role": user.role,
            "google_connected": bool(svc.google and svc.google.connected())}


def _resolve_later(db, svc, person_id: int) -> None:
    with db.session() as s:
        person = s.get(Person, person_id)
        if person is not None:
            try:
                resolve_person(s, svc, person)
            except Exception:  # best effort
                log.exception("background resolution failed for %s", person_id)


@router.post("/capture", response_model=CaptureOut)
def capture_creator(data: CaptureIn, request: Request, background: BackgroundTasks, user: User = Depends(api_user),
                    session: Session = Depends(get_db), svc=Depends(get_svc)):
    person, out = capture(session, svc.http, user, data, now=utcnow())
    session.commit()
    background.add_task(_resolve_later, request.app.state.db, svc, person.id)
    return out


@router.get("/today")
def today(user: User = Depends(api_user), session: Session = Depends(get_db), svc=Depends(get_svc)):
    return views.today_summary(session, load_settings(session), svc)


@router.get("/outreach")
def list_outreach(channel: str | None = None, status: str | None = None, limit: int = 50,
                  user: User = Depends(api_user), session: Session = Depends(get_db)):
    stmt = select(Outreach).order_by(Outreach.id.desc()).limit(min(limit, 200))
    if channel:
        stmt = stmt.where(Outreach.channel == channel)
    if status:
        stmt = stmt.where(Outreach.status == status)
    return [views.outreach_dict(o) for o in session.scalars(stmt)]


def _outreach(session: Session, outreach_id: int) -> Outreach:
    o = session.get(Outreach, outreach_id)
    if o is None:
        raise HTTPException(404, "Not found")
    return o


class ApproveIn(BaseModel):
    subject: str | None = None
    body: str | None = None


@router.post("/outreach/{outreach_id}/approve")
def approve(outreach_id: int, data: ApproveIn | None = None, user: User = Depends(api_owner),
            session: Session = Depends(get_db), svc=Depends(get_svc)):
    o = _outreach(session, outreach_id)
    try:
        outq.approve(session, o, load_settings(session), now=utcnow(), rng=svc.rng, user_id=user.id,
                  subject=data.subject if data else None, body=data.body if data else None)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return views.outreach_dict(o)


@router.post("/outreach/{outreach_id}/skip")
def skip(outreach_id: int, user: User = Depends(api_owner), session: Session = Depends(get_db)):
    o = _outreach(session, outreach_id)
    try:
        outq.skip(session, o, load_settings(session), now=utcnow())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return views.outreach_dict(o)


class DmSentIn(BaseModel):
    body: str | None = None


@router.get("/dm-queue")
def dm_queue(user: User = Depends(api_user), session: Session = Depends(get_db)):
    return [views.outreach_dict(o) for o in views.dm_queue(session)]


@router.post("/outreach/{outreach_id}/dm-sent")
def dm_sent(outreach_id: int, data: DmSentIn | None = None, user: User = Depends(api_user), session: Session = Depends(get_db)):
    o = _outreach(session, outreach_id)
    try:
        outq.mark_dm_sent(session, o, user_id=user.id, now=utcnow(), settings=load_settings(session),
                       body=data.body if data else None)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return views.outreach_dict(o)


@router.get("/inbox")
def inbox(limit: int = 50, user: User = Depends(api_owner), session: Session = Depends(get_db)):
    rows = session.scalars(select(InboundMessage).where(InboundMessage.kind == "reply")
                           .order_by(InboundMessage.received_at.desc()).limit(min(limit, 200)))
    return [views.inbound_dict(m) for m in rows]


@router.post("/inbox/{message_id}/read")
def mark_read(message_id: int, user: User = Depends(api_owner), session: Session = Depends(get_db)):
    m = session.get(InboundMessage, message_id)
    if m is None:
        raise HTTPException(404, "Not found")
    m.read = True
    return {"ok": True}


def _google_call(fn):
    try:
        return fn()
    except (GmailError, GoogleAuthError, CalendarError) as e:
        raise HTTPException(502, str(e)) from e


@router.get("/outreach/{outreach_id}/thread")
def thread(outreach_id: int, user: User = Depends(api_owner), session: Session = Depends(get_db), svc=Depends(get_svc)):
    o = _outreach(session, outreach_id)
    return {"outreach": views.outreach_dict(o), "messages": _google_call(lambda: thread_messages(svc, o))}


@router.post("/outreach/{outreach_id}/draft")
def draft(outreach_id: int, user: User = Depends(api_owner), session: Session = Depends(get_db), svc=Depends(get_svc)):
    o = _outreach(session, outreach_id)
    messages = _google_call(lambda: thread_messages(svc, o))
    try:
        text = suggest_reply(svc, load_settings(session), o.person, messages)
    except LLMError as e:
        raise HTTPException(502, str(e)) from e
    return {"draft": text}


class ReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=20000)


@router.post("/outreach/{outreach_id}/reply")
def reply(outreach_id: int, data: ReplyIn, user: User = Depends(api_owner), session: Session = Depends(get_db),
          svc=Depends(get_svc)):
    o = _outreach(session, outreach_id)
    _google_call(lambda: send_reply(session, svc, load_settings(session), o, data.body, now=utcnow()))
    return {"ok": True, "person_status": o.person.status}


@router.get("/people")
def people(q: str | None = None, status: str | None = None, kind: str | None = None, limit: int = 50, offset: int = 0,
           user: User = Depends(api_user), session: Session = Depends(get_db)):
    rows, total = views.search_people(session, q=q, status=status, kind=kind, limit=min(limit, 200), offset=offset)
    return {"total": total, "items": [views.person_brief(p) for p in rows]}


def _person(session: Session, person_id: int) -> Person:
    p = session.get(Person, person_id)
    if p is None:
        raise HTTPException(404, "Not found")
    return p


@router.get("/people/{person_id}")
def person(person_id: int, user: User = Depends(api_user), session: Session = Depends(get_db)):
    return views.person_detail(_person(session, person_id))


class PersonPatch(BaseModel):
    status: PersonStatus | None = None
    notes: str | None = None
    opted_out: bool | None = None


@router.patch("/people/{person_id}")
def update_person(person_id: int, data: PersonPatch, user: User = Depends(api_owner), session: Session = Depends(get_db)):
    p = _person(session, person_id)
    if data.status is not None:
        p.status = data.status
    if data.notes is not None:
        p.notes = data.notes
    if data.opted_out:
        outq.add_opt_out(session, p, reason=f"marked by {user.name}", source="manual", now=utcnow())
    return views.person_detail(p)


class MeetingIn(BaseModel):
    title: str
    start: datetime  # ISO 8601; naive values are read in the app's time zone
    duration_minutes: int = Field(default=30, ge=5, le=600)
    notes: str = ""
    invite: bool = False
    with_meet: bool = True
    location: str | None = None


@router.post("/people/{person_id}/meetings")
def add_meeting(person_id: int, data: MeetingIn, user: User = Depends(api_owner), session: Session = Depends(get_db),
                svc=Depends(get_svc)):
    p = _person(session, person_id)
    settings = load_settings(session)
    start = data.start if data.start.tzinfo else data.start.replace(tzinfo=get_tz(settings.timezone))
    m = _google_call(lambda: create_meeting(session, svc, p, title=data.title, start=start, duration_minutes=data.duration_minutes,
                                            tz=settings.timezone, notes=data.notes, invite=data.invite,
                                            with_meet=data.with_meet, location=data.location, user_id=user.id))
    if STATUS_RANK.get(p.status, 0) < STATUS_RANK[PersonStatus.IN_CONVERSATION]:
        p.status = PersonStatus.IN_CONVERSATION
    return {"id": m.id, "calendar_event_id": m.calendar_event_id, "meet_url": m.meet_url, "html_link": m.html_link}


class DeviceIn(BaseModel):
    expo_push_token: str = Field(min_length=10, max_length=255)
    platform: str | None = None


@router.post("/devices")
def register_device(data: DeviceIn, user: User = Depends(api_user), session: Session = Depends(get_db)):
    d = session.scalar(select(Device).where(Device.expo_push_token == data.expo_push_token))
    if d is None:
        d = Device(user_id=user.id, expo_push_token=data.expo_push_token, platform=data.platform)
        session.add(d)
    d.user_id, d.platform, d.last_seen_at = user.id, data.platform, utcnow()
    return {"ok": True}


class NotifyIn(BaseModel):
    notify_replies: bool


@router.post("/me/notifications")
def set_notifications(data: NotifyIn, user: User = Depends(api_user), session: Session = Depends(get_db)):
    session.get(User, user.id).notify_replies = data.notify_replies
    return {"ok": True}


@router.get("/seeds")
def seeds(user: User = Depends(api_user), session: Session = Depends(get_db)):
    rows = session.scalars(select(Artist).where(Artist.source == "manual").order_by(Artist.name))
    return [{"id": a.id, "name": a.name, "active": a.active, "expanded_at": views.iso(a.expanded_at)} for a in rows]


class SeedIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)


@router.post("/seeds")
def add_seed_api(data: SeedIn, user: User = Depends(api_owner), session: Session = Depends(get_db)):
    a = add_seed(session, data.name)
    return {"id": a.id, "name": a.name}


@router.delete("/seeds/{artist_id}")
def remove_seed_api(artist_id: int, user: User = Depends(api_owner), session: Session = Depends(get_db)):
    a = session.get(Artist, artist_id)
    if a is None or a.source != "manual":
        raise HTTPException(404, "Not a seed artist")
    remove_seed(session, a)
    return {"ok": True}


@router.post("/jobs/{name}", status_code=202)
def run_job_api(name: str, request: Request, user: User = Depends(api_owner)):
    if name not in JOBS:
        raise HTTPException(404, f"Unknown job. Choose from: {', '.join(JOBS)}")
    views.run_in_background(request.app.state.db, request.app.state.svc, name)
    return {"started": name}
