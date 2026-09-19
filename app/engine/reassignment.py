"""The orchestrator: leave request in, staffing decisions out.

``plan_for_leave`` is pure -- it reads, it thinks, it returns a plan, and
nothing in the world has changed yet.  ``execute_plan`` is the only function
here that writes rows or sends messages.  Keeping the split means a manager
(or a test, or the simulation) can look at exactly what the system intends to
do before it does it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import to_local
from app.config import Settings
from app.engine import candidates as ranking
from app.engine.escalation import escalate, summarise_blockers
from app.engine.journal import record_decision
from app.engine.snapshot import load_snapshot, open_coverage_task_ids
from app.engine.types import ImpactedTask, ReassignmentPlan, TaskPlan
from app.models.audit import DecisionRecord
from app.models.coverage import CoverageOffer, CoverageRequest
from app.models.employee import Employee
from app.models.enums import (
    CoverageStatus,
    DecisionAction,
    LeaveStatus,
    OfferStatus,
    ShiftAssignmentStatus,
    TaskPriority,
    TaskStatus,
)
from app.models.leave import LeaveRequest
from app.models.shift import Shift, ShiftAssignment
from app.models.task import Task
from app.notifications import templates
from app.notifications.base import Notifier

TRIGGER = "leave_request"


# --------------------------------------------------------------------------
# Planning (pure)
# --------------------------------------------------------------------------


def impacted_tasks(session: Session, leave: LeaveRequest, settings: Settings) -> list[Task]:
    """Live work the leave collides with, tightest deadline first."""
    tasks = list(
        session.scalars(
            select(Task).where(
                Task.assignee_id == leave.employee_id,
                Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED]),
                Task.due_at > leave.starts_at,
                Task.starts_at < leave.ends_at,
            )
        )
    )
    return sorted(tasks, key=lambda task: (task.deadline, task.id))


def is_critical(task: Task, leave: LeaveRequest, settings: Settings) -> bool:
    """Is there a hard outside deadline that lands while the person is away?

    This is the collision the whole system exists for: the driver turns up at
    14:50, and the only person who was going to have the order packed left at
    14:30.  An order pickup is such a deadline because the driver will not
    wait.  Work with no order attached has a due time, but missing it costs
    nothing outside the building, so it is not critical merely for falling in
    the same window -- only an explicit critical priority makes it so.
    """
    if task.priority == TaskPriority.CRITICAL:
        return True
    if task.order is None:
        return False
    grace = timedelta(minutes=settings.critical_pickup_grace_minutes)
    return leave.starts_at <= task.order.pickup_at <= leave.ends_at + grace


def _describe(task: Task, leave: LeaveRequest, settings: Settings) -> ImpactedTask:
    order = task.order
    return ImpactedTask(
        task_id=task.id,
        title=task.title,
        starts_at=task.starts_at,
        due_at=task.due_at,
        deadline=task.deadline,
        priority=str(task.priority),
        critical=is_critical(task, leave, settings),
        order_code=order.code if order else None,
        pickup_at=order.pickup_at if order else None,
    )


def plan_for_leave(
    session: Session, leave: LeaveRequest, settings: Settings, now: datetime
) -> ReassignmentPlan:
    """Work out what should happen. Changes nothing."""
    tasks = impacted_tasks(session, leave, settings)
    already_hunting = open_coverage_task_ids(session)
    horizon_end = max([task.due_at for task in tasks] + [leave.ends_at]) + timedelta(days=1)
    snapshot = load_snapshot(session, settings, now, horizon_end=horizon_end)

    employee = session.get(Employee, leave.employee_id)
    plan = ReassignmentPlan(
        leave_request_id=leave.id,
        employee_id=leave.employee_id,
        employee_name=employee.full_name if employee else f"employee {leave.employee_id}",
        leave_starts_at=leave.starts_at,
        leave_ends_at=leave.ends_at,
        generated_at=now,
    )

    for task in tasks:
        impact = _describe(task, leave, settings)

        if task.id in already_hunting:
            plan.task_plans.append(
                TaskPlan(
                    impact=impact,
                    action=DecisionAction.NO_ACTION_NEEDED,
                    reason="a cover request for this task is already open",
                )
            )
            continue

        window_start = max(task.starts_at, leave.starts_at, now)
        window_end = max(task.due_at, window_start + timedelta(minutes=1))

        ranked = ranking.rank_candidates(
            snapshot,
            task,
            window_start=window_start,
            window_end=window_end,
            critical=impact.critical,
            vacating_employee_id=leave.employee_id,
        )
        on_shift = ranking.on_shift_first(ranked)
        off_shift = ranking.off_shift(ranked)

        if on_shift and settings.auto_reassign_enabled:
            chosen = on_shift[0]
            task_plan = TaskPlan(
                impact=impact,
                action=DecisionAction.AUTO_REASSIGNED,
                reason=(
                    f"{chosen.employee_name} is on shift, qualified and free"
                    if chosen.displaces_task_id is None
                    else (
                        f"{chosen.employee_name} is on shift and qualified; "
                        f"lower-priority task {chosen.displaces_task_id} is displaced"
                    )
                ),
                chosen=chosen,
                candidates=ranked,
            )
            snapshot.commit_tentatively(
                chosen.employee_id,
                task_id=task.id,
                starts_at=window_start,
                ends_at=window_end,
                minutes=ranking.added_minutes_for(
                    snapshot.facts[chosen.employee_id], task, chosen.on_shift
                ),
                priority_rank=task.priority_rank,
                adds_hours=not chosen.on_shift,
            )

        elif off_shift and settings.auto_coverage_requests_enabled:
            ask_list = off_shift[: settings.coverage_batch_size]
            task_plan = TaskPlan(
                impact=impact,
                action=DecisionAction.COVERAGE_REQUESTED,
                reason=(
                    f"nobody on shift can take it; asking {len(ask_list)} off-shift "
                    f"colleague(s) in order of fit"
                ),
                ask_list=ask_list,
                candidates=ranked,
            )
            for candidate in ask_list:
                snapshot.note_ask(candidate.employee_id)

        elif on_shift or off_shift:
            task_plan = TaskPlan(
                impact=impact,
                action=DecisionAction.BLOCKED_BY_POLICY,
                reason=(
                    "cover is available but autonomous action is switched off in config; "
                    "a manager has to confirm"
                ),
                ask_list=(on_shift or off_shift)[: settings.coverage_batch_size],
                candidates=ranked,
            )

        else:
            blockers = summarise_blockers(ranked)
            task_plan = TaskPlan(
                impact=impact,
                action=DecisionAction.ESCALATED,
                reason=(
                    "no eligible cover: " + "; ".join(blockers)
                    if blockers
                    else "no eligible cover and nobody else on the roster"
                ),
                candidates=ranked,
            )

        plan.task_plans.append(task_plan)

    # Shifts the person will not be working.
    for (assignment,) in session.execute(
        select(ShiftAssignment)
        .join(Shift, Shift.id == ShiftAssignment.shift_id)
        .where(
            ShiftAssignment.employee_id == leave.employee_id,
            ShiftAssignment.status == ShiftAssignmentStatus.SCHEDULED,
            Shift.ends_at > leave.starts_at,
            Shift.starts_at < leave.ends_at,
        )
    ).all():
        plan.released_shift_assignment_ids.append(assignment.id)

    return plan


# --------------------------------------------------------------------------
# Execution (writes and sends)
# --------------------------------------------------------------------------


def _coverage_deadline(task: Task, settings: Settings, now: datetime) -> datetime:
    """When to stop waiting for an answer.

    Whichever comes first: the ordinary response timeout, or the point before
    the driver arrives where a manager still has time to do something.
    """
    timeout = now + timedelta(minutes=settings.coverage_response_timeout_minutes)
    last_useful = task.deadline - timedelta(minutes=settings.escalation_lead_minutes)
    return max(now, min(timeout, last_useful))


def _apply_auto_reassignment(
    session: Session,
    plan: ReassignmentPlan,
    task_plan: TaskPlan,
    task: Task,
    leaver: Employee | None,
    notifier: Notifier,
    settings: Settings,
    now: datetime,
) -> DecisionRecord:
    chosen = task_plan.chosen
    assert chosen is not None
    taker = session.get(Employee, chosen.employee_id)

    displaced_note = None
    if chosen.displaces_task_id is not None:
        displaced = session.get(Task, chosen.displaces_task_id)
        if displaced is not None:
            displaced.assignee_id = None
            displaced.status = TaskStatus.PENDING
            displaced_note = (
                f"task {displaced.id} ({displaced.title}) was dropped by "
                f"{chosen.employee_name} and now needs an owner"
            )

    previous_assignee_id = task.assignee_id
    task.assignee_id = chosen.employee_id
    task.status = TaskStatus.PENDING
    session.flush()

    messages = []
    if taker is not None:
        messages.append(
            notifier.send(
                templates.reassignment_notice(
                    taker,
                    task,
                    settings,
                    reason=f"Picked up from {plan.employee_name}, who is on leave.",
                )
            )
        )
    if leaver is not None and taker is not None:
        messages.append(notifier.send(templates.handover_notice(leaver, task, taker, settings)))

    detail_lines = [task_plan.reason]
    if displaced_note:
        detail_lines.append(displaced_note)
    messages.append(
        notifier.send(
            templates.manager_notice(
                f"{task.title} reassigned from {plan.employee_name} to {chosen.employee_name}"
                + (f" (order {task.order.code})" if task.order else ""),
                settings,
                detail_lines,
            )
        )
    )

    record = record_decision(
        session,
        now=now,
        action=DecisionAction.AUTO_REASSIGNED,
        trigger=TRIGGER,
        summary=(f"Reassigned '{task.title}' from {plan.employee_name} to {chosen.employee_name}"),
        subject_type="task",
        subject_id=task.id,
        details={
            "leave_request_id": plan.leave_request_id,
            "task_id": task.id,
            "from_employee_id": previous_assignee_id,
            "to_employee_id": chosen.employee_id,
            "plan": task_plan.as_dict(),
            "displaced": displaced_note,
            "messages": [m.message.text for m in messages],
        },
    )
    if displaced_note:
        notifier.send(templates.manager_notice(displaced_note, settings))
    return record


def _open_coverage_request(
    session: Session,
    plan: ReassignmentPlan,
    task_plan: TaskPlan,
    task: Task,
    notifier: Notifier,
    settings: Settings,
    now: datetime,
) -> DecisionRecord:
    request = CoverageRequest(
        task_id=task.id,
        leave_request_id=plan.leave_request_id,
        vacating_employee_id=plan.employee_id,
        # No time baked in on purpose. Every surface that shows this sentence
        # already shows the job's own times beside it, and a clock reading
        # frozen into a string is one that cannot follow the reader's timezone.
        reason=f"{plan.employee_name} is on leave",
        status=CoverageStatus.OPEN,
        wave=1,
        created_at=now,
        expires_at=_coverage_deadline(task, settings, now),
    )
    session.add(request)
    session.flush()

    sent = []
    for rank, candidate in enumerate(task_plan.ask_list, start=1):
        employee = session.get(Employee, candidate.employee_id)
        if employee is None:
            continue
        offer = CoverageOffer(
            coverage_request_id=request.id,
            employee_id=employee.id,
            status=OfferStatus.SENT,
            rank=rank,
            score=candidate.score,
            sent_at=now,
        )
        session.add(offer)
        session.flush()  # the offer id is baked into the signed reply links
        delivery = notifier.send(templates.cover_request(employee, task, request, offer, settings))
        offer.channel_ref = delivery.ref
        sent.append({"employee": employee.full_name, "rank": rank, "ok": delivery.ok})

    return record_decision(
        session,
        now=now,
        action=DecisionAction.COVERAGE_REQUESTED,
        trigger=TRIGGER,
        summary=(
            f"Asked {len(sent)} colleague(s) to cover '{task.title}'"
            + (f" for order {task.order.code}" if task.order else "")
        ),
        subject_type="coverage_request",
        subject_id=request.id,
        details={
            "leave_request_id": plan.leave_request_id,
            "task_id": task.id,
            "expires_at": request.expires_at.isoformat(),
            "asked": sent,
            "plan": task_plan.as_dict(),
        },
    )


def execute_plan(
    session: Session,
    plan: ReassignmentPlan,
    notifier: Notifier,
    settings: Settings,
    now: datetime,
) -> list[DecisionRecord]:
    """Make the plan true. This is the only place the leave flow writes."""
    leave = session.get(LeaveRequest, plan.leave_request_id)
    leaver = session.get(Employee, plan.employee_id)
    records: list[DecisionRecord] = []

    for task_plan in plan.task_plans:
        task = session.get(Task, task_plan.impact.task_id)
        if task is None:
            continue

        if task_plan.action == DecisionAction.AUTO_REASSIGNED:
            records.append(
                _apply_auto_reassignment(
                    session, plan, task_plan, task, leaver, notifier, settings, now
                )
            )

        elif task_plan.action == DecisionAction.COVERAGE_REQUESTED:
            records.append(
                _open_coverage_request(session, plan, task_plan, task, notifier, settings, now)
            )

        elif task_plan.action in (DecisionAction.ESCALATED, DecisionAction.BLOCKED_BY_POLICY):
            urgency = "Driver due" if task_plan.impact.pickup_at else "Due"
            deadline = task_plan.impact.deadline
            records.append(
                escalate(
                    session,
                    notifier,
                    settings,
                    now=now,
                    trigger=TRIGGER,
                    summary=(
                        f"No cover for '{task.title}'"
                        + (
                            f" (order {task_plan.impact.order_code})"
                            if task_plan.impact.order_code
                            else ""
                        )
                        + f" - {plan.employee_name} is on leave. {urgency} "
                        + f"{to_local(deadline, settings.business_tz):%H:%M}."
                    ),
                    subject_type="task",
                    subject_id=task.id,
                    detail_lines=summarise_blockers(task_plan.candidates) or [task_plan.reason],
                    details={
                        "leave_request_id": plan.leave_request_id,
                        "plan": task_plan.as_dict(),
                    },
                    urgent=task_plan.impact.critical,
                )
            )

    # Release the shifts they will not be working.
    for assignment_id in plan.released_shift_assignment_ids:
        assignment = session.get(ShiftAssignment, assignment_id)
        if assignment is not None:
            assignment.status = ShiftAssignmentStatus.RELEASED

    # The leave itself is only auto-approved once nothing critical is dangling.
    if leave is not None:
        if plan.fully_resolved:
            leave.status = LeaveStatus.APPROVED
            leave.decided_at = now
            leave.decided_by = "system"
            records.append(
                record_decision(
                    session,
                    now=now,
                    action=DecisionAction.LEAVE_APPROVED,
                    trigger=TRIGGER,
                    summary=(
                        f"Leave approved for {plan.employee_name}: "
                        f"{len(plan.task_plans)} impacted task(s) all covered"
                    ),
                    subject_type="leave_request",
                    subject_id=leave.id,
                    details={"plan": plan.as_dict()},
                )
            )
        else:
            leave.status = LeaveStatus.PENDING_COVERAGE
            records.append(
                record_decision(
                    session,
                    now=now,
                    action=DecisionAction.LEAVE_PENDING_COVERAGE,
                    trigger=TRIGGER,
                    summary=(
                        f"Leave for {plan.employee_name} is pending cover for "
                        f"{sum(1 for p in plan.task_plans if not p.resolved)} task(s)"
                    ),
                    subject_type="leave_request",
                    subject_id=leave.id,
                    details={"plan": plan.as_dict()},
                )
            )

    session.flush()
    return records


def handle_leave_request(
    session: Session,
    leave: LeaveRequest,
    notifier: Notifier,
    settings: Settings,
    now: datetime,
) -> tuple[ReassignmentPlan, list[DecisionRecord]]:
    """Plan, then execute. The entry point the API and the scheduler use."""
    plan = plan_for_leave(session, leave, settings, now)
    records = execute_plan(session, plan, notifier, settings, now)
    return plan, records
