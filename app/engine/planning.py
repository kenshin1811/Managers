"""Turning an order book into an evening's work.

This is the half of the agent that acts like a manager rather than a
firefighter: given today's orders and who is on, it works out what has to
happen, in what order, by when, and who does it.

The shape of the problem is not "is there enough time today" -- there usually
is -- but "can the 18:00 van leave on time". Everything on a run goes
together, so one late consignment holds up every stop behind it, and spare
capacity at 20:00 buys you nothing at 18:00. So the planner schedules
backwards from each departure and reports the runs it cannot make, with the
number of minutes it is short.

It reuses the guardrails and the candidate ranking unchanged. A packer who
would go over their hours is not a candidate here either, and the reason they
were passed over is recorded the same way.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.engine import candidates as ranking
from app.engine.escalation import summarise_blockers
from app.engine.snapshot import WorkforceSnapshot, load_snapshot
from app.engine.types import CandidateScore
from app.models.destination import Destination
from app.models.enums import (
    DecisionAction,
    OrderStatus,
    ProductKind,
    Stage,
    TaskPriority,
    TaskStatus,
)
from app.models.order import Order
from app.models.product import Product
from app.models.task import Task

PLANNER = "day-planner"

#: Stages run in this order and each waits on the one before it.
PIPELINE = (Stage.RETRIEVE, Stage.PACK, Stage.SORT, Stage.DISPATCH)

SKILL_FOR_STAGE = {
    Stage.RETRIEVE: "freezer",
    Stage.SORT: "pack_frozen",
    Stage.DISPATCH: "dispatch",
}


# --------------------------------------------------------------------------
# What the planner produces
# --------------------------------------------------------------------------


@dataclass
class WorkUnit:
    """A piece of work the day needs, before anybody is attached to it."""

    key: str
    stage: Stage
    title: str
    run: str
    minutes: float
    deadline: datetime
    skill_code: str | None
    min_proficiency: int = 1
    quantity: int = 0
    product_id: int | None = None
    order_id: int | None = None
    depends_on: str | None = None
    priority: TaskPriority = TaskPriority.HIGH


@dataclass
class Placement:
    """A work unit with a person and a slot, or the reason it has neither."""

    unit: WorkUnit
    employee_id: int | None = None
    employee_name: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    late_by: float = 0.0
    reason: str = ""
    candidates: list[CandidateScore] = field(default_factory=list)

    @property
    def placed(self) -> bool:
        return self.employee_id is not None

    @property
    def on_time(self) -> bool:
        return self.placed and self.late_by <= 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.unit.key,
            "stage": str(self.unit.stage),
            "title": self.unit.title,
            "run": self.unit.run,
            "quantity": self.unit.quantity,
            "minutes": round(self.unit.minutes, 1),
            "employee": self.employee_name,
            "starts_at": self.starts_at.isoformat() + "Z" if self.starts_at else None,
            "ends_at": self.ends_at.isoformat() + "Z" if self.ends_at else None,
            "deadline": self.unit.deadline.isoformat() + "Z",
            "late_by": round(self.late_by, 1),
            "reason": self.reason,
        }


@dataclass
class RunPlan:
    run: str
    departs_at: datetime
    stops: list[str]
    units: int
    ready_at: datetime | None = None
    short_by: float = 0.0
    blockers: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        # A run with work nobody can take is not on time, however the
        # arithmetic falls out: there is simply no plan for it.
        if self.blockers or self.ready_at is None:
            return "missed"
        if self.short_by > 0:
            return "missed"
        return "on_time"

    def as_dict(self, margin: int) -> dict[str, Any]:
        at_risk = (
            self.ready_at is not None
            and self.short_by <= 0
            and (self.departs_at - self.ready_at).total_seconds() / 60 < margin
        )
        return {
            "run": self.run,
            "departs_at": self.departs_at.isoformat() + "Z",
            "stops": self.stops,
            "units": self.units,
            "ready_at": self.ready_at.isoformat() + "Z" if self.ready_at else None,
            "short_by": round(self.short_by, 1),
            "status": "at_risk" if at_risk else self.status,
            "blockers": self.blockers,
        }


@dataclass
class DayPlan:
    generated_at: datetime
    cutoff: datetime
    placements: list[Placement] = field(default_factory=list)
    runs: list[RunPlan] = field(default_factory=list)

    @property
    def feasible(self) -> bool:
        return all(p.on_time for p in self.placements)

    @property
    def total_minutes(self) -> float:
        return sum(p.unit.minutes for p in self.placements)

    @property
    def missed_runs(self) -> list[RunPlan]:
        # Short on minutes and short of people both mean the van goes late.
        return [r for r in self.runs if r.status == "missed"]

    def as_dict(self, settings: Settings) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat() + "Z",
            "cutoff": self.cutoff.isoformat() + "Z",
            "feasible": self.feasible,
            "total_minutes": round(self.total_minutes),
            "runs": [r.as_dict(settings.at_risk_margin_minutes) for r in self.runs],
            "placements": [p.as_dict() for p in self.placements],
        }


# --------------------------------------------------------------------------
# Working out what the day needs
# --------------------------------------------------------------------------


def _cutoff(now: datetime, settings: Settings) -> datetime:
    """Today's hard stop, as a naive-UTC moment."""
    from app.clock import to_local

    local = to_local(now, settings.business_tz)
    local_cutoff = local.replace(
        hour=settings.dispatch_cutoff_hour, minute=0, second=0, microsecond=0
    )
    return now + (local_cutoff - local)


