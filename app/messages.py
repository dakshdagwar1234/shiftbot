"""Why this exists:
Contains static response templates for the shift-change agent.
Ensures user-facing responses are deterministic code templates and never unvetted LLM text.
"""


def change_applied(new_time: str, date_str: str = "today") -> str:
    """Template for successful shift change booking."""
    return (
        f"Your shift change has been confirmed. Your cab is booked for {new_time} on {date_str}. "
        "Your previous seat has been released and you are all set."
    )


def rule_rejected(reason: str, nearest_time: str | None = None) -> str:
    """Template for request rejected by business rules with alternative option."""
    msg = f"Your request could not be applied automatically: {reason}"
    if nearest_time:
        msg += f" The nearest available cab with open seats is at {nearest_time}. Would you like to take that slot instead?"
    else:
        msg += " No alternative cabs with open seats are available right now."
    return msg


def cab_full(requested_time: str, next_available_time: str | None = None) -> str:
    """Template when the requested cab has zero available seats."""
    if next_available_time:
        return (
            f"The cab at {requested_time} is completely full. "
            f"The nearest available cab is at {next_available_time}. Would you like to switch to that slot?"
        )
    return (
        f"The cab at {requested_time} is completely full, and no alternative cabs have open seats. "
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


def request_approved(new_time: str, date_str: str = "today") -> str:
    """Template when a manager approves the request."""
    return (
        f"Your manager has approved your shift change request. "
        f"Your new cab is confirmed for {new_time} on {date_str}."
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
    return "Could you please specify your desired new shift time and date (for example: 'today drop at 21:00')?"
