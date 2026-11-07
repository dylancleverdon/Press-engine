from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from urllib.parse import unquote

import httpx
import pytest
from sqlalchemy import select

from press_engine import soundalikes as sa
from press_engine import suggestions as sg
from press_engine.appsettings import load_settings, update_settings
from press_engine.discovery.lastfm import LastFM, add_seed, searchable_artists
from press_engine.models import Artist, Coverage, Outreach, OutreachStatus, Person, PersonStatus, Suggestion, User
from press_engine.scoring import artist_weight, score_heuristic
from press_engine.sheets import API, sheet_id_from

from . import factories as f

NOW = datetime(2026, 11, 9, 9, 0)
LASTFM = "https://ws.audioscrobbler.com/2.0/"

SIMILAR = {
    "Athletic Progression": [("Mammal Hands", "1"), ("GoGo Penguin", "0.62"), ("Distant Cousin", "0.2")],
    "Mammal Hands": [("Portico Quartet", "0.9"), ("Athletic Progression", "0.8")],
    "GoGo Penguin": [("Portico Quartet", "0.5")],
}


@pytest.fixture
def lastfm(services, router):
    def handler(req):
        method, artist = req.url.params["method"], req.url.params["artist"]
        if method == "artist.getsimilar":
            items = [{"name": n, "match": m} for n, m in SIMILAR.get(artist, [])]
            return httpx.Response(200, json={"similarartists": {"artist": items}})
        return httpx.Response(200, json={"toptags": {"tag": []}})

    router.add("GET", LASTFM, handler)
    services.lastfm = LastFM("k", services.http)
    return services


def artist(session, name):
    return session.scalar(select(Artist).where(Artist.name == name))


def test_strong_matches_used_weak_ones_suggested_and_second_hop(session, lastfm):
    add_seed(session, "Athletic Progression")
    stats = sa.discover_soundalikes(session, lastfm, load_settings(session), now=NOW)
    assert stats["seeds"]["suggested"] == 1
    assert artist(session, "Mammal Hands").review == "auto" and artist(session, "GoGo Penguin").review == "auto"
    distant = artist(session, "Distant Cousin")
    assert distant.review == "suggested" and "20% similar" in distant.suggested_reason
    names = {a.name for a in searchable_artists(session, "last_press_search_at", 50)}
    assert "Distant Cousin" not in names and "Mammal Hands" in names

    # Second hop: soundalikes of soundalikes are always suggestions, discounted.
    portico = artist(session, "Portico Quartet")
    assert portico.review == "suggested"
    assert abs(portico.similarity - 1.0 * 0.9 * 0.8) < 1e-6
    assert "similar to your seeds" in portico.suggested_reason

    sa.accept(session, portico)
    assert portico.searchable and artist_weight(portico) > 0
    sa.reject(session, distant)
    assert not distant.searchable and artist_weight(distant) == 0


def test_cocoverage_suggestions_from_contacts(session):
    seed = f.artist(session, "Athletic Progression", seed=True)
    for name in ("Ann Writer", "Bo Critic", "Cy Blogger"):
        p = f.person(session, name, outlet=f"{name.split()[0].lower()}.dk", covers=[seed])
        session.add(Coverage(person=p, url=f"https://{name[:2]}.dk/k", artist_name="Kokoroko"))
    p = f.person(session, "Lone Fan", outlet="lone.dk", covers=[seed])
    session.add(Coverage(person=p, url="https://lone.dk/x", artist_name="Obscure Act"))
    session.flush()
    stats = sa.cocoverage(session, load_settings(session))
    assert stats["new_suggestions"] == 1  # Obscure Act has only one contact
    k = artist(session, "Kokoroko")
    assert k.review == "suggested" and "3 of your contacts" in k.suggested_reason
    assert session.scalar(select(Coverage).where(Coverage.artist_name == "Kokoroko")).artist_id == k.id

    ann = session.scalar(select(Person).where(Person.name == "Ann Writer"))
    score_heuristic(session, now=NOW)
    before = ann.taste_score
    sa.accept(session, k)
    score_heuristic(session, now=NOW)
    assert ann.taste_score > before


def _pitched(session, name, a, *, replied: bool, outlet: str):
    p = f.person(session, name, outlet=outlet, email=f"{name.split()[0].lower()}@{outlet}", covers=[a])
    p.discovered_artist_id = a.id
    status = OutreachStatus.REPLIED if replied else OutreachStatus.SENT
    session.add(Outreach(person=p, channel="email", to_address=f"x@{outlet}", status=status, sent_at=NOW - timedelta(days=3)))
    p.status = PersonStatus.REPLIED if replied else PersonStatus.PITCHED
    return p


