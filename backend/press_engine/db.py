"""Database engine and session helpers (SQLite by default, Postgres-ready)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            if ":memory:" not in url:
                cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    return engine


class Database:
    """Owns the engine and session factory so tests can swap in their own."""

    def __init__(self, url: str):
        self.url = url
        self.engine = make_engine(url)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        from . import models  # noqa: F401  (register tables)

        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self.SessionLocal()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()


_db: Database | None = None


def get_database() -> Database:
    global _db
    if _db is None:
        from .config import get_config

        _db = Database(get_config().resolved_database_url)
        _db.create_all()
    return _db


def set_database(db: Database) -> None:
    global _db
    _db = db
