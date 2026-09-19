"""Handing a decision back to a human.

Escalation is not the failure mode of this system -- it is one of its two
correct outcomes.  Anything the engine cannot settle without breaking a
guardrail arrives here, with the whole trace attached, so the manager picking
it up is not starting from scratch.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings
from app.engine.journal import record_decision
from app.engine.types import CandidateScore
from app.models.audit import DecisionRecord
from app.models.enums import DecisionAction
from app.notifications import templates
from app.notifications.base import Notifier

SKIP_ENTIRELY = {"not_the_person_leaving", "employee_active"}


def summarise_blockers(candidates: list[CandidateScore]) -> list[str]:
    """Turn a pile of rule failures into something a manager can read.

    "Nobody was available" is useless. "Four people lack the packing
    certification, two would go over their daily hours" tells a manager what
    to actually do about it.
    """
    tally: Counter[str] = Counter()
    for candidate in candidates:
        blockers = candidate.blocking_rules
        if not blockers:
            continue
        # The employee going on leave and anyone off the roster are not
        # candidates at all, so their other failures are noise.
        if any(rule.rule in SKIP_ENTIRELY for rule in blockers):
            continue
        # One headline reason each. Rules are evaluated most-fundamental
        # first, so the first failure is the one worth reporting, and the
        # counts then add up to a number of people rather than a number of
        # rule evaluations.
        tally[blockers[0].rule] += 1
    phrasing = {
        "has_required_skill": "not trained for it",
        "not_on_leave": "already on leave",
        "no_conflicting_work": "busy with equal or higher priority work",
        "within_daily_hours": "would exceed the daily hours limit",
        "within_weekly_hours": "would exceed the weekly hours limit",
        "min_rest_respected": "would not get the minimum rest",
        "within_availability": "outside their stated availability",
        "outside_quiet_hours": "quiet hours, and this is not critical",
        "under_ask_limit": "already asked the maximum times today",
        "employee_active": "not on the active roster",
        "not_the_person_leaving": "is the employee going on leave",
    }
    return [f"{count} x {phrasing.get(rule, rule)}" for rule, count in tally.most_common()]


def escalate(
    session: Session,
    notifier: Notifier,
    settings: Settings,
    *,
    now: datetime,
    trigger: str,
    summary: str,
    subject_type: str = "",
    subject_id: int | None = None,
    detail_lines: list[str] | None = None,
    details: dict[str, Any] | None = None,
    urgent: bool = False,
) -> DecisionRecord:
    """Record the escalation and put it in front of a manager."""
    message = templates.escalation(summary, settings, detail_lines, urgent=urgent)
    delivery = notifier.send(message)
    payload = dict(details or {})
    payload["notified"] = {
        "channel": message.channel,
        "ok": delivery.ok,
        "text": message.text,
    }
    if detail_lines:
        payload["blockers"] = detail_lines
    return record_decision(
        session,
        now=now,
        action=DecisionAction.ESCALATED,
        trigger=trigger,
        summary=summary,
        subject_type=subject_type,
        subject_id=subject_id,
        details=payload,
    )
