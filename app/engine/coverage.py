"""Running a cover request: replies, races, timeouts and giving up.

The interesting problem here is not the happy path, it is two people tapping
"I can cover" three seconds apart.  Acceptance is a single conditional UPDATE
-- ``WHERE status = 'open'`` -- so the database decides the winner and the
loser gets told immediately rather than turning up to a job someone else is
already doing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.engine import candidates as ranking
from app.engine import guardrails
from app.engine.escalation import escalate, summarise_blockers
from app.engine.journal import record_decision
from app.engine.snapshot import load_snapshot
from app.engine.types import RuleResult
from app.models.audit import DecisionRecord
from app.models.coverage import CoverageOffer, CoverageRequest
from app.models.employee import Employee
from app.models.enums import (
    CoverageStatus,
    DecisionAction,
    LeaveStatus,
    OfferStatus,
    TaskStatus,
)
from app.models.leave import LeaveRequest
from app.models.task import Task
from app.notifications import templates
from app.notifications.base import Notifier

TRIGGER_REPLY = "coverage_reply"
TRIGGER_SWEEP = "coverage_timeout"

# Quiet hours and the daily ask limit govern whether we may *bother* someone.
# Once they have volunteered, re-applying them would be absurd -- they are
# already awake and already answering. The rules about hours, rest, skill and
# clashes still very much apply.
MESSAGING_ONLY_RULES = {"outside_quiet_hours", "under_ask_limit"}


@dataclass
class CoverageOutcome:
    ok: bool
    status: str
    """``accepted``, ``already_filled``, ``declined``, ``blocked``, ``closed`` or ``unknown``."""

    message: str
    offer_id: int | None = None
    request_id: int | None = None
    task_id: int | None = None
    blockers: list[str] = field(default_factory=list)


def _window(task: Task, now: datetime) -> tuple[datetime, datetime]:
    start = max(task.starts_at, now)
    return start, max(task.due_at, start + timedelta(minutes=1))


def recheck_eligibility(
    session: Session,
    settings: Settings,
    now: datetime,
    task: Task,
    employee_id: int,
    vacating_employee_id: int | None,
) -> list[RuleResult]:
    """Re-run the work-related guardrails at the moment somebody accepts.

    Minutes have passed since we asked.  They may have clocked extra hours, or
    picked up another job.  Letting a stale "yes" through is exactly how an
    automated system walks somebody into a twelfth hour.
    """
    snapshot = load_snapshot(session, settings, now, horizon_end=task.due_at + timedelta(days=1))
    facts = snapshot.get(employee_id)
    if facts is None:
        return [RuleResult(rule="employee_active", passed=False, reason="not on the active roster")]

    window_start, window_end = _window(task, now)
    on_shift = facts.on_shift_during(window_start, window_end)
    ctx = guardrails.EvaluationContext(
        facts=facts,
        task=task,
        window_start=window_start,
        window_end=window_end,
        on_shift=on_shift,
        added_minutes=ranking.added_minutes_for(facts, task, on_shift),
        critical=True,
        now=now,
        settings=settings,
        vacating_employee_id=vacating_employee_id,
    )
    results, _ = guardrails.evaluate(ctx)
    return [r for r in results if r.rule not in MESSAGING_ONLY_RULES]


def accept_offer(
    session: Session,
    offer: CoverageOffer,
    notifier: Notifier,
    settings: Settings,
    now: datetime,
) -> CoverageOutcome:
    """Somebody said yes. Commits on the way through -- the claim has to be atomic."""
    request = session.get(CoverageRequest, offer.coverage_request_id)
    task = session.get(Task, request.task_id) if request else None
    employee = session.get(Employee, offer.employee_id)
    if request is None or task is None or employee is None:
        return CoverageOutcome(False, "unknown", "That cover request no longer exists.")

    # Replay of a link they already used.
    if offer.status == OfferStatus.ACCEPTED:
        return CoverageOutcome(
            True,
            "accepted",
            f"You're already covering {task.title}.",
            offer.id,
            request.id,
            task.id,
        )
    if offer.status in (OfferStatus.SUPERSEDED, OfferStatus.EXPIRED):
        return CoverageOutcome(
            False,
            "already_filled",
            "Someone else has already covered this. Thanks anyway.",
            offer.id,
            request.id,
            task.id,
        )
    if offer.status == OfferStatus.DECLINED:
        return CoverageOutcome(
            False, "closed", "You already declined this one.", offer.id, request.id, task.id
        )

    if request.status != CoverageStatus.OPEN:
        offer.status = OfferStatus.SUPERSEDED
        offer.responded_at = now
        session.commit()
        return CoverageOutcome(
            False,
            "already_filled",
            "Someone else got there first. Thanks for answering.",
            offer.id,
            request.id,
            task.id,
        )

    # A willing volunteer we are not allowed to accept is precisely the case a
    # manager needs to see, so it escalates rather than failing quietly.
    rules = recheck_eligibility(
        session, settings, now, task, employee.id, request.vacating_employee_id
    )
    broken = [r for r in rules if not r.passed]
    if broken:
        reasons = [r.reason for r in broken]
        escalate(
            session,
            notifier,
            settings,
            now=now,
            trigger=TRIGGER_REPLY,
            summary=(
                f"{employee.full_name} offered to cover '{task.title}' "
                f"but a staffing rule blocks it"
            ),
            subject_type="coverage_request",
            subject_id=request.id,
            detail_lines=reasons,
            details={
                "task_id": task.id,
                "employee_id": employee.id,
                "rules": [r.as_dict() for r in rules],
            },
            urgent=True,
        )
        session.commit()
        return CoverageOutcome(
            False,
            "blocked",
            "Thanks for offering - we can't put you on this one. A manager has been told.",
            offer.id,
            request.id,
            task.id,
            blockers=reasons,
        )

    # The claim. Exactly one caller can turn an open request into a filled one.
    claimed = session.execute(
        update(CoverageRequest)
        .where(CoverageRequest.id == request.id, CoverageRequest.status == CoverageStatus.OPEN)
        .values(status=CoverageStatus.FILLED, filled_by_id=employee.id, filled_at=now)
    )
    if claimed.rowcount == 0:
        offer.status = OfferStatus.SUPERSEDED
        offer.responded_at = now
        session.commit()
        return CoverageOutcome(
            False,
            "already_filled",
            "Someone else got there first. Thanks for answering.",
            offer.id,
            request.id,
            task.id,
        )
    session.commit()

    # From here the win is settled; the rest is bookkeeping and messages.
    session.refresh(request)
    offer.status = OfferStatus.ACCEPTED
    offer.responded_at = now
    task.assignee_id = employee.id
    task.status = TaskStatus.PENDING

    losers = list(
        session.scalars(
            select(CoverageOffer).where(
                CoverageOffer.coverage_request_id == request.id,
                CoverageOffer.id != offer.id,
                CoverageOffer.status == OfferStatus.SENT,
            )
        )
    )
    for loser in losers:
        loser.status = OfferStatus.SUPERSEDED
        loser.responded_at = now
        if loser.employee is not None:
            message = templates.coverage_superseded(loser.employee, task, employee.full_name)
            if loser.channel_ref:
                notifier.update(message.channel, loser.channel_ref, message)
            else:
                notifier.send(message)

    notifier.send(templates.coverage_confirmed(employee, task, settings))
    notifier.send(
        templates.manager_notice(
            f"{employee.full_name} is covering '{task.title}'"
            + (f" for order {task.order.code}" if task.order else ""),
            settings,
            [f"Responded {now:%H:%M} UTC, {len(losers)} other ask(s) stood down."],
        )
    )

    record_decision(
        session,
        now=now,
        action=DecisionAction.COVERAGE_FILLED,
        trigger=TRIGGER_REPLY,
        summary=f"{employee.full_name} accepted cover for '{task.title}'",
        subject_type="coverage_request",
        subject_id=request.id,
        details={
            "task_id": task.id,
            "employee_id": employee.id,
            "superseded_offers": [loser.id for loser in losers],
            "rules": [r.as_dict() for r in rules],
        },
    )
    _settle_leave_if_clear(session, notifier, settings, now, request.leave_request_id)
    session.commit()

    return CoverageOutcome(
        True,
        "accepted",
        f"Thanks - you're covering {task.title}.",
        offer.id,
        request.id,
        task.id,
    )


def decline_offer(
    session: Session,
    offer: CoverageOffer,
    notifier: Notifier,
    settings: Settings,
    now: datetime,
) -> CoverageOutcome:
    """Somebody said no. If that was the last open ask, go wider or escalate."""
    request = session.get(CoverageRequest, offer.coverage_request_id)
    task = session.get(Task, request.task_id) if request else None
    if request is None or task is None:
        return CoverageOutcome(False, "unknown", "That cover request no longer exists.")

    if offer.status == OfferStatus.DECLINED:
        return CoverageOutcome(
            True, "declined", "Already noted, thanks.", offer.id, request.id, task.id
        )
    if offer.status != OfferStatus.SENT:
        return CoverageOutcome(
            False,
            "closed",
            "This one is already settled. Thanks anyway.",
            offer.id,
            request.id,
            task.id,
        )

    offer.status = OfferStatus.DECLINED
    offer.responded_at = now
    session.flush()

    if request.status == CoverageStatus.OPEN and not _has_pending_offers(session, request.id):
        widen_or_escalate(session, request, notifier, settings, now, trigger=TRIGGER_REPLY)

    session.commit()
    return CoverageOutcome(
        True,
        "declined",
        "No problem - noted, we'll ask someone else.",
        offer.id,
        request.id,
        task.id,
    )


def _has_pending_offers(session: Session, request_id: int) -> bool:
    return bool(
        session.scalars(
            select(CoverageOffer.id).where(
                CoverageOffer.coverage_request_id == request_id,
                CoverageOffer.status == OfferStatus.SENT,
            )
        ).first()
    )


def widen_or_escalate(
    session: Session,
    request: CoverageRequest,
    notifier: Notifier,
    settings: Settings,
    now: datetime,
    *,
    trigger: str = TRIGGER_SWEEP,
) -> DecisionRecord | None:
    """Nobody has said yes yet. Ask more people, or stop and call a manager."""
    task = session.get(Task, request.task_id)
    if task is None or request.status != CoverageStatus.OPEN:
        return None

    out_of_time = now >= task.deadline - timedelta(minutes=settings.escalation_lead_minutes)
    already_asked = {offer.employee_id for offer in request.offers}

    window_start, window_end = _window(task, now)
    snapshot = load_snapshot(session, settings, now, horizon_end=task.due_at + timedelta(days=1))
    ranked = ranking.rank_candidates(
        snapshot,
        task,
        window_start=window_start,
        window_end=window_end,
        critical=True,
        vacating_employee_id=request.vacating_employee_id,
    )
    fresh = [c for c in ranked if c.eligible and c.employee_id not in already_asked]

    # Somebody may have come on shift since the first wave went out.
    on_shift = [c for c in fresh if c.on_shift]
    if on_shift and settings.auto_reassign_enabled and not out_of_time:
        chosen = on_shift[0]
        return _fill_directly(
            session, request, task, chosen.employee_id, notifier, settings, now, trigger
        )

    if fresh and not out_of_time and settings.auto_coverage_requests_enabled:
        return _send_next_wave(session, request, task, fresh, notifier, settings, now, trigger)

    request.status = CoverageStatus.ESCALATED
    request.escalated_at = now
    for offer in request.offers:
        if offer.status == OfferStatus.SENT:
            offer.status = OfferStatus.EXPIRED
            offer.responded_at = now

    reason = (
        f"no answer and the driver is due at {task.deadline:%H:%M}"
        if out_of_time
        else "everyone asked has declined and nobody else is eligible"
    )
    record = escalate(
        session,
        notifier,
        settings,
        now=now,
        trigger=trigger,
        summary=(
            f"Still no cover for '{task.title}'"
            + (f" (order {task.order.code})" if task.order else "")
            + f" - {reason}"
        ),
        subject_type="coverage_request",
        subject_id=request.id,
        detail_lines=summarise_blockers(ranked)
        or [f"{len(already_asked)} person(s) asked, none available"],
        details={
            "task_id": task.id,
            "wave": request.wave,
            "asked_employee_ids": sorted(already_asked),
            "candidates": [c.as_dict() for c in ranked],
        },
        urgent=True,
    )
    session.flush()
    return record


def _fill_directly(
    session: Session,
    request: CoverageRequest,
    task: Task,
    employee_id: int,
    notifier: Notifier,
    settings: Settings,
    now: datetime,
    trigger: str,
) -> DecisionRecord:
    employee = session.get(Employee, employee_id)
    request.status = CoverageStatus.FILLED
    request.filled_by_id = employee_id
    request.filled_at = now
    task.assignee_id = employee_id
    for offer in request.offers:
        if offer.status == OfferStatus.SENT:
            offer.status = OfferStatus.SUPERSEDED
            offer.responded_at = now
            if offer.employee is not None and employee is not None:
                message = templates.coverage_superseded(offer.employee, task, employee.full_name)
                if offer.channel_ref:
                    notifier.update(message.channel, offer.channel_ref, message)
                else:
                    notifier.send(message)
    if employee is not None:
        notifier.send(
            templates.reassignment_notice(
                employee, task, settings, reason="Nobody answered the cover request in time."
            )
        )
    record = record_decision(
        session,
        now=now,
        action=DecisionAction.AUTO_REASSIGNED,
        trigger=trigger,
        summary=(
            f"Assigned '{task.title}' to {employee.full_name if employee else employee_id} "
            f"- came on shift while the cover request was open"
        ),
        subject_type="coverage_request",
        subject_id=request.id,
        details={"task_id": task.id, "employee_id": employee_id},
    )
    _settle_leave_if_clear(session, notifier, settings, now, request.leave_request_id)
    session.flush()
    return record


def _send_next_wave(
    session: Session,
    request: CoverageRequest,
    task: Task,
    fresh: list,
    notifier: Notifier,
    settings: Settings,
    now: datetime,
    trigger: str,
) -> DecisionRecord:
    request.wave += 1
    request.expires_at = max(
        now,
        min(
            now + timedelta(minutes=settings.coverage_response_timeout_minutes),
            task.deadline - timedelta(minutes=settings.escalation_lead_minutes),
        ),
    )
    for offer in request.offers:
        if offer.status == OfferStatus.SENT:
            offer.status = OfferStatus.EXPIRED
            offer.responded_at = now

    asked = []
    start_rank = len(request.offers)
    for index, candidate in enumerate(fresh[: settings.coverage_batch_size], start=1):
        employee = session.get(Employee, candidate.employee_id)
        if employee is None:
            continue
        offer = CoverageOffer(
            coverage_request_id=request.id,
            employee_id=employee.id,
            status=OfferStatus.SENT,
            rank=start_rank + index,
            score=candidate.score,
            sent_at=now,
        )
        session.add(offer)
        session.flush()
        delivery = notifier.send(templates.cover_request(employee, task, request, offer, settings))
        offer.channel_ref = delivery.ref
        asked.append(employee.full_name)

    record = record_decision(
        session,
        now=now,
        action=DecisionAction.COVERAGE_REQUESTED,
        trigger=trigger,
        summary=f"Widened the search for '{task.title}' to {len(asked)} more colleague(s)",
        subject_type="coverage_request",
        subject_id=request.id,
        details={
            "task_id": task.id,
            "wave": request.wave,
            "asked": asked,
            "expires_at": request.expires_at.isoformat(),
        },
    )
    session.flush()
    return record


def sweep_open_requests(
    session: Session, notifier: Notifier, settings: Settings, now: datetime
) -> list[DecisionRecord]:
    """Every open cover request that has run out of time or run out of runway."""
    records: list[DecisionRecord] = []
    for request in session.scalars(
        select(CoverageRequest).where(CoverageRequest.status == CoverageStatus.OPEN)
    ):
        task = session.get(Task, request.task_id)
        if task is None:
            continue
        timed_out = request.expires_at <= now
        pickup_looming = now >= task.deadline - timedelta(minutes=settings.escalation_lead_minutes)
        if timed_out or pickup_looming:
            record = widen_or_escalate(session, request, notifier, settings, now)
            if record is not None:
                records.append(record)
    session.commit()
    return records


def _settle_leave_if_clear(
    session: Session,
    notifier: Notifier,
    settings: Settings,
    now: datetime,
    leave_request_id: int | None,
) -> None:
    """Approve the leave once every hunt it started has found somebody."""
    if leave_request_id is None:
        return
    leave = session.get(LeaveRequest, leave_request_id)
    if leave is None or leave.status != LeaveStatus.PENDING_COVERAGE:
        return
    unresolved = session.scalars(
        select(CoverageRequest.id).where(
            CoverageRequest.leave_request_id == leave_request_id,
            CoverageRequest.status.in_([CoverageStatus.OPEN, CoverageStatus.ESCALATED]),
        )
    ).first()
    if unresolved is not None:
        return

    leave.status = LeaveStatus.APPROVED
    leave.decided_at = now
    leave.decided_by = "system"
    employee = session.get(Employee, leave.employee_id)
    name = employee.full_name if employee else f"employee {leave.employee_id}"
    notifier.send(
        templates.manager_notice(
            f"Leave approved for {name} - all impacted work is covered", settings
        )
    )
    record_decision(
        session,
        now=now,
        action=DecisionAction.LEAVE_APPROVED,
        trigger=TRIGGER_REPLY,
        summary=f"Leave approved for {name}: cover secured for every impacted task",
        subject_type="leave_request",
        subject_id=leave.id,
        details={"leave_request_id": leave.id},
    )
