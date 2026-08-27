"""Timezone conversion, DST correctness and human-readable output."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.formatting import (
    format_due,
    format_due_short,
    humanize_minutes,
    submission_status_text,
    tier_for,
)

CHICAGO = ZoneInfo("America/Chicago")

from tests.conftest import make_assignment  # noqa: E402


def test_utc_is_rendered_in_the_configured_timezone():
    # 04:59 UTC on Sep 5 is 11:59 PM CDT on Sep 4.
    due = datetime(2025, 9, 5, 4, 59, tzinfo=UTC)
    assert format_due(due, CHICAGO) == "Thursday, September 4 at 11:59 PM"


def test_daylight_saving_transition_is_handled():
    """Same UTC clock time, opposite sides of the US DST change."""
    summer = datetime(2025, 7, 1, 17, 0, tzinfo=UTC)  # CDT, UTC-5
    winter = datetime(2025, 12, 1, 17, 0, tzinfo=UTC)  # CST, UTC-6
    assert format_due(summer, CHICAGO).endswith("12:00 PM")
    assert format_due(winter, CHICAGO).endswith("11:00 AM")


def test_midnight_and_noon_render_as_twelve():
    assert "12:30 AM" in format_due(datetime(2025, 6, 1, 5, 30, tzinfo=UTC), CHICAGO)
    assert "12:30 PM" in format_due(datetime(2025, 6, 1, 17, 30, tzinfo=UTC), CHICAGO)


def test_short_format_is_compact():
    due = datetime(2025, 9, 5, 4, 59, tzinfo=UTC)
    assert format_due_short(due, CHICAGO) == "Thu Sep 4, 11:59 PM"


def test_a_non_us_timezone_also_works():
    due = datetime(2025, 9, 5, 4, 59, tzinfo=UTC)
    assert format_due(due, ZoneInfo("Asia/Tokyo")) == "Friday, September 5 at 1:59 PM"


@pytest.mark.parametrize(
    "minutes,expected",
    [
        (1, "1 minute"),
        (30, "30 minutes"),
        (60, "1 hour"),
        (65, "1 hour 5 minutes"),
        (300, "5 hours"),
        (720, "12 hours"),
        (1440, "1 day"),
        (2880, "2 days"),
        (3000, "2 days 2 hours"),
        (0.4, "less than a minute"),
        (0, "0 minutes"),
    ],
)
def test_humanize_minutes(minutes, expected):
    assert humanize_minutes(minutes) == expected


def test_urgency_escalates_as_the_deadline_approaches():
    far = tier_for(720)
    near = tier_for(60)
    critical = tier_for(5)
    assert far != near != critical
    assert critical[0] == "🚨"


def test_status_text_distinguishes_unknown_from_unsubmitted():
    unknown = make_assignment(submission_known=False, submission_state="unknown")
    unsubmitted = make_assignment(submission_known=True, submission_state="unsubmitted")
    assert "Unknown" in submission_status_text(unknown)
    assert submission_status_text(unsubmitted) == "Not submitted"
    assert submission_status_text(make_assignment(submission_state="graded")) == "Graded"
