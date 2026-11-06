from __future__ import annotations

from datetime import date

from sqlalchemy import select

from press_engine.contacts.dedupe import find_merge_candidates, resolve_candidate
from press_engine.contacts.ingest import add_identity
from press_engine.contacts.resolve import resolve_person, verified_personal_email
from press_engine.discovery.pages import fetch_page
from press_engine.models import Coverage, Identity, MergeCandidate, Person
from press_engine.scoring import score_heuristic, score_with_llm, taste_profile

from . import factories as f
from .conftest import FakeLLM

ARTICLE = """<html><head><title>Review: Mammal Hands</title></head><body>
<p class="byline">By <a href="/author/sam-writer/">Sam Writer</a></p><p>{text}</p>
<a href="/kontakt">Kontakt</a></body></html>""".replace("{text}", "Mammal Hands review text. " * 30)
AUTHOR_PAGE = """<html><body><h1>Sam Writer</h1><p>Sam writes about jazz.</p>
<a href="https://www.instagram.com/samwrites/">Instagram</a> <a href="https://linktr.ee/samwrites">links</a>
<p>Reach me: sam.writer@gmail.com</p></body></html>"""
KONTAKT = "<html><body>Redaktionen: <a href='mailto:redaktion@jazzblog.dk'>redaktion@jazzblog.dk</a></body></html>"


def test_resolution_follows_author_page_and_merges(session, services, router):
    mh = f.artist(session, "Mammal Hands", seed=True)
    router.html("https://jazzblog.dk/review", ARTICLE)
    router.html("https://jazzblog.dk/author/sam-writer", AUTHOR_PAGE)
    router.html("https://jazzblog.dk/kontakt", KONTAKT)
    page = fetch_page(session, services.fetcher, "https://jazzblog.dk/review")
    sam = f.person(session, "Sam Writer", outlet="jazzblog.dk")
    session.add(Coverage(person=sam, url=page.url, artist_name=mh.name, artist=mh))
    # Someone a bandmate captured from Instagram earlier: same person, other record.
    captured = f.person(session, "@samwrites", kind="creator", handles={"instagram": "samwrites"})
    session.flush()

    survivor = resolve_person(session, services, sam)
    assert verified_personal_email(survivor).value == "sam.writer@gmail.com"
    assert {i.type for i in survivor.identities} >= {"email", "instagram"}
    assert session.get(Person, captured.id) is None or survivor.id == captured.id  # merged by link following
    assert survivor.name == "Sam Writer"
    assert survivor.outlets[0].domain == "jazzblog.dk"


def test_generic_inbox_goes_to_outlet(session, services, router):
    router.html("https://jazzblog.dk/kontakt", KONTAKT)
    router.html("https://jazzblog.dk/review2", ARTICLE.replace("/author/sam-writer/", "/x"))
    page = fetch_page(session, services.fetcher, "https://jazzblog.dk/review2")
    desk = f.person(session, "Jazzblog", outlet="jazzblog.dk")
    desk.is_desk = True
    session.add(Coverage(person=desk, url=page.url, artist_name="Mammal Hands"))
    session.flush()
    resolve_person(session, services, desk)
    assert desk.outlets[0].contact_email == "redaktion@jazzblog.dk"
    assert verified_personal_email(desk) is None


def test_identity_conflict_merges_people(session):
    a = f.person(session, "Jo Critic", email="jo@zine.dk")
    b = f.person(session, "Jo Critic", outlet="zine.dk", handles={"tiktok": "jocritic"})
    survivor = add_identity(session, b, "email", "jo@zine.dk", verified=True)
    assert survivor.id == a.id
    assert {i.type for i in survivor.identities} == {"email", "tiktok"}
    assert session.get(Person, b.id) is None


def test_fuzzy_candidates_need_overlap(session):
    f.person(session, "Mette Jensen", outlet="jazznyt.dk")
    f.person(session, "Mette Jenssen", outlet="jazznyt.dk")
    f.person(session, "Mette Jensen", outlet="other.de")  # same name, nothing shared
    f.person(session, "Jazz Jonny", handles={"tiktok": "jazzjonny"})
    f.person(session, "J. Jonny", handles={"instagram": "jazzjonny"})
    n = find_merge_candidates(session)
    assert n == 2  # Jensen/Jenssen (shared outlet) and the two Jonnys (same handle)
    reasons = [c.reason for c in session.scalars(select(MergeCandidate))]
    assert any("same outlet" in r for r in reasons)
    assert any("Same handle 'jazzjonny'" in r for r in reasons)
    assert find_merge_candidates(session) == 0  # idempotent

    cand = session.scalars(select(MergeCandidate).where(MergeCandidate.reason.contains("handle"))).first()
    survivor = resolve_candidate(session, cand, same=True)
    assert {i.type for i in survivor.identities} == {"tiktok", "instagram"}


def test_heuristic_scoring_prefers_multiple_matches(session):
    seed = f.artist(session, "Athletic Progression", seed=True)
    close = f.artist(session, "Mammal Hands", similarity=1.0)
    far = f.artist(session, "Some Distant Band", similarity=0.1)
    one = f.person(session, "One Match", outlet="a.dk", covers=[far])
    many = f.person(session, "Many Matches", outlet="b.dk", covers=[seed, close])
    old = f.person(session, "Old Matches", outlet="c.dk", covers=[seed, close], published=date(2019, 1, 1))
    score_heuristic(session)
    assert many.taste_score > old.taste_score > one.taste_score > 0
    profile, _ = taste_profile(many)
    assert profile["matched"] == 2 and list(profile["artists"])[0] == "Athletic Progression"


def test_llm_scoring_blends_and_validates_piece(session, services):
    seed = f.artist(session, "Athletic Progression", seed=True)
    p = f.person(session, "Writer One", outlet="a.dk", email="w@a.dk", covers=[seed])
    score_heuristic(session)
    url = p.coverage[0].url

    llm = FakeLLM(json_fn=lambda s, prompt, schema: {
        "score": 90, "reason": "Covers the seed artist.", "opener": "Loved your Athletic Progression review.",
        "piece_url": url})
    from press_engine.appsettings import load_settings

    stats = score_with_llm(session, llm, load_settings(session))
    assert stats["scored"] == 1
    assert p.llm_score == 90 and p.opener.startswith("Loved")
    assert abs(p.taste_score - (0.6 * 0.9 + 0.4 * p.heuristic_score)) < 1e-6
    assert "Athletic Progression" in llm.json_calls[0]

    # An opener pointing at a piece that doesn't exist is thrown away.
    p.llm_scored_at = None
    p.opener = None
    llm2 = FakeLLM(json_fn=lambda s, prompt, schema: {"score": 50, "reason": "x", "opener": "Made up", "piece_url": "https://nope"})
    score_with_llm(session, llm2, load_settings(session))
    assert p.opener is None and p.llm_score == 50


def test_unverified_emails_are_kept_but_flagged(session, services, router):
    router.html("https://linktr.ee/zoe", "<html><body>Booking: zoe [at] zoemusic [dot] com</body></html>")
    zoe = f.person(session, "Zoe", kind="creator", handles={"linkinbio": "linktr.ee/zoe"})
    zoe.identities[0].display = "https://linktr.ee/zoe"
    resolve_person(session, services, zoe)
    ident = session.scalar(select(Identity).where(Identity.type == "email"))
    assert ident.value == "zoe@zoemusic.com" and ident.verified is False
    assert verified_personal_email(zoe) is None
