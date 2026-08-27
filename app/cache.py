"""The in-memory mirror of persisted state that the loops read every tick.

Why this exists: the reminder loop runs every 45 seconds and, before this
cache, asked PostgreSQL two questions on every one of those ticks — even when
nothing was due and nothing had changed. Against a database that suspends when
idle (Neon's scale to zero) a query every 45 seconds means the compute never
sleeps and bills continuously.

Nothing here is authoritative. PostgreSQL remains the source of truth, and in
particular :meth:`Database.claim_reminder` is still the only thing that decides
whether a reminder may be sent. The cache exists to answer "is there anything
to do?" without a round trip; when the answer is yes, the database is consulted
exactly as before. A stale cache can therefore cost a wasted claim attempt that
returns ``False`` — it can never produce a duplicate reminder or lose one.

The cache is rebuilt from PostgreSQL at startup (:meth:`hydrate`) and updated
in place afterwards, so a restart or redeploy resumes with the same view the
database holds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from .database import Database
from .models import Assignment

log = logging.getLogger(__name__)

# Reminder history is keyed by assignment *and* due-date version, so moving a
# deadline starts a fresh schedule.
ReminderKey = tuple[int, str]


@dataclass
class StateCache:
    """Active assignments and their recorded reminder thresholds."""

    assignments: dict[int, Assignment] = field(default_factory=dict)
    recorded: dict[ReminderKey, set[int]] = field(default_factory=dict)
    delivered_count: int = 0

    hydrated: bool = False
    last_sync_at: datetime | None = None
    last_complete_sync_at: datetime | None = None
    last_error: str | None = None

    # ------------------------------------------------------------- loading

    def hydrate(self, db: Database, *, state_keys: list[str]) -> dict[str, str]:
        """Rebuild the whole cache from PostgreSQL. Called once, at startup.

        Raises whatever the database raises: a bot that cannot read its own
        reminder history must not start evaluating, because every threshold
        would look un-sent and it would re-notify the world.
        """
        assignments = db.all_active_assignments()
        self.assignments = {a.id: a for a in assignments}
        self.recorded = db.sent_thresholds_map()
        self.delivered_count = db.delivered_reminder_count()
        states = db.get_states(state_keys)
        self.hydrated = True
        log.info(
            "Cache hydrated: %d active assignment(s), %d reminder schedule(s), %d delivered",
            len(self.assignments), len(self.recorded), self.delivered_count,
        )
        return states

    # ------------------------------------------------------------- reading

    def active(self) -> list[Assignment]:
        """Active assignments with a deadline, soonest first."""
        dated = [a for a in self.assignments.values() if a.due_at is not None]
        dated.sort(key=lambda a: a.due_at)  # type: ignore[arg-type,return-value]
        return dated

    def monitored_count(self) -> int:
        return sum(1 for a in self.assignments.values() if a.due_at is not None)

    def thresholds_for(self, assignment: Assignment) -> set[int]:
        return self.recorded.get((assignment.id, assignment.due_key), set())

    # ------------------------------------------------------------- writing

    def put(self, assignment: Assignment) -> None:
        """Record an assignment as it now exists in the database."""
        previous = self.assignments.get(assignment.id)
        self.assignments[assignment.id] = assignment
        if previous is not None and previous.due_key != assignment.due_key:
            # The deadline moved, so the old schedule can never match again.
            # Dropping it keeps the cache from growing with dead keys; the rows
            # themselves stay in PostgreSQL until they are pruned.
            self.recorded.pop((assignment.id, previous.due_key), None)

    def retire(self, assignment_ids: set[int]) -> None:
        for assignment_id in assignment_ids:
            assignment = self.assignments.pop(assignment_id, None)
            if assignment is not None:
                self.recorded.pop((assignment_id, assignment.due_key), None)

    def note_recorded(self, assignment_id: int, due_key: str, thresholds: list[int]) -> None:
        self.recorded.setdefault((assignment_id, due_key), set()).update(thresholds)

    def note_released(self, assignment_id: int, due_key: str, threshold: int) -> None:
        self.recorded.get((assignment_id, due_key), set()).discard(threshold)

    def note_delivered(self) -> None:
        self.delivered_count += 1
