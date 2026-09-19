"""Shared FastAPI dependencies."""

from __future__ import annotations

from datetime import datetime

from app import runtime
from app.config import Settings, get_settings


def settings_dep() -> Settings:
    return get_settings()


def notifier_dep():
    return runtime.notifier()


def now_dep() -> datetime:
    return runtime.clock().now()
