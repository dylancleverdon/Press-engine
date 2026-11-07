"""Soundalikes page (seeds, suggestions, fit) and the suggestions box."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import soundalikes as sa
from .. import suggestions as sg
from ..models import Artist, ArtistSimilarity, Suggestion, User
from ..util.timeutil import utcnow
from . import views
from .deps import get_db, get_svc, web_owner, web_user

router = APIRouter(include_in_schema=False)


def _page():
    from .routes_web import back, page

    return page, back


def _artist(session: Session, aid: int) -> Artist:
    a = session.get(Artist, aid)
    if a is None:
        raise HTTPException(404)
    return a


@router.get("/seeds", response_class=HTMLResponse)
def seeds(request: Request, user: User = Depends(web_user), session: Session = Depends(get_db), svc=Depends(get_svc)):
    page, _ = _page()
    artists = list(session.scalars(select(Artist).order_by(Artist.similarity.desc(), Artist.name)))
    names = {a.id: a.name for a in artists}
    parents: dict[int, list[str]] = {}
    for link in session.scalars(select(ArtistSimilarity)):
        if link.seed_id in names:
            parents.setdefault(link.artist_id, []).append(names[link.seed_id])
    seed_rows = [a for a in artists if a.is_seed]
    suggested = [a for a in artists if not a.is_seed and a.active and a.review == "suggested"]
    in_use = [a for a in artists if not a.is_seed and a.searchable]
    in_use.sort(key=lambda a: ({"weak": 0, "strong": 1, "ok": 2, "learning": 3}.get(a.fit_verdict, 4), -a.similarity))
    excluded = [a for a in artists if not a.is_seed and (not a.active or a.review == "rejected")]
    return page(request, "seeds.html", user, session, seeds=seed_rows, suggested=suggested, in_use=in_use,
                excluded=excluded, parents=parents, lastfm=svc.lastfm is not None)


@router.post("/artists/{aid}/review")
def review(aid: int, decision: str = Form(...), user: User = Depends(web_owner), session: Session = Depends(get_db)):
    _, back = _page()
    a = _artist(session, aid)
    if decision == "accept":
        sa.accept(session, a)
        msg = f"{a.name} will be searched from the next run"
    elif decision == "seed":
        from ..discovery.lastfm import add_seed

        add_seed(session, a.name)
        msg = f"{a.name} is now a seed; its own soundalikes are looked up on the next run"
    elif decision == "restore":
        a.active = True
        a.review = "accepted"
        sa.recompute_similarities(session)
        msg = f"{a.name} restored"
    else:
        sa.reject(session, a)
        msg = f"{a.name} excluded"
    return back("/seeds", msg)


@router.post("/artists/{aid}/prune")
def prune(aid: int, user: User = Depends(web_owner), session: Session = Depends(get_db)):
    _, back = _page()
    a = _artist(session, aid)
    result = sa.prune(session, a, now=utcnow())
    return back("/seeds", f"Stopped using {a.name}: removed {result['removed']} contacts it found"
                          + (f", kept {result['kept']} you've already contacted" if result["kept"] else ""))


@router.post("/soundalikes/run")
def run_now(request: Request, user: User = Depends(web_owner)):
    _, back = _page()
    views.run_in_background(request.app.state.db, request.app.state.svc, "expand", force=True)
    return back("/seeds", "Looking for more soundalikes now. Refresh in a minute.")


# --- suggestions box ----------------------------------------------------------------------

@router.get("/suggestions", response_class=HTMLResponse)
def suggestions(request: Request, user: User = Depends(web_user), session: Session = Depends(get_db), svc=Depends(get_svc)):
    page, _ = _page()
    from ..appsettings import load_settings
    from ..sheets import sheet_id_from

    rows = list(session.scalars(select(Suggestion).order_by(Suggestion.id.desc())))
    sid = sheet_id_from(load_settings(session).suggestions_sheet_id or "")
    return page(request, "suggestions.html", user, session, rows=rows, statuses=sg.STATUSES, areas=sg.AREAS,
                sheet_url=f"https://docs.google.com/spreadsheets/d/{sid}/edit" if sid else None,
                sheet_ready=svc.sheets() is not None)


@router.post("/suggestions")
def suggest(request: Request, text: str = Form(...), area: str = Form(""), user: User = Depends(web_user),
            session: Session = Depends(get_db), svc=Depends(get_svc)):
    _, back = _page()
    if not text.strip():
        return back("/suggestions", "Write the suggestion first")
    sg.submit(session, user, text, area)
    session.commit()
    if svc.sheets() is not None:
        views.run_in_background(request.app.state.db, svc, "sheet_sync")
        return back("/suggestions", "Thanks! Added to the suggestions sheet.")
    return back("/suggestions", "Thanks! Saved. It goes to the Google Sheet once Google is connected with Sheets access.")


@router.post("/suggestions/{sid}")
def suggestion_edit(request: Request, sid: int, status: str = Form("new"), notes: str = Form(""), adjustments: str = Form(""),
                    user: User = Depends(web_owner), session: Session = Depends(get_db), svc=Depends(get_svc)):
    _, back = _page()
    s = session.get(Suggestion, sid)
    if s is None:
        raise HTTPException(404)
    sg.edit(session, s, status=status, notes=notes, adjustments=adjustments)
    session.commit()
    if svc.sheets() is not None:
        views.run_in_background(request.app.state.db, svc, "sheet_sync")
    return back("/suggestions", f"Updated suggestion #{s.id}")


@router.post("/suggestions-sync")
def sync_now(request: Request, user: User = Depends(web_owner)):
    _, back = _page()
    views.run_in_background(request.app.state.db, request.app.state.svc, "sheet_sync")
    return back("/suggestions", "Syncing with the sheet. Refresh in a few seconds.")
