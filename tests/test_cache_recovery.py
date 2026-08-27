"""Cache rebuilding and survival of a suspended/dropped database connection.

The cache makes the loops quiet, but it must never become a second source of
truth. These prove it is rebuilt exactly from PostgreSQL after a restart, and
that a connection dropped underneath the process — what a scale-to-zero
database does when it suspends — costs neither a lost nor a duplicated
reminder.
"""

from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest

from app.database import Database, DatabaseUnavailable
from app.reminder_service import (
    STATE_LAST_FULL_SYNC,
    STATE_LAST_SYNC,
    ReminderService,
)
from tests.conftest import NOW, make_assignment, seed
from tests.test_idle_quiet import Recorder, StableCanvas, counting


def build(config, db, notifier=None, canvas=None):
    service = ReminderService(config, db, canvas or StableCanvas(), notifier or Recorder())
    service.hydrate()
    return service


def terminate_backends(database_url: str) -> int:
    """Kill every connection the bot holds, as a suspending database would."""
    with psycopg.connect(database_url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE application_name = 'phat-discord-bot' AND pid <> pg_backend_pid()"
        ).fetchall()
    return len(rows)


# ------------------------------------------------------- rebuilding the cache


async def test_cache_rebuilds_exactly_after_a_full_restart(config, database_url):
    first = Database(database_url)
    service = build(config, first, canvas=StableCanvas())
    await service.sync_canvas()
    before = {
        aid: a.persisted_fields() for aid, a in service.cache.assignments.items()
    }
    assert len(before) == 5
    first.close()

    second = Database(database_url)
    try:
        restarted = build(config, second)
        after = {
            aid: a.persisted_fields() for aid, a in restarted.cache.assignments.items()
        }
        assert after == before, "the rebuilt cache must match what was persisted"
    finally:
        second.close()


async def test_reminder_history_is_rebuilt_so_nothing_re_fires(config, database_url):
    first = Database(database_url)
    service = build(config, first)
    seed(service, make_assignment(due_in_minutes=55))
    notifier = Recorder()
    service.notifier = notifier
    assert await service.evaluate_reminders(now=NOW) == 1
    first.close()

    second = Database(database_url)
    try:
        restarted = build(config, second)
        assignment = restarted.cache.active()[0]
        # 55 minutes out crosses 720/300/120/60: 60 was delivered, the rest
        # were recorded as skipped. All four must come back.
        assert restarted.cache.thresholds_for(assignment) == {720, 300, 120, 60}

        again = Recorder()
        restarted.notifier = again
        # And the rebuilt cache is enough to stay silent with zero queries.
        with counting(second) as calls:
            assert await restarted.evaluate_reminders(now=NOW + timedelta(minutes=1)) == 0
        assert again.sent == []
        assert calls == []
    finally:
        second.close()


async def test_suppressed_thresholds_are_rebuilt_too(config, database_url):
    """Catch-up suppressions must survive a restart or they would fire late."""
    first = Database(database_url)
    service = build(config, first)
    seed(service, make_assignment(due_in_minutes=20))
    assert await service.evaluate_reminders(now=NOW) == 1
    first.close()

    second = Database(database_url)
    try:
        restarted = build(config, second)
        assignment = restarted.cache.active()[0]
        assert restarted.cache.thresholds_for(assignment) == {720, 300, 120, 60, 30}
    finally:
        second.close()


async def test_sync_timestamps_survive_a_restart(config, database_url):
    first = Database(database_url)
    service = build(config, first, canvas=StableCanvas())
    await service.sync_canvas()          # a real change, so state is persisted
    persisted = service.cache.last_sync_at
    assert first.get_state(STATE_LAST_SYNC)
    assert first.get_state(STATE_LAST_FULL_SYNC)
    first.close()

    second = Database(database_url)
    try:
        restarted = build(config, second)
        # Timestamps are stored to whole-second ISO precision by design.
        assert restarted.cache.last_sync_at == persisted.replace(microsecond=0)
        assert restarted.last_error() is None
    finally:
        second.close()


