"""One employee's own view, and the line around it.

Most of these are about what is *not* in the payload. A page that quietly
hands every employee the whole roster's hours is not a smaller dashboard, it
is a leak, and the difference is invisible until somebody looks.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.deps import notifier_dep, now_dep, settings_dep
from app.db import get_session
from app.engine import coverage as coverage_engine
from app.engine import reassignment
from app.main import create_app
from app.models.coverage import CoverageOffer, CoverageRequest
from app.models.enums import CoverageStatus
from app.tokens import make_identity_token, make_token
from tests.conftest import a_pack_job
from tests.test_reassignment import file_leave, nobody_on_shift_can_pack


@pytest.fixture
def client(session, settings, notifier, clock):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[settings_dep] = lambda: settings
    app.dependency_overrides[notifier_dep] = lambda: notifier
    app.dependency_overrides[now_dep] = lambda: clock.now()
    return TestClient(app)


@pytest.fixture
def asked(client, session, settings, world, notifier, now):
    """A live cover request for Shaleen's job, with Tavi and Valentino asked."""
    nobody_on_shift_can_pack(session, world)
    a_pack_job(session, world)
    session.commit()
    leave = file_leave(session, world, now, board=False)
    reassignment.handle_leave_request(session, leave, notifier, settings, now)
    session.commit()
    return world


def view(client, world, key: str, **params):
    return client.get(f"/api/me/{world.employee_id(key)}", params=params)


# --- the boundary -----------------------------------------------------------


def test_my_page_says_nothing_about_my_colleagues(client, asked):
    body = view(client, asked, "valentino").json()
    blob = json.dumps(body)

    for colleague in ("Tavi", "Marlo", "Jasoo", "Ken"):
        assert colleague not in blob, f"{colleague} should not appear on Valentino's own page"

    # The dashboard's sections, by name, must not have followed it over.
    for leaked in ("staff", "feed", "counters", "board"):
        assert leaked not in body


def test_the_other_candidates_scores_do_not_travel(client, asked):
    """Who else was asked, and how they ranked, is not the asked person's business."""
    blob = json.dumps(view(client, asked, "valentino").json())
    assert "score" not in blob
    assert "fit" not in blob


def test_i_only_see_cover_requests_addressed_to_me(client, asked, session):
    valentino = view(client, asked, "valentino").json()
    marlo = view(client, asked, "marlo").json()

    titles = [r["task_title"] for r in valentino["cover_requests"]]
    assert titles == ["Pack and label 90 jam donut"]
    assert valentino["cover_requests"][0]["my_status"] == "sent"
    assert marlo["cover_requests"] == [], "Marlo is over his hours, so he sees nothing"


def test_my_hours_are_the_ones_the_guardrails_read(client, asked, settings):
    """Not a separate calculation -- the same figures, or the page is lying."""
    marlo = view(client, asked, "marlo").json()
    assert marlo["hours"]["week"] == pytest.approx(48.0, abs=0.5)
    assert marlo["hours"]["weekly_hours"] == settings.max_weekly_hours
    assert marlo["cover_requests"] == [], "he is over the limit, so he was not asked"


def test_the_agent_block_is_the_same_one_the_dashboard_shows(client, asked, session, now):
    from app.models.state import SWEEP_HEARTBEAT
    from app.state import touch

    touch(session, SWEEP_HEARTBEAT, now)
    session.commit()

    mine = view(client, asked, "valentino").json()["agent"]
    theirs = client.get("/api/dashboard").json()["agent"]
    assert mine["status"] == theirs["status"] == "active"
    assert mine["sweep_interval_seconds"] == theirs["sweep_interval_seconds"]


# --- what is mine to know ---------------------------------------------------


def test_i_can_see_who_picked_up_my_work(client, asked, session, settings, notifier, now):
    request = session.scalars(
        select(CoverageRequest).where(CoverageRequest.status == CoverageStatus.OPEN)
    ).one()
    offer = next(o for o in request.offers if o.employee_id == asked.employee_id("valentino"))
    coverage_engine.accept_offer(session, offer, notifier, settings, now)

    shaleen = view(client, asked, "shaleen").json()
    taken = {(h["task_title"], h["taken_by"]) for h in shaleen["handovers"]}
    assert ("Pack and label 90 jam donut", "Valentino") in taken
    assert any(h["how"] == "volunteered" for h in shaleen["handovers"])
    assert shaleen["leave"][0]["status"] == "approved"


