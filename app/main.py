"""Application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import coverage, decisions, employees, leave, schedule, slack, timeclock
from app.config import get_settings
from app.db import create_all
from app.scheduler import build_scheduler

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
    for module in (employees, schedule, timeclock, leave, coverage, decisions, slack):
        app.include_router(module.router)

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
