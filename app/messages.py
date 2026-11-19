"""Why this exists:
Contains static response templates and formatting helpers for the shift-change agent.
Ensures user-facing responses are deterministic code templates and never unvetted LLM text.
"""
from datetime import date, time, timedelta

from app.clock import now as clock_now


def format_time(t: time | str | None) -> str:
    """Formats time object or string to 12-hour AM/PM format (e.g. '9:00 PM')."""
    if not t:
        return ""
    if isinstance(t, str):
        s = t.strip()
        if "AM" in s.upper() or "PM" in s.upper():
            return s
        try:
            parts = [int(p) for p in s.split(":")[:2]]
            t_obj = time(parts[0], parts[1])
        except Exception:
            return s
    elif isinstance(t, time):
        t_obj = t
    else:
        return str(t)

    formatted = t_obj.strftime("%I:%M %p")
    if formatted.startswith("0"):
        formatted = formatted[1:]
    return formatted


def format_date(d: date | str | None) -> str:
    """Formats date to 'today', 'tomorrow', or '21 Nov' format."""
    if not d:
        return "today"
    if isinstance(d, str):
        s = d.strip().lower()
        if s in ("today", "tomorrow"):
            return s
        try:
            d_obj = date.fromisoformat(d.strip())
        except Exception:
            return d
    elif isinstance(d, date):
        d_obj = d
    else:
        return str(d)

    today = clock_now().date()
    if d_obj == today:
        return "today"
    if d_obj == today + timedelta(days=1):
        return "tomorrow"
    return f"{d_obj.day} {d_obj.strftime('%b')}"


def change_applied(new_time: str | time, date_str: str | date = "today") -> str:
    """Template for successful shift change booking."""
    t_fmt = format_time(new_time)
    d_fmt = format_date(date_str)
    return (
        f"Your shift change has been confirmed. Your cab is booked for {t_fmt} on {d_fmt}. "
        "Your previous seat has been released and you are all set."
    )


def rule_rejected(reason: str, nearest_time: str | time | None = None) -> str:
    """Template for request rejected by business rules with alternative option."""
    msg = f"Your request could not be applied automatically: {reason}"
    if nearest_time:
        t_fmt = format_time(nearest_time)
        msg += f" The nearest available cab with open seats is at {t_fmt}. Would you like to take that slot instead?"
    else:
        msg += " No alternative cabs with open seats are available right now."
    return msg


def cab_full(requested_time: str | time, next_available_time: str | time | None = None) -> str:
    """Template when the requested cab has zero available seats."""
    req_fmt = format_time(requested_time)
    if next_available_time:
        next_fmt = format_time(next_available_time)
        return (
            f"The cab at {req_fmt} is completely full. "
            f"The nearest available cab is at {next_fmt}. Would you like to switch to that slot?"
        )
    return (
        f"The cab at {req_fmt} is completely full, and no alternative cabs have open seats. "
        "Would you like to submit a request for manager approval?"
    )


def needs_manager_approval(cutoff_minutes: int) -> str:
    """Template when a request falls inside the company cutoff window."""
    return (
        f"This change is inside your company's {cutoff_minutes}-minute cutoff window and requires manager approval. "
        "Would you like me to submit an approval request to your manager?"
    )


def request_pending() -> str:
    """Template when a request has been submitted for manager approval."""
    return (
        "Your shift change request has been submitted for manager approval. "
        "Your current cab booking remains active until your manager makes a decision."
    )


def request_approved(new_time: str | time, date_str: str | date = "today") -> str:
    """Template when a manager approves the request."""
    t_fmt = format_time(new_time)
    d_fmt = format_date(date_str)
    return (
        f"Your manager has approved your shift change request. "
        f"Your new cab is confirmed for {t_fmt} on {d_fmt}."
    )


def request_rejected(reason: str | None = None) -> str:
    """Template when a manager rejects the request."""
    if reason:
        return f"Your manager did not approve the shift change: {reason}. Your original cab remains active."
    return "Your manager did not approve the shift change request. Your original cab booking remains active."


def tool_timeout() -> str:
    """Template when a tool execution exceeds the time limit."""
    return "I couldn't check cab availability right now. Nothing was changed. Try again in a minute."


def generic_failure() -> str:
    """Template when an unexpected error occurs on the server."""
    return "Something went wrong on our side. Nothing was changed. Please try again or call the transport desk."


def ask_for_time_clarification() -> str:
    """Template when the user message lacks required shift details."""
    return "Could you please specify your desired new shift time and date (for example: 'today drop at 9:00 PM')?"


def llm_unavailable() -> str:
    """Template when the LLM service is unavailable or timed out."""
    return "I couldn't process that right now. Nothing was changed. Please try again in a minute or call the transport desk."


def could_not_understand() -> str:
    """Template when the LLM cannot parse or understand the user message."""
    return "I didn't understand that. Could you tell me the new time, like 'move my drop to 9 PM'?"
