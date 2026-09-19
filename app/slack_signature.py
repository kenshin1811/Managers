"""Verifying that an inbound request really came from Slack.

Anyone who learns the interactivity URL can POST to it, and that endpoint
reassigns work.  Without this check, a stranger could accept cover on
somebody else's behalf.
"""

from __future__ import annotations

import hmac
import time
from hashlib import sha256

REPLAY_WINDOW_SECONDS = 300


def verify_slack_signature(
    signing_secret: str,
    timestamp: str,
    body: bytes,
    signature: str,
    now: float | None = None,
) -> bool:
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = now if now is not None else time.time()
    if abs(current - sent_at) > REPLAY_WINDOW_SECONDS:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")
