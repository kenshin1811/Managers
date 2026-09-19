"""Picking a notifier."""

from __future__ import annotations

import logging

from app.config import Settings
from app.notifications.base import Notifier
from app.notifications.console import ConsoleNotifier

logger = logging.getLogger("managers.notifications")


def build_notifier(settings: Settings) -> Notifier:
    """Slack when it is fully configured and dry run is off; console otherwise."""
    if settings.slack_configured:
        from app.notifications.slack import SlackNotifier

        logger.info("messaging: Slack (live)")
        return SlackNotifier(
            token=settings.slack_bot_token or "", default_channel=settings.slack_manager_channel
        )
    if settings.slack_bot_token and settings.dry_run:
        logger.info("messaging: dry run (a Slack token is set but DRY_RUN is on)")
    else:
        logger.info("messaging: dry run (no Slack token configured)")
    return ConsoleNotifier()
