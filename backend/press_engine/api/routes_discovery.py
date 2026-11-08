"""Discovery tab: an overview of the engines, one page per engine, and vetting."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..appsettings import AppSettings, load_settings, save_settings
from ..discovery import engines as eng
from ..models import Artist, JobRun, Person, User
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
def engine_page(request: Request, key: str, show: str | None = None, user: User = Depends(web_user),
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
    rows = eng.people(session, e, show)
    session.scalars(select(Person).where(Person.id.in_([p.id for p in rows]))
                    .options(selectinload(Person.identities), selectinload(Person.coverage)))
    artist_names = {a.id: a.name for a in session.scalars(select(Artist))}
    searches = eng.recent_searches(session, e)
    return page(request, "engine.html", user, session, settings=settings, engine=e, fields=fields, rows=rows, show=show,
                counts=eng.counts(session, e), connected=_connected(svc, e), searches=searches,
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

    st = eng.recheck_youtube(session, load_settings(session))
    return back("/discovery/youtube", f"Checked {st['checked']} finds: dropped {st['videos_dropped']} videos that don't count, "
                f"moved {st['rejected']} with nothing left to Rejected, flagged {st['merged_flagged']} that hold several channels.")


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
def vet(key: str, decision: str = Form(...), ids: list[int] = Form(default=[]), show: str = Form("pending"),
        one: int | None = None, user: User = Depends(web_owner), session: Session = Depends(get_db)):
    """Approve or reject one contact (?one=id) or the ticked ones (ids)."""
    from .routes_web import back

    e = _engine(key)
    if one is not None:
        ids = [one]
    if not ids:
        return back(f"/discovery/{e.key}?show={show}", "Tick some contacts first")
    if decision not in ("approve", "reject", "pending"):
        raise HTTPException(400)
    people = list(session.scalars(select(Person).where(Person.id.in_(ids)))) if ids else []
    for p in people:
        {"approve": eng.approve, "reject": eng.reject, "pending": eng.unreview}[decision](p)
    n = len(people)
    what = {"approve": "added to Contacts", "reject": "rejected", "pending": "moved back to review"}[decision]
    msg = f"{people[0].name} {what}" if n == 1 else f"{n} contacts {what}"
    return back(f"/discovery/{e.key}?show={show}", msg)
