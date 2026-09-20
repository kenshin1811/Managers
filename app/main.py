"""Application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    console,
    coverage,
    dashboard,
    decisions,
    demo,
    employees,
    leave,
    me,
    schedule,
    slack,
    timeclock,
)
from app.config import get_settings
from app.db import create_all
from app.scheduler import build_scheduler, run_sweep

WEB_DIR = Path(__file__).parent / "web"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("managers")

DESCRIPTION = """
Automated employee management.

Tracks job assignments and hours, and when somebody takes leave, works out
which jobs are at risk, reassigns them, and asks other staff to cover -- on
its own, inside guardrails, with every decision written to an audit log a
manager can read and reverse.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    create_all()
    # One sweep on boot, before the interval starts: anything that fell due
    # while the service was down is overdue now, not in sixty seconds. It also
    # means the dashboard has a heartbeat to show immediately.
    try:
        run_sweep(settings)
    except Exception:  # pragma: no cover - never let a sweep stop the service
        logger.exception("startup sweep failed")
    scheduler = build_scheduler(settings)
    scheduler.start()
    logger.info(
        "started | autonomy: reassign=%s coverage=%s | messaging: %s",
        settings.auto_reassign_enabled,
        settings.auto_coverage_requests_enabled,
        "slack" if settings.slack_configured else "dry run",
    )
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Managers",
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
    )
    for module in (
        employees,
        schedule,
        timeclock,
        leave,
        coverage,
        decisions,
        slack,
        dashboard,
        demo,
        me,
        console,
    ):
        app.include_router(module.router)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def home() -> FileResponse:
        """The manager's dashboard."""
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/me", include_in_schema=False)
    def employee_page() -> FileResponse:
        """One employee's own page. Identity is settled client-side, from
        ?employee= in dry run or the signed ?token= link otherwise."""
        return FileResponse(WEB_DIR / "me.html")

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        settings = get_settings()
        return {
            "status": "ok",
            "dry_run": settings.dry_run,
            "auto_reassign": settings.auto_reassign_enabled,
            "auto_coverage_requests": settings.auto_coverage_requests_enabled,
        }

    return app


app = create_app()
