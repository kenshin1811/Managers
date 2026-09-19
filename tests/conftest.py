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
from app.notifications import ConsoleNotifier
from app.sim.seed import World, at, seed_kitchen


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite://",
        business_tz="America/New_York",
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
    """14:05 local on the simulation day -- 45 minutes before the driver."""
    return at(14, 5)


@pytest.fixture
def clock(now) -> FrozenClock:
    return FrozenClock(now)


@pytest.fixture
def world(session) -> World:
    """The kitchen, with somebody on shift who can pack."""
    return seed_kitchen(session, on_shift_packer=True)


@pytest.fixture
def short_staffed(session) -> World:
    """The kitchen with no on-shift packer, so cover has to be called in."""
    return seed_kitchen(session, on_shift_packer=False)


def make_facts(employee: Employee, **overrides) -> EmployeeFacts:
    """An ``EmployeeFacts`` with everything empty unless a test says otherwise."""
    facts = EmployeeFacts(employee=employee)
    for key, value in overrides.items():
        setattr(facts, key, value)
    return facts
