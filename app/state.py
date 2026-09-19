"""Reading and writing the system's own heartbeat."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.state import SystemState


def touch(session: Session, key: str, now: datetime, value: str = "") -> None:
    row = session.get(SystemState, key)
    if row is None:
        session.add(SystemState(key=key, value=value, updated_at=now))
    else:
        row.updated_at = now
        row.value = value
    session.flush()


def last_seen(session: Session, key: str) -> datetime | None:
    row = session.get(SystemState, key)
    return row.updated_at if row else None
