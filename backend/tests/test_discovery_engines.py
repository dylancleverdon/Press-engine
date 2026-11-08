"""Discovery tab: per-engine runs, vetting and the pages (suggestions #5-#10)."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import unquote

import pytest
from sqlalchemy import select

from press_engine.appsettings import load_settings, update_settings
from press_engine.discovery import engines as eng
from press_engine.discovery.youtube import YouTubeClient
from press_engine.jobs import job_discover
from press_engine.models import Person, Suggestion
from press_engine.scoring import score_heuristic
from press_engine.api import views

from . import factories as f

NOW = datetime(2026, 11, 2, 7, 0)
YT = "https://www.googleapis.com/youtube/v3"


@pytest.fixture
def youtube(services, router):
    router.json("GET", f"{YT}/search", {"items": [
        {"id": {"videoId": "v1"}, "snippet": {"channelId": "UCrev", "channelTitle": "Jazz Reviews", "title": "Mammal Hands review",
                                              "publishedAt": "2026-06-01T10:00:00Z"}},
        {"id": {"videoId": "v2"}, "snippet": {"channelId": "UCbig", "channelTitle": "Huge Channel", "title": "Mammal Hands reaction",
                                              "publishedAt": "2026-06-01T10:00:00Z"}},
    ]})
    router.json("GET", f"{YT}/channels", {"items": [
        {"id": "UCrev", "snippet": {"title": "Jazz Reviews", "description": "hello@jazzreviews.co.uk"}, "statistics": {"subscriberCount": "12000"}},
        {"id": "UCbig", "snippet": {"title": "Huge Channel", "description": ""}, "statistics": {"subscriberCount": "5000000"}},
    ]})
    services.youtube = YouTubeClient("k", services.http)
    return services


def test_engine_finds_wait_for_vetting_and_skip_the_queue(session, youtube):
    f.artist(session, "Mammal Hands", seed=True)
    update_settings(session, youtube_max_subscribers=1_000_000)
    stats = job_discover(session, youtube, load_settings(session), NOW, sources=("youtube",))
    assert set(stats) >= {"youtube"} and "press" not in stats  # only the engine asked for
    assert stats["youtube"]["waiting_for_review"] == 1
    people = list(session.scalars(select(Person)))
    assert [p.name for p in people] == ["Jazz Reviews"]  # the 5M-subscriber channel is over the limit
    p = people[0]
    assert p.vetting == "pending"
    assert views.search_people(session)[1] == 0  # not in Contacts yet
    score_heuristic(session, now=NOW)
    yt = eng.ENGINES["youtube"]
    assert eng.counts(session, yt)["pending"] == 1

    eng.approve(p)
    assert views.search_people(session)[1] == 1
    eng.reject(p)
    assert eng.counts(session, yt) == {"pending": 0, "approved": 0, "rejected": 1, "total": 1}
    # A later run finds the same channel again: it stays rejected rather than coming back.
    session.commit()
    job_discover(session, youtube, load_settings(session), NOW, sources=("youtube",))
    assert session.get(Person, p.id).vetting == "rejected" and session.scalar(select(Person).where(Person.id != p.id)) is None


def test_auto_approve_adds_straight_to_contacts(session, youtube):
    f.artist(session, "Mammal Hands", seed=True)
    update_settings(session, youtube_auto_approve=True)
    stats = job_discover(session, youtube, load_settings(session), NOW, sources=("youtube",))
    assert "waiting_for_review" not in stats["youtube"]
    assert {p.vetting for p in session.scalars(select(Person))} == {"approved"}


def test_existing_contacts_and_captures_count_as_approved(session):
    p = f.person(session, "Old Contact", email="old@example.com")
    assert p.vetting == "approved"
    p.found_via = "capture_extension"
    assert eng.engine_for(p).key == "captures"


def test_discovery_pages_nav_and_vetting(db, services, session, youtube):
    from fastapi.testclient import TestClient

    from press_engine.api.app import create_app
    from press_engine.api.deps import create_user

    session.commit()
    with db.session() as s:
        _, token = create_user(s, "D", role="owner")
        f.artist(s, "Mammal Hands", seed=True)
        s.add(Suggestion(text="line one\nline two"))
    with db.session() as s:
        job_discover(s, youtube, load_settings(s), NOW, sources=("youtube",))
        pid = s.scalar(select(Person.id).where(Person.name == "Jazz Reviews"))
    client = TestClient(create_app(db=db, svc=youtube, start_scheduler=False))
    client.post("/login", data={"token": token, "next": "/"})

    home = client.get("/").text
    nav = home[home.index("<nav>"):home.index("</nav>")]
    order = [nav.index(x) for x in (">Today<", ">Discovery<", ">Contacts<", ">Inbox<", 'aria-label="Settings"', 'aria-label="Refresh this page"')]
    assert order == sorted(order)
    assert ">Runs<" not in nav and ">Connections<" not in nav and ">DM queue<" not in nav and ">Duplicates<" not in nav
    assert 'href="/merges"' in client.get("/contacts").text  # Duplicates lives under Contacts
    assert 'href="/dms"' in client.get("/inbox").text  # DM queue under Inbox
    settings_page = client.get("/settings").text
    assert 'href="/connections"' in settings_page and 'href="/runs"' in settings_page

    overview = client.get("/discovery")
    assert overview.status_code == 200 and "2 to vet" in overview.text and "via API key" in overview.text
    for key in eng.ENGINES:
        assert client.get(f"/discovery/{key}").status_code == 200
    page = client.get("/discovery/youtube").text
    assert "Jazz Reviews" in page and "Mammal Hands" in page and "Run YouTube now" in page and "Auto-approve" in page

    r = client.post(f"/discovery/youtube/vet?one={pid}", data={"decision": "approve"}, follow_redirects=False)
    assert "added to Contacts" in unquote(r.headers["location"])
    assert "Jazz Reviews" in client.get("/contacts").text

    r = client.post("/discovery/youtube/settings", data={"youtube_min_subscribers": "500", "youtube_auto_approve": "on",
                                                        "youtube_daily_searches": "20", "youtube_max_subscribers": "0"},
                    follow_redirects=False)
    assert r.status_code == 303
    with db.session() as s:
        st = load_settings(s)
        assert (st.youtube_min_subscribers, st.youtube_auto_approve, st.youtube_daily_searches) == (500, True, 20)
        assert st.band_name == "Our Band"  # other settings untouched

    r = client.post("/discovery/youtube/run", follow_redirects=False)
    assert "Running the YouTube engine" in unquote(r.headers["location"])
    assert 'class="grow pre-wrap"' in client.get("/suggestions").text  # suggestion line breaks shown


def test_static_assets_are_versioned_and_revalidated(db, services):
    from fastapi.testclient import TestClient

    from press_engine import __version__
    from press_engine.api.app import create_app

    client = TestClient(create_app(db=db, svc=services, start_scheduler=False))
    assert f"/static/style.css?v={__version__}" in client.get("/login").text
    assert client.get("/static/style.css").headers["cache-control"] == "no-cache"


def test_links_in_a_description_dont_merge_channels(session):
    """The Office Drummer glitch: a channel linking another channel or a producer's
    email must not swallow that contact; it goes to Duplicates instead."""
    from press_engine.contacts.creators import ingest_profile_text, upsert_creator
    from press_engine.models import MergeCandidate
    from press_engine.util.urls import SocialRef

    beats, _ = upsert_creator(session, SocialRef("youtube", "UCbeats", "UCbeats", "https://www.youtube.com/channel/UCbeats"),
                              name="Type Beats", found_via="youtube")
    beats = ingest_profile_text(session, beats, "Prod. by me: lilskam4k@gmail.com", source_url="https://www.youtube.com/channel/UCbeats")
    drummer, _ = upsert_creator(session, SocialRef("youtube", "UCdrum", "UCdrum", "https://www.youtube.com/channel/UCdrum"),
                                name="Office Drummer Daily", found_via="youtube")
    drummer = ingest_profile_text(session, drummer, "Beats: https://www.youtube.com/channel/UCbeats lilskam4k@gmail.com",
                                  source_url="https://www.youtube.com/channel/UCdrum")
    assert beats.id != drummer.id and session.get(Person, beats.id) is not None
    assert {i.value for i in drummer.identities} == {"UCdrum"}
    assert eng.merged_channels(drummer) == 1
    assert session.scalar(select(MergeCandidate).where(MergeCandidate.person_b_id == drummer.id)) is not None


def test_youtube_skips_unrelated_and_live_videos(session, services, router):
    from press_engine.discovery.youtube import run_youtube_discovery

    f.artist(session, "Crumb", seed=True)
    router.json("GET", f"{YT}/search", {"items": [
        {"id": {"videoId": "a"}, "snippet": {"channelId": "UCreact", "channelTitle": "Office Drummer Daily", "title": "FIRST TIME Hearing YES"}},
        {"id": {"videoId": "b"}, "snippet": {"channelId": "UClive", "channelTitle": "Gig Filmer", "title": "Crumb - Live at Primavera 2025 (full set)"}},
        {"id": {"videoId": "c"}, "snippet": {"channelId": "UCrev", "channelTitle": "Indie Reviews", "title": "Crumb - AMAMA album review"}},
    ]})
    router.json("GET", f"{YT}/channels", {"items": [
        {"id": "UCrev", "snippet": {"title": "Indie Reviews"}, "statistics": {"subscriberCount": "9000"}}]})
    services.youtube = YouTubeClient("k", services.http)
    stats = run_youtube_discovery(session, services, load_settings(session), now=NOW, per_artist=1)
    assert stats["skipped_videos"] == 2
    assert [p.name for p in session.scalars(select(Person))] == ["Indie Reviews"]
    asked = router.requests_to(f"{YT}/channels")[0].url.params["id"]
    assert asked == "UCrev"  # skipped videos' channels aren't even looked up

    # Older finds waiting to be vetted are cleaned up with the same rules.
    from press_engine.contacts.ingest import add_coverage

    p = session.scalar(select(Person))
    p.vetting = "pending"
    add_coverage(session, p, url="https://www.youtube.com/watch?v=x", artist_name="Crumb", title="FIRST TIME Hearing YES", source="youtube")
    session.flush()
    st = eng.recheck_youtube(session, load_settings(session))
    assert st["videos_dropped"] == 1 and st["rejected"] == 0 and len(p.coverage) == 1


def test_link_pages_embedded_links_and_twitch():
    from press_engine.discovery.pages import parse_html
    from press_engine.util.urls import parse_social

    html = ('<html><body><script id="__NEXT_DATA__">{"links":[{"url":"https:\\/\\/www.youtube.com\\/@officedrummer"},'
            '{"url":"https://www.twitch.tv/OfficeDrummer"},{"url":"https://officedrummer.com/booking"},'
            '{"url":"https://linktr.ee/static/app.js"}]}</script></body></html>')
    links = {l["href"]: l for l in parse_html(html, "https://linktr.ee/officedrummer")["links"]}
    assert links["https://www.youtube.com/@officedrummer"]["type"] == "youtube"
    assert links["https://www.twitch.tv/OfficeDrummer"]["type"] == "twitch"
    assert "https://officedrummer.com/booking" in links and not any("static" in h for h in links)
    assert parse_html(html, "https://example.com/post")["links"] == []  # only link pages are read this way
    assert parse_social("https://www.twitch.tv/directory") is None


def test_report_twitch_and_back_button_pages(db, services, session, youtube):
    from fastapi.testclient import TestClient

    from press_engine.api.app import create_app
    from press_engine.api.deps import create_user

    session.commit()
    with db.session() as s:
        _, token = create_user(s, "D", role="owner")
        f.artist(s, "Mammal Hands", seed=True)
    with db.session() as s:
        job_discover(s, youtube, load_settings(s), NOW, sources=("youtube",))
        pid = s.scalar(select(Person.id).where(Person.name == "Jazz Reviews"))
    client = TestClient(create_app(db=db, svc=youtube, start_scheduler=False))
    client.post("/login", data={"token": token, "next": "/"})

    assert 'aria-label="Go back"' in client.get("/").text
    twitch = client.get("/discovery/twitch").text
    assert "Coming soon" in twitch and "Run Twitch now" not in twitch
    assert "coming soon" in client.get("/discovery").text
    assert f"/contacts/{pid}/report" in client.get("/discovery/youtube").text

    form = client.get(f"/contacts/{pid}/report?next=/discovery/youtube")
    assert form.status_code == 200 and "hello@jazzreviews.co.uk" in form.text and "Mammal Hands" in form.text
    r = client.post(f"/contacts/{pid}/report", data={"text": "That email isn't theirs", "next": "/discovery/youtube"},
                    files=[("images", ("shot.png", b"\x89PNG x", "image/png"))], follow_redirects=False)
    assert r.headers["location"].startswith("/discovery/youtube") and "Reported as suggestion" in unquote(r.headers["location"])
    with db.session() as s:
        sug = s.scalar(select(Suggestion))
        assert sug.area == "Discovery report" and "That email isn't theirs" in sug.text
        assert "Contact #" in sug.details and "hello@jazzreviews.co.uk" in sug.details and len(sug.images) == 1
        sid = sug.id
    page = client.get("/suggestions").text
    assert f"/suggestions/{sid}/images/1" in page and "Details attached" in page
    assert client.get(f"/suggestions/{sid}/images/1").content == b"\x89PNG x"
