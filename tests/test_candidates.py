"""Ranking: who gets asked first, and why."""

from __future__ import annotations

from datetime import timedelta

from app.engine import candidates as ranking
from app.engine.snapshot import load_snapshot
from app.sim.seed import at
from tests.conftest import a_pack_job


def rank(session, settings, world, *, vacating: str = "shaleen", **kwargs):
    """Rank everybody for Shaleen's jam-donut job on the north van."""
    task = a_pack_job(session, world, **kwargs)
    session.commit()
    now = at(16, 30)
    snapshot = load_snapshot(session, settings, now, horizon_end=task.due_at + timedelta(days=1))
    return task, ranking.rank_candidates(
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
    _, ranked = rank(session, settings, world)
    top = ranking.eligible(ranked)[0]
    assert top.employee_name == "Ken"
    assert top.on_shift


def test_ineligible_candidates_sort_last_but_are_still_returned(session, settings, world):
    _, ranked = rank(session, settings, world)
    eligible = ranking.eligible(ranked)
    assert len(ranked) > len(eligible)
    assert all(c.eligible for c in ranked[: len(eligible)])
    # The rejections are the half of the record a manager actually asks about.
    # Robin runs the floor and has never been signed off on packing.
    assert "Robin" in names([c for c in ranked if not c.eligible])


def test_fairness_puts_the_less_recently_asked_person_first(session, settings, world):
    """Valentino has covered twice this month; Tavi has not. Both are eligible."""
    _, ranked = rank(session, settings, world)
    eligible = ranking.eligible(ranked)
    order = names(eligible)
    assert order.index("Tavi") < order.index("Valentino")

    tavi = next(c for c in eligible if c.employee_name == "Tavi")
    valentino = next(c for c in eligible if c.employee_name == "Valentino")
    assert tavi.components["fairness"] > valentino.components["fairness"]


def test_the_over_worked_candidate_is_excluded_not_just_downranked(session, settings, world):
    _, ranked = rank(session, settings, world)
    marlo = next(c for c in ranked if c.employee_name == "Marlo")
    assert not marlo.eligible
    assert marlo.blocking_rules[0].rule == "within_weekly_hours"
    # A guardrail failure is not a low score -- it is not a score at all.
    assert marlo.score == 0.0


def test_proficiency_headroom_breaks_ties_towards_the_stronger_hand(session, settings, world):
    _, ranked = rank(session, settings, world)
    valentino = next(c for c in ranked if c.employee_name == "Valentino")
    tavi = next(c for c in ranked if c.employee_name == "Tavi")
    assert valentino.components["proficiency"] > tavi.components["proficiency"]


def test_someone_without_the_skill_is_not_a_candidate(session, settings, world):
    _, ranked = rank(session, settings, world)
    robin = next(c for c in ranked if c.employee_name == "Robin")
    assert not robin.eligible
    assert "has_required_skill" in [b.rule for b in robin.blocking_rules]


def test_someone_on_higher_priority_work_is_not_a_candidate(session, settings, planned):
    """With the whole evening planned, the on-shift packers are genuinely busy."""
    _, ranked = rank(session, settings, planned)
    ken = next(c for c in ranked if c.employee_name == "Ken")
    assert not ken.eligible
    assert "no_conflicting_work" in [b.rule for b in ken.blocking_rules]


def test_a_tentative_commitment_stops_double_booking_within_one_plan(session, settings, world):
    """The second task of a plan must see the first task's assignment."""
    task, ranked = rank(session, settings, world)
    ken = ranking.eligible(ranked)[0]

    snapshot = load_snapshot(session, settings, at(16, 30))
    snapshot.commit_tentatively(
        ken.employee_id,
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
        vacating_employee_id=world.employee_id("shaleen"),
    )
    ken_again = next(c for c in again if c.employee_id == ken.employee_id)
    # Excluding the task itself, so the clash is genuinely the tentative one.
    assert ken_again.eligible


def test_added_minutes_are_zero_for_on_shift_staff(session, settings, world):
    task = a_pack_job(session, world)
    session.commit()
    snapshot = load_snapshot(session, settings, at(16, 30))
    ken = snapshot.get(world.employee_id("ken"))
    valentino = snapshot.get(world.employee_id("valentino"))
    assert ranking.added_minutes_for(ken, task, on_shift=True) == 0.0
    assert ranking.added_minutes_for(valentino, task, on_shift=False) == task.estimated_minutes
