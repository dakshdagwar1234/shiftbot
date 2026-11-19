"""Why this exists:
Provides a centralized, controllable clock pinned to Asia/Kolkata timezone.
Allows overriding system time via FIXED_NOW environment variable for deterministic tests.
"""
from datetime import datetime
import os
from zoneinfo import ZoneInfo

KOLKATA_TZ = ZoneInfo("Asia/Kolkata")


def now() -> datetime:
    """Returns current datetime in Asia/Kolkata, or FIXED_NOW if configured."""
    fixed = os.getenv("FIXED_NOW")
    if fixed:
        dt = datetime.fromisoformat(fixed)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=KOLKATA_TZ)
        return dt.astimezone(KOLKATA_TZ)
    return datetime.now(KOLKATA_TZ)
