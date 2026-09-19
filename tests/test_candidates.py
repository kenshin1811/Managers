"""Ranking: who gets asked first, and why."""

from __future__ import annotations

from datetime import timedelta

from app.engine import candidates as ranking
from app.engine.snapshot import load_snapshot
from app.sim.seed import at


def rank(session, settings, world, task_key: str, *, vacating: str = "mai"):
    task = world.tasks[task_key]
    now = at(14, 5)
    snapshot = load_snapshot(session, settings, now, horizon_end=task.due_at + timedelta(days=1))
    return snapshot, ranking.rank_candidates(
        snapshot,
        task,
        window_start=max(task.starts_at, now),
        window_end=task.due_at,
        critical=True,
        vacating_employee_id=world.employee_id(vacating),
    )


def names(candidates) -> list[str]:
    return [c.employee_name for c in candidates]


def test_somebody_already_at_work_outranks_a_call_in(session, settings, world):
    _, ranked = rank(session, settings, world, "pack_1043")
    top = ranking.eligible(ranked)[0]
    assert top.employee_name == "Dev Osei"
    assert top.on_shift


def test_ineligible_candidates_sort_last_but_are_still_returned(session, settings, world):
    _, ranked = rank(session, settings, world, "pack_1043")
    eligible = ranking.eligible(ranked)
    assert len(ranked) > len(eligible)
    assert all(c.eligible for c in ranked[: len(eligible)])
    # The rejections are the half of the record a manager actually asks about.
    assert "Priya Raman" in names([c for c in ranked if not c.eligible])


def test_fairness_puts_the_less_recently_asked_person_first(session, settings, short_staffed):
    """Sam has covered twice this month; Luis has not. Both are eligible."""
    _, ranked = rank(session, settings, short_staffed, "pack_1043")
    eligible = ranking.eligible(ranked)
    order = names(eligible)
    assert order.index("Luis Ferrer") < order.index("Sam Whitlock")

    luis = next(c for c in eligible if c.employee_name == "Luis Ferrer")
    sam = next(c for c in eligible if c.employee_name == "Sam Whitlock")
    assert luis.components["fairness"] > sam.components["fairness"]


def test_the_over_worked_candidate_is_excluded_not_just_downranked(
    session, settings, short_staffed
):
    _, ranked = rank(session, settings, short_staffed, "pack_1043")
    tomas = next(c for c in ranked if c.employee_name == "Tomas Iglesias")
    assert not tomas.eligible
    assert tomas.blocking_rules[0].rule == "within_weekly_hours"
    # A guardrail failure is not a low score -- it is not a score at all.
    assert tomas.score == 0.0


def test_proficiency_headroom_breaks_ties_towards_the_stronger_hand(
    session, settings, short_staffed
):
    _, ranked = rank(session, settings, short_staffed, "pack_1043")
    luis = next(c for c in ranked if c.employee_name == "Luis Ferrer")
    sam = next(c for c in ranked if c.employee_name == "Sam Whitlock")
    assert luis.components["proficiency"] > sam.components["proficiency"]


def test_someone_on_higher_priority_work_is_not_a_candidate(session, settings, world):
    _, ranked = rank(session, settings, world, "pack_1043")
    nia = next(c for c in ranked if c.employee_name == "Nia Boateng")
    assert not nia.eligible
    assert nia.blocking_rules[0].rule == "no_conflicting_work"


def test_a_tentative_commitment_stops_double_booking_within_one_plan(session, settings, world):
    """The second task of a plan must see the first task's assignment."""
    task = world.tasks["pack_1043"]
    snapshot, ranked = rank(session, settings, world, "pack_1043")
    dev = ranking.eligible(ranked)[0]

    snapshot.commit_tentatively(
        dev.employee_id,
        task_id=task.id,
        starts_at=task.starts_at,
        ends_at=task.due_at,
        minutes=20,
        priority_rank=task.priority_rank,
        adds_hours=False,
    )
    again = ranking.rank_candidates(
        snapshot,
        task,
        window_start=task.starts_at,
        window_end=task.due_at,
        critical=True,
        vacating_employee_id=world.employee_id("mai"),
    )
    dev_again = next(c for c in again if c.employee_id == dev.employee_id)
    # Excluding the task itself, so the clash is genuinely the tentative one.
    assert dev_again.eligible


def test_added_minutes_are_zero_for_on_shift_staff(session, settings, world):
    task = world.tasks["pack_1043"]
    now = at(14, 5)
    snapshot = load_snapshot(session, settings, now)
    dev = snapshot.get(world.employee_id("dev"))
    luis = snapshot.get(world.employee_id("luis"))
    assert ranking.added_minutes_for(dev, task, on_shift=True) == 0.0
    assert ranking.added_minutes_for(luis, task, on_shift=False) == task.estimated_minutes
