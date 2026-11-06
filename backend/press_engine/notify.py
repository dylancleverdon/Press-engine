"""Push notifications to the mobile app via the Expo push service."""

from __future__ import annotations

import logging

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Device, User

log = logging.getLogger(__name__)


def push(session: Session, http: httpx.Client, url: str, *, title: str, body: str, data: dict | None = None,
         owners_only: bool = True) -> int:
    """Send to every registered device of users who want reply notifications."""
    q = select(Device).join(User).where(User.notify_replies.is_(True))
    if owners_only:
        q = q.where(User.role == "owner")
    devices = list(session.scalars(q))
    if not devices:
        return 0
    messages = [{"to": d.expo_push_token, "title": title, "body": body[:180], "data": data or {}, "sound": "default"}
                for d in devices]
    try:
        r = http.post(url, json=messages, headers={"Accept": "application/json"}, timeout=15)
        tickets = r.json().get("data", []) if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError) as e:
        log.warning("push failed: %s", e)
        return 0
    sent = 0
    for device, ticket in zip(devices, tickets):
        if ticket.get("status") == "ok":
            sent += 1
        elif (ticket.get("details") or {}).get("error") == "DeviceNotRegistered":
            session.delete(device)
    session.flush()
    return sent
