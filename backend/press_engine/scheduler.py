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

from .appsettings import load_settings
from .db import Database
from .jobs import last_run, run_job
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

    def fast_tick(self) -> None:
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
