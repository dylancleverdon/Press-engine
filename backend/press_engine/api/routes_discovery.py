"""Discovery tab: an overview of the engines, one page per engine, and vetting."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..appsettings import AppSettings, load_settings, save_settings
from ..discovery import engines as eng
from ..models import Artist, JobRun, Person, SearchRun, User
from . import views
from .deps import get_db, get_svc, web_owner, web_user

router = APIRouter(include_in_schema=False)


def _engine(key: str) -> eng.Engine:
    e = eng.ENGINES.get(key)
    if e is None:
        raise HTTPException(404)
    return e


def _connected(svc, e: eng.Engine) -> tuple[bool, str]:
    """Whether the engine can run, and what it uses (or what's missing)."""
    if e.coming_soon:
        return False, "Coming soon"
    if e.key == "youtube":
        yt = svc.youtube_client()
        return (True, f"via {yt.via}") if yt else (False, "Connect Google with YouTube access on Connections")
    if e.key in ("press", "tiktok"):
        return (True, f"via {svc.search.name}") if svc.search else (False, "Add a Brave or SerpAPI web search key on Connections")
    if e.key == "playlists":
        return (True, "via Spotify") if svc.spotify else (False, "Add Spotify keys on Connections")
    return True, "Chrome extension and phone share sheet"


def engine_status(session: Session, e: eng.Engine, settings) -> dict:
    """The engine's last run in plain words, and today's search use (#25)."""
    from datetime import timedelta

    from ..discovery.press import searches_since
    from ..util.timeutil import utcnow

    out: dict = {"last": None, "used": None, "cap": None, "next_free": None}
    for run in session.scalars(select(JobRun).where(JobRun.job == "discover").order_by(JobRun.started_at.desc()).limit(30)):
        st = (run.stats or {}).get(e.key)
        if run.ok is False and st is None and run.error:
            out["last"] = {"at": run.started_at, "ok": False, "text": f"The run failed: {run.error[:200]}"}
            break
        if st is not None:
            out["last"] = {"at": run.started_at, **describe_engine_stats(st)}
            break
    cap_field = {"youtube": "youtube_daily_searches", "tiktok": "tiktok_daily_searches", "press": "search_daily_budget",
                 "playlists": "playlist_daily_searches"}.get(e.key)
    if cap_field and e.search_engine:
        now = utcnow()
        since = now - timedelta(hours=24)
        out["used"] = searches_since(session, e.search_engine, since)
        out["cap"] = getattr(settings, cap_field)
        if out["used"] >= out["cap"]:
            oldest = session.scalar(select(SearchRun.run_at).where(SearchRun.engine == e.search_engine,
                                                                     SearchRun.run_at >= since).order_by(SearchRun.run_at))
            out["next_free"] = oldest + timedelta(hours=24) if oldest else None
    return out


def describe_engine_stats(st: dict) -> dict:
    if not isinstance(st, dict):
        return {"ok": True, "text": str(st)}
    if st.get("error"):
        return {"ok": False, "text": f"Stopped with an error: {st['error']}"}
    if st.get("stopped"):
        return {"ok": False, "text": st["stopped"]}
    if st.get("skipped"):
        why = st["skipped"]
        if "budget used up" in why:
            return {"ok": False, "text": "Didn't search: today's searches were already used up (see the meter below)."}
        return {"ok": False, "text": f"Didn't run: {why}"}
    bits = []
    if "searches" in st:
        bits.append(f"{st['searches']} searches")
    if st.get("new_people") is not None:
        bits.append(f"{st['new_people']} new")
    if st.get("waiting_for_review"):
        bits.append(f"{st['waiting_for_review']} waiting to be vetted")
    if st.get("skipped_videos"):
        bits.append(f"{st['skipped_videos']} videos skipped by the rules")
    if st.get("too_small"):
        bits.append(f"{st['too_small']} channels outside the subscriber limits")
    if st.get("rules", {}).get("filtered"):
        bits.append(f"{st['rules']['filtered']} moved to Filtered out")
    if st.get("errors"):
        bits.append(f"{st['errors']} searches failed (see Recent searches)")
    return {"ok": not st.get("errors"), "text": "Finished: " + (", ".join(bits) if bits else "nothing new") + "."}


def _last_discover(session: Session) -> JobRun | None:
    return session.scalar(select(JobRun).where(JobRun.job == "discover").order_by(JobRun.started_at.desc()).limit(1))


@router.get("/discovery", response_class=HTMLResponse)
def overview(request: Request, user: User = Depends(web_user), session: Session = Depends(get_db), svc=Depends(get_svc)):
    from .routes_web import page

    artists = list(session.scalars(select(Artist)))
    soundalikes = {
        "seeds": sum(1 for a in artists if a.is_seed and a.active),
        "in_use": sum(1 for a in artists if not a.is_seed and a.searchable),
        "suggested": sum(1 for a in artists if not a.is_seed and a.active and a.review == "suggested"),
    }
    cards = [{"engine": e, "counts": eng.counts(session, e), "connected": _connected(svc, e)} for e in eng.ENGINES.values()]
    return page(request, "discovery.html", user, session, cards=cards, soundalikes=soundalikes,
                last_run=_last_discover(session), running="discover" in views.running_jobs())


@router.get("/discovery/{key}", response_class=HTMLResponse)
def engine_page(request: Request, key: str, show: str | None = None, how: str | None = None, user: User = Depends(web_user),
                session: Session = Depends(get_db), svc=Depends(get_svc)):
    from .routes_web import _field_kind, _form_value, page

    e = _engine(key)
    if show not in eng.VETTING:  # captures are saved by hand, so they skip vetting
        show = eng.APPROVED if not e.runnable else eng.PENDING
    settings = load_settings(session)
    fields = [(name, label, *_field_kind(name), _form_value(settings, name)) for name, label in e.settings]
    if e.auto_field:
        fields.append((e.auto_field, "Auto-approve: add what this engine finds straight to Contacts", "bool", [],
                       getattr(settings, e.auto_field)))
    if how not in eng.REACH:
        how = None
    rows = eng.people(session, e, show, how=how)
    session.scalars(select(Person).where(Person.id.in_([p.id for p in rows]))
                    .options(selectinload(Person.identities), selectinload(Person.coverage)))
    artist_names = {a.id: a.name for a in session.scalars(select(Artist))}
    searches = eng.recent_searches(session, e)
    return page(request, "engine.html", user, session, settings=settings, engine=e, fields=fields, rows=rows, show=show,
                counts=eng.counts(session, e), connected=_connected(svc, e), searches=searches, how=how,
                reach_counts=eng.reach_counts(session, e, show), shown_total=len(eng.people(session, e, show, limit=None, how=how)),
                reach=eng.reach, status=engine_status(session, e, settings),
                artist_names=artist_names, contact_line=views.contact_line, merged_channels=eng.merged_channels,
                running="discover" in views.running_jobs())


@router.post("/discovery/{key}/run")
def engine_run(request: Request, key: str, user: User = Depends(web_owner), svc=Depends(get_svc)):
    from .routes_web import back

    e = _engine(key)
    if not e.runnable:
        raise HTTPException(400)
    ok, why = _connected(svc, e)
    if not ok:
        return back(f"/discovery/{key}", why)
    views.run_in_background(request.app.state.db, svc, "discover", sources=(key,))
    return back(f"/discovery/{key}", f"Running the {e.label} engine. This page refreshes when it's done.")


@router.post("/discovery/youtube/recheck")
def youtube_recheck(user: User = Depends(web_owner), session: Session = Depends(get_db)):
    from .routes_web import back

    st = eng.apply_youtube_rules(session, load_settings(session))
    return back("/discovery/youtube", eng.describe_rules_result(st))


@router.post("/discovery/{key}/settings")
async def engine_settings(request: Request, key: str, user: User = Depends(web_owner), session: Session = Depends(get_db)):
    from .routes_web import apply_settings_form, back

    e = _engine(key)
    form = await request.form()
    data = load_settings(session).model_dump()
    names = [name for name, _ in e.settings] + ([e.auto_field] if e.auto_field else [])
    apply_settings_form(form, names, data)
    try:
        save_settings(session, AppSettings.model_validate(data))
    except ValidationError as err:
        errors = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in err.errors())
        return back(f"/discovery/{key}", f"Not saved - {errors}")
    if e.key == "youtube":  # new rules apply straight away to what's waiting
        st = eng.apply_youtube_rules(session, load_settings(session))
        return back(f"/discovery/{key}", f"YouTube settings saved. {eng.describe_rules_result(st)}")
    return back(f"/discovery/{key}", f"{e.label} settings saved")


