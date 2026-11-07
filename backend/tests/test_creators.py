from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from press_engine.appsettings import load_settings
from press_engine.calendar import CalendarClient, create_meeting
from press_engine.capture import CaptureIn, capture, parse_count
from press_engine.discovery.playlists import SpotifyClient, run_playlist_discovery
from press_engine.discovery.search import BraveSearch
from press_engine.discovery.tiktok import run_tiktok_discovery
from press_engine.discovery.youtube import YouTubeClient, run_youtube_discovery
from press_engine.models import DiscoveryLink, Identity, Person, User

from . import factories as f

NOW = datetime(2026, 11, 2, 7, 0)


def test_youtube_engine_creates_creators_with_description_email(session, services, router):
    f.artist(session, "Mammal Hands", seed=True)
    router.json("GET", "https://www.googleapis.com/youtube/v3/search", {"items": [
        {"id": {"videoId": "v1"}, "snippet": {"channelId": "UCreviewer", "channelTitle": "Jazz Reviews", "title": "Mammal Hands - Floa REVIEW", "publishedAt": "2026-06-01T10:00:00Z"}},
        {"id": {"videoId": "v2"}, "snippet": {"channelId": "UCtopic", "channelTitle": "Mammal Hands - Topic", "title": "Floa", "publishedAt": "2026-06-01T10:00:00Z"}},
    ]})
    router.json("GET", "https://www.googleapis.com/youtube/v3/channels", {"items": [
        {"id": "UCreviewer", "snippet": {"title": "Jazz Reviews", "customUrl": "@jazzreviews", "country": "GB",
                                          "description": "Reviews every week. Business: hello@jazzreviews.co.uk\nhttps://www.instagram.com/jazzreviews"},
         "statistics": {"subscriberCount": "12000"}},
    ]})
    services.youtube = YouTubeClient("k", services.http)
    stats = run_youtube_discovery(session, services, load_settings(session), now=NOW)
    assert stats["new_people"] == 1
    p = session.scalar(select(Person).where(Person.name == "Jazz Reviews"))
    idents = {(i.type, i.value, i.verified) for i in p.identities}
    assert ("youtube", "UCreviewer", False) in idents and ("youtube", "@jazzreviews", False) in idents
    assert ("email", "hello@jazzreviews.co.uk", True) in idents  # their own business inbox
    assert ("instagram", "jazzreviews", False) in idents
    assert p.followers == 12000 and p.coverage[0].url == "https://www.youtube.com/watch?v=v1"


def test_tiktok_via_search_never_fetches_tiktok(session, services, router):
    f.artist(session, "Mammal Hands", seed=True)
    router.json("GET", "https://api.search.brave.com/", {"web": {"results": [
        {"url": "https://www.tiktok.com/@jazzjonny/video/7311", "title": "Mammal Hands edit", "description": "using Floa by Mammal Hands"},
        {"url": "https://www.tiktok.com/@mammalhands/video/7312", "title": "Our new single", "description": ""},
        {"url": "https://www.tiktok.com/music/Floa-7299", "title": "Floa - Mammal Hands | TikTok", "description": ""},
    ]}})
    services.search = BraveSearch("k", services.http)
    stats = run_tiktok_discovery(session, services, load_settings(session), now=NOW)
    assert stats["new_people"] == 1 and stats["sound_pages"] == 1
    jonny = session.scalar(select(Identity).where(Identity.type == "tiktok")).person
    assert jonny.name == "@jazzjonny" and jonny.coverage[0].kind == "tiktok"
    assert session.scalar(select(DiscoveryLink)).kind == "tiktok_sound"
    assert not router.requests_to("https://www.tiktok.com")


def test_spotify_playlists_only_with_contact_and_artist_present(session, services, router):
    f.artist(session, "Mammal Hands", seed=True)
    router.json("POST", "https://accounts.spotify.com/api/token", {"access_token": "t", "expires_in": 3600})
    router.json("GET", "https://api.spotify.com/v1/search", {"playlists": {"items": [
        None,
        {"id": "p1", "name": "Spiritual jazz now", "description": "Submissions: curator.kim@gmail.com",
         "owner": {"id": "kim", "display_name": "Kim"}, "external_urls": {"spotify": "https://open.spotify.com/playlist/p1"}},
        {"id": "p2", "name": "No contact", "description": "just vibes", "owner": {"id": "x"}},
        {"id": "p3", "name": "Editorial", "description": "hi@spotify.com", "owner": {"id": "spotify"}},
    ]}})
    router.json("GET", "https://api.spotify.com/v1/playlists/p1/tracks", {"items": [{"track": {"artists": [{"name": "Mammal Hands"}]}}]})
    services.spotify = SpotifyClient("id", "secret", services.http)
    stats = run_playlist_discovery(session, services, load_settings(session), now=NOW)
    assert stats["new_people"] == 1 and stats["no_contact"] == 1
    kim = session.scalar(select(Person).where(Person.name == "Kim"))
    assert kim.kind == "curator"
    assert {(i.type, i.value, i.verified) for i in kim.identities} >= {("email", "curator.kim@gmail.com", True), ("spotify", "kim", False)}


