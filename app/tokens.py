"""Signed, expiring, single-use links for cover request replies.

Slack's interactive buttons need a publicly reachable callback URL, which
nobody has on day one.  A URL button carrying a signed token works from the
first message, with no Slack app configuration at all: the employee taps
"I can cover", their browser hits this service, and the token proves it was
really them we asked.

Single use is enforced by the offer's own status, not by the token -- a replay
of an accepted token finds the offer already resolved and changes nothing.
"""

from __future__ import annotations

import base64
import hmac
import time
from dataclasses import dataclass
from hashlib import sha256


class TokenError(Exception):
    """The token was malformed, tampered with, or has expired."""


@dataclass(frozen=True)
class CoverageToken:
    offer_id: int
    action: str
    expires_at: int


def _sign(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), payload.encode(), sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def make_token(
    offer_id: int, action: str, secret: str, ttl_minutes: int, now: float | None = None
) -> str:
    if action not in ("accept", "decline"):
        raise ValueError(f"unknown action {action!r}")
    expires_at = int((now if now is not None else time.time()) + ttl_minutes * 60)
    payload = f"{offer_id}.{action}.{expires_at}"
    return f"{payload}.{_sign(payload, secret)}"


def parse_token(token: str, secret: str, now: float | None = None) -> CoverageToken:
    parts = token.split(".")
    if len(parts) != 4:
        raise TokenError("malformed token")
    offer_raw, action, expires_raw, signature = parts
    payload = f"{offer_raw}.{action}.{expires_raw}"
    if not hmac.compare_digest(signature, _sign(payload, secret)):
        raise TokenError("bad signature")
    try:
        offer_id = int(offer_raw)
        expires_at = int(expires_raw)
    except ValueError as exc:
        raise TokenError("malformed token") from exc
    if (now if now is not None else time.time()) > expires_at:
        raise TokenError("token expired")
    if action not in ("accept", "decline"):
        raise TokenError("unknown action")
    return CoverageToken(offer_id=offer_id, action=action, expires_at=expires_at)