@router.get("/contacts/{pid}/report", response_class=HTMLResponse)
def report_form(request: Request, pid: int, next: str = "", user: User = Depends(web_user), session: Session = Depends(get_db)):
    from ..reports import discovery_report
    from .routes_web import page

    person = session.get(Person, pid)
    if person is None:
        raise HTTPException(404)
    return page(request, "report.html", user, session, p=person, details=discovery_report(session, person),
                next=next if next.startswith("/") else f"/contacts/{pid}")


@router.post("/contacts/{pid}/report")
async def report_send(request: Request, pid: int, text: str = Form(...), next: str = Form(""),
                      images: list[UploadFile] = File(default=[]), user: User = Depends(web_user),
                      session: Session = Depends(get_db), svc=Depends(get_svc)):
    from .. import suggestions as sg
    from ..reports import discovery_report
    from .routes_soundalikes import read_images
    from .routes_web import back

    person = session.get(Person, pid)
    if person is None:
        raise HTTPException(404)
    dest = next if next.startswith("/") else f"/contacts/{pid}"
    if not text.strip():
        return back(f"/contacts/{pid}/report?next={dest}", "Say what's wrong first")
    s = sg.submit(session, user, f"{text.strip()}\n\n(Report on contact #{person.id}: {person.name})", "Discovery report",
                  details=discovery_report(session, person), images=await read_images(images),
                  image_dir=sg.image_dir_for(svc.cfg))
    session.commit()
    if svc.sheets() is not None:
        views.run_in_background(request.app.state.db, svc, "sheet_sync")
    return back(dest, f"Thanks! Reported as suggestion #{s.id}, with everything about {person.name} attached.")


