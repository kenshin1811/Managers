"""A small kitchen, seeded so the decision flow has something real to chew on.

The roster is deliberately awkward.  One person is the only packer on shift,
one is busy with work that outranks anything you could hand them, one is
off-shift but already at the weekly hours limit, and one has covered twice
this month already.  A roster where everybody is interchangeable would not
test anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.models.coverage import CoverageOffer, CoverageRequest
from app.models.employee import AvailabilityWindow, Employee, EmployeeSkill, Skill
from app.models.enums import (
    CoverageStatus,
    OfferStatus,
    OrderStatus,
    TaskPriority,
    TaskStatus,
)
from app.models.order import Order
from app.models.shift import Shift, ShiftAssignment
from app.models.task import Task
from app.models.timeclock import TimeEntry

SIM_DATE = date(2026, 9, 25)  # a Friday: the week already has four days in it,
# which is what lets the weekly-hours guardrail have anything to bite on.
SIM_TZ = "America/New_York"


def at(hour: int, minute: int = 0, *, day: date = SIM_DATE, tz: str = SIM_TZ) -> datetime:
    """Local wall-clock time on the simulation day, as naive UTC."""
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(tz))
    return local.astimezone(UTC).replace(tzinfo=None)


@dataclass
class World:
    employees: dict[str, Employee] = field(default_factory=dict)
    skills: dict[str, Skill] = field(default_factory=dict)
    orders: dict[str, Order] = field(default_factory=dict)
    tasks: dict[str, Task] = field(default_factory=dict)
    shift: Shift | None = None

    def employee_id(self, key: str) -> int:
        return self.employees[key].id


def seed_kitchen(session: Session, *, on_shift_packer: bool = True) -> World:
    """Build the kitchen.

    ``on_shift_packer`` is the single switch the scenarios turn.  With it on,
    somebody already at work can take over and the system never has to
    interrupt anyone's day off.  With it off, the only way to get the order
    out is to ring round.
    """
    world = World()

    for code, name in [
        ("prep", "Food prep"),
        ("pack", "Order packing"),
        ("expo", "Expediting"),
    ]:
        skill = Skill(code=code, name=name)
        session.add(skill)
        world.skills[code] = skill
    session.flush()

    def hire(
        key: str,
        full_name: str,
        slack: str,
        skills: dict[str, int],
        *,
        is_manager: bool = False,
    ) -> Employee:
        employee = Employee(
            full_name=full_name,
            slack_user_id=slack,
            email=f"{key}@example.test",
            role="manager" if is_manager else "staff",
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

    mai = hire("mai", "Mai Tran", "U_MAI", {"prep": 4, "pack": 4})
    dev = hire("dev", "Dev Osei", "U_DEV", {"prep": 3, **({"pack": 3} if on_shift_packer else {})})
    priya = hire("priya", "Priya Raman", "U_PRIYA", {"prep": 5})
    nia = hire("nia", "Nia Boateng", "U_NIA", {"expo": 5, "pack": 4})
    luis = hire("luis", "Luis Ferrer", "U_LUIS", {"pack": 4, "prep": 2})
    sam = hire("sam", "Sam Whitlock", "U_SAM", {"pack": 3})
    tomas = hire("tomas", "Tomas Iglesias", "U_TOMAS", {"pack": 3})
    hire("robin", "Robin Diaz", "U_ROBIN", {}, is_manager=True)

    # The three off-shift packers are all willing to be called in on a Monday
    # daytime. Whether they *may* be is what the guardrails decide.
    for employee in (luis, sam, tomas):
        session.add(
            AvailabilityWindow(
                employee_id=employee.id, weekday=4, start_minute=8 * 60, end_minute=22 * 60
            )
        )

    shift = Shift(name="Lunch service", location="Kitchen", starts_at=at(10, 0), ends_at=at(18, 0))
    session.add(shift)
    session.flush()
    world.shift = shift
    for employee in (mai, dev, priya, nia):
        session.add(ShiftAssignment(shift_id=shift.id, employee_id=employee.id))
        session.add(
            TimeEntry(
                employee_id=employee.id, shift_id=shift.id, clock_in=at(10, 0), source="kiosk"
            )
        )

    # Tomas has worked four twelve-hour days already this week. He is trained,
    # willing and available -- and at 48 hours the weekly limit rules him out
    # anyway. This is the case the audit trail has to explain well, because
    # from the outside it looks like the system ignored an obvious candidate.
    for offset in range(1, 5):
        day = SIM_DATE - timedelta(days=offset)
        session.add(
            TimeEntry(
                employee_id=tomas.id,
                clock_in=at(8, 0, day=day),
                clock_out=at(20, 0, day=day),
                source="kiosk",
            )
        )

    orders = [
        ("1039", "Okonkwo", at(11, 40), at(12, 30), OrderStatus.PICKED_UP),
        ("1043", "Okafor", at(14, 5), at(14, 50), OrderStatus.OPEN),
        ("1051", "Delgado", at(15, 10), at(16, 20), OrderStatus.OPEN),
    ]
    for code, customer, placed, pickup, status in orders:
        order = Order(
            code=code,
            customer_name=customer,
            placed_at=placed,
            pickup_at=pickup,
            driver_name="Ari" if status != OrderStatus.PICKED_UP else "Jo",
            driver_service="QuickDrop",
            status=status,
        )
        session.add(order)
        world.orders[code] = order
    session.flush()

    def job(
        key: str,
        title: str,
        assignee: Employee,
        skill: str | None,
        min_level: int,
        start: datetime,
        due: datetime,
        *,
        order_code: str | None = None,
        minutes: int = 20,
        priority: TaskPriority = TaskPriority.ROUTINE,
        station: str = "",
        status: TaskStatus = TaskStatus.PENDING,
    ) -> Task:
        task = Task(
            title=title,
            station=station,
            order_id=world.orders[order_code].id if order_code else None,
            assignee_id=assignee.id,
            required_skill_id=world.skills[skill].id if skill else None,
            min_proficiency=min_level,
            starts_at=start,
            due_at=due,
            estimated_minutes=minutes,
            priority=priority,
            status=status,
        )
        session.add(task)
        session.flush()
        world.tasks[key] = task
        return task

    # The headline collision: Mai is packing an order whose driver is minutes away.
    job(
        "pack_1043",
        "Pack order 1043",
        mai,
        "pack",
        3,
        at(14, 15),
        at(14, 45),
        order_code="1043",
        minutes=20,
        priority=TaskPriority.HIGH,
        station="Pass",
    )
    # Second, less urgent job also on Mai -- it should be handled differently.
    job(
        "prep_1051",
        "Prep order 1051",
        mai,
        "prep",
        2,
        at(15, 30),
        at(16, 0),
        order_code="1051",
        minutes=30,
        station="Cold line",
    )
    # Routine work with no driver attached: it still needs an owner, but
    # missing it costs nothing outside the building.
    job(
        "restock",
        "Restock the cold line",
        mai,
        "prep",
        1,
        at(16, 30),
        at(17, 0),
        minutes=30,
        station="Cold line",
    )
    # Nia is on work that outranks anything we could hand her.
    job(
        "expo_line",
        "Run the expo pass",
        nia,
        "expo",
        3,
        at(14, 0),
        at(15, 0),
        minutes=60,
        priority=TaskPriority.CRITICAL,
        station="Pass",
    )

    _seed_cover_history(session, world)
    session.commit()
    return world


def _seed_cover_history(session: Session, world: World) -> None:
    """Sam covered twice recently.

    Fairness is a scoring input, not a guardrail: it does not stop Sam being
    asked, it just means Luis gets asked first. Without history like this the
    same obliging person gets every call.
    """
    sam = world.employees["sam"]
    past_task = Task(
        title="Pack order 0990 (last week)",
        order_id=None,
        assignee_id=sam.id,
        starts_at=at(13, 0, day=SIM_DATE - timedelta(days=6)),
        due_at=at(13, 30, day=SIM_DATE - timedelta(days=6)),
        estimated_minutes=30,
        status=TaskStatus.DONE,
    )
    session.add(past_task)
    session.flush()

    for days_ago in (6, 12):
        moment = at(12, 0, day=SIM_DATE - timedelta(days=days_ago))
        request = CoverageRequest(
            task_id=past_task.id,
            reason="historical",
            status=CoverageStatus.FILLED,
            created_at=moment,
            expires_at=moment + timedelta(minutes=10),
            filled_by_id=sam.id,
            filled_at=moment + timedelta(minutes=3),
        )
        session.add(request)
        session.flush()
        session.add(
            CoverageOffer(
                coverage_request_id=request.id,
                employee_id=sam.id,
                status=OfferStatus.ACCEPTED,
                rank=1,
                score=50.0,
                sent_at=moment,
                responded_at=moment + timedelta(minutes=3),
            )
        )
