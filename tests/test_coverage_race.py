"""Two people accept the same cover request at the same moment.

This is the failure everyone waves away as unlikely and which happens on the
first busy Friday: two phones buzz, two thumbs move, and two people walk to
the same station while a third order goes unpacked.  The test uses real
threads against a real file-backed SQLite database, because an in-memory
database shared inside one session would not actually be racing anything.
"""

from __future__ import annotations

import threading

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import build_engine, create_all
from app.engine import coverage as coverage_engine
from app.engine import reassignment
from app.models.coverage import CoverageOffer, CoverageRequest
from app.models.enums import CoverageStatus, OfferStatus
from app.models.task import Task
from app.notifications import ConsoleNotifier
from app.sim.seed import at, seed_kitchen
from tests.test_reassignment import file_leave


def test_only_one_of_two_simultaneous_acceptances_wins(tmp_path):
    db_path = tmp_path / "race.db"
    engine = build_engine(f"sqlite:///{db_path}")
    create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        business_tz="America/New_York",
        dry_run=True,
        coverage_link_secret="test-secret",
    )
    notifier = ConsoleNotifier(echo=False)
    now = at(14, 5)

    with factory() as setup:
        world = seed_kitchen(setup, on_shift_packer=False)
        leave = file_leave(setup, world, now)
        reassignment.handle_leave_request(setup, leave, notifier, settings, now)
        setup.commit()
        request_id = setup.scalars(
            select(CoverageRequest.id).where(CoverageRequest.status == CoverageStatus.OPEN)
        ).one()
        offer_ids = [
            setup.scalars(
                select(CoverageOffer.id).where(
                    CoverageOffer.coverage_request_id == request_id,
                    CoverageOffer.employee_id == world.employee_id(key),
                )
            ).one()
            for key in ("luis", "sam")
        ]
        task_id = setup.get(CoverageRequest, request_id).task_id

    start = threading.Barrier(len(offer_ids))
    outcomes: dict[int, str] = {}
    errors: list[BaseException] = []

    def accept(offer_id: int) -> None:
        try:
            with factory() as session:
                offer = session.get(CoverageOffer, offer_id)
                start.wait(timeout=5)
                outcome = coverage_engine.accept_offer(session, offer, notifier, settings, now)
                outcomes[offer_id] = outcome.status
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
            errors.append(exc)

    threads = [threading.Thread(target=accept, args=(offer_id,)) for offer_id in offer_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not errors, f"a thread blew up: {errors}"
    assert sorted(outcomes.values()) == ["accepted", "already_filled"]

    with factory() as check:
        request = check.get(CoverageRequest, request_id)
        assert request.status == CoverageStatus.FILLED
        accepted = [o for o in request.offers if o.status == OfferStatus.ACCEPTED]
        assert len(accepted) == 1, "exactly one person is on the hook for this job"
        assert request.filled_by_id == accepted[0].employee_id
        assert check.get(Task, task_id).assignee_id == accepted[0].employee_id
        assert all(
            o.status == OfferStatus.SUPERSEDED for o in request.offers if o.id != accepted[0].id
        )

    engine.dispose()


def test_the_loser_is_told_immediately_rather_than_left_hanging(tmp_path):
    db_path = tmp_path / "race2.db"
    engine = build_engine(f"sqlite:///{db_path}")
    create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    settings = Settings(
        database_url=f"sqlite:///{db_path}", business_tz="America/New_York", dry_run=True
    )
    notifier = ConsoleNotifier(echo=False)
    now = at(14, 5)

    with factory() as session:
        world = seed_kitchen(session, on_shift_packer=False)
        leave = file_leave(session, world, now)
        reassignment.handle_leave_request(session, leave, notifier, settings, now)
        session.commit()
        request = session.scalars(
            select(CoverageRequest).where(CoverageRequest.status == CoverageStatus.OPEN)
        ).one()
        luis, sam = sorted(request.offers, key=lambda o: o.rank)

        coverage_engine.accept_offer(session, luis, notifier, settings, now)
        notifier.clear()
        outcome = coverage_engine.accept_offer(session, sam, notifier, settings, now)

    assert outcome.status == "already_filled"
    assert "already" in outcome.message.lower()
    engine.dispose()
