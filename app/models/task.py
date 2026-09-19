"""A unit of work assigned to one person -- the "detailed job assignment"."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import TaskPriority, TaskStatus


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    station: Mapped[str] = mapped_column(String(80), default="")

    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), default=None, index=True
    )
    assignee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), default=None, index=True
    )
    required_skill_id: Mapped[int | None] = mapped_column(
        ForeignKey("skills.id", ondelete="SET NULL"), default=None
    )
    min_proficiency: Mapped[int] = mapped_column(Integer, default=1)

    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    estimated_minutes: Mapped[int] = mapped_column(Integer, default=15)
    status: Mapped[str] = mapped_column(String(20), default=TaskStatus.PENDING)
    priority: Mapped[str] = mapped_column(String(20), default=TaskPriority.ROUTINE)

    order: Mapped[Order | None] = relationship(  # noqa: F821
        back_populates="tasks", lazy="selectin"
    )
    assignee: Mapped[Employee | None] = relationship(lazy="selectin")  # noqa: F821
    required_skill: Mapped[Skill | None] = relationship(lazy="selectin")  # noqa: F821

    @property
    def priority_rank(self) -> int:
        return TaskPriority(self.priority).rank

    @property
    def is_live(self) -> bool:
        """Still needs doing by somebody."""
        return self.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)

    def overlaps(self, starts_at: datetime, ends_at: datetime) -> bool:
        return self.starts_at < ends_at and starts_at < self.due_at

    @property
    def deadline(self) -> datetime:
        """The moment that actually matters: the driver, if there is one."""
        return self.order.pickup_at if self.order is not None else self.due_at

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Task {self.id} {self.title!r} assignee={self.assignee_id}>"