async def test_a_retired_assignment_does_not_come_back_after_restart(config, database_url):
    first = Database(database_url)
    canvas = StableCanvas()
    service = build(config, first, canvas=canvas)
    await service.sync_canvas()
    canvas.payload.pop()
    await service.sync_canvas()
    first.close()

    second = Database(database_url)
    try:
        restarted = build(config, second)
        assert len(restarted.cache.assignments) == 4
        assert 504 not in restarted.cache.assignments
    finally:
        second.close()


def test_hydration_failure_is_not_silently_tolerated(config, database_url):
    db = Database(database_url)
    db.close()
    service = ReminderService(config, db, StableCanvas(), Recorder())
    with pytest.raises((psycopg.Error, DatabaseUnavailable)):
        service.hydrate()
    assert service.cache.hydrated is False


# -------------------------------------------------- dropped / suspended links


async def test_a_dropped_connection_does_not_lose_a_reminder(config, database_url):
    """The database goes away between ticks; the reminder must still land."""
    db = Database(database_url)
    try:
        service = build(config, db)
        seed(service, make_assignment(due_in_minutes=55))

        terminate_backends(database_url)      # the database "suspends"

        notifier = Recorder()
        service.notifier = notifier
        assert await service.evaluate_reminders(now=NOW) == 1
        assert notifier.sent == [(123, 60)]
        assert db.delivered_reminder_count() == 1
        assert db.healthy is True
    finally:
        db.close()


async def test_a_drop_mid_schedule_does_not_duplicate_a_reminder(config, database_url):
    db = Database(database_url)
    try:
        service = build(config, db)
        seed(service, make_assignment(due_in_minutes=55))
        notifier = Recorder()
        service.notifier = notifier
        assert await service.evaluate_reminders(now=NOW) == 1

        terminate_backends(database_url)

        # Later ticks stay silent, and the claim row is still exactly one.
        for tick in range(1, 4):
            assert await service.evaluate_reminders(now=NOW + timedelta(seconds=45 * tick)) == 0
        assert notifier.sent == [(123, 60)]
        assert db.delivered_reminder_count() == 1
    finally:
        db.close()


async def test_a_drop_survives_a_restart_without_duplicating(config, database_url):
    db = Database(database_url)
    service = build(config, db)
    seed(service, make_assignment(due_in_minutes=55))
    notifier = Recorder()
    service.notifier = notifier
    assert await service.evaluate_reminders(now=NOW) == 1
    terminate_backends(database_url)
    db.close()

    restarted_db = Database(database_url)
    try:
        restarted = build(config, restarted_db)
        again = Recorder()
        restarted.notifier = again
        assert await restarted.evaluate_reminders(now=NOW + timedelta(minutes=1)) == 0
        assert again.sent == []
        assert restarted_db.delivered_reminder_count() == 1
    finally:
        restarted_db.close()


async def test_a_dropped_connection_does_not_break_the_sync(config, database_url):
    db = Database(database_url)
    try:
        canvas = StableCanvas()
        service = build(config, db, canvas=canvas)
        await service.sync_canvas()

        terminate_backends(database_url)

        canvas.payload[0]["name"] = "Renamed after the drop"
        result = await service.sync_canvas()
        assert result.ok
        assert db.get_assignment(500).name == "Renamed after the drop"
    finally:
        db.close()


def test_retry_gives_up_and_reports_unhealthy(config, database_url):
    """A database that is really gone must surface, not hang forever."""
    db = Database(
        database_url, retry_attempts=2
    )
    try:
        db.close()
        with pytest.raises((psycopg.Error, DatabaseUnavailable)):
            db.count_monitored()
        assert db.healthy is False
    finally:
        pass


def test_retry_only_covers_transient_faults(db, monkeypatch):
    """A programming error must fail immediately, not be retried three times."""
    attempts = {"n": 0}

    def boom() -> None:
        attempts["n"] += 1
        raise psycopg.ProgrammingError("syntax error")

    with pytest.raises(psycopg.ProgrammingError):
        db._run("bad query", boom)
    assert attempts["n"] == 1
    assert db.healthy is False


def test_retry_recovers_on_a_later_attempt(db):
    attempts = {"n": 0}

    def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise psycopg.OperationalError("connection reset while waking")
        return "ok"

    assert db._run("flaky", flaky) == "ok"
    assert attempts["n"] == 3
    assert db.healthy is True