def _is_planner_draft(task: Task) -> bool:
    """Work this planner put up and nobody has touched."""
    return (
        task.planned_by == PLANNER and task.status == TaskStatus.PENDING and not task.done_quantity
    )


def build_work(session: Session, settings: Settings, now: datetime) -> list[WorkUnit]:
    """Explode the open order book into stages, batched the way a line works.

    Batching is per run rather than per order: the team packs a tub of jam
    donuts once and splits it between Fitzroy and Brunswick afterwards, which
    is both how it is actually done and far fewer, larger jobs on the board.
    Batching across *runs* would be more efficient still, and wrong -- it
    would drag the 20:45 stock forward to the 18:00 deadline.
    """
    horizon = now + timedelta(hours=settings.plan_horizon_hours)
    orders = list(
        session.scalars(
            select(Order)
            .where(Order.status == OrderStatus.OPEN, Order.pickup_at <= horizon)
            .order_by(Order.pickup_at)
        )
    )

    by_run: dict[str, list[Order]] = {}
    for order in orders:
        run = order.destination.run if order.destination else "unassigned"
        by_run.setdefault(run, []).append(order)

    units: list[WorkUnit] = []
    for run, run_orders in by_run.items():
        departs = min(order.pickup_at for order in run_orders)
        stops = len(run_orders)

        # Backwards from the van: load it, and before that sort it.
        dispatch_minutes = (
            settings.dispatch_minutes_per_run + settings.dispatch_minutes_per_stop * stops
        )
        dispatch_deadline = departs
        sort_deadline = departs - timedelta(minutes=dispatch_minutes)

        # How much of each product this run needs, and which orders want it.
        demand: dict[int, int] = {}
        for order in run_orders:
            for line in order.lines:
                if line.outstanding:
                    demand[line.product_id] = demand.get(line.product_id, 0) + line.outstanding
        if not demand:
            continue

        pack_keys: list[str] = []
        for product_id, quantity in sorted(demand.items()):
            product = session.get(Product, product_id)
            if product is None:
                continue

            pack_key = f"{run}:pack:{product.code}"
            retrieve_key = None

            if product.kind == ProductKind.FROZEN:
                trays = math.ceil(quantity / max(1, product.units_per_tray))
                retrieve_minutes = (
                    settings.retrieve_setup_minutes + settings.retrieve_minutes_per_tray * trays
                )
                retrieve_key = f"{run}:retrieve:{product.code}"
                units.append(
                    WorkUnit(
                        key=retrieve_key,
                        stage=Stage.RETRIEVE,
                        title=f"Pull {quantity} {product.name.lower()} from the freezer",
                        run=run,
                        minutes=retrieve_minutes,
                        # Pulled in time for packing to start, not for the van.
                        deadline=sort_deadline - timedelta(minutes=product.pack_minutes(quantity)),
                        skill_code="freezer",
                        quantity=quantity,
                        product_id=product.id,
                    )
                )

            units.append(
                WorkUnit(
                    key=pack_key,
                    stage=Stage.PACK,
                    title=f"Pack and label {quantity} {product.name.lower()}",
                    run=run,
                    minutes=product.pack_minutes(quantity),
                    deadline=sort_deadline,
                    skill_code=(
                        "pack_fresh" if product.kind == ProductKind.FRESH else "pack_frozen"
                    ),
                    quantity=quantity,
                    product_id=product.id,
                    depends_on=retrieve_key,
                )
            )
            pack_keys.append(pack_key)

        # One sort per stop: the batches get split into that shop's boxes.
        sort_keys: list[str] = []
        for order in run_orders:
            outstanding = sum(line.outstanding for line in order.lines)
            if not outstanding:
                continue
            sort_key = f"{run}:sort:{order.code}"
            units.append(
                WorkUnit(
                    key=sort_key,
                    stage=Stage.SORT,
                    title=f"Sort {outstanding} for {order.where}",
                    run=run,
                    minutes=settings.sort_setup_minutes
                    + outstanding * settings.sort_seconds_per_unit / 60.0,
                    deadline=sort_deadline,
                    skill_code="pack_frozen",
                    quantity=outstanding,
                    order_id=order.id,
                    # Sorting waits on the last batch this run needs.
                    depends_on=pack_keys[-1] if pack_keys else None,
                )
            )
            sort_keys.append(sort_key)

        units.append(
            WorkUnit(
                key=f"{run}:dispatch",
                stage=Stage.DISPATCH,
                title=f"Load the {run} van ({stops} stop{'s' if stops > 1 else ''})",
                run=run,
                minutes=dispatch_minutes,
                deadline=dispatch_deadline,
                skill_code="dispatch",
                quantity=sum(sum(line.outstanding for line in order.lines) for order in run_orders),
                depends_on=sort_keys[-1] if sort_keys else None,
                priority=TaskPriority.CRITICAL,
            )
        )

    return units


