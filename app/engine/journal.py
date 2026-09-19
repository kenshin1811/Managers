"""Writing the audit trail.

Every autonomous action goes through here.  The rule the rest of the engine
follows is simple: record first, act second.  If the process dies halfway, the
record of what it was trying to do survives.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import DecisionRecord
from app.models.enums import DecisionAction


def record_decision(
    session: Session,
    *,
    now: datetime,
    action: DecisionAction,
    trigger: str,
    summary: str,
    subject_type: str = "",
    subject_id: int | None = None,
    details: dict[str, Any] | None = None,
    actor: str = "system",
) -> DecisionRecord:
    record = DecisionRecord(
        created_at=now,
        actor=actor,
        trigger=trigger,
        action=str(action),
        subject_type=subject_type,
        subject_id=subject_id,
        summary=summary,
        details=details or {},
    )
    session.add(record)
    session.flush()
    return record


def recent_decisions(session: Session, limit: int = 50) -> list[DecisionRecord]:
    return list(
        session.scalars(
            select(DecisionRecord)
            .order_by(DecisionRecord.created_at.desc(), DecisionRecord.id.desc())
            .limit(limit)
        )
    )
