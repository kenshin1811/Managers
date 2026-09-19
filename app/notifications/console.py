"""The notifier used when nothing should actually be sent.

This is the default.  Turning a system like this loose on real people before
you have read what it intends to say to them is how you end up texting the
whole kitchen at 3am.
"""

from __future__ import annotations

import logging

from app.notifications.base import Delivery, Message

logger = logging.getLogger("managers.notifications")


class ConsoleNotifier:
    """Records every message and logs it. Sends nothing."""

    def __init__(self, echo: bool = True) -> None:
        self.sent: list[Message] = []
        self.updates: list[tuple[str, Message]] = []
        self.echo = echo

    def send(self, message: Message) -> Delivery:
        self.sent.append(message)
        if self.echo:
            logger.info("[dry-run %s] -> %s: %s", message.kind, message.channel, message.text)
        return Delivery(message=message, ok=True, ref=f"dry-run-{len(self.sent)}")

    def update(self, channel: str, ref: str, message: Message) -> Delivery:
        self.updates.append((ref, message))
        if self.echo:
            logger.info("[dry-run edit %s] -> %s: %s", ref, channel, message.text)
        return Delivery(message=message, ok=True, ref=ref)

    # -- test and simulation helpers --------------------------------------
    def of_kind(self, kind: str) -> list[Message]:
        return [m for m in self.sent if m.kind == kind]

    def clear(self) -> None:
        self.sent.clear()
        self.updates.clear()