# --------------------------------------------------------------------------
# Handing the work out
# --------------------------------------------------------------------------


def _probe_task(unit: WorkUnit, session: Session, window_end: datetime, now: datetime) -> Task:
    """A throwaway Task so the real guardrails can judge this unit.

    Not added to the session. The rules only read attributes, so building one
    here means the planner is held to exactly the same skill, hours, rest and
    clash checks as every other decision -- rather than a second, looser copy
    written for planning.
    """
    skill = None
    if unit.skill_code:
        from app.models.employee import Skill

        skill = session.scalar(select(Skill).where(Skill.code == unit.skill_code))
    probe = Task(
        title=unit.title,
        order_id=unit.order_id,
        required_skill_id=skill.id if skill else None,
        min_proficiency=unit.min_proficiency,
        starts_at=now,
        due_at=window_end,
        estimated_minutes=int(math.ceil(unit.minutes)),
        priority=unit.priority,
        status=TaskStatus.PENDING,
        stage=unit.stage,
        quantity=unit.quantity,
    )
    probe.required_skill = skill
    probe.order = None
    return probe


def plan_day(
    session: Session, settings: Settings, now: datetime, *, notify_snapshot: bool = True
) -> DayPlan:
    """Work out the evening. Changes nothing."""
    cutoff = _cutoff(now, settings)
    units = build_work(session, settings, now)
    plan = DayPlan(generated_at=now, cutoff=cutoff)

    horizon_end = max([u.deadline for u in units], default=cutoff) + timedelta(hours=1)
    snapshot = load_snapshot(session, settings, now, horizon_end=horizon_end)

    # Drop the planner's own untouched work before deciding anything. Without
    # this, a re-plan runs against a board still full of its previous output,
    # finds everybody permanently busy, and concludes the evening is doomed.
    # The rule matches what commit_plan replaces, exactly: the agent may
    # reconsider its own future, never work the floor has started or a person
    # put up by hand.
    for facts in snapshot.all_facts():
        facts.live_tasks = [task for task in facts.live_tasks if not _is_planner_draft(task)]

    # When each person next comes free, and when each unit's upstream finishes.
    free_at: dict[int, datetime] = {facts.employee.id: now for facts in snapshot.all_facts()}
    finished: dict[str, datetime] = {}
    placed_by_key: dict[str, Placement] = {}

    # Deadline first, then pipeline order, so the tightest van is served first
    # and a stage is never scheduled before the one it waits on.
    for unit in sorted(units, key=lambda u: (u.deadline, Stage(u.stage).order, u.key)):
        ready = now
        if unit.depends_on:
            upstream = finished.get(unit.depends_on)
            if upstream is None:
                # Its upstream could not be placed; this cannot be either.
                placement = Placement(
                    unit=unit,
                    reason=f"waiting on {unit.depends_on}, which has nobody",
                    late_by=(now - unit.deadline).total_seconds() / 60,
                )
                plan.placements.append(placement)
                placed_by_key[unit.key] = placement
                continue
            ready = max(ready, upstream)

        placement = _place(unit, ready, snapshot, session, settings, now, free_at)
        plan.placements.append(placement)
        placed_by_key[unit.key] = placement
        if placement.placed:
            finished[unit.key] = placement.ends_at
            free_at[placement.employee_id] = placement.ends_at

    plan.runs = _summarise_runs(session, settings, plan, now)
    return plan