@router.post("/discovery/{key}/vet")
def vet(request: Request, key: str, decision: str = Form(...), ids: list[int] = Form(default=[]), show: str = Form("pending"),
        how: str = Form(""), everything: str = "", one: int | None = None,
        user: User = Depends(web_owner), session: Session = Depends(get_db)):
    """Approve or reject one contact (?one=id), the ticked ones (ids), or everything in the
    current tab and filter (everything=1). Answers JSON to the page's own requests, so
    the page doesn't reload (and jump back to the top)."""
    from fastapi.responses import JSONResponse

    from .routes_web import back

    e = _engine(key)
    if decision not in eng.DECISIONS:
        raise HTTPException(400)
    here = f"/discovery/{e.key}?show={show}" + (f"&how={how}" if how in eng.REACH else "")
    if one is not None:
        people = [p for p in [session.get(Person, one)] if p is not None]
    elif everything:
        people = eng.people(session, e, show if show in eng.VETTING else eng.PENDING, limit=None,
                            how=how if how in eng.REACH else None)
    else:
        people = list(session.scalars(select(Person).where(Person.id.in_(ids)))) if ids else []
    if not people:
        return back(here, "Tick some contacts first")
    before = {p.id: p.vetting for p in people}
    for p in people:
        eng.decide(p, decision)
    n = len(people)
    what = {"approve": "added to Contacts", "reject": "rejected", "pending": "moved back to To vet",
            "filtered": "moved back to Filtered out"}[decision]
    msg = f"{people[0].name} {what}" if n == 1 else f"{n} contacts {what}"
    session.commit()
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"ok": True, "message": msg, "ids": [p.id for p in people], "before": before,
                             "counts": eng.counts(session, e)})
    return back(here, msg)
