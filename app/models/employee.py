"""People, what they can do, and when they are willing to be asked."""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Skill {self.code}>"


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(200), unique=True, default=None)
    slack_user_id: Mapped[str | None] = mapped_column(String(50), unique=True, default=None)
    role: Mapped[str] = mapped_column(String(80), default="staff")
    is_manager: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    skills: Mapped[list[EmployeeSkill]] = relationship(
        back_populates="employee", cascade="all, delete-orphan", lazy="selectin"
    )
    availability: Mapped[list[AvailabilityWindow]] = relationship(
        back_populates="employee", cascade="all, delete-orphan", lazy="selectin"
    )

    def proficiency_in(self, skill_id: int) -> int | None:
        """Their level in a skill, or ``None`` if they do not have it at all."""
        for link in self.skills:
            if link.skill_id == skill_id:
                return link.proficiency
        return None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Employee {self.id} {self.full_name}>"


class EmployeeSkill(Base):
    __tablename__ = "employee_skills"
    __table_args__ = (UniqueConstraint("employee_id", "skill_id", name="uq_employee_skill"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id", ondelete="CASCADE"))
    proficiency: Mapped[int] = mapped_column(Integer, default=3)
    """1 (can do it under supervision) to 5 (trains others)."""

    employee: Mapped[Employee] = relationship(back_populates="skills")
    skill: Mapped[Skill] = relationship(lazy="selectin")


class AvailabilityWindow(Base):
    """When an off-shift employee is willing to be asked to come in.

    Minutes from local midnight, so a window can be compared without dragging
    dates into it.  A window that ends before it starts wraps past midnight.
    """

    __tablename__ = "availability_windows"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"))
    weekday: Mapped[int] = mapped_column(Integer)
    """0 = Monday, matching ``datetime.weekday()``."""

    start_minute: Mapped[int] = mapped_column(Integer, default=0)
    end_minute: Mapped[int] = mapped_column(Integer, default=24 * 60)

    employee: Mapped[Employee] = relationship(back_populates="availability")

    def covers(self, weekday: int, minute_of_day: int) -> bool:
        if self.weekday != weekday:
            return False
        if self.start_minute <= self.end_minute:
            return self.start_minute <= minute_of_day < self.end_minute
        # Wraps past midnight, e.g. 22:00 -> 02:00.
        return minute_of_day >= self.start_minute or minute_of_day < self.end_minute
