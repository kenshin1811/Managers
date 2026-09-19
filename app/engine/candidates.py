"""Who could take this job, and who should.

Two stages, kept apart on purpose.  The guardrails decide who *may* be asked
-- that answer is binary and non-negotiable.  The score below only decides the
*order* among people who already passed.  A high score never rescues someone a
guardrail rejected.
"""

from __future__ import annotations

from datetime import datetime

from app.engine import guardrails
from app.engine.guardrails import EvaluationContext
from app.engine.snapshot import EmployeeFacts, WorkforceSnapshot
from app.engine.types import CandidateScore
from app.models.task import Task


def added_minutes_for(facts: EmployeeFacts, task: Task, on_shift: bool) -> float:
    """Hours this task would add to someone's day.

    Somebody already on the clock is being *redirected*, not given extra
    hours, so it adds nothing.  Somebody coming in off-shift is working longer
    than they were rostered for, and that counts.
    """
    if on_shift:
        return 0.0
    return float(task.estimated_minutes)


def _score(
    facts: EmployeeFacts,
    task: Task,
    *,
    on_shift: bool,
    displaces_task_id: int | None,
    snapshot: WorkforceSnapshot,
) -> dict[str, float]:
    settings = snapshot.settings
    components: dict[str, float] = {}

    # Continuity: already working this same order. Least disruption, fewest
    # handover mistakes, and they already know what is in the bag.
    same_order = task.order_id is not None and any(
        other.order_id == task.order_id and other.id != task.id for other in facts.live_tasks
    )
    components["continuity"] = settings.weight_continuity if same_order else 0.0

    # Already here beats having to come in, every time.
    components["on_shift"] = settings.weight_on_shift if on_shift else 0.0

    # Genuinely free beats having to drop something.
    if displaces_task_id is None:
        components["idle"] = settings.weight_idle
    else:
        components["idle"] = settings.weight_idle * 0.3

    # Comfortably qualified beats scraping the minimum.
    if task.required_skill_id is not None:
        level = facts.employee.proficiency_in(task.required_skill_id) or 0
        headroom = max(0, level - task.min_proficiency)
        components["proficiency"] = settings.weight_proficiency * min(1.0, headroom / 4.0)
    else:
        components["proficiency"] = 0.0

    # Fairness: spread the burden. Someone who covered twice this month should
    # not be first in line again.
    components["fairness"] = settings.weight_fairness / (1.0 + facts.recent_covers)

    return components


def rank_candidates(
    snapshot: WorkforceSnapshot,
    task: Task,
    *,
    window_start: datetime,
    window_end: datetime,
    critical: bool,
    vacating_employee_id: int | None = None,
) -> list[CandidateScore]:
    """Evaluate the whole workforce against one task.

    Everyone is returned, not just the eligible -- the rejections are half of
    what makes the audit trail worth keeping.  Eligible candidates come first,
    best score first; ties break on employee id so the result is stable.
    """
    scored: list[CandidateScore] = []

    for facts in snapshot.all_facts():
        on_shift = facts.on_shift_during(window_start, window_end)
        ctx = EvaluationContext(
            facts=facts,
            task=task,
            window_start=window_start,
            window_end=window_end,
            on_shift=on_shift,
            added_minutes=added_minutes_for(facts, task, on_shift),
            critical=critical,
            now=snapshot.now,
            settings=snapshot.settings,
            vacating_employee_id=vacating_employee_id,
        )
        rules, displaces = guardrails.evaluate(ctx)
        candidate = CandidateScore(
            employee_id=facts.employee.id,
            employee_name=facts.employee.full_name,
            on_shift=on_shift,
            rules=rules,
            displaces_task_id=displaces,
        )
        if candidate.eligible:
            candidate.components = _score(
                facts, task, on_shift=on_shift, displaces_task_id=displaces, snapshot=snapshot
            )
            candidate.score = sum(candidate.components.values())
        scored.append(candidate)

    scored.sort(key=lambda c: (not c.eligible, -c.score, c.employee_id))
    return scored


def eligible(candidates: list[CandidateScore]) -> list[CandidateScore]:
    return [c for c in candidates if c.eligible]


def on_shift_first(candidates: list[CandidateScore]) -> list[CandidateScore]:
    """Eligible candidates who are already at work."""
    return [c for c in candidates if c.eligible and c.on_shift]


def off_shift(candidates: list[CandidateScore]) -> list[CandidateScore]:
    """Eligible candidates who would have to be called in."""
    return [c for c in candidates if c.eligible and not c.on_shift]
