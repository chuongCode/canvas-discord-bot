"""Human-readable rendering.

Everything here is pure and Discord-free so it can be unit tested. UTC goes in,
localised strings come out; the configured timezone is used for display only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import Assignment, ensure_utc

# Escalation tiers, checked in order: (threshold_minutes_at_or_below, emoji, colour)
_TIERS: tuple[tuple[int, str, int], ...] = (
    (15, "🚨", 0xE01B24),  # red      - final call
    (60, "⏰", 0xF57C00),  # orange   - within the hour
    (300, "🔔", 0xF6D32D),  # yellow  - a few hours out
    (10**9, "📌", 0x3584E4),  # blue   - heads up
)


def local(value: datetime, tz: ZoneInfo) -> datetime:
    """Convert an aware UTC datetime into the display timezone (DST-correct)."""
    return ensure_utc(value).astimezone(tz)


def format_due(value: datetime, tz: ZoneInfo) -> str:
    """e.g. "Friday, September 4 at 11:59 PM"."""
    dt = local(value, tz)
    hour = dt.hour % 12 or 12
    meridiem = "AM" if dt.hour < 12 else "PM"
    return f"{dt:%A}, {dt:%B} {dt.day} at {hour}:{dt:%M} {meridiem}"


def format_due_short(value: datetime, tz: ZoneInfo) -> str:
    """e.g. "Fri Sep 4, 11:59 PM" — for list commands."""
    dt = local(value, tz)
    hour = dt.hour % 12 or 12
    meridiem = "AM" if dt.hour < 12 else "PM"
    return f"{dt:%a} {dt:%b} {dt.day}, {hour}:{dt:%M} {meridiem}"


def humanize_minutes(minutes: float) -> str:
    """Turn a minute count into "2 days", "1 hour 5 minutes", "30 minutes"."""
    total = int(round(minutes))
    if total <= 0:
        return "less than a minute" if minutes > 0 else "0 minutes"
    if total < 60:
        return f"{total} minute{'s' if total != 1 else ''}"

    hours, mins = divmod(total, 60)
    if hours < 24:
        head = f"{hours} hour{'s' if hours != 1 else ''}"
        return f"{head} {mins} minute{'s' if mins != 1 else ''}" if mins else head

    days, rem_hours = divmod(hours, 24)
    head = f"{days} day{'s' if days != 1 else ''}"
    return f"{head} {rem_hours} hour{'s' if rem_hours != 1 else ''}" if rem_hours else head


def threshold_label(threshold_minutes: int) -> str:
    """The label used in a reminder headline, e.g. 720 -> "12 hours"."""
    return humanize_minutes(threshold_minutes)


def tier_for(threshold_minutes: int) -> tuple[str, int]:
    """(emoji, embed colour) for a reminder threshold."""
    for ceiling, emoji, colour in _TIERS:
        if threshold_minutes <= ceiling:
            return emoji, colour
    return _TIERS[-1][1], _TIERS[-1][2]


def submission_status_text(assignment: Assignment) -> str:
    """Short, honest description of what Canvas told us about submission."""
    if assignment.excused:
        return "Excused"
    if not assignment.submission_known:
        return "Unknown (Canvas did not report a submission)"
    state = assignment.submission_state
    if state == "graded":
        return "Graded"
    if state == "pending_review":
        return "Submitted (pending review)"
    if state == "submitted" or assignment.submitted_at is not None:
        return "Submitted"
    return "Not submitted"


@dataclass(frozen=True)
class ReminderText:
    title: str
    colour: int
    remaining_text: str


def build_reminder_text(
    assignment: Assignment,
    threshold_minutes: int,
    minutes_remaining: float,
) -> ReminderText:
    """Headline for a reminder.

    The headline states the *actual* time left, not the threshold: after
    downtime the 30-minute reminder may legitimately fire with 21 minutes left,
    and saying "due in 30 minutes" then would be a lie.
    """
    emoji, colour = tier_for(threshold_minutes)
    return ReminderText(
        title=f"{emoji} Assignment due in {humanize_minutes(minutes_remaining)}",
        colour=colour,
        remaining_text=humanize_minutes(minutes_remaining),
    )


def assignment_line(assignment: Assignment, tz: ZoneInfo, now: datetime) -> str:
    """One bullet for /due, /today, /week."""
    remaining = assignment.minutes_remaining(now)
    when = format_due_short(assignment.due_at, tz) if assignment.due_at else "No due date"
    tail = f" · in {humanize_minutes(remaining)}" if remaining and remaining > 0 else " · past due"
    return f"**{assignment.name}**\n{assignment.course_name} — {when}{tail}"