def test_fit_verdicts_weights_order_and_prune(session):
    good = f.artist(session, "Good Fit", similarity=0.6)
    bad = f.artist(session, "Bad Fit", similarity=0.9)
    for i in range(10):
        _pitched(session, f"Good{i} Writer", good, replied=i < 5, outlet=f"g{i}.dk")
        _pitched(session, f"Bad{i} Writer", bad, replied=False, outlet=f"b{i}.dk")
    fresh = []
    for i in range(3):  # found through Bad Fit, never contacted
        p = f.person(session, f"Fresh{i} Writer", outlet=f"fresh{i}.dk", covers=[bad])
        p.discovered_artist_id = bad.id
        fresh.append(p.id)
    session.flush()

    stats = sa.update_fit(session, load_settings(session))
    assert good.fit_verdict == "strong" and bad.fit_verdict == "weak"
    assert stats["overall_pitched"] == 20 and stats["overall_replied"] == 5
    assert bad.fit_stats["contacts"] == 13 and bad.fit_stats["pitched"] == 10 and "reply rate" in bad.fit_stats["why"]
    assert artist_weight(bad) == pytest.approx((0.3 + 0.7 * 0.9) * 0.6)
    assert [a.name for a in searchable_artists(session, "last_press_search_at", 5)][-1] == "Bad Fit"

    result = sa.prune(session, bad, now=NOW)
    assert result == {"removed": 3, "kept": 10}
    assert all(session.get(Person, pid) is None for pid in fresh)
    assert bad.review == "rejected" and not bad.searchable


def test_not_enough_data_is_learning(session):
    a = f.artist(session, "New One")
    for i in range(3):
        _pitched(session, f"N{i} Writer", a, replied=False, outlet=f"n{i}.dk")
    sa.update_fit(session, load_settings(session))
    assert a.fit_verdict == "learning" and a.fit_factor == 1.0 and "3 of 8 pitches" in a.fit_stats["why"]


# --- suggestions <-> Google Sheet ------------------------------------------------------------

class FakeSheets:
    """Just enough of the Sheets v4 API: tabs, values get/update/append/clear."""

    def __init__(self, router, sid):
        self.sid = sid
        self.tabs: dict[str, list[list[str]]] = {"Sheet1": []}
        self.batches: list[dict] = []
        router.add("GET", f"{API}/{sid}/values/", self._get)
        router.add("PUT", f"{API}/{sid}/values/", self._put)
        router.add("POST", f"{API}/{sid}/values/", self._post_values)
        router.add("POST", f"{API}/{sid}:batchUpdate", self._batch)
        router.add("GET", f"{API}/{sid}?", self._meta)

    @staticmethod
    def _range(req):
        rng = unquote(str(req.url).split("/values/", 1)[1].split("?", 1)[0])
        rng = rng.split(":append")[0].split(":clear")[0]
        tab, _, cells = rng.partition("!")
        m = re.match(r"[A-Z]+(\d+)?", cells)
        return tab, int(m.group(1)) if m and m.group(1) else 1

    def _meta(self, req):
        return httpx.Response(200, json={"sheets": [{"properties": {"title": t, "sheetId": i}} for i, t in enumerate(self.tabs)]})

    def _batch(self, req):
        body = json.loads(req.content)
        self.batches.append(body)
        replies = []
        for r in body["requests"]:
            if "addSheet" in r:
                title = r["addSheet"]["properties"]["title"]
                self.tabs[title] = []
                replies.append({"addSheet": {"properties": {"title": title, "sheetId": len(self.tabs)}}})
            else:
                replies.append({})
        return httpx.Response(200, json={"replies": replies})

    def _get(self, req):
        tab, start = self._range(req)
        rows = self.tabs[tab][start - 1:]
        return httpx.Response(200, json={"values": rows})

    def _write(self, tab, start, values):
        grid = self.tabs[tab]
        while len(grid) < start - 1 + len(values):
            grid.append([])
        for i, row in enumerate(values):
            grid[start - 1 + i] = [str(c) for c in row]

    def _put(self, req):
        tab, start = self._range(req)
        self._write(tab, start, json.loads(req.content)["values"])
        return httpx.Response(200, json={})

    def _post_values(self, req):
        tab, start = self._range(req)
        if ":clear" in str(req.url):
            del self.tabs[tab][start - 1:]
        else:  # append after the last row
            self._write(tab, len(self.tabs[tab]) + 1, json.loads(req.content)["values"])
        return httpx.Response(200, json={})


