"""PostgreSQL persistence.

Deliberately synchronous: this is a single-user bot doing a handful of row
operations, so an async driver would add complexity and no throughput.

The pool is tuned for a database that **suspends when idle** (Neon's scale to
zero, and similar). ``min_size=0`` means no connection is held open across a
quiet period, so nothing on our side keeps the compute awake or has to be torn
down when it sleeps; ``max_idle`` returns the pool to zero shortly after a
burst of work finishes. Waking a suspended database costs a cold start on the
next query, so every operation is retried a bounded number of times — see
:func:`_retrying`.

Timestamps are stored as canonical ISO-8601 UTC *text* (``2025-09-04T23:59:00Z``)
rather than ``timestamptz``. That is deliberate: reminder identity is the tuple
``(assignment_id, due_at, threshold_minutes)``, and keeping the due date as the
exact string :func:`app.models.to_iso` produces means the primary key can never
drift on a round trip. The format sorts lexicographically in deadline order, so
``ORDER BY`` and range comparisons still mean what they say.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import TypeVar

import psycopg
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from .config import redact_database_url
from .models import (
    Assignment,
    SubmissionSnapshot,
    ensure_utc,
    from_iso,
    resolve_submission,
    to_iso,
    utcnow,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

# A suspended database answers the first query with a connection error while it
# wakes. That is expected, not exceptional, so retry briefly before giving up.
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 0.4

# How long an idle connection lingers before the pool closes it. Short enough
# that a suspending database is left alone between bursts of work.
DEFAULT_MAX_IDLE = 30.0

SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = "schema_version"

# Arbitrary but fixed: serialises schema creation when two instances boot at
# once (Render overlaps the old and new container during a deploy).
_MIGRATION_LOCK_ID = 0x0CA5_1A55

_SCHEMA = """
CREATE TABLE IF NOT EXISTS assignments (
    id                BIGINT  PRIMARY KEY,
    course_id         BIGINT  NOT NULL,
    course_name       TEXT    NOT NULL,
    name              TEXT    NOT NULL,
    due_at            TEXT,
    html_url          TEXT    NOT NULL,
    points_possible   DOUBLE PRECISION,
    submission_state  TEXT    NOT NULL DEFAULT 'unknown',
    submitted_at      TEXT,
    graded_at         TEXT,
    excused           BOOLEAN NOT NULL DEFAULT FALSE,
    is_complete       BOOLEAN NOT NULL DEFAULT FALSE,
    submission_known  BOOLEAN NOT NULL DEFAULT FALSE,
    active            BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen_at     TEXT    NOT NULL,
    last_seen_at      TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assignments_due ON assignments (active, is_complete, due_at);

-- Reminder identity is (assignment, due-date version, threshold). Keying on the
-- due date means a rescheduled assignment gets a brand new reminder schedule.
-- The primary key is also the concurrency control: a row is INSERTed to *claim*
-- a reminder before it is delivered, so two instances of the bot racing during
-- a deploy can never both send the same message.
CREATE TABLE IF NOT EXISTS reminders_sent (
    assignment_id     BIGINT  NOT NULL,
    due_at            TEXT    NOT NULL,
    threshold_minutes INTEGER NOT NULL,
    sent_at           TEXT    NOT NULL,
    delivered         BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (assignment_id, due_at, threshold_minutes)
);

CREATE INDEX IF NOT EXISTS idx_reminders_sent_at ON reminders_sent (sent_at);

CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def filter_incomplete(
    assignments: list[Assignment],
    *,
    now: datetime | None = None,
    until: datetime | None = None,
    include_past_due: bool = False,
) -> list[Assignment]:
    """Narrow a list of active assignments to outstanding, upcoming work.

    Lives here as a free function so the database-backed and cache-backed
    callers apply exactly the same rule.
    """
    moment = ensure_utc(now or utcnow())
    results: list[Assignment] = []
    for assignment in assignments:
        if assignment.is_complete or assignment.due_at is None:
            continue
        if not include_past_due and assignment.due_at <= moment:
            continue
        if until is not None and assignment.due_at > ensure_utc(until):
            continue
        results.append(assignment)
    return results


class DatabaseUnavailable(RuntimeError):
    """Every retry of an operation failed. The caller decides what that means."""


class Database:
    """Every persistent read and write the bot performs.

    Each public method runs in its own transaction: psycopg's pool context
    manager commits on a clean exit and rolls back if the block raises, so a
    process killed mid-call leaves no half-written state.

    ``healthy`` records the outcome of the most recent operation. Nothing polls
    the database to keep it current — the health endpoint reports what was last
    observed, precisely so that probing does not wake a sleeping database.
    """

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 0,
        max_size: int = 4,
        connect_timeout: float = 30.0,
        max_idle: float = DEFAULT_MAX_IDLE,
        retry_attempts: int = RETRY_ATTEMPTS,
    ) -> None:
        self.dsn = dsn
        self.safe_dsn = redact_database_url(dsn)
        self._retry_attempts = max(1, retry_attempts)
        self.healthy: bool | None = None
        self.last_contact_at: datetime | None = None
        self._pool: ConnectionPool[Connection[DictRow]] = ConnectionPool(
            dsn,
            # 0: hold nothing open while idle, so a suspending database is free
            # to sleep and we never inherit a connection it killed underneath us.
            min_size=min_size,
            max_size=max_size,
            max_idle=max_idle,
            kwargs={"row_factory": dict_row, "application_name": "phat-discord-bot"},
            # Hand out only connections that answer, so one dropped while the
            # database slept is replaced instead of failing the query.
            check=ConnectionPool.check_connection,
            timeout=connect_timeout,
            open=False,
        )
        # wait=False: with min_size=0 there is nothing to pre-warm, and the
        # first real operation is retried anyway. _initialize below is what
        # actually proves the database is reachable.
        self._pool.open(wait=False)
        self._initialize()

    # ---------------------------------------------------------------- retry

    def _run(self, label: str, operation: Callable[[], T]) -> T:
        """Execute one database operation, retrying transient failures.

        A database that suspends when idle answers the first query after a
        quiet spell with a connection error while it wakes up. Retrying that is
        the difference between a reminder going out and a tick being skipped.

        Only :class:`psycopg.OperationalError` is retried — it covers the
        connection-level faults that a wake-up or a recycled connection
        produce. A programming error or constraint violation is not transient
        and is raised immediately.
        """
        last: Exception | None = None
        for attempt in range(self._retry_attempts):
            try:
                result = operation()
            except psycopg.OperationalError as exc:
                last = exc
                if attempt + 1 < self._retry_attempts:
                    # Jittered backoff: a cold start takes a moment, and two
                    # instances waking together should not retry in lockstep.
                    delay = RETRY_BASE_DELAY * (2**attempt) * (0.5 + random.random())
                    log.info(
                        "Database %s failed (attempt %s/%s), retrying in %.1fs: %s",
                        label, attempt + 1, self._retry_attempts, delay, exc,
                    )
                    time.sleep(delay)
                    continue
            except psycopg.Error:
                self._mark(False)
                raise
            else:
                self._mark(True)
                return result

        self._mark(False)
        raise DatabaseUnavailable(f"{label} failed after {self._retry_attempts} attempts") from last

    def _mark(self, ok: bool) -> None:
        self.healthy = ok
        if ok:
            self.last_contact_at = utcnow()

    # ---------------------------------------------------------------- setup

    def _initialize(self) -> None:
        self._run("schema initialisation", self._initialize_once)
        log.info("PostgreSQL ready at %s (schema v%s)", self.safe_dsn, SCHEMA_VERSION)

    def _initialize_once(self) -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_ID,))
                cur.execute(_SCHEMA)
                cur.execute(
                    "SELECT value FROM app_state WHERE key = %s", (STATE_SCHEMA_VERSION,)
                )
                row = cur.fetchone()
                current = int(row["value"]) if row and row["value"] else 0
                if current < SCHEMA_VERSION:
                    # Future migrations branch on `current` here.
                    cur.execute(
                        "INSERT INTO app_state (key, value) VALUES (%s, %s) "
                        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                        (STATE_SCHEMA_VERSION, str(SCHEMA_VERSION)),
                    )

    def close(self) -> None:
        self._pool.close()

    def ping(self) -> None:
        """Raise if the database is unreachable.

        Deliberately *not* called by the health endpoint any more — probing on
        every request would keep a suspending database awake forever. It exists
        for tests and for an explicit, human-triggered check.
        """

        def _op() -> None:
            with self._pool.connection() as conn:
                conn.execute("SELECT 1")

        self._run("ping", _op)

    # ------------------------------------------------------------ app state

    def get_state(self, key: str) -> str | None:
        def _op() -> str | None:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT value FROM app_state WHERE key = %s", (key,)
                ).fetchone()
            return row["value"] if row else None

        return self._run("get_state", _op)

    def get_states(self, keys: list[str]) -> dict[str, str]:
        """Read several keys in one round trip. Used once, at startup."""
        if not keys:
            return {}

        def _op() -> dict[str, str]:
            with self._pool.connection() as conn:
                rows = conn.execute(
                    "SELECT key, value FROM app_state WHERE key = ANY(%s)", (keys,)
                ).fetchall()
            return {r["key"]: r["value"] for r in rows if r["value"] is not None}

        return self._run("get_states", _op)

    def set_state(self, key: str, value: str) -> None:
        self.set_states({key: value})

    def set_states(self, values: Mapping[str, str]) -> None:
        """Write several keys in one statement.

        Batched because these are only ever written alongside a real change
        now; making that a single round trip keeps the wake-up short.
        """
        if not values:
            return
        items = list(values.items())

        def _op() -> None:
            with self._pool.connection() as conn, conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO app_state (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    items,
                )

        self._run("set_states", _op)

    def get_state_datetime(self, key: str) -> datetime | None:
        return from_iso(self.get_state(key))

    def set_state_datetime(self, key: str, value: datetime) -> None:
        self.set_state(key, to_iso(value) or "")

    # ---------------------------------------------------------- assignments

    def upsert_assignment(self, assignment: Assignment, *, seen_at: datetime | None = None) -> None:
        """Insert or refresh one assignment from a successful Canvas sync.

        Submission state is only overwritten when Canvas actually told us
        something (`submission_known`); otherwise the previously known state is
        preserved so a partial response cannot resurrect completed work.

        The read of the previous state and the write happen in one transaction,
        so a concurrent sync cannot interleave between them.
        """
        now = to_iso(seen_at or utcnow())

        def _op() -> None:
            self._upsert_once(assignment, now)

        self._run("upsert_assignment", _op)

    def _upsert_once(self, assignment: Assignment, now: str | None) -> None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT is_complete, submission_known, submission_state, submitted_at, "
                "graded_at, excused FROM assignments WHERE id = %s FOR UPDATE",
                (assignment.id,),
            ).fetchone()
            existing = (
                SubmissionSnapshot(
                    submission_state=row["submission_state"],
                    submitted_at=from_iso(row["submitted_at"]),
                    graded_at=from_iso(row["graded_at"]),
                    excused=bool(row["excused"]),
                    is_complete=bool(row["is_complete"]),
                    submission_known=bool(row["submission_known"]),
                )
                if row is not None
                else None
            )
            # Same rule the sync loop uses to decide whether a write is needed
            # at all, so the two can never disagree.
            merged = resolve_submission(assignment, existing)

            conn.execute(
                """
                INSERT INTO assignments (
                    id, course_id, course_name, name, due_at, html_url, points_possible,
                    submission_state, submitted_at, graded_at, excused, is_complete,
                    submission_known, active, first_seen_at, last_seen_at, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                    course_id=EXCLUDED.course_id,
                    course_name=EXCLUDED.course_name,
                    name=EXCLUDED.name,
                    due_at=EXCLUDED.due_at,
                    html_url=EXCLUDED.html_url,
                    points_possible=EXCLUDED.points_possible,
                    submission_state=EXCLUDED.submission_state,
                    submitted_at=EXCLUDED.submitted_at,
                    graded_at=EXCLUDED.graded_at,
                    excused=EXCLUDED.excused,
                    is_complete=EXCLUDED.is_complete,
                    submission_known=EXCLUDED.submission_known,
                    active=TRUE,
                    last_seen_at=EXCLUDED.last_seen_at,
                    updated_at=EXCLUDED.updated_at
                """,
                (
                    assignment.id,
                    assignment.course_id,
                    assignment.course_name,
                    assignment.name,
                    to_iso(assignment.due_at),
                    assignment.html_url,
                    assignment.points_possible,
                    merged.submission_state,
                    to_iso(merged.submitted_at),
                    to_iso(merged.graded_at),
                    merged.excused,
                    merged.is_complete,
                    merged.submission_known,
                    now,
                    now,
                    now,
                ),
            )

    def deactivate_missing(self, course_ids: list[int], keep_ids: list[int]) -> int:
        """Mark assignments that vanished from a *successfully* synced course.

        Only courses in ``course_ids`` are touched, so a course whose fetch
        failed keeps every cached row untouched.
        """
        if not course_ids:
            return 0
        sql = (
            "UPDATE assignments SET active=FALSE, updated_at=%s "
            "WHERE active AND course_id = ANY(%s)"
        )
        params: list[object] = [to_iso(utcnow()), course_ids]
        if keep_ids:
            sql += " AND NOT (id = ANY(%s))"
            params.append(keep_ids)
        def _op() -> int:
            with self._pool.connection() as conn:
                return conn.execute(sql, params).rowcount or 0

        return self._run("deactivate_missing", _op)

    def deactivate_courses_not_in(self, active_course_ids: list[int]) -> int:
        """Retire assignments whose course is no longer an active enrollment.

        Only call this after the course listing itself succeeded, otherwise a
        transient Canvas outage would look like "every course ended".
        """
        def _op() -> int:
            with self._pool.connection() as conn:
                if active_course_ids:
                    return conn.execute(
                        "UPDATE assignments SET active=FALSE, updated_at=%s "
                        "WHERE active AND NOT (course_id = ANY(%s))",
                        (to_iso(utcnow()), active_course_ids),
                    ).rowcount or 0
                return conn.execute(
                    "UPDATE assignments SET active=FALSE, updated_at=%s WHERE active",
                    (to_iso(utcnow()),),
                ).rowcount or 0

        return self._run("deactivate_courses_not_in", _op)

    def _row_to_assignment(self, row: DictRow) -> Assignment:
        return Assignment(
            id=row["id"],
            course_id=row["course_id"],
            course_name=row["course_name"],
            name=row["name"],
            due_at=from_iso(row["due_at"]),
            html_url=row["html_url"],
            points_possible=row["points_possible"],
            submission_state=row["submission_state"],
            submitted_at=from_iso(row["submitted_at"]),
            graded_at=from_iso(row["graded_at"]),
            excused=bool(row["excused"]),
            is_complete=bool(row["is_complete"]),
            submission_known=bool(row["submission_known"]),
            active=bool(row["active"]),
        )

    def get_assignment(self, assignment_id: int) -> Assignment | None:
        def _op() -> Assignment | None:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT * FROM assignments WHERE id = %s", (assignment_id,)
                ).fetchone()
            return self._row_to_assignment(row) if row else None

        return self._run("get_assignment", _op)

    def active_assignments(self) -> list[Assignment]:
        """Every active assignment that still has a due date, soonest first."""
        return [a for a in self.all_active_assignments() if a.due_at is not None]

    def all_active_assignments(self) -> list[Assignment]:
        """Every active assignment, dated or not, soonest deadline first.

        This is what the in-memory cache is built from at startup. It includes
        undated assignments — they generate no reminders, but the sync loop
        still has to know they exist to decide whether a retirement UPDATE is
        needed, and leaving them out would strand them as active forever.
        """

        def _op() -> list[Assignment]:
            with self._pool.connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM assignments WHERE active "
                    "ORDER BY due_at ASC NULLS LAST"
                ).fetchall()
            return [self._row_to_assignment(r) for r in rows]

        return self._run("all_active_assignments", _op)

    def incomplete_assignments(
        self,
        *,
        now: datetime | None = None,
        until: datetime | None = None,
        include_past_due: bool = False,
    ) -> list[Assignment]:
        """Active, not-yet-complete assignments with a due date, soonest first."""
        return filter_incomplete(
            self.active_assignments(), now=now, until=until, include_past_due=include_past_due
        )

    def _count(self, sql: str) -> int:
        def _op() -> int:
            with self._pool.connection() as conn:
                row = conn.execute(sql).fetchone()
            # COUNT(*) always yields one row; the guard is for the checker.
            return int(row["n"]) if row else 0

        return self._run("count", _op)

    def count_monitored(self) -> int:
        return self._count(
            "SELECT COUNT(*) AS n FROM assignments WHERE active AND due_at IS NOT NULL"
        )

    def is_empty(self) -> bool:
        return self._count("SELECT COUNT(*) AS n FROM assignments") == 0

    # ------------------------------------------------------------ reminders

    def sent_thresholds(self, assignment_id: int, due_key: str) -> set[int]:
        """Thresholds already accounted for at this assignment's due-date version."""
        def _op() -> set[int]:
            with self._pool.connection() as conn:
                rows = conn.execute(
                    "SELECT threshold_minutes FROM reminders_sent "
                    "WHERE assignment_id = %s AND due_at = %s",
                    (assignment_id, due_key),
                ).fetchall()
            return {int(r["threshold_minutes"]) for r in rows}

        return self._run("sent_thresholds", _op)

    def sent_thresholds_map(self) -> dict[tuple[int, str], set[int]]:
        """Every recorded threshold for every live assignment, in one round trip.

        The reminder loop needs this for each assignment on every tick. Against
        a networked database, one query beats one-per-assignment. It is only a
        pre-filter: :meth:`claim_reminder` remains the authority on whether a
        reminder may be sent.
        """
        def _op() -> dict[tuple[int, str], set[int]]:
            with self._pool.connection() as conn:
                rows = conn.execute(
                    "SELECT r.assignment_id, r.due_at, r.threshold_minutes "
                    "FROM reminders_sent r "
                    "JOIN assignments a ON a.id = r.assignment_id AND a.due_at = r.due_at "
                    "WHERE a.active"
                ).fetchall()
            recorded: dict[tuple[int, str], set[int]] = {}
            for row in rows:
                key = (int(row["assignment_id"]), row["due_at"])
                recorded.setdefault(key, set()).add(int(row["threshold_minutes"]))
            return recorded

        return self._run("sent_thresholds_map", _op)

    def claim_reminder(
        self,
        assignment_id: int,
        due_key: str,
        threshold_minutes: int,
        *,
        sent_at: datetime | None = None,
    ) -> bool:
        """Atomically reserve one reminder slot before attempting delivery.

        Returns True only for the caller that won the insert. This is what makes
        delivery exactly-once even when two instances of the bot overlap during
        a redeploy: the loser sees False and stays quiet. Release the claim with
        :meth:`release_reminder` if the message does not actually go out.
        """
        stamp = to_iso(sent_at or utcnow())

        def _op() -> bool:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "INSERT INTO reminders_sent "
                    "(assignment_id, due_at, threshold_minutes, sent_at, delivered) "
                    "VALUES (%s,%s,%s,%s,FALSE) ON CONFLICT DO NOTHING RETURNING 1 AS claimed",
                    (assignment_id, due_key, threshold_minutes, stamp),
                ).fetchone()
            return row is not None

        # Retried on a connection fault: a claim lost to a cold start would
        # silently drop the reminder for this due-date version forever.
        return self._run("claim_reminder", _op)

    def mark_reminder_delivered(
        self,
        assignment_id: int,
        due_key: str,
        threshold_minutes: int,
        *,
        sent_at: datetime | None = None,
    ) -> None:
        """Promote a claim to a delivered reminder."""
        stamp = to_iso(sent_at or utcnow())

        def _op() -> None:
            with self._pool.connection() as conn:
                conn.execute(
                    "UPDATE reminders_sent SET delivered = TRUE, sent_at = %s "
                    "WHERE assignment_id = %s AND due_at = %s AND threshold_minutes = %s",
                    (stamp, assignment_id, due_key, threshold_minutes),
                )

        self._run("mark_reminder_delivered", _op)

    def release_reminder(self, assignment_id: int, due_key: str, threshold_minutes: int) -> None:
        """Give back a claim whose delivery failed, so the next tick retries it."""
        def _op() -> None:
            with self._pool.connection() as conn:
                conn.execute(
                    "DELETE FROM reminders_sent WHERE assignment_id = %s AND due_at = %s "
                    "AND threshold_minutes = %s AND NOT delivered",
                    (assignment_id, due_key, threshold_minutes),
                )

        self._run("release_reminder", _op)

    def record_reminder(
        self,
        assignment_id: int,
        due_key: str,
        threshold_minutes: int,
        *,
        delivered: bool,
        sent_at: datetime | None = None,
    ) -> None:
        self.record_reminders(
            assignment_id, due_key, [threshold_minutes], delivered=delivered, sent_at=sent_at
        )

    def record_reminders(
        self,
        assignment_id: int,
        due_key: str,
        thresholds: list[int],
        *,
        delivered: bool,
        sent_at: datetime | None = None,
    ) -> None:
        """Record several thresholds at once, atomically."""
        if not thresholds:
            return
        stamp = to_iso(sent_at or utcnow())
        rows = [(assignment_id, due_key, t, stamp, delivered) for t in thresholds]

        def _op() -> None:
            with self._pool.connection() as conn, conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO reminders_sent "
                    "(assignment_id, due_at, threshold_minutes, sent_at, delivered) "
                    "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    rows,
                )

        self._run("record_reminders", _op)

    def delivered_reminder_count(self) -> int:
        return self._count("SELECT COUNT(*) AS n FROM reminders_sent WHERE delivered")

    # -------------------------------------------------------------- cleanup

    def prune(self, *, older_than: timedelta = timedelta(days=60)) -> None:
        """Drop long-dead rows so the database stays small forever."""
        cutoff = to_iso(utcnow() - older_than)

        def _op() -> None:
            with self._pool.connection() as conn:
                conn.execute("DELETE FROM reminders_sent WHERE sent_at < %s", (cutoff,))
                conn.execute(
                    "DELETE FROM assignments WHERE NOT active AND updated_at < %s", (cutoff,)
                )

        try:
            self._run("prune", _op)
        except (psycopg.Error, DatabaseUnavailable):
            # Housekeeping only; never block startup on it.
            log.warning("Could not prune old rows; continuing", exc_info=True)
