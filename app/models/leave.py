"""Leave requests -- the event that sets the whole decision flow in motion."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import LeaveStatus, LeaveType


class LeaveRequest(Base):
    __tablename__ = "leave_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    leave_type: Mapped[str] = mapped_column(String(20), default=LeaveType.PERSONAL)
    reason: Mapped[str] = mapped_column(Text, default="")
    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(30), default=LeaveStatus.PENDING_COVERAGE)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    decided_by: Mapped[str | None] = mapped_column(String(80), default=None)
    """``system`` or ``manager:<employee id>`` -- who actually made the call."""

    notes: Mapped[str] = mapped_column(Text, default="")

    employee: Mapped[Employee] = relationship(lazy="selectin")  # noqa: F821

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<LeaveRequest {self.id} emp={self.employee_id} {self.status}>"
