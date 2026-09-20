"""Record what the real engine actually does, step by step.

    python -m app.sim.capture

The dashboard cannot be hosted anywhere the reader can reach, so instead we
run the real thing here and keep its output.  Every payload written by this
script came out of the same ``/api/dashboard`` endpoint the browser calls,
after the same engine functions the service runs in production -- it is a
recording, not a mock-up.  The spoken commands went through
``app.engine.commands`` exactly as the microphone's do.

What a recording cannot do is follow a path nobody recorded.  The replay page
says so plainly rather than letting anyone think they are driving a live
system.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import notifier_dep, now_dep, settings_dep
from app.clock import FrozenClock
from app.config import Settings
from app.db import build_engine, create_all, get_session
from app.engine import commands as command_engine
from app.engine import coverage as coverage_engine
from app.engine import planning, reassignment
from app.main import create_app
from app.models.coverage import CoverageOffer, CoverageRequest
from app.models.enums import CoverageStatus, LeaveStatus, LeaveType, OfferStatus
from app.models.leave import LeaveRequest
from app.models.state import SWEEP_HEARTBEAT
from app.notifications import ConsoleNotifier
from app.sim.seed import ANCHOR, World, at, seed_factory
from app.state import touch

OUTPUT = Path(__file__).resolve().parents[1] / "web" / "snapshots.json"

BUSINESS_TZ = "Australia/Melbourne"

#: Recorded on the simulation's own day rather than at whatever moment the
#: script happens to run. Anchoring to real time made the recording depend on
#: the hour it was made: run it outside the afternoon shift and the seeded
#: call-in staff fall outside their own availability, every candidate is ruled
#: out and the whole thing escalates. The replay page shifts these onto the
#: reader's clock, so the absolute values here never reach anybody's screen.
SIM_NOW = at(*ANCHOR)

#: How stale the heartbeat has to be for the agent to read as stopped.
STALLED_AFTER = timedelta(minutes=6)

#: The spoken instructions the walkthrough records, in the order a floor
#: manager would plausibly give them. Each one is a real trip through
#: ``commands.handle``; the page offers exactly these and nothing else.
ADD_ORDER = "add 240 jam donuts for Fitzroy"
SEND_HOME = "Shaleen is off sick"
CALL_IN = "Valentino is back"
ASK_STATUS = "what's at risk"

#: Jasoo steps out for half an hour. It collides with one job -- packing the
#: banana bread for the 17:30 internal van -- and on a two-packer evening
#: there is nobody on shift free to take it, which is the whole reason the
#: call-round exists.
LEAVE_IN = timedelta(minutes=10)
LEAVE_FOR = timedelta(minutes=30)


#: A recorded timestamp keeps only its UTC value. The replay page shifts
#: every stamp onto the reader's clock and re-renders the local strings from
#: scratch anyway, so carrying them here costs about two megabytes to be
#: thrown away at load.
STAMP_KEYS = {"utc", "local", "local_full"}


def thin(node: Any) -> Any:
    """Drop the rendered halves of every timestamp in a payload."""
    if isinstance(node, list):
        return [thin(item) for item in node]
    if isinstance(node, dict):
        if isinstance(node.get("utc"), str) and set(node) <= STAMP_KEYS:
            return {"utc": node["utc"]}
        return {key: thin(value) for key, value in node.items()}
    return node


def normalise(transcript: str) -> str:
    """The key a spoken phrase is filed under, so "Re-plan" finds "replan"."""
    return re.sub(r"[^a-z0-9]+", " ", transcript.lower()).strip()


@dataclass
class Recorder:
    """One throwaway service, wired to a clock we control."""

    session: Session
    client: TestClient
    clock: FrozenClock
    settings: Settings
    notifier: ConsoleNotifier
    world: World
    frames: dict[str, Any]
    me_frames: dict[str, Any] = field(default_factory=dict)
    last_message: str = ""
    leave_actions: list[str] = field(default_factory=list)

    def beat(self, age_seconds: int = 4) -> None:
        """Pretend the sweep ran ``age_seconds`` ago."""
        touch(self.session, SWEEP_HEARTBEAT, self.clock.now() - timedelta(seconds=age_seconds))
        self.session.commit()

    def say(self, transcript: str) -> Any:
        """Give one spoken instruction, through the path the microphone uses."""
        result = command_engine.handle(
            self.session,
            self.settings,
            self.clock.now(),
            transcript,
            self.notifier,
            source="voice",
        )
        self.session.commit()
        return result

    def snap(self, key: str, caption: str) -> dict[str, Any]:
        payload = thin(self.client.get("/api/dashboard").json())
        payload["_caption"] = caption
        self.frames[key] = payload
        # The same moment from each person's own side. Recording all of them
        # costs a few hundred kilobytes and means the walkthrough can switch
        # roles without a gap -- including the empty views, which are their own
        # kind of answer: Marlo was never asked, and his page shows it.
        self.me_frames[key] = {
            str(employee.id): thin(self.client.get(f"/api/me/{employee.id}").json())
            for employee in self.world.employees.values()
        }
        print(f"  {key:<26} {caption}")
        return payload


def _build(
    variant: str,
    anchor: datetime,
    frames: dict[str, Any],
    me_frames: dict[str, Any] | None = None,
) -> Recorder:
    settings = Settings(
        database_url="sqlite://",
        business_tz=BUSINESS_TZ,
        dry_run=True,
        coverage_link_secret="replay-recording",
        public_base_url="https://managers.example.test",
    )
    engine = build_engine("sqlite://")
    create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()
    clock = FrozenClock(anchor)
    notifier = ConsoleNotifier(echo=False)

    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[settings_dep] = lambda: settings
    app.dependency_overrides[notifier_dep] = lambda: notifier
    app.dependency_overrides[now_dep] = lambda: clock.now()

    world = seed_factory(
        session,
        full_team=(variant == "full_team"),
        anchor=anchor,
        tz=settings.business_tz,
    )
    # Seeding lays out the order book; the agent works out the evening. This
    # is the same call the service makes on a reset, not a shortcut around it.
    planning.plan_and_commit(session, settings, anchor, trigger="capture")
    session.commit()

    return Recorder(
        session=session,
        client=TestClient(app),
        clock=clock,
        settings=settings,
        notifier=notifier,
        world=world,
        frames=frames,
        me_frames=me_frames if me_frames is not None else {},
    )


# --------------------------------------------------------------------------
# The floor manager's console
# --------------------------------------------------------------------------

#: Every path through the console worth showing: the commands to give, the
#: frame it lands on, and what to say about it.
CONSOLE_PATHS: list[tuple[list[str], str, str]] = [
    ([], "floor.0", "Friday teatime. Three packers, five vans, and the evening fits."),
    (
        [ADD_ORDER],
        "floor.more",
        "A late order lands: 240 jam donuts for Fitzroy, on the six o'clock van.",
    ),
    (
        [SEND_HOME],
        "floor.off",
        "Shaleen goes home. The agent rebuilds the evening before asking anybody for anything.",
    ),
    (
        [SEND_HOME, CALL_IN],
        "floor.off.back",
        "Valentino is called in, and the evening is planned around him.",
    ),
    (
        [ADD_ORDER, SEND_HOME],
        "floor.more.off",
        "The late order and a packer short, together. This is where the vans start slipping.",
    ),
    (
        [ADD_ORDER, SEND_HOME, CALL_IN],
        "floor.more.off.back",
        "Valentino comes in to a heavier evening than the one he was rostered off.",
    ),
]


def _walk_console(
    anchor: datetime,
    frames: dict[str, Any],
    me_frames: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Record each command from each frame it can be given at.

    Rebuilding from scratch for every path is wasteful and completely worth
    it: each frame is then the genuine end state of that exact sequence of
    real engine calls, rather than something patched together afterwards.
    """
    landing = {tuple(path): key for path, key, _ in CONSOLE_PATHS}
    transitions: dict[str, dict[str, Any]] = {}

    for path, key, caption in CONSOLE_PATHS:
        rec = _build("full_team", anchor, frames, me_frames)
        rec.beat()
        for said in path:
            rec.say(said)
        rec.beat()
        rec.snap(key, caption)

        moves: dict[str, Any] = {}
        # A question rearranges nothing, so it answers from right here.
        status = command_engine.handle(
            rec.session, rec.settings, rec.clock.now(), ASK_STATUS, None, source="voice"
        )
        rec.session.rollback()
        moves[normalise(ASK_STATUS)] = {"next": key, "speech": status.speech, "ok": status.ok}

        for said in (ADD_ORDER, SEND_HOME, CALL_IN):
            target = landing.get((*path, said))
            if target is None:
                continue
            branch = _build("full_team", anchor, frames, me_frames)
            branch.beat()
            for earlier in path:
                branch.say(earlier)
            result = branch.say(said)
            moves[normalise(said)] = {
                "next": target,
                "speech": result.speech,
                "ok": result.ok,
            }
            branch.session.close()
        transitions[key] = moves
        rec.session.close()

    return transitions


