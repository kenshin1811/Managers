"""Three runs of the decision flow, printed as a trace you can read.

    python -m app.sim.scenario

Every run is a fresh in-memory database, a frozen clock and a dry-run
notifier, so nothing is sent and nothing persists.  What is printed is the
real output of the real engine: the same functions the API calls.
"""

from __future__ import annotations

import textwrap

from sqlalchemy.orm import Session, sessionmaker

from app.clock import FrozenClock, to_local
from app.config import Settings
from app.db import build_engine, create_all
from app.engine import coverage as coverage_engine
from app.engine import reassignment
from app.engine.types import ReassignmentPlan, TaskPlan
from app.models.coverage import CoverageOffer, CoverageRequest
from app.models.enums import LeaveStatus, LeaveType
from app.models.leave import LeaveRequest
from app.models.task import Task
from app.notifications import ConsoleNotifier
from app.sim.seed import World, at, seed_kitchen

WIDTH = 78
RULE = "=" * WIDTH
THIN = "-" * WIDTH


def _settings() -> Settings:
    return Settings(
        business_tz="America/New_York",
        dry_run=True,
        database_url="sqlite://",
        public_base_url="https://managers.example.test",
        coverage_link_secret="simulation-only",
    )


def _world(on_shift_packer: bool) -> tuple[Session, World, Settings]:
    settings = _settings()
    engine = build_engine("sqlite://")
    create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    world = seed_kitchen(session, on_shift_packer=on_shift_packer)
    return session, world, settings


def _t(moment, settings: Settings) -> str:
    return f"{to_local(moment, settings.business_tz):%H:%M}" if moment else "-"


def _heading(number: int, title: str, blurb: str) -> None:
    print(f"\n{RULE}\nSCENARIO {number}: {title}\n{RULE}")
    print(textwrap.fill(blurb, WIDTH))


def _print_impact(plan: ReassignmentPlan, settings: Settings) -> None:
    print(
        f"\n  {plan.employee_name} requests leave "
        f"{_t(plan.leave_starts_at, settings)}-{_t(plan.leave_ends_at, settings)} local."
    )
    if not plan.task_plans:
        print("  Nothing of theirs overlaps the leave. No action needed.")
        return
    print(f"  {len(plan.task_plans)} job(s) collide with it:\n")
    for task_plan in plan.task_plans:
        impact = task_plan.impact
        flag = "CRITICAL" if impact.critical else "routine "
        order = (
            f"order {impact.order_code}, driver {_t(impact.pickup_at, settings)}"
            if impact.order_code
            else "no order"
        )
        print(
            f"    [{flag}] {impact.title:<22} "
            f"{_t(impact.starts_at, settings)}-{_t(impact.due_at, settings)}  ({order})"
        )


def _print_candidates(task_plan: TaskPlan, settings: Settings, limit: int = 4) -> None:
    print(f"\n  > {task_plan.impact.title}")
    eligible = [c for c in task_plan.candidates if c.eligible]
    rejected = [c for c in task_plan.candidates if not c.eligible]

    if eligible:
        print("    eligible:")
        for candidate in eligible[:limit]:
            where = "on shift" if candidate.on_shift else "off shift"
            parts = ", ".join(
                f"{name} {value:+.0f}" for name, value in candidate.components.items() if value
            )
            print(f"      {candidate.score:6.1f}  {candidate.employee_name:<16} {where:<9} {parts}")
    else:
        print("    eligible: nobody")

    if rejected:
        # Every rejection is printed, not just the first few: the whole point
        # of the trace is answering "why not them?"
        print("    ruled out:")
        for candidate in rejected:
            blocker = candidate.blocking_rules[0]
            print(f"      {'':6}  {candidate.employee_name:<16} {blocker.rule}: {blocker.reason}")
    print(f"    ACTION: {task_plan.action} - {task_plan.reason}")


def _print_messages(notifier: ConsoleNotifier, title: str = "Messages that would be sent") -> None:
    if not notifier.sent:
        print(f"\n  {title}: none")
        return
    print(f"\n  {title}:")
    for message in notifier.sent:
        for line in textwrap.wrap(
            f"[{message.kind}] -> {message.channel}: {message.text}",
            WIDTH - 6,
            subsequent_indent="        ",
        ):
            print(f"    {line}")
        # An escalation carries the reasons in its blocks, and those reasons
        # are the entire value of the message to whoever picks it up.
        for detail in _block_lines(message):
            print(f"        {detail}")


def _block_lines(message) -> list[str]:
    if message.kind != "escalation":
        return []
    lines: list[str] = []
    for block in (message.blocks or [])[1:]:
        text = (block.get("text") or {}).get("text", "")
        for line in text.splitlines():
            if line.startswith("- "):
                lines.append(line)
    return lines


def _print_board(session: Session, world: World, settings: Settings) -> None:
    print("\n  Job board now:")
    names = {e.id: e.full_name for e in world.employees.values()}
    for task in session.query(Task).order_by(Task.starts_at).all():
        if task.status == "done":
            continue
        who = names.get(task.assignee_id, "UNASSIGNED")
        print(
            f"    {_t(task.starts_at, settings)}-{_t(task.due_at, settings)}  "
            f"{task.title:<26} {who}"
        )


def _file_leave(session: Session, world: World, settings: Settings, now) -> LeaveRequest:
    leave = LeaveRequest(
        employee_id=world.employee_id("mai"),
        leave_type=LeaveType.EMERGENCY,
        reason="Family emergency - has to leave now",
        starts_at=at(14, 30),
        ends_at=at(18, 0),
        status=LeaveStatus.PENDING_COVERAGE,
        created_at=now,
    )
    session.add(leave)
    session.flush()
    return leave


