"""Time tracking.

Clocking in and out is the boring half of this endpoint's job.  The useful
half is that these numbers are what the reassignment guardrails read when they
decide whether somebody can be asked to stay on.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import now_dep, settings_dep
from app.api.schemas import ClockIn, ClockOut, HoursSummary, TimeEntryOut
from app.clock import day_bounds, week_bounds
from app.db import get_session
from app.models.employee import Employee
from app.models.timeclock import TimeEntry

router = APIRouter(tags=["time tracking"], prefix="/timeclock")


def _open_entry(session: Session, employee_id: int) -> TimeEntry | None:
    return session.scalar(
        select(TimeEntry)
        .where(TimeEntry.employee_id == employee_id, TimeEntry.clock_out.is_(None))
        .order_by(TimeEntry.clock_in.desc())
    )


@router.post("/clock-in", response_model=TimeEntryOut, status_code=201)
def clock_in(
    payload: ClockIn, session: Session = Depends(get_session), now=Depends(now_dep)
) -> TimeEntry:
    if session.get(Employee, payload.employee_id) is None:
        raise HTTPException(404, "No such employee")
    if _open_entry(session, payload.employee_id) is not None:
        raise HTTPException(409, "Already clocked in")

    entry = TimeEntry(
        employee_id=payload.employee_id,
        shift_id=payload.shift_id,
        clock_in=payload.at or now,
        source=payload.source,
    )
    session.add(entry)
    session.commit()
    return entry


@router.post("/clock-out", response_model=TimeEntryOut)
def clock_out(
    payload: ClockOut, session: Session = Depends(get_session), now=Depends(now_dep)
) -> TimeEntry:
    entry = _open_entry(session, payload.employee_id)
    if entry is None:
        raise HTTPException(409, "Not clocked in")
    entry.clock_out = payload.at or now
    entry.break_minutes = payload.break_minutes
    if entry.clock_out <= entry.clock_in:
        raise HTTPException(400, "Clock-out must be after clock-in")
    session.commit()
    return entry


@router.get("/entries", response_model=list[TimeEntryOut])
def list_entries(
    employee_id: int | None = None, session: Session = Depends(get_session)
) -> list[TimeEntry]:
    query = select(TimeEntry).order_by(TimeEntry.clock_in.desc())
    if employee_id is not None:
        query = query.where(TimeEntry.employee_id == employee_id)
    return list(session.scalars(query))


@router.get("/summary/{employee_id}", response_model=HoursSummary)
def hours_summary(
    employee_id: int,
    session: Session = Depends(get_session),
    settings=Depends(settings_dep),
    now=Depends(now_dep),
) -> HoursSummary:
    """Hours worked today and this week, against the limits the engine enforces."""
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(404, "No such employee")

    day_start, day_end = day_bounds(now, settings.business_tz)
    week_start, week_end = week_bounds(now, settings.business_tz)

    minutes_today = 0.0
    minutes_week = 0.0
    on_the_clock = False
    for entry in session.scalars(
        select(TimeEntry).where(
            TimeEntry.employee_id == employee_id,
            TimeEntry.clock_in >= week_start,
            TimeEntry.clock_in < week_end,
        )
    ):
        worked = entry.worked_minutes(until=now)
        minutes_week += worked
        if day_start <= entry.clock_in < day_end:
            minutes_today += worked
        on_the_clock = on_the_clock or entry.is_open

    return HoursSummary(
        employee_id=employee.id,
        full_name=employee.full_name,
        hours_today=round(minutes_today / 60.0, 2),
        hours_week=round(minutes_week / 60.0, 2),
        daily_limit=settings.max_daily_hours,
        weekly_limit=settings.max_weekly_hours,
        on_the_clock=on_the_clock,
    )
