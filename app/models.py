"""Domain models and Canvas payload normalisation.

All datetimes handled here are timezone-aware and stored in UTC. Naive
datetimes are rejected outright so a deadline calculation can never silently
drift by the local UTC offset.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

# Canvas submission workflow states that mean "there is nothing left to do".
COMPLETED_WORKFLOW_STATES = frozenset({"submitted", "graded", "pending_review"})

# Assignment states we refuse to monitor.
INACTIVE_ASSIGNMENT_STATES = frozenset({"deleted", "unpublished", "duplicating", "failed_to_duplicate"})


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Normalise an aware datetime to UTC. Naive input is a programming error."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("naive datetime is not allowed; deadlines must be tz-aware")
    return value.astimezone(UTC)


def parse_canvas_datetime(raw: Any) -> datetime | None:
    """Parse a Canvas ISO-8601 timestamp into an aware UTC datetime.

    Canvas returns e.g. "2025-09-05T04:59:00Z". Anything unparseable returns
    None rather than raising, because a single malformed field must not take
    down a whole sync.
    """
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Canvas documents UTC; assume it rather than the host's local zone.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def to_iso(value: datetime | None) -> str | None:
    """Serialise an aware datetime to a canonical UTC ISO-8601 string."""
    if value is None:
        return None
    return ensure_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def from_iso(value: str | None) -> datetime | None:
    """Inverse of :func:`to_iso` for values read back out of SQLite."""
    return parse_canvas_datetime(value)


@dataclass(frozen=True)
class SubmissionSnapshot:
    """The submission half of a persisted assignment row."""

    submission_state: str = "unknown"
    submitted_at: datetime | None = None
    graded_at: datetime | None = None
    excused: bool = False
    is_complete: bool = False
    submission_known: bool = False


def resolve_submission(
    incoming: Assignment, existing: SubmissionSnapshot | None
) -> SubmissionSnapshot:
    """Decide the submission state to persist for a freshly synced assignment.

    Canvas sometimes returns an assignment with no usable submission object.
    When that happens the previously known state wins, so a partial response
    cannot resurrect work that is already done.

    This is the single definition of that rule. Both the database write and the
    in-memory "has anything actually changed?" comparison call it, so the two
    can never drift apart and cause a spurious write every sync.
    """
    if incoming.submission_known:
        return SubmissionSnapshot(
            submission_state=incoming.submission_state,
            submitted_at=incoming.submitted_at,
            graded_at=incoming.graded_at,
            excused=incoming.excused,
            is_complete=incoming.is_complete,
            submission_known=True,
        )
    if existing is not None:
        return existing
    return SubmissionSnapshot()


@dataclass(frozen=True)
class Course:
    id: int
    name: str


@dataclass
class Assignment:
    """Normalised Canvas assignment plus our derived completion state."""

    id: int
    course_id: int
    course_name: str
    name: str
    due_at: datetime | None
    html_url: str
    points_possible: float | None = None

    # Submission state
    submission_state: str = "unknown"
    submitted_at: datetime | None = None
    graded_at: datetime | None = None
    excused: bool = False
    is_complete: bool = False
    submission_known: bool = False

    active: bool = True

    @property
    def due_key(self) -> str:
        """Reminder-schedule identity for the *current* due date.

        Reminder history is keyed on (assignment_id, due_key, threshold), so a
        due-date change automatically produces a fresh reminder schedule
        instead of permanently suppressing already-sent thresholds.
        """
        return to_iso(self.due_at) or ""

    def minutes_remaining(self, now: datetime) -> float | None:
        if self.due_at is None:
            return None
        return (ensure_utc(self.due_at) - ensure_utc(now)).total_seconds() / 60.0

    @property
    def submission(self) -> SubmissionSnapshot:
        return SubmissionSnapshot(
            submission_state=self.submission_state,
            submitted_at=self.submitted_at,
            graded_at=self.graded_at,
            excused=self.excused,
            is_complete=self.is_complete,
            submission_known=self.submission_known,
        )

    def with_submission(self, submission: SubmissionSnapshot) -> Assignment:
        """A copy carrying ``submission``, leaving everything else alone."""
        return replace(
            self,
            submission_state=submission.submission_state,
            submitted_at=submission.submitted_at,
            graded_at=submission.graded_at,
            excused=submission.excused,
            is_complete=submission.is_complete,
            submission_known=submission.submission_known,
        )

    def persisted_fields(self) -> tuple:
        """Everything about this assignment that is actually stored.

        Excludes ``first_seen_at``/``last_seen_at``/``updated_at``, which are
        bookkeeping columns nothing reads back except pruning. Two assignments
        with equal ``persisted_fields()`` produce an identical row, so writing
        one over the other would be a no-op — which is how the sync loop avoids
        touching the database when Canvas has not changed.
        """
        return (
            self.id,
            self.course_id,
            self.course_name,
            self.name,
            to_iso(self.due_at),
            self.html_url,
            self.points_possible,
            self.submission_state,
            to_iso(self.submitted_at),
            to_iso(self.graded_at),
            self.excused,
            self.is_complete,
            self.submission_known,
            self.active,
        )


def submission_is_complete(submission: dict[str, Any] | None) -> tuple[bool, bool]:
    """Decide whether a Canvas submission counts as done.

    Returns ``(is_complete, is_known)``. ``is_known`` is False when Canvas gave
    us no usable submission object -- callers must then keep whatever they
    already knew rather than assuming the work is outstanding.
    """
    if not isinstance(submission, dict) or not submission:
        return False, False

    if submission.get("excused") is True:
        return True, True

    state = submission.get("workflow_state")
    if isinstance(state, str) and state in COMPLETED_WORKFLOW_STATES:
        return True, True

    if submission.get("submitted_at"):
        return True, True

    # A teacher-entered grade with no online submission (paper, in-class) still
    # means the work is done.
    if submission.get("graded_at") and submission.get("score") is not None:
        return True, True

    if isinstance(state, str) and state:
        return False, True

    return False, False


def normalize_assignment(
    raw: dict[str, Any],
    *,
    course_id: int,
    course_name: str,
    base_url: str,
) -> Assignment | None:
    """Convert one Canvas assignment payload into an :class:`Assignment`.

    Returns None for payloads we cannot or should not monitor (missing id,
    unpublished, deleted). Malformed optional fields are tolerated.
    """
    if not isinstance(raw, dict):
        return None

    try:
        assignment_id = int(raw["id"])
    except (KeyError, TypeError, ValueError):
        return None

    if raw.get("workflow_state") in INACTIVE_ASSIGNMENT_STATES:
        return None
    if raw.get("published") is False:
        return None

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        name = f"Assignment {assignment_id}"

    html_url = raw.get("html_url")
    if not isinstance(html_url, str) or not html_url.startswith("http"):
        html_url = f"{base_url.rstrip('/')}/courses/{course_id}/assignments/{assignment_id}"

    points = raw.get("points_possible")
    if not isinstance(points, (int, float)):
        points = None

    submission = raw.get("submission")
    is_complete, known = submission_is_complete(submission if isinstance(submission, dict) else None)

    sub_state = "unknown"
    submitted_at = graded_at = None
    excused = False
    if isinstance(submission, dict) and submission:
        raw_state = submission.get("workflow_state")
        sub_state = raw_state if isinstance(raw_state, str) and raw_state else "unknown"
        submitted_at = parse_canvas_datetime(submission.get("submitted_at"))
        graded_at = parse_canvas_datetime(submission.get("graded_at"))
        excused = submission.get("excused") is True

    return Assignment(
        id=assignment_id,
        course_id=course_id,
        course_name=course_name,
        name=name.strip(),
        due_at=parse_canvas_datetime(raw.get("due_at")),
        html_url=html_url,
        points_possible=float(points) if points is not None else None,
        submission_state=sub_state,
        submitted_at=submitted_at,
        graded_at=graded_at,
        excused=excused,
        is_complete=is_complete,
        submission_known=known,
    )


def normalize_course(raw: dict[str, Any]) -> Course | None:
    """Convert one Canvas course payload into a :class:`Course`."""
    if not isinstance(raw, dict):
        return None
    if raw.get("access_restricted_by_date"):
        return None
    try:
        course_id = int(raw["id"])
    except (KeyError, TypeError, ValueError):
        return None
    name = raw.get("name") or raw.get("course_code")
    if not isinstance(name, str) or not name.strip():
        name = f"Course {course_id}"
    return Course(id=course_id, name=name.strip())