def test_my_jobs_carry_the_driver_time(client, session, world):
    a_pack_job(session, world)
    session.commit()

    shaleen = view(client, world, "shaleen").json()
    pack = next(t for t in shaleen["tasks"] if t["title"] == "Pack and label 90 jam donut")
    assert pack["order_code"] == "D1042"
    assert pack["pickup_at"]["local"] == "18:00"


def test_being_on_the_clock_means_an_open_time_entry(client, world):
    """Not being rostered -- somebody can be scheduled and not have arrived."""
    assert view(client, world, "shaleen").json()["hours"]["on_the_clock"] is True
    assert view(client, world, "valentino").json()["hours"]["on_the_clock"] is False


def test_an_unknown_employee_is_a_404(client):
    assert client.get("/api/me/9999").status_code == 404


# --- identity ---------------------------------------------------------------


def test_the_person_picker_is_a_dry_run_convenience(client, settings, world):
    assert len(client.get("/api/me/switchable").json()) == 7
    settings.dry_run = False
    response = client.get("/api/me/switchable")
    assert response.status_code == 403
    assert "link we sent you" in response.json()["detail"]


def test_outside_dry_run_a_bare_id_gets_you_nothing(client, settings, world):
    settings.dry_run = False
    response = view(client, world, "valentino")
    assert response.status_code == 401


def test_a_tampered_identity_link_is_refused(client, settings, world):
    settings.dry_run = False
    bad = make_identity_token(world.employee_id("valentino"), "not-the-real-secret", 60)
    assert view(client, world, "valentino", token=bad).status_code == 401


def test_an_expired_identity_link_is_refused(client, settings, world):
    settings.dry_run = False
    stale = make_identity_token(
        world.employee_id("valentino"), settings.coverage_link_secret, 60, now=time.time() - 7200
    )
    assert view(client, world, "valentino", token=stale).status_code == 401


def test_a_valid_link_opens_only_its_own_page(client, settings, world):
    settings.dry_run = False
    token = make_identity_token(world.employee_id("valentino"), settings.coverage_link_secret, 60)
    assert view(client, world, "valentino", token=token).status_code == 200

    # The same link pointed at somebody else's id is refused, not quietly
    # redirected to its owner -- an employee should not be able to read a
    # colleague's schedule by editing a number in the address bar.
    stolen = client.get(f"/api/me/{world.employee_id('shaleen')}", params={"token": token})
    assert stolen.status_code == 403


def test_an_accept_link_is_not_a_pass_to_somebody_schedule(client, settings, world):
    """The two token kinds are signed the same way; only the action separates them."""
    settings.dry_run = False
    accept = make_token(1, "accept", settings.coverage_link_secret, 60)
    assert view(client, world, "valentino", token=accept).status_code == 401


def test_whoami_gives_back_an_id_and_nothing_else(client, settings, world):
    token = make_identity_token(world.employee_id("tavi"), settings.coverage_link_secret, 60)
    body = client.get("/api/me/whoami", params={"token": token}).json()
    assert body == {"employee_id": world.employee_id("tavi")}


def test_whoami_refuses_a_coverage_token(client, settings):
    accept = make_token(1, "accept", settings.coverage_link_secret, 60)
    assert client.get("/api/me/whoami", params={"token": accept}).status_code == 401


# --- the page ---------------------------------------------------------------


def test_the_employee_page_is_served(client):
    response = client.get("/me")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "My shift" in response.text


def test_replying_from_my_own_page_moves_the_job(client, asked, session):
    request = session.scalars(
        select(CoverageRequest).where(CoverageRequest.status == CoverageStatus.OPEN)
    ).one()
    result = client.post(
        f"/coverage/{request.id}/reply",
        json={"employee_id": asked.employee_id("valentino"), "accept": True},
    ).json()
    assert result["ok"]

    valentino = view(client, asked, "valentino").json()
    assert [t["title"] for t in valentino["tasks"]] == ["Pack and label 90 jam donut"]
    assert valentino["cover_requests"][0]["my_status"] == "accepted"


def test_an_offer_i_already_answered_still_shows_what_happened(
    client, asked, session, settings, notifier, now
):
    request = session.scalars(
        select(CoverageRequest).where(CoverageRequest.status == CoverageStatus.OPEN)
    ).one()
    mine = session.scalars(
        select(CoverageOffer).where(
            CoverageOffer.coverage_request_id == request.id,
            CoverageOffer.employee_id == asked.employee_id("valentino"),
        )
    ).one()
    coverage_engine.decline_offer(session, mine, notifier, settings, now)

    valentino = view(client, asked, "valentino").json()
    assert valentino["cover_requests"][0]["my_status"] == "declined"
    assert valentino["tasks"] == []
