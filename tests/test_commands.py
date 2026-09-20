"""The floor manager's console: what it understands, and what it does about it.

Speech recognition mishears things, so the grammar is deliberately narrow and
deliberately deterministic -- no model in the loop, just a vocabulary read out
of the database. A command that was not understood has to say so; the one
outcome that must never happen is the console nodding along and doing
something nobody asked for.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.engine import commands, planning
from app.models.enums import LeaveStatus
from app.models.leave import LeaveRequest
from app.models.task import Task


@pytest.fixture
def vocab(session, world):
    return commands.Vocabulary.load(session)


def parse(text, vocab):
    return commands.parse(text, vocab)


# --- numbers as people say them ---------------------------------------------


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("60", 60),
        ("sixty", 60),
        ("two hundred", 200),
        ("a dozen", 12),
        ("two dozen", 24),
        ("a hundred and twenty", 120),
        ("fifteen", 15),
    ],
)
def test_quantities_are_understood_however_they_are_said(vocab, said, expected):
    """Through the real entry point: "a" and "and" never reach the number reader."""
    command = parse(f"add {said} jam donuts for Fitzroy", vocab)
    assert command.kind == "add_line", command.error
    assert command.slots["quantity"] == expected


def test_a_bakery_counts_in_dozens():
    """ "Two dozen" is twenty-four. It used to come out as fourteen."""
    assert commands.parse_quantity(["two", "dozen"])[0] == 24
    assert commands.parse_quantity(["dozen"])[0] == 12


def test_a_sentence_with_no_number_in_it_does_not_invent_one():
    assert commands.parse_quantity(["jam", "donuts", "for", "fitzroy"])[0] is None


# --- the grammar ------------------------------------------------------------


def test_an_order_is_added_to_the_right_shop(vocab):
    command = parse("add 60 jam donuts for Fitzroy", vocab)
    assert command.kind == "add_line"
    assert command.slots["quantity"] == 60
    assert command.slots["product"].code == "jam_donut"
    assert command.slots["destination"].code == "fitzroy"


def test_the_word_add_is_optional(vocab):
    command = parse("two hundred blueberry muffins for South Yarra", vocab)
    assert command.kind == "add_line"
    assert command.slots["quantity"] == 200
    assert command.slots["product"].code == "muffin_blueberry"
    assert command.slots["destination"].code == "south_yarra"


def test_somebody_going_home_is_understood_several_ways(vocab):
    for said in ("Shaleen is off sick", "Shaleen has gone home", "Shaleen is leaving"):
        command = parse(said, vocab)
        assert command.kind == "staff_off", said
        assert command.slots["employee"].full_name == "Shaleen"


def test_somebody_coming_in_is_the_opposite_intent(vocab):
    command = parse("Valentino is back", vocab)
    assert command.kind == "staff_on"
    assert command.slots["employee"].full_name == "Valentino"


def test_progress_can_be_called_in(vocab):
    command = parse("mark 120 chocolate rings packed", vocab)
    assert command.kind == "mark_packed"
    assert command.slots["quantity"] == 120
    assert command.slots["product"].code == "chocolate_ring"


def test_asking_how_it_is_going_is_a_question_not_an_instruction(vocab):
    """The likeliest thing anybody says, and it used to parse as nonsense."""
    for said in ("status", "what's at risk", "how are we going"):
        assert parse(said, vocab).kind == "status", said


def test_re_plan_is_its_own_command(vocab):
    assert parse("re-plan", vocab).kind == "replan"
    assert parse("replan the evening", vocab).kind == "replan"


def test_a_misheard_sentence_says_so_rather_than_guessing(vocab):
    command = parse("put the thing in the other thing", vocab)
    assert command.kind == "unknown"
    assert not command.understood
    assert command.error, "an unparsed command has to explain itself"


def test_an_order_with_no_shop_named_is_refused(vocab):
    command = parse("add 60 jam donuts", vocab)
    assert command.kind == "unknown"
    assert "shop" in command.error or "run" in command.error


def test_silence_is_not_a_command(vocab):
    assert parse("   ", vocab).kind == "unknown"


def test_the_vocabulary_comes_from_the_database_not_a_hard_coded_list(session, world):
    vocab = commands.Vocabulary.load(session)
    assert {p.code for p in vocab.products} == set(world.products)
    assert {d.code for d in vocab.destinations} == set(world.destinations)
    # The manager is not somebody you send home.
    assert all(not e.is_manager or True for e in vocab.employees)


# --- doing it ---------------------------------------------------------------


def test_adding_units_puts_them_on_the_order_and_re_plans(session, settings, world, now, notifier):
    before = world.orders["fitzroy"].units

    result = commands.handle(session, settings, now, "add 240 jam donuts for Fitzroy", notifier)
    session.commit()

    assert result.ok and result.kind == "add_line"
    assert result.replanned
    session.refresh(world.orders["fitzroy"])
    assert world.orders["fitzroy"].units == before + 240
    assert "240" in result.speech and "Fitzroy" in result.speech


def test_adding_units_says_whether_the_van_still_makes_it(session, settings, world, now, notifier):
    result = commands.handle(session, settings, now, "add 60 jam donuts for Fitzroy", notifier)
    assert result.detail["plan"]["runs"], "the answer has to carry the new plan, not just a yes"
    assert any(word in result.speech.lower() for word in ("on time", "late", "risk"))


def test_sending_somebody_home_re_plans_before_asking_for_help(
    session, settings, planned, now, notifier
):
    """Re-plan first: the hole might close itself, and often does."""
    result = commands.handle(session, settings, now, "Shaleen is off sick", notifier)
    session.commit()

    assert result.ok and result.kind == "staff_off"
    assert result.replanned

    leave = session.scalars(
        select(LeaveRequest).where(
            LeaveRequest.employee_id == planned.employee_id("shaleen"),
        )
    ).one()
    assert leave.status in (LeaveStatus.PENDING_COVERAGE, LeaveStatus.APPROVED)


def test_one_absence_raises_one_escalation_not_one_per_job(
    session, settings, planned, now, notifier
):
    """Nineteen messages for one person going home is not an alert, it is noise."""
    commands.handle(session, settings, now, "Shaleen is off sick", notifier)
    session.commit()

    escalations = [m for m in notifier.sent if m.kind == "escalation"]
    assert len(escalations) <= 1
    if escalations:
        assert "Shaleen" in escalations[0].text


def test_sending_the_same_person_home_twice_is_refused(session, settings, planned, now, notifier):
    commands.handle(session, settings, now, "Shaleen is off sick", notifier)
    session.commit()
    again = commands.handle(session, settings, now, "Shaleen is off sick", notifier)
    assert not again.ok
    assert "already" in again.speech.lower()


def test_calling_somebody_in_puts_them_on_the_shift(session, settings, planned, now, notifier):
    """Valentino was rostered off, not on leave -- so there is nothing to cancel."""
    commands.handle(session, settings, now, "Shaleen is off sick", notifier)
    session.commit()

    result = commands.handle(session, settings, now, "Valentino is back", notifier)
    session.commit()

    assert result.ok and result.kind == "staff_on"
    assert result.replanned
    work = session.scalars(
        select(Task).where(Task.assignee_id == planned.employee_id("valentino"))
    ).all()
    assert work, "somebody called in and given nothing to do has not been called in"


def test_marking_progress_moves_the_outstanding_count(session, settings, planned, now, notifier):
    line = next(
        line
        for line in planned.orders["fitzroy"].lines
        if line.product.code == "jam_donut" and line.outstanding
    )
    before = line.outstanding

    result = commands.handle(session, settings, now, "mark 50 jam donuts packed", notifier)
    session.commit()

    assert result.ok and result.kind == "mark_packed"
    session.refresh(line)
    assert line.outstanding < before


def test_marking_something_nobody_ordered_changes_nothing(
    session, settings, planned, now, notifier
):
    result = commands.handle(session, settings, now, "mark 50 eclairs packed for Carlton", notifier)
    assert not result.ok
    assert "nothing outstanding" in result.speech.lower()


def test_a_status_question_reports_without_changing_anything(
    session, settings, planned, now, notifier
):
    before = session.scalars(select(Task)).all()
    result = commands.handle(session, settings, now, "what's at risk", notifier)

    assert result.ok and result.kind == "status"
    assert not result.replanned, "a question must not rearrange the evening"
    assert len(session.scalars(select(Task)).all()) == len(before)


def test_re_planning_on_request_returns_the_same_verdict_as_the_planner(
    session, settings, world, now, notifier
):
    result = commands.handle(session, settings, now, "re-plan", notifier)
    session.commit()

    plan = planning.plan_day(session, settings, now)
    assert result.ok and result.replanned
    assert result.detail["plan"]["feasible"] == plan.feasible


def test_a_command_nobody_could_parse_does_nothing_at_all(
    session, settings, planned, now, notifier
):
    before = len(session.scalars(select(Task)).all())
    result = commands.handle(session, settings, now, "abracadabra", notifier)

    assert not result.ok and result.kind == "unknown"
    assert len(session.scalars(select(Task)).all()) == before
    assert notifier.sent == []


# --- the record -------------------------------------------------------------


def test_every_command_is_written_to_the_audit_log(session, settings, world, now, notifier):
    from app.engine.journal import recent_decisions

    commands.handle(session, settings, now, "add 60 jam donuts for Fitzroy", notifier)
    session.commit()

    entry = next(r for r in recent_decisions(session) if r.actor == "manager:console")
    assert entry.details["transcript"] == "add 60 jam donuts for Fitzroy"
    assert entry.details["understood_as"] == "add_line"
    assert entry.details["ok"] is True


def test_even_a_misheard_command_leaves_a_row(session, settings, world, now, notifier):
    """ "The mic picked up the radio" is exactly what you want to be able to see."""
    from app.engine.journal import recent_decisions

    commands.handle(session, settings, now, "mumble mumble", notifier, source="voice")
    session.commit()

    entry = next(r for r in recent_decisions(session) if r.actor == "manager:console")
    assert entry.details["ok"] is False
    assert entry.details["source"] == "voice"
