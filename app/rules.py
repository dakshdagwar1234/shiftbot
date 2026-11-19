"""Why this exists:
Implements deterministic business rules per company (cutoff policy, seat math, nearest slot).
Contains pure logic completely independent of the database and LLM.
"""
from datetime import datetime, time
from typing import Any
from pydantic import BaseModel, ConfigDict


class RuleResult(BaseModel):
    """Immutable outcome of a business rule evaluation."""
    model_config = ConfigDict(frozen=True)

    ok: bool
    reason: str


def check_cutoff(now: datetime, trip_slot_datetime: datetime, cutoff_minutes: int) -> RuleResult:
    """Evaluates whether a shift change request satisfies the company cutoff window.

    Rule: Request must be made at least cutoff_minutes before the scheduled trip.
    """
    diff_minutes = (trip_slot_datetime - now).total_seconds() / 60.0

    if diff_minutes >= cutoff_minutes:
        return RuleResult(
            ok=True,
            reason=f"Request is {diff_minutes:.0f} minutes before trip, satisfying the {cutoff_minutes}-minute cutoff.",
        )
    return RuleResult(
        ok=False,
        reason=(
            f"Request is {diff_minutes:.0f} minutes before trip, inside the {cutoff_minutes}-minute cutoff. "
            "Manager approval required."
        ),
    )


def free_seats(capacity: int, seats_taken: int) -> int:
    """Calculates available seats in a cab, ensuring the result is never negative."""
    return max(0, capacity - seats_taken)


def nearest_slot(requested_time: time, cabs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Finds the available cab with free seats closest to the requested time.

    In case of a tie in absolute time difference, selects the earlier slot.
    Returns None if no cabs have available seats.
    """
    available_cabs = [
        cab for cab in cabs
        if free_seats(cab.get("capacity", 0), cab.get("seats_taken", 0)) > 0
    ]
    if not available_cabs:
        return None

    def _to_minutes(t: time | str) -> int:
        if isinstance(t, str):
            # Parse HH:MM or HH:MM:SS
            parts = [int(p) for p in t.split(":")[:2]]
            return parts[0] * 60 + parts[1]
        return t.hour * 60 + t.minute

    req_min = _to_minutes(requested_time)

    # Sort key: (absolute distance in minutes, slot time in minutes)
    # The second element breaks ties by preferring the earlier slot.
    return min(
        available_cabs,
        key=lambda c: (abs(_to_minutes(c["slot_time"]) - req_min), _to_minutes(c["slot_time"])),
    )