@pytest.fixture
def sheet(services, router, session):
    sid = "1C-Y3u0ixkD8KX19ns70GC3XA4tpZ1YvdF-HPskqIOTM"
    fake = FakeSheets(router, sid)
    services.cfg.token_path.write_text(json.dumps({"token": "t", "scopes": ["https://www.googleapis.com/auth/spreadsheets"]}))
    services.google.access_token = lambda: "test-token"
    update_settings(session, suggestions_sheet_id=f"https://docs.google.com/spreadsheets/d/{sid}/edit")
    return fake


def test_suggestions_round_trip_with_the_sheet(session, services, sheet):
    d = User(name="D", role="owner", token_hash="d" * 64)
    session.add(d)
    session.flush()
    first = sg.submit(session, d, "Look at Scandinavian jazz blogs too", "Finding press")
    stats = sg.sync(session, services, load_settings(session), now=NOW)
    assert stats["pushed"] == 1 and not first.dirty
    tab = sheet.tabs["Suggestions"]
    assert tab[0] == sg.HEADER and tab[1][0] == str(first.id) and tab[1][2] == "D" and tab[1][5] == "new"
    validation = [r for b in sheet.batches for r in b["requests"] if "setDataValidation" in r]
    assert validation and validation[0]["setDataValidation"]["rule"]["condition"]["values"][3] == {"userEnteredValue": "shipped"}

    # Reviewed in the sheet -> comes back into the app.
    tab[1][5], tab[1][6], tab[1][7] = "shipped", "Added da/no/sv blogs", "Only blogs with a byline"
    # And a suggestion typed straight into the sheet gets imported.
    tab.append(["", "", "", "Mac app", "Dark mode for the app", "", "", "", ""])
    stats = sg.sync(session, services, load_settings(session), now=NOW + timedelta(minutes=10))
    assert stats["pulled"] == 1 and stats["imported"] == 1
    assert (first.status, first.notes, first.adjustments) == ("shipped", "Added da/no/sv blogs", "Only blogs with a byline")
    imported = session.scalar(select(Suggestion).where(Suggestion.text == "Dark mode for the app"))
    assert tab[2][0] == str(imported.id) and tab[2][2] == "sheet"

    # Edited in the app -> written to the sheet.
    sg.edit(session, imported, status="accepted", notes="Next release")
    stats = sg.sync(session, services, load_settings(session), now=NOW + timedelta(minutes=20))
    assert stats["updated_in_sheet"] == 1 and tab[2][5] == "accepted" and tab[2][6] == "Next release"
    assert "Soundalikes" in sheet.tabs


def test_sync_without_sheets_access_keeps_suggestions_locally(session, services):
    services.cfg.token_path.write_text(json.dumps({"token": "t", "scopes": ["https://www.googleapis.com/auth/gmail.send"]}))
    sg.submit(session, None, "Something", None)
    stats = sg.sync(session, services, load_settings(session), now=NOW)
    assert "reconnect" in stats["skipped"] and session.scalar(select(Suggestion)).dirty


def test_sheet_id_from_url():
    assert sheet_id_from("https://docs.google.com/spreadsheets/d/abc123/edit#gid=0") == "abc123"
    assert sheet_id_from(" abc123 ") == "abc123"


def test_pages_render(db, services, sheet, session):
    from fastapi.testclient import TestClient

    from press_engine.api.app import create_app
    from press_engine.api.deps import create_user

    session.commit()  # release the fixture's write lock
    with db.session() as s:
        _, token = create_user(s, "D", role="owner")
        seed = f.artist(s, "Athletic Progression", seed=True)
        sug = f.artist(s, "Maybe Band", similarity=0.3)
        sug.review = "suggested"
        sug.suggested_reason = "Last.fm: 30% similar to Athletic Progression"
        assert seed
    client = TestClient(create_app(db=db, svc=services, start_scheduler=False))
    client.post("/login", data={"token": token, "next": "/"})
    page = client.get("/seeds")
    assert page.status_code == 200 and "Maybe Band" in page.text and "Use it" in page.text
    r = client.post(f"/artists/{sug.id}/review", data={"decision": "accept"}, follow_redirects=False)
    assert r.status_code == 303
    with db.session() as s:
        assert s.get(Artist, sug.id).review == "accepted"
    assert client.get("/suggestions").status_code == 200
    r = client.post("/suggestions", data={"text": "More Dutch press", "area": "Finding press"}, follow_redirects=False)
    assert "Thanks" in unquote(r.headers["location"])
    assert "Open it" in client.get("/connections").text
