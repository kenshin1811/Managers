"""Each hard rule, on its own.

These are the rules that decide whether an automated system is allowed to
change somebody's working day, so each one gets a test that proves it says no
for the right reason -- not just that it says no.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.engine import guardrails
from app.engine.guardrails import EvaluationContext
from app.models.employee import Employee, EmployeeSkill, Skill
from app.models.enums import TaskPriority
from app.models.task import Task
from app.sim.seed import at
from tests.conftest import make_facts


@pytest.fixture
def skill() -> Skill:
    skill = Skill(code="pack", name="Order packing")
    skill.id = 1
    return skill


@pytest.fixture
def task(skill) -> Task:
    task = Task(
        title="Pack order 1043",
        required_skill_id=skill.id,
        min_proficiency=3,
        starts_at=at(14, 15),
        due_at=at(14, 45),
        estimated_minutes=20,
        priority=TaskPriority.HIGH,
    )
    task.id = 99
    task.required_skill = skill
    task.order = None
    return task


def employee_with(level: int | None, skill_id: int = 1) -> Employee:
    employee = Employee(full_name="Test Person", active=True)
    employee.id = 42
    employee.skills = []
    employee.availability = []
    if level is not None:
        employee.skills = [EmployeeSkill(employee_id=42, skill_id=skill_id, proficiency=level)]
    return employee


def context(task, settings, *, employee=None, on_shift=True, added=0.0, critical=False, **facts_kw):
    employee = employee or employee_with(4)
    return EvaluationContext(
        facts=make_facts(employee, **facts_kw),
        task=task,
        window_start=at(14, 15),
        window_end=at(14, 45),
        on_shift=on_shift,
        added_minutes=added,
        critical=critical,
        now=at(14, 5),
        settings=settings,
    )


def test_untrained_employee_is_rejected_by_name(task, settings):
    result = guardrails.rule_has_required_skill(
        context(task, settings, employee=employee_with(None))
    )
    assert not result.passed
    assert "not trained in pack" in result.reason


def test_under_qualified_employee_is_rejected(task, settings):
    result = guardrails.rule_has_required_skill(context(task, settings, employee=employee_with(2)))
    assert not result.passed
    assert result.detail == {"skill": "pack", "required": 3, "held": 2}


def test_qualified_employee_passes(task, settings):
    assert guardrails.rule_has_required_skill(context(task, settings)).passed


def test_task_with_no_skill_requirement_accepts_anyone(task, settings):
    task.required_skill_id = None
    assert guardrails.rule_has_required_skill(
        context(task, settings, employee=employee_with(None))
    ).passed


def test_daily_hours_limit_blocks_an_eleventh_hour(task, settings):
    ctx = context(task, settings, on_shift=False, added=60, minutes_today=9.5 * 60)
    result = guardrails.rule_within_daily_hours(ctx)
    assert not result.passed
    assert result.detail["projected_hours"] == pytest.approx(10.5)


def test_daily_hours_ignores_added_time_for_someone_already_on_shift(task, settings):
    """Redirecting somebody who is already at work does not lengthen their day."""
    ctx = context(task, settings, on_shift=True, added=0.0, minutes_today=9.5 * 60)
    assert guardrails.rule_within_daily_hours(ctx).passed


def test_weekly_hours_limit_blocks_a_willing_volunteer(task, settings):
    ctx = context(task, settings, on_shift=False, added=20, minutes_week=48 * 60)
    result = guardrails.rule_within_weekly_hours(ctx)
    assert not result.passed
    assert "weekly limit" in result.reason


def test_minimum_rest_is_enforced_for_call_ins(task, settings):
    ctx = context(task, settings, on_shift=False, last_worked_end=at(14, 15) - timedelta(hours=4))
    result = guardrails.rule_min_rest_respected(ctx)
    assert not result.passed
    assert result.detail["rest_hours"] == pytest.approx(4.0)


def test_minimum_rest_does_not_apply_to_someone_already_on_shift(task, settings):
    ctx = context(task, settings, on_shift=True, last_worked_end=at(14, 0))
    result = guardrails.rule_min_rest_respected(ctx)
    assert result.passed
    assert "already on shift" in result.reason


def test_call_in_needs_availability_on_file(task, settings):
    result = guardrails.rule_within_availability(context(task, settings, on_shift=False))
    assert not result.passed
    assert "no availability on file" in result.reason


def test_quiet_hours_block_routine_work(task, settings):
    ctx = context(task, settings, on_shift=False, critical=False)
    ctx.now = at(23, 30)
    result = guardrails.rule_outside_quiet_hours(ctx)
    assert not result.passed


def test_quiet_hours_yield_to_a_waiting_driver(task, settings):
    ctx = context(task, settings, on_shift=False, critical=True)
    ctx.now = at(23, 30)
    result = guardrails.rule_outside_quiet_hours(ctx)
    assert result.passed
    assert result.detail["override"] is True


def test_quiet_hours_override_can_be_switched_off(task, settings):
    settings.quiet_hours_override_for_critical = False
    ctx = context(task, settings, on_shift=False, critical=True)
    ctx.now = at(23, 30)
    assert not guardrails.rule_outside_quiet_hours(ctx).passed


def test_nobody_is_pestered_past_the_daily_ask_limit(task, settings):
    ctx = context(task, settings, on_shift=False, asks_today=settings.max_coverage_asks_per_day)
    result = guardrails.rule_under_ask_limit(ctx)
    assert not result.passed
    assert result.detail["asks_today"] == 3


def test_equal_priority_clash_is_a_hard_no(task, settings, skill):
    clash = Task(
        title="Other work",
        starts_at=at(14, 20),
        due_at=at(14, 50),
        priority=TaskPriority.HIGH,
        status="pending",
    )
    clash.id = 7
    clash.order = None
    ctx = context(task, settings, live_tasks=[clash])
    result = guardrails.rule_no_conflicting_work(ctx)
    assert not result.passed
    assert result.detail["conflicting_task_ids"] == [7]


def test_lower_priority_work_may_be_displaced(task, settings):
    clash = Task(
        title="Routine wiping down",
        starts_at=at(14, 20),
        due_at=at(14, 50),
        priority=TaskPriority.ROUTINE,
        status="pending",
    )
    clash.id = 8
    clash.order = None
    ctx = context(task, settings, live_tasks=[clash])
    result = guardrails.rule_no_conflicting_work(ctx)
    assert result.passed
    assert result.detail["displaced_task_ids"] == [8]


def test_evaluate_reports_the_displaced_task(task, settings):
    clash = Task(
        title="Routine wiping down",
        starts_at=at(14, 20),
        due_at=at(14, 50),
        priority=TaskPriority.ROUTINE,
        status="pending",
    )
    clash.id = 8
    clash.order = None
    rules, displaced = guardrails.evaluate(context(task, settings, live_tasks=[clash]))
    assert displaced == 8
    assert len(rules) == len(guardrails.ALL_RULES)


def test_the_person_going_on_leave_is_never_their_own_cover(task, settings):
    ctx = context(task, settings)
    ctx.vacating_employee_id = ctx.facts.employee.id
    assert not guardrails.rule_not_the_person_leaving(ctx).passed
