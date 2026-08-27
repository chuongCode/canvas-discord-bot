"""Threshold crossing, duplicate suppression, catch-up and due-date changes."""

from __future__ import annotations

from app.config import DEFAULT_THRESHOLDS_MINUTES as T
from app.reminder_service import decide_reminder


def test_no_reminder_before_the_first_threshold():
    assert decide_reminder(minutes_remaining=800, thresholds=T, already_recorded=[]).is_noop


def test_crossing_a_threshold_sends_exactly_that_threshold():
    d = decide_reminder(720, T, [])
    assert d.send == 720 and d.suppress == ()


def test_threshold_crossing_is_not_exact_equality():
    # Poll lands between thresholds: 61 -> 59 minutes must still fire the 1h.
    d = decide_reminder(59.0, T, already_recorded=[720, 300, 120])
    assert d.send == 60


def test_the_11pm_edge_case_from_the_brief():
    """Due 11:59 PM, checks at 10:57 PM and 11:02 PM.

    62 minutes left: 1h not yet crossed. 57 minutes left: crossed -> send once,
    and every later tick is silent until the next threshold.
    """
    recorded = {720, 300, 120}
    assert decide_reminder(62.0, T, recorded).is_noop

    d = decide_reminder(57.0, T, recorded)
    assert d.send == 60
    recorded.add(60)

    assert decide_reminder(55.0, T, recorded).is_noop
    assert decide_reminder(45.0, T, recorded).is_noop
    assert decide_reminder(31.0, T, recorded).is_noop


def test_duplicate_suppression_for_the_same_threshold():
    assert decide_reminder(30.0, T, already_recorded=[720, 300, 120, 60, 30]).is_noop


def test_downtime_catchup_sends_only_the_most_relevant():
    """Back online with 20 minutes left: send the 30m, skip 12h/5h/2h/1h."""
    d = decide_reminder(20.0, T, already_recorded=[])
    assert d.send == 30
    assert set(d.suppress) == {720, 300, 120, 60}


def test_catchup_never_resends_thresholds_already_recorded():
    d = decide_reminder(20.0, T, already_recorded=[720, 300])
    assert d.send == 30
    assert set(d.suppress) == {120, 60}


def test_past_due_assignments_are_silent():
    assert decide_reminder(0.0, T, []).is_noop
    assert decide_reminder(-90.0, T, []).is_noop


def test_missing_due_date_is_silent():
    assert decide_reminder(None, T, []).is_noop


def test_final_minute_still_fires():
    d = decide_reminder(0.4, T, already_recorded=set(T) - {1})
    assert d.send == 1


def test_thresholds_are_configurable():
    custom = (180, 45)
    d = decide_reminder(40.0, custom, [])
    assert d.send == 45 and d.suppress == (180,)


def test_reminder_ordering_is_by_urgency_not_list_order():
    shuffled = (60, 720, 30, 300)
    d = decide_reminder(25.0, shuffled, [])
    assert d.send == 30


# --------------------------------------------------------------------------
# Timeline simulations: the strongest check against duplicates and misses.
# --------------------------------------------------------------------------


def simulate(remaining_sequence, thresholds=T):
    """Replay a sequence of 'minutes remaining' observations.

    Returns the ordered list of thresholds actually delivered.
    """
    recorded: set[int] = set()
    delivered: list[int] = []
    for remaining in remaining_sequence:
        decision = decide_reminder(remaining, thresholds, recorded)
        recorded.update(decision.suppress)
        if decision.send is not None:
            delivered.append(decision.send)
            recorded.add(decision.send)
    return delivered


def test_continuous_operation_delivers_every_threshold_exactly_once():
    """45-second polling from 13 hours out down to the deadline."""
    remaining = [13 * 60 - (i * 0.75) for i in range(int(13 * 60 / 0.75))]
    delivered = simulate(remaining)
    assert delivered == sorted(T, reverse=True)
    assert len(delivered) == len(set(delivered)), "no threshold may fire twice"


def test_slow_polling_still_hits_every_threshold():
    """Even a sluggish 5-minute evaluation loop misses nothing."""
    remaining = [13 * 60 - (i * 5) for i in range(int(13 * 60 / 5))]
    delivered = simulate(remaining)
    # The run ends 5 minutes out, so the 1-minute threshold is simply not
    # reached; every threshold that *was* reached fired, once, in order.
    assert delivered == [720, 300, 120, 60, 30, 15, 10, 5]


def test_downtime_in_the_middle_of_the_schedule():
    """12h reminder goes out, the process dies, and returns with 20m left."""
    before = [722, 721, 720, 719, 718]  # the 12-hour reminder fires here
    after = [20, 19, 18, 12, 8, 4, 2, 0.5]  # ... then a long blackout
    delivered = simulate(before + after)
    assert delivered == [720, 30, 15, 10, 5, 1], "no stale 5h/2h/1h backlog"
    assert len(delivered) == len(set(delivered))


def test_restart_between_every_single_tick_changes_nothing():
    """Reminder state lives in the DB, so a crash loop must not duplicate."""
    remaining = [13 * 60 - (i * 0.75) for i in range(int(13 * 60 / 0.75))]
    recorded: set[int] = set()
    delivered: list[int] = []
    for value in remaining:
        # Each iteration is a "fresh process" reading the same persisted set.
        decision = decide_reminder(value, T, set(recorded))
        recorded.update(decision.suppress)
        if decision.send is not None:
            delivered.append(decision.send)
            recorded.add(decision.send)
    assert delivered == sorted(T, reverse=True)


def test_due_date_pushed_back_replays_the_schedule_once_more():
    """12h fires for Monday; deadline moves +2 days; 12h fires again, once."""
    monday = simulate([13 * 60, 12 * 60, 11 * 60])
    assert monday == [720]

    # New due date == new identity == empty recorded set.
    wednesday = simulate([60 * 60, 13 * 60, 12 * 60, 11 * 60])
    assert wednesday == [720]


def test_due_date_pulled_forward_does_not_spam():
    """Deadline yanked from 3 days out to 40 minutes: one reminder, not five."""
    delivered = simulate([40])
    assert delivered == [60]
