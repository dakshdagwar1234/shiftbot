"""Why this exists:
Tests shift cutoff window enforcement and business rule functions (free_seats, nearest_slot).
Verifies deterministic policy calculations without touching any database or LLM.
"""
from datetime import datetime, time, timedelta

from app.rules import check_cutoff, free_seats, nearest_slot


def test_cutoff_before_cutoff_window():
    """Request made well before the cutoff window satisfies the rule."""
    trip_time = datetime(2026, 11, 20, 18, 0)
    # 180 minutes before trip (cutoff is 120)
    now = trip_time - timedelta(minutes=180)
    result = check_cutoff(now=now, trip_slot_datetime=trip_time, cutoff_minutes=120)
    assert result.ok is True
    assert "satisfying the 120-minute cutoff" in result.reason


def test_cutoff_exactly_at_cutoff_window():
    """Request made exactly at the cutoff window boundary satisfies the rule."""
    trip_time = datetime(2026, 11, 20, 18, 0)
    # Exactly 120 minutes before trip
    now = trip_time - timedelta(minutes=120)
    result = check_cutoff(now=now, trip_slot_datetime=trip_time, cutoff_minutes=120)
    assert result.ok is True
    assert "satisfying the 120-minute cutoff" in result.reason


def test_cutoff_one_minute_inside_cutoff_window():
    """Request made even one minute inside the cutoff window requires approval."""
    trip_time = datetime(2026, 11, 20, 18, 0)
    # 119 minutes before trip (cutoff is 120)
    now = trip_time - timedelta(minutes=119)
    result = check_cutoff(now=now, trip_slot_datetime=trip_time, cutoff_minutes=120)
    assert result.ok is False
    assert "Manager approval required" in result.reason
    assert "inside the 120-minute cutoff" in result.reason


def test_cutoff_different_policies_for_alpha_and_beta():
    """At 90 minutes before trip, Beta (60m cutoff) passes but Alpha (120m cutoff) fails."""
    trip_time = datetime(2026, 11, 20, 18, 0)
    now = trip_time - timedelta(minutes=90)

    # Comp Alpha (120 minutes cutoff) -> 90 < 120 -> Rejected
    alpha_result = check_cutoff(now=now, trip_slot_datetime=trip_time, cutoff_minutes=120)
    assert alpha_result.ok is False

    # Comp Beta (60 minutes cutoff) -> 90 >= 60 -> OK
    beta_result = check_cutoff(now=now, trip_slot_datetime=trip_time, cutoff_minutes=60)
    assert beta_result.ok is True


def test_free_seats_calculation():
    """Verifies free seats arithmetic and non-negative lower bound."""
    assert free_seats(capacity=4, seats_taken=2) == 2
    assert free_seats(capacity=4, seats_taken=4) == 0
    assert free_seats(capacity=6, seats_taken=5) == 1
    assert free_seats(capacity=4, seats_taken=5) == 0  # safeguard against negative values


def test_nearest_slot_skips_full_cabs():
    """Nearest slot skips cabs where free_seats == 0."""
    cabs = [
        {"cab_id": "c1", "slot_time": time(18, 0), "capacity": 4, "seats_taken": 4},  # full
        {"cab_id": "c2", "slot_time": time(19, 0), "capacity": 4, "seats_taken": 1},  # available
    ]
    chosen = nearest_slot(requested_time=time(18, 0), cabs=cabs)
    assert chosen is not None
    assert chosen["cab_id"] == "c2"


def test_nearest_slot_tie_breaks_to_earlier_slot():
    """When two available slots are equidistant from requested time, choose the earlier slot."""
    cabs = [
        {"cab_id": "cab_early", "slot_time": time(19, 0), "capacity": 4, "seats_taken": 2},  # 60m earlier
        {"cab_id": "cab_late", "slot_time": time(21, 0), "capacity": 4, "seats_taken": 1},   # 60m later
    ]
    chosen = nearest_slot(requested_time=time(20, 0), cabs=cabs)
    assert chosen is not None
    assert chosen["cab_id"] == "cab_early"


def test_nearest_slot_returns_none_when_no_cabs_available():
    """Returns None if all candidate cabs are completely full."""
    cabs = [
        {"cab_id": "c1", "slot_time": time(18, 0), "capacity": 4, "seats_taken": 4},
        {"cab_id": "c2", "slot_time": time(19, 0), "capacity": 4, "seats_taken": 4},
    ]
    chosen = nearest_slot(requested_time=time(18, 30), cabs=cabs)
    assert chosen is None
