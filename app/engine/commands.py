"""Understanding what the floor manager just said.

The browser does speech to text and sends the words here; parsing happens in
Python so it can be tested, and so typing a command and saying it take exactly
the same path. Nothing about this is a language model: it is a small grammar
over a vocabulary read from the database, which means it is predictable, it
works with no network, and when it does not understand something it says so
instead of guessing.

That last part matters more than breadth. A manager shouting over a packing
line wants to be told "I didn't catch a shop name" immediately, not to
discover at 18:05 that sixty jam donuts went to the wrong suburb.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import to_local
from app.config import Settings
from app.models.destination import Destination
from app.models.employee import Employee
from app.models.enums import LeaveStatus, LeaveType, OrderStatus
from app.models.leave import LeaveRequest
from app.models.order import Order, OrderLine
from app.models.product import Product

NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
    "dozen": 12,
}

#: Words that carry no meaning here but turn up in ordinary speech.
NOISE = {
    "please",
    "the",
    "a",
    "an",
    "of",
    "some",
    "more",
    "extra",
    "and",
    "can",
    "you",
    "we",
    "i",
    "just",
    "okay",
    "ok",
    "um",
    "uh",
}

STOP_AFTER_QUANTITY = {"for", "to", "at", "on", "going"}

#: Words a naive de-pluraliser would mangle.
KEEP_AS_IS = {"status", "is", "this", "his", "bus", "gas", "yes", "less", "across"}


@dataclass
class Command:
    kind: str
    transcript: str
    slots: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def understood(self) -> bool:
        return self.kind != "unknown" and not self.error


@dataclass
class CommandResult:
    ok: bool
    kind: str
    speech: str
    """One sentence, written to be read aloud or glanced at."""

    detail: dict[str, Any] = field(default_factory=dict)
    replanned: bool = False


# --------------------------------------------------------------------------
# Vocabulary, read from the database so it can never drift from the catalogue
# --------------------------------------------------------------------------


@dataclass
class Vocabulary:
    products: list[Product]
    destinations: list[Destination]
    employees: list[Employee]

    @classmethod
    def load(cls, session: Session) -> Vocabulary:
        return cls(
            products=list(session.scalars(select(Product))),
            destinations=list(session.scalars(select(Destination))),
            employees=list(session.scalars(select(Employee).where(Employee.active.is_(True)))),
        )


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w]


def _singular(word: str) -> str:
    """Crude de-pluralising, with the words it would wreck left alone.

    Stripping a trailing "s" turned "status" into "statu" and quietly broke
    the one command a manager is most likely to say.
    """
    if word in KEEP_AS_IS or len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("us", "is", "ss")):
        return word
    if word.endswith("es") and word[:-2].endswith(("sh", "ch", "x", "s")):
        return word[:-2]
    return word[:-1] if word.endswith("s") else word


def _tokens(text: str) -> list[str]:
    return [_singular(w) for w in _words(text) if w not in NOISE]


def parse_quantity(words: list[str]) -> tuple[int | None, int]:
    """Read a number from the front of ``words``.

    Returns the value and how many words it consumed. Handles digits and the
    spoken forms a person actually uses -- "sixty", "two hundred", "a dozen".
    """
    total = 0
    consumed = 0
    seen = False
    for word in words:
        if word.isdigit():
            if seen:
                break
            total = int(word)
            seen = True
            consumed += 1
            continue
        if word in NUMBER_WORDS:
            value = NUMBER_WORDS[word]
            if value == 100 and seen:
                total = max(1, total) * 100
            elif seen and total % 100 == 0 and value < 100:
                total += value
            elif seen and total < 100 and value < 100:
                total += value
            else:
                total = value
            seen = True
            consumed += 1
            continue
        break
    return (total if seen else None), consumed


def _score_match(tokens: list[str], candidate: str) -> float:
    """How well a phrase matches a catalogue entry, 0 to 1."""
    target = set(_tokens(candidate))
    if not target:
        return 0.0
    hits = sum(1 for token in tokens if token in target)
    if not hits:
        return 0.0
    # Reward covering the target, not just overlapping it: "muffin" alone
    # should not beat "blueberry muffin" for a blueberry muffin.
    return hits / len(target)


def match_product(tokens: list[str], vocabulary: Vocabulary) -> Product | None:
    best, best_score = None, 0.0
    for product in vocabulary.products:
        score = max(
            _score_match(tokens, product.name),
            _score_match(tokens, product.code.replace("_", " ")),
        )
        if score > best_score:
            best, best_score = product, score
    return best if best_score >= 0.5 else None


def match_destination(tokens: list[str], vocabulary: Vocabulary) -> Destination | None:
    best, best_score = None, 0.0
    for destination in vocabulary.destinations:
        score = max(
            _score_match(tokens, destination.suburb),
            _score_match(tokens, destination.name),
            _score_match(tokens, destination.code.replace("_", " ")),
        )
        if score > best_score:
            best, best_score = destination, score
    return best if best_score >= 0.5 else None


def match_employee(tokens: list[str], vocabulary: Vocabulary) -> Employee | None:
    for employee in vocabulary.employees:
        first = _singular(employee.full_name.split()[0].lower())
        if first in tokens:
            return employee
    return None


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

OFF_WORDS = {"off", "sick", "home", "leaving", "leave", "out", "away", "gone"}
BACK_WORDS = {"back", "in", "available", "return", "returning", "here"}
REPLAN_WORDS = {"replan", "plan", "reschedule", "redo"}
STATUS_WORDS = {"status", "risk", "late", "track", "doing", "behind", "ready"}
PACKED_WORDS = {"packed", "done", "finished", "complete", "completed"}


def parse(transcript: str, vocabulary: Vocabulary) -> Command:
    """Turn a sentence into an intent, or say plainly that it did not."""
    raw = transcript.strip()
    if not raw:
        return Command(kind="unknown", transcript=raw, error="I didn't catch that.")

    tokens = _tokens(raw)
    if not tokens:
        return Command(kind="unknown", transcript=raw, error="I didn't catch that.")

    employee = match_employee(tokens, vocabulary)
    token_set = set(tokens)

    # "Ken is off sick" / "Valentino is back"
    if employee is not None:
        if token_set & OFF_WORDS:
            return Command(kind="staff_off", transcript=raw, slots={"employee": employee})
        if token_set & BACK_WORDS:
            return Command(kind="staff_on", transcript=raw, slots={"employee": employee})

    # "mark sixty jam donuts packed"
    if token_set & PACKED_WORDS:
        quantity, consumed = parse_quantity([t for t in tokens if t not in {"mark", "record"}])
        rest = [t for t in tokens if t not in PACKED_WORDS and t not in {"mark", "record"}]
        product = match_product(rest[consumed:] or rest, vocabulary)
        destination = match_destination(rest, vocabulary)
        if product is None and destination is None:
            return Command(
                kind="unknown",
                transcript=raw,
                error="Packed what? Name a product or a shop.",
            )
        return Command(
            kind="mark_packed",
            transcript=raw,
            slots={"quantity": quantity, "product": product, "destination": destination},
        )

    if token_set & REPLAN_WORDS:
        return Command(kind="replan", transcript=raw)

    if token_set & STATUS_WORDS:
        return Command(kind="status", transcript=raw)

    # "add sixty jam donuts for Fitzroy"
    quantity, consumed = parse_quantity([t for t in tokens if t != "add"])
    if quantity is not None:
        body = [t for t in tokens if t != "add"][consumed:]
        split = len(body)
        for index, token in enumerate(body):
            if token in STOP_AFTER_QUANTITY:
                split = index
                break
        product = match_product(body[:split], vocabulary)
        destination = match_destination(body[split:] or body, vocabulary)
        if product is None:
            return Command(kind="unknown", transcript=raw, error="I didn't catch which product.")
        if destination is None:
            return Command(
                kind="unknown", transcript=raw, error="I didn't catch which shop or run."
            )
        return Command(
            kind="add_line",
            transcript=raw,
            slots={"quantity": quantity, "product": product, "destination": destination},
        )

    return Command(
        kind="unknown",
        transcript=raw,
        error='I didn\'t understand that. Try "add 60 jam donuts for Fitzroy".',
    )


# --------------------------------------------------------------------------
# Doing it
# --------------------------------------------------------------------------


def _local(moment: datetime, settings: Settings) -> str:
    return f"{to_local(moment, settings.business_tz):%H:%M}"


def _replan(session: Session, settings: Settings, now: datetime, trigger: str):
    from app.engine import planning

    plan, _ = planning.plan_and_commit(session, settings, now, trigger=trigger)
    return plan


def _run_verdict(plan, settings: Settings) -> str:
    missed = plan.missed_runs
    if not missed:
        # A run with nothing outstanding has no ready time, and an evening
        # where every run is like that is finished, not on schedule.
        ready = [r for r in plan.runs if r.ready_at is not None]
        if not ready:
            return "Nothing left to pack."
        last = max(ready, key=lambda r: r.ready_at)
        return (
            f"All {len(plan.runs)} runs on time, last one ready {_local(last.ready_at, settings)}."
        )
    worst = max(missed, key=lambda r: r.short_by)
    return f"{len(missed)} run(s) will miss: {worst.run} is short by {worst.short_by:.0f} minutes."


def execute(
    session: Session,
    settings: Settings,
    now: datetime,
    command: Command,
    notifier=None,
) -> CommandResult:
    if not command.understood:
        return CommandResult(
            ok=False, kind="unknown", speech=command.error or "I didn't understand that."
        )

    if command.kind == "add_line":
        return _add_line(session, settings, now, command)
    if command.kind == "staff_off":
        return _staff_off(session, settings, now, command, notifier)
    if command.kind == "staff_on":
        return _staff_on(session, settings, now, command)
    if command.kind == "mark_packed":
        return _mark_packed(session, settings, now, command)
    if command.kind == "replan":
        plan = _replan(session, settings, now, "voice_replan")
        return CommandResult(
            ok=True,
            kind="replan",
            speech="Re-planned. " + _run_verdict(plan, settings),
            detail=plan.as_dict(settings),
            replanned=True,
        )
    if command.kind == "status":
        from app.engine import planning

        plan = planning.plan_day(session, settings, now)
        return CommandResult(
            ok=True,
            kind="status",
            speech=_run_verdict(plan, settings),
            detail=plan.as_dict(settings),
        )

    return CommandResult(ok=False, kind=command.kind, speech="I don't know how to do that yet.")


def _add_line(session, settings, now, command) -> CommandResult:
    product: Product = command.slots["product"]
    destination: Destination = command.slots["destination"]
    quantity: int = command.slots["quantity"]

    order = session.scalar(
        select(Order).where(
            Order.destination_id == destination.id, Order.status == OrderStatus.OPEN
        )
    )
    created = False
    if order is None:
        # No open order for that shop: make one on the next van out.
        from app.sim.seed import RUN_DEPARTURES

        hour, minute = RUN_DEPARTURES.get(destination.run, (settings.dispatch_cutoff_hour, 0))
        local_now = to_local(now, settings.business_tz)
        departure = now + (
            local_now.replace(hour=hour, minute=minute, second=0, microsecond=0) - local_now
        )
        order = Order(
            code=f"V{int(now.timestamp()) % 100000}",
            customer_name=destination.name,
            channel=destination.channel,
            destination_id=destination.id,
            placed_at=now,
            pickup_at=departure,
            driver_service=f"{destination.run.title()} run",
            status=OrderStatus.OPEN,
        )
        session.add(order)
        session.flush()
        created = True

    line = session.scalar(
        select(OrderLine).where(OrderLine.order_id == order.id, OrderLine.product_id == product.id)
    )
    if line is None:
        line = OrderLine(order_id=order.id, product_id=product.id, quantity=quantity)
        session.add(line)
    else:
        line.quantity += quantity
    session.flush()

    plan = _replan(session, settings, now, "voice_add_line")
    session.commit()
    return CommandResult(
        ok=True,
        kind="add_line",
        speech=(
            f"{quantity} {product.name.lower()} added for {destination.name}"
            f"{' on a new order' if created else ''}, leaving "
            f"{_local(order.pickup_at, settings)}. " + _run_verdict(plan, settings)
        ),
        detail={
            "order": order.code,
            "product": product.code,
            "quantity": quantity,
            "destination": destination.code,
            "plan": plan.as_dict(settings),
        },
        replanned=True,
    )


def _staff_off(session, settings, now, command, notifier) -> CommandResult:
    employee: Employee = command.slots["employee"]
    existing = session.scalar(
        select(LeaveRequest).where(
            LeaveRequest.employee_id == employee.id,
            LeaveRequest.status.in_([LeaveStatus.PENDING_COVERAGE, LeaveStatus.APPROVED]),
            LeaveRequest.ends_at > now,
        )
    )
    if existing is not None:
        return CommandResult(
            ok=False, kind="staff_off", speech=f"{employee.full_name} is already marked off."
        )

    leave = LeaveRequest(
        employee_id=employee.id,
        leave_type=LeaveType.SICK,
        reason="Marked off by the floor manager",
        starts_at=now,
        ends_at=now + timedelta(hours=12),
        status=LeaveStatus.PENDING_COVERAGE,
        created_at=now,
    )
    session.add(leave)
    session.flush()

    # Re-plan before asking anybody for anything. On a packing line, losing a
    # pair of hands usually means the evening gets rearranged, not that twenty
    # separate people need pestering about twenty separate jobs -- and a
    # manager who gets twenty messages for one absence stops reading them.
    plan = _replan(session, settings, now, "voice_staff_off")

    missed = plan.missed_runs
    if missed and notifier is not None:
        _ask_for_help(session, settings, now, plan, employee, notifier)

    session.commit()
    return CommandResult(
        ok=True,
        kind="staff_off",
        speech=f"{employee.full_name} is off. " + _run_verdict(plan, settings),
        detail={
            "employee_id": employee.id,
            "missed_runs": [r.run for r in missed],
            "plan": plan.as_dict(settings),
        },
        replanned=True,
    )


def _ask_for_help(session, settings, now, plan, employee, notifier) -> None:
    """Only once the re-plan has failed: say what is short and who could fix it."""
    from app.engine.escalation import escalate

    missed = plan.missed_runs
    lines = [
        f"{run.run} run leaves {_local(run.departs_at, settings)}, short by {run.short_by:.0f} min"
        for run in missed
    ]

    # Whoever the planner kept passing over for want of a body, ranked.
    suggestions: dict[str, float] = {}
    for placement in plan.placements:
        if placement.on_time:
            continue
        for candidate in placement.candidates:
            if candidate.eligible and not candidate.on_shift:
                suggestions[candidate.employee_name] = max(
                    suggestions.get(candidate.employee_name, 0.0), candidate.score
                )
    if suggestions:
        best = sorted(suggestions, key=lambda name: -suggestions[name])[:3]
        lines.append("Could come in: " + ", ".join(best))

    escalate(
        session,
        notifier,
        settings,
        now=now,
        trigger="voice_staff_off",
        summary=(
            f"{employee.full_name} is off and the evening no longer fits: "
            f"{len(missed)} run(s) will be late"
        ),
        subject_type="day_plan",
        detail_lines=lines,
        details={"employee_id": employee.id, "runs": [r.run for r in missed]},
        urgent=True,
    )


def _staff_on(session, settings, now, command) -> CommandResult:
    employee: Employee = command.slots["employee"]
    leaves = list(
        session.scalars(
            select(LeaveRequest).where(
                LeaveRequest.employee_id == employee.id,
                LeaveRequest.status.in_([LeaveStatus.PENDING_COVERAGE, LeaveStatus.APPROVED]),
                LeaveRequest.ends_at > now,
            )
        )
    )
    for leave in leaves:
        leave.status = LeaveStatus.CANCELLED
        leave.decided_at = now
        leave.decided_by = "floor manager"

    # "Valentino is back" usually means somebody rostered off is coming in,
    # not that a leave request needs cancelling. Cancelling leave alone left
    # him still not on the shift, so the plan did not improve and the command
    # appeared to do nothing.
    called_in = _put_on_shift(session, employee, now)
    session.flush()

    plan = _replan(session, settings, now, "voice_staff_on")
    session.commit()
    what = "is on the shift" if called_in else "is back on"
    return CommandResult(
        ok=True,
        kind="staff_on",
        speech=f"{employee.full_name} {what}. " + _run_verdict(plan, settings),
        detail={
            "employee_id": employee.id,
            "called_in": called_in,
            "plan": plan.as_dict(settings),
        },
        replanned=True,
    )


def _put_on_shift(session: Session, employee: Employee, now: datetime) -> bool:
    """Roster somebody onto the shift that is running, if they are not already.

    Returns whether anything changed, so the reply can say what happened.
    """
    from app.models.enums import ShiftAssignmentStatus
    from app.models.shift import Shift, ShiftAssignment

    shift = session.scalar(
        select(Shift).where(Shift.starts_at <= now, Shift.ends_at > now).order_by(Shift.starts_at)
    )
    if shift is None:
        return False

    assignment = session.scalar(
        select(ShiftAssignment).where(
            ShiftAssignment.shift_id == shift.id,
            ShiftAssignment.employee_id == employee.id,
        )
    )
    if assignment is not None and assignment.status == ShiftAssignmentStatus.SCHEDULED:
        return False
    if assignment is not None:
        assignment.status = ShiftAssignmentStatus.SCHEDULED
    else:
        session.add(ShiftAssignment(shift_id=shift.id, employee_id=employee.id))

    from app.models.timeclock import TimeEntry

    open_entry = session.scalar(
        select(TimeEntry).where(TimeEntry.employee_id == employee.id, TimeEntry.clock_out.is_(None))
    )
    if open_entry is None:
        session.add(
            TimeEntry(employee_id=employee.id, shift_id=shift.id, clock_in=now, source="voice")
        )
    return True


def _mark_packed(session, settings, now, command) -> CommandResult:
    product: Product | None = command.slots.get("product")
    destination: Destination | None = command.slots.get("destination")
    quantity: int | None = command.slots.get("quantity")

    query = select(OrderLine).join(Order).where(Order.status == OrderStatus.OPEN)
    if product is not None:
        query = query.where(OrderLine.product_id == product.id)
    if destination is not None:
        query = query.where(Order.destination_id == destination.id)

    # Earliest van first: "we've done 200 jam donuts" means the ones that were
    # holding up the next departure, not whichever row the database returns.
    query = query.order_by(Order.pickup_at)
    lines = [line for line in session.scalars(query) if line.outstanding]
    if not lines:
        return CommandResult(
            ok=False, kind="mark_packed", speech="Nothing outstanding matches that."
        )

    remaining = quantity
    touched = 0
    for line in lines:
        take = line.outstanding if remaining is None else min(remaining, line.outstanding)
        line.packed += take
        touched += take
        if remaining is not None:
            remaining -= take
            if remaining <= 0:
                break
    session.flush()

    plan = _replan(session, settings, now, "voice_mark_packed")
    session.commit()
    what = product.name.lower() if product else "units"
    where = f" for {destination.name}" if destination else ""
    return CommandResult(
        ok=True,
        kind="mark_packed",
        speech=f"{touched} {what}{where} marked packed. " + _run_verdict(plan, settings),
        detail={"packed": touched, "plan": plan.as_dict(settings)},
        replanned=True,
    )


def handle(
    session: Session, settings: Settings, now: datetime, transcript: str, notifier=None
) -> CommandResult:
    """Parse and run one spoken or typed instruction."""
    command = parse(transcript, Vocabulary.load(session))
    result = execute(session, settings, now, command, notifier)
    result.detail.setdefault("transcript", transcript)
    result.detail.setdefault("understood_as", command.kind)
    return result
