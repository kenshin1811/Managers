"""Hard constraints on what the system may do without asking a human.

Every rule is a small pure function returning a named :class:`RuleResult`.
Nothing here relaxes a rule under pressure: if the only way to cover an order
is to break one of these, the engine escalates instead.  The reason this is
worth the ceremony is that "the computer rostered me for a twelfth hour" is a
real harm, and a rule with a name and a recorded reason is one a person can
argue with.

Rules that only make sense for someone who would be *coming in specially*
(rest, availability, quiet hours, how often we have already pestered them) are
skipped for staff already on shift, and say so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.clock import minute_of_day, to_local
from app.config import Settings
from app.engine.snapshot import EmployeeFacts
from app.engine.types import RuleResult
from app.models.task import Task


@dataclass
class EvaluationContext:
    """One person, one task, one moment."""

    facts: EmployeeFacts
    task: Task
    window_start: datetime
    window_end: datetime
    on_shift: bool
    added_minutes: float
    """Hours this work would *add* -- zero for someone already on the clock."""

    critical: bool
    now: datetime
    settings: Settings
    vacating_employee_id: int | None = None


def _skip(rule: str) -> RuleResult:
    return RuleResult(rule=rule, passed=True, reason="not applicable: already on shift")


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


def rule_employee_active(ctx: EvaluationContext) -> RuleResult:
    active = ctx.facts.employee.active
    return RuleResult(
        rule="employee_active",
        passed=active,
        reason="on the active roster" if active else "not on the active roster",
    )


def rule_not_the_person_leaving(ctx: EvaluationContext) -> RuleResult:
    is_vacating = ctx.facts.employee.id == ctx.vacating_employee_id
    return RuleResult(
        rule="not_the_person_leaving",
        passed=not is_vacating,
        reason="this is the employee going on leave" if is_vacating else "different employee",
    )


def rule_has_required_skill(ctx: EvaluationContext) -> RuleResult:
    skill_id = ctx.task.required_skill_id
    if skill_id is None:
        return RuleResult(
            rule="has_required_skill", passed=True, reason="task requires no specific skill"
        )
    level = ctx.facts.employee.proficiency_in(skill_id)
    skill_code = ctx.task.required_skill.code if ctx.task.required_skill else str(skill_id)
    if level is None:
        return RuleResult(
            rule="has_required_skill",
            passed=False,
            reason=f"not trained in {skill_code}",
            detail={"skill": skill_code, "required": ctx.task.min_proficiency, "held": None},
        )
    passed = level >= ctx.task.min_proficiency
    return RuleResult(
        rule="has_required_skill",
        passed=passed,
        reason=(
            f"{skill_code} at level {level} (needs {ctx.task.min_proficiency})"
            if passed
            else f"{skill_code} at level {level}, below the required {ctx.task.min_proficiency}"
        ),
        detail={"skill": skill_code, "required": ctx.task.min_proficiency, "held": level},
    )


def rule_not_on_leave(ctx: EvaluationContext) -> RuleResult:
    clash = ctx.facts.on_leave_during(ctx.window_start, ctx.window_end)
    return RuleResult(
        rule="not_on_leave",
        passed=not clash,
        reason="on leave for this window" if clash else "not on leave",
    )


def rule_no_conflicting_work(ctx: EvaluationContext) -> RuleResult:
    """Clashing work of equal or higher priority is a hard no.

    Lower-priority work is allowed to be displaced -- the engine records which
    task would be dropped so the audit row shows the trade it made.
    """
    clashes = ctx.facts.conflicting_tasks(
        ctx.window_start, ctx.window_end, exclude_task_id=ctx.task.id
    )
    if not clashes:
        return RuleResult(rule="no_conflicting_work", passed=True, reason="free for this window")
    this_rank = ctx.task.priority_rank
    blocking = [tid for tid, rank in clashes if rank >= this_rank]
    if blocking:
        return RuleResult(
            rule="no_conflicting_work",
            passed=False,
            reason=f"already on work of equal or higher priority (task {blocking[0]})",
            detail={"conflicting_task_ids": blocking},
        )
    displaced = [tid for tid, _ in clashes]
    return RuleResult(
        rule="no_conflicting_work",
        passed=True,
        reason=f"holds lower-priority work that can be displaced (task {displaced[0]})",
        detail={"displaced_task_ids": displaced},
    )


def rule_within_daily_hours(ctx: EvaluationContext) -> RuleResult:
    projected = ctx.facts.projected_today(ctx.added_minutes) / 60.0
    limit = ctx.settings.max_daily_hours
    passed = projected <= limit
    return RuleResult(
        rule="within_daily_hours",
        passed=passed,
        reason=f"{projected:.1f}h of a {limit:.0f}h daily limit",
        detail={"projected_hours": round(projected, 2), "limit_hours": limit},
    )


def rule_within_weekly_hours(ctx: EvaluationContext) -> RuleResult:
    projected = ctx.facts.projected_week(ctx.added_minutes) / 60.0
    limit = ctx.settings.max_weekly_hours
    passed = projected <= limit
    return RuleResult(
        rule="within_weekly_hours",
        passed=passed,
        reason=f"{projected:.1f}h of a {limit:.0f}h weekly limit",
        detail={"projected_hours": round(projected, 2), "limit_hours": limit},
    )


def rule_min_rest_respected(ctx: EvaluationContext) -> RuleResult:
    if ctx.on_shift:
        return _skip("min_rest_respected")
    rest = ctx.facts.rest_hours_before(ctx.window_start)
    limit = ctx.settings.min_rest_hours
    if rest is None:
        return RuleResult(
            rule="min_rest_respected",
            passed=True,
            reason="no recent shift on record",
        )
    passed = rest >= limit
    return RuleResult(
        rule="min_rest_respected",
        passed=passed,
        reason=f"{rest:.1f}h rest since last shift (needs {limit:.0f}h)",
        detail={"rest_hours": round(rest, 2), "limit_hours": limit},
    )


def rule_within_availability(ctx: EvaluationContext) -> RuleResult:
    if ctx.on_shift:
        return _skip("within_availability")
    windows = ctx.facts.employee.availability
    if not windows:
        return RuleResult(
            rule="within_availability",
            passed=False,
            reason="no availability on file for call-ins",
        )
    weekday, minute = minute_of_day(ctx.window_start, ctx.settings.business_tz)
    passed = any(window.covers(weekday, minute) for window in windows)
    local = to_local(ctx.window_start, ctx.settings.business_tz)
    return RuleResult(
        rule="within_availability",
        passed=passed,
        reason=(
            f"available at {local:%a %H:%M}" if passed else f"not available at {local:%a %H:%M}"
        ),
        detail={"local_time": local.isoformat()},
    )


def rule_outside_quiet_hours(ctx: EvaluationContext) -> RuleResult:
    """Do not message off-duty staff in the middle of the night over routine work."""
    if ctx.on_shift:
        return _skip("outside_quiet_hours")
    settings = ctx.settings
    local_now = to_local(ctx.now, settings.business_tz)
    hour = local_now.hour
    start, end = settings.quiet_hours_start, settings.quiet_hours_end
    in_quiet = hour >= start or hour < end if start > end else start <= hour < end
    if not in_quiet:
        return RuleResult(
            rule="outside_quiet_hours",
            passed=True,
            reason=f"{local_now:%H:%M} local, not quiet hours",
        )
    if ctx.critical and settings.quiet_hours_override_for_critical:
        return RuleResult(
            rule="outside_quiet_hours",
            passed=True,
            reason=f"{local_now:%H:%M} local is quiet hours, overridden for critical work",
            detail={"override": True},
        )
    return RuleResult(
        rule="outside_quiet_hours",
        passed=False,
        reason=f"{local_now:%H:%M} local is quiet hours and this work is not critical",
    )


def rule_under_ask_limit(ctx: EvaluationContext) -> RuleResult:
    if ctx.on_shift:
        return _skip("under_ask_limit")
    limit = ctx.settings.max_coverage_asks_per_day
    asks = ctx.facts.asks_today
    passed = asks < limit
    return RuleResult(
        rule="under_ask_limit",
        passed=passed,
        reason=f"asked {asks} time(s) today of a {limit} limit",
        detail={"asks_today": asks, "limit": limit},
    )


ALL_RULES = (
    rule_employee_active,
    rule_not_the_person_leaving,
    rule_has_required_skill,
    rule_not_on_leave,
    rule_no_conflicting_work,
    rule_within_daily_hours,
    rule_within_weekly_hours,
    rule_min_rest_respected,
    rule_within_availability,
    rule_outside_quiet_hours,
    rule_under_ask_limit,
)


def evaluate(ctx: EvaluationContext) -> tuple[list[RuleResult], int | None]:
    """Run every guardrail. Returns the results and any task that would be displaced."""
    results = [rule(ctx) for rule in ALL_RULES]
    displaced: int | None = None
    for result in results:
        ids = result.detail.get("displaced_task_ids")
        if result.passed and ids:
            displaced = ids[0]
    return results, displaced
