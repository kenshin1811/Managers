"""Outbound messaging.

The engine talks to a :class:`~app.notifications.base.Notifier`, never to
Slack.  With no bot token configured -- the default -- it gets a
:class:`~app.notifications.console.ConsoleNotifier` instead, which records
everything and sends nothing.  Tests and the simulation run on exactly the
same code path as production.
"""

from app.notifications.base import Delivery, Message, Notifier
from app.notifications.console import ConsoleNotifier
from app.notifications.factory import build_notifier

__all__ = ["ConsoleNotifier", "Delivery", "Message", "Notifier", "build_notifier"]
