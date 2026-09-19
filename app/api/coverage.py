"""Cover requests and the replies to them."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import notifier_dep, now_dep, settings_dep
from app.api.schemas import CoverageOut, CoverageReply, CoverageReplyResult
from app.db import get_session
from app.engine import coverage as coverage_engine
from app.models.coverage import CoverageOffer, CoverageRequest
from app.tokens import TokenError, parse_token

router = APIRouter(tags=["coverage"], prefix="/coverage")


def _apply(session, offer, accept, notifier, settings, now) -> coverage_engine.CoverageOutcome:
    if accept:
        return coverage_engine.accept_offer(session, offer, notifier, settings, now)
    return coverage_engine.decline_offer(session, offer, notifier, settings, now)


@router.get("", response_model=list[CoverageOut])
def list_coverage(
    status: str | None = None, session: Session = Depends(get_session)
) -> list[CoverageRequest]:
    query = select(CoverageRequest).order_by(CoverageRequest.created_at.desc())
    if status:
        query = query.where(CoverageRequest.status == status)
    return list(session.scalars(query))


@router.get("/respond", response_class=HTMLResponse)
def respond_via_link(
    token: str = Query(..., description="Signed, expiring token from the Slack message"),
    session: Session = Depends(get_session),
    settings=Depends(settings_dep),
    notifier=Depends(notifier_dep),
    now=Depends(now_dep),
) -> HTMLResponse:
    """The endpoint behind the buttons in a cover request.

    This exists so the system works before anyone has configured a Slack app:
    a signed URL button needs no interactivity endpoint registered with Slack,
    only this service being reachable.
    """
    try:
        parsed = parse_token(token, settings.coverage_link_secret)
    except TokenError as exc:
        return _page("Link not valid", str(exc), ok=False, status_code=400)

    offer = session.get(CoverageOffer, parsed.offer_id)
    if offer is None:
        return _page("Not found", "That cover request no longer exists.", ok=False, status_code=404)

    outcome = _apply(session, offer, parsed.action == "accept", notifier, settings, now)
    heading = "Thanks!" if outcome.ok else "Nothing to do"
    return _page(heading, outcome.message, ok=outcome.ok)


@router.get("/{request_id}", response_model=CoverageOut)
def get_coverage(request_id: int, session: Session = Depends(get_session)) -> CoverageRequest:
    request = session.get(CoverageRequest, request_id)
    if request is None:
        raise HTTPException(404, "No such coverage request")
    return request


@router.post("/{request_id}/reply", response_model=CoverageReplyResult)
def reply(
    request_id: int,
    payload: CoverageReply,
    session: Session = Depends(get_session),
    settings=Depends(settings_dep),
    notifier=Depends(notifier_dep),
    now=Depends(now_dep),
) -> CoverageReplyResult:
    """Accept or decline on behalf of an employee (kiosk, phone call, tests)."""
    offer = session.scalar(
        select(CoverageOffer).where(
            CoverageOffer.coverage_request_id == request_id,
            CoverageOffer.employee_id == payload.employee_id,
        )
    )
    if offer is None:
        raise HTTPException(404, "That employee was not asked to cover this")

    outcome = _apply(session, offer, payload.accept, notifier, settings, now)
    return CoverageReplyResult(
        ok=outcome.ok,
        status=outcome.status,
        message=outcome.message,
        task_id=outcome.task_id,
        blockers=outcome.blockers,
    )


@router.post("/sweep")
def sweep(
    session: Session = Depends(get_session),
    settings=Depends(settings_dep),
    notifier=Depends(notifier_dep),
    now=Depends(now_dep),
) -> dict:
    """Run the timeout and pre-pickup checks now instead of waiting for the scheduler."""
    records = coverage_engine.sweep_open_requests(session, notifier, settings, now)
    return {"swept_at": now.isoformat(), "decisions": [record.id for record in records]}


def _page(heading: str, body: str, *, ok: bool, status_code: int = 200) -> HTMLResponse:
    colour = "#0b7a3b" if ok else "#8a4b00"
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{heading}</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; margin: 0;
         display: grid; place-items: center; min-height: 100vh; background: #f6f7f9; }}
  .card {{ background: #fff; padding: 2rem; border-radius: 12px; max-width: 26rem;
           box-shadow: 0 1px 3px rgba(0,0,0,.12); text-align: center; }}
  h1 {{ font-size: 1.35rem; margin: 0 0 .5rem; color: {colour}; }}
  p {{ margin: 0; color: #3c4043; line-height: 1.5; }}
</style></head>
<body><div class="card"><h1>{heading}</h1><p>{body}</p></div></body></html>"""
    return HTMLResponse(html, status_code=status_code)
