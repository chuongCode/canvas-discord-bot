"""PostgreSQL-specific persistence guarantees.

The ephemeral-filesystem host is the reason these exist. Reminder state now
lives in a database that outlives the container, so the interesting failures
are no longer "the file was lost" but "two processes overlapped during a
redeploy" and "the process died between sending and recording".
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from app.canvas_client import CanvasError
from app.database import SCHEMA_VERSION, STATE_SCHEMA_VERSION, Database, DatabaseUnavailable
from app.reminder_service import ReminderService
from tests.conftest import NOW, make_assignment, seed


class Recorder:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.sent: list[tuple[int, int]] = []

    async def __call__(self, assignment, threshold, remaining):
        self.sent.append((assignment.id, threshold))
        return self.ok


class FakeCanvas:
    base_url = "https://canvas.example.edu"

    async def get_active_courses(self, *, students_only=True):
        raise CanvasError("not used here")

    async def get_course_assignments(self, course_id: int):
        return []


def service(config, db, notifier):
    """A service with its cache loaded from PostgreSQL, as startup does it."""
    instance = ReminderService(config, db, FakeCanvas(), notifier)
    instance.hydrate()
    return instance


# ------------------------------------------------------------------- schema


def test_the_schema_is_created_and_versioned(db, database_url):
    assert db.get_state(STATE_SCHEMA_VERSION) == str(SCHEMA_VERSION)

    with psycopg.connect(database_url) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            ).fetchall()
        }
    assert {"assignments", "reminders_sent", "app_state"} <= tables


def test_opening_the_same_database_twice_is_safe(database_url):
    """Two instances boot concurrently during a rolling redeploy."""
    first = Database(database_url)
    second = Database(database_url)
    try:
        first.upsert_assignment(make_assignment())
        assert second.count_monitored() == 1
    finally:
        first.close()
        second.close()


def test_reminder_identity_survives_a_full_reconnect(db, database_url):
    a = make_assignment()
    db.upsert_assignment(a)
    db.record_reminder(a.id, a.due_key, 60, delivered=True)
    db.close()

    reopened = Database(database_url)
    try:
        assert reopened.sent_thresholds(a.id, a.due_key) == {60}
        assert reopened.delivered_reminder_count() == 1
        assert reopened.get_assignment(a.id).name == a.name
    finally:
        reopened.close()


def test_large_canvas_ids_round_trip(db):
    """Canvas IDs exceed 32 bits at some institutions; the column is BIGINT."""
    big = 9_007_199_254_740_993
    db.upsert_assignment(make_assignment(assignment_id=big, course_id=big - 1))
    stored = db.get_assignment(big)
    assert stored is not None and stored.course_id == big - 1


def test_booleans_and_floats_round_trip_as_python_types(db):
    a = make_assignment(is_complete=True, submission_state="graded")
    a.points_possible = 12.5
    a.excused = True
    db.upsert_assignment(a)
    stored = db.get_assignment(a.id)
    assert stored.is_complete is True
    assert stored.excused is True
    assert stored.points_possible == 12.5


def test_a_null_due_date_round_trips_as_none(db):
    db.upsert_assignment(make_assignment(due_in_minutes=None))
    stored = db.get_assignment(123)
    assert stored is not None and stored.due_at is None
    # ...and an assignment with no deadline is not monitored.
    assert db.active_assignments() == []


# --------------------------------------------------- claim / release / dedupe


def test_a_claim_can_only_be_won_once(db):
    a = make_assignment()
    assert db.claim_reminder(a.id, a.due_key, 60) is True
    assert db.claim_reminder(a.id, a.due_key, 60) is False


def test_a_released_claim_can_be_won_again(db):
    a = make_assignment()
    assert db.claim_reminder(a.id, a.due_key, 60) is True
    db.release_reminder(a.id, a.due_key, 60)
    assert db.sent_thresholds(a.id, a.due_key) == set()
    assert db.claim_reminder(a.id, a.due_key, 60) is True


def test_a_delivered_reminder_can_never_be_released(db):
    """The safety property: once sent, the record is permanent."""
    a = make_assignment()
    db.claim_reminder(a.id, a.due_key, 60)
    db.mark_reminder_delivered(a.id, a.due_key, 60)
    db.release_reminder(a.id, a.due_key, 60)
    assert db.sent_thresholds(a.id, a.due_key) == {60}
    assert db.delivered_reminder_count() == 1


def test_a_claim_does_not_collide_with_a_suppression_record(db):
    a = make_assignment()
    db.record_reminders(a.id, a.due_key, [720, 300], delivered=False)
    assert db.claim_reminder(a.id, a.due_key, 720) is False
    assert db.sent_thresholds(a.id, a.due_key) == {720, 300}


def test_two_connections_racing_for_one_reminder_produce_one_winner(database_url):
    """The redeploy overlap: old and new instance evaluate the same tick."""
    old = Database(database_url)
    new = Database(database_url)
    try:
        a = make_assignment()
        results = [
            old.claim_reminder(a.id, a.due_key, 60),
            new.claim_reminder(a.id, a.due_key, 60),
        ]
        assert results.count(True) == 1
        assert results.count(False) == 1
    finally:
        old.close()
        new.close()


def test_sent_thresholds_map_only_covers_the_current_due_date(db):
    monday = make_assignment(due_in_minutes=60)
    db.upsert_assignment(monday)
    db.record_reminder(monday.id, monday.due_key, 60, delivered=True)

    wednesday = make_assignment(due_in_minutes=60 + 2880)
    db.upsert_assignment(wednesday)

    recorded = db.sent_thresholds_map()
    assert recorded.get((wednesday.id, wednesday.due_key)) is None
    assert recorded.get((monday.id, monday.due_key)) is None, "stale due version is dropped"


def test_sent_thresholds_map_matches_the_per_assignment_query(db):
    a = make_assignment()
    db.upsert_assignment(a)
    db.record_reminders(a.id, a.due_key, [720, 300], delivered=False)
    db.record_reminder(a.id, a.due_key, 60, delivered=True)
    assert db.sent_thresholds_map()[(a.id, a.due_key)] == db.sent_thresholds(a.id, a.due_key)


# ----------------------------------------------------- restarts and redeploys


async def test_no_duplicate_reminder_after_a_simulated_restart(config, database_url):
    """Send, drop the whole process, come back: the reminder is not repeated."""
    first = Database(database_url)
    notifier = Recorder()
    first.upsert_assignment(make_assignment(due_in_minutes=55))
    svc = service(config, first, notifier)   # hydrate picks the row up
    assert await svc.evaluate_reminders(now=NOW) == 1
    assert notifier.sent == [(123, 60)]
    first.close()  # the container is torn down

    for tick in range(1, 4):
        restarted = Database(database_url)
        again = Recorder()
        svc = service(config, restarted, again)   # a full cold rebuild each time
        moment = NOW + timedelta(seconds=45 * tick)
        assert await svc.evaluate_reminders(now=moment) == 0
        assert again.sent == []
        restarted.close()


async def test_two_overlapping_instances_send_exactly_one_reminder(config, database_url):
    """Render runs the old and new deploy together for a few seconds."""
    old_db = Database(database_url)
    new_db = Database(database_url)
    try:
        old_db.upsert_assignment(make_assignment(due_in_minutes=55))

        old_notifier, new_notifier = Recorder(), Recorder()
        # Both hydrate from the same database, so both believe the reminder is
        # unsent — the claim is what has to break the tie.
        old = service(config, old_db, old_notifier)
        new = service(config, new_db, new_notifier)

        results = await asyncio.gather(
            old.evaluate_reminders(now=NOW), new.evaluate_reminders(now=NOW)
        )
        assert sum(results) == 1, "exactly one instance may deliver"
        assert len(old_notifier.sent) + len(new_notifier.sent) == 1
        assert old_db.delivered_reminder_count() == 1
    finally:
        old_db.close()
        new_db.close()


async def test_a_crash_between_claiming_and_sending_retries_next_tick(config, database_url):
    """A process killed mid-delivery leaves a claim; the retry still happens."""
    db = Database(database_url)
    try:
        a = make_assignment(due_in_minutes=55)
        db.upsert_assignment(a)

        async def die(*_args):
            raise RuntimeError("SIGKILL mid-send")

        assert await service(config, db, die).evaluate_reminders(now=NOW) == 0
        assert 60 not in db.sent_thresholds(a.id, a.due_key)

        notifier = Recorder()
        svc = service(config, db, notifier)
        assert await svc.evaluate_reminders(now=NOW) == 1
        assert notifier.sent == [(123, 60)]
    finally:
        db.close()


async def test_a_moved_deadline_gets_a_fresh_schedule_across_a_restart(config, database_url):
    db = Database(database_url)
    monday = make_assignment(due_in_minutes=700)
    db.upsert_assignment(monday)
    notifier = Recorder()
    assert await service(config, db, notifier).evaluate_reminders(now=NOW) == 1
    assert notifier.sent == [(123, 720)]
    db.close()

    # The instructor moves the deadline while the bot is down.
    restarted = Database(database_url)
    try:
        again = Recorder()
        svc = service(config, restarted, again)
        wednesday = make_assignment(due_in_minutes=700 + 2 * 24 * 60)
        seed(svc, wednesday)

        assert await svc.evaluate_reminders(now=NOW + timedelta(minutes=1)) == 0
        later = NOW + timedelta(minutes=2 * 24 * 60)
        assert await svc.evaluate_reminders(now=later) == 1
        assert again.sent == [(123, 720)]
    finally:
        restarted.close()


async def test_submission_state_persists_across_a_restart(config, database_url):
    db = Database(database_url)
    db.upsert_assignment(
        make_assignment(due_in_minutes=55, is_complete=True, submission_state="submitted")
    )
    db.close()

    restarted = Database(database_url)
    try:
        stored = restarted.get_assignment(123)
        assert stored.is_complete is True
        assert stored.submission_state == "submitted"
        notifier = Recorder()
        assert await service(config, restarted, notifier).evaluate_reminders(now=NOW) == 0
    finally:
        restarted.close()


async def test_first_run_suppression_only_applies_to_an_empty_database(config, database_url):
    from dataclasses import replace

    quiet = replace(config, suppress_reminders_on_first_run=True)

    fresh = Database(database_url)
    try:
        # Boot order: the service hydrates against an empty database, then the
        # first sync populates it.
        notifier = Recorder()
        svc = service(quiet, fresh, notifier)
        seed(svc, make_assignment(due_in_minutes=20))
        assert await svc.evaluate_reminders(now=NOW) == 0
        assert notifier.sent == []
    finally:
        fresh.close()

    # A later restart sees a populated database, so suppression does not re-arm.
    restarted = Database(database_url)
    try:
        notifier = Recorder()
        svc = service(quiet, restarted, notifier)
        seed(svc, make_assignment(assignment_id=999, due_in_minutes=55))
        assert await svc.evaluate_reminders(now=NOW) == 1
        assert notifier.sent == [(999, 60)]
    finally:
        restarted.close()


# ------------------------------------------------------------------- pruning


def test_prune_drops_only_long_dead_rows(db):
    live = make_assignment(assignment_id=1)
    db.upsert_assignment(live)
    db.record_reminder(live.id, live.due_key, 60, delivered=True)

    ancient = datetime(2020, 1, 1, tzinfo=UTC)
    stale = make_assignment(assignment_id=2)
    db.upsert_assignment(stale)
    db.record_reminder(stale.id, stale.due_key, 60, delivered=True, sent_at=ancient)
    db.deactivate_missing(course_ids=[1], keep_ids=[1])

    db.prune(older_than=timedelta(days=60))
    assert db.delivered_reminder_count() == 1
    assert {a.id for a in db.active_assignments()} == {1}


def test_prune_never_raises_when_the_database_is_gone(db):
    db.close()
    db.prune()  # logs and returns


# ---------------------------------------------------------------- connections


def test_an_unreachable_database_fails_fast(database_url):
    unreachable = "postgresql://nobody@127.0.0.1:1/none?connect_timeout=1"
    with pytest.raises((psycopg.Error, DatabaseUnavailable)):
        Database(unreachable, connect_timeout=2.0, retry_attempts=1)


def test_ping_reports_a_closed_pool(db):
    db.ping()
    assert db.healthy is True
    db.close()
    with pytest.raises((psycopg.Error, DatabaseUnavailable)):
        db.ping()
    assert db.healthy is False


def test_the_pool_recovers_from_a_dropped_connection(db, database_url):
    """A managed Postgres recycling a connection must not wedge the bot."""
    db.upsert_assignment(make_assignment())

    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE application_name = 'phat-discord-bot' AND pid <> pg_backend_pid()"
        )

    assert db.count_monitored() == 1
