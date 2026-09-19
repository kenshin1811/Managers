"""Messaging: what gets said, to whom, and what stops it going out."""

from __future__ import annotations

import time

import pytest

from app.config import Settings
from app.notifications import ConsoleNotifier, build_notifier
from app.notifications.base import Message
from app.notifications.slack import SlackNotifier
from app.notifications.templates import (
    cover_request,
    coverage_links,
    coverage_superseded,
    escalation,
    reassignment_notice,
)
from app.tokens import TokenError, make_token, parse_token

# --- choosing a notifier ----------------------------------------------------


def test_no_token_means_nothing_can_be_sent():
    assert isinstance(build_notifier(Settings(slack_bot_token=None)), ConsoleNotifier)


def test_dry_run_overrides_a_real_token():
    """A token in the environment is not consent to start messaging people."""
    settings = Settings(slack_bot_token="xoxb-real-looking", dry_run=True)
    assert isinstance(build_notifier(settings), ConsoleNotifier)
    assert settings.slack_configured is False


def test_slack_is_used_only_when_configured_and_dry_run_is_off():
    settings = Settings(slack_bot_token="xoxb-real-looking", dry_run=False)
    assert settings.slack_configured is True
    assert isinstance(build_notifier(settings), SlackNotifier)


def test_console_notifier_records_instead_of_sending():
    notifier = ConsoleNotifier(echo=False)
    delivery = notifier.send(Message(channel="U1", text="hello", kind="test"))
    assert delivery.ok and delivery.ref
    assert notifier.of_kind("test")[0].text == "hello"
    notifier.clear()
    assert notifier.sent == []


# --- what the messages say --------------------------------------------------


@pytest.fixture
def request_with_offer(session, settings, short_staffed, notifier, now):
    from sqlalchemy import select

    from app.engine import reassignment
    from app.models.coverage import CoverageRequest
    from app.models.enums import CoverageStatus
    from tests.test_reassignment import file_leave

    leave = file_leave(session, short_staffed, now)
    reassignment.handle_leave_request(session, leave, notifier, settings, now)
    session.commit()
    request = session.scalars(
        select(CoverageRequest).where(CoverageRequest.status == CoverageStatus.OPEN)
    ).one()
    return request, request.offers[0]


def test_a_cover_request_can_be_answered_from_the_lock_screen(
    session, settings, short_staffed, request_with_offer
):
    """The plain-text fallback has to carry the whole question on its own."""
    request, offer = request_with_offer
    task = session.get(type(request).task.property.mapper.class_, request.task_id)
    message = cover_request(offer.employee, task, request, offer, settings)

    assert message.kind == "cover_request"
    assert message.channel == offer.employee.slack_user_id
    for expected in ("Cover needed", "Pack order 1043", "20 min", "14:50"):
        assert expected in message.text


def test_a_cover_request_carries_working_accept_and_decline_links(settings, request_with_offer):
    _, offer = request_with_offer
    accept_url, decline_url = coverage_links(offer, settings)
    assert accept_url.startswith(settings.public_base_url)
    assert accept_url != decline_url

    accept_token = accept_url.split("token=")[1]
    parsed = parse_token(accept_token, settings.coverage_link_secret)
    assert parsed.offer_id == offer.id
    assert parsed.action == "accept"


def test_a_message_still_names_somebody_without_a_slack_account(session, settings, short_staffed):
    """In dry run you want to see who would have been contacted."""
    from app.models.task import Task

    employee = short_staffed.employees["luis"]
    employee.slack_user_id = None
    task = session.get(Task, short_staffed.tasks["pack_1043"].id)
    message = reassignment_notice(employee, task, settings, reason="testing")
    assert message.channel == "@luis"


def test_an_escalation_is_marked_urgent_and_explains_itself(settings):
    message = escalation(
        "No cover for 'Pack order 1043'",
        settings,
        ["3 x not trained for it", "1 x would exceed the weekly hours limit"],
        urgent=True,
    )
    assert message.channel == settings.slack_manager_channel
    assert message.meta["urgent"] is True
    rendered = str(message.blocks)
    assert "weekly hours limit" in rendered
    assert "rather than break a staffing rule" in rendered


def test_a_stood_down_colleague_is_told_who_took_it(session, short_staffed):
    from app.models.task import Task

    task = session.get(Task, short_staffed.tasks["pack_1043"].id)
    message = coverage_superseded(short_staffed.employees["sam"], task, "Luis Ferrer")
    assert "Luis Ferrer" in message.text
    assert "Thanks" in message.text


# --- the tokens behind the links --------------------------------------------


def test_a_token_round_trips():
    token = make_token(7, "accept", "secret", 60)
    parsed = parse_token(token, "secret")
    assert (parsed.offer_id, parsed.action) == (7, "accept")


def test_a_token_signed_with_another_secret_is_refused():
    token = make_token(7, "accept", "secret", 60)
    with pytest.raises(TokenError, match="bad signature"):
        parse_token(token, "different-secret")


def test_a_tampered_offer_id_is_refused():
    token = make_token(7, "accept", "secret", 60)
    offer, action, expiry, signature = token.split(".")
    with pytest.raises(TokenError, match="bad signature"):
        parse_token(f"8.{action}.{expiry}.{signature}", "secret")


def test_an_expired_token_is_refused():
    token = make_token(7, "accept", "secret", 1, now=time.time() - 3600)
    with pytest.raises(TokenError, match="expired"):
        parse_token(token, "secret")


def test_a_malformed_token_is_refused():
    with pytest.raises(TokenError, match="malformed"):
        parse_token("not-a-token", "secret")


def test_an_unknown_action_cannot_be_minted():
    with pytest.raises(ValueError, match="unknown action"):
        make_token(7, "delete-everything", "secret", 60)
