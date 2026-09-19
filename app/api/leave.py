"""Leave requests -- the trigger for everything else."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import notifier_dep, now_dep, settings_dep
from app.api.schemas import LeaveDecision, LeaveIn, LeaveOut, LeaveResult
from app.db import get_session
from app.engine import reassignment
from app.engine.journal import record_decision
from app.models.employee import Employee
from app.models.enums import DecisionAction, LeaveStatus
from app.models.leave import LeaveRequest

router = APIRouter(tags=["leave"], prefix="/leave-requests")


@router.post("", response_model=LeaveResult, status_code=201)
def request_leave(
    payload: LeaveIn,
    session: Session = Depends(get_session),
    settings=Depends(settings_dep),
    notifier=Depends(notifier_dep),
    now=Depends(now_dep),
) -> LeaveResult:
    """File leave and let the engine deal with the fallout immediately.

    The response carries the whole plan, not just an id: whoever filed this
    can see at once whether their work is covered or whether a manager is
    being pulled in.
    """
    if session.get(Employee, payload.employee_id) is None:
        raise HTTPException(404, "No such employee")
    if payload.ends_at <= payload.starts_at:
        raise HTTPException(400, "Leave must end after it starts")

    leave = LeaveRequest(
        employee_id=payload.employee_id,
        leave_type=payload.leave_type,
        reason=payload.reason,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        status=LeaveStatus.PENDING_COVERAGE,
        created_at=now,
    )
    session.add(leave)
    session.flush()

    plan, records = reassignment.handle_leave_request(session, leave, notifier, settings, now)
    session.commit()

    return LeaveResult(
        leave=LeaveOut.model_validate(leave),
        plan=plan.as_dict(),
        decisions=[record.id for record in records],
    )


@router.get("", response_model=list[LeaveOut])
def list_leave(
    status: str | None = None, session: Session = Depends(get_session)
) -> list[LeaveRequest]:
    query = select(LeaveRequest).order_by(LeaveRequest.starts_at.desc())
    if status:
        query = query.where(LeaveRequest.status == status)
    return list(session.scalars(query))


@router.get("/{leave_id}", response_model=LeaveOut)
def get_leave(leave_id: int, session: Session = Depends(get_session)) -> LeaveRequest:
    leave = session.get(LeaveRequest, leave_id)
    if leave is None:
        raise HTTPException(404, "No such leave request")
    return leave


@router.post("/{leave_id}/preview")
def preview_leave(
    leave_id: int,
    session: Session = Depends(get_session),
    settings=Depends(settings_dep),
    now=Depends(now_dep),
) -> dict:
    """What *would* happen, without anything happening.

    Useful before a shift starts, and the thing to reach for when somebody
    asks why the system picked who it picked.
    """
    leave = session.get(LeaveRequest, leave_id)
    if leave is None:
        raise HTTPException(404, "No such leave request")
    return reassignment.plan_for_leave(session, leave, settings, now).as_dict()


@router.post("/{leave_id}/decide", response_model=LeaveOut)
def decide_leave(
    leave_id: int,
    payload: LeaveDecision,
    session: Session = Depends(get_session),
    now=Depends(now_dep),
) -> LeaveRequest:
    """A manager settles a leave request the system would not settle itself."""
    leave = session.get(LeaveRequest, leave_id)
    if leave is None:
        raise HTTPException(404, "No such leave request")

    leave.status = LeaveStatus.APPROVED if payload.approve else LeaveStatus.DENIED
    leave.decided_at = now
    leave.decided_by = f"manager:{payload.manager_id}"
    leave.notes = payload.note

    record_decision(
        session,
        now=now,
        action=DecisionAction.LEAVE_APPROVED if payload.approve else DecisionAction.OVERRIDDEN,
        trigger="manual",
        summary=(f"Manager {'approved' if payload.approve else 'denied'} leave request {leave.id}"),
        subject_type="leave_request",
        subject_id=leave.id,
        actor=f"manager:{payload.manager_id}",
        details={"note": payload.note},
    )
    session.commit()
    return leave
