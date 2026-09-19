"""The audit log, and the manager's undo button."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import notifier_dep, now_dep, settings_dep
from app.api.schemas import DecisionOut, OverrideIn
from app.db import get_session
from app.engine.journal import record_decision
from app.models.audit import DecisionRecord
from app.models.employee import Employee
from app.models.enums import DecisionAction
from app.models.task import Task
from app.notifications import templates

router = APIRouter(tags=["decisions"], prefix="/decisions")


@router.get("", response_model=list[DecisionOut])
def list_decisions(
    limit: int = 50,
    action: str | None = None,
    subject_type: str | None = None,
    session: Session = Depends(get_session),
) -> list[DecisionRecord]:
    query = select(DecisionRecord).order_by(
        DecisionRecord.created_at.desc(), DecisionRecord.id.desc()
    )
    if action:
        query = query.where(DecisionRecord.action == action)
    if subject_type:
        query = query.where(DecisionRecord.subject_type == subject_type)
    return list(session.scalars(query.limit(limit)))


@router.get("/{decision_id}", response_model=DecisionOut)
def get_decision(decision_id: int, session: Session = Depends(get_session)) -> DecisionRecord:
    record = session.get(DecisionRecord, decision_id)
    if record is None:
        raise HTTPException(404, "No such decision")
    return record


@router.post("/{decision_id}/override", response_model=DecisionOut)
def override_decision(
    decision_id: int,
    payload: OverrideIn,
    session: Session = Depends(get_session),
    settings=Depends(settings_dep),
    notifier=Depends(notifier_dep),
    now=Depends(now_dep),
) -> DecisionRecord:
    """Reverse an automatic decision.

    An automated system that people cannot overrule is not a tool, it is a
    boss.  The reversal is itself recorded, so the log shows both the original
    call and the human who disagreed with it.
    """
    original = session.get(DecisionRecord, decision_id)
    if original is None:
        raise HTTPException(404, "No such decision")
    if original.reverted_by_id is not None:
        raise HTTPException(409, "That decision has already been overridden")

    details = original.details or {}
    task_id = details.get("task_id")
    task = session.get(Task, task_id) if task_id else None
    if task is None:
        raise HTTPException(
            400, "That decision has no task attached, so there is nothing to reverse"
        )

    target_id = payload.employee_id or details.get("from_employee_id")
    previous_id = task.assignee_id
    task.assignee_id = target_id

    taker = session.get(Employee, target_id) if target_id else None
    if taker is not None:
        notifier.send(
            templates.reassignment_notice(
                taker,
                task,
                settings,
                reason=payload.note or "A manager moved this back to you.",
            )
        )
    notifier.send(
        templates.manager_notice(
            f"Decision {original.id} overridden: '{task.title}' now with "
            f"{taker.full_name if taker else 'nobody'}",
            settings,
            [payload.note] if payload.note else None,
        )
    )

    reversal = record_decision(
        session,
        now=now,
        action=DecisionAction.OVERRIDDEN,
        trigger="manual_override",
        summary=(
            f"Manager overrode decision {original.id}: '{task.title}' moved "
            f"from {previous_id} to {target_id}"
        ),
        subject_type="task",
        subject_id=task.id,
        actor=f"manager:{payload.manager_id}",
        details={
            "overrode_decision_id": original.id,
            "task_id": task.id,
            "from_employee_id": previous_id,
            "to_employee_id": target_id,
            "note": payload.note,
        },
    )
    original.reverted_by_id = reversal.id
    session.commit()
    return reversal