def _place(
    unit: WorkUnit,
    ready: datetime,
    snapshot: WorkforceSnapshot,
    session: Session,
    settings: Settings,
    now: datetime,
    free_at: dict[int, datetime],
) -> Placement:
    """Give one unit to the person who can finish it soonest.

    Earliest finish rather than best fit, because the only thing that matters
    here is whether the van leaves on time. Score breaks ties -- among people
    who would finish at the same moment, the better-suited one gets it.
    """
    # The window put to the guardrails is the work itself, not everything
    # between now and the deadline. Asking about the wider window made every
    # packer look permanently busy the moment they were given their first job.
    duration = timedelta(minutes=unit.minutes)
    probe = _probe_task(unit, session, ready + duration, ready)
    ranked = ranking.rank_candidates(
        snapshot,
        probe,
        window_start=ready,
        window_end=ready + duration,
        critical=True,
    )
    eligible = [c for c in ranked if c.eligible and c.on_shift]

    best: Placement | None = None
    best_score = -1.0
    for candidate in eligible:
        # Sequencing within this plan is the planner's own bookkeeping, so it
        # comes from when the person was last given something, not from the
        # clash rule. Work already on the board is a different matter, and is
        # checked against the slot they would really occupy.
        starts = max(ready, free_at.get(candidate.employee_id, now))
        ends = starts + duration
        facts = snapshot.get(candidate.employee_id)
        if facts is not None:
            clashes = facts.conflicting_tasks(starts, ends)
            if any(rank >= probe.priority_rank for _, rank in clashes):
                continue

        better = best is None or ends < best.ends_at
        tie_break = best is not None and ends == best.ends_at and candidate.score > best_score
        if better or tie_break:
            best_score = candidate.score
            best = Placement(
                unit=unit,
                employee_id=candidate.employee_id,
                employee_name=candidate.employee_name,
                starts_at=starts,
                ends_at=ends,
                late_by=(ends - unit.deadline).total_seconds() / 60,
                reason=(
                    f"{candidate.employee_name} is free at "
                    f"{starts:%H:%M} and finishes at {ends:%H:%M}"
                ),
                candidates=ranked,
            )

    if best is not None:
        if best.late_by > 0:
            best.reason += f" -- {best.late_by:.0f} min past the deadline"
        return best

    blockers = summarise_blockers(ranked)
    return Placement(
        unit=unit,
        reason="nobody on shift can take it: " + "; ".join(blockers)
        if blockers
        else "nobody on shift can take it",
        late_by=(ready - unit.deadline).total_seconds() / 60,
        candidates=ranked,
    )


