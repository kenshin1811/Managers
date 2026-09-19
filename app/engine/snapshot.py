"""A read-only picture of the workforce at one moment.

Loading this once and handing it to the pure rule functions keeps the engine
off the database while it is thinking, and keeps the query count flat no
matter how many people or tasks are involved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import day_bounds, week_bounds
from app.config import Settings
from app.models.coverage import CoverageOffer
from app.models.employee import Employee
from app.models.enums import CoverageStatus, LeaveStatus, OfferStatus
from app.models.leave import LeaveRequest
from app.models.shift import Shift, ShiftAssignment
from app.models.task import Task
from app.models.timeclock import TimeEntry

FAIRNESS_LOOKBACK_DAYS = 30


@dataclass
class TentativeCommitment:
    """Work the current plan has already earmarked for this person.

    Without this, a plan that reassigns three tasks from one leaving employee
    would happily hand all three to the same colleague for the same ten
    minutes.  Tentative commitments make each decision aware of the ones
    before it.
    """

    task_id: int
    starts_at: datetime
    ends_at: datetime
    minutes: float
    priority_rank: int
    adds_hours: bool


@dataclass
class EmployeeFacts:
    """Everything the guardrails need to know about one person."""

    employee: Employee
    minutes_today: float = 0.0
    minutes_week: float = 0.0
    last_worked_end: datetime | None = None
    shifts: list[Shift] = field(default_factory=list)
    live_tasks: list[Task] = field(default_factory=list)
    leaves: list[LeaveRequest] = field(default_factory=list)
    asks_today: int = 0
    recent_covers: int = 0
    tentative: list[TentativeCommitment] = field(default_factory=list)

    # -- schedule questions ------------------------------------------------
    def on_shift_during(self, starts_at: datetime, ends_at: datetime) -> bool:
        return any(shift.overlaps(starts_at, ends_at) for shift in self.shifts)

    def shift_covering(self, starts_at: datetime, ends_at: datetime) -> Shift | None:
        for shift in self.shifts:
            if shift.starts_at <= starts_at and ends_at <= shift.ends_at:
                return shift
        return None

    def on_leave_during(self, starts_at: datetime, ends_at: datetime) -> bool:
        return any(leave.starts_at < ends_at and starts_at < leave.ends_at for leave in self.leaves)

    def conflicting_tasks(
        self, starts_at: datetime, ends_at: datetime, exclude_task_id: int | None = None
    ) -> list[tuple[int, int]]:
        """``(task_id, priority_rank)`` for everything that clashes with the window."""
        clashes: list[tuple[int, int]] = []
        for task in self.live_tasks:
            if task.id == exclude_task_id:
                continue
            if task.overlaps(starts_at, ends_at):
                clashes.append((task.id, task.priority_rank))
        for commitment in self.tentative:
            if commitment.task_id == exclude_task_id:
                continue
            if commitment.starts_at < ends_at and starts_at < commitment.ends_at:
                clashes.append((commitment.task_id, commitment.priority_rank))
        return clashes

    # -- hours -------------------------------------------------------------
    def tentative_minutes(self) -> float:
        return sum(c.minutes for c in self.tentative if c.adds_hours)

    def projected_today(self, extra_minutes: float = 0.0) -> float:
        return self.minutes_today + self.tentative_minutes() + extra_minutes

    def projected_week(self, extra_minutes: float = 0.0) -> float:
        return self.minutes_week + self.tentative_minutes() + extra_minutes

    def rest_hours_before(self, moment: datetime) -> float | None:
        """Hours of rest they would get before ``moment``, or ``None`` if unknown."""
        if self.last_worked_end is None or self.last_worked_end >= moment:
            return None
        return (moment - self.last_worked_end).total_seconds() / 3600.0


@dataclass
class WorkforceSnapshot:
    now: datetime
    settings: Settings
    facts: dict[int, EmployeeFacts] = field(default_factory=dict)

    def all_facts(self) -> list[EmployeeFacts]:
        return list(self.facts.values())

    def get(self, employee_id: int) -> EmployeeFacts | None:
        return self.facts.get(employee_id)

    def commit_tentatively(
        self,
        employee_id: int,
        *,
        task_id: int,
        starts_at: datetime,
        ends_at: datetime,
        minutes: float,
        priority_rank: int,
        adds_hours: bool,
    ) -> None:
        """Earmark work for someone so later steps of the same plan can see it."""
        facts = self.facts.get(employee_id)
        if facts is None:
            return
        facts.tentative.append(
            TentativeCommitment(
                task_id=task_id,
                starts_at=starts_at,
                ends_at=ends_at,
                minutes=minutes,
                priority_rank=priority_rank,
                adds_hours=adds_hours,
            )
        )

    def note_ask(self, employee_id: int) -> None:
        facts = self.facts.get(employee_id)
        if facts is not None:
            facts.asks_today += 1


def load_snapshot(
    session: Session,
    settings: Settings,
    now: datetime,
    *,
    horizon_end: datetime | None = None,
) -> WorkforceSnapshot:
    """Read every fact the guardrails need, in a handful of queries."""
    horizon_start = now - timedelta(days=1)
    horizon_end = horizon_end or (now + timedelta(days=2))

    day_start, day_end = day_bounds(now, settings.business_tz)
    week_start, week_end = week_bounds(now, settings.business_tz)

    employees = list(session.scalars(select(Employee).where(Employee.active.is_(True))))
    snapshot = WorkforceSnapshot(now=now, settings=settings)
    for employee in employees:
        snapshot.facts[employee.id] = EmployeeFacts(employee=employee)

    # Shifts they are scheduled on anywhere near the horizon.
    shift_rows = session.execute(
        select(ShiftAssignment, Shift)
        .join(Shift, Shift.id == ShiftAssignment.shift_id)
        .where(Shift.ends_at > horizon_start, Shift.starts_at < horizon_end)
    ).all()
    for assignment, shift in shift_rows:
        facts = snapshot.facts.get(assignment.employee_id)
        if facts is not None and assignment.status != "released":
            facts.shifts.append(shift)

    # Work they already hold.
    for task in session.scalars(
        select(Task).where(
            Task.assignee_id.is_not(None),
            Task.status.in_(["pending", "in_progress", "blocked"]),
            Task.due_at > horizon_start,
            Task.starts_at < horizon_end,
        )
    ):
        facts = snapshot.facts.get(task.assignee_id)
        if facts is not None:
            facts.live_tasks.append(task)

    # Leave that is already on the books. A request still hunting for cover
    # counts too -- that person is on their way out of the door.
    for leave in session.scalars(
        select(LeaveRequest).where(
            LeaveRequest.status.in_([LeaveStatus.APPROVED, LeaveStatus.PENDING_COVERAGE]),
            LeaveRequest.ends_at > horizon_start,
            LeaveRequest.starts_at < horizon_end,
        )
    ):
        facts = snapshot.facts.get(leave.employee_id)
        if facts is not None:
            facts.leaves.append(leave)

    # Hours already worked, today and this week.
    for entry in session.scalars(
        select(TimeEntry).where(TimeEntry.clock_in < week_end, TimeEntry.clock_in >= week_start)
    ):
        facts = snapshot.facts.get(entry.employee_id)
        if facts is None:
            continue
        worked = entry.worked_minutes(until=now)
        facts.minutes_week += worked
        if day_start <= entry.clock_in < day_end:
            facts.minutes_today += worked
        end = entry.clock_out or (now if entry.is_open else None)
        if end is not None and (facts.last_worked_end is None or end > facts.last_worked_end):
            facts.last_worked_end = end

    # Scheduled-but-not-yet-worked shift time still counts against the limits:
    # somebody at nine hours scheduled is not a candidate for a tenth.
    for facts in snapshot.facts.values():
        for shift in facts.shifts:
            remaining_start = max(shift.starts_at, now)
            if remaining_start >= shift.ends_at:
                continue
            remaining = (shift.ends_at - remaining_start).total_seconds() / 60.0
            facts.minutes_week += remaining
            if day_start <= shift.starts_at < day_end:
                facts.minutes_today += remaining
            if facts.last_worked_end is None or shift.ends_at > facts.last_worked_end:
                facts.last_worked_end = shift.ends_at

    # How often we have already bothered them, and how often they have said yes.
    fairness_since = now - timedelta(days=FAIRNESS_LOOKBACK_DAYS)
    for offer in session.scalars(
        select(CoverageOffer).where(CoverageOffer.sent_at >= fairness_since)
    ):
        facts = snapshot.facts.get(offer.employee_id)
        if facts is None:
            continue
        if day_start <= offer.sent_at < day_end:
            facts.asks_today += 1
        if offer.status == OfferStatus.ACCEPTED:
            facts.recent_covers += 1

    return snapshot


def open_coverage_task_ids(session: Session) -> set[int]:
    """Tasks that already have a hunt running, so they are not started twice."""
    from app.models.coverage import CoverageRequest

    return set(
        session.scalars(
            select(CoverageRequest.task_id).where(CoverageRequest.status == CoverageStatus.OPEN)
        )
    )
