"""Replies to a cover request: accepting, declining, and running out of time."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.engine import coverage as coverage_engine
from app.engine import reassignment
from app.models.coverage import CoverageOffer, CoverageRequest
from app.models.enums import CoverageStatus, DecisionAction, LeaveStatus, OfferStatus
from app.models.task import Task
from app.models.timeclock import TimeEntry
from app.sim.seed import SIM_DATE, at
from tests.test_reassignment import file_leave


@pytest.fixture
def open_request(session, settings, short_staffed, notifier, now):
    """A live cover request for the packing job, with Luis and Sam asked."""
    leave = file_leave(session, short_staffed, now)
    reassignment.handle_leave_request(session, leave, notifier, settings, now)
    session.commit()
    notifier.clear()
    request = session.scalars(
        select(CoverageRequest).where(CoverageRequest.status == CoverageStatus.OPEN)
    ).one()
    return request


def offer_for(session, request, world, key) -> CoverageOffer:
    return session.scalars(
        select(CoverageOffer).where(
            CoverageOffer.coverage_request_id == request.id,
            CoverageOffer.employee_id == world.employee_id(key),
        )
    ).one()


# --- accepting --------------------------------------------------------------


def test_accepting_assigns_the_task_and_stands_everyone_else_down(
    session, settings, short_staffed, notifier, now, open_request
):
    luis = offer_for(session, open_request, short_staffed, "luis")
    outcome = coverage_engine.accept_offer(
        session, luis, notifier, settings, now + timedelta(minutes=3)
    )

    assert outcome.ok and outcome.status == "accepted"
    session.refresh(open_request)
    assert open_request.status == CoverageStatus.FILLED
    assert open_request.filled_by_id == short_staffed.employee_id("luis")

    task = session.get(Task, open_request.task_id)
    assert task.assignee_id == short_staffed.employee_id("luis")

    sam = offer_for(session, open_request, short_staffed, "sam")
    assert sam.status == OfferStatus.SUPERSEDED
    assert any("Sam" not in m.text and "covering" in m.text for m in notifier.sent)


def test_the_leave_is_approved_only_once_cover_is_secured(
    session, settings, short_staffed, notifier, now, open_request
):
    leave = session.scalars(
        select(
            LeaveStatus.__class__
            and __import__("app.models.leave", fromlist=["LeaveRequest"]).LeaveRequest
        )
    ).one()
    assert leave.status == LeaveStatus.PENDING_COVERAGE

    luis = offer_for(session, open_request, short_staffed, "luis")
    coverage_engine.accept_offer(session, luis, notifier, settings, now + timedelta(minutes=3))

    session.refresh(leave)
    assert leave.status == LeaveStatus.APPROVED
    assert leave.decided_by == "system"


def test_using_the_same_link_twice_changes_nothing(
    session, settings, short_staffed, notifier, now, open_request
):
    luis = offer_for(session, open_request, short_staffed, "luis")
    coverage_engine.accept_offer(session, luis, notifier, settings, now)
    before = len(notifier.sent)

    again = coverage_engine.accept_offer(session, luis, notifier, settings, now)
    assert again.ok and again.status == "accepted"
    assert len(notifier.sent) == before, "a replay must not message anyone a second time"


def test_a_late_acceptance_is_told_someone_else_got_there_first(
    session, settings, short_staffed, notifier, now, open_request
):
    luis = offer_for(session, open_request, short_staffed, "luis")
    sam = offer_for(session, open_request, short_staffed, "sam")
    coverage_engine.accept_offer(session, luis, notifier, settings, now)

    outcome = coverage_engine.accept_offer(session, sam, notifier, settings, now)
    assert not outcome.ok
    assert outcome.status == "already_filled"
    assert session.get(Task, open_request.task_id).assignee_id == (
        short_staffed.employee_id("luis")
    )


def test_a_volunteer_who_would_break_a_rule_is_refused_and_escalated(
    session, settings, short_staffed, notifier, now, open_request
):
    """Minutes passed between the ask and the answer. The rules are re-checked."""
    luis_id = short_staffed.employee_id("luis")
    for offset in range(1, 5):
        day = SIM_DATE - timedelta(days=offset)
        session.add(
            TimeEntry(
                employee_id=luis_id,
                clock_in=at(8, 0, day=day),
                clock_out=at(20, 0, day=day),
                source="kiosk",
            )
        )
    session.flush()

    luis = offer_for(session, open_request, short_staffed, "luis")
    outcome = coverage_engine.accept_offer(session, luis, notifier, settings, now)

    assert not outcome.ok and outcome.status == "blocked"
    assert any("weekly limit" in reason for reason in outcome.blockers)
    session.refresh(open_request)
    assert open_request.status == CoverageStatus.OPEN, "the job still needs somebody"
    assert [m.kind for m in notifier.sent].count("escalation") == 1


# --- declining --------------------------------------------------------------


def test_one_decline_leaves_the_request_open(
    session, settings, short_staffed, notifier, now, open_request
):
    luis = offer_for(session, open_request, short_staffed, "luis")
    outcome = coverage_engine.decline_offer(session, luis, notifier, settings, now)

    assert outcome.ok and outcome.status == "declined"
    session.refresh(open_request)
    assert open_request.status == CoverageStatus.OPEN
    assert luis.status == OfferStatus.DECLINED


def test_the_last_decline_escalates_rather_than_going_quiet(
    session, settings, short_staffed, notifier, now, open_request
):
    for key in ("luis", "sam"):
        offer = offer_for(session, open_request, short_staffed, key)
        coverage_engine.decline_offer(session, offer, notifier, settings, now)

    session.refresh(open_request)
    assert open_request.status == CoverageStatus.ESCALATED
    assert open_request.escalated_at is not None
    escalations = [m for m in notifier.sent if m.kind == "escalation"]
    assert escalations, "silence is the one outcome a manager must never get"
    assert "declined" in escalations[-1].text or "no cover" in escalations[-1].text.lower()


def test_declining_widens_the_search_when_somebody_is_left(
    session, settings, short_staffed, notifier, now
):
    settings.coverage_batch_size = 1
    leave = file_leave(session, short_staffed, now)
    reassignment.handle_leave_request(session, leave, notifier, settings, now)
    session.commit()
    request = session.scalars(
        select(CoverageRequest).where(CoverageRequest.status == CoverageStatus.OPEN)
    ).one()
    assert len(request.offers) == 1

    coverage_engine.decline_offer(session, request.offers[0], notifier, settings, now)
    session.refresh(request)

    assert request.status == CoverageStatus.OPEN
    assert request.wave == 2
    assert len(request.offers) == 2
    assert request.offers[-1].employee_id == short_staffed.employee_id("sam")


# --- timeouts ---------------------------------------------------------------


def test_the_sweep_ignores_a_request_still_in_its_window(
    session, settings, short_staffed, notifier, now, open_request
):
    records = coverage_engine.sweep_open_requests(session, notifier, settings, now)
    assert records == []
    session.refresh(open_request)
    assert open_request.status == CoverageStatus.OPEN


def test_silence_becomes_an_escalation_before_the_driver_arrives(
    session, settings, short_staffed, notifier, now, open_request
):
    pickup = short_staffed.orders["1043"].pickup_at
    too_late = pickup - timedelta(minutes=settings.escalation_lead_minutes - 1)

    records = coverage_engine.sweep_open_requests(session, notifier, settings, too_late)

    assert records and records[0].action == DecisionAction.ESCALATED
    session.refresh(open_request)
    assert open_request.status == CoverageStatus.ESCALATED
    assert all(o.status == OfferStatus.EXPIRED for o in open_request.offers)
    assert any(m.meta.get("urgent") for m in notifier.sent if m.kind == "escalation")


def test_a_timeout_widens_the_pool_while_there_is_still_time(
    session, settings, short_staffed, notifier, now
):
    settings.coverage_batch_size = 1
    settings.coverage_response_timeout_minutes = 5
    leave = file_leave(session, short_staffed, now)
    reassignment.handle_leave_request(session, leave, notifier, settings, now)
    session.commit()
    request = session.scalars(
        select(CoverageRequest).where(CoverageRequest.status == CoverageStatus.OPEN)
    ).one()

    records = coverage_engine.sweep_open_requests(
        session, notifier, settings, request.expires_at + timedelta(seconds=1)
    )

    assert records and records[0].action == DecisionAction.COVERAGE_REQUESTED
    session.refresh(request)
    assert request.status == CoverageStatus.OPEN
    assert request.wave == 2
    assert len(request.offers) == 2
