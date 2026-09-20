"""The day planner: turning an order book into who does what, in what order.

The planner is the part of this system that actually manages. Everything
else reacts to something going wrong; this decides what the evening looks
like when nothing has. So the tests here are less about the arithmetic than
about the two things a floor manager would check first -- that the pipeline
runs in the right order, and that a van nobody can get ready is called late
rather than quietly left off the board.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.engine import planning
from app.models.enums import Stage, TaskStatus
from app.models.task import Task
from app.sim.seed import RUN_DEPARTURES, at

# --- what the day needs -----------------------------------------------------


def test_the_work_comes_out_of_the_order_book_stage_by_stage(session, settings, world, now):
    work = planning.build_work(session, settings, now)

    stages = {unit.stage for unit in work}
    assert stages == {Stage.RETRIEVE, Stage.PACK, Stage.SORT, Stage.DISPATCH}
    assert all(unit.minutes > 0 for unit in work)
    assert all(unit.run in RUN_DEPARTURES for unit in work)


def test_only_what_is_still_outstanding_is_planned(session, settings, world, now):
    """The internal run is 85% packed already; the planner must not redo it."""
    work = planning.build_work(session, settings, now)

    internal = sum(u.quantity for u in work if u.run == "internal" and u.stage == Stage.PACK)
    ordered = world.orders["wholesale_team"].units
    assert 0 < internal < ordered


def test_retrieval_is_batched_across_a_run_rather_than_per_order(session, settings, world, now):
    """Two shops on the north run both want jam donuts: that is one trip."""
    work = planning.build_work(session, settings, now)
    pulls = [
        u for u in work if u.run == "north" and u.stage == Stage.RETRIEVE and "jam donut" in u.title
    ]
    assert len(pulls) == 1, "nobody walks to the freezer twice for the same product"

    north = ("fitzroy", "brunswick")
    wanted = sum(
        line.outstanding
        for key in north
        for line in world.orders[key].lines
        if line.product.code == "jam_donut"
    )
    assert pulls[0].quantity == wanted


def test_fresh_donuts_skip_the_freezer(session, settings, world, now):
    """Vanilla slices, long Johns and eclairs are made fresh, not pulled."""
    work = planning.build_work(session, settings, now)
    fresh = {"vanilla slice", "long john", "eclair"}

    pulled = {u.title.lower() for u in work if u.stage == Stage.RETRIEVE}
    assert not any(name in title for title in pulled for name in fresh)

    packed = {u.title.lower() for u in work if u.stage == Stage.PACK}
    assert any("long john" in title for title in packed)


def test_nothing_is_planned_past_the_nine_oclock_cutoff(session, settings, world, now):
    work = planning.build_work(session, settings, now)
    cutoff = planning._cutoff(now, settings)
    assert all(unit.deadline <= cutoff for unit in work)


# --- who does it, and when --------------------------------------------------


def test_a_full_team_gets_every_van_away_on_time(session, settings, world, now):
    plan = planning.plan_day(session, settings, now)

    assert plan.feasible
    assert plan.missed_runs == []
    assert all(run.ready_at <= run.departs_at for run in plan.runs)


def test_the_pipeline_is_respected_within_a_run(session, settings, world, now):
    """Nobody packs from an empty bench, and nobody loads an unsorted van."""
    plan = planning.plan_day(session, settings, now)
    by_key = {p.unit.key: p for p in plan.placements}

    for placement in plan.placements:
        upstream = placement.unit.depends_on
        if upstream is None or upstream not in by_key:
            continue
        before = by_key[upstream]
        assert before.ends_at <= placement.starts_at, (
            f"{placement.unit.title} starts before {before.unit.title} finishes"
        )


def test_nobody_is_in_two_places_at_once(session, settings, world, now):
    plan = planning.plan_day(session, settings, now)

    slots: dict[int, list[tuple]] = {}
    for placement in plan.placements:
        slots.setdefault(placement.employee_id, []).append(
            (placement.starts_at, placement.ends_at, placement.unit.title)
        )
    for assignments in slots.values():
        assignments.sort()
        for earlier, later in zip(assignments, assignments[1:], strict=False):
            assert earlier[1] <= later[0], f"{earlier[2]} overlaps {later[2]}"


def test_work_goes_to_people_who_are_signed_off_for_it(session, settings, world, now):
    """The freezer is the real bottleneck here, not the headcount."""
    plan = planning.plan_day(session, settings, now)
    signed_off = {world.employees[k].full_name for k in ("ken", "shaleen")}

    pulls = [p for p in plan.placements if p.unit.stage == Stage.RETRIEVE]
    assert pulls
    assert {p.employee_name for p in pulls} <= signed_off


def test_the_vans_are_planned_in_the_order_they_leave(session, settings, world, now):
    plan = planning.plan_day(session, settings, now)
    assert [run.run for run in plan.runs] == ["internal", "north", "east", "south", "city"]
    departures = [run.departs_at for run in plan.runs]
    assert departures == sorted(departures)


# --- when it does not fit ---------------------------------------------------


def test_a_thin_roster_makes_the_tight_runs_slip(session, settings, short_staffed, now):
    """Two packers is not a smaller evening, it is a late one -- and it says so."""
    plan = planning.plan_day(session, settings, now)

    assert not plan.feasible
    assert plan.missed_runs, "the whole point of the short roster is that it does not fit"
    late = {run.run for run in plan.missed_runs}
    assert "north" in late, "the 18:00 van is the one with no slack in it"


def test_a_run_nobody_can_touch_is_reported_missed_not_on_time(
    session, settings, short_staffed, now
):
    """A van with nothing placed against it must never read as ready."""
    plan = planning.plan_day(session, settings, now)
    for run in plan.runs:
        if run.ready_at is None:
            assert run.status == "missed"


def test_an_unplaceable_job_carries_the_reason(session, settings, world, now):
    from app.models.employee import EmployeeSkill

    session.execute(
        EmployeeSkill.__table__.delete().where(EmployeeSkill.skill_id == world.skills["freezer"].id)
    )
    session.flush()

    plan = planning.plan_day(session, settings, now)
    stuck = [p for p in plan.placements if p.unit.stage == Stage.RETRIEVE and not p.placed]
    assert stuck, "with nobody signed off on the freezer, nothing can be pulled"
    assert all(p.reason for p in stuck), "a job nobody can do has to say why"


# --- putting it on the board ------------------------------------------------


def test_committing_puts_real_tasks_on_the_board(session, settings, world, now):
    plan, tasks = planning.plan_and_commit(session, settings, now)
    session.commit()

    assert tasks
    assert len(tasks) == len([p for p in plan.placements if p.placed])
    stored = session.scalars(select(Task).where(Task.planned_by.is_not(None))).all()
    assert len(stored) == len(tasks)
    assert all(task.assignee_id is not None for task in stored)
    assert all(task.stage is not None and task.run is not None for task in stored)


def test_dependencies_survive_the_commit(session, settings, world, now):
    planning.plan_and_commit(session, settings, now)
    session.commit()

    packs = session.scalars(select(Task).where(Task.stage == Stage.PACK)).all()
    linked = [t for t in packs if t.depends_on_id is not None]
    assert linked, "packing frozen stock waits on the freezer trip"
    for task in linked:
        assert task.depends_on.stage == Stage.RETRIEVE
        # due_at on the board is the deadline, not the slot, so the ordering
        # to check is when each job is meant to be picked up.
        assert task.depends_on.starts_at < task.starts_at
        assert task.blocked_by_upstream, "upstream is still pending, so this is blocked"


def test_replanning_does_not_trip_over_the_board_it_just_made(session, settings, world, now):
    """The second run has to ignore its own unstarted draft.

    Left in, every packer looks fully booked against work that only exists
    because the planner put it there, and the honest answer -- this evening
    fits -- comes back as five late vans.
    """
    first, _ = planning.plan_and_commit(session, settings, now)
    session.commit()
    assert first.feasible

    again, _ = planning.plan_and_commit(session, settings, now)
    session.commit()
    assert again.feasible
    assert [r.status for r in again.runs] == [r.status for r in first.runs]


def test_replanning_leaves_alone_what_is_not_its_own(session, settings, world, now):
    _, first = planning.plan_and_commit(session, settings, now)
    session.commit()

    # Somebody has started one of them, and somebody has added one by hand.
    started = first[0]
    started.status = TaskStatus.IN_PROGRESS
    session.add(
        Task(
            title="Sweep the freezer floor",
            assignee_id=world.employee_id("ken"),
            starts_at=now + timedelta(hours=3),
            due_at=now + timedelta(hours=3, minutes=30),
            estimated_minutes=30,
        )
    )
    session.commit()

    planning.plan_and_commit(session, settings, now)
    session.commit()

    titles = {task.title for task in session.scalars(select(Task)).all()}
    assert "Sweep the freezer floor" in titles, "hand-written work is not the planner's to delete"
    assert started.title in titles, "work somebody has started is not the planner's either"


def test_a_committed_task_knows_which_planning_run_made_it(session, settings, world, now):
    _, tasks = planning.plan_and_commit(session, settings, now, trigger="a_test")
    session.commit()
    assert all(task.planned_by for task in tasks)


def test_the_plan_is_recorded_as_a_decision(session, settings, world, now):
    from app.engine.journal import recent_decisions

    planning.plan_and_commit(session, settings, now)
    session.commit()

    entry = next(r for r in recent_decisions(session) if r.subject_type == "day_plan")
    assert entry.details["runs"]
    assert entry.details["total_minutes"] > 0
    assert "minutes of work" in entry.summary


# --- the shape of the answer ------------------------------------------------


def test_a_van_finishing_just_before_it_leaves_reads_as_at_risk(session, settings, world, now):
    plan = planning.plan_day(session, settings, now)
    run = plan.runs[0]
    run.ready_at = run.departs_at - timedelta(minutes=settings.at_risk_margin_minutes - 1)
    run.short_by = 0.0

    # On time by the arithmetic, but one hold-up from not being, and a floor
    # manager wants to hear about it before it is a missed van.
    assert run.status == "on_time"
    assert run.as_dict(settings.at_risk_margin_minutes)["status"] == "at_risk"


def test_the_plan_serialises_everything_the_page_needs(session, settings, world, now):
    plan = planning.plan_day(session, settings, now)
    body = plan.as_dict(settings)

    assert body["feasible"] is True
    assert {"generated_at", "cutoff", "runs", "placements", "total_minutes"} <= set(body)
    assert all(stamp.endswith("Z") for stamp in (body["generated_at"], body["cutoff"]))
    first = body["placements"][0]
    assert first["stage"] in {"retrieve", "pack", "sort", "dispatch"}
    assert first["deadline"].endswith("Z")


def test_the_cutoff_is_nine_in_the_evening_local_time(session, settings, world, now):
    assert planning._cutoff(now, settings) == at(21, 0)
