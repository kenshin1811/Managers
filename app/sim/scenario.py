"""Four runs of the decision flow, printed as a trace you can read.

    python -m app.sim.scenario

Every run is a fresh in-memory database, a frozen clock and a dry-run
notifier, so nothing is sent and nothing persists.  What is printed is the
real output of the real engine: the same functions the API calls.
"""

from __future__ import annotations

import re
import textwrap
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clock import FrozenClock, to_local
from app.config import Settings
from app.db import build_engine, create_all
from app.engine import commands as command_engine
from app.engine import coverage as coverage_engine
from app.engine import planning, reassignment
from app.engine.types import ReassignmentPlan, TaskPlan
from app.models.coverage import CoverageOffer, CoverageRequest
from app.models.enums import LeaveStatus, LeaveType
from app.models.leave import LeaveRequest
from app.models.task import Task
from app.notifications import ConsoleNotifier
from app.sim.seed import ANCHOR, World, at, seed_factory

WIDTH = 78
RULE = "=" * WIDTH
THIN = "-" * WIDTH

BUSINESS_TZ = "Australia/Melbourne"


def _settings() -> Settings:
    return Settings(
        business_tz=BUSINESS_TZ,
        dry_run=True,
        database_url="sqlite://",
        public_base_url="https://managers.example.test",
        coverage_link_secret="simulation-only",
    )


def _world(full_team: bool) -> tuple[Session, World, Settings]:
    settings = _settings()
    engine = build_engine("sqlite://")
    create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    world = seed_factory(session, full_team=full_team)
    return session, world, settings


def _planned(full_team: bool) -> tuple[Session, World, Settings, FrozenClock]:
    """A seeded factory with the agent's plan already on the board."""
    session, world, settings = _world(full_team)
    clock = FrozenClock(at(*ANCHOR))
    planning.plan_and_commit(session, settings, clock.now(), trigger="scenario")
    session.commit()
    return session, world, settings, clock


def _t(moment, settings: Settings) -> str:
    return f"{to_local(moment, settings.business_tz):%H:%M}" if moment else "-"


def _heading(number: int, title: str, blurb: str) -> None:
    print(f"\n{RULE}\nSCENARIO {number}: {title}\n{RULE}")
    print(textwrap.fill(blurb, WIDTH))


def _print_runs(plan: planning.DayPlan, settings: Settings) -> None:
    print("\n  The vans:")
    print(f"    {'run':<10} {'leaves':<8} {'ready':<8} {'status':<9} stops")
    for run in plan.runs:
        print(
            f"    {run.run:<10} {_t(run.departs_at, settings):<8} "
            f"{_t(run.ready_at, settings):<8} {run.status:<9} {len(run.stops)}"
        )


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
        print(
            f"    [{flag}] {impact.title:<40} "
            f"due {_t(impact.due_at, settings)}"
            + (f", van {_t(impact.pickup_at, settings)}" if impact.pickup_at else "")
        )


def _print_candidates(task_plan: TaskPlan, settings: Settings, limit: int = 4) -> None:
    print(f"\n  {task_plan.impact.title}")
    print(f"    {'who':<12} {'fit':>6}  {'on shift':<9} why not")
    for candidate in task_plan.candidates[:limit]:
        if candidate.eligible:
            why = "-"
        else:
            why = "; ".join(rule.reason for rule in candidate.blocking_rules)
        print(
            f"    {candidate.employee_name:<12} {candidate.score:>6.1f}  "
            f"{'yes' if candidate.on_shift else 'no':<9} {why}"
        )
    if len(task_plan.candidates) > limit:
        print(f"    ... and {len(task_plan.candidates) - limit} more")
    print(f"    -> {task_plan.action}: {task_plan.reason}")


def _print_messages(notifier: ConsoleNotifier, title: str = "Messages that would be sent") -> None:
    if not notifier.sent:
        print("\n  No messages.")
        return
    print(f"\n{THIN}\n  {title}\n{THIN}")
    for message in notifier.sent:
        urgent = " [URGENT]" if message.meta.get("urgent") else ""
        print(f"\n  to {message.channel}  ({message.kind}){urgent}")
        for line in _block_lines(message):
            print(f"    {line}")


def _block_lines(message) -> list[str]:
    """The message as a reader would see it, blocks included.

    The plain-text field is the lock-screen fallback and deliberately short;
    an escalation's useful half -- which runs are short, and who could come in
    -- lives in the blocks, so a trace that printed only the text would leave
    out the part a manager acts on.
    """
    paragraphs = [message.text]
    seen = {_plain(message.text)}
    for block in message.blocks or []:
        for value in _texts(block):
            # The first block usually restates the fallback text with emoji and
            # bold markers around it; printing both is just noise.
            if value and _plain(value) not in seen:
                seen.add(_plain(value))
                paragraphs.append(value)

    lines: list[str] = []
    for paragraph in paragraphs:
        for part in paragraph.split("\n"):
            if not part.strip():
                continue
            lines.extend(textwrap.wrap(part, WIDTH - 6) or [""])
    return lines


