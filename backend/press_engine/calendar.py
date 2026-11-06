"""Google Calendar integration (spec §4.10): meetings linked to contacts."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from .models import IdentityType, Meeting, Person
from .util.timeutil import to_utc_naive

EVENTS = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


class CalendarError(RuntimeError):
    pass


class CalendarClient:
    def __init__(self, token_provider: Callable[[], str], http: httpx.Client):
        self.token_provider = token_provider
        self.http = http

    def _req(self, method: str, url: str, **kw) -> dict:
        try:
            r = self.http.request(method, url, headers={"Authorization": f"Bearer {self.token_provider()}"}, timeout=30, **kw)
        except httpx.HTTPError as e:
            raise CalendarError(f"Google Calendar unreachable: {e}") from e
        if r.status_code >= 400:
            raise CalendarError(f"Google Calendar {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    def create_event(self, *, summary: str, start: datetime, end: datetime, tz: str, description: str = "",
                     attendees: list[str] | None = None, with_meet: bool = False, location: str | None = None) -> dict:
        body: dict = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": tz},
            "end": {"dateTime": end.isoformat(), "timeZone": tz},
        }
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
        params = {"sendUpdates": "all" if attendees else "none"}
        if with_meet:
            body["conferenceData"] = {"createRequest": {"requestId": uuid.uuid4().hex,
                                                         "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
            params["conferenceDataVersion"] = "1"
        return self._req("POST", EVENTS, params=params, json=body)

    def delete_event(self, event_id: str) -> None:
        self._req("DELETE", f"{EVENTS}/{event_id}", params={"sendUpdates": "all"})


def create_meeting(session: Session, svc, person: Person, *, title: str, start: datetime, duration_minutes: int, tz: str,
                   notes: str = "", invite: bool = False, with_meet: bool = False, location: str | None = None,
                   user_id: int | None = None) -> Meeting:
    """``start`` is timezone-aware. Saves locally even if Google isn't connected."""
    end = start + timedelta(minutes=duration_minutes)
    event: dict = {}
    cal = svc.calendar()
    if cal is not None:
        attendees = []
        if invite:
            email = next((i.value for i in person.identities if i.type == IdentityType.EMAIL and i.verified), None)
            if email:
                attendees.append(email)
        event = cal.create_event(summary=title, start=start, end=end, tz=tz, attendees=attendees, with_meet=with_meet,
                                 location=location, description=f"{notes}\n\nContact: {person.name}".strip())
    meeting = Meeting(person_id=person.id, title=title, starts_at=to_utc_naive(start), ends_at=to_utc_naive(end),
                      location=location, notes=notes or None, created_by_user_id=user_id,
                      calendar_event_id=event.get("id"), html_link=event.get("htmlLink"), meet_url=event.get("hangoutLink"))
    session.add(meeting)
    session.flush()
    session.refresh(person, ["meetings"])
    return meeting


def cancel_meeting(session: Session, svc, meeting: Meeting) -> None:
    cal = svc.calendar()
    if cal is not None and meeting.calendar_event_id:
        cal.delete_event(meeting.calendar_event_id)
    session.delete(meeting)
    session.flush()
