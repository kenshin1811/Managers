"""The leave-to-cover flow, end to end at the engine level."""

from __future__ import annotations

from sqlalchemy import delete, select

from app.engine import reassignment
from app.engine.journal import recent_decisions
from app.models.audit import DecisionRecord
from app.models.coverage import CoverageRequest
from app.models.employee import EmployeeSkill
from app.models.enums import (
    CoverageStatus,
    DecisionAction,
    LeaveStatus,
    LeaveType,
    ShiftAssignmentStatus,
)
from app.models.leave import LeaveRequest
from app.models.shift import ShiftAssignment
from app.models.task import Task
from app.sim.seed import at


def file_leave(session, world, now, *, starts=None, ends=None) -> LeaveRequest:
    leave = LeaveRequest(
        employee_id=world.employee_id("mai"),
        leave_type=LeaveType.EMERGENCY,
        reason="Family emergency",
        starts_at=starts or at(14, 30),
        ends_at=ends or at(18, 0),
        status=LeaveStatus.PENDING_COVERAGE,
        created_at=now,
    )
    session.add(leave)
    session.flush()
    return leave


def strip_skill(session, world, code: str, keep: set[str]) -> None:
    skill_id = world.skills[code].id
    keep_ids = {world.employee_id(key) for key in keep}
    session.execute(
        delete(EmployeeSkill).where(
            EmployeeSkill.skill_id == skill_id, EmployeeSkill.employee_id.not_in(keep_ids)
        )
    )
    session.flush()


# --- planning is pure -------------------------------------------------------


def test_planning_changes_nothing_and_sends_nothing(session, settings, world, notifier, now):
    leave = file_leave(session, world, now)
    before = world.tasks["pack_1043"].assignee_id

    plan = reassignment.plan_for_leave(session, leave, settings, now)

    assert plan.task_plans, "the leave should collide with something"
    session.refresh(world.tasks["pack_1043"])
    assert world.tasks["pack_1043"].assignee_id == before
    assert notifier.sent == []
    assert session.scalars(select(DecisionRecord)).all() == []


def test_the_driver_collision_is_flagged_critical_and_sorted_first(session, settings, world, now):
    leave = file_leave(session, world, now)
    plan = reassignment.plan_for_leave(session, leave, settings, now)

    first = plan.task_plans[0]
    assert first.impact.title == "Pack order 1043"
    assert first.impact.critical
    assert first.impact.pickup_at == at(14, 50)

    # Work with no order attached has a deadline, but missing it costs nothing
    # outside the building, so it is not critical.
    restock = next(p for p in plan.task_plans if p.impact.title == "Restock the cold line")
    assert not restock.impact.critical


# --- the three outcomes -----------------------------------------------------


def test_on_shift_colleague_is_assigned_automatically(session, settings, world, notifier, now):
    leave = file_leave(session, world, now)
    plan, records = reassignment.handle_leave_request(session, leave, notifier, settings, now)
    session.commit()

    pack = session.get(Task, world.tasks["pack_1043"].id)
    assert pack.assignee_id == world.employee_id("dev")
    assert plan.fully_resolved
    assert leave.status == LeaveStatus.APPROVED
    assert leave.decided_by == "system"

    kinds = [m.kind for m in notifier.sent]
    assert "reassignment" in kinds  # the person picking it up
    assert "handover" in kinds  # the person leaving
    assert "manager_notice" in kinds  # the manager channel
    assert any(r.action == DecisionAction.AUTO_REASSIGNED for r in records)


def test_no_cover_on_shift_opens_a_cover_request(session, settings, short_staffed, notifier, now):
    leave = file_leave(session, short_staffed, now)
    reassignment.handle_leave_request(session, leave, notifier, settings, now)
    session.commit()

    request = session.scalars(select(CoverageRequest).order_by(CoverageRequest.id.desc())).first()
    assert request.status == CoverageStatus.OPEN
    assert request.task_id == short_staffed.tasks["pack_1043"].id
    asked = {offer.employee_id for offer in request.offers}
    assert asked == {
        short_staffed.employee_id("luis"),
        short_staffed.employee_id("sam"),
    }

    # The task stays with the person leaving until somebody actually says yes.
    assert session.get(Task, short_staffed.tasks["pack_1043"].id).assignee_id == (
        short_staffed.employee_id("mai")
    )
    assert leave.status == LeaveStatus.PENDING_COVERAGE


