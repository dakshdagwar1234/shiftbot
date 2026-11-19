"""Why this exists:
Implements execution tools for roster lookup, cab evaluation, atomic booking, and pending requests.
Requires Identity as the first parameter for all operations and strictly enforces company_id isolation.
"""
import concurrent.futures
from datetime import date, datetime, time
import logging
from typing import Any, Callable

import psycopg

from app.auth import Identity
from app.clock import KOLKATA_TZ, now as clock_now
from app.db import get_cabs as db_get_cabs, get_company_rules, get_connection
from app.rules import RuleResult, check_cutoff, free_seats, nearest_slot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TYPED DOMAIN EXCEPTIONS
# ---------------------------------------------------------------------------

class ToolError(Exception):
    """Base class for all business tool errors."""
    pass


class NotFound(ToolError):
    """Raised when a roster, cab, or required entity cannot be found."""
    pass


class RuleViolation(ToolError):
    """Raised when a business rule (such as cutoff policy) is violated."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class CabFull(ToolError):
    """Raised when attempting to allocate a seat in a cab with zero capacity remaining."""
    pass


class ToolTimeout(ToolError):
    """Raised when a tool execution exceeds the configured timeout threshold."""
    pass


class ToolFailure(ToolError):
    """Raised when an unhandled server or database error occurs."""
    pass


# ---------------------------------------------------------------------------
# CORE TOOLS
# ---------------------------------------------------------------------------

def get_roster(identity: Identity, service_date: date, db_url: str | None = None) -> dict[str, Any] | None:
    """Retrieves scheduled trip for the authenticated employee on a specific date."""
    query = """
    SELECT r.roster_id, r.company_id, r.employee_id, r.cab_id, r.service_date, r.trip_type,
           c.slot_time, c.route, c.capacity, c.seats_taken
    FROM roster r
    LEFT JOIN cabs c ON r.cab_id = c.cab_id AND r.company_id = c.company_id
    WHERE r.company_id = %s AND r.employee_id = %s AND r.service_date = %s
    """
    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (identity.company_id, identity.employee_id, service_date))
            return cur.fetchone()


def get_latest_request(identity: Identity, db_url: str | None = None) -> dict[str, Any] | None:
    """Retrieves the most recent shift change request for the authenticated employee."""
    query = """
    SELECT request_id, company_id, employee_id, roster_id, old_cab_id, requested_slot_time,
           status, reason, created_at
    FROM shift_requests
    WHERE company_id = %s AND employee_id = %s
    ORDER BY created_at DESC, request_id DESC
    LIMIT 1
    """
    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (identity.company_id, identity.employee_id))
            return cur.fetchone()


def check_cab_options(
    identity: Identity,
    roster_id: int | str,
    requested_time: time | str,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Evaluates requested cab availability and cutoff rules for a rostered trip.

    Returns the exact cab if available, otherwise the nearest free alternative and cutoff verdict.
    """
    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            # 1. Fetch current roster entry
            cur.execute(
                """SELECT r.roster_id, r.company_id, r.employee_id, r.cab_id, r.service_date, r.trip_type,
                          c.slot_time AS old_slot_time
                   FROM roster r
                   LEFT JOIN cabs c ON r.cab_id = c.cab_id AND r.company_id = c.company_id
                   WHERE r.roster_id = %s AND r.company_id = %s AND r.employee_id = %s""",
                (roster_id, identity.company_id, identity.employee_id),
            )
            roster = cur.fetchone()
            if not roster:
                raise NotFound("Roster entry not found for this employee.")

            # 2. Check company cutoff against original trip time
            comp_rules = get_company_rules(identity.company_id, db_url=db_url)
            if not comp_rules:
                raise NotFound("Company configuration not found.")

            trip_slot_dt = datetime.combine(roster["service_date"], roster["old_slot_time"]).replace(tzinfo=KOLKATA_TZ)
            cutoff_verdict = check_cutoff(
                now=clock_now(),
                trip_slot_datetime=trip_slot_dt,
                cutoff_minutes=comp_rules["cutoff_minutes"],
            )

            # 3. Fetch candidate cabs (same company, same date, same trip_type)
            cabs = db_get_cabs(
                company_id=identity.company_id,
                service_date=roster["service_date"],
                trip_type=roster["trip_type"],
                db_url=db_url,
            )

    # 4. Resolve exact slot and nearest available alternative
    def _to_time(t: time | str) -> time:
        if isinstance(t, str):
            parts = [int(p) for p in t.split(":")[:2]]
            return time(parts[0], parts[1])
        return t

    req_t = _to_time(requested_time)
    exact_cab = next(
        (c for c in cabs if c["slot_time"] == req_t and free_seats(c["capacity"], c["seats_taken"]) > 0),
        None,
    )
    nearest_cab = nearest_slot(req_t, cabs)

    return {
        "cutoff": cutoff_verdict,
        "requested_cab": exact_cab,
        "nearest_cab": nearest_cab,
        "original_cab_id": roster["cab_id"],
        "original_slot_time": roster["old_slot_time"],
        "service_date": roster["service_date"],
    }


