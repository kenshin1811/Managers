"""Slack delivery."""

from __future__ import annotations

import logging

from app.notifications.base import Delivery, Message

logger = logging.getLogger("managers.notifications.slack")


class SlackNotifier:
    """Posts through the Slack Web API.

    A failed send is logged and reported, never raised: a Slack outage must
    not take down the reassignment that the message was only describing.  The
    coverage request still exists, and the timeout sweep will escalate it to a
    manager if nobody answers.
    """

    def __init__(self, token: str, default_channel: str = "#managers") -> None:
        from slack_sdk import WebClient

        self.client = WebClient(token=token)
        self.default_channel = default_channel

    def send(self, message: Message) -> Delivery:
        from slack_sdk.errors import SlackApiError

        try:
            response = self.client.chat_postMessage(
                channel=message.channel or self.default_channel,
                text=message.text,
                blocks=message.blocks,
            )
            return Delivery(message=message, ok=True, ref=response.get("ts"))
        except SlackApiError as exc:  # pragma: no cover - network path
            logger.warning("slack send failed for %s: %s", message.channel, exc)
            return Delivery(message=message, ok=False, error=str(exc))

    def update(self, channel: str, ref: str, message: Message) -> Delivery:
        from slack_sdk.errors import SlackApiError

        try:
            self.client.chat_update(
                channel=channel, ts=ref, text=message.text, blocks=message.blocks
            )
            return Delivery(message=message, ok=True, ref=ref)
        except SlackApiError as exc:  # pragma: no cover - network path
            logger.warning("slack update failed for %s/%s: %s", channel, ref, exc)
            return Delivery(message=message, ok=False, error=str(exc))
