"""The messaging contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Message:
    channel: str
    """A Slack user id, channel id or ``#channel`` name."""

    text: str
    """Plain-text fallback. Always meaningful on its own -- this is what
    lands in a phone notification."""

    kind: str
    """``cover_request``, ``reassignment``, ``handover``, ``escalation`` or
    ``resolution``. Used for filtering in tests and the audit trail."""

    blocks: list[dict[str, Any]] | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Delivery:
    message: Message
    ok: bool
    ref: str | None = None
    """Provider handle for the sent message, so it can be edited later."""

    error: str | None = None


class Notifier(Protocol):
    def send(self, message: Message) -> Delivery: ...

    def update(self, channel: str, ref: str, message: Message) -> Delivery: ...
