"""The donut factory's packing team, seeded for a working evening.

The team is four people -- Ken, Jasoo, Shaleen and Valentino -- and the
factory runs seven days a week, which means they cannot all be on every day.
Today three are rostered and Valentino is off, which is both realistic and the
reason the call-in machinery has anything to do.

The shift is 13:00 to 21:00 and the last van has to leave before 21:00. What
actually bites is not the total hours in the day -- there are plenty -- but
the individual runs: everything for the 18:00 north run has to be out of the
freezer, packed, labelled, sorted and loaded by 18:00, and no amount of spare
capacity at 20:00 helps with that.

Two assumptions worth correcting if they are wrong: "currtart tarts" is read
here as custard tarts, and the human floor manager is a placeholder name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.clock import to_local
from app.models.coverage import CoverageOffer, CoverageRequest
from app.models.destination import Destination
from app.models.employee import AvailabilityWindow, Employee, EmployeeSkill, Skill
from app.models.enums import Channel, CoverageStatus, OfferStatus, OrderStatus, ProductKind
from app.models.order import Order, OrderLine
from app.models.product import Product
from app.models.shift import Shift, ShiftAssignment
from app.models.timeclock import TimeEntry

SIM_DATE = date(2026, 9, 25)  # a Friday: the week already has four days in it,
# which is what lets the weekly-hours guardrail have anything to bite on.
SIM_TZ = "Australia/Melbourne"

#: The moment the scenarios treat as "now" -- mid-shift, with the tight run
#: ninety minutes out and two more behind it.
ANCHOR = (16, 30)

SHIFT_START = (13, 0)
SHIFT_END = (21, 0)

#: Nothing leaves the building after this, any day of the week.
CUTOFF = (21, 0)


class Timeline:
    """Maps the simulation's wall-clock day onto a real moment.

    The printed scenarios want a fixed, readable day so the output is the same
    every run.  The dashboard demo wants that same day to be happening *now*,
    so its countdowns tick and the next van really is ninety minutes out. One
    offset serves both.
    """

    def __init__(
        self, anchor: datetime | None = None, day: date = SIM_DATE, tz: str = SIM_TZ
    ) -> None:
        self.day = day
        self.tz = tz
        self.offset = timedelta(0)
        if anchor is not None:
            self.offset = anchor - self._raw(*ANCHOR, day=day)

    def _raw(self, hour: int, minute: int = 0, *, day: date | None = None) -> datetime:
        target = day or self.day
        local = datetime(
            target.year, target.month, target.day, hour, minute, tzinfo=ZoneInfo(self.tz)
        )
        return local.astimezone(UTC).replace(tzinfo=None)

    def at(self, hour: int, minute: int = 0, *, day: date | None = None) -> datetime:
        return self._raw(hour, minute, day=day) + self.offset


def at(hour: int, minute: int = 0, *, day: date = SIM_DATE, tz: str = SIM_TZ) -> datetime:
    """Local wall-clock time on the simulation day, as naive UTC."""
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(tz))
    return local.astimezone(UTC).replace(tzinfo=None)


@dataclass
class World:
    employees: dict[str, Employee] = field(default_factory=dict)
    skills: dict[str, Skill] = field(default_factory=dict)
    products: dict[str, Product] = field(default_factory=dict)
    destinations: dict[str, Destination] = field(default_factory=dict)
    orders: dict[str, Order] = field(default_factory=dict)
    tasks: dict[str, Task] = field(default_factory=dict)  # noqa: F821
    shift: Shift | None = None
    timeline: Timeline | None = None

    def employee_id(self, key: str) -> int:
        return self.employees[key].id

    def product_id(self, key: str) -> int:
        return self.products[key].id


# --------------------------------------------------------------------------
# The catalogue
# --------------------------------------------------------------------------

#: ``key, name, kind, category, seconds per unit, units per tray``
#:
#: The pack rates are what turn an order into minutes of somebody's evening,
#: so they are the numbers to argue with first. Cream-filled work is slower
#: than dropping rings into a clamshell, and a whole apple cake is slower
#: again because it is boxed and labelled one at a time.
CATALOGUE = [
    ("vanilla_slice", "Vanilla slice", ProductKind.FRESH, "slice", 8.0, 9),
    ("long_john", "Long John", ProductKind.FRESH, "filled", 6.0, 12),
    ("eclair", "Eclair", ProductKind.FRESH, "filled", 7.0, 12),
    ("banana_bread", "Banana bread", ProductKind.FROZEN, "loaf", 9.0, 6),
    ("muffin_wcr", "White chocolate raspberry muffin", ProductKind.FROZEN, "muffin", 5.0, 12),
    ("muffin_blueberry", "Blueberry muffin", ProductKind.FROZEN, "muffin", 5.0, 12),
    ("muffin_dcc", "Double chocolate chip muffin", ProductKind.FROZEN, "muffin", 5.0, 12),
    ("jam_donut", "Jam donut", ProductKind.FROZEN, "donut", 4.0, 12),
    ("strawberry_ring", "Strawberry ring", ProductKind.FROZEN, "donut", 5.0, 12),
    ("chocolate_ring", "Chocolate ring", ProductKind.FROZEN, "donut", 5.0, 12),
    ("custard_tart", "Custard tart", ProductKind.FROZEN, "tart", 7.0, 9),
    ("apple_cake", "Apple cake", ProductKind.FROZEN, "cake", 10.0, 6),
]

#: ``key, name, suburb, channel, run, stop``
DESTINATIONS = [
    ("wholesale_team", "Wholesale packer team", "On site", Channel.INTERNAL, "internal", 1),
    ("fitzroy", "Fitzroy shopfront", "Fitzroy", Channel.RETAIL, "north", 1),
    ("brunswick", "Brunswick shopfront", "Brunswick", Channel.RETAIL, "north", 2),
    ("richmond", "Richmond shopfront", "Richmond", Channel.RETAIL, "east", 1),
    ("hawthorn", "Hawthorn shopfront", "Hawthorn", Channel.RETAIL, "east", 2),
    ("south_yarra", "Cafe group", "South Yarra", Channel.WHOLESALE, "south", 1),
    ("st_kilda", "St Kilda shopfront", "St Kilda", Channel.RETAIL, "south", 2),
    ("docklands", "Docklands catering", "Docklands", Channel.WHOLESALE, "city", 1),
    ("carlton", "Carlton wholesale", "Carlton", Channel.WHOLESALE, "city", 2),
]

#: When each run leaves. Everything on a run goes together, so one late
#: consignment holds up every stop behind it.
RUN_DEPARTURES = {
    "internal": (17, 30),
    "north": (18, 0),
    "east": (19, 15),
    "south": (20, 0),
    "city": (20, 45),
}

#: How much of each run is already packed by the time the scenarios start.
#:
#: The team clocked on at 13:00, so a 16:30 board showing nothing done would
#: be a fiction -- and an unfair one, since it would make a perfectly ordinary
#: evening look impossible. What is left is the real question: enough to keep
#: three packers busy to the last van, and too much for two.
PROGRESS = {
    "internal": 0.85,
    "north": 0.55,
    "east": 0.25,
    "south": 0.10,
    "city": 0.0,
}

#: ``destination key -> [(product key, quantity)]`` -- roughly 2,200 units.
ORDER_BOOK = {
    "wholesale_team": [("banana_bread", 120), ("strawberry_ring", 100)],
    "fitzroy": [("jam_donut", 180), ("chocolate_ring", 120), ("long_john", 60)],
    "brunswick": [("jam_donut", 150), ("muffin_blueberry", 90), ("vanilla_slice", 40)],
    "richmond": [("strawberry_ring", 120), ("custard_tart", 80), ("eclair", 40)],
    "hawthorn": [("apple_cake", 60), ("muffin_wcr", 90), ("long_john", 50)],
    "south_yarra": [("muffin_dcc", 150), ("muffin_blueberry", 150)],
    "st_kilda": [("jam_donut", 100), ("banana_bread", 60), ("vanilla_slice", 30)],
    "docklands": [("jam_donut", 200), ("chocolate_ring", 100)],
    "carlton": [("custard_tart", 90), ("apple_cake", 70)],
}


# --------------------------------------------------------------------------


def seed_factory(
    session: Session,
    *,
    full_team: bool = True,
    anchor: datetime | None = None,
    tz: str = SIM_TZ,
) -> World:
    """Build the factory floor.

    ``full_team`` is the switch the scenarios turn. With it on, three packers
    are rostered and the evening is workable. With it off, only two are, and
    the 18:00 run stops being arithmetic and starts being a problem.

    ``anchor`` slides the whole day so that "now" lands on a real moment,
    which is what the dashboard needs for live countdowns. ``tz`` must match
    the running service's business timezone, since that is the clock the
    availability guardrail reads.
    """
    world = World()
    timeline = Timeline(anchor, tz=tz)
    at = timeline.at  # noqa: A001 -- deliberately shadows the module helper
    world.timeline = timeline

    # --- skills ---------------------------------------------------------
    for code, name in [
        ("freezer", "Freezer retrieval"),
        ("pack_fresh", "Fresh packing"),
        ("pack_frozen", "Frozen packing"),
        ("dispatch", "Dispatch and loading"),
    ]:
        skill = Skill(code=code, name=name)
        session.add(skill)
        world.skills[code] = skill
    session.flush()

    def hire(
        key: str, full_name: str, slack: str, skills: dict[str, int], *, is_manager: bool = False
    ) -> Employee:
        employee = Employee(
            full_name=full_name,
            slack_user_id=slack,
            email=f"{key}@donuts.example",
            role="floor manager" if is_manager else "packer",
            is_manager=is_manager,
        )
        session.add(employee)
        session.flush()
        for code, level in skills.items():
            session.add(
                EmployeeSkill(
                    employee_id=employee.id, skill_id=world.skills[code].id, proficiency=level
                )
            )
        world.employees[key] = employee
        return employee

    # The named team. Their skills differ on purpose: an evening where
    # everybody can do everything would not need planning, it would need a
    # queue.
    ken = hire(
        "ken",
        "Ken",
        "U_KEN",
        {"freezer": 5, "pack_frozen": 4, "pack_fresh": 3, "dispatch": 4},
    )
    jasoo = hire("jasoo", "Jasoo", "U_JASOO", {"pack_fresh": 5, "pack_frozen": 3})
    shaleen = hire("shaleen", "Shaleen", "U_SHALEEN", {"pack_frozen": 5, "freezer": 4})
    valentino = hire(
        "valentino",
        "Valentino",
        "U_VALENTINO",
        {"pack_frozen": 4, "pack_fresh": 4, "dispatch": 5},
    )
    # Relief cover. A seven-day operation cannot run on four people alone, and
    # two reliefs is also what gives the call-in machinery a real choice to
    # make: Tavi is available, Marlo is at the weekly limit.
    tavi = hire("tavi", "Tavi", "U_TAVI", {"pack_frozen": 3, "pack_fresh": 3})
    marlo = hire("marlo", "Marlo", "U_MARLO", {"pack_frozen": 3, "freezer": 3})
    hire("robin", "Robin", "U_ROBIN", {}, is_manager=True)

    rostered = [ken, jasoo, shaleen] if full_team else [ken, jasoo]
    off_today = [valentino, tavi, marlo] + ([] if full_team else [shaleen])

    # Whoever is off today is willing to be called in this evening.
    call_in_weekday = to_local(at(*SHIFT_START), tz).weekday()
    for employee in off_today:
        session.add(
            AvailabilityWindow(
                employee_id=employee.id,
                weekday=call_in_weekday,
                start_minute=10 * 60,
                end_minute=22 * 60,
            )
        )

    shift = Shift(
        name="Packing, afternoon",
        location="Packing hall",
        starts_at=at(*SHIFT_START),
        ends_at=at(*SHIFT_END),
    )
    session.add(shift)
    session.flush()
    world.shift = shift
    for employee in rostered:
        session.add(ShiftAssignment(shift_id=shift.id, employee_id=employee.id))
        session.add(
            TimeEntry(
                employee_id=employee.id,
                shift_id=shift.id,
                clock_in=at(*SHIFT_START),
                source="kiosk",
            )
        )

    # Marlo has worked four long days already. Trained, willing, available --
    # and the weekly limit rules him out anyway. From the outside this looks
    # like the agent ignoring an obvious candidate, so the audit trail has to
    # explain it well.
    for offset in range(1, 5):
        day = SIM_DATE - timedelta(days=offset)
        session.add(
            TimeEntry(
                employee_id=marlo.id,
                clock_in=at(8, 0, day=day),
                clock_out=at(20, 0, day=day),
                source="kiosk",
            )
        )

    # --- catalogue ------------------------------------------------------
    for code, name, kind, category, seconds, per_tray in CATALOGUE:
        product = Product(
            code=code,
            name=name,
            kind=kind,
            category=category,
            pack_seconds_per_unit=seconds,
            units_per_tray=per_tray,
            needs_label=True,
            required_skill_id=world.skills[
                "pack_fresh" if kind == ProductKind.FRESH else "pack_frozen"
            ].id,
        )
        session.add(product)
        world.products[code] = product

    for code, name, suburb, channel, run, stop in DESTINATIONS:
        destination = Destination(
            code=code, name=name, suburb=suburb, channel=channel, run=run, stop_order=stop
        )
        session.add(destination)
        world.destinations[code] = destination
    session.flush()

    # --- the order book --------------------------------------------------
    for index, (destination_key, lines) in enumerate(ORDER_BOOK.items(), start=1):
        destination = world.destinations[destination_key]
        departure = RUN_DEPARTURES[destination.run]
        order = Order(
            code=f"D{1040 + index}",
            customer_name=destination.name,
            channel=destination.channel,
            destination_id=destination.id,
            placed_at=at(9, 0),
            pickup_at=at(*departure),
            driver_name="Van " + destination.run,
            driver_service=f"{destination.run.title()} run",
            status=OrderStatus.OPEN,
        )
        session.add(order)
        session.flush()
        done = PROGRESS.get(destination.run, 0.0)
        for product_key, quantity in lines:
            session.add(
                OrderLine(
                    order_id=order.id,
                    product_id=world.products[product_key].id,
                    quantity=quantity,
                    packed=int(quantity * done),
                )
            )
        world.orders[destination_key] = order

    _seed_cover_history(session, world, at)
    session.commit()
    return world


def _seed_cover_history(session: Session, world: World, at) -> None:
    """Valentino covered twice recently.

    Fairness is a scoring input, not a guardrail: it does not stop him being
    asked, it just means somebody else gets asked first. Without history like
    this, the same obliging person gets every call.
    """
    from app.models.task import Task

    valentino = world.employees["valentino"]
    past_task = Task(
        title="Pack jam donuts (last week)",
        assignee_id=valentino.id,
        starts_at=at(15, 0, day=SIM_DATE - timedelta(days=6)),
        due_at=at(16, 0, day=SIM_DATE - timedelta(days=6)),
        estimated_minutes=60,
        status="done",
    )
    session.add(past_task)
    session.flush()

    for days_ago in (6, 12):
        moment = at(14, 0, day=SIM_DATE - timedelta(days=days_ago))
        request = CoverageRequest(
            task_id=past_task.id,
            reason="historical",
            status=CoverageStatus.FILLED,
            created_at=moment,
            expires_at=moment + timedelta(minutes=10),
            filled_by_id=valentino.id,
            filled_at=moment + timedelta(minutes=3),
        )
        session.add(request)
        session.flush()
        session.add(
            CoverageOffer(
                coverage_request_id=request.id,
                employee_id=valentino.id,
                status=OfferStatus.ACCEPTED,
                rank=1,
                score=50.0,
                sent_at=moment,
                responded_at=moment + timedelta(minutes=3),
            )
        )
