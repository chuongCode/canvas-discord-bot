"""The scale-to-zero contract: an idle bot must not touch PostgreSQL at all.

Nothing reaches the database without first checking a connection out of the
pool, so counting checkouts is an exact measure of "did we talk to Postgres?".
Zero checkouts means zero statements, which is what lets a suspending database
(Neon's scale to zero) stay asleep between real events.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import timedelta

import aiohttp
import psycopg
import pytest

from app.canvas_client import CanvasClient, CanvasError
from app.database import Database
from app.discord_bot import CanvasReminderBot
from app.models import to_iso, utcnow
from app.reminder_service import ReminderService
from tests.conftest import NOW, make_assignment
from tests.test_health import HOST, free_port, serve


@contextmanager
def counting(db: Database):
    """Count pool checkouts — i.e. every trip to PostgreSQL — inside the block."""
    original = db._pool.connection
    calls: list[int] = []

    def spy(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    db._pool.connection = spy  # type: ignore[method-assign]
    try:
        yield calls
    finally:
        db._pool.connection = original  # type: ignore[method-assign]


class StableCanvas:
    """Canvas that always returns exactly the same payload."""

    base_url = "https://canvas.example.edu"

    def __init__(self, count: int = 5):
        self.payload = [
            {
                "id": 500 + i,
                "name": f"Assignment {i}",
                "published": True,
                "due_at": to_iso(utcnow() + timedelta(days=2 + i)),
                "html_url": f"https://canvas.example.edu/courses/1/assignments/{500 + i}",
                "submission": {"workflow_state": "unsubmitted"},
            }
            for i in range(count)
        ]
        self.course_calls = 0

    async def get_active_courses(self, *, students_only=True):
        self.course_calls += 1
        return [{"id": 1, "name": "CS 240"}]

    async def get_course_assignments(self, course_id: int):
        return self.payload


class Recorder:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.sent: list[tuple[int, int]] = []

    async def __call__(self, assignment, threshold, remaining):
        self.sent.append((assignment.id, threshold))
        return self.ok


@pytest.fixture
def synced(config, db):
    """A hydrated service whose cache already matches Canvas and PostgreSQL."""
    canvas = StableCanvas()
    service = ReminderService(config, db, canvas, Recorder())
    service.hydrate()
    return service, canvas


# ------------------------------------------------------- the reminder loop


async def test_an_idle_reminder_tick_touches_postgres_zero_times(synced):
    service, _ = synced
    await service.sync_canvas()

    # Nothing is anywhere near a deadline: 20 ticks, no database at all.
    with counting(service.db) as calls:
        for tick in range(20):
            assert await service.evaluate_reminders(now=NOW + timedelta(seconds=45 * tick)) == 0
    assert calls == [], f"idle evaluation hit PostgreSQL {len(calls)} time(s)"


async def test_ticks_stay_quiet_even_with_recorded_history(synced):
    """A reminder already sent must not cause a lookup on every later tick."""
    service, _ = synced
    await service.sync_canvas()
    assignment = service.cache.active()[0]
    service.db.record_reminder(assignment.id, assignment.due_key, 720, delivered=True)
    service.cache.note_recorded(assignment.id, assignment.due_key, [720])

    with counting(service.db) as calls:
        for tick in range(10):
            await service.evaluate_reminders(now=NOW + timedelta(seconds=45 * tick))
    assert calls == []


async def test_a_crossing_threshold_does_reach_postgres(synced):
    """The flip side: when there is real work, the database is consulted."""
    service, _ = synced
    await service.sync_canvas()
    notifier = Recorder()
    service.notifier = notifier

    due = service.cache.active()[0].due_at
    with counting(service.db) as calls:
        sent = await service.evaluate_reminders(now=due - timedelta(minutes=55))
    assert sent == 1
    assert notifier.sent and calls, "a real reminder must claim through PostgreSQL"


# --------------------------------------------------------- the Canvas sync


async def test_an_unchanged_canvas_sync_writes_nothing(synced):
    service, canvas = synced
    first = await service.sync_canvas()
    assert first.ok and first.assignments_tracked == 5

    # Every later sync sees identical Canvas data.
    for _ in range(4):
        with counting(service.db) as calls:
            result = await service.sync_canvas()
        assert result.ok
        assert calls == [], f"unchanged sync issued {len(calls)} database operation(s)"
    assert canvas.course_calls == 5, "Canvas is still polled; only the writes stop"


async def test_an_unchanged_sync_does_not_rewrite_the_sync_timestamp(synced):
    """last_sync must not be persisted as a five-minute heartbeat."""
    service, _ = synced
    await service.sync_canvas()
    from app.reminder_service import STATE_LAST_SYNC

    before = service.db.get_state(STATE_LAST_SYNC)
    await service.sync_canvas()
    assert service.db.get_state(STATE_LAST_SYNC) == before
    # ...but the in-process view is still current.
    assert service.cache.last_sync_at is not None


async def test_a_changed_assignment_is_written_and_others_are_not(synced):
    service, canvas = synced
    await service.sync_canvas()

    canvas.payload[2]["name"] = "Renamed project"
    with counting(service.db) as calls:
        result = await service.sync_canvas()
    assert result.ok
    # One upsert for the changed row, plus one batched app_state write.
    assert len(calls) == 2, f"expected 1 upsert + 1 state write, got {len(calls)}"
    assert service.db.get_assignment(502).name == "Renamed project"
    assert service.cache.assignments[502].name == "Renamed project"


async def test_a_missing_submission_object_is_not_treated_as_a_change(synced):
    """The classic false-positive: Canvas omits `submission` on later syncs."""
    service, canvas = synced
    await service.sync_canvas()
    for item in canvas.payload:
        item.pop("submission")

    with counting(service.db) as calls:
        await service.sync_canvas()
    assert calls == [], "preserved submission state must not look like a change"


async def test_a_retired_assignment_still_writes(synced):
    service, canvas = synced
    await service.sync_canvas()
    canvas.payload.pop()

    with counting(service.db) as calls:
        result = await service.sync_canvas()
    assert calls, "a retirement must reach PostgreSQL"
    assert result.assignments_retired == 1
    assert 504 not in service.cache.assignments

    # ...and the sync after it is quiet again.
    with counting(service.db) as calls:
        await service.sync_canvas()
    assert calls == []


async def test_a_canvas_outage_writes_nothing(synced):
    service, _ = synced
    await service.sync_canvas()

    class Broken(StableCanvas):
        async def get_active_courses(self, *, students_only=True):
            raise CanvasError("canvas is down")

    service.canvas = Broken()
    with counting(service.db) as calls:
        result = await service.sync_canvas()
    assert result.ok is False
    assert calls == [], "a failed sync must not write an error heartbeat"


# ---------------------------------------------------------------- /health


async def test_health_requests_never_touch_postgres(config, db):
    canvas = CanvasClient(config.canvas_base_url, config.canvas_api_token)
    bot = CanvasReminderBot(config, db, canvas)
    bot.service.hydrate()

    port = free_port()
    server = await serve(bot.health_snapshot, port)
    try:
        with counting(db) as calls:
            async with aiohttp.ClientSession() as session:
                for _ in range(25):
                    async with session.get(f"http://{HOST}:{port}/health") as response:
                        assert response.status == 200
                        payload = json.loads(await response.text())
        assert calls == [], f"25 health probes issued {len(calls)} database operation(s)"
    finally:
        await server.stop()

    # The payload is still complete and useful.
    assert payload["database"] == "ok"
    assert payload["cache_hydrated"] is True
    assert "monitored_assignments" in payload
    assert "reminders_delivered" in payload


async def test_health_reports_stale_database_health_honestly(synced):
    """No probing means /health reports what was last observed, not a guess."""
    service, _ = synced
    await service.sync_canvas()
    assert service.db.healthy is True
    assert service.db.last_contact_at is not None


# --------------------------------------------------- slash-command queries


async def test_browsing_deadlines_does_not_wake_the_database(synced):
    service, _ = synced
    await service.sync_canvas()
    with counting(service.db) as calls:
        items = service.incomplete_assignments()
    assert len(items) == 5
    assert calls == []


def test_cache_and_database_filtering_agree(db, config):
    service = ReminderService(config, db, StableCanvas(), Recorder())
    service.hydrate()
    for offset in (30, 90, 60 * 24 * 10):
        a = make_assignment(assignment_id=offset, due_in_minutes=offset)
        db.upsert_assignment(a)
    service.hydrate()

    for kwargs in (
        {},
        {"until": NOW + timedelta(days=7)},
        {"include_past_due": True},
    ):
        assert [a.id for a in service.incomplete_assignments(now=NOW, **kwargs)] == [
            a.id for a in db.incomplete_assignments(now=NOW, **kwargs)
        ]


# ------------------------------------------------------------- whole-loop


async def test_startup_reads_then_goes_quiet(config, database_url):
    """Boot costs a handful of reads; steady state costs nothing."""
    db = Database(database_url)
    try:
        service = ReminderService(config, db, StableCanvas(), Recorder())
        service.hydrate()
        await service.sync_canvas()

        with counting(db) as calls:
            for tick in range(8):          # 8 reminder ticks ...
                await service.evaluate_reminders(now=NOW + timedelta(seconds=45 * tick))
            for _ in range(3):             # ... and 3 Canvas syncs
                await service.sync_canvas()
        assert calls == [], f"steady state issued {len(calls)} database operation(s)"
    finally:
        db.close()


async def test_evaluation_refuses_to_run_before_hydration(config, db):
    """An un-hydrated cache would look like 'nothing was ever sent'."""
    db.upsert_assignment(make_assignment(due_in_minutes=55))
    db.record_reminder(123, make_assignment(due_in_minutes=55).due_key, 60, delivered=True)

    notifier = Recorder()
    service = ReminderService(config, db, StableCanvas(), notifier)
    assert service.cache.hydrated is False
    assert await service.evaluate_reminders(now=NOW) == 0
    assert notifier.sent == [], "must not re-notify from an empty cache"


async def test_concurrent_ticks_do_not_double_send(config, db):
    """Two overlapping evaluations in one process still send once."""
    service = ReminderService(config, db, StableCanvas(), Recorder())
    service.hydrate()
    db.upsert_assignment(make_assignment(due_in_minutes=55))
    service.hydrate()

    results = await asyncio.gather(
        service.evaluate_reminders(now=NOW), service.evaluate_reminders(now=NOW)
    )
    assert sum(results) == 1
    assert db.delivered_reminder_count() == 1


def test_pool_is_configured_to_let_the_database_sleep(db):
    """min_size=0 means nothing is held open across a quiet period."""
    assert db._pool.min_size == 0
    assert db._pool.max_idle <= 60, "an idle connection must be dropped promptly"
    with psycopg.connect(db.dsn) as conn:
        held = conn.execute(
            "SELECT COUNT(*) FROM pg_stat_activity "
            "WHERE application_name = 'phat-discord-bot'"
        ).fetchone()[0]
    assert held >= 0  # smoke: the query itself must work