def _summarise_runs(
    session: Session, settings: Settings, plan: DayPlan, now: datetime
) -> list[RunPlan]:
    """Per-van verdict: ready when, and if not, short by how much."""
    runs: dict[str, RunPlan] = {}
    for placement in plan.placements:
        run = placement.unit.run
        if run not in runs:
            departs = max(p.unit.deadline for p in plan.placements if p.unit.run == run)
            stops = sorted(
                {d.name for d in session.scalars(select(Destination).where(Destination.run == run))}
            )
            runs[run] = RunPlan(run=run, departs_at=departs, stops=stops, units=0)
        entry = runs[run]
        if placement.unit.stage == Stage.DISPATCH:
            entry.units = placement.unit.quantity
        if placement.placed:
            entry.ready_at = max(entry.ready_at or placement.ends_at, placement.ends_at)
        else:
            entry.blockers.append(f"{placement.unit.title}: {placement.reason}")
        entry.short_by = max(entry.short_by, placement.late_by)

    return sorted(runs.values(), key=lambda r: r.departs_at)


# --------------------------------------------------------------------------
# Making it true
# --------------------------------------------------------------------------


def commit_plan(session: Session, plan: DayPlan, settings: Settings, now: datetime) -> list[Task]:
    """Write the plan to the board, replacing the planner's previous work.

    Only the planner's own unstarted tasks are cleared. Anything a person
    added by hand, and anything already under way, survives a re-plan -- the
    agent is allowed to change its mind about the future, not to rewrite what
    the floor has already done.
    """
    session.execute(
        delete(Task).where(
            Task.planned_by == PLANNER,
            Task.status == TaskStatus.PENDING,
            Task.done_quantity == 0,
        )
    )
    session.flush()

    created: dict[str, Task] = {}
    for placement in plan.placements:
        unit = placement.unit
        task = Task(
            title=unit.title,
            station=Stage(unit.stage).label,
            order_id=unit.order_id,
            assignee_id=placement.employee_id,
            required_skill_id=_skill_id(session, unit.skill_code),
            min_proficiency=unit.min_proficiency,
            starts_at=placement.starts_at or now,
            due_at=unit.deadline,
            estimated_minutes=int(math.ceil(unit.minutes)),
            status=TaskStatus.PENDING,
            priority=unit.priority,
            stage=unit.stage,
            run=unit.run,
            product_id=unit.product_id,
            quantity=unit.quantity,
            planned_by=PLANNER,
        )
        session.add(task)
        session.flush()
        created[unit.key] = task

    # Second pass: the dependency ids only exist once every row does.
    for placement in plan.placements:
        unit = placement.unit
        if unit.depends_on and unit.depends_on in created:
            created[unit.key].depends_on_id = created[unit.depends_on].id
    session.flush()
    return list(created.values())


def _skill_id(session: Session, code: str | None) -> int | None:
    if not code:
        return None
    from app.models.employee import Skill

    skill = session.scalar(select(Skill).where(Skill.code == code))
    return skill.id if skill else None


def plan_and_commit(
    session: Session, settings: Settings, now: datetime, *, trigger: str = "plan_day"
) -> tuple[DayPlan, list[Task]]:
    """Plan the evening and put it on the board, with a record of why."""
    from app.engine.journal import record_decision

    plan = plan_day(session, settings, now)
    tasks = commit_plan(session, plan, settings, now)

    missed = plan.missed_runs
    summary = (
        f"Planned {len(tasks)} jobs across {len(plan.runs)} runs, "
        f"{round(plan.total_minutes)} minutes of work"
    )
    if missed:
        summary += f" -- {len(missed)} run(s) will not make it: " + ", ".join(
            f"{r.run} short by {r.short_by:.0f} min" for r in missed
        )
    record_decision(
        session,
        now=now,
        action=DecisionAction.AUTO_REASSIGNED if not missed else DecisionAction.ESCALATED,
        trigger=trigger,
        summary=summary,
        subject_type="day_plan",
        details=plan.as_dict(settings),
    )
    session.flush()
    return plan, tasks
