"""Time tracking as an input to staffing decisions, not just a record.

The point of these tests is the link between the two: hours logged here are
what stop the engine asking somebody to work an eleventh hour.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.engine import candidates as ranking
from app.engine.snapshot import load_snapshot
from app.models.timeclock import TimeEntry
from app.sim.seed import SIM_DATE, at
from tests.conftest import a_pack_job


def test_an_open_entry_counts_up_to_now_not_to_knocking_off_time():
    entry = TimeEntry(employee_id=1, clock_in=at(10, 0))
    assert entry.is_open
    assert entry.worked_minutes(until=at(14, 0)) == pytest.approx(240)


def test_breaks_come_off_the_paid_total():
    entry = TimeEntry(employee_id=1, clock_in=at(10, 0), clock_out=at(18, 0), break_minutes=30)
    assert entry.worked_minutes() == pytest.approx(450)


def test_an_entry_with_no_end_and_no_reference_point_counts_nothing():
    assert TimeEntry(employee_id=1, clock_in=at(10, 0)).worked_minutes() == 0.0


def test_scheduled_but_unworked_shift_time_still_counts_against_the_limit(session, settings, world):
    """Somebody rostered until 18:00 is not free for an extra job at 17:00."""
    snapshot = load_snapshot(session, settings, at(14, 5))
    dev = snapshot.get(world.employee_id("ken"))
    # Four hours worked so far plus nearly four still rostered.
    assert dev.minutes_today == pytest.approx(4 * 60 + 235, abs=10)


def test_logged_hours_take_somebody_out_of_the_candidate_pool(session, settings, world):
    valentino_id = world.employee_id("valentino")
    task = a_pack_job(session, world)
    session.commit()
    now = at(16, 30)

    def is_eligible() -> bool:
        snapshot = load_snapshot(session, settings, now)
        ranked = ranking.rank_candidates(
            snapshot,
            task,
            window_start=at(16, 40),
            window_end=at(17, 45),
            critical=True,
            vacating_employee_id=world.employee_id("shaleen"),
        )
        return next(c for c in ranked if c.employee_id == valentino_id).eligible

    assert is_eligible(), "Valentino starts out available"

    for offset in range(1, 5):
        day = SIM_DATE - timedelta(days=offset)
        session.add(
            TimeEntry(
                employee_id=valentino_id,
                clock_in=at(8, 0, day=day),
                clock_out=at(20, 0, day=day),
            )
        )
    session.flush()

    assert not is_eligible(), "48 hours logged this week has to rule him out"


def test_last_weeks_hours_do_not_count_against_this_week(session, settings, world):
    valentino_id = world.employee_id("valentino")
    for offset in range(8, 12):
        day = SIM_DATE - timedelta(days=offset)
        session.add(
            TimeEntry(
                employee_id=valentino_id,
                clock_in=at(8, 0, day=day),
                clock_out=at(20, 0, day=day),
            )
        )
    session.flush()

    snapshot = load_snapshot(session, settings, at(14, 5))
    assert snapshot.get(valentino_id).minutes_week == pytest.approx(0.0)
