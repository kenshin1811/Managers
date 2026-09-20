"""The dashboard payload and the demo switch.

The page's whole job is to answer "is the agent working?", so the heartbeat
states get more attention here than the prettier parts.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.deps import notifier_dep, now_dep, settings_dep
from app.db import get_session
from app.main import create_app
from app.models.coverage import CoverageRequest
from app.models.employee import Employee
from app.models.enums import CoverageStatus
from app.models.state import SWEEP_HEARTBEAT
from app.scheduler import SWEEP_SECONDS
from app.state import touch
from tests.test_reassignment import file_leave


@pytest.fixture
def client(session, settings, notifier, clock):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[settings_dep] = lambda: settings
    app.dependency_overrides[notifier_dep] = lambda: notifier
    app.dependency_overrides[now_dep] = lambda: clock.now()
    return TestClient(app)


# --- the heartbeat ----------------------------------------------------------


def test_agent_reads_as_starting_before_the_first_sweep(client):
    agent = client.get("/api/dashboard").json()["agent"]
    assert agent["status"] == "starting"
    assert agent["last_sweep"] is None
    assert agent["seconds_since_sweep"] is None


def test_a_recent_sweep_means_the_agent_is_active(client, session, now):
    touch(session, SWEEP_HEARTBEAT, now - timedelta(seconds=5))
    session.commit()
    agent = client.get("/api/dashboard").json()["agent"]
    assert agent["status"] == "active"
    assert agent["seconds_since_sweep"] == 5


def test_a_stale_heartbeat_is_reported_as_stalled(client, session, now):
    """Several missed sweeps is not quiet, it is stopped -- and must say so."""
    touch(session, SWEEP_HEARTBEAT, now - timedelta(seconds=SWEEP_SECONDS * 4))
    session.commit()
    agent = client.get("/api/dashboard").json()["agent"]
    assert agent["status"] == "stalled"


def test_the_agent_block_reports_the_autonomy_settings(client, settings):
    settings.auto_coverage_requests_enabled = False
    agent = client.get("/api/dashboard").json()["agent"]
    assert agent["auto_reassign"] is True
    assert agent["auto_coverage_requests"] is False
    assert agent["messaging"] == "Dry run"
    assert agent["limits"]["weekly_hours"] == settings.max_weekly_hours


# --- the rest of the payload ------------------------------------------------


def test_every_timestamp_carries_utc_and_a_local_rendering(client, world):
    board = client.get("/api/dashboard").json()["board"]
    assert board, "the seeded kitchen has live work"
    stamp = board[0]["starts_at"]
    # The browser does the arithmetic in UTC and prints the local string; a
    # naive ISO string without the Z would be read as browser-local time.
    assert stamp["utc"].endswith("Z")
    assert len(stamp["local"]) == 5


def test_the_board_names_the_person_holding_each_job(client, world):
    board = client.get("/api/dashboard").json()["board"]
    pack = next(t for t in board if t["title"] == "Pack order 1043")
    assert pack["assignee"] == "Shaleen"
    assert pack["order_code"] == "1043"
    assert pack["pickup_at"]["local"] == "14:50"


def test_staff_carry_their_hours_against_the_limits(client, world):
    staff = client.get("/api/dashboard").json()["staff"]
    tomas = next(p for p in staff if p["name"] == "Marlo")
    assert tomas["hours_week"] == pytest.approx(48.0, abs=0.5)
    assert tomas["on_shift"] is False
    dev = next(p for p in staff if p["name"] == "Ken")
    assert dev["on_shift"] is True


def test_a_cover_request_appears_with_everyone_who_was_asked(
    client, session, settings, short_staffed, notifier, now
):
    leave = file_leave(session, short_staffed, now)
    from app.engine import reassignment

    reassignment.handle_leave_request(session, leave, notifier, settings, now)
    session.commit()

    body = client.get("/api/dashboard").json()
    assert body["counters"]["open_coverage"] == 1
    cover = body["coverage"][0]
    assert cover["status"] == CoverageStatus.OPEN
    assert cover["task_title"] == "Pack order 1043"
    assert [o["employee_name"] for o in cover["offers"]] == ["Valentino", "Tavi"]
    assert all(o["status"] == "sent" for o in cover["offers"])


def test_the_feed_carries_the_reasons_people_were_ruled_out(
    client, session, settings, world, notifier, now
):
    from app.engine import reassignment

    leave = file_leave(session, world, now)
    reassignment.handle_leave_request(session, leave, notifier, settings, now)
    session.commit()

    feed = client.get("/api/dashboard").json()["feed"]
    entry = next(d for d in feed if d["action"] == "auto_reassigned")
    assert entry["chosen"]
    assert entry["considered"] > 1
    assert any("not trained" in r["reason"] for r in entry["rejected"])


def test_counters_only_count_today(client, session, settings, world, notifier, now):
    from app.engine import reassignment

    leave = file_leave(session, world, now)
    reassignment.handle_leave_request(session, leave, notifier, settings, now)
    session.commit()

    counters = client.get("/api/dashboard").json()["counters"]
    assert counters["auto_reassigned"] >= 1
    assert counters["decisions_today"] >= counters["auto_reassigned"]


# --- the demo switch --------------------------------------------------------


def test_demo_is_offered_only_in_dry_run(client, settings):
    assert client.get("/api/demo/variants").json()["available"] is True
    settings.dry_run = False
    assert client.get("/api/demo/variants").json()["available"] is False


def test_demo_refuses_to_wipe_a_live_system(client, settings):
    settings.dry_run = False
    response = client.post("/api/demo/reset")
    assert response.status_code == 403
    assert "DRY_RUN" in response.json()["detail"]


def test_demo_reset_builds_the_kitchen_around_right_now(client, session, clock):
    body = client.post("/api/demo/reset", params={"variant": "call_in"}).json()
    assert body["employees"] == 8

    from app.models.order import Order

    order = session.scalar(select(Order).where(Order.code == "1043"))
    minutes_away = (order.pickup_at - clock.now()).total_seconds() / 60
    assert 40 < minutes_away < 50, (
        "the driver has to be arriving soon for the demo to mean anything"
    )


def test_demo_reset_keeps_the_heartbeat(client, session, now):
    """Wiping the kitchen must not make the agent look dead."""
    touch(session, SWEEP_HEARTBEAT, now - timedelta(seconds=5))
    session.commit()
    client.post("/api/demo/reset")
    assert client.get("/api/dashboard").json()["agent"]["status"] == "active"


def test_demo_reset_rejects_an_unknown_variant(client):
    assert client.post("/api/demo/reset", params={"variant": "nonsense"}).status_code == 400


def test_demo_leave_sets_the_engine_off(client, session):
    client.post("/api/demo/reset", params={"variant": "call_in"})
    body = client.post("/api/demo/leave").json()

    assert body["leave_status"] == "pending_coverage"
    actions = {p["action"] for p in body["plan"]["task_plans"]}
    assert "coverage_requested" in actions
    assert session.scalars(
        select(CoverageRequest).where(CoverageRequest.status == CoverageStatus.OPEN)
    ).first()


def test_demo_leave_on_shift_variant_reassigns_instead_of_asking(client):
    client.post("/api/demo/reset", params={"variant": "on_shift"})
    body = client.post("/api/demo/leave").json()
    assert body["leave_status"] == "approved"
    assert all(p["action"] == "auto_reassigned" for p in body["plan"]["task_plans"])


def test_demo_leave_needs_a_kitchen_first(client, session):
    session.execute(Employee.__table__.delete())
    session.commit()
    assert client.post("/api/demo/leave").status_code == 409


def test_demo_leave_refuses_to_send_mai_home_twice(client):
    client.post("/api/demo/reset")
    assert client.post("/api/demo/leave").status_code == 200
    assert client.post("/api/demo/leave").status_code == 409


# --- the page itself --------------------------------------------------------


def test_the_dashboard_page_is_served_at_the_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Managers" in response.text


def test_the_static_assets_are_served(client):
    for path, kind in (("/static/styles.css", "css"), ("/static/app.js", "javascript")):
        response = client.get(path)
        assert response.status_code == 200, path
        assert kind in response.headers["content-type"]
