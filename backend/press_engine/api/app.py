"""FastAPI application: JSON API for the extension / phone, plus the web dashboard."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import get_config
from ..db import Database, get_database
from .deps import LoginRequired, OwnerRequired

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(*, db: Database | None = None, svc=None, start_scheduler: bool | None = None) -> FastAPI:
    from ..scheduler import Scheduler
    from ..services import build_services

    cfg = svc.cfg if svc is not None else get_config()
    db = db or get_database()
    svc = svc or build_services(cfg)
    run_scheduler = cfg.scheduler_enabled if start_scheduler is None else start_scheduler

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from .. import runtime

        scheduler = Scheduler(db, svc) if run_scheduler else None
        if scheduler:
            scheduler.start()
            runtime.attach_scheduler(scheduler)
        yield
        if scheduler:
            scheduler.stop()
            runtime.attach_scheduler(None)

    app = FastAPI(title="Press Engine", version=__version__, lifespan=lifespan)
    app.state.db = db
    app.state.svc = svc
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^chrome-extension://[a-p]{32}$",
        allow_origins=cfg.extra_cors_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    @app.exception_handler(LoginRequired)
    async def _login(request: Request, exc: LoginRequired):
        from sqlalchemy import func, select

        from ..models import User

        with db.session() as s:
            first_run = not s.scalar(select(func.count(User.id)))
        if first_run:
            return RedirectResponse("/setup", status_code=303)
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.exception_handler(OwnerRequired)
    async def _owner(request: Request, exc: OwnerRequired):
        return HTMLResponse("<p>Only the owner (D) can do that. <a href='/'>Back</a></p>", status_code=403)

    from .routes_api import router as api_router
    from .routes_desktop import router as desktop_router
    from .routes_web import router as web_router

    app.include_router(api_router)
    app.include_router(desktop_router)
    app.include_router(web_router)
    return app