def _plain(text: str) -> str:
    """Slack markup removed, for spotting two renderings of the same sentence."""
    return re.sub(r"[*_`]|:[a-z_]+:", "", text).strip().lower()


def _texts(node) -> list[str]:
    """Every rendered string inside a Slack block, in order."""
    if isinstance(node, dict):
        if node.get("type") in {"mrkdwn", "plain_text"} and isinstance(node.get("text"), str):
            return [node["text"]]
        found: list[str] = []
        for value in node.values():
            found.extend(_texts(value))
        return found
    if isinstance(node, list):
        found = []
        for value in node:
            found.extend(_texts(value))
        return found
    return []


def _print_board(session: Session, world: World, settings: Settings, limit: int = 12) -> None:
    print("\n  Job board now (first rows):")
    names = {e.id: e.full_name for e in world.employees.values()}
    rows = session.scalars(select(Task).order_by(Task.starts_at, Task.id)).all()
    live = [task for task in rows if task.status != "done"]
    for task in live[:limit]:
        who = names.get(task.assignee_id, "UNASSIGNED")
        print(
            f"    {_t(task.starts_at, settings)}  {str(task.stage or '-'):<9} "
            f"{task.title[:44]:<44} {who}"
        )
    if len(live) > limit:
        print(f"    ... and {len(live) - limit} more")


def _file_leave(session: Session, world: World, clock: FrozenClock, key: str) -> LeaveRequest:
    now = clock.now()
    leave = LeaveRequest(
        employee_id=world.employee_id(key),
        leave_type=LeaveType.EMERGENCY,
        reason="Family emergency - has to step out",
        starts_at=now + timedelta(minutes=10),
        ends_at=now + timedelta(minutes=40),
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
        "the agent plans the evening",
        "Friday, 16:30. Nine orders, about 2,250 units, five vans between 17:30 "
        "and 20:45, and three packers on the floor. Nobody has told the agent "
        "who should do what. It reads the order book, works out what is still "
        "outstanding, and lays the evening out stage by stage: pull from the "
        "freezer, pack and label, sort to orders, load the van.",
    )
    session, world, settings = _world(full_team=True)
    clock = FrozenClock(at(*ANCHOR))

    work = planning.build_work(session, settings, clock.now())
    by_stage: dict[str, list] = {}
    for unit in work:
        by_stage.setdefault(str(unit.stage), []).append(unit)
    print("\n  What the evening needs:")
    for stage in ("retrieve", "pack", "sort", "dispatch"):
        units = by_stage.get(stage, [])
        minutes = sum(u.minutes for u in units)
        print(f"    {stage:<10} {len(units):>3} job(s), {minutes:>6.0f} min")
    print(f"    {'total':<10} {len(work):>3} job(s), {sum(u.minutes for u in work):>6.0f} min")

    plan, tasks = planning.plan_and_commit(session, settings, clock.now(), trigger="scenario")
    session.commit()
    _print_runs(plan, settings)
    _print_board(session, world, settings)
    print(
        textwrap.fill(
            f"  {len(tasks)} jobs on the board, and every van makes it. Note what "
            "the constraint is: not the total hours -- there are plenty -- but "
            "the individual departures. No amount of spare capacity at 20:00 "
            "helps with an 18:00 van.",
            WIDTH,
        )
    )
    session.close()


def scenario_two() -> None:
    _heading(
        2,
        "the floor manager says something",
        "A late order comes in over the phone and the floor manager calls it "
        "across the room. The same sentence the microphone would hear goes "
        "through the same parser, and the agent says back whether the van still "
        "makes it.",
    )
    session, world, settings, clock = _planned(full_team=True)
    notifier = ConsoleNotifier(echo=False)

    for said in (
        "what's at risk",
        "add 240 jam donuts for Fitzroy",
        "mark 120 jam donuts packed",
        "what's at risk",
        "put the thing in the other thing",
    ):
        result = command_engine.handle(session, settings, clock.now(), said, notifier)
        session.commit()
        mark = "ok " if result.ok else "no "
        print(f'\n  "{said}"')
        for line in textwrap.wrap(result.speech, WIDTH - 8):
            print(f"    {mark}{line}")
            mark = "   "
    print(
        textwrap.fill(
            "  The last one is the important one. A console that guesses is worse "
            "than one that says it did not understand, because a wrong guess "
            "moves real donuts to the wrong shop.",
            WIDTH,
        )
    )
    session.close()


