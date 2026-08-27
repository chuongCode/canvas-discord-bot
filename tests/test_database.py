"""Persistence, sorting and reminder-history behaviour."""

from __future__ import annotations

from datetime import timedelta

from app.database import Database
from tests.conftest import NOW, make_assignment


def test_assignments_are_sorted_by_deadline(db):
    for offset, aid in ((600, 3), (60, 1), (300, 2)):
        db.upsert_assignment(make_assignment(assignment_id=aid, due_in_minutes=offset))
    assert [a.id for a in db.active_assignments()] == [1, 2, 3]


def test_incomplete_filter_excludes_completed_and_past_due(db):
    db.upsert_assignment(make_assignment(assignment_id=1, due_in_minutes=60))
    db.upsert_assignment(make_assignment(assignment_id=2, due_in_minutes=90, is_complete=True))
    db.upsert_assignment(make_assignment(assignment_id=3, due_in_minutes=-30))
    assert [a.id for a in db.incomplete_assignments(now=NOW)] == [1]


def test_incomplete_filter_honours_an_until_bound(db):
    db.upsert_assignment(make_assignment(assignment_id=1, due_in_minutes=60))
    db.upsert_assignment(make_assignment(assignment_id=2, due_in_minutes=60 * 24 * 10))
    within_week = db.incomplete_assignments(now=NOW, until=NOW + timedelta(days=7))
    assert [a.id for a in within_week] == [1]


def test_reminder_history_survives_reconnecting(db, database_url):
    assignment = make_assignment()
    db.upsert_assignment(assignment)
    db.record_reminder(assignment.id, assignment.due_key, 60, delivered=True)
    db.close()

    reopened = Database(database_url)
    assert reopened.sent_thresholds(assignment.id, assignment.due_key) == {60}
    reopened.close()


def test_reminder_history_is_scoped_to_the_due_date_version(db):
    monday = make_assignment(due_in_minutes=60)
    db.record_reminder(monday.id, monday.due_key, 60, delivered=True)
    wednesday = make_assignment(due_in_minutes=60 + 2880)
    assert db.sent_thresholds(wednesday.id, wednesday.due_key) == set()


def test_recording_the_same_reminder_twice_is_idempotent(db):
    a = make_assignment()
    db.record_reminder(a.id, a.due_key, 60, delivered=True)
    db.record_reminder(a.id, a.due_key, 60, delivered=True)
    assert db.delivered_reminder_count() == 1


def test_suppressed_reminders_are_not_counted_as_delivered(db):
    a = make_assignment()
    db.record_reminders(a.id, a.due_key, [720, 300], delivered=False)
    db.record_reminder(a.id, a.due_key, 60, delivered=True)
    assert db.sent_thresholds(a.id, a.due_key) == {720, 300, 60}
    assert db.delivered_reminder_count() == 1


def test_deactivate_missing_only_touches_synced_courses(db):
    db.upsert_assignment(make_assignment(assignment_id=1, course_id=1))
    db.upsert_assignment(make_assignment(assignment_id=2, course_id=2))
    db.deactivate_missing(course_ids=[1], keep_ids=[])
    assert {a.id for a in db.active_assignments()} == {2}


def test_upsert_updates_in_place_rather_than_duplicating(db):
    db.upsert_assignment(make_assignment(name="Old name"))
    db.upsert_assignment(make_assignment(name="New name", due_in_minutes=120))
    rows = db.active_assignments()
    assert len(rows) == 1
    assert rows[0].name == "New name"
