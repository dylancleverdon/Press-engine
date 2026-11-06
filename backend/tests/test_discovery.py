from __future__ import annotations

from sqlalchemy import select

from press_engine.appsettings import load_settings
from press_engine.discovery.lastfm import LastFM, add_seed, expand_seeds, remove_seed, searchable_artists
from press_engine.discovery.press import query_plan, run_press_discovery
from press_engine.discovery.search import BraveSearch, SerpApiSearch
from press_engine.models import Artist, Coverage, Identity, Outlet, Page, Person, SearchRun

from .conftest import FakeLLM

LASTFM = "https://ws.audioscrobbler.com/2.0/"

REVIEW_HTML = """<html lang="da"><head><title>Anmeldelse: Mammal Hands – Floa</title>
<meta name="author" content="Mette Jensen"><meta property="og:site_name" content="Jazznyt"></head>
<body><article><h1>Mammal Hands – Floa</h1><p class="byline">Af Mette Jensen</p>
<p>{body}</p></article>
<footer>Skriv til <a href="mailto:mette.jensen@jazznyt.dk">mette.jensen@jazznyt.dk</a> eller redaktion@jazznyt.dk</footer>
</body></html>""".replace("{body}", "Mammal Hands er tilbage med et smukt album, der minder om Athletic Progression. " * 12)

SHOP_HTML = "<html><head><title>Buy Mammal Hands vinyl</title></head><body>" + ("Add to cart. " * 60) + "</body></html>"


def _lastfm_router(router):
    def handler(req):
        import httpx

        method = req.url.params["method"]
        artist = req.url.params["artist"]
        if method == "artist.getsimilar" and artist == "Athletic Progression":
            return httpx.Response(200, json={"similarartists": {"artist": [
                {"name": "Mammal Hands", "match": "1", "mbid": "m1", "url": "https://last.fm/mh"},
                {"name": "GoGo Penguin", "match": "0.62", "url": "https://last.fm/gp"},
                {"name": "Athletic Progression", "match": "0.5"},
            ]}})
        if method == "artist.gettoptags":
            return httpx.Response(200, json={"toptags": {"tag": [{"name": "Jazz"}, {"name": "Danish"}]}})
        return httpx.Response(200, json={"error": 6, "message": "The artist you supplied could not be found"})

    router.add("GET", LASTFM, handler)


def test_seed_expansion_and_removal(session, services, router):
    _lastfm_router(router)
    seed = add_seed(session, "Athletic Progression")
    stats = expand_seeds(session, LastFM("key", services.http))
    assert stats["seeds"] == 1 and stats["new_artists"] == 2
    mh = session.scalar(select(Artist).where(Artist.name == "Mammal Hands"))
    gp = session.scalar(select(Artist).where(Artist.name == "GoGo Penguin"))
    assert mh.similarity == 1.0 and abs(gp.similarity - 0.62) < 1e-9
    assert seed.tags == ["jazz", "danish"]
    assert [a.name for a in searchable_artists(session, "last_press_search_at", 10)][0] == "Athletic Progression"

    remove_seed(session, seed)
    assert mh.similarity == 0.0
    assert searchable_artists(session, "last_press_search_at", 10) == []


def test_brave_and_serpapi_parsing(services, router):
    router.json("GET", "https://api.search.brave.com/", {"web": {"results": [
        {"url": "https://a.dk/x", "title": "T", "description": "D", "age": "2 days ago"}]}})
    router.json("GET", "https://serpapi.com/", {"organic_results": [{"link": "https://b.de/y", "title": "T2", "snippet": "S"}]})
    assert BraveSearch("k", services.http).search('"X" review', lang="ja", freshness="year")[0].url == "https://a.dk/x"
    req = router.requests_to("https://api.search.brave.com/")[0]
    assert req.url.params["search_lang"] == "jp" and req.url.params["freshness"] == "py"
    assert req.headers["X-Subscription-Token"] == "k"
    assert SerpApiSearch("k", services.http).search("q")[0].url == "https://b.de/y"