def scenario_three() -> None:
    _heading(
        3,
        "a packer goes home and the evening is rebuilt",
        "Shaleen is sent home mid-shift. Losing a third of the floor does not "
        "mean twenty separate people need pestering about twenty separate jobs: "
        "the agent re-plans first, and only escalates what the new plan cannot "
        "absorb -- once, naming who could come in.",
    )
    session, world, settings, clock = _planned(full_team=True)
    notifier = ConsoleNotifier(echo=False)

    before = planning.plan_day(session, settings, clock.now())
    _print_runs(before, settings)

    result = command_engine.handle(session, settings, clock.now(), "Shaleen is off sick", notifier)
    session.commit()
    print(f"\n  -> {result.speech}")
    after = planning.plan_day(session, settings, clock.now())
    _print_runs(after, settings)
    _print_messages(notifier, "What the manager receives")

    notifier.clear()
    back = command_engine.handle(session, settings, clock.now(), "Valentino is back", notifier)
    session.commit()
    print(f'\n  "Valentino is back"\n  -> {back.speech}')
    _print_runs(planning.plan_day(session, settings, clock.now()), settings)
    session.close()


def scenario_four() -> None:
    _heading(
        4,
        "somebody steps out and the system rings round",
        "A thinner roster, and Jasoo has to step out for half an hour. One job "
        "-- the banana bread for the 17:30 internal van -- has nobody on shift "
        "free to take it, so the agent asks the people who are off today, in "
        "order of fit. Then it does it again with everybody saying no.",
    )
    session, world, settings, clock = _planned(full_team=False)
    notifier = ConsoleNotifier(echo=False)

    leave = _file_leave(session, world, clock, "jasoo")
    plan = reassignment.plan_for_leave(session, leave, settings, clock.now())
    _print_impact(plan, settings)
    print(f"\n{THIN}\n  Candidates considered (nothing has happened yet)\n{THIN}")
    for task_plan in plan.task_plans:
        _print_candidates(task_plan, settings, limit=6)

    reassignment.execute_plan(session, plan, notifier, settings, clock.now())
    session.commit()
    _print_messages(notifier, "Cover requests going out")

    request = session.scalars(select(CoverageRequest).order_by(CoverageRequest.id.desc())).first()
    print(f"\n  Cover request {request.id} open until {_t(request.expires_at, settings)} local.")

    notifier.clear()
    clock.advance(minutes=3)
    first = sorted(request.offers, key=lambda o: o.rank)[0]
    print(f"\n  {_t(clock.now(), settings)} - {first.employee.full_name} taps 'I can cover'.")
    outcome = coverage_engine.accept_offer(session, first, notifier, settings, clock.now())
    print(f"  -> {outcome.status}: {outcome.message}")
    _print_messages(notifier, "Follow-up messages")
    session.refresh(leave)
    print(f"\n  Leave request: {leave.status} (decided by {leave.decided_by})")
    session.close()

    # --- and now the version where nobody can ------------------------------
    print(f"\n{THIN}\n  The same evening, with everybody saying no\n{THIN}")
    session, world, settings, clock = _planned(full_team=False)
    notifier = ConsoleNotifier(echo=False)
    leave = _file_leave(session, world, clock, "jasoo")
    reassignment.handle_leave_request(session, leave, notifier, settings, clock.now())
    session.commit()

    request = session.scalars(select(CoverageRequest).order_by(CoverageRequest.id.desc())).first()
    print(f"\n  Asked: {', '.join(o.employee.full_name for o in request.offers)}")

    notifier.clear()
    for offer in sorted(request.offers, key=lambda o: o.rank):
        clock.advance(minutes=2)
        live = session.get(CoverageOffer, offer.id)
        outcome = coverage_engine.decline_offer(session, live, notifier, settings, clock.now())
        print(
            f"  {_t(clock.now(), settings)} - {live.employee.full_name} declines "
            f"-> {outcome.status}"
        )

    session.refresh(request)
    print(f"\n  Cover request {request.id} is now: {request.status}")
    _print_messages(notifier, "What the manager receives")
    session.refresh(leave)
    print(f"\n  Leave request: {leave.status}")
    print(
        textwrap.fill(
            "  Note what did not happen: the system did not quietly assign Marlo "
            "and push him past 48 hours, and it did not approve the leave while "
            "the van was uncovered. It stopped and said so.",
            WIDTH,
        )
    )
    session.close()


def main() -> None:
    print(RULE)
    print("Managers - a donut factory's packing floor, simulated".center(WIDTH))
    print(RULE)
    print(
        textwrap.fill(
            "Dry run: no message leaves the process, and each scenario uses its own "
            f"in-memory database. Times shown are local ({BUSINESS_TZ}).",
            WIDTH,
        )
    )
    scenario_one()
    scenario_two()
    scenario_three()
    scenario_four()
    print(f"\n{RULE}\nDone. Nothing was sent and nothing was saved.\n{RULE}")


if __name__ == "__main__":
    main()
