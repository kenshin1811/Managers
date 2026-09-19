"""Orders, shifts and the job assignments that hang off them."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import notifier_dep, now_dep, settings_dep
from app.api.schemas import (
    OrderIn,
    OrderOut,
    ShiftIn,
    ShiftOut,
    TaskIn,
    TaskOut,
    TaskReassign,
)
from app.db import get_session
from app.engine.journal import record_decision
from app.models.employee import Employee, Skill
from app.models.enums import DecisionAction
from app.models.order import Order
from app.models.shift import Shift, ShiftAssignment
from app.models.task import Task
from app.notifications import templates

router = APIRouter(tags=["schedule"])


@router.post("/orders", response_model=OrderOut, status_code=201)
def create_order(
    payload: OrderIn, session: Session = Depends(get_session), now=Depends(now_dep)
) -> Order:
    order = Order(
        code=payload.code,
        customer_name=payload.customer_name,
        placed_at=payload.placed_at or now,
        pickup_at=payload.pickup_at,
        driver_name=payload.driver_name,
        driver_service=payload.driver_service,
    )
    session.add(order)
    session.commit()
    return order


@router.get("/orders", response_model=list[OrderOut])
def list_orders(session: Session = Depends(get_session)) -> list[Order]:
    return list(session.scalars(select(Order).order_by(Order.pickup_at)))


@router.post("/shifts", response_model=ShiftOut, status_code=201)
def create_shift(payload: ShiftIn, session: Session = Depends(get_session)) -> Shift:
    shift = Shift(
        name=payload.name,
        location=payload.location,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
    )
    session.add(shift)
    session.flush()
    for employee_id in payload.employee_ids:
        session.add(ShiftAssignment(shift_id=shift.id, employee_id=employee_id))
    session.commit()
    return shift


@router.get("/shifts", response_model=list[ShiftOut])
def list_shifts(session: Session = Depends(get_session)) -> list[Shift]:
    return list(session.scalars(select(Shift).order_by(Shift.starts_at)))


@router.post("/tasks", response_model=TaskOut, status_code=201)
def create_task(payload: TaskIn, session: Session = Depends(get_session)) -> Task:
    skill_id = None
    if payload.required_skill_code:
        skill = session.scalar(select(Skill).where(Skill.code == payload.required_skill_code))
        if skill is None:
            raise HTTPException(404, f"No such skill: {payload.required_skill_code}")
        skill_id = skill.id

    task = Task(
        title=payload.title,
        station=payload.station,
        order_id=payload.order_id,
        assignee_id=payload.assignee_id,
        required_skill_id=skill_id,
        min_proficiency=payload.min_proficiency,
        starts_at=payload.starts_at,
        due_at=payload.due_at,
        estimated_minutes=payload.estimated_minutes,
        priority=payload.priority,
    )
    session.add(task)
    session.commit()
    return task


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks(
    assignee_id: int | None = None,
    order_id: int | None = None,
    session: Session = Depends(get_session),
) -> list[Task]:
    query = select(Task).order_by(Task.starts_at, Task.id)
    if assignee_id is not None:
        query = query.where(Task.assignee_id == assignee_id)
    if order_id is not None:
        query = query.where(Task.order_id == order_id)
    return list(session.scalars(query))


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: int, session: Session = Depends(get_session)) -> Task:
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "No such task")
    return task


@router.post("/tasks/{task_id}/reassign", response_model=TaskOut)
def reassign_task(
    task_id: int,
    payload: TaskReassign,
    session: Session = Depends(get_session),
    settings=Depends(settings_dep),
    notifier=Depends(notifier_dep),
    now=Depends(now_dep),
) -> Task:
    """A manual reassignment.

    Recorded in the same audit log as the automatic ones, with the manager as
    the actor, so the history reads as one sequence of decisions rather than
    two disconnected ones.
    """
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "No such task")
    previous = task.assignee_id
    task.assignee_id = payload.employee_id

    taker = session.get(Employee, payload.employee_id) if payload.employee_id else None
    if taker is not None:
        notifier.send(
            templates.reassignment_notice(
                taker, task, settings, reason=payload.note or "Assigned by a manager."
            )
        )

    record_decision(
        session,
        now=now,
        action=DecisionAction.OVERRIDDEN,
        trigger="manual",
        summary=(
            f"Manager reassigned '{task.title}' "
            f"from {previous} to {payload.employee_id or 'nobody'}"
        ),
        subject_type="task",
        subject_id=task.id,
        actor=f"manager:{payload.manager_id}" if payload.manager_id else "manual",
        details={
            "task_id": task.id,
            "from_employee_id": previous,
            "to_employee_id": payload.employee_id,
            "note": payload.note,
        },
    )
    session.commit()
    return task
