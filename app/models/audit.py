"""The audit trail.

Every autonomous action writes one of these before it takes effect.  A record
carries not just what the system did but *why*: the candidates it looked at,
the score each one got, and every guardrail it evaluated with the reason it
passed or failed.  If a staffing decision is ever challenged, this row is the
answer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class DecisionRecord(Base):
    __tablename__ = "decision_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    actor: Mapped[str] = mapped_column(String(80), default="system")
    """``system`` or ``manager:<employee id>``."""

    trigger: Mapped[str] = mapped_column(String(80))
    """What set this off, e.g. ``leave_request`` or ``coverage_timeout``."""

    action: Mapped[str] = mapped_column(String(40), index=True)
    subject_type: Mapped[str] = mapped_column(String(40), default="")
    subject_id: Mapped[int | None] = mapped_column(default=None)
    summary: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    """Candidates with scores, rule evaluations, impact set, messages sent."""

    reverted_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("decision_records.id", ondelete="SET NULL"), default=None
    )
    reverted_by: Mapped[DecisionRecord | None] = relationship(remote_side=[id])

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<DecisionRecord {self.id} {self.action} {self.subject_type}:{self.subject_id}>"