def test_capture_from_extension_and_share_sheet_dedupes(session, services, router):
    f.artist(session, "Mammal Hands", seed=True)
    bandmate = User(name="Sofie", role="member", token_hash="s" * 64)
    session.add(bandmate)
    session.flush()
    person, out = capture(session, services.http, bandmate, CaptureIn(
        url="https://www.tiktok.com/@jazzjonny?lang=en", display_name="Jazz Jonny", followers="12.3K",
        bio="jazz edits daily ✉️ jonny.bookings@gmail.com", bio_link="https://linktr.ee/jazzjonny",
        caption="Mammal Hands at 2am"), now=NOW)
    assert out.created and person.name == "Jazz Jonny" and person.followers == 12300
    assert person.found_by.name == "Sofie"
    assert {i.type for i in person.identities} == {"tiktok", "linkinbio", "email"}
    assert person.coverage[0].artist_name == "Mammal Hands" and person.taste_score > 0

    router.add("HEAD", "https://vm.tiktok.com/ZM123", lambda r: httpx.Response(301, headers={"location": "https://www.tiktok.com/@JazzJonny/video/99"}))
    router.add("HEAD", "https://www.tiktok.com/@JazzJonny/video/99", httpx.Response(200))
    again, out2 = capture(session, services.http, bandmate, CaptureIn(url="https://vm.tiktok.com/ZM123/", source="share"), now=NOW)
    assert not out2.created and again.id == person.id

    reel, out3 = capture(session, services.http, bandmate, CaptureIn(url="https://www.instagram.com/reel/Cx1/", source="share"), now=NOW)
    assert out3.created and reel.needs_review


def test_parse_count():
    assert parse_count("12.3K") == 12300 and parse_count("1,204") == 1204 and parse_count("2M") == 2_000_000
    assert parse_count("1.204") == 1204  # European thousands separator
    assert parse_count(None) is None


def test_calendar_meeting_with_meet_link(session, services, router):
    p = f.person(session, "Mette Jensen", email="mette@jazznyt.dk")
    captured = []

    def handler(req):
        captured.append(req)
        return httpx.Response(200, json={"id": "evt1", "htmlLink": "https://calendar.google.com/e/1", "hangoutLink": "https://meet.google.com/abc"})

    router.add("POST", "https://www.googleapis.com/calendar/v3/calendars/primary/events", handler)
    services._calendar = CalendarClient(lambda: "tok", services.http)
    start = datetime(2026, 11, 10, 14, 0, tzinfo=ZoneInfo("Europe/Copenhagen"))
    m = create_meeting(session, services, p, title="Interview with Jazznyt", start=start, duration_minutes=30,
                       tz="Europe/Copenhagen", invite=True, with_meet=True)
    assert m.calendar_event_id == "evt1" and m.meet_url.startswith("https://meet")
    assert m.starts_at == datetime(2026, 11, 10, 13, 0)
    body = __import__("json").loads(captured[0].content)
    assert body["attendees"] == [{"email": "mette@jazznyt.dk"}]
    assert captured[0].url.params["conferenceDataVersion"] == "1"


def test_youtube_uses_google_sign_in_when_no_key(session, services):
    import httpx

    from press_engine.outreach.google_auth import YOUTUBE_SCOPE

    class FakeGoogle:
        def __init__(self, scopes):
            self.scopes = scopes

        def connected(self):
            return True

        def has_scope(self, s):
            return s in self.scopes

        def access_token(self):
            return "tok123"

    services.youtube = None
    services.google = FakeGoogle(set())
    assert services.youtube_client() is None
    assert "skipped" in run_youtube_discovery(session, services, load_settings(session), now=NOW)

    services.google = FakeGoogle({YOUTUBE_SCOPE})
    yt = services.youtube_client()
    assert yt is not None and yt.via == "Google sign-in"
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["key"] = request.url.params.get("key")
        return httpx.Response(200, json={"items": []})

    services.http = httpx.Client(transport=httpx.MockTransport(handler))
    services.youtube_client().search("x")
    assert seen == {"auth": "Bearer tok123", "key": None}