# --------------------------------------------------------------------------
# Somebody asks for cover
# --------------------------------------------------------------------------


def _file_leave(rec: Recorder) -> LeaveRequest:
    now = rec.clock.now()
    leave = LeaveRequest(
        employee_id=rec.world.employee_id("jasoo"),
        leave_type=LeaveType.EMERGENCY,
        reason="Family emergency - has to step out",
        starts_at=now + LEAVE_IN,
        ends_at=now + LEAVE_IN + LEAVE_FOR,
        status=LeaveStatus.PENDING_COVERAGE,
        created_at=now,
    )
    rec.session.add(leave)
    rec.session.flush()
    plan, _ = reassignment.handle_leave_request(rec.session, leave, rec.notifier, rec.settings, now)
    rec.session.commit()
    rec.leave_actions = [str(p.action) for p in plan.task_plans]
    return leave


def _open_request(rec: Recorder) -> CoverageRequest:
    return rec.session.scalars(
        select(CoverageRequest).where(CoverageRequest.status == CoverageStatus.OPEN)
    ).one()


def _offer(rec: Recorder, request: CoverageRequest, key: str) -> CoverageOffer:
    return rec.session.scalars(
        select(CoverageOffer).where(
            CoverageOffer.coverage_request_id == request.id,
            CoverageOffer.employee_id == rec.world.employee_id(key),
        )
    ).one()


