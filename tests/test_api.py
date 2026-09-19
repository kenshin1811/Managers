"""The HTTP surface, including a full round trip built entirely through it."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import timedelta
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.deps import notifier_dep, now_dep, settings_dep
from app.db import get_session
from app.main import create_app
from app.models.coverage import CoverageRequest
from app.models.enums import CoverageStatus
from app.sim.seed import at
from app.tokens import make_token, parse_identity_token


@pytest.fixture
def client(session, settings, notifier, clock):
    # Deliberately not used as a context manager: that would run the lifespan,
    # which creates the real database file and starts the scheduler.
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[settings_dep] = lambda: settings
    app.dependency_overrides[notifier_dep] = lambda: notifier
    app.dependency_overrides[now_dep] = lambda: clock.now()
    return TestClient(app)


def iso(moment) -> str:
    return moment.isoformat()


def test_health_reports_the_autonomy_settings(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["dry_run"] is True
    assert "auto_reassign" in body


# --- the whole thing, built through the API ---------------------------------


def test_full_round_trip_from_empty_database(client, session, notifier):
    """Skills, staff, a shift, an order, a job, hours, leave, cover, audit."""
    assert client.post("/skills", json={"code": "pack", "name": "Packing"}).status_code == 201

    mai = client.post("/employees", json={"full_name": "Mai Tran", "slack_user_id": "U_MAI"}).json()
    dev = client.post("/employees", json={"full_name": "Dev Osei", "slack_user_id": "U_DEV"}).json()
    for person in (mai, dev):
        response = client.post(
            f"/employees/{person['id']}/skills", json={"skill_code": "pack", "proficiency": 4}
        )
        assert response.status_code == 200

    shift = client.post(
        "/shifts",
        json={
            "name": "Lunch",
            "starts_at": iso(at(10, 0)),
            "ends_at": iso(at(18, 0)),
            "employee_ids": [mai["id"], dev["id"]],
        },
    ).json()

    order = client.post(
        "/orders",
        json={"code": "1043", "customer_name": "Okafor", "pickup_at": iso(at(14, 50))},
    ).json()

    task = client.post(
        "/tasks",
        json={
            "title": "Pack order 1043",
            "order_id": order["id"],
            "assignee_id": mai["id"],
            "required_skill_code": "pack",
            "min_proficiency": 3,
            "starts_at": iso(at(14, 15)),
            "due_at": iso(at(14, 45)),
            "estimated_minutes": 20,
            "priority": "high",
        },
    ).json()

    for person in (mai, dev):
        response = client.post(
            "/timeclock/clock-in",
            json={"employee_id": person["id"], "shift_id": shift["id"], "at": iso(at(10, 0))},
        )
        assert response.status_code == 201

    summary = client.get(f"/timeclock/summary/{mai['id']}").json()
    assert summary["on_the_clock"] is True
    assert summary["hours_today"] == pytest.approx(4.08, abs=0.05)

    # The moment everything hangs off.
    response = client.post(
        "/leave-requests",
        json={
            "employee_id": mai["id"],
            "starts_at": iso(at(14, 30)),
            "ends_at": iso(at(18, 0)),
            "leave_type": "emergency",
            "reason": "Family emergency",
        },
    )
    assert response.status_code == 201
    body = response.json()

    assert body["leave"]["status"] == "approved"
    plan = body["plan"]
    assert plan["fully_resolved"] is True
    task_plan = plan["task_plans"][0]
    assert task_plan["action"] == "auto_reassigned"
    assert task_plan["chosen"]["employee_name"] == "Dev Osei"
    assert task_plan["task"]["critical"] is True

    assert client.get(f"/tasks/{task['id']}").json()["assignee_id"] == dev["id"]

    decisions = client.get("/decisions").json()
    actions = {d["action"] for d in decisions}
    assert {"auto_reassigned", "leave_approved"} <= actions

    # And a manager disagrees.
    auto = next(d for d in decisions if d["action"] == "auto_reassigned")
    override = client.post(
        f"/decisions/{auto['id']}/override",
        json={"manager_id": mai["id"], "employee_id": mai["id"], "note": "Mai is staying"},
    )
    assert override.status_code == 200
    assert client.get(f"/tasks/{task['id']}").json()["assignee_id"] == mai["id"]
    assert client.get(f"/decisions/{auto['id']}").json()["reverted_by_id"] == override.json()["id"]

    second = client.post(f"/decisions/{auto['id']}/override", json={"manager_id": mai["id"]})
    assert second.status_code == 409, "a decision cannot be overridden twice"


# --- replying to a cover request over HTTP ----------------------------------


def test_signed_link_accepts_cover_without_any_slack_setup(
    client, session, settings, short_staffed, notifier
):
    response = client.post(
        "/leave-requests",
        json={
            "employee_id": short_staffed.employee_id("mai"),
            "starts_at": iso(at(14, 30)),
            "ends_at": iso(at(18, 0)),
            "leave_type": "emergency",
        },
    )
    assert response.status_code == 201

    request = session.scalars(
        select(CoverageRequest).where(CoverageRequest.status == CoverageStatus.OPEN)
    ).one()
    offer = next(o for o in request.offers if o.employee_id == short_staffed.employee_id("luis"))

    token = make_token(offer.id, "accept", settings.coverage_link_secret, 60)
    hop = client.get("/coverage/respond", params={"token": token}, follow_redirects=False)

    # Answering lands them on their own page, carrying an identity link and
    # the outcome -- they have just agreed to come in, and the next thing they
    # need is the rest of their day, not a dead-end card.
    assert hop.status_code == 303
    destination = hop.headers["location"]
    assert destination.startswith("/me?token=")
    assert "covering" in unquote(destination)

    identity = parse_qs(urlparse(destination).query)["token"][0]
    assert parse_identity_token(identity, settings.coverage_link_secret).employee_id == (
        short_staffed.employee_id("luis")
    )

    assert client.get(f"/coverage/{request.id}").json()["status"] == "filled"
    assert client.get(f"/tasks/{request.task_id}").json()["assignee_id"] == (
        short_staffed.employee_id("luis")
    )


def test_a_tampered_link_is_refused(client, settings):
    bad = make_token(1, "accept", "not-the-real-secret", 60)
    page = client.get("/coverage/respond", params={"token": bad})
    assert page.status_code == 400
    assert "not valid" in page.text.lower()


def test_an_expired_link_is_refused(client, settings):
    stale = make_token(1, "accept", settings.coverage_link_secret, 60, now=time.time() - 7200)
    page = client.get("/coverage/respond", params={"token": stale})
    assert page.status_code == 400


def test_replying_for_someone_who_was_never_asked_is_a_404(client, session, short_staffed):
    client.post(
        "/leave-requests",
        json={
            "employee_id": short_staffed.employee_id("mai"),
            "starts_at": iso(at(14, 30)),
            "ends_at": iso(at(18, 0)),
        },
    )
    request = session.scalars(
        select(CoverageRequest).where(CoverageRequest.status == CoverageStatus.OPEN)
    ).one()
    response = client.post(
        f"/coverage/{request.id}/reply",
        json={"employee_id": short_staffed.employee_id("priya"), "accept": True},
    )
    assert response.status_code == 404


def test_the_sweep_can_be_run_on_demand(client, session, short_staffed, clock):
    client.post(
        "/leave-requests",
        json={
            "employee_id": short_staffed.employee_id("mai"),
            "starts_at": iso(at(14, 30)),
            "ends_at": iso(at(18, 0)),
        },
    )
    clock.set(short_staffed.orders["1043"].pickup_at - timedelta(minutes=5))
    body = client.post("/coverage/sweep").json()
    assert body["decisions"], "an unanswered request this close to pickup must escalate"


# --- guarding the endpoints -------------------------------------------------


def test_clocking_in_twice_is_refused(client, world):
    payload = {"employee_id": world.employee_id("luis"), "at": iso(at(14, 0))}
    assert client.post("/timeclock/clock-in", json=payload).status_code == 201
    assert client.post("/timeclock/clock-in", json=payload).status_code == 409


def test_leave_must_end_after_it_starts(client, world):
    response = client.post(
        "/leave-requests",
        json={
            "employee_id": world.employee_id("mai"),
            "starts_at": iso(at(15, 0)),
            "ends_at": iso(at(14, 0)),
        },
    )
    assert response.status_code == 400


def test_preview_shows_the_plan_without_acting_on_it(client, session, world):
    created = client.post(
        "/leave-requests",
        json={
            "employee_id": world.employee_id("luis"),
            "starts_at": iso(at(20, 0)),
            "ends_at": iso(at(22, 0)),
        },
    ).json()
    before = client.get("/decisions").json()

    plan = client.post(f"/leave-requests/{created['leave']['id']}/preview").json()
    assert "task_plans" in plan
    assert client.get("/decisions").json() == before, "a preview must not decide anything"


def test_slack_callback_refuses_an_unsigned_request(client, settings):
    settings.slack_signing_secret = "shhh"
    payload = {"actions": [{"action_id": "coverage_accept_1", "value": "1"}]}
    response = client.post(
        "/slack/interactivity",
        content=urlencode({"payload": json.dumps(payload)}),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": str(int(time.time())),
            "X-Slack-Signature": "v0=obviously-wrong",
        },
    )
    assert response.status_code == 401


def test_slack_callback_accepts_a_properly_signed_request(client, session, settings, short_staffed):
    settings.slack_signing_secret = "shhh"
    client.post(
        "/leave-requests",
        json={
            "employee_id": short_staffed.employee_id("mai"),
            "starts_at": iso(at(14, 30)),
            "ends_at": iso(at(18, 0)),
        },
    )
    request = session.scalars(
        select(CoverageRequest).where(CoverageRequest.status == CoverageStatus.OPEN)
    ).one()
    offer = next(o for o in request.offers if o.employee_id == short_staffed.employee_id("luis"))

    payload = {
        "user": {"id": "U_LUIS"},
        "actions": [{"action_id": f"coverage_accept_{offer.id}", "value": str(offer.id)}],
    }
    body = urlencode({"payload": json.dumps(payload)}).encode()
    timestamp = str(int(time.time()))
    signature = (
        "v0="
        + hmac.new(
            settings.slack_signing_secret.encode(),
            b"v0:" + timestamp.encode() + b":" + body,
            hashlib.sha256,
        ).hexdigest()
    )

    response = client.post(
        "/slack/interactivity",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": signature,
        },
    )
    assert response.status_code == 200
    assert "covering" in response.json()["text"]
    assert client.get(f"/coverage/{request.id}").json()["filled_by_id"] == (
        short_staffed.employee_id("luis")
    )


def test_slack_callback_will_not_let_one_person_accept_for_another(
    client, session, settings, short_staffed
):
    settings.slack_signing_secret = "shhh"
    client.post(
        "/leave-requests",
        json={
            "employee_id": short_staffed.employee_id("mai"),
            "starts_at": iso(at(14, 30)),
            "ends_at": iso(at(18, 0)),
        },
    )
    request = session.scalars(
        select(CoverageRequest).where(CoverageRequest.status == CoverageStatus.OPEN)
    ).one()
    offer = next(o for o in request.offers if o.employee_id == short_staffed.employee_id("luis"))

    payload = {
        "user": {"id": "U_SOMEONE_ELSE"},
        "actions": [{"action_id": f"coverage_accept_{offer.id}", "value": str(offer.id)}],
    }
    body = urlencode({"payload": json.dumps(payload)}).encode()
    timestamp = str(int(time.time()))
    signature = (
        "v0="
        + hmac.new(
            settings.slack_signing_secret.encode(),
            b"v0:" + timestamp.encode() + b":" + body,
            hashlib.sha256,
        ).hexdigest()
    )
    response = client.post(
        "/slack/interactivity",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": signature,
        },
    )
    assert "addressed to someone else" in response.json()["text"]
    assert client.get(f"/coverage/{request.id}").json()["status"] == "open"
