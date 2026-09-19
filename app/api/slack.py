"""Slack interactivity callback.

Optional.  The signed links in :mod:`app.api.coverage` work with no Slack app
configuration at all; this endpoint is the upgrade, for once you have a public
URL and want the original message to update itself in place.

Nothing here is trusted until the signature checks out.  This endpoint moves
people's work around, so an unsigned POST is simply refused.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import notifier_dep, now_dep, settings_dep
from app.db import get_session
from app.engine import coverage as coverage_engine
from app.models.coverage import CoverageOffer
from app.slack_signature import verify_slack_signature

logger = logging.getLogger("managers.api.slack")
router = APIRouter(tags=["slack"], prefix="/slack")


def _parse_action(payload: dict) -> tuple[int, bool] | None:
    """Pull ``(offer_id, accept)`` out of a Slack block-action payload."""
    for action in payload.get("actions", []):
        action_id = action.get("action_id", "")
        if action_id.startswith("coverage_accept_") or action_id.startswith("coverage_decline_"):
            raw = action.get("value") or action_id.rsplit("_", 1)[-1]
            try:
                return int(raw), action_id.startswith("coverage_accept_")
            except ValueError:
                return None
    return None


@router.post("/interactivity")
async def interactivity(
    request: Request,
    session: Session = Depends(get_session),
    settings=Depends(settings_dep),
    notifier=Depends(notifier_dep),
    now=Depends(now_dep),
) -> dict:
    if not settings.slack_signing_secret:
        raise HTTPException(503, "Slack interactivity is not configured")

    body = await request.body()
    ok = verify_slack_signature(
        settings.slack_signing_secret,
        request.headers.get("X-Slack-Request-Timestamp", ""),
        body,
        request.headers.get("X-Slack-Signature", ""),
    )
    if not ok:
        logger.warning("rejected an unsigned or stale Slack callback")
        raise HTTPException(401, "Bad Slack signature")

    form = parse_qs(body.decode())
    raw_payload = (form.get("payload") or [""])[0]
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Malformed Slack payload") from exc

    parsed = _parse_action(payload)
    if parsed is None:
        return {"text": "Nothing to do."}

    offer_id, accept = parsed
    offer = session.get(CoverageOffer, offer_id)
    if offer is None:
        return {"text": "That cover request no longer exists."}

    # The button carries the offer id, but the person who clicked it is the one
    # Slack tells us about -- they have to match, or anyone in the channel
    # could accept on someone else's behalf.
    clicker = (payload.get("user") or {}).get("id")
    if clicker and offer.employee is not None and offer.employee.slack_user_id:
        if clicker != offer.employee.slack_user_id:
            return {"text": "That cover request was addressed to someone else."}

    if accept:
        outcome = coverage_engine.accept_offer(session, offer, notifier, settings, now)
    else:
        outcome = coverage_engine.decline_offer(session, offer, notifier, settings, now)
    return {"text": outcome.message, "replace_original": False}