def _reply(rec: Recorder, request: CoverageRequest, key: str, accept: bool) -> None:
    rec.clock.advance(minutes=2)
    offer = _offer(rec, request, key)
    now = rec.clock.now()
    if accept:
        outcome = coverage_engine.accept_offer(rec.session, offer, rec.notifier, rec.settings, now)
    else:
        outcome = coverage_engine.decline_offer(rec.session, offer, rec.notifier, rec.settings, now)
    # Kept so the walkthrough can show what the engine actually said back,
    # rather than a plausible-sounding sentence written for the occasion.
    rec.last_message = outcome.message


def _pending(rec: Recorder, request_id: int) -> list[tuple[str, int]]:
    """``(seed key, employee id)`` for everyone still waiting to answer."""
    by_id = {rec.world.employee_id(k): k for k in ("shaleen", "valentino", "tavi", "marlo", "ken")}
    offers = rec.session.scalars(
        select(CoverageOffer).where(
            CoverageOffer.coverage_request_id == request_id,
            CoverageOffer.status == OfferStatus.SENT,
        )
    )
    return [(by_id[o.employee_id], o.employee_id) for o in offers if o.employee_id in by_id]


def _walk_call_in(
    anchor: datetime,
    frames: dict[str, Any],
    me_frames: dict[str, Any],
    replies: dict[str, Any],
    leave_actions: dict[str, list[str]],
) -> str:
    """Record every answer anyone could give, to the depth the offers allow."""

    def build(path: list[tuple[str, bool]]) -> tuple[Recorder, int]:
        rec = _build("short_team", anchor, frames, me_frames)
        rec.beat()
        _file_leave(rec)
        request = _open_request(rec)
        request_id = request.id
        for key, accept in path:
            _reply(rec, request, key, accept)
        rec.beat()
        return rec, request_id

    root_key = "short.1"
    rec, request_id = build([])
    leave_actions["short.0"] = rec.leave_actions
    rec.snap(root_key, "Jasoo steps out. Nobody on shift is free, so three people are asked.")

    frontier: list[tuple[str, list[tuple[str, bool]]]] = [(root_key, [])]
    while frontier:
        parent_key, path = frontier.pop(0)
        rec, request_id = build(path)
        options = _pending(rec, request_id)
        rec.session.close()
        if not options:
            continue
        replies.setdefault(parent_key, {})
        for seed_key, employee_id in options:
            for accept in (True, False):
                branch = path + [(seed_key, accept)]
                child_key = f"{parent_key}.{seed_key}-{'accept' if accept else 'decline'}"
                child, _ = build(branch)
                message = child.last_message
                name = child.world.employees[seed_key].full_name
                verb = "takes it" if accept else "cannot"
                child.snap(child_key, f"{name} {verb}.")
                child.session.close()
                action = "accept" if accept else "decline"
                replies[parent_key][f"{action}:{employee_id}"] = {
                    "next": child_key,
                    "message": message,
                }
                if not accept:
                    frontier.append((child_key, branch))
    return root_key


