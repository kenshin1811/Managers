"""Everything the dashboard needs, in one request.

The page asks one question above all others -- *is the agent actually
working?* -- so the heartbeat comes first and is read from the database rather
than from this process, because the scheduler may not be in this process.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import now_dep, settings_dep
from app.clock import day_bounds, to_local
from app.config import Settings
from app.db import get_session
from app.engine.snapshot import load_snapshot
from app.models.audit import DecisionRecord
from app.models.coverage import CoverageRequest
from app.models.employee import Employee
from app.models.enums import CoverageStatus, DecisionAction, TaskStatus
from app.models.state import SWEEP_HEARTBEAT
from app.models.task import Task
from app.scheduler import SWEEP_SECONDS
from app.state import last_seen

router = APIRouter(tags=["dashboard"], prefix="/api")

#: Miss this many sweeps and the agent is not merely quiet, it is stopped.
STALL_AFTER_SWEEPS = 3


def stamp(moment: datetime | None, settings: Settings) -> dict[str, Any] | None:
    """A moment in both forms: UTC for arithmetic, local for reading."""
    if moment is None:
        return None
    return {
        "utc": moment.isoformat() + "Z",
        "local": f"{to_local(moment, settings.business_tz):%H:%M}",
        "local_full": f"{to_local(moment, settings.business_tz):%a %d %b, %H:%M}",
    }


def _agent_block(session: Session, settings: Settings, now: datetime) -> dict[str, Any]:
    beat = last_seen(session, SWEEP_HEARTBEAT)
    if beat is None:
        status, since = "starting", None
    else:
        since = (now - beat).total_seconds()
        status = "active" if since <= SWEEP_SECONDS * STALL_AFTER_SWEEPS else "stalled"
    return {
        "status": status,
        "last_sweep": stamp(beat, settings),
        "seconds_since_sweep": round(since) if since is not None else None,
        "sweep_interval_seconds": SWEEP_SECONDS,
        "now": stamp(now, settings),
        "dry_run": settings.dry_run,
        "messaging": "Slack" if settings.slack_configured else "Dry run",
        "auto_reassign": settings.auto_reassign_enabled,
        "auto_coverage_requests": settings.auto_coverage_requests_enabled,
        "business_tz": settings.business_tz,
        "limits": {
            "daily_hours": settings.max_daily_hours,
            "weekly_hours": settings.max_weekly_hours,
            "min_rest_hours": settings.min_rest_hours,
        },
    }


@router.get("/dashboard")
def dashboard(
    session: Session = Depends(get_session),
    settings: Settings = Depends(settings_dep),
    now: datetime = Depends(now_dep),
) -> dict[str, Any]:
    day_start, day_end = day_bounds(now, settings.business_tz)
    names = {employee.id: employee.full_name for employee in session.scalars(select(Employee))}

    # --- what the agent has done today ---
    tallies = dict(
        session.execute(
            select(DecisionRecord.action, func.count(DecisionRecord.id))
            .where(DecisionRecord.created_at >= day_start, DecisionRecord.created_at < day_end)
            .group_by(DecisionRecord.action)
        ).all()
    )

    requests = list(
        session.scalars(
            select(CoverageRequest)
            .where(CoverageRequest.status.in_([CoverageStatus.OPEN, CoverageStatus.ESCALATED]))
            .order_by(CoverageRequest.created_at.desc())
        )
    )
    coverage = []
    for request in requests:
        task = session.get(Task, request.task_id)
        if task is None:
            continue
        coverage.append(
            {
                "id": request.id,
                "status": request.status,
                "wave": request.wave,
                "task_id": task.id,
                "task_title": task.title,
                "order_code": task.order.code if task.order else None,
                "deadline": stamp(task.deadline, settings),
                "expires_at": stamp(request.expires_at, settings),
                "created_at": stamp(request.created_at, settings),
                "vacating": names.get(request.vacating_employee_id),
                "filled_by": names.get(request.filled_by_id),
                "offers": [
                    {
                        "id": offer.id,
                        "employee_id": offer.employee_id,
                        "employee_name": names.get(offer.employee_id, "?"),
                        "status": offer.status,
                        "rank": offer.rank,
                        "score": round(offer.score, 1),
                        "responded_at": stamp(offer.responded_at, settings),
                    }
                    for offer in sorted(request.offers, key=lambda o: o.rank)
                ],
            }
        )

    # --- the job board, a working day either side of now ---
    board = []
    for task in session.scalars(
        select(Task)
        .where(
            Task.due_at > now - timedelta(hours=2),
            Task.starts_at < now + timedelta(hours=12),
            Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED]),
        )
        .order_by(Task.starts_at, Task.id)
    ):
        board.append(
            {
                "id": task.id,
                "title": task.title,
                "station": task.station,
                "assignee_id": task.assignee_id,
                "assignee": names.get(task.assignee_id),
                "priority": task.priority,
                "status": task.status,
                "starts_at": stamp(task.starts_at, settings),
                "due_at": stamp(task.due_at, settings),
                "deadline": stamp(task.deadline, settings),
                "order_code": task.order.code if task.order else None,
                "pickup_at": stamp(task.order.pickup_at, settings) if task.order else None,
                "driver": task.order.driver_service if task.order else None,
            }
        )

    # --- the decision feed: the agent showing its working ---
    feed = []
    for record in session.scalars(
        select(DecisionRecord)
        .order_by(DecisionRecord.created_at.desc(), DecisionRecord.id.desc())
        .limit(30)
    ):
        details = record.details or {}
        plan = details.get("plan") or {}
        considered = plan.get("candidates") or []
        feed.append(
            {
                "id": record.id,
                "action": record.action,
                "actor": record.actor,
                "trigger": record.trigger,
                "summary": record.summary,
                "at": stamp(record.created_at, settings),
                "subject_type": record.subject_type,
                "subject_id": record.subject_id,
                "reverted_by_id": record.reverted_by_id,
                "blockers": details.get("blockers") or [],
                "considered": len(considered),
                "chosen": (plan.get("chosen") or {}).get("employee_name"),
                "rejected": [
                    {
                        "name": c["employee_name"],
                        "rule": next(r["rule"] for r in c["rules"] if not r["passed"]),
                        "reason": next(r["reason"] for r in c["rules"] if not r["passed"]),
                    }
                    for c in considered
                    if not c["eligible"] and any(not r["passed"] for r in c["rules"])
                ][:6],
            }
        )

    # --- who is available, and how close to their limits ---
    snapshot = load_snapshot(session, settings, now)
    staff = []
    for facts in sorted(snapshot.all_facts(), key=lambda f: f.employee.full_name):
        employee = facts.employee
        on_shift = facts.on_shift_during(now, now + timedelta(minutes=1))
        staff.append(
            {
                "id": employee.id,
                "name": employee.full_name,
                "role": employee.role,
                "is_manager": employee.is_manager,
                "on_shift": on_shift,
                "on_leave": facts.on_leave_during(now, now + timedelta(minutes=1)),
                "hours_today": round(facts.minutes_today / 60.0, 1),
                "hours_week": round(facts.minutes_week / 60.0, 1),
                "skills": [
                    {"code": link.skill.code, "level": link.proficiency} for link in employee.skills
                ],
                "live_tasks": len([t for t in facts.live_tasks if t.due_at > now]),
                "asks_today": facts.asks_today,
                "recent_covers": facts.recent_covers,
            }
        )

    return {
        "agent": _agent_block(session, settings, now),
        "counters": {
            "decisions_today": sum(tallies.values()),
            "auto_reassigned": tallies.get(DecisionAction.AUTO_REASSIGNED, 0),
            "coverage_requested": tallies.get(DecisionAction.COVERAGE_REQUESTED, 0),
            "coverage_filled": tallies.get(DecisionAction.COVERAGE_FILLED, 0),
            "escalated": tallies.get(DecisionAction.ESCALATED, 0),
            "open_coverage": sum(1 for c in coverage if c["status"] == CoverageStatus.OPEN),
            "needs_manager": sum(1 for c in coverage if c["status"] == CoverageStatus.ESCALATED),
        },
        "coverage": coverage,
        "board": board,
        "feed": feed,
        "staff": staff,
    }