def apply_shift_change(
    identity: Identity,
    roster_id: int | str,
    new_cab_id: str,
    idempotency_key: str,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Atomically allocates a seat on a new cab, releases the old seat, and updates roster."""
    with get_connection(db_url) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                # 1. Idempotency check: if key already exists, return existing result
                cur.execute(
                    """SELECT request_id, company_id, employee_id, roster_id, old_cab_id, requested_slot_time, status
                       FROM shift_requests
                       WHERE company_id = %s AND idempotency_key = %s""",
                    (identity.company_id, idempotency_key),
                )
                existing = cur.fetchone()
                if existing:
                    return {
                        "status": existing["status"],
                        "request_id": existing["request_id"],
                        "roster_id": existing["roster_id"],
                        "cab_id": new_cab_id,
                        "idempotent": True,
                    }

                # 2. Verify roster belongs to this company AND this employee
                cur.execute(
                    """SELECT r.roster_id, r.company_id, r.employee_id, r.cab_id, r.service_date,
                              c.slot_time AS old_slot_time
                       FROM roster r
                       LEFT JOIN cabs c ON r.cab_id = c.cab_id AND r.company_id = c.company_id
                       WHERE r.roster_id = %s AND r.company_id = %s AND r.employee_id = %s""",
                    (roster_id, identity.company_id, identity.employee_id),
                )
                roster = cur.fetchone()
                if not roster:
                    raise NotFound("Roster record not found for this employee.")

                # 3. Re-verify cutoff window
                cur.execute(
                    "SELECT cutoff_minutes FROM companies WHERE company_id = %s",
                    (identity.company_id,),
                )
                comp = cur.fetchone()
                cutoff_minutes = comp["cutoff_minutes"] if comp else 120

                trip_slot_dt = datetime.combine(roster["service_date"], roster["old_slot_time"]).replace(tzinfo=KOLKATA_TZ)
                cutoff_res = check_cutoff(now=clock_now(), trip_slot_datetime=trip_slot_dt, cutoff_minutes=cutoff_minutes)
                if not cutoff_res.ok:
                    raise RuleViolation(cutoff_res.reason)

                # 4. Atomically take seat on new cab
                cur.execute(
                    """UPDATE cabs
                       SET seats_taken = seats_taken + 1
                       WHERE cab_id = %s AND company_id = %s AND seats_taken < capacity
                       RETURNING cab_id, slot_time, capacity, seats_taken""",
                    (new_cab_id, identity.company_id),
                )
                new_cab = cur.fetchone()
                if not new_cab:
                    raise CabFull("Requested cab is full or does not exist for this company.")

                # 5. Record shift_request row with ON CONFLICT handling for concurrent double-submits
                now_dt = clock_now()
                cur.execute(
                    """INSERT INTO shift_requests (
                           company_id, employee_id, roster_id, old_cab_id, requested_slot_time,
                           status, reason, idempotency_key, created_at
                       ) VALUES (%s, %s, %s, %s, %s, 'APPLIED', %s, %s, %s)
                       ON CONFLICT (idempotency_key) DO NOTHING
                       RETURNING request_id""",
                    (
                        identity.company_id,
                        identity.employee_id,
                        roster_id,
                        roster["cab_id"],
                        new_cab["slot_time"],
                        "Shift change applied successfully via agent",
                        idempotency_key,
                        now_dt,
                    ),
                )
                req_row = cur.fetchone()

                # If another concurrent thread inserted with this idempotency_key first:
                if not req_row:
                    # Roll back the seat we just took
                    cur.execute(
                        """UPDATE cabs
                           SET seats_taken = GREATEST(0, seats_taken - 1)
                           WHERE cab_id = %s AND company_id = %s""",
                        (new_cab_id, identity.company_id),
                    )
                    cur.execute(
                        """SELECT request_id, status FROM shift_requests
                           WHERE company_id = %s AND idempotency_key = %s""",
                        (identity.company_id, idempotency_key),
                    )
                    ex = cur.fetchone()
                    return {
                        "status": ex["status"] if ex else "APPLIED",
                        "request_id": ex["request_id"] if ex else None,
                        "roster_id": roster_id,
                        "cab_id": new_cab_id,
                        "idempotent": True,
                    }

                # 6. Free seat on old cab
                if roster["cab_id"]:
                    cur.execute(
                        """UPDATE cabs
                           SET seats_taken = GREATEST(0, seats_taken - 1)
                           WHERE cab_id = %s AND company_id = %s""",
                        (roster["cab_id"], identity.company_id),
                    )

                # 7. Update roster
                cur.execute(
                    """UPDATE roster
                       SET cab_id = %s
                       WHERE roster_id = %s AND company_id = %s AND employee_id = %s""",
                    (new_cab_id, roster_id, identity.company_id, identity.employee_id),
                )

                # 8. Record audit_log row
                cur.execute(
                    """INSERT INTO audit_log (company_id, actor_id, actor_role, action, detail, created_at)
                       VALUES (%s, %s, %s, 'APPLY_SHIFT_CHANGE', %s, %s)""",
                    (
                        identity.company_id,
                        identity.employee_id,
                        identity.role,
                        f"Shift changed from {roster['cab_id']} to {new_cab_id}",
                        now_dt,
                    ),
                )

                return {
                    "status": "APPLIED",
                    "request_id": req_row["request_id"],
                    "roster_id": roster_id,
                    "new_cab_id": new_cab_id,
                    "old_cab_id": roster["cab_id"],
                    "idempotent": False,
                }


def create_pending_request(
    identity: Identity,
    roster_id: int | str,
    requested_slot_time: time | str,
    reason: str,
    thread_id: str,
    idempotency_key: str,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Creates a PENDING_APPROVAL request without altering cab seat counts."""
    with get_connection(db_url) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                # Idempotency check
                cur.execute(
                    """SELECT request_id, status FROM shift_requests
                       WHERE company_id = %s AND idempotency_key = %s""",
                    (identity.company_id, idempotency_key),
                )
                existing = cur.fetchone()
                if existing:
                    return {"request_id": existing["request_id"], "status": existing["status"], "idempotent": True}

                # Verify roster belongs to employee
                cur.execute(
                    """SELECT roster_id, cab_id FROM roster
                       WHERE roster_id = %s AND company_id = %s AND employee_id = %s""",
                    (roster_id, identity.company_id, identity.employee_id),
                )
                roster = cur.fetchone()
                if not roster:
                    raise NotFound("Roster entry not found for this employee.")

                def _to_time(t: time | str) -> time:
                    if isinstance(t, str):
                        parts = [int(p) for p in t.split(":")[:2]]
                        return time(parts[0], parts[1])
                    return t

                now_dt = clock_now()
                req_t = _to_time(requested_slot_time)

                cur.execute(
                    """INSERT INTO shift_requests (
                           company_id, employee_id, roster_id, old_cab_id, requested_slot_time,
                           status, reason, idempotency_key, thread_id, created_at
                       ) VALUES (%s, %s, %s, %s, %s, 'PENDING_APPROVAL', %s, %s, %s, %s)
                       ON CONFLICT (idempotency_key) DO NOTHING
                       RETURNING request_id""",
                    (
                        identity.company_id,
                        identity.employee_id,
                        roster_id,
                        roster["cab_id"],
                        req_t,
                        reason,
                        idempotency_key,
                        thread_id,
                        now_dt,
                    ),
                )
                req_row = cur.fetchone()
                if not req_row:
                    cur.execute(
                        """SELECT request_id, status FROM shift_requests
                           WHERE company_id = %s AND idempotency_key = %s""",
                        (identity.company_id, idempotency_key),
                    )
                    ex = cur.fetchone()
                    return {"request_id": ex["request_id"] if ex else None, "status": ex["status"] if ex else "PENDING_APPROVAL", "idempotent": True}

                cur.execute(
                    """INSERT INTO audit_log (company_id, actor_id, actor_role, action, detail, created_at)
                       VALUES (%s, %s, %s, 'CREATE_PENDING_REQUEST', %s, %s)""",
                    (
                        identity.company_id,
                        identity.employee_id,
                        identity.role,
                        f"Created pending approval request for slot {req_t}",
                        now_dt,
                    ),
                )

                return {
                    "request_id": req_row["request_id"],
                    "status": "PENDING_APPROVAL",
                    "idempotent": False,
                }


# ---------------------------------------------------------------------------
# TIMEOUT & ERROR-WRAPPING RUNNER
# ---------------------------------------------------------------------------

def run_tool(fn: Callable[..., Any], *args: Any, timeout: float = 2.0, **kwargs: Any) -> Any:
    """Executes a tool function with a hard timeout and catches unexpected failures."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise ToolTimeout("Tool execution timed out.")
        except (NotFound, RuleViolation, CabFull, ToolTimeout, ToolFailure):
            # Pass typed domain exceptions through intact
            raise
        except Exception as exc:
            logger.exception("Unexpected error in tool execution %s: %s", getattr(fn, "__name__", str(fn)), exc)
            raise ToolFailure("An unexpected failure occurred while processing your request.") from None
