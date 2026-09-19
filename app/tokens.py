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


COVERAGE_ACTIONS = ("accept", "decline")
IDENTITY_ACTION = "whoami"


@dataclass(frozen=True)
class CoverageToken:
    offer_id: int
    action: str
    expires_at: int


@dataclass(frozen=True)
class IdentityToken:
    """Proof that we sent this link to this person.

    Worth being precise about what it is not: this is not a login. It shows
    the link came from us and has not been tampered with or left lying around
    past its expiry. Anyone holding the link can use it.
    """

    employee_id: int
    expires_at: int


def _sign(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), payload.encode(), sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _mint(subject_id: int, action: str, secret: str, ttl_minutes: int, now: float | None) -> str:
    expires_at = int((now if now is not None else time.time()) + ttl_minutes * 60)
    payload = f"{subject_id}.{action}.{expires_at}"
    return f"{payload}.{_sign(payload, secret)}"


def _read(token: str, secret: str, now: float | None) -> tuple[int, str, int]:
    """Verify and unpack, without judging what the action means."""
    parts = token.split(".")
    if len(parts) != 4:
        raise TokenError("malformed token")
    subject_raw, action, expires_raw, signature = parts
    payload = f"{subject_raw}.{action}.{expires_raw}"
    if not hmac.compare_digest(signature, _sign(payload, secret)):
        raise TokenError("bad signature")
    try:
        subject_id = int(subject_raw)
        expires_at = int(expires_raw)
    except ValueError as exc:
        raise TokenError("malformed token") from exc
    if (now if now is not None else time.time()) > expires_at:
        raise TokenError("token expired")
    return subject_id, action, expires_at


def make_token(
    offer_id: int, action: str, secret: str, ttl_minutes: int, now: float | None = None
) -> str:
    if action not in COVERAGE_ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    return _mint(offer_id, action, secret, ttl_minutes, now)


def parse_token(token: str, secret: str, now: float | None = None) -> CoverageToken:
    offer_id, action, expires_at = _read(token, secret, now)
    if action not in COVERAGE_ACTIONS:
        raise TokenError("unknown action")
    return CoverageToken(offer_id=offer_id, action=action, expires_at=expires_at)


def make_identity_token(
    employee_id: int, secret: str, ttl_minutes: int, now: float | None = None
) -> str:
    """A link that opens one person's own page, and nobody else's."""
    return _mint(employee_id, IDENTITY_ACTION, secret, ttl_minutes, now)


def parse_identity_token(token: str, secret: str, now: float | None = None) -> IdentityToken:
    employee_id, action, expires_at = _read(token, secret, now)
    # An accept link must not double as a pass to somebody's whole schedule.
    if action != IDENTITY_ACTION:
        raise TokenError("not an identity token")
    return IdentityToken(employee_id=employee_id, expires_at=expires_at)
