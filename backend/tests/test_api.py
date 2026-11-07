from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from press_engine.api.app import create_app
from press_engine.api.deps import create_user
from press_engine.appsettings import update_settings
from press_engine.models import Device, Outreach, OutreachStatus, Person, PersonStatus
from press_engine.outreach.gmail import GmailClient
from press_engine.outreach.queue import build_daily_queue
from press_engine.scheduler import Scheduler
from press_engine.scoring import score_heuristic

from . import factories as f
from .gmail_fake import GmailFake


@pytest.fixture
def app_client(db, services):
    app = create_app(db=db, svc=services, start_scheduler=False)
    with db.session() as s:
        _, owner = create_user(s, "D", role="owner")
        _, member = create_user(s, "Sofie", role="member")
    client = TestClient(app)
    client.tokens = {"owner": owner, "member": member}
    return client


def auth(client, who="owner"):
    return {"Authorization": f"Bearer {client.tokens[who]}"}


def seed_queue(db, now):
    with db.session() as s:
        update_settings(s, timezone="Europe/Copenhagen", band_name="Night Swim", min_score_to_queue=0.1)
        seed = f.artist(s, "Athletic Progression", seed=True)
        f.person(s, "Writer Ason", outlet="zinea.dk", email="w@zinea.dk", covers=[seed])
        f.person(s, "Tok Creator", kind="creator", covers=[seed], handles={"tiktok": "tokcreator"})
        score_heuristic(s, now=now)
        build_daily_queue(s, __import__("press_engine.appsettings", fromlist=["load_settings"]).load_settings(s), now=now, rng=random.Random(1))


def test_auth_required_and_roles(app_client):
    assert app_client.get("/api/health").json()["ok"]
    assert app_client.get("/api/today").status_code == 401
    assert app_client.get("/api/today", headers=auth(app_client, "member")).status_code == 200
    assert app_client.get("/api/inbox", headers=auth(app_client, "member")).status_code == 403
    assert app_client.get("/api/me", headers=auth(app_client)).json()["role"] == "owner"


