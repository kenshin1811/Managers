"""Shared fixtures.

Every test runs against a throwaway database, a frozen clock and a notifier
that records instead of sending, so the whole decision flow is exercised
without a single real side effect.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.clock import FrozenClock
from app.config import Settings
from app.db import build_engine, create_all
from app.engine.snapshot import EmployeeFacts
from app.models.employee import Employee
from app.models.enums import Stage, TaskPriority
from app.models.task import Task
from app.notifications import ConsoleNotifier
from app.sim.seed import ANCHOR, World, at, seed_factory


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite://",
        business_tz="Australia/Melbourne",
        dry_run=True,
        coverage_link_secret="test-secret",
        public_base_url="http://testserver",
    )


@pytest.fixture
def db_engine():
    engine = build_engine("sqlite://")
    create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(db_engine) -> Session:
    factory = sessionmaker(bind=db_engine, expire_on_commit=False, future=True)
    with factory() as session:
        yield session


@pytest.fixture
def notifier() -> ConsoleNotifier:
    return ConsoleNotifier(echo=False)


@pytest.fixture
def now() -> datetime:
    """16:30 local on the simulation day -- mid-shift, three vans still to go."""
    return at(*ANCHOR)


@pytest.fixture
def clock(now) -> FrozenClock:
    return FrozenClock(now)


@pytest.fixture
def world(session) -> World:
    """The factory with three packers rostered: the evening fits."""
    return seed_factory(session, full_team=True)


@pytest.fixture
def short_staffed(session) -> World:
    """Two packers rostered, so the vans start slipping and help is needed."""
    return seed_factory(session, full_team=False)


@pytest.fixture
def planned(session, settings, world, now) -> World:
    """The factory with tonight's work already on the board."""
    from app.engine import planning

    planning.plan_and_commit(session, settings, now)
    session.commit()
    return world


def a_pack_job(
    session: Session,
    world: World,
    *,
    assignee: str = "shaleen",
    quantity: int = 90,
    minutes: int = 25,
    min_proficiency: int = 1,
) -> Task:
    """Put exactly one packing job on the board and return it.

    The factory seed creates no tasks on purpose -- the planner does that --
    but the engine tests are about one decision at a time, and planning a
    whole evening to get at a single job would bury the assertion under
    sixty other rows. So these tests put one job up and nothing else: the
    north van's jam donuts, Shaleen's, due before the van at 18:00.
    """
    order = world.orders["fitzroy"]
    task = Task(
        title=f"Pack and label {quantity} jam donut",
        station="Packing hall",
        order_id=order.id,
        assignee_id=world.employee_id(assignee),
        required_skill_id=world.skills["pack_frozen"].id,
        min_proficiency=min_proficiency,
        starts_at=at(16, 40),
        due_at=at(17, 45),
        estimated_minutes=minutes,
        priority=TaskPriority.CRITICAL,
        stage=Stage.PACK,
        run="north",
        product_id=world.product_id("jam_donut"),
        quantity=quantity,
    )
    session.add(task)
    session.flush()
    world.tasks["pack_north"] = task
    return task


def a_board(session: Session, world: World) -> dict[str, Task]:
    """Three of Shaleen's jobs, back to back, and nothing else on the board.

    Enough for the reassignment engine to have a real problem -- one job with
    a van behind it, one without -- and small enough that a failing assertion
    still points at one decision.
    """
    a_pack_job(session, world)

    pack_east = Task(
        title="Pack and label 60 custard tart",
        station="Packing hall",
        order_id=world.orders["richmond"].id,
        assignee_id=world.employee_id("shaleen"),
        required_skill_id=world.skills["pack_frozen"].id,
        starts_at=at(17, 45),
        due_at=at(18, 45),
        estimated_minutes=30,
        priority=TaskPriority.HIGH,
        stage=Stage.PACK,
        run="east",
        product_id=world.product_id("custard_tart"),
        quantity=60,
    )
    # No order behind it, so missing it costs nothing outside the building.
    restock = Task(
        title="Restock the pack line",
        station="Packing hall",
        assignee_id=world.employee_id("shaleen"),
        required_skill_id=world.skills["pack_frozen"].id,
        starts_at=at(18, 45),
        due_at=at(19, 30),
        estimated_minutes=20,
        priority=TaskPriority.ROUTINE,
    )
    session.add_all([pack_east, restock])
    session.flush()
    world.tasks["pack_east"] = pack_east
    world.tasks["restock"] = restock
    session.commit()
    return world.tasks


def make_facts(employee: Employee, **overrides) -> EmployeeFacts:
    """An ``EmployeeFacts`` with everything empty unless a test says otherwise."""
    facts = EmployeeFacts(employee=employee)
    for key, value in overrides.items():
        setattr(facts, key, value)
    return facts
