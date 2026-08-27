"""End-to-end service behaviour with a real PostgreSQL schema and a fake Canvas."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.canvas_client import CanvasError
from app.database import Database
from app.models import to_iso
from app.reminder_service import ReminderService
from tests.conftest import NOW, make_assignment, seed


class FakeCanvas:
    """Stands in for CanvasClient. Records calls, can be told to fail."""

    base_url = "https://canvas.example.edu"

    def __init__(self, courses=None, assignments=None):
        self.courses = courses if courses is not None else [{"id": 1, "name": "CS 240"}]
        self.assignments = assignments or {}
        self.fail_courses = False
        self.fail_course_ids: set[int] = set()
        self.calls = 0

    async def get_active_courses(self, *, students_only=True):
        if self.fail_courses:
            raise CanvasError("canvas is down")
        return self.courses

    async def get_course_assignments(self, course_id: int):
        self.calls += 1
        if course_id in self.fail_course_ids:
            raise CanvasError("course unreadable")
        return self.assignments.get(course_id, [])


class Recorder:
    """Notifier double. Returns whatever `ok` says, remembering every call."""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.sent: list[tuple[int, int]] = []

    async def __call__(self, assignment, threshold, remaining):
        self.sent.append((assignment.id, threshold))
        return self.ok


def iso_in(minutes: float) -> str:
    """A Canvas-style UTC timestamp `minutes` from real now.

    Sync filters by a window around the wall clock, so these must be relative.
    """
    return to_iso(datetime.now(UTC) + timedelta(minutes=minutes))


def canvas_assignment(assignment_id, due_at, *, submission=None, name="Project 3"):
    payload = {
        "id": assignment_id,
        "name": name,
        "due_at": due_at,
        "published": True,
        "html_url": f"https://canvas.example.edu/courses/1/assignments/{assignment_id}",
    }
    if submission is not None:
        payload["submission"] = submission
    return payload


def build(config, db, canvas, notifier):
    """A service with its cache loaded, exactly as startup does it."""
    service = ReminderService(config, db, canvas, notifier)
    service.hydrate()
    return service


# --------------------------------------------------------------- reminders


async def test_reminder_is_sent_once_and_persists_across_restart(config, db):
    db.upsert_assignment(make_assignment(due_in_minutes=55))
    notifier = Recorder()
    service = build(config, db, FakeCanvas(), notifier)

    assert await service.evaluate_reminders(now=NOW) == 1
    assert notifier.sent == [(123, 60)]

    # Same process, later tick.
    assert await service.evaluate_reminders(now=NOW + timedelta(minutes=1)) == 0

    # Simulate a restart: brand new objects, same database.
    db.close()
    reopened = Database(config.database_url)
    restarted = build(config, reopened, FakeCanvas(), Recorder())
    assert await restarted.evaluate_reminders(now=NOW + timedelta(minutes=2)) == 0
    reopened.close()


async def test_downtime_catchup_sends_one_reminder_not_a_backlog(config, db):
    db.upsert_assignment(make_assignment(due_in_minutes=20))
    notifier = Recorder()
    service = build(config, db, FakeCanvas(), notifier)

    assert await service.evaluate_reminders(now=NOW) == 1
    assert notifier.sent == [(123, 30)]

    # The skipped thresholds are recorded so they can never fire late.
    recorded = db.sent_thresholds(123, make_assignment(due_in_minutes=20).due_key)
    assert recorded == {720, 300, 120, 60, 30}


async def test_failed_delivery_is_retried_on_the_next_tick(config, db):
    db.upsert_assignment(make_assignment(due_in_minutes=55))
    failing = Recorder(ok=False)
    service = build(config, db, FakeCanvas(), failing)

    assert await service.evaluate_reminders(now=NOW) == 0
    assert failing.sent == [(123, 60)]

    failing.ok = True
    assert await service.evaluate_reminders(now=NOW + timedelta(seconds=45)) == 1


async def test_notifier_exception_does_not_mark_the_reminder_sent(config, db):
    db.upsert_assignment(make_assignment(due_in_minutes=55))

    async def explode(*_args):
        raise RuntimeError("discord exploded")

    service = build(config, db, FakeCanvas(), explode)
    assert await service.evaluate_reminders(now=NOW) == 0
    # The threshold we tried to deliver stays unrecorded so it is retried; the
    # stale ones we deliberately skipped stay suppressed.
    recorded = db.sent_thresholds(123, make_assignment(due_in_minutes=55).due_key)
    assert 60 not in recorded

    service.notifier = Recorder()
    assert await service.evaluate_reminders(now=NOW) == 1


async def test_submitted_assignments_are_never_reminded(config, db):
    db.upsert_assignment(
        make_assignment(due_in_minutes=55, is_complete=True, submission_state="submitted")
    )
    notifier = Recorder()
    service = build(config, db, FakeCanvas(), notifier)
    assert await service.evaluate_reminders(now=NOW) == 0
    assert notifier.sent == []


async def test_submitting_mid_schedule_stops_future_reminders(config, db):
    assignment = make_assignment(due_in_minutes=55)
    db.upsert_assignment(assignment)
    notifier = Recorder()
    service = build(config, db, FakeCanvas(), notifier)
    assert await service.evaluate_reminders(now=NOW) == 1

    assignment.is_complete = True
    assignment.submission_state = "submitted"
    seed(service, assignment)

    # 25 minutes later the 30m threshold is crossed, but the work is done.
    assert await service.evaluate_reminders(now=NOW + timedelta(minutes=30)) == 0
    assert notifier.sent == [(123, 60)]


async def test_due_date_change_creates_a_fresh_reminder_schedule(config, db):
    """12h reminder sent for Monday; professor moves it to Wednesday."""
    monday = make_assignment(due_in_minutes=700)
    db.upsert_assignment(monday)
    notifier = Recorder()
    service = build(config, db, FakeCanvas(), notifier)
    assert await service.evaluate_reminders(now=NOW) == 1
    assert notifier.sent == [(123, 720)]

    wednesday = make_assignment(due_in_minutes=700 + 2 * 24 * 60)
    seed(service, wednesday)

    # Nothing fires immediately for the new, distant deadline...
    assert await service.evaluate_reminders(now=NOW + timedelta(minutes=1)) == 0
    # ...but the 12h reminder fires again against the new due date.
    later = NOW + timedelta(minutes=700 + 2 * 24 * 60 - 700)
    assert await service.evaluate_reminders(now=later) == 1
    assert notifier.sent[-1] == (123, 720)


async def test_first_run_suppression_records_a_baseline_without_notifying(config):
    from dataclasses import replace

    quiet = replace(config, suppress_reminders_on_first_run=True)
    fresh = Database(config.database_url)
    notifier = Recorder()
    service = build(quiet, fresh, FakeCanvas(), notifier)

    seed(service, make_assignment(due_in_minutes=20))
    assert await service.evaluate_reminders(now=NOW) == 0
    assert notifier.sent == []

    # Suppression applies only to that first pass; new work notifies normally.
    seed(service, make_assignment(assignment_id=999, due_in_minutes=55))
    assert await service.evaluate_reminders(now=NOW) == 1
    assert notifier.sent == [(999, 60)]
    fresh.close()


# --------------------------------------------------------------------- sync


async def test_sync_discovers_courses_and_assignments(config, db):
    canvas = FakeCanvas(
        courses=[{"id": 1, "name": "CS 240"}, {"id": 2, "name": "MATH 301"}],
        assignments={
            1: [canvas_assignment(11, iso_in(600))],
            2: [canvas_assignment(22, iso_in(2000), name="Homework 4")],
        },
    )
    service = build(config, db, canvas, Recorder())
    result = await service.sync_canvas()

    assert result.ok and result.complete
    assert result.assignments_tracked == 2
    stored = {a.id: a for a in db.active_assignments()}
    assert stored[11].course_name == "CS 240"
    assert stored[22].name == "Homework 4"


async def test_canvas_outage_never_wipes_cached_state(config, db):
    db.upsert_assignment(make_assignment(due_in_minutes=55))
    canvas = FakeCanvas()
    canvas.fail_courses = True
    service = build(config, db, canvas, Recorder())

    result = await service.sync_canvas()
    assert result.ok is False
    assert len(db.active_assignments()) == 1, "cache must survive a Canvas outage"

    # And reminders keep working off the cache.
    notifier = Recorder()
    service.notifier = notifier
    assert await service.evaluate_reminders(now=NOW) == 1


async def test_one_unreadable_course_does_not_retire_the_others(config, db):
    canvas = FakeCanvas(
        courses=[{"id": 1, "name": "CS 240"}, {"id": 2, "name": "MATH 301"}],
        assignments={
            1: [canvas_assignment(11, iso_in(600))],
            2: [canvas_assignment(22, iso_in(2000))],
        },
    )
    service = build(config, db, canvas, Recorder())
    await service.sync_canvas()
    assert len(db.active_assignments()) == 2

    canvas.fail_course_ids = {2}
    result = await service.sync_canvas()
    assert result.ok and not result.complete
    assert {a.id for a in db.active_assignments()} == {11, 22}


async def test_deleted_assignment_stops_generating_reminders(config, db):
    canvas = FakeCanvas(assignments={1: [canvas_assignment(11, iso_in(600))]})
    service = build(config, db, canvas, Recorder())
    await service.sync_canvas()
    assert len(db.active_assignments()) == 1

    canvas.assignments[1] = []
    await service.sync_canvas()
    assert db.active_assignments() == []


async def test_assignment_losing_its_due_date_stops_reminders(config, db):
    canvas = FakeCanvas(assignments={1: [canvas_assignment(11, iso_in(600))]})
    service = build(config, db, canvas, Recorder())
    await service.sync_canvas()
    canvas.assignments[1] = [canvas_assignment(11, None)]
    await service.sync_canvas()
    assert db.active_assignments() == []


async def test_ended_course_retires_its_assignments(config, db):
    canvas = FakeCanvas(assignments={1: [canvas_assignment(11, iso_in(600))]})
    service = build(config, db, canvas, Recorder())
    await service.sync_canvas()
    canvas.courses = []
    await service.sync_canvas()
    assert db.active_assignments() == []


async def test_sync_picks_up_a_changed_due_date(config, db):
    canvas = FakeCanvas(assignments={1: [canvas_assignment(11, iso_in(600))]})
    service = build(config, db, canvas, Recorder())
    await service.sync_canvas()

    canvas.assignments[1] = [canvas_assignment(11, iso_in(3000))]
    await service.sync_canvas()
    updated = db.get_assignment(11)
    assert abs((updated.due_at - datetime.now(UTC)).total_seconds() / 60 - 3000) < 5


async def test_unknown_submission_state_does_not_overwrite_a_known_one(config, db):
    """A response missing `submission` must not resurrect completed work."""
    canvas = FakeCanvas(
        assignments={
            1: [canvas_assignment(11, iso_in(600), submission={"workflow_state": "graded"})]
        }
    )
    service = build(config, db, canvas, Recorder())
    await service.sync_canvas()
    assert db.get_assignment(11).is_complete is True

    canvas.assignments[1] = [canvas_assignment(11, iso_in(600))]  # no submission key
    await service.sync_canvas()
    assert db.get_assignment(11).is_complete is True


async def test_assignments_far_outside_the_window_are_not_tracked(config, db):
    canvas = FakeCanvas(assignments={1: [canvas_assignment(11, iso_in(400 * 24 * 60))]})
    service = build(config, db, canvas, Recorder())
    result = await service.sync_canvas()
    assert result.assignments_tracked == 0


async def test_malformed_payloads_do_not_break_the_sync(config, db):
    canvas = FakeCanvas(
        assignments={
            1: [
                {"id": None},
                {"name": "no id at all"},
                canvas_assignment(11, iso_in(600)),
                {"id": 12, "name": "bad date", "due_at": "yesterday"},
            ]
        }
    )
    service = build(config, db, canvas, Recorder())
    result = await service.sync_canvas()
    assert result.ok
    assert {a.id for a in db.active_assignments()} == {11}
