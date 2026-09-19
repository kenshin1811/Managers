"""The recorded walkthrough.

A recording that quietly drifts from the engine is worse than no recording at
all: it would show a reader decisions the system no longer makes. These tests
hold it to the engine's actual behaviour.
"""

from __future__ import annotations

import json

import pytest

from app.sim import capture as capture_mod
from app.sim.seed import at
from tools import build_replay

SECTIONS = ("agent", "counters", "coverage", "board", "feed", "staff")


@pytest.fixture(scope="module")
def recording() -> dict:
    return capture_mod.capture(anchor=at(14, 5))


def test_every_frame_is_a_complete_dashboard_payload(recording):
    assert recording["frames"], "nothing was recorded"
    for key, frame in recording["frames"].items():
        missing = [section for section in SECTIONS if section not in frame]
        assert not missing, f"{key} is missing {missing}"
        assert frame["_caption"], f"{key} has no caption"


def test_the_on_shift_kitchen_moves_the_job_without_asking_anyone(recording):
    before = recording["frames"]["onshift.0"]
    after = recording["frames"]["onshift.1"]

    pack_before = next(t for t in before["board"] if t["title"] == "Pack order 1043")
    pack_after = next(t for t in after["board"] if t["title"] == "Pack order 1043")
    assert pack_before["assignee"] == "Mai Tran"
    assert pack_after["assignee"] == "Dev Osei"
    assert after["counters"]["auto_reassigned"] >= 1
    assert after["counters"]["open_coverage"] == 0, "nobody's day off should be interrupted"


def test_the_call_in_kitchen_asks_the_two_eligible_packers(recording):
    frame = recording["frames"]["callin.1"]
    open_requests = [c for c in frame["coverage"] if c["status"] == "open"]
    assert len(open_requests) == 1
    assert [o["employee_name"] for o in open_requests[0]["offers"]] == [
        "Luis Ferrer",
        "Sam Whitlock",
    ]


def test_accepting_moves_the_job_and_clears_the_request(recording):
    frame = recording["frames"]["callin.1.luis-accept"]
    pack = next(t for t in frame["board"] if t["title"] == "Pack order 1043")
    assert pack["assignee"] == "Luis Ferrer"
    assert frame["counters"]["open_coverage"] == 0
    assert frame["counters"]["coverage_filled"] >= 1


def test_both_declining_escalates_rather_than_going_quiet(recording):
    frame = recording["frames"]["callin.1.luis-decline.sam-decline"]
    assert frame["counters"]["needs_manager"] >= 1
    escalations = [d for d in frame["feed"] if d["action"] == "escalated"]
    assert escalations, "somebody has to be told"
    assert any("weekly hours limit" in reason for reason in escalations[0]["blockers"])


def test_the_stalled_frame_actually_reads_as_stalled(recording):
    assert recording["frames"]["stalled"]["agent"]["status"] == "stalled"


def test_every_recorded_transition_lands_on_a_recorded_frame(recording):
    """A dead link here is a button that does nothing on the published page."""
    frames = recording["frames"]
    for target in recording["start"].values():
        assert target in frames
    for source, target in recording["leave"].items():
        assert source in frames and target in frames
    for source, moves in recording["replies"].items():
        assert source in frames
        for key, move in moves.items():
            assert move["next"] in frames, f"{source} --{key}--> {move['next']} is missing"
            assert move["message"], f"{source} --{key}--> has no message"


def test_both_replies_are_offered_to_everyone_still_waiting(recording):
    """Whatever a reader clicks has to go somewhere real."""
    for key, frame in recording["frames"].items():
        pending = [
            offer
            for cover in frame["coverage"]
            if cover["status"] == "open"
            for offer in cover["offers"]
            if offer["status"] == "sent"
        ]
        if not pending or key == "stalled":
            continue
        moves = recording["replies"].get(key, {})
        for offer in pending:
            for action in ("accept", "decline"):
                assert f"{action}:{offer['employee_id']}" in moves, (
                    f"{key}: no recorded outcome for {offer['employee_name']} to {action}"
                )


def test_recording_the_same_moment_twice_gives_the_same_result():
    """Otherwise a rebuild would churn the published page for no reason."""
    first = capture_mod.capture(anchor=at(14, 5))
    second = capture_mod.capture(anchor=at(14, 5))
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# --- the assembled page -----------------------------------------------------


def test_the_built_page_carries_the_dashboard_unchanged():
    page = build_replay.build()
    styles = (build_replay.WEB / "styles.css").read_text()
    app_js = (build_replay.WEB / "app.js").read_text()

    # Not a copy of the dashboard -- the dashboard itself.
    assert styles.strip() in page
    assert app_js.strip() in page
    assert '<script id="replay-data"' in page
    assert f"<title>{build_replay.TITLE}</title>" in page


def test_the_built_page_asks_the_server_for_nothing():
    """A published page cannot fetch from a machine that isn't there."""
    page = build_replay.build()
    assert "/static/" not in page
    # The host supplies the document shell; a second one would nest documents.
    for tag in ("<!doctype", "<html", "<head>", "<body>"):
        assert tag not in page.lower()


def test_the_built_page_says_what_it_is():
    page = build_replay.build()
    assert "Recorded walkthrough" in page
    assert "not talking to a live server" in page
