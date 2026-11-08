"""Background scheduler.

Two threads so a long discovery run never delays the drip:
* slow loop - daily discovery and daily queue building (local time of settings.timezone);
  missed runs (machine asleep) are caught up once on wake.
* fast loop - send due emails every minute, poll Gmail for replies every few minutes.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

from . import runtime, updater
from .appsettings import load_settings
from .db import Database
from .jobs import last_run, run_job
from .models import JobRun
from .util.timeutil import get_tz, parse_hhmm, to_local, utcnow

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, db: Database, svc, *, clock: Callable[[], datetime] = utcnow, tick_seconds: float = 20):
        self.db = db
        self.svc = svc
        self.clock = clock
        self.tick_seconds = tick_seconds
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last: dict[str, datetime] = {}
        self._fast_lock = threading.Lock()  # held while sending; updates wait for it

    # --- decisions (pure enough to unit-test) ---
    def daily_due(self, job: str, hhmm: str, tz_name: str, now: datetime) -> bool:
        tz = get_tz(tz_name)
        local = to_local(now, tz)
        if local.time() < parse_hhmm(hhmm):
            return False
        with self.db.session() as s:
            prev = last_run(s, job)
        return prev is None or to_local(prev.started_at, tz).date() < local.date()

    def interval_due(self, key: str, every: timedelta, now: datetime) -> bool:
        prev = self._last.get(key)
        return prev is None or now - prev >= every

    # --- loops ---
    def slow_tick(self) -> None:
        now = self.clock()
        with self.db.session() as s:
            settings = load_settings(s)
        if self.daily_due("discover", settings.discovery_time, settings.timezone, now):
            log.info("running daily discovery")
            run_job(self.db, self.svc, "discover", now=now)
        now = self.clock()
        if self.daily_due("queue", settings.queue_time, settings.timezone, now):
            log.info("building today's queue")
            run_job(self.db, self.svc, "queue", now=now)
        self.update_tick(self.clock(), settings)
        self.restart_tick(self.clock(), settings)

    def update_tick(self, now: datetime, settings) -> None:
        """Mac app only: look for a new release every few hours and install it."""
        cfg = self.svc.cfg
        if not (cfg.desktop and settings.auto_update and cfg.app_support_dir and cfg.uv_path):
            return
        every = timedelta(hours=cfg.update_check_hours)
        if "update" not in self._last:  # first check a couple of minutes after starting
            self._last["update"] = now - every + timedelta(minutes=2)
            return
        if not self.interval_due("update", every, now):
            return
        self._last["update"] = now
        state = updater.maybe_update(self.svc.http, cfg, now=now)
        if state.get("installed") or state.get("error"):
            with self.db.session() as s:
                s.add(JobRun(job="update", started_at=now, finished_at=self.clock(), ok=not state.get("error"),
                             stats={"installed": state.get("installed"), "latest": state.get("latest")}, error=state.get("error")))
        # The restart itself waits for the banner's countdown (restart_tick).

    def restart_tick(self, now: datetime, settings) -> None:
        """Restart into an installed update once its countdown is over and nothing's running."""
        from . import progress
        from .api import views

        cfg = self.svc.cfg
        if not cfg.desktop:
            return
        busy = bool(views.running_jobs()) or bool(progress.current())
        local_hour = to_local(now, get_tz(settings.timezone)).hour
        if updater.restart_due(cfg, now, busy=busy, local_hour=local_hour):
            state = updater.read_state(cfg)
            runtime.request_restart(f"update to {state.get('installed')}", delay=0)

    def quiesce(self) -> None:
        """Wait for an email that's being sent right now, then start no new work."""
        with self._fast_lock:
            self._stop.set()

    def fast_tick(self) -> None:
        with self._fast_lock:
            if not self._stop.is_set():
                self._fast_tick()

    def _fast_tick(self) -> None:
        now = self.clock()
        with self.db.session() as s:
            settings = load_settings(s)
        if self.interval_due("send", timedelta(seconds=55), now):
            self._last["send"] = now
            run_job(self.db, self.svc, "send", now=now, quiet=True)
        if self.interval_due("replies", timedelta(minutes=max(1, settings.reply_poll_minutes)), now):
            self._last["replies"] = now
            new_threads = self.interval_due("replies_new_threads", timedelta(hours=1), now)
            if new_threads:
                self._last["replies_new_threads"] = now
            run_job(self.db, self.svc, "replies", now=now, quiet=True, include_new_threads=new_threads)
        if self.interval_due("sheet_sync", timedelta(minutes=10), now):
            self._last["sheet_sync"] = now
            run_job(self.db, self.svc, "sheet_sync", now=now, quiet=True)

    def _loop(self, tick: Callable[[], None], name: str) -> None:
        while not self._stop.is_set():
            try:
                tick()
            except Exception:  # never let the loop die
                log.exception("%s loop error", name)
            self._stop.wait(self.tick_seconds)

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for name, tick in (("slow", self.slow_tick), ("fast", self.fast_tick)):
            t = threading.Thread(target=self._loop, args=(tick, name), name=f"press-scheduler-{name}", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("scheduler started")

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout)
        self._threads.clear()
