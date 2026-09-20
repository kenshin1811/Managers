"""The agent noticing on its own that the evening stopped working.

Everything else in this suite pokes the engine with an event. These tests
poke it with nothing at all, which is the harder case and the one a floor
actually lives in: the work does not get done, the clock moves, and a van
that was fine an hour ago is not fine any more.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.engine import planning
from app.engine import watch as watch_engine
from app.models.enums import TaskStatus
from app.models.task import Task
from app.sim.seed import at


def statuses(session, settings, now) -> dict[str, str]:
    plan = planning.plan_day(session, settings, now)
    return {r.run: r.as_dict(settings.at_risk_margin_minutes)["status"] for r in plan.runs}


def first_look(session, settings, notifier, now):
    """The agent's opening opinion, which it records without acting on."""
    return watch_engine.watch(session, settings, notifier, now)


# --- the quiet cases --------------------------------------------------------


def test_an_empty_board_is_not_a_late_one(session, settings, world, notifier, now):
    """Before the first plan of the day there is nothing to have a view about."""
    result = watch_engine.watch(session, settings, notifier, now)

    assert not result.acted
    assert result.plan is None
    assert notifier.sent == []


def test_the_first_look_records_its_verdict_without_touching_anything(
    session, settings, planned, notifier, now
):
    before = {t.id for t in session.scalars(select(Task)).all()}

    result = first_look(session, settings, notifier, now)
    session.commit()

    assert not result.planned, "there is nothing yet to have changed its mind about"
    assert result.records == []
    assert {t.id for t in session.scalars(select(Task)).all()} == before


def test_nothing_changing_changes_nothing(session, settings, planned, notifier, now):
    """A timer firing is not a reason for anybody on the floor to move.

    Committing a plan deletes and recreates the planner's rows, so a re-plan
    every minute would hand every packer fresh task ids for an identical
    board.
    """
    first_look(session, settings, notifier, now)
    session.commit()
    before = {t.id for t in session.scalars(select(Task)).all()}
    notifier.clear()

    for minutes in (1, 2, 3):
        result = watch_engine.watch(session, settings, notifier, now + timedelta(minutes=minutes))
        session.commit()
        assert not result.acted, f"acted at +{minutes} min with nothing changed"

    assert {t.id for t in session.scalars(select(Task)).all()} == before
    assert notifier.sent == []


# --- the case the whole thing exists for ------------------------------------


def test_time_passing_on_its_own_is_enough_to_raise_an_alarm(
    session, settings, planned, notifier, now
):
    """Nobody pressed anything. The work just did not get done."""
    first_look(session, settings, notifier, now)
    session.commit()
    notifier.clear()
    assert all(s == "on_time" for s in statuses(session, settings, now).values())

    # Two hours later, with the board untouched: everything that was going to
    # happen has not happened.
    later = now + timedelta(hours=2)
    assert any(s != "on_time" for s in statuses(session, settings, later).values()), (
        "the seeded evening has to actually go wrong for this test to mean anything"
    )

    result = watch_engine.watch(session, settings, notifier, later)
    session.commit()

    assert result.slipped, "a van moved the wrong way and nothing said so"
    assert result.planned, "the board it rewrote is the one it now believes"
    escalations = [m for m in notifier.sent if m.kind == "escalation"]
    assert len(escalations) == 1
    assert "slipped" in escalations[0].text


def test_the_same_slip_is_only_raised_once(session, settings, planned, notifier, now):
    """Sixty alerts an hour is how a manager learns to ignore all of them."""
    first_look(session, settings, notifier, now)
    session.commit()

    later = now + timedelta(hours=2)
    watch_engine.watch(session, settings, notifier, later)
    session.commit()
    notifier.clear()

    again = watch_engine.watch(session, settings, notifier, later + timedelta(minutes=1))
    session.commit()

    assert not again.acted
    assert notifier.sent == []


def test_the_alert_names_each_van_and_when_it_leaves(session, settings, planned, notifier, now):
    first_look(session, settings, notifier, now)
    session.commit()
    notifier.clear()

    result = watch_engine.watch(session, settings, notifier, now + timedelta(hours=2))
    session.commit()

    escalation = next(m for m in notifier.sent if m.kind == "escalation")
    rendered = str(escalation.blocks)
    for run in result.slipped:
        assert run in rendered
    assert "leaves" in rendered, "a van that is late is only useful with its time"


