"""Process-wide wiring: the clock and the notifier.

Both are swappable.  The simulation runs on a :class:`~app.clock.FrozenClock`
so a whole afternoon of driver pickups happens in milliseconds, and tests swap
in a recording notifier.  Nothing else in the app needs to know.
"""

from __future__ import annotations

from app.clock import SYSTEM_CLOCK, Clock
from app.config import Settings, get_settings
from app.notifications.base import Notifier
from app.notifications.factory import build_notifier

_clock: Clock = SYSTEM_CLOCK
_notifier: Notifier | None = None


def clock() -> Clock:
    return _clock


def set_clock(new_clock: Clock) -> None:
    global _clock
    _clock = new_clock


def notifier(settings: Settings | None = None) -> Notifier:
    global _notifier
    if _notifier is None:
        _notifier = build_notifier(settings or get_settings())
    return _notifier


def set_notifier(new_notifier: Notifier | None) -> None:
    global _notifier
    _notifier = new_notifier


def reset() -> None:
    """Used between tests."""
    global _clock, _notifier
    _clock = SYSTEM_CLOCK
    _notifier = None
