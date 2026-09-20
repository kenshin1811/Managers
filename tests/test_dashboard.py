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
from app.models.employee import Employee
from app.models.enums import CoverageStatus
from app.models.state import SWEEP_HEARTBEAT
from app.scheduler import SWEEP_SECONDS
from app.state import touch
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


def test_every_timestamp_carries_utc_and_a_local_rendering(client, planned):
    board = client.get("/api/dashboard").json()["board"]
    assert board, "the planned evening has live work"
    stamp = board[0]["starts_at"]
    # The browser does the arithmetic in UTC and prints the local string; a
    # naive ISO string without the Z would be read as browser-local time.
    assert stamp["utc"].endswith("Z")
    assert len(stamp["local"]) == 5


def test_the_board_names_the_person_holding_each_job(client, session, world):
    a_pack_job(session, world)
    session.commit()

    board = client.get("/api/dashboard").json()["board"]
    pack = next(t for t in board if t["title"] == "Pack and label 90 jam donut")
    assert pack["assignee"] == "Shaleen"
    assert pack["order_code"] == "D1042"
    assert pack["pickup_at"]["local"] == "18:00"
    assert pack["stage"] == "pack"
    assert pack["run"] == "north"


# --- the production block ---------------------------------------------------


def test_the_vans_are_listed_in_the_order_they_leave(client, planned):
    runs = client.get("/api/dashboard").json()["production"]["runs"]
    assert [r["run"] for r in runs] == ["internal", "north", "east", "south", "city"]
    assert [r["departs_at"]["local"] for r in runs] == [
        "17:30",
        "18:00",
        "19:15",
        "20:00",
        "20:45",
    ]
    assert all(r["status"] == "on_time" for r in runs), "three packers can do this evening"


def test_each_van_carries_its_stops_and_who_is_working_on_it(client, planned):
    north = next(
        r for r in client.get("/api/dashboard").json()["production"]["runs"] if r["run"] == "north"
    )
    assert north["stops"] == ["Fitzroy shopfront, Fitzroy", "Brunswick shopfront, Brunswick"]
    assert north["people"], "a van nobody is working on is the thing to notice"
    assert north["outstanding"] < north["ordered"], "the north run is part packed already"


def test_the_pipeline_is_reported_stage_by_stage(client, planned):
    production = client.get("/api/dashboard").json()["production"]
    assert list(production["stages"]) == ["retrieve", "pack", "sort", "dispatch"]
    assert production["stages"]["pack"]["label"] == "Pack and label"
    assert all(stage["open"] > 0 for stage in production["stages"].values())

    # What is left is what the floor actually asks about.
    assert 0 < production["units_outstanding"] < production["units_ordered"]
    remaining = production["remaining"]
    assert remaining[0]["units"] >= remaining[-1]["units"], "biggest job first"
    assert sum(item["units"] for item in remaining) == production["units_outstanding"]


def test_every_shop_is_reported_line_by_line(client, planned):
    """The other half of the job: the right product at the right address."""
    production = client.get("/api/dashboard").json()["production"]
    deliveries = production["deliveries"]

    assert len(deliveries) == 9
    assert [d["departs_at"]["utc"] for d in deliveries] == sorted(
        d["departs_at"]["utc"] for d in deliveries
    ), "soonest van first"

    fitzroy = next(d for d in deliveries if d["where"].startswith("Fitzroy"))
    assert fitzroy["run"] == "north"
    assert fitzroy["channel"] == "retail"
    assert {line["product"] for line in fitzroy["lines"]} == {
        "Jam donut",
        "Chocolate ring",
        "Long John",
    }
    assert fitzroy["short"] == sum(line["short"] for line in fitzroy["lines"])
    assert all(line["packed"] + line["short"] == line["ordered"] for line in fitzroy["lines"])
    assert fitzroy["lines"][0]["short"] >= fitzroy["lines"][-1]["short"], "biggest gap first"


def test_a_shop_is_only_safe_when_its_van_is_too(client, session, settings, planned, notifier, now):
    """Packed but riding a van that will not leave on time is not delivered."""
    from app.engine import commands

    before = client.get("/api/dashboard").json()["production"]
    assert all(d["status"] == "on_time" for d in before["deliveries"] if not d["complete"])

    commands.handle(session, settings, now, "Shaleen is off sick", notifier)
    session.commit()

    after = client.get("/api/dashboard").json()["production"]
    stranded = [d for d in after["deliveries"] if not d["complete"] and d["status"] == "missed"]
    assert stranded, "a shop on a late van has to read as late itself"


def test_the_short_count_is_the_number_a_manager_would_ask_for(client, planned):
    production = client.get("/api/dashboard").json()["production"]
    expected = sum(1 for d in production["deliveries"] if d["short"])
    assert production["shops_short"] == expected
    assert 0 < production["shops_short"] <= len(production["deliveries"])


