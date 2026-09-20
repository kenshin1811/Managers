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

SECTIONS = ("agent", "counters", "production", "coverage", "board", "feed", "staff")


@pytest.fixture(scope="module")
def recording() -> dict:
    return capture_mod.capture(anchor=at(16, 30))


def test_every_frame_is_a_complete_dashboard_payload(recording):
    assert recording["frames"], "nothing was recorded"
    for key, frame in recording["frames"].items():
        missing = [section for section in SECTIONS if section not in frame]
        assert not missing, f"{key} is missing {missing}"
        assert frame["_caption"], f"{key} has no caption"


def test_the_evening_starts_out_fitting(recording):
    """If the opening frame were already late, nothing after it would read."""
    runs = recording["frames"]["floor.0"]["production"]["runs"]
    assert len(runs) == 5
    assert all(run["status"] == "on_time" for run in runs)
    assert recording["frames"]["floor.0"]["production"]["units_outstanding"] > 0


# --- the console ------------------------------------------------------------


def test_every_recorded_command_lands_on_a_recorded_frame(recording):
    """A dead phrase here is a suggestion that does nothing on the page."""
    frames = recording["frames"]
    for source, moves in recording["commands"].items():
        assert source in frames
        for said, move in moves.items():
            assert move["next"] in frames, f'{source} --"{said}"--> {move["next"]} is missing'
            assert move["speech"], f'{source} --"{said}"--> says nothing back'


def test_the_hints_on_the_page_are_exactly_what_was_recorded(recording):
    """Offering a phrase the recording cannot answer is worse than offering none."""
    somewhere = set()
    for moves in recording["commands"].values():
        somewhere.update(moves)
    for example in recording["vocabulary"]["examples"]:
        assert capture_mod.normalise(example) in somewhere, example


def test_asking_how_it_is_going_changes_nothing(recording):
    key = capture_mod.normalise(capture_mod.ASK_STATUS)
    for source, moves in recording["commands"].items():
        if key in moves:
            assert moves[key]["next"] == source, "a question must not move the walkthrough"


def test_a_late_order_shows_up_on_the_run_it_was_added_to(recording):
    before = recording["frames"]["floor.0"]["production"]
    after = recording["frames"]["floor.more"]["production"]
    assert after["units_ordered"] == before["units_ordered"] + 240

    north_before = next(r for r in before["runs"] if r["run"] == "north")
    north_after = next(r for r in after["runs"] if r["run"] == "north")
    assert north_after["outstanding"] > north_before["outstanding"]


def test_losing_a_packer_shows_up_as_vans_running_late(recording):
    before = recording["frames"]["floor.0"]["production"]["runs"]
    after = recording["frames"]["floor.more.off"]["production"]["runs"]

    assert all(run["status"] == "on_time" for run in before)
    assert any(run["status"] != "on_time" for run in after)


def test_one_absence_raises_one_escalation_not_one_per_job(recording):
    """Nineteen alerts for one person going home is noise, not an alert.

    Two rows are expected and no more: the re-plan recording that the evening
    no longer fits, and the one ask for help that follows it. Shaleen was
    holding many more jobs than that.
    """
    frame = recording["frames"]["floor.more.off"]
    escalations = [entry for entry in frame["feed"] if entry["action"] == "escalated"]
    assert len(escalations) <= 2

    hers = [t for t in recording["frames"]["floor.more"]["board"] if t["assignee"] == "Shaleen"]
    assert len(hers) > 5, "she has to be holding real work for this to mean anything"


def test_calling_somebody_in_gives_them_something_to_do(recording):
    after = recording["frames"]["floor.off.back"]
    valentino = next(p for p in after["staff"] if p["name"] == "Valentino")
    assert valentino["on_shift"], "somebody called in has to be on the shift"
    assert valentino["live_tasks"] > 0, "and has to have been given work"


# --- the call round ---------------------------------------------------------


def test_stepping_out_asks_everyone_who_could_come_in(recording):
    frame = recording["frames"]["short.1"]
    open_requests = [c for c in frame["coverage"] if c["status"] == "open"]
    assert len(open_requests) == 1
    asked = {o["employee_name"] for o in open_requests[0]["offers"]}
    assert asked == {"Shaleen", "Tavi", "Valentino"}
    assert "Marlo" not in asked, "he is at the weekly limit"


def test_accepting_moves_the_job_and_clears_the_request(recording):
    frame = recording["frames"]["short.1.shaleen-accept"]
    assert frame["counters"]["open_coverage"] == 0
    assert frame["counters"]["coverage_filled"] >= 1
    shaleen = next(p for p in frame["staff"] if p["name"] == "Shaleen")
    assert shaleen["live_tasks"] > 0


def test_everybody_declining_escalates_rather_than_going_quiet(recording):
    frame = recording["frames"]["short.1.shaleen-decline.tavi-decline.valentino-decline"]
    assert frame["counters"]["needs_manager"] >= 1
    escalations = [d for d in frame["feed"] if d["action"] == "escalated"]
    assert escalations, "somebody has to be told"


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
    first = capture_mod.capture(anchor=at(16, 30))
    second = capture_mod.capture(anchor=at(16, 30))
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# --- what the recording carries ---------------------------------------------