def test_a_van_that_will_miss_is_urgent_and_one_cutting_it_fine_is_not(
    session, settings, planned, notifier, now
):
    first_look(session, settings, notifier, now)
    session.commit()
    notifier.clear()

    result = watch_engine.watch(session, settings, notifier, now + timedelta(hours=2))
    session.commit()

    plan = result.plan
    worst = {r.as_dict(settings.at_risk_margin_minutes)["status"] for r in plan.runs}
    escalation = next(m for m in notifier.sent if m.kind == "escalation")
    assert escalation.meta["urgent"] is ("missed" in worst)


def test_recovering_is_not_an_alert(session, settings, planned, notifier, now):
    """Good news does not need a klaxon -- but the board still catches up."""
    first_look(session, settings, notifier, now)
    session.commit()

    later = now + timedelta(hours=2)
    watch_engine.watch(session, settings, notifier, later)
    session.commit()
    notifier.clear()

    # The floor catches up: everything outstanding is suddenly done.
    for task in session.scalars(select(Task).where(Task.stage.is_not(None))).all():
        task.done_quantity = task.quantity
        task.status = TaskStatus.DONE
    session.commit()

    result = watch_engine.watch(session, settings, notifier, later + timedelta(minutes=1))
    session.commit()

    assert result.slipped == []
    assert [m for m in notifier.sent if m.kind == "escalation"] == []


# --- it is written down -----------------------------------------------------


def test_the_alarm_lands_in_the_audit_log_like_everything_else(
    session, settings, planned, notifier, now
):
    from app.engine.journal import recent_decisions

    first_look(session, settings, notifier, now)
    session.commit()
    watch_engine.watch(session, settings, notifier, now + timedelta(hours=2))
    session.commit()

    entry = next(r for r in recent_decisions(session) if r.trigger == "watch")
    assert entry.details["runs"]
    assert entry.details["plan"]["runs"], "the plan it was looking at is part of the record"


def test_yesterdays_verdict_does_not_carry_over(session, settings, planned, notifier, now):
    """A new day starts with no opinion, not with last night's."""
    first_look(session, settings, notifier, now)
    session.commit()

    tomorrow = at(16, 30) + timedelta(days=1)
    result = watch_engine.watch(session, settings, notifier, tomorrow)
    session.commit()

    assert not result.planned, "a fresh day has nothing to compare against"
    assert result.slipped == []


# --- through the sweep ------------------------------------------------------


def test_the_sweep_runs_the_watch_as_well(db_engine, session, settings, planned, notifier, now):
    from sqlalchemy.orm import sessionmaker

    from app import runtime
    from app.clock import FrozenClock
    from app.models.state import SWEEP_HEARTBEAT
    from app.scheduler import run_sweep
    from app.state import last_seen

    factory = sessionmaker(bind=db_engine, expire_on_commit=False, future=True)
    runtime.set_notifier(notifier)
    try:
        runtime.set_clock(FrozenClock(now))
        run_sweep(settings, factory)  # the first look, which records and stops

        runtime.set_clock(FrozenClock(now + timedelta(hours=2)))
        acted = run_sweep(settings, factory)
    finally:
        runtime.reset()

    assert acted >= 1, "two hours of nothing happening is something to act on"
    assert last_seen(session, SWEEP_HEARTBEAT) is not None
    assert any(m.kind == "escalation" for m in notifier.sent)


def test_a_failing_watch_does_not_take_the_coverage_sweep_down(
    db_engine, session, settings, planned, notifier, now, monkeypatch
):
    """An unanswered cover request has a driver behind it. It comes first."""
    from sqlalchemy.orm import sessionmaker

    from app import runtime
    from app.clock import FrozenClock
    from app.models.state import SWEEP_HEARTBEAT
    from app.scheduler import run_sweep
    from app.state import last_seen

    def explode(*args, **kwargs):
        raise RuntimeError("the planner fell over")

    monkeypatch.setattr(watch_engine, "watch", explode)
    factory = sessionmaker(bind=db_engine, expire_on_commit=False, future=True)
    runtime.set_notifier(notifier)
    try:
        runtime.set_clock(FrozenClock(now))
        acted = run_sweep(settings, factory)
    finally:
        runtime.reset()

    assert acted == 0
    assert last_seen(session, SWEEP_HEARTBEAT) is not None, "the heartbeat still has to beat"