def test_losing_a_packer_shows_up_as_vans_running_late(
    client, session, settings, planned, notifier, now
):
    from app.engine import commands

    before = client.get("/api/dashboard").json()["production"]["runs"]
    assert all(r["status"] == "on_time" for r in before)

    commands.handle(session, settings, now, "Shaleen is off sick", notifier)
    session.commit()

    after = client.get("/api/dashboard").json()["production"]["runs"]
    assert any(r["status"] != "on_time" for r in after), (
        "taking a third of the floor away has to move something"
    )
    assert any(r["late_minutes"] > 0 for r in after)


def test_staff_carry_their_hours_against_the_limits(client, world):
    staff = client.get("/api/dashboard").json()["staff"]
    marlo = next(p for p in staff if p["name"] == "Marlo")
    assert marlo["hours_week"] == pytest.approx(48.0, abs=0.5)
    assert marlo["on_shift"] is False
    ken = next(p for p in staff if p["name"] == "Ken")
    assert ken["on_shift"] is True


def test_a_cover_request_appears_with_everyone_who_was_asked(
    client, session, settings, world, notifier, now
):
    from app.engine import reassignment

    nobody_on_shift_can_pack(session, world)
    a_pack_job(session, world)
    session.commit()
    leave = file_leave(session, world, now, board=False)
    reassignment.handle_leave_request(session, leave, notifier, settings, now)
    session.commit()

    body = client.get("/api/dashboard").json()
    assert body["counters"]["open_coverage"] == 1
    cover = body["coverage"][0]
    assert cover["status"] == CoverageStatus.OPEN
    assert cover["task_title"] == "Pack and label 90 jam donut"
    # Tavi is asked first: Valentino covered twice recently.
    assert [o["employee_name"] for o in cover["offers"]] == ["Tavi", "Valentino"]
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


def test_demo_reset_builds_the_factory_around_right_now(client, session, clock):
    body = client.post("/api/demo/reset", params={"variant": "short_team"}).json()
    assert body["employees"] == 7
    assert body["orders"] == 9
    assert body["units"] == 2250
    assert body["tasks"] > 0, "seeding lays out the orders; the agent plans the evening"

    from app.models.order import Order

    order = session.scalar(select(Order).where(Order.code == "D1042"))
    minutes_away = (order.pickup_at - clock.now()).total_seconds() / 60
    assert 80 < minutes_away < 100, (
        "the next van has to be close for the countdown to mean anything"
    )


def test_demo_reset_plans_the_evening_rather_than_shipping_a_board(client):
    """A pre-planned seed would hide the part worth watching."""
    body = client.post("/api/demo/reset", params={"variant": "full_team"}).json()
    assert body["feasible"] is True
    assert body["tasks"] > 40, "every run needs pulling, packing, sorting and loading"


def test_demo_reset_keeps_the_heartbeat(client, session, now):
    """Wiping the factory must not make the agent look dead."""
    touch(session, SWEEP_HEARTBEAT, now - timedelta(seconds=5))
    session.commit()
    client.post("/api/demo/reset")
    assert client.get("/api/dashboard").json()["agent"]["status"] == "active"


def test_demo_reset_rejects_an_unknown_variant(client):
    assert client.post("/api/demo/reset", params={"variant": "nonsense"}).status_code == 400


def test_demo_disrupt_sets_the_engine_off(client, session):
    client.post("/api/demo/reset", params={"variant": "full_team"})
    body = client.post("/api/demo/disrupt").json()

    assert body["ok"] is True
    # It says what it did out loud, because the microphone path says it too.
    assert "Shaleen" in body["speech"]
    assert body["detail"]["understood_as"] == "staff_off"


def test_demo_disrupt_goes_through_the_same_route_as_the_microphone(client, session):
    """The button is only evidence if it takes the path a spoken order takes."""
    client.post("/api/demo/reset", params={"variant": "full_team"})
    client.post("/api/demo/disrupt")

    from app.models.audit import DecisionRecord

    logged = session.scalars(
        select(DecisionRecord).where(DecisionRecord.actor == "manager:console")
    ).all()
    assert logged, "every command reaches the audit log, however it was given"


def test_demo_disrupt_needs_a_factory_first(client, session):
    session.execute(Employee.__table__.delete())
    session.commit()
    assert client.post("/api/demo/disrupt").status_code == 409


def test_demo_disrupt_refuses_to_send_shaleen_home_twice(client):
    client.post("/api/demo/reset")
    assert client.post("/api/demo/disrupt").status_code == 200
    assert client.post("/api/demo/disrupt").status_code == 409


# --- the page itself --------------------------------------------------------


def test_the_dashboard_page_is_served_at_the_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Packing floor" in response.text


def test_the_static_assets_are_served(client):
    assets = (
        ("/static/styles.css", "css"),
        ("/static/app.js", "javascript"),
        ("/static/console.js", "javascript"),
    )
    for path, kind in assets:
        response = client.get(path)
        assert response.status_code == 200, path
        assert kind in response.headers["content-type"]