# --------------------------------------------------------------------------


def scenario_one() -> None:
    _heading(
        1,
        "somebody on shift can take it",
        "Mai is packing order 1043. The driver is due at 14:50. At 14:05 she has "
        "a family emergency and has to leave at 14:30. Somebody already at work "
        "is trained to pack, so nobody's day off gets interrupted.",
    )
    session, world, settings = _world(on_shift_packer=True)
    notifier = ConsoleNotifier(echo=False)
    clock = FrozenClock(at(14, 5))

    leave = _file_leave(session, world, settings, clock.now())
    plan = reassignment.plan_for_leave(session, leave, settings, clock.now())
    _print_impact(plan, settings)
    print(f"\n{THIN}\n  Candidates considered (nothing has happened yet)\n{THIN}")
    for task_plan in plan.task_plans:
        _print_candidates(task_plan, settings)

    reassignment.execute_plan(session, plan, notifier, settings, clock.now())
    session.commit()

    _print_board(session, world, settings)
    _print_messages(notifier)
    session.refresh(leave)
    print(f"\n  Leave request: {leave.status} (decided by {leave.decided_by})")
    session.close()


def scenario_two() -> None:
    _heading(
        2,
        "nobody on shift qualifies, so the system rings round",
        "Same emergency, but today nobody on shift is signed off on packing. "
        "The engine asks off-shift staff in order of fit, one of them says yes, "
        "and the rest are stood down before they reply.",
    )
    session, world, settings = _world(on_shift_packer=False)
    notifier = ConsoleNotifier(echo=False)
    clock = FrozenClock(at(14, 5))

    leave = _file_leave(session, world, settings, clock.now())
    plan = reassignment.plan_for_leave(session, leave, settings, clock.now())
    _print_impact(plan, settings)
    print(f"\n{THIN}\n  Candidates considered\n{THIN}")
    for task_plan in plan.task_plans:
        _print_candidates(task_plan, settings)

    reassignment.execute_plan(session, plan, notifier, settings, clock.now())
    session.commit()
    _print_messages(notifier, "Cover requests going out")

    request = session.query(CoverageRequest).order_by(CoverageRequest.id.desc()).first()
    print(f"\n  Cover request {request.id} open until {_t(request.expires_at, settings)} local.")

    notifier.clear()
    clock.advance(minutes=3)
    luis_offer = (
        session.query(CoverageOffer)
        .filter(
            CoverageOffer.coverage_request_id == request.id,
            CoverageOffer.employee_id == world.employee_id("luis"),
        )
        .one()
    )
    print(f"\n  {_t(clock.now(), settings)} - Luis taps 'I can cover'.")
    outcome = coverage_engine.accept_offer(session, luis_offer, notifier, settings, clock.now())
    print(f"  -> {outcome.status}: {outcome.message}")

    _print_board(session, world, settings)
    _print_messages(notifier, "Follow-up messages")
    session.refresh(leave)
    print(f"\n  Leave request: {leave.status} (decided by {leave.decided_by})")
    session.close()


def scenario_three() -> None:
    _heading(
        3,
        "everybody declines and the clock runs out",
        "Same again, but both off-shift packers say no. The engine looks for "
        "anyone it has not already asked, finds that the only remaining packer "
        "would blow through the weekly hours limit, and stops rather than "
        "breaking the rule. A manager gets the whole trace.",
    )
    session, world, settings = _world(on_shift_packer=False)
    notifier = ConsoleNotifier(echo=False)
    clock = FrozenClock(at(14, 5))

    leave = _file_leave(session, world, settings, clock.now())
    plan, _ = reassignment.handle_leave_request(session, leave, notifier, settings, clock.now())
    session.commit()
    _print_impact(plan, settings)

    request = session.query(CoverageRequest).order_by(CoverageRequest.id.desc()).first()
    asked = [offer.employee.full_name for offer in request.offers]
    print(f"\n  Asked: {', '.join(asked)}")

    notifier.clear()
    for key, minutes in (("luis", 2), ("sam", 4)):
        clock.advance(minutes=minutes)
        offer = (
            session.query(CoverageOffer)
            .filter(
                CoverageOffer.coverage_request_id == request.id,
                CoverageOffer.employee_id == world.employee_id(key),
            )
            .one()
        )
        outcome = coverage_engine.decline_offer(session, offer, notifier, settings, clock.now())
        print(
            f"  {_t(clock.now(), settings)} - {offer.employee.full_name} declines "
            f"-> {outcome.status}"
        )

    session.refresh(request)
    print(f"\n  Cover request {request.id} is now: {request.status}")
    _print_messages(notifier, "What the manager receives")
    _print_board(session, world, settings)

    session.refresh(leave)
    print(f"\n  Leave request: {leave.status}")
    print(
        textwrap.fill(
            "  Note what did not happen: the system did not quietly assign Tomas "
            "and push him past 48 hours, and it did not approve the leave while "
            "the order was uncovered. It stopped and said so.",
            WIDTH,
        )
    )
    session.close()


def main() -> None:
    print(RULE)
    print("Managers - automated employee management, simulated".center(WIDTH))
    print(RULE)
    print(
        textwrap.fill(
            "Dry run: no message leaves the process, and each scenario uses its own "
            "in-memory database. Times shown are local (America/New_York).",
            WIDTH,
        )
    )
    scenario_one()
    scenario_two()
    scenario_three()
    print(f"\n{RULE}\nDone. Nothing was sent and nothing was saved.\n{RULE}")


if __name__ == "__main__":
    main()
