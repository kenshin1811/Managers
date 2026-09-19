"""The background sweep.

Without this, a cover request nobody answers just sits there: the order is
uncovered, the leave is unapproved, and nothing on any screen says so.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import runtime
from app.clock import FrozenClock
from app.engine import reassignment
from app.models.coverage import CoverageRequest
from app.models.enums import CoverageStatus
from app.scheduler import SWEEP_SECONDS, build_scheduler, run_sweep
from app.sim.seed import at
from tests.test_reassignment import file_leave


def test_the_sweep_job_is_registered_and_does_not_pile_up():
    from app.config import Settings

    scheduler = build_scheduler(Settings())
    try:
        job = scheduler.get_job("coverage_sweep")
        assert job is not None
        assert job.trigger.interval.total_seconds() == SWEEP_SECONDS
        # A slow sweep must not have a second copy launched on top of it.
        assert job.max_instances == 1
        assert job.coalesce is True
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


def test_run_sweep_escalates_an_unanswered_request(
    db_engine, session, settings, short_staffed, notifier, now
):
    leave = file_leave(session, short_staffed, now)
    reassignment.handle_leave_request(session, leave, notifier, settings, now)
    session.commit()

    pickup = short_staffed.orders["1043"].pickup_at
    runtime.set_clock(FrozenClock(pickup - timedelta(minutes=5)))
    runtime.set_notifier(notifier)
    factory = sessionmaker(bind=db_engine, expire_on_commit=False, future=True)
    try:
        acted = run_sweep(settings, factory)
    finally:
        runtime.reset()

    assert acted == 1
    request = session.scalars(
        select(CoverageRequest).where(CoverageRequest.leave_request_id == leave.id)
    ).one()
    session.refresh(request)
    assert request.status == CoverageStatus.ESCALATED
    assert any(m.kind == "escalation" for m in notifier.sent)


def test_run_sweep_does_nothing_when_there_is_nothing_to_do(
    db_engine, session, settings, world, notifier
):
    runtime.set_clock(FrozenClock(at(14, 5)))
    runtime.set_notifier(notifier)
    factory = sessionmaker(bind=db_engine, expire_on_commit=False, future=True)
    try:
        assert run_sweep(settings, factory) == 0
    finally:
        runtime.reset()
