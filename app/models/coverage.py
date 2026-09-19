"""Cover requests: the system asking other employees to fill in."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import CoverageStatus, OfferStatus


class CoverageRequest(Base):
    """One piece of work that needs somebody, and the hunt for that somebody."""

    __tablename__ = "coverage_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    leave_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("leave_requests.id", ondelete="SET NULL"), default=None, index=True
    )
    vacating_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default=CoverageStatus.OPEN, index=True)
    wave: Mapped[int] = mapped_column(Integer, default=1)
    """Which round of asking this is. Each timeout widens the pool by one."""

    created_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    filled_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None
    )
    filled_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    task: Mapped[Task] = relationship(lazy="selectin")  # noqa: F821
    offers: Mapped[list[CoverageOffer]] = relationship(
        back_populates="request", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def is_open(self) -> bool:
        return self.status == CoverageStatus.OPEN

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CoverageRequest {self.id} task={self.task_id} {self.status}>"


class CoverageOffer(Base):
    """One person being asked. First acceptance wins; the rest are superseded."""

    __tablename__ = "coverage_offers"

    id: Mapped[int] = mapped_column(primary_key=True)
    coverage_request_id: Mapped[int] = mapped_column(
        ForeignKey("coverage_requests.id", ondelete="CASCADE"), index=True
    )
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default=OfferStatus.SENT)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    sent_at: Mapped[datetime] = mapped_column(DateTime)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    channel_ref: Mapped[str | None] = mapped_column(String(120), default=None)
    """Slack message ts, so the message can be rewritten once it is resolved."""

    request: Mapped[CoverageRequest] = relationship(back_populates="offers")
    employee: Mapped[Employee] = relationship(lazy="selectin")  # noqa: F821
