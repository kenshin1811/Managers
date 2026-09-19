"""Time tracking.

This is not bookkeeping for its own sake: hours worked are a *hard input* to
the reassignment engine.  Asking someone to cover is only legal and humane if
it keeps them under their daily and weekly limits, and those numbers come from
here.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class TimeEntry(Base):
    __tablename__ = "time_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    shift_id: Mapped[int | None] = mapped_column(
        ForeignKey("shifts.id", ondelete="SET NULL"), default=None
    )
    clock_in: Mapped[datetime] = mapped_column(DateTime, index=True)
    clock_out: Mapped[datetime | None] = mapped_column(DateTime, default=None, index=True)
    break_minutes: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(20), default="api")

    employee: Mapped[Employee] = relationship(lazy="selectin")  # noqa: F821

    @property
    def is_open(self) -> bool:
        return self.clock_out is None

    def worked_minutes(self, until: datetime | None = None) -> float:
        """Paid minutes so far, breaks deducted.

        An open entry is measured up to ``until`` so that an employee who is
        still on the clock counts against their daily limit right now, not
        after they go home.
        """
        end = self.clock_out or until
        if end is None or end <= self.clock_in:
            return 0.0
        total = (end - self.clock_in).total_seconds() / 60.0
        # The column default only lands on insert, so a transient entry can
        # still have `break_minutes` unset when this is called.
        return max(0.0, total - (self.break_minutes or 0))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<TimeEntry {self.id} emp={self.employee_id} open={self.is_open}>"
