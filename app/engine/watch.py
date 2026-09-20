"""The agent watching its own evening.

Everything else in the engine reacts to something: a leave request, a spoken
command, somebody tapping a link.  This is the part that acts on nothing
happening at all.

Time passing is itself a change.  A run that was comfortable at 16:30 is not
comfortable at 18:20 with the same work still outstanding, and nobody has
pressed anything.  So on every sweep the agent re-plans the evening --
privately, in memory -- and compares its verdict to the last one it recorded.
When a van slips from on time to at risk, or from at risk to late, it rewrites
the board and says so, once.

Two things it deliberately does *not* do:

It does not rewrite the board when its verdict has not changed. Committing a
plan deletes and recreates the planner's rows, so a re-plan every minute would
hand every packer a fresh set of task ids each minute -- for a board that is
identical. Nothing on the floor should move because a timer fired.

It does not escalate twice for the same slip. A manager who gets the same
alert sixty times an hour stops reading any of them, and the sixty-first is
the one that mattered.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import to_local
from app.config import Settings
from app.engine import planning
from app.engine.escalation import escalate
from app.models.audit import DecisionRecord
from app.models.enums import TaskStatus
from app.models.state import SystemState
from app.models.task import Task
from app.notifications.base import Notifier

logger = logging.getLogger("managers.watch")

#: Where the last verdict is kept, so a slip is noticed once rather than
#: every minute until somebody fixes it.
RUN_STATUS = "run_status"

#: Ranked worst-last. A run only earns an alert when it moves *down* this
#: list; recovering is good news and good news does not need an alert.
SEVERITY = {"on_time": 0, "at_risk": 1, "missed": 2}

WORDING = {
    "at_risk": "is cutting it fine",
    "missed": "will not make it",
}


@dataclass
class WatchResult:
    """What the agent noticed this time round."""

    planned: bool = False
    """True when the board was actually rewritten."""

    slipped: list[str] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)
    records: list[DecisionRecord] = field(default_factory=list)
    plan: planning.DayPlan | None = None

    @property
    def acted(self) -> bool:
        return self.planned or bool(self.records)


def _statuses(plan: planning.DayPlan, settings: Settings) -> dict[str, str]:
    """Each run's verdict, as the dashboard would show it."""
    margin = settings.at_risk_margin_minutes
    return {run.run: run.as_dict(margin)["status"] for run in plan.runs}


def _remembered(session: Session, today: str) -> dict[str, str]:
    """The last verdict, or nothing if it was for a different day."""
    row = session.get(SystemState, RUN_STATUS)
    if row is None or not row.value:
        return {}
    try:
        stored = json.loads(row.value)
    except json.JSONDecodeError:
        return {}
    if stored.get("day") != today:
        return {}
    return stored.get("runs", {})


def _remember(session: Session, today: str, statuses: dict[str, str], now: datetime) -> None:
    payload = json.dumps({"day": today, "runs": statuses}, sort_keys=True)
    row = session.get(SystemState, RUN_STATUS)
    if row is None:
        session.add(SystemState(key=RUN_STATUS, value=payload, updated_at=now))
    else:
        row.value = payload
        row.updated_at = now
    session.flush()


def _has_live_work(session: Session) -> bool:
    return (
        session.scalar(
            select(Task.id)
            .where(
                Task.stage.is_not(None),
                Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED]),
            )
            .limit(1)
        )
        is not None
    )


def watch(
    session: Session,
    settings: Settings,
    notifier: Notifier,
    now: datetime,
) -> WatchResult:
    """Look at the evening, and act only if the answer has changed.

    Returns what it noticed. The caller commits; this writes nothing it has
    not decided to write.
    """
    result = WatchResult()
    if not _has_live_work(session):
        # An empty board is not a late one. Before the first plan of the day
        # there is nothing to have an opinion about.
        return result

    plan = planning.plan_day(session, settings, now)
    result.plan = plan
    statuses = _statuses(plan, settings)
    today = to_local(now, settings.business_tz).date().isoformat()
    before = _remembered(session, today)

    for run, status in statuses.items():
        was = before.get(run)
        if was is None:
            continue
        if SEVERITY[status] > SEVERITY[was]:
            result.slipped.append(run)
        elif SEVERITY[status] < SEVERITY[was]:
            result.recovered.append(run)

    # First look of the day: record the verdict, leave the board alone. There
    # is nothing to compare against yet, and the plan already on the board is
    # the one this just reproduced.
    if not before:
        _remember(session, today, statuses, now)
        return result

    if statuses == before:
        return result

    # The verdict moved, so the board is rewritten -- this is the moment the
    # agent has actually changed its mind about the evening.
    planning.commit_plan(session, plan, settings, now)
    result.planned = True
    _remember(session, today, statuses, now)

    if result.slipped:
        result.records.append(_raise_it(session, settings, notifier, now, plan, result.slipped))
    return result


def _raise_it(
    session: Session,
    settings: Settings,
    notifier: Notifier,
    now: datetime,
    plan: planning.DayPlan,
    slipped: list[str],
) -> DecisionRecord:
    """One alert naming every van that just moved the wrong way."""
    margin = settings.at_risk_margin_minutes
    by_run = {run.run: run for run in plan.runs}
    lines = []
    for name in slipped:
        run = by_run[name]
        status = run.as_dict(margin)["status"]
        leaves = to_local(run.departs_at, settings.business_tz).strftime("%H:%M")
        short = f", short by {run.short_by:.0f} min" if run.short_by > 0 else ""
        lines.append(f"{name} run leaves {leaves} and {WORDING[status]}{short}")

    worst = max(slipped, key=lambda name: SEVERITY[by_run[name].as_dict(margin)["status"]])
    urgent = by_run[worst].as_dict(margin)["status"] == "missed"
    summary = f"{len(slipped)} van(s) slipped with nobody touching anything: " + ", ".join(slipped)
    logger.info("watch: %s", summary)
    return escalate(
        session,
        notifier,
        settings,
        now=now,
        trigger="watch",
        summary=summary,
        subject_type="day_plan",
        detail_lines=lines,
        details={"runs": slipped, "plan": plan.as_dict(settings)},
        urgent=urgent,
    )
