"""Database engine, session factory and declarative base."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings


def _is_memory(url: str) -> bool:
    return url.startswith("sqlite") and (":memory:" in url or url in ("sqlite://", "sqlite:///"))


class Base(DeclarativeBase):
    """Declarative base for every model."""


def build_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url
    connect_args: dict = {}
    kwargs: dict = {}
    if url.startswith("sqlite"):
        # The scheduler and the request handlers touch the DB from different
        # threads; SQLite's default thread check would reject that.
        connect_args["check_same_thread"] = False
    if _is_memory(url):
        # An in-memory SQLite database lives inside its connection, and the
        # default pool hands out one connection per thread -- so a request
        # served on a worker thread would find an empty, tableless database.
        # StaticPool shares the single connection that actually holds the data.
        kwargs["poolclass"] = StaticPool
    engine = create_engine(url, future=True, connect_args=connect_args, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver glue
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


engine: Engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def create_all(target: Engine | None = None) -> None:
    from app import models  # noqa: F401  -- registers every mapper

    Base.metadata.create_all(target or engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope(factory=None) -> Iterator[Session]:
    """Transactional scope for scripts, the scheduler and tests."""
    session = (factory or SessionLocal)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