# --------------------------------------------------------------------------


def capture(anchor: datetime | None = None) -> dict[str, Any]:
    """Run every branch worth showing and keep the dashboard's view of each."""
    anchor = anchor or SIM_NOW
    frames: dict[str, Any] = {}
    me_frames: dict[str, Any] = {}
    replies: dict[str, Any] = {}
    leave_actions: dict[str, list[str]] = {}

    # --- the floor manager talks to the floor ------------------------------
    print("console:")
    transitions = _walk_console(anchor, frames, me_frames)

    # --- somebody has to step out ------------------------------------------
    print("call_in:")
    rec = _build("short_team", anchor, frames, me_frames)
    rec.beat()
    rec.snap("short.0", "Two packers tonight, and the evening is already tight.")
    vocabulary = rec.client.get("/api/console/vocabulary").json()
    roster = rec.client.get("/api/me/switchable").json()
    leave_employee_id = rec.world.employee_id("jasoo")
    rec.session.close()
    _walk_call_in(anchor, frames, me_frames, replies, leave_actions)

    # --- the agent itself has stopped --------------------------------------
    print("stalled:")
    rec = _build("full_team", anchor, frames, me_frames)
    rec.beat(age_seconds=int(STALLED_AFTER.total_seconds()))
    rec.snap("stalled", "What it looks like when the scheduler dies: the page says so.")
    rec.session.close()

    # The page offers exactly the phrases that were recorded. Anything else
    # would be a hint that does nothing when a reader says it.
    vocabulary["examples"] = [ADD_ORDER, SEND_HOME, CALL_IN, ASK_STATUS]

    return {
        "recorded_at": anchor.isoformat() + "Z",
        "business_tz": BUSINESS_TZ,
        "roster": roster,
        "vocabulary": vocabulary,
        # The walkthrough follows one person's leave; the employee page needs
        # to know whose, so it can offer the form to them and nobody else.
        "leave_employee_id": leave_employee_id,
        "me_frames": me_frames,
        "start": {"full_team": "floor.0", "short_team": "short.0"},
        "commands": transitions,
        "leave": {"short.0": "short.1"},
        "replies": replies,
        "leave_actions": leave_actions,
        "stalled": "stalled",
        "frames": frames,
    }


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    data = capture()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(data, indent=1, sort_keys=True))
    size = OUTPUT.stat().st_size
    print(f"\n{len(data['frames'])} frames -> {OUTPUT} ({size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
