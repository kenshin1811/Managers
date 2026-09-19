"""What the system actually says to people.

Two rules shape every message here.  First, the plain-text fallback has to
stand on its own, because that is what shows up on a phone's lock screen.
Second, a cover request has to contain everything needed to answer it without
opening anything else -- what the job is, when it starts, how long it takes,
and which order is waiting on it.  Somebody is reading this between two other
tasks.
"""

from __future__ import annotations

from datetime import datetime

from app.clock import to_local
from app.config import Settings
from app.models.coverage import CoverageOffer, CoverageRequest
from app.models.employee import Employee
from app.models.task import Task
from app.notifications.base import Message
from app.tokens import make_token


def _local(moment: datetime | None, settings: Settings) -> str:
    if moment is None:
        return "unscheduled"
    return f"{to_local(moment, settings.business_tz):%a %H:%M}"


def _channel_for(employee: Employee) -> str:
    """Slack user id if we have one, otherwise a readable placeholder.

    In dry run the placeholder is the whole point -- you can see who *would*
    have been messaged before anyone hands over a bot token.
    """
    return employee.slack_user_id or f"@{employee.full_name.split()[0].lower()}"


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def coverage_links(offer: CoverageOffer, settings: Settings) -> tuple[str, str]:
    """Signed, expiring accept and decline URLs for one offer."""
    base = settings.public_base_url.rstrip("/")
    accept = make_token(
        offer.id, "accept", settings.coverage_link_secret, settings.coverage_link_ttl_minutes
    )
    decline = make_token(
        offer.id, "decline", settings.coverage_link_secret, settings.coverage_link_ttl_minutes
    )
    return (
        f"{base}/coverage/respond?token={accept}",
        f"{base}/coverage/respond?token={decline}",
    )


def cover_request(
    employee: Employee,
    task: Task,
    request: CoverageRequest,
    offer: CoverageOffer,
    settings: Settings,
) -> Message:
    order = task.order
    accept_url, decline_url = coverage_links(offer, settings)
    deadline = _local(task.deadline, settings)
    window = f"{_local(task.starts_at, settings)}-{_local(task.due_at, settings)}"

    if order is not None:
        headline = (
            f"*Cover needed:* {task.title} for order *{order.code}* - driver arrives {deadline}"
        )
        order_line = (
            f"*Order:* {order.code}"
            + (f" for {order.customer_name}" if order.customer_name else "")
            + f"\n*Driver pickup:* {deadline}"
            + (f" ({order.driver_service})" if order.driver_service else "")
        )
    else:
        headline = f"*Cover needed:* {task.title} - due {deadline}"
        order_line = f"*Due:* {deadline}"

    text = (
        f"Cover needed: {task.title} ({window}, about {task.estimated_minutes} min)."
        f" Deadline {deadline}. Reply in Slack to accept."
    )

    blocks = [
        _section(headline),
        _section(
            f"*Job:* {task.title}"
            + (f" at {task.station}" if task.station else "")
            + f"\n*When:* {window} (about {task.estimated_minutes} min)\n{order_line}"
        ),
        _context(
            f"Asked because {request.reason or 'the assigned colleague is unavailable'}."
            f" First person to accept gets it - we will tell everyone else."
        ),
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "I can cover"},
                    "url": accept_url,
                    "action_id": f"coverage_accept_{offer.id}",
                    "value": str(offer.id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Can't this time"},
                    "url": decline_url,
                    "action_id": f"coverage_decline_{offer.id}",
                    "value": str(offer.id),
                },
            ],
        },
    ]
    return Message(
        channel=_channel_for(employee),
        text=text,
        kind="cover_request",
        blocks=blocks,
        meta={"offer_id": offer.id, "coverage_request_id": request.id, "task_id": task.id},
    )


def reassignment_notice(employee: Employee, task: Task, settings: Settings, reason: str) -> Message:
    order = task.order
    deadline = _local(task.deadline, settings)
    order_bit = f" for order {order.code}" if order else ""
    text = (
        f"You've picked up: {task.title}{order_bit}."
        f" Starts {_local(task.starts_at, settings)}, needs to be done by {deadline}."
    )
    return Message(
        channel=_channel_for(employee),
        text=text,
        kind="reassignment",
        blocks=[
            _section(f"*New job for you:* {task.title}{order_bit}"),
            _section(
                f"*When:* {_local(task.starts_at, settings)} - {_local(task.due_at, settings)}"
                f"\n*Deadline:* {deadline}"
                + (f"\n*Station:* {task.station}" if task.station else "")
            ),
            _context(reason),
        ],
        meta={"task_id": task.id},
    )


def handover_notice(employee: Employee, task: Task, taker: Employee, settings: Settings) -> Message:
    order = task.order
    order_bit = f" (order {order.code})" if order else ""
    text = (
        f"Leave noted. {taker.full_name} is taking over {task.title}{order_bit}"
        f" - nothing further needed from you."
    )
    return Message(
        channel=_channel_for(employee),
        text=text,
        kind="handover",
        blocks=[
            _section(f"*Handover confirmed:* {task.title}{order_bit}"),
            _section(f"*{taker.full_name}* is taking it from {_local(task.starts_at, settings)}."),
            _context("Take care. Nothing further needed from you on this one."),
        ],
        meta={"task_id": task.id},
    )


def coverage_confirmed(employee: Employee, task: Task, settings: Settings) -> Message:
    order = task.order
    order_bit = f" for order {order.code}" if order else ""
    text = (
        f"Thanks - you're covering {task.title}{order_bit},"
        f" {_local(task.starts_at, settings)} to {_local(task.due_at, settings)}."
    )
    return Message(
        channel=_channel_for(employee),
        text=text,
        kind="resolution",
        blocks=[
            _section(f"*You're covering:* {task.title}{order_bit}"),
            _section(
                f"*When:* {_local(task.starts_at, settings)} - {_local(task.due_at, settings)}"
                f"\n*Deadline:* {_local(task.deadline, settings)}"
            ),
            _context("Thanks for stepping in."),
        ],
        meta={"task_id": task.id},
    )


def coverage_superseded(employee: Employee, task: Task, taker_name: str) -> Message:
    text = f"No need any more - {taker_name} is covering {task.title}. Thanks for responding."
    return Message(
        channel=_channel_for(employee),
        text=text,
        kind="resolution",
        blocks=[_section(f"~{task.title}~\n*{taker_name}* is covering this. Thanks anyway.")],
        meta={"task_id": task.id},
    )


def manager_notice(
    summary: str, settings: Settings, detail_lines: list[str] | None = None
) -> Message:
    blocks = [_section(f":white_check_mark: {summary}")]
    if detail_lines:
        blocks.append(_context("\n".join(detail_lines)))
    return Message(
        channel=settings.slack_manager_channel,
        text=summary,
        kind="manager_notice",
        blocks=blocks,
    )


def escalation(
    summary: str, settings: Settings, detail_lines: list[str] | None = None, urgent: bool = False
) -> Message:
    icon = ":rotating_light:" if urgent else ":warning:"
    blocks = [_section(f"{icon} *Needs a manager:* {summary}")]
    if detail_lines:
        blocks.append(_section("\n".join(f"- {line}" for line in detail_lines)))
    blocks.append(_context("The system stopped here rather than break a staffing rule."))
    return Message(
        channel=settings.slack_manager_channel,
        text=f"Needs a manager: {summary}",
        kind="escalation",
        blocks=blocks,
        meta={"urgent": urgent},
    )
