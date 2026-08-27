"""Canvas payload normalisation, submission awareness and time handling."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models import (
    ensure_utc,
    normalize_assignment,
    normalize_course,
    parse_canvas_datetime,
    submission_is_complete,
    to_iso,
)


def test_parses_canvas_utc_timestamps():
    parsed = parse_canvas_datetime("2025-09-05T04:59:00Z")
    assert parsed == datetime(2025, 9, 5, 4, 59, tzinfo=UTC)
    assert parsed.tzinfo is not None


def test_parses_offset_timestamps_into_utc():
    assert parse_canvas_datetime("2025-09-04T23:59:00-05:00") == datetime(
        2025, 9, 5, 4, 59, tzinfo=UTC
    )


@pytest.mark.parametrize("bad", [None, "", "not a date", 12345, "2025-13-45T99:99:99Z"])
def test_malformed_timestamps_return_none_instead_of_raising(bad):
    assert parse_canvas_datetime(bad) is None


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError):
        ensure_utc(datetime(2025, 9, 4, 23, 59))


def test_iso_roundtrip_is_canonical():
    assert to_iso(datetime(2025, 9, 5, 4, 59, tzinfo=UTC)) == "2025-09-05T04:59:00Z"


@pytest.mark.parametrize(
    "submission,expected",
    [
        ({"workflow_state": "submitted"}, True),
        ({"workflow_state": "graded"}, True),
        ({"workflow_state": "pending_review"}, True),
        ({"workflow_state": "unsubmitted", "excused": True}, True),
        ({"workflow_state": "unsubmitted", "submitted_at": "2025-09-01T10:00:00Z"}, True),
        ({"workflow_state": "unsubmitted", "graded_at": "2025-09-01T10:00:00Z", "score": 95}, True),
        ({"workflow_state": "unsubmitted"}, False),
    ],
)
def test_submission_completion_detection(submission, expected):
    complete, known = submission_is_complete(submission)
    assert complete is expected
    assert known is True


@pytest.mark.parametrize("missing", [None, {}, "nope", []])
def test_absent_submission_is_unknown_not_incomplete(missing):
    complete, known = submission_is_complete(missing if isinstance(missing, dict) else None)
    assert complete is False
    assert known is False, "unknown must be distinguishable from confirmed-unsubmitted"


def test_normalize_assignment_maps_every_required_field():
    assignment = normalize_assignment(
        {
            "id": 987,
            "name": "  Project 3  ",
            "due_at": "2025-09-05T04:59:00Z",
            "html_url": "https://canvas.example.edu/courses/1/assignments/987",
            "points_possible": 100,
            "published": True,
            "submission": {"workflow_state": "unsubmitted"},
        },
        course_id=1,
        course_name="CS 240",
        base_url="https://canvas.example.edu",
    )
    assert assignment is not None
    assert assignment.id == 987
    assert assignment.course_id == 1
    assert assignment.course_name == "CS 240"
    assert assignment.name == "Project 3"
    assert assignment.due_at == datetime(2025, 9, 5, 4, 59, tzinfo=UTC)
    assert assignment.html_url.endswith("/assignments/987")
    assert assignment.is_complete is False
    assert assignment.submission_known is True


def test_normalize_assignment_synthesises_a_url_when_canvas_omits_it():
    assignment = normalize_assignment(
        {"id": 5, "name": "X", "due_at": "2025-09-05T04:59:00Z"},
        course_id=7,
        course_name="C",
        base_url="https://canvas.example.edu/",
    )
    assert assignment.html_url == "https://canvas.example.edu/courses/7/assignments/5"


@pytest.mark.parametrize(
    "raw",
    [
        {"name": "no id"},
        {"id": "abc", "name": "bad id"},
        {"id": 1, "workflow_state": "deleted"},
        {"id": 2, "published": False},
        "not a dict",
    ],
)
def test_unusable_assignment_payloads_are_dropped(raw):
    assert normalize_assignment(raw, course_id=1, course_name="C", base_url="https://x") is None


def test_normalize_course_skips_date_restricted_courses():
    assert normalize_course({"id": 1, "access_restricted_by_date": True}) is None
    assert normalize_course({"id": 2, "course_code": "CS240"}).name == "CS240"
