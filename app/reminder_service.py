"""Canvas synchronisation and threshold-crossing reminder evaluation.

The scheduling rule, in one paragraph:

A threshold T is *crossed* once the time remaining until the deadline falls to
or below T minutes. On every evaluation we look at which thresholds are crossed
but not yet recorded for this assignment's current due-date version. If there
are several -- because the process was asleep, restarting or simply polling
between ticks -- we deliver only the smallest (the most recently crossed, and
therefore most urgent) and record the rest as skipped. That gives exactly-once
delivery per (assignment, due date, threshold) with no stale backlog.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import psycopg

from .cache import StateCache
from .canvas_client import CanvasAuthError, CanvasClient, CanvasError
from .config import Config
from .database import Database, DatabaseUnavailable, filter_incomplete
from .models import (
    Assignment,
    ensure_utc,
    from_iso,
    normalize_assignment,
    normalize_course,
    resolve_submission,
    to_iso,
    utcnow,
)

log = logging.getLogger(__name__)

STATE_LAST_SYNC = "last_successful_sync_at"
STATE_LAST_FULL_SYNC = "last_complete_sync_at"
STATE_LAST_ERROR = "last_sync_error"
STATE_KEYS = [STATE_LAST_SYNC, STATE_LAST_FULL_SYNC, STATE_LAST_ERROR]

# A reminder callback returns True when Discord accepted the message. Anything
# falsy means "not delivered" and the threshold stays unrecorded so we retry.
Notifier = Callable[[Assignment, int, float], Awaitable[bool]]


@dataclass(frozen=True)
class ReminderDecision:
    """What to do for one assignment on one evaluation tick."""

    send: int | None = None
    suppress: tuple[int, ...] = ()

    @property
    def is_noop(self) -> bool:
        return self.send is None and not self.suppress


def decide_reminder(
    minutes_remaining: float | None,
    thresholds: Sequence[int],
    already_recorded: Iterable[int],
) -> ReminderDecision:
    """Pure threshold-crossing decision. See the module docstring.

    ``already_recorded`` must be the thresholds recorded for the assignment's
    *current* due date, so moving a deadline naturally resets the schedule.
    """
    if minutes_remaining is None or minutes_remaining <= 0:
        return ReminderDecision()

    recorded = set(already_recorded)
    pending = sorted(t for t in set(thresholds) if minutes_remaining <= t and t not in recorded)
    if not pending:
        return ReminderDecision()

    # Smallest crossed threshold == most recently crossed == most relevant.
    return ReminderDecision(send=pending[0], suppress=tuple(pending[1:]))


@dataclass
class SyncResult:
    ok: bool = False
    complete: bool = False
    courses_total: int = 0
    courses_failed: list[str] = field(default_factory=list)
    assignments_tracked: int = 0
    assignments_retired: int = 0
    error: str | None = None

    def summary(self) -> str:
        if not self.ok:
            return f"Sync failed: {self.error}"
        parts = [
            f"{self.courses_total} course(s)",
            f"{self.assignments_tracked} assignment(s) tracked",
        ]
        if self.assignments_retired:
            parts.append(f"{self.assignments_retired} retired")
        if self.courses_failed:
            parts.append(f"{len(self.courses_failed)} course(s) unreadable")
        return ", ".join(parts)


class ReminderService:
    """Canvas synchronisation and reminder evaluation over a cached view.

    Both loops read :attr:`cache` rather than the database. PostgreSQL is
    written only when something actually changed — a new or altered
    assignment, a retirement, or a reminder being claimed and delivered. An
    idle tick and an unchanged Canvas sync issue no statements at all, which
    lets a scale-to-zero database stay asleep.
    """

    def __init__(
        self,
        config: Config,
        db: Database,
        canvas: CanvasClient,
        notifier: Notifier,
        cache: StateCache | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.canvas = canvas
        self.notifier = notifier
        self.cache = cache if cache is not None else StateCache()
        self._sync_lock = asyncio.Lock()
        self._bootstrap_suppress = False
        self.canvas_healthy: bool | None = None

    # ------------------------------------------------------------- startup

    def hydrate(self) -> None:
        """Load persisted state into the cache. Must succeed before evaluating.

        Also decides first-run suppression here rather than in ``__init__``, so
        that "is the database empty?" is answered by the same read that builds
        the cache instead of costing a separate query.
        """
        states = self.cache.hydrate(self.db, state_keys=STATE_KEYS)
        self.cache.last_sync_at = from_iso(states.get(STATE_LAST_SYNC))
        self.cache.last_complete_sync_at = from_iso(states.get(STATE_LAST_FULL_SYNC))
        self.cache.last_error = states.get(STATE_LAST_ERROR) or None
        self._bootstrap_suppress = (
            self.config.suppress_reminders_on_first_run
            and not self.cache.assignments
            and self.cache.delivered_count == 0
            and self.db.is_empty()
        )

    # -------------------------------------------------------------- sync

    def _in_window(self, due_at: datetime | None, now: datetime) -> bool:
        if due_at is None:
            return False
        earliest = now - timedelta(hours=self.config.monitor_past_grace_hours)
        latest = now + timedelta(days=self.config.monitor_lookahead_days)
        return earliest <= ensure_utc(due_at) <= latest

    async def sync_canvas(self) -> SyncResult:
        """Pull Canvas state into the database. Cache is only touched on success."""
        async with self._sync_lock:
            return await self._sync_canvas_locked()

    async def _sync_canvas_locked(self) -> SyncResult:
        result = SyncResult()
        now = utcnow()

        try:
            raw_courses = await self.canvas.get_active_courses(
                students_only=self.config.student_enrollments_only
            )
        except CanvasAuthError as exc:
            self.canvas_healthy = False
            result.error = str(exc)
            self.cache.last_error = result.error
            log.error("Canvas authentication failed: %s", exc)
            return result
        except CanvasError as exc:
            # Transient. Keep every cached assignment exactly as it is.
            self.canvas_healthy = False
            result.error = str(exc)
            self.cache.last_error = result.error
            log.warning("Canvas sync aborted, keeping cached state: %s", exc)
            return result

        courses = [c for c in (normalize_course(raw) for raw in raw_courses) if c is not None]
        result.courses_total = len(courses)
        if not courses:
            log.warning("Canvas reported no active courses for this token")

        synced_course_ids: list[int] = []
        kept_ids: list[int] = []
        changed: list[Assignment] = []

        for course in courses:
            try:
                raw_assignments = await self.canvas.get_course_assignments(course.id)
            except CanvasError as exc:
                # One bad course must not retire the other courses' assignments.
                result.courses_failed.append(course.name)
                log.warning("Could not read assignments for %s: %s", course.name, exc)
                continue

            synced_course_ids.append(course.id)
            for raw in raw_assignments:
                try:
                    assignment = normalize_assignment(
                        raw,
                        course_id=course.id,
                        course_name=course.name,
                        base_url=self.canvas.base_url,
                    )
                except Exception:  # defensive: never let one row kill the sync
                    log.exception("Skipping malformed assignment payload in %s", course.name)
                    continue
                if assignment is None or not self._in_window(assignment.due_at, now):
                    continue
                kept_ids.append(assignment.id)
                effective = self._effective(assignment)
                if self._has_changed(effective):
                    changed.append(effective)

        result.assignments_tracked = len(kept_ids)
        retiring = self._assignments_to_retire(synced_course_ids, set(kept_ids), courses)

        # ---- the only place this method touches PostgreSQL ----------------
        # Nothing below runs when Canvas returned exactly what we already have,
        # which is the normal case and the reason an idle bot issues no
        # statements at all.
        try:
            for assignment in changed:
                self.db.upsert_assignment(assignment, seen_at=now)
                self.cache.put(assignment)

            if retiring:
                retired = self.db.deactivate_missing(synced_course_ids, kept_ids)
                retired += self.db.deactivate_courses_not_in([c.id for c in courses])
                self.cache.retire(retiring)
                result.assignments_retired = retired
        except (psycopg.Error, DatabaseUnavailable) as exc:
            # The cache is only advanced for writes that landed, so the next
            # sync retries whatever did not.
            self.canvas_healthy = True
            result.error = f"database write failed: {exc}"
            self.cache.last_error = result.error
            log.error("Canvas sync could not persist changes: %s", exc)
            return result

        result.ok = True
        result.complete = not result.courses_failed
        self.canvas_healthy = True

        # Sync timestamps live in memory. Persisting them every five minutes
        # purely as a heartbeat is exactly what keeps a scale-to-zero database
        # awake, and nothing about reminder correctness depends on them — they
        # are reported by /status and /health only.
        self.cache.last_sync_at = now
        if result.complete:
            self.cache.last_complete_sync_at = now
            self.cache.last_error = None
        else:
            self.cache.last_error = f"partial sync: {', '.join(result.courses_failed)}"

        if changed or retiring:
            # We are already awake and writing, so record the timestamps too.
            self._persist_sync_state()

        if changed or retiring:
            log.info("Canvas sync complete — %s", result.summary())
        else:
            log.debug("Canvas sync complete — no changes, no database writes")
        return result

    # ------------------------------------------------------ change detection

    def _effective(self, incoming: Assignment) -> Assignment:
        """What this assignment will look like once persisted.

        Canvas may omit the submission object, in which case the stored state
        wins. Applying that rule *here* is what lets the comparison below be
        exact: without it, every sync of an assignment with no submission
        payload would look like a change and trigger a pointless write.
        """
        cached = self.cache.assignments.get(incoming.id)
        existing = cached.submission if cached is not None else None
        return incoming.with_submission(resolve_submission(incoming, existing))

    def _has_changed(self, effective: Assignment) -> bool:
        cached = self.cache.assignments.get(effective.id)
        if cached is None:
            return True
        return cached.persisted_fields() != effective.persisted_fields()

    def _assignments_to_retire(
        self, synced_course_ids: list[int], kept_ids: set[int], courses: list
    ) -> set[int]:
        """Which cached assignments the UPDATEs would actually deactivate.

        Computing this in memory means the two retirement UPDATEs are skipped
        entirely in the steady state, where they would match zero rows.
        """
        synced = set(synced_course_ids)
        live_courses = {c.id for c in courses}
        retiring: set[int] = set()
        for assignment in self.cache.assignments.values():
            if assignment.course_id in synced and assignment.id not in kept_ids:
                retiring.add(assignment.id)
            elif assignment.course_id not in live_courses:
                retiring.add(assignment.id)
        return retiring

    def _persist_sync_state(self) -> None:
        values = {
            STATE_LAST_SYNC: to_iso(self.cache.last_sync_at) or "",
            STATE_LAST_FULL_SYNC: to_iso(self.cache.last_complete_sync_at) or "",
            STATE_LAST_ERROR: self.cache.last_error or "",
        }
        try:
            self.db.set_states(values)
        except (psycopg.Error, DatabaseUnavailable):
            # Display-only. Losing it costs nothing but a stale /status after
            # a restart, and it must never fail a sync that already persisted.
            log.warning("Could not persist sync timestamps", exc_info=True)

    # -------------------------------------------------------- evaluation

    async def evaluate_reminders(self, *, now: datetime | None = None) -> int:
        """Deliver any newly-crossed reminders. Returns how many were sent.

        **This performs no database work when nothing is due.** The decision is
        made entirely from :attr:`cache`, so the 45-second tick is pure
        arithmetic and a sleeping database stays asleep.

        When a threshold *is* crossed the flow is unchanged: claim-then-send.
        The ``(assignment, due version, threshold)`` row is inserted before the
        Discord call and removed again if the send fails, so the reminder is
        exactly-once even while a deploy briefly runs two instances against the
        same database — the second one loses the insert and stays quiet. The
        cache is only a pre-filter; :meth:`Database.claim_reminder` remains the
        authority, so a stale cache costs at most a wasted attempt.
        """
        if not self.cache.hydrated:
            # Evaluating against an empty cache would treat every threshold as
            # un-sent and re-notify everything.
            log.warning("Skipping reminder evaluation: cache is not hydrated yet")
            return 0

        moment = ensure_utc(now or utcnow())
        deliver = not self._bootstrap_suppress
        sent = 0

        for assignment in self.cache.active():
            if assignment.is_complete:
                continue
            due_key = assignment.due_key
            decision = decide_reminder(
                assignment.minutes_remaining(moment),
                self.config.thresholds_minutes,
                self.cache.thresholds_for(assignment),
            )
            if decision.is_noop:
                continue

            try:
                sent += await self._act_on(assignment, due_key, decision, moment, deliver)
            except (psycopg.Error, DatabaseUnavailable):
                # Isolated per assignment: a database wake-up that times out on
                # one reminder must not abandon the rest of the tick. Nothing
                # was recorded, so the next tick retries this one.
                log.exception(
                    "Database unavailable while handling '%s'; will retry", assignment.name
                )
                continue

        if self._bootstrap_suppress:
            log.info("First run on an empty database: baseline recorded, no reminders sent")
            self._bootstrap_suppress = False

        return sent

    async def _act_on(
        self,
        assignment: Assignment,
        due_key: str,
        decision: ReminderDecision,
        moment: datetime,
        deliver: bool,
    ) -> int:
        """Carry out one assignment's decision. Returns reminders delivered."""
        if decision.suppress:
            log.info(
                "Skipping stale thresholds %s for '%s' (catch-up)",
                list(decision.suppress), assignment.name,
            )
            self.db.record_reminders(
                assignment.id, due_key, list(decision.suppress), delivered=False
            )
            self.cache.note_recorded(assignment.id, due_key, list(decision.suppress))

        if decision.send is None:
            return 0

        remaining = assignment.minutes_remaining(moment) or 0.0
        if not deliver:
            self.db.record_reminder(assignment.id, due_key, decision.send, delivered=False)
            self.cache.note_recorded(assignment.id, due_key, [decision.send])
            return 0

        if not self.db.claim_reminder(assignment.id, due_key, decision.send, sent_at=moment):
            # Another instance (or an overlapping tick) owns this one. Record
            # it locally so we stop re-attempting the claim every tick.
            self.cache.note_recorded(assignment.id, due_key, [decision.send])
            log.debug(
                "Reminder for '%s' (%s min) already claimed elsewhere",
                assignment.name, decision.send,
            )
            return 0

        self.cache.note_recorded(assignment.id, due_key, [decision.send])
        try:
            delivered = await self.notifier(assignment, decision.send, remaining)
        except Exception:
            log.exception("Failed to deliver reminder for '%s'", assignment.name)
            self._release(assignment.id, due_key, decision.send)
            return 0

        if delivered:
            self.db.mark_reminder_delivered(assignment.id, due_key, decision.send, sent_at=moment)
            self.cache.note_delivered()
            return 1

        self._release(assignment.id, due_key, decision.send)
        log.warning(
            "Reminder for '%s' (%s min) was not delivered; will retry",
            assignment.name, decision.send,
        )
        return 0

    def _release(self, assignment_id: int, due_key: str, threshold: int) -> None:
        """Give back a claim whose delivery failed, in both places.

        If the database release fails the cache entry is kept, which leaves the
        claim standing rather than risking a second delivery. The claim row is
        undelivered, so a later restart rehydrates it as pending and retries.
        """
        try:
            self.db.release_reminder(assignment_id, due_key, threshold)
        except (psycopg.Error, DatabaseUnavailable):
            log.warning(
                "Could not release the claim for assignment %s (%s min); "
                "it stays claimed until the next restart",
                assignment_id, threshold, exc_info=True,
            )
            return
        self.cache.note_released(assignment_id, due_key, threshold)

    # ------------------------------------------------------------ queries

    def incomplete_assignments(
        self,
        *,
        now: datetime | None = None,
        until: datetime | None = None,
        include_past_due: bool = False,
    ) -> list[Assignment]:
        """Cache-backed equivalent of :meth:`Database.incomplete_assignments`.

        The slash commands read this so that browsing deadlines does not wake a
        sleeping database. The filtering rule is identical.
        """
        return filter_incomplete(
            self.cache.active(), now=now, until=until, include_past_due=include_past_due
        )

    # ------------------------------------------------------------ status

    def last_sync_at(self) -> datetime | None:
        return self.cache.last_sync_at

    def last_complete_sync_at(self) -> datetime | None:
        return self.cache.last_complete_sync_at

    def last_error(self) -> str | None:
        return self.cache.last_error or None
