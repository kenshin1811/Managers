"""A demo switch, so the dashboard can be watched doing something.

An empty dashboard cannot answer "is this working?".  These endpoints load the
seeded kitchen anchored to the current moment -- so the driver really is
arriving in 45 minutes -- and then fire the leave request that sets the engine
off.

They wipe the database, so they refuse to run unless ``DRY_RUN`` is on. Dry
run is the sandbox; anything else is somebody's real roster.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import notifier_dep, now_dep, settings_dep
from app.config import Settings
from app.db import Base, get_session
from app.engine import reassignment
from app.models.enums import LeaveStatus, LeaveType
from app.models.leave import LeaveRequest
from app.models.state import SystemState
from app.sim.seed import seed_kitchen

router = APIRouter(tags=["demo"], prefix="/api/demo")

VARIANTS = {
    "on_shift": "A trained colleague is already at work",
    "call_in": "Nobody on shift can pack, so cover has to be called in",
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

    world = seed_kitchen(
        session,
        on_shift_packer=(variant == "on_shift"),
        anchor=now,
        tz=settings.business_tz,
    )
    session.commit()
    return {
        "variant": variant,
        "description": VARIANTS[variant],
        "employees": len(world.employees),
        "tasks": len(world.tasks),
        "pickup_at": world.orders["1043"].pickup_at.isoformat() + "Z",
        "leave_starts_at": (now + timedelta(minutes=25)).isoformat() + "Z",
    }


@router.post("/leave")
def trigger_leave(
    session: Session = Depends(get_session),
    settings: Settings = Depends(settings_dep),
    notifier=Depends(notifier_dep),
    now: datetime = Depends(now_dep),
) -> dict[str, Any]:
    """Mai has a family emergency. Let the engine deal with it."""
    _guard(settings)
    from app.models.employee import Employee

    mai = session.scalar(select(Employee).where(Employee.full_name == "Mai Tran"))
    if mai is None:
        raise HTTPException(409, "Load the demo kitchen first.")
    existing = session.scalar(
        select(LeaveRequest).where(
            LeaveRequest.employee_id == mai.id,
            LeaveRequest.status.in_([LeaveStatus.PENDING_COVERAGE, LeaveStatus.APPROVED]),
        )
    )
    if existing is not None:
        raise HTTPException(409, "Mai has already gone. Reset the demo to run it again.")

    leave = LeaveRequest(
        employee_id=mai.id,
        leave_type=LeaveType.EMERGENCY,
        reason="Family emergency - has to leave now",
        starts_at=now + timedelta(minutes=25),
        ends_at=now + timedelta(hours=4),
        status=LeaveStatus.PENDING_COVERAGE,
        created_at=now,
    )
    session.add(leave)
    session.flush()

    plan, records = reassignment.handle_leave_request(session, leave, notifier, settings, now)
    session.commit()
    return {
        "leave_id": leave.id,
        "leave_status": leave.status,
        "decisions": [record.id for record in records],
        "plan": plan.as_dict(),
    }
