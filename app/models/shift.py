"""Scheduled work blocks and who is on them."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import ShiftAssignmentStatus


class Shift(Base):
    __tablename__ = "shifts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), default="shift")
    location: Mapped[str] = mapped_column(String(120), default="main")
    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime, index=True)

    assignments: Mapped[list[ShiftAssignment]] = relationship(
        back_populates="shift", cascade="all, delete-orphan", lazy="selectin"
    )

    def covers(self, moment: datetime) -> bool:
        return self.starts_at <= moment < self.ends_at

    def overlaps(self, starts_at: datetime, ends_at: datetime) -> bool:
        return self.starts_at < ends_at and starts_at < self.ends_at

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Shift {self.id} {self.name} {self.starts_at:%Y-%m-%d %H:%M}>"


class ShiftAssignment(Base):
    __tablename__ = "shift_assignments"
    __table_args__ = (UniqueConstraint("shift_id", "employee_id", name="uq_shift_employee"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("shifts.id", ondelete="CASCADE"), index=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default=ShiftAssignmentStatus.SCHEDULED)

    shift: Mapped[Shift] = relationship(back_populates="assignments", lazy="selectin")