def test_capture_endpoint_and_cors(app_client):
    r = app_client.post("/api/capture", headers=auth(app_client, "member"),
                        json={"url": "https://www.tiktok.com/@jazzjonny", "display_name": "Jazz Jonny", "source": "extension"})
    assert r.status_code == 200 and r.json()["created"] is True
    again = app_client.post("/api/capture", headers=auth(app_client, "member"), json={"url": "https://www.tiktok.com/@JazzJonny/video/1"})
    assert again.json()["created"] is False and again.json()["person_id"] == r.json()["person_id"]
    pre = app_client.options("/api/capture", headers={"Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
                                                     "Access-Control-Request-Method": "POST",
                                                     "Access-Control-Request-Headers": "authorization,content-type"})
    assert pre.headers.get("access-control-allow-origin") == "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
    evil = app_client.options("/api/capture", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in evil.headers


def test_queue_approve_dm_and_today(app_client, db):
    now = datetime(2026, 11, 2, 7, 0)
    seed_queue(db, now)
    today = app_client.get("/api/today", headers=auth(app_client)).json()
    assert len(today["awaiting_approval"]) == 1 and today["dm_queue"] == 1
    oid = today["awaiting_approval"][0]["id"]
    assert app_client.post(f"/api/outreach/{oid}/approve", headers=auth(app_client, "member")).status_code == 403
    r = app_client.post(f"/api/outreach/{oid}/approve", headers=auth(app_client), json={"body": "Edited body"})
    assert r.status_code == 200 and r.json()["status"] == "approved" and r.json()["body"] == "Edited body"

    dm = app_client.get("/api/dm-queue", headers=auth(app_client, "member")).json()[0]
    assert dm["platform"] == "tiktok" and dm["profile_url"]
    r = app_client.post(f"/api/outreach/{dm['id']}/dm-sent", headers=auth(app_client, "member"), json={})
    assert r.json()["status"] == "sent" and r.json()["sent_by"] == "Sofie"
    with db.session() as s:
        assert s.get(Outreach, dm["id"]).person.status == PersonStatus.PITCHED


def test_device_registration_people_and_meeting(app_client, db):
    r = app_client.post("/api/devices", headers=auth(app_client), json={"expo_push_token": "ExponentPushToken[xyz]", "platform": "ios"})
    assert r.json()["ok"]
    with db.session() as s:
        assert s.scalar(select(Device)).platform == "ios"
        p = f.person(s, "Mette Jensen", email="mette@jazznyt.dk")
        pid = p.id
    people = app_client.get("/api/people?q=mette", headers=auth(app_client)).json()
    assert people["total"] == 1 and people["items"][0]["email"] == "mette@jazznyt.dk"
    r = app_client.post(f"/api/people/{pid}/meetings", headers=auth(app_client),
                        json={"title": "Interview", "start": "2026-11-10T14:00:00", "duration_minutes": 30})
    assert r.status_code == 200  # saved locally (Google not connected in tests)
    detail = app_client.get(f"/api/people/{pid}", headers=auth(app_client)).json()
    assert detail["status"] == "in_conversation" and detail["meetings"][0]["title"] == "Interview"
    r = app_client.patch(f"/api/people/{pid}", headers=auth(app_client), json={"opted_out": True})
    assert r.json()["opted_out"] is True


def test_reply_from_app(app_client, db, services, router):
    gmail = GmailFake(router)
    services._gmail = GmailClient(lambda: "t", services.http)
    with db.session() as s:
        p = f.person(s, "Writer Bson", email="w@zineb.dk")
        o = Outreach(person=p, channel="email", to_address="w@zineb.dk", to_kind="personal", status=OutreachStatus.SENT,
                     subject="Night Swim", body="hi", sent_at=datetime(2026, 11, 2, 9), gmail_thread_id="t-1")
        s.add(o)
        s.flush()
        oid = o.id
    gmail.add_inbound(thread_id="t-1", from_addr="w@zineb.dk", subject="Re: Night Swim", body="Sure, send it!",
                      internal_ms=int(datetime(2026, 11, 2, 12).timestamp() * 1000))
    thread = app_client.get(f"/api/outreach/{oid}/thread", headers=auth(app_client)).json()
    assert thread["messages"][0]["text"] == "Sure, send it!"
    assert "Thanks so much" in app_client.post(f"/api/outreach/{oid}/draft", headers=auth(app_client)).json()["draft"]
    r = app_client.post(f"/api/outreach/{oid}/reply", headers=auth(app_client), json={"body": "Here you go"})
    assert r.json()["person_status"] == "in_conversation"
    assert gmail.sent[-1]["payload"]["threadId"] == "t-1"


def test_dashboard_pages_render(app_client, db):
    now = datetime(2026, 11, 2, 7, 0)
    seed_queue(db, now)
    assert app_client.get("/", follow_redirects=False).status_code == 303  # not logged in
    r = app_client.post("/login", data={"token": app_client.tokens["owner"], "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    with db.session() as s:
        pid = s.scalar(select(Person.id))
    for path in ["/", "/contacts", f"/contacts/{pid}", "/dms", "/inbox", "/merges", "/seeds", "/settings", "/runs", "/export.csv"]:
        resp = app_client.get(path)
        assert resp.status_code == 200, path
    assert "Writer Ason" in app_client.get("/").text
    assert "Writer Ason" in app_client.get("/export.csv").text

    # Settings form round-trip, including validation errors.
    form = {"band_name": "Night Swim", "timezone": "Europe/Copenhagen", "send_window_start": "10:00", "send_window_end": "16:00",
            "send_days": ["0", "1", "2"], "require_approval": "on", "pitch_subject": "{band_name} for {first_name}",
            }
    assert app_client.post("/settings", data=form, follow_redirects=False).status_code == 303
    press = {"press_queries": "en: \"{artist}\" review\nda: \"{artist}\" anmeldelse", "press_languages": "en\nda",
             "search_daily_budget": "60", "daily_new_contacts_target": "20", "max_pages_per_run": "150", "search_freshness": "year"}
    assert app_client.post("/discovery/press/settings", data=press, follow_redirects=False).status_code == 303
    with db.session() as s:
        from press_engine.appsettings import load_settings

        st = load_settings(s)
        assert st.send_days == [0, 1, 2] and st.send_window_start == "10:00" and st.require_approval
        assert st.press_queries == {"en": ['"{artist}" review'], "da": ['"{artist}" anmeldelse']}
        assert st.allow_outlet_inbox is False  # unchecked checkbox
    bad = app_client.post("/settings", data={**form, "pitch_subject": "{nope}"}, follow_redirects=False)
    assert "Not+saved" in bad.headers["location"] or "Not%20saved" in bad.headers["location"]

    # Members can't open owner pages.
    app_client.cookies.clear()
    app_client.post("/login", data={"token": app_client.tokens["member"], "next": "/"})
    assert app_client.get("/settings").status_code == 403
    assert app_client.get("/dms").status_code == 200


def test_scheduler_daily_jobs_run_once_per_day(db, services):
    clock = {"now": datetime(2026, 11, 2, 1, 0)}  # 02:00 Copenhagen
    with db.session() as s:
        update_settings(s, timezone="Europe/Copenhagen", discovery_time="03:00", queue_time="07:30")
    sched = Scheduler(db, services, clock=lambda: clock["now"])
    assert not sched.daily_due("discover", "03:00", "Europe/Copenhagen", clock["now"])
    clock["now"] = datetime(2026, 11, 2, 2, 30)  # 03:30 local
    sched.slow_tick()
    from press_engine.jobs import last_run

    with db.session() as s:
        assert last_run(s, "discover") is not None and last_run(s, "queue") is None
    assert not sched.daily_due("discover", "03:00", "Europe/Copenhagen", clock["now"] + timedelta(hours=5))
    assert sched.daily_due("discover", "03:00", "Europe/Copenhagen", clock["now"] + timedelta(days=1))
    clock["now"] = datetime(2026, 11, 2, 7, 0)
    sched.slow_tick()
    with db.session() as s:
        assert last_run(s, "queue") is not None
    sched.fast_tick()  # Google not connected: quiet, no crash
