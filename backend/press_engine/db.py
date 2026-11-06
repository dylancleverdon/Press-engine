"""Database engine and session helpers (SQLite by default, Postgres-ready)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import literal as sa_literal
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

log = logging.getLogger(__name__)


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
        upgrade_schema(self.engine)

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


def upgrade_schema(engine: Engine) -> list[str]:
    """Add columns that newer versions define but an existing database lacks.

    Automatic updates deliver new code to an existing database, so additive
    changes (new tables, new columns) are applied on startup. New tables come
    from create_all; this handles new columns. Renames and drops still need a
    hand-written migration.
    """
    insp = sa_inspect(engine)
    added: list[str] = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(dialect=engine.dialect)}'
                default = col.default.arg if col.default is not None and col.default.is_scalar else None
                if default is not None:
                    literal = sa_literal(default).compile(dialect=engine.dialect, compile_kwargs={"literal_binds": True})
                    ddl += f" DEFAULT {literal}"
                    if not col.nullable:
                        ddl += " NOT NULL"
                conn.execute(text(ddl))
                added.append(f"{table.name}.{col.name}")
                log.info("schema upgrade: added %s.%s", table.name, col.name)
    return added


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