def test_query_plan_rotates(session):
    settings = load_settings(session)
    artist = add_seed(session, "Athletic Progression")
    first = query_plan(session, artist, settings, 3)
    assert first[0] == ("en", '"Athletic Progression" review')
    assert {lang for lang, _ in first} == {"en", "da", "de"}
    session.add(SearchRun(engine="press", provider="x", query="q", artist_id=artist.id))
    session.flush()
    assert query_plan(session, artist, settings, 3)[0] != first[0]


def _llm_extractor(system, prompt, schema):
    if "Buy Mammal Hands vinyl" in prompt:
        return {"relevant": False, "content_type": "other", "authors": [], "outlet_name": None,
                "artists_covered": [], "published_date": None, "language": "en", "summary": None}
    # Small models make things up; the pipeline must throw the invented bits away.
    return {
        "relevant": True,
        "content_type": "review",
        "authors": [
            {"name": "Mette Jensen", "role": "writer", "email": "mette@made-up.dk"},
            {"name": "Invented Person", "role": None, "email": None},
        ],
        "outlet_name": "Jazznyt",
        "artists_covered": ["Mammal Hands", "Athletic Progression", "Hallucinated Band"],
        "published_date": "2025-03-03",
        "language": "da",
        "summary": "A review of Floa.",
    }


def test_press_discovery_end_to_end(session, services, router):
    _lastfm_router(router)
    add_seed(session, "Athletic Progression")
    expand_seeds(session, LastFM("key", services.http))
    router.json("GET", "https://api.search.brave.com/", {"web": {"results": [
        {"url": "https://jazznyt.dk/anmeldelse/floa?utm_source=x", "title": "Anmeldelse"},
        {"url": "https://shop.example.net/mammal-hands", "title": "Shop"},
        {"url": "https://en.wikipedia.org/wiki/Mammal_Hands", "title": "Wiki"},
    ]}})
    router.html("https://jazznyt.dk/anmeldelse/floa", REVIEW_HTML)
    router.html("https://shop.example.net/", SHOP_HTML)
    services.search = BraveSearch("k", services.http)
    services.extract_llm = FakeLLM(json_fn=_llm_extractor)
    settings = load_settings(session)

    stats = run_press_discovery(session, services, settings, max_queries=2, per_artist=2)
    assert stats["searches"] == 2
    assert stats["blocked"] >= 1  # wikipedia
    assert stats["new_people"] == 1

    person = session.scalar(select(Person).where(Person.name == "Mette Jensen"))
    assert person is not None
    assert session.scalar(select(Person).where(Person.name == "Invented Person")) is None
    emails = {i.value: i.verified for i in person.identities if i.type == "email"}
    assert emails == {"mette.jensen@jazznyt.dk": True}  # verbatim from page; invented one dropped
    outlet = session.scalar(select(Outlet).where(Outlet.domain == "jazznyt.dk"))
    assert outlet.contact_email == "redaktion@jazznyt.dk"
    covered = {c.artist_name for c in session.scalars(select(Coverage).where(Coverage.person_id == person.id))}
    assert covered == {"Mammal Hands", "Athletic Progression"}
    assert all(c.artist_id for c in person.coverage)

    # Pages are cached: a second run does not refetch or re-extract.
    calls_before = len(services.extract_llm.json_calls)
    run_press_discovery(session, services, settings, max_queries=1, per_artist=1)
    assert len(services.extract_llm.json_calls) == calls_before
    assert session.scalar(select(Page).where(Page.url == "https://jazznyt.dk/anmeldelse/floa")).relevant is True


def test_heuristic_extraction_without_llm(session, services, router):
    add_seed(session, "Mammal Hands")
    router.json("GET", "https://api.search.brave.com/", {"web": {"results": [{"url": "https://jazznyt.dk/anmeldelse/floa", "title": "x"}]}})
    router.html("https://jazznyt.dk/anmeldelse/floa", REVIEW_HTML)
    services.search = BraveSearch("k", services.http)
    stats = run_press_discovery(session, services, load_settings(session), max_queries=1, per_artist=1)
    assert stats["new_people"] == 1
    person = session.scalar(select(Person).where(Person.name == "Mette Jensen"))
    assert person.coverage[0].kind == "review"
    assert session.scalar(select(Identity).where(Identity.value == "mette.jensen@jazznyt.dk")).verified
