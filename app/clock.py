"""Injectable clock.

Every time-dependent decision in the engine reads the current time through a
``Clock`` rather than calling ``datetime.now()`` directly.  That is what makes
"the driver arrives in 20 minutes" testable without waiting 20 minutes.

All datetimes in this system are **naive UTC**.  SQLite does not preserve
timezone information, so storing aware datetimes would silently produce naive
ones on read and break every comparison.  Local wall-clock time is derived only
where a human would notice it (quiet hours), via :func:`to_local`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    """Current time as a naive UTC datetime."""
    return datetime.now(UTC).replace(tzinfo=None)


def to_local(moment: datetime, tz_name: str) -> datetime:
    """Render a naive-UTC moment as local wall-clock time in ``tz_name``."""
    return moment.replace(tzinfo=UTC).astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)


class Clock(Protocol):
    """Anything that can tell the engine what time it is."""

    def now(self) -> datetime: ...


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        return utcnow()


class FrozenClock:
    """A clock that only moves when a test tells it to."""

    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at

    def advance(self, **delta: float) -> datetime:
        """Move the clock forward, e.g. ``clock.advance(minutes=15)``."""
        self._at += timedelta(**delta)
        return self._at

    def set(self, at: datetime) -> datetime:
        self._at = at
        return self._at


def day_bounds(moment: datetime, tz_name: str) -> tuple[datetime, datetime]:
    """The local calendar day containing ``moment``, returned in naive UTC."""
    tz = ZoneInfo(tz_name)
    local = moment.replace(tzinfo=UTC).astimezone(tz)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(UTC).replace(tzinfo=None),
        end_local.astimezone(UTC).replace(tzinfo=None),
    )


def week_bounds(moment: datetime, tz_name: str) -> tuple[datetime, datetime]:
    """The local ISO week (Monday 00:00 local) containing ``moment``, in naive UTC.

    Hour limits are enforced per fixed week rather than on a rolling window,
    because that is how working-time rules are normally written.
    """
    tz = ZoneInfo(tz_name)
    local = moment.replace(tzinfo=UTC).astimezone(tz)
    start_local = (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end_local = start_local + timedelta(days=7)
    return (
        start_local.astimezone(UTC).replace(tzinfo=None),
        end_local.astimezone(UTC).replace(tzinfo=None),
    )


def minute_of_day(moment: datetime, tz_name: str) -> tuple[int, int]:
    """Local ``(weekday, minutes since local midnight)`` for a naive-UTC moment."""
    local = to_local(moment, tz_name)
    return local.weekday(), local.hour * 60 + local.minute


SYSTEM_CLOCK = SystemClock()
