"""Background sweeps.

A cover request that nobody answers is the dangerous case: silence looks
exactly like everything being fine.  This scheduler is what turns silence into
an escalation before the driver is at the door.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app import runtime
from app.config import Settings
from app.db import session_scope
from app.engine import coverage as coverage_engine
from app.models.state import SWEEP_HEARTBEAT
from app.state import touch

logger = logging.getLogger("managers.scheduler")

SWEEP_SECONDS = 60


def run_sweep(settings: Settings, session_factory=None) -> int:
    """One pass over every open cover request. Returns how many it acted on.

    ``session_factory`` exists so this can be driven against a test database
    without reaching for the process-wide one.
    """
    now = runtime.clock().now()
    notifier = runtime.notifier(settings)
    with session_scope(session_factory) as session:
        records = coverage_engine.sweep_open_requests(session, notifier, settings, now)
        # Recorded even on a quiet pass: "nothing to do" and "not running" look
        # identical from the outside, and only one of them is fine.
        touch(session, SWEEP_HEARTBEAT, now, value=str(len(records)))
        if records:
            logger.info("coverage sweep acted on %d request(s)", len(records))
        return len(records)


def build_scheduler(settings: Settings) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        run_sweep,
        "interval",
        seconds=SWEEP_SECONDS,
        args=[settings],
        id="coverage_sweep",
        max_instances=1,
        coalesce=True,
    )
    return scheduler
