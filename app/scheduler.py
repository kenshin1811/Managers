"""Background sweeps.

Two kinds of silence are dangerous here, and this is what turns both of them
into something a manager sees.

A cover request nobody answers looks exactly like everything being fine.  So
does a van quietly running out of time while the floor works steadily on the
wrong thing -- nobody pressed anything, so nothing told anybody.  The first is
``coverage.sweep_open_requests``; the second is ``watch.watch``, which
re-plans the evening on its own and speaks up when its verdict changes.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app import runtime
from app.config import Settings
from app.db import session_scope
from app.engine import coverage as coverage_engine
from app.engine import watch as watch_engine
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
        if records:
            logger.info("coverage sweep acted on %d request(s)", len(records))

        # Then the evening itself. A failure here must not take the coverage
        # sweep down with it: an unanswered cover request has a driver behind
        # it, and that is the more urgent of the two.
        acted = len(records)
        try:
            seen = watch_engine.watch(session, settings, notifier, now)
        except Exception:  # pragma: no cover - defensive
            logger.exception("the floor watch failed; coverage sweep still ran")
        else:
            if seen.acted:
                acted += 1
                logger.info(
                    "watch re-planned the evening%s",
                    f"; slipped: {', '.join(seen.slipped)}" if seen.slipped else "",
                )

        # Recorded even on a quiet pass: "nothing to do" and "not running" look
        # identical from the outside, and only one of them is fine.
        touch(session, SWEEP_HEARTBEAT, now, value=str(acted))
        return acted


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
