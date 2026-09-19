"""One employee's own view.

The dashboard shows a manager everything. This shows a person their own work
and nothing else, which is not a courtesy -- an employee has no business
reading a colleague's hours, and the fit scores the engine gave everyone it
considered would turn an ordinary staffing decision into an argument.

What is deliberately absent: other people's hours, the decision log, and the
scores of the other candidates on a cover request. What is present, because it
is genuinely theirs to know: who picked up their work when they went home.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dashboard import _agent_block, stamp
from app.api.deps import now_dep, settings_dep
from app.api.timeclock import _open_entry
from app.config import Settings
from app.db import get_session
from app.engine.snapshot import load_snapshot
from app.models.audit import DecisionRecord
from app.models.coverage import CoverageOffer, CoverageRequest
from app.models.employee import Employee
from app.models.enums import CoverageStatus, DecisionAction, TaskStatus
from app.models.leave import LeaveRequest
from app.models.task import Task
from app.tokens import TokenError, parse_identity_token

router = APIRouter(tags=["employee"], prefix="/api/me")

RECENT = timedelta(hours=24)


def _authorise(employee_id: int, token: str | None, settings: Settings) -> None:
    """Who may read this person's page.

    In dry run the whole service is a sandbox and the page offers a person
    picker, so there is nothing to protect. Anywhere else the signed link is
    required -- otherwise the convenience that makes the demo pleasant would
    quietly become a way to read any employee's schedule by guessing a number.
    """
    if settings.dry_run:
        return
    if not token:
        raise HTTPException(401, "This page needs the link we sent you.")
    try:
        parsed = parse_identity_token(token, settings.coverage_link_secret)
    except TokenError as exc:
        raise HTTPException(401, f"That link is not valid: {exc}") from exc
    if parsed.employee_id != employee_id:
        raise HTTPException(403, "That link belongs to somebody else.")


@router.get("/switchable")
def switchable(
    session: Session = Depends(get_session), settings: Settings = Depends(settings_dep)
) -> list[dict[str, Any]]:
    """The person picker. Dry run only, and refused outright anywhere else."""
    if not settings.dry_run:
        raise HTTPException(
            403, "Picking a person is a dry-run convenience. Use the link we sent you."
        )
    return [
        {"id": e.id, "name": e.full_name, "role": e.role, "is_manager": e.is_manager}
        for e in session.scalars(
            select(Employee).where(Employee.active.is_(True)).order_by(Employee.full_name)
        )
    ]


@router.get("/whoami")
def whoami(
    token: str = Query(..., description="The signed link we sent you"),
    settings: Settings = Depends(settings_dep),
) -> dict[str, int]:
    """Who a link belongs to.

    The page cannot read the token itself -- it has no secret -- so it asks.
    This returns an id and nothing else: a stolen link should not also be a
    way to confirm someone's name.
    """
    try:
        parsed = parse_identity_token(token, settings.coverage_link_secret)
    except TokenError as exc:
        raise HTTPException(401, f"That link is not valid: {exc}") from exc
    return {"employee_id": parsed.employee_id}


def _handovers(session: Session, employee_id: int, names: dict[int, str]) -> list[dict[str, Any]]:
    """Which of my jobs somebody else is doing, and who.

    Two routes lead here and both matter to the person who went home: a
    colleague on shift was handed the job, or somebody off shift was asked and
    said yes.
    """
    out: list[dict[str, Any]] = []

    for request in session.scalars(
        select(CoverageRequest).where(
            CoverageRequest.vacating_employee_id == employee_id,
            CoverageRequest.status == CoverageStatus.FILLED,
        )
    ):
        task = session.get(Task, request.task_id)
        out.append(
            {
                "task_title": task.title if task else f"task {request.task_id}",
                "taken_by": names.get(request.filled_by_id, "somebody"),
                "how": "volunteered",
                "at": None,
            }
        )

    for record in session.scalars(
        select(DecisionRecord)
        .where(DecisionRecord.action == DecisionAction.AUTO_REASSIGNED)
        .order_by(DecisionRecord.created_at.desc(), DecisionRecord.id.desc())
        .limit(200)
    ):
        details = record.details or {}
        if details.get("from_employee_id") != employee_id:
            continue
        task = session.get(Task, details.get("task_id")) if details.get("task_id") else None
        out.append(
            {
                "task_title": task.title if task else record.summary,
                "taken_by": names.get(details.get("to_employee_id"), "somebody"),
                "how": "reassigned",
                "at": record.created_at.isoformat() + "Z",
            }
        )
    return out


@router.get("/{employee_id}")
def me(
    employee_id: int,
    token: str | None = Query(default=None, description="The signed link we sent you"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(settings_dep),
    now: datetime = Depends(now_dep),
) -> dict[str, Any]:
    _authorise(employee_id, token, settings)

    employee = session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(404, "No such employee")

    snapshot = load_snapshot(session, settings, now)
    facts = snapshot.get(employee_id)
    names = {e.id: e.full_name for e in session.scalars(select(Employee))}
    soon = now + timedelta(minutes=1)

    # --- my shift ---------------------------------------------------------
    shift = None
    if facts is not None and facts.shifts:
        upcoming = sorted(facts.shifts, key=lambda s: s.starts_at)
        current = next((s for s in upcoming if s.covers(now)), None) or next(
            (s for s in upcoming if s.starts_at > now), None
        )
        if current is not None:
            shift = {
                "name": current.name,
                "location": current.location,
                "starts_at": stamp(current.starts_at, settings),
                "ends_at": stamp(current.ends_at, settings),
                "in_progress": current.covers(now),
            }

    # --- my jobs -----------------------------------------------------------
    tasks = [
        {
            "id": task.id,
            "title": task.title,
            "station": task.station,
            "priority": task.priority,
            "status": task.status,
            "starts_at": stamp(task.starts_at, settings),
            "due_at": stamp(task.due_at, settings),
            "deadline": stamp(task.deadline, settings),
            "order_code": task.order.code if task.order else None,
            "pickup_at": stamp(task.order.pickup_at, settings) if task.order else None,
            "driver": task.order.driver_service if task.order else None,
        }
        for task in session.scalars(
            select(Task)
            .where(
                Task.assignee_id == employee_id,
                Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED]),
                Task.due_at > now - timedelta(hours=1),
            )
            .order_by(Task.starts_at, Task.id)
        )
    ]

    # --- what I have been asked to cover -----------------------------------
    cover_requests = []
    for offer in session.scalars(
        select(CoverageOffer)
        .where(CoverageOffer.employee_id == employee_id, CoverageOffer.sent_at >= now - RECENT)
        .order_by(CoverageOffer.sent_at.desc())
    ):
        request = session.get(CoverageRequest, offer.coverage_request_id)
        task = session.get(Task, request.task_id) if request else None
        if request is None or task is None:
            continue
        cover_requests.append(
            {
                "id": request.id,
                "offer_id": offer.id,
                "request_status": request.status,
                "my_status": offer.status,
                "task_title": task.title,
                "station": task.station,
                "estimated_minutes": task.estimated_minutes,
                "order_code": task.order.code if task.order else None,
                "starts_at": stamp(task.starts_at, settings),
                "due_at": stamp(task.due_at, settings),
                "deadline": stamp(task.deadline, settings),
                "expires_at": stamp(request.expires_at, settings),
                "created_at": stamp(request.created_at, settings),
                "asked_because": request.reason,
                # Who is covering, once it is settled -- not who else was asked.
                "filled_by": names.get(request.filled_by_id),
            }
        )

    # --- my leave ----------------------------------------------------------
    leave = [
        {
            "id": row.id,
            "status": row.status,
            "leave_type": row.leave_type,
            "reason": row.reason,
            "starts_at": stamp(row.starts_at, settings),
            "ends_at": stamp(row.ends_at, settings),
            "decided_by": row.decided_by,
            "decided_at": stamp(row.decided_at, settings),
        }
        for row in session.scalars(
            select(LeaveRequest)
            .where(LeaveRequest.employee_id == employee_id, LeaveRequest.ends_at > now - RECENT)
            .order_by(LeaveRequest.created_at.desc())
        )
    ]

    open_entry = _open_entry(session, employee_id)
    limits = {
        "daily_hours": settings.max_daily_hours,
        "weekly_hours": settings.max_weekly_hours,
    }
    return {
        "agent": _agent_block(session, settings, now),
        "me": {
            "id": employee.id,
            "name": employee.full_name,
            "role": employee.role,
            "is_manager": employee.is_manager,
            "slack_user_id": employee.slack_user_id,
            "on_shift": bool(facts and facts.on_shift_during(now, soon)),
            "on_leave": bool(facts and facts.on_leave_during(now, soon)),
            "skills": [
                {"code": link.skill.code, "name": link.skill.name, "level": link.proficiency}
                for link in employee.skills
            ],
        },
        "hours": {
            # The same figures the guardrails read, so what an employee sees
            # is what the engine used when it decided whether to ask them.
            "today": round((facts.minutes_today if facts else 0) / 60.0, 1),
            "week": round((facts.minutes_week if facts else 0) / 60.0, 1),
            # Being on the clock is having an open time entry, not being
            # rostered: somebody can be scheduled and not yet have arrived.
            "on_the_clock": open_entry is not None,
            "clocked_in_at": stamp(open_entry.clock_in, settings) if open_entry else None,
            **limits,
        },
        "shift": shift,
        "tasks": tasks,
        "cover_requests": cover_requests,
        "leave": leave,
        "handovers": _handovers(session, employee_id, names),
        "can": {
            "switch_people": settings.dry_run,
            "clock": True,
            "request_leave": True,
        },
    }