def test_timestamps_are_stored_once_and_rendered_in_the_browser(recording):
    """The local strings are the reader's to compute, on their own clock."""
    stamp = recording["frames"]["floor.0"]["board"][0]["starts_at"]
    assert set(stamp) == {"utc"}
    assert stamp["utc"].endswith("Z")


# --- the employee side ------------------------------------------------------


def test_every_frame_was_recorded_from_everybody_own_side(recording):
    roster = {str(p["id"]) for p in recording["roster"]}
    assert len(roster) == 7
    for key, views in recording["me_frames"].items():
        assert set(views) == roster, f"{key} is missing somebody's view"
        for view in views.values():
            assert {"me", "hours", "tasks", "cover_requests", "leave", "agent"} <= set(view)


def test_whoever_was_asked_can_see_it_on_their_own_page(recording):
    """Otherwise switching roles at that moment would show an empty screen."""
    for key, frame in recording["frames"].items():
        for cover in frame["coverage"]:
            if cover["status"] != "open":
                continue
            for offer in cover["offers"]:
                if offer["status"] != "sent":
                    continue
                view = recording["me_frames"][key][str(offer["employee_id"])]
                titles = [r["task_title"] for r in view["cover_requests"]]
                assert cover["task_title"] in titles, (
                    f"{key}: {offer['employee_name']} was asked but their page does not say so"
                )


def test_the_person_who_left_can_see_who_took_their_work(recording):
    jasoo = str(recording["leave_employee_id"])
    after = recording["me_frames"]["short.1.shaleen-accept"][jasoo]
    assert "Shaleen" in {h["taken_by"] for h in after["handovers"]}, (
        "the person who stepped out has to be able to see who picked it up"
    )


def test_a_colleague_is_named_only_where_the_work_requires_it(recording):
    """Not every mention of a colleague is a leak.

    Being told whose job you are covering is necessary and ordinary -- you may
    have to hand it back. What must not travel is anything *about* them: their
    hours, their skills, their jobs, or why they went home. So this checks the
    structural sections are mine alone, and lets the two operational fields
    through by name.
    """
    names = {p["name"] for p in recording["roster"]}
    allowed_keys = {"asked_because", "filled_by", "taken_by"}

    def strip(node):
        """The payload with the operationally necessary names removed."""
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k not in allowed_keys}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    for key, views in recording["me_frames"].items():
        for view in views.values():
            mine = view["me"]["name"]
            body = strip({k: v for k, v in view.items() if k != "handovers"})
            blob = json.dumps(body)
            for other in names - {mine}:
                assert other not in blob, f"{key}: {mine}'s page carries {other}"


def test_why_somebody_went_home_stays_between_them_and_the_manager(recording):
    """The cover request says who is away. It does not say why."""
    jasoo = str(recording["leave_employee_id"])
    reason = recording["me_frames"]["short.1"][jasoo]["leave"][0]["reason"]
    assert reason, "the recording needs a reason for this test to mean anything"

    for key, views in recording["me_frames"].items():
        for employee_id, view in views.items():
            if employee_id == jasoo:
                continue
            assert reason not in json.dumps(view), f"{key}: {view['me']['name']} can read it"


# --- the assembled page -----------------------------------------------------


def test_the_built_page_carries_the_dashboard_unchanged():
    page = build_replay.build()
    styles = (build_replay.WEB / "styles.css").read_text()

    # Not a copy of either page -- the pages themselves.
    assert styles.strip() in page
    for script in ("common.js", "app.js", "me.js", "console.js"):
        assert (build_replay.WEB / script).read_text().strip() in page
    assert '<script id="replay-data"' in page
    assert f"<title>{build_replay.TITLE}</title>" in page


def test_the_built_page_asks_the_server_for_nothing():
    """A published page cannot fetch from a machine that isn't there."""
    page = build_replay.build()
    assert "/static/" not in page
    # The host supplies the document shell; a second one would nest documents.
    for tag in ("<!doctype", "<html", "<head>", "<body>"):
        assert tag not in page.lower()


def test_the_built_page_holds_both_screens_and_one_toast():
    page = build_replay.build()
    assert 'id="view-manager"' in page
    assert 'id="view-employee"' in page
    assert 'id="role-picker"' in page
    # Two elements with the same id would be invalid, and only one can show.
    assert page.count('id="toast"') == 1
    # Each page was written as a standalone script with top-level names of its
    # own; in one document they need separate scopes.
    assert page.count("<script>(function () {") == 3


def test_the_built_page_says_what_it_is():
    page = build_replay.build()
    assert "Recorded walkthrough" in page
    assert "not talking to a live server" in page


def test_the_built_page_fits_comfortably_inside_the_publishing_limit():
    """16 MB is the ceiling; a page anywhere near it would not load either."""
    page = build_replay.build()
    assert len(page.encode()) < 8 * 1024 * 1024