def test_cover_request_expires_before_the_driver_arrives(
    session, settings, short_staffed, notifier, now
):
    leave = file_leave(session, short_staffed, now)
    reassignment.handle_leave_request(session, leave, notifier, settings, now)
    session.commit()

    request = session.scalars(select(CoverageRequest).order_by(CoverageRequest.id.desc())).first()
    pickup = short_staffed.orders["1043"].pickup_at
    assert request.expires_at < pickup, "a manager needs time to act before the driver lands"


def test_nobody_eligible_escalates_instead_of_bending_a_rule(
    session, settings, short_staffed, notifier, now
):
    strip_skill(session, short_staffed, "pack", keep={"mai"})
    leave = file_leave(session, short_staffed, now)
    plan, records = reassignment.handle_leave_request(session, leave, notifier, settings, now)
    session.commit()

    pack_plan = plan.task_plans[0]
    assert pack_plan.action == DecisionAction.ESCALATED
    assert "no eligible cover" in pack_plan.reason

    escalations = [m for m in notifier.sent if m.kind == "escalation"]
    assert escalations and escalations[0].meta["urgent"] is True
    open_requests = session.scalars(
        select(CoverageRequest).where(CoverageRequest.status == CoverageStatus.OPEN)
    ).all()
    assert open_requests == [], "nobody should have been messaged"
    assert leave.status == LeaveStatus.PENDING_COVERAGE
    assert any(r.action == DecisionAction.ESCALATED for r in records)


def test_autonomy_can_be_switched_off_entirely(session, settings, world, notifier, now):
    settings.auto_reassign_enabled = False
    settings.auto_coverage_requests_enabled = False
    leave = file_leave(session, world, now)
    plan, _ = reassignment.handle_leave_request(session, leave, notifier, settings, now)
    session.commit()

    assert all(p.action == DecisionAction.BLOCKED_BY_POLICY for p in plan.task_plans)
    assert session.get(Task, world.tasks["pack_1043"].id).assignee_id == world.employee_id("mai")
    assert [m.kind for m in notifier.sent].count("escalation") == len(plan.task_plans)


# --- side effects -----------------------------------------------------------


def test_the_shift_is_released_so_the_hours_stop_counting(session, settings, world, notifier, now):
    leave = file_leave(session, world, now)
    reassignment.handle_leave_request(session, leave, notifier, settings, now)
    session.commit()

    assignment = session.scalars(
        select(ShiftAssignment).where(ShiftAssignment.employee_id == world.employee_id("mai"))
    ).first()
    assert assignment.status == ShiftAssignmentStatus.RELEASED


def test_one_plan_does_not_hand_the_same_person_overlapping_jobs(
    session, settings, world, notifier, now
):
    """Priya is the obvious pick for both prep jobs -- but they do not overlap."""
    leave = file_leave(session, world, now)
    reassignment.handle_leave_request(session, leave, notifier, settings, now)
    session.commit()

    prep = session.get(Task, world.tasks["prep_1051"].id)
    restock = session.get(Task, world.tasks["restock"].id)
    assert prep.assignee_id is not None and restock.assignee_id is not None
    assert prep.due_at <= restock.starts_at, (
        "these jobs must not overlap for this test to mean anything"
    )


def test_every_decision_records_the_candidates_and_the_rules(
    session, settings, world, notifier, now
):
    leave = file_leave(session, world, now)
    reassignment.handle_leave_request(session, leave, notifier, settings, now)
    session.commit()

    record = next(
        r
        for r in recent_decisions(session)
        if r.action == DecisionAction.AUTO_REASSIGNED
        and r.subject_id == world.tasks["pack_1043"].id
    )
    plan = record.details["plan"]
    assert plan["chosen"]["employee_name"] == "Dev Osei"
    assert record.details["from_employee_id"] == world.employee_id("mai")

    considered = plan["candidates"]
    assert len(considered) > 1
    rejected = [c for c in considered if not c["eligible"]]
    assert rejected, "the rejections belong in the record too"
    assert all(c["rules"] for c in considered), "every candidate carries its rule evaluations"


def test_a_second_leave_does_not_start_a_duplicate_hunt(
    session, settings, short_staffed, notifier, now
):
    leave = file_leave(session, short_staffed, now)
    reassignment.handle_leave_request(session, leave, notifier, settings, now)
    session.commit()
    open_query = select(CoverageRequest).where(CoverageRequest.status == CoverageStatus.OPEN)
    first_count = len(session.scalars(open_query).all())

    again = reassignment.plan_for_leave(session, leave, settings, now)
    pack_plan = next(p for p in again.task_plans if p.impact.title == "Pack order 1043")
    assert pack_plan.action == DecisionAction.NO_ACTION_NEEDED
    assert "already open" in pack_plan.reason
    assert len(session.scalars(open_query).all()) == first_count
