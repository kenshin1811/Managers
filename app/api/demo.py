"""A demo switch, so the dashboard can be watched doing something.

An empty dashboard cannot answer "is this working?".  These endpoints load the
seeded kitchen anchored to the current moment -- so the driver really is
arriving in 45 minutes -- and then fire the leave request that sets the engine
off.

They wipe the database, so they refuse to run unless ``DRY_RUN`` is on. Dry
run is the sandbox; anything else is somebody's real roster.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import notifier_dep, now_dep, settings_dep
from app.config import Settings
from app.db import Base, get_session
from app.engine import commands, planning
from app.models.enums import LeaveStatus
from app.models.leave import LeaveRequest
from app.models.state import SystemState
from app.sim.seed import seed_factory

router = APIRouter(tags=["demo"], prefix="/api/demo")

VARIANTS = {
    "full_team": "Three packers rostered: the evening fits",
    "short_team": "Two packers rostered: the vans start slipping",
}


def _guard(settings: Settings) -> None:
    if not settings.dry_run:
        raise HTTPException(
            403,
            "The demo wipes the database and only runs with DRY_RUN on. "
            "This service is configured to send real messages.",
        )


@router.get("/variants")
def variants(settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    return {"available": settings.dry_run, "variants": VARIANTS}


@router.post("/reset")
def reset(
    variant: str = "on_shift",
    session: Session = Depends(get_session),
    settings: Settings = Depends(settings_dep),
    now: datetime = Depends(now_dep),
) -> dict[str, Any]:
    """Wipe everything and lay out the seeded kitchen as of right now."""
    _guard(settings)
    if variant not in VARIANTS:
        raise HTTPException(400, f"Unknown variant. Try one of: {', '.join(VARIANTS)}")

    # The heartbeat is a fact about the service, not about the kitchen, so it
    # survives -- otherwise resetting the demo would make the agent look dead.
    for table in reversed(Base.metadata.sorted_tables):
        if table.name != SystemState.__tablename__:
            session.execute(table.delete())
    session.flush()

    world = seed_factory(
        session,
        full_team=(variant == "full_team"),
        anchor=now,
        tz=settings.business_tz,
    )
    # Seeding lays out the orders; the agent works out the evening. Shipping a
    # pre-planned board would hide the part worth watching.
    plan, tasks = planning.plan_and_commit(session, settings, now, trigger="demo_reset")
    session.commit()
    return {
        "variant": variant,
        "description": VARIANTS[variant],
        "employees": len(world.employees),
        "orders": len(world.orders),
        "units": sum(order.units for order in world.orders.values()),
        "tasks": len(tasks),
        "feasible": plan.feasible,
        "next_van": min(o.pickup_at for o in world.orders.values()).isoformat() + "Z",
    }


@router.post("/disrupt")
def disrupt(
    session: Session = Depends(get_session),
    settings: Settings = Depends(settings_dep),
    notifier=Depends(notifier_dep),
    now: datetime = Depends(now_dep),
) -> dict[str, Any]:
    """Shaleen goes home sick, through the same path the microphone uses.

    Deliberately routed through the command parser rather than straight at the
    engine: the demo button and the floor manager's voice should not be able
    to take different routes, or the button stops being evidence.
    """
    _guard(settings)
    from app.models.employee import Employee

    shaleen = session.scalar(select(Employee).where(Employee.full_name == "Shaleen"))
    if shaleen is None:
        raise HTTPException(409, "Load the factory first.")
    existing = session.scalar(
        select(LeaveRequest).where(
            LeaveRequest.employee_id == shaleen.id,
            LeaveRequest.status.in_([LeaveStatus.PENDING_COVERAGE, LeaveStatus.APPROVED]),
        )
    )
    if existing is not None:
        raise HTTPException(409, "Shaleen has already gone. Reset to run it again.")

    result = commands.handle(session, settings, now, "Shaleen is off sick", notifier)
    session.commit()
    return {"ok": result.ok, "speech": result.speech, "detail": result.detail}
