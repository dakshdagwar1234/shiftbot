"""Why this exists:
Comprehensive tests for business tools, atomic booking, concurrency, timeouts, and idempotency.
Uses an isolated test database (shift_test_db) to keep production demo seed data untouched.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
import os
import time as time_module
import pytest

from app.auth import Identity
from app.clock import now as clock_now
from app.db import (
    ensure_service_day,
    get_cabs,
    get_connection,
    init_db,
)
from app.tools import (
    CabFull,
    NotFound,
    RuleViolation,
    ToolFailure,
    ToolTimeout,
    apply_shift_change,
    get_roster,
    run_tool,
)

TEST_DB_URL = os.getenv("TEST_DATABASE_URL", "postgresql://shift:shift_secret@localhost:5432/shift_test_db")
if "db:5432" not in TEST_DB_URL and os.path.exists("/.dockerenv"):
    TEST_DB_URL = "postgresql://shift:shift_secret@db:5432/shift_test_db"


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    """Sets up fresh tables and seed data in shift_test_db before each test."""
    monkeypatch.setenv("FIXED_NOW", "2026-11-20T14:00:00+05:30")
    test_date = date(2026, 11, 20)

    # Wipe and freshly re-seed test database to ensure complete test isolation
    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS audit_log, shift_requests, roster, cabs, employees, companies CASCADE")
        conn.commit()

    init_db(TEST_DB_URL)
    ensure_service_day(test_date, db_url=TEST_DB_URL)
    yield


def test_clock_fixed_now_override(monkeypatch):
    """Verifies that FIXED_NOW environment variable strictly overrides system time."""
    monkeypatch.setenv("FIXED_NOW", "2026-12-25T09:30:00+05:30")
    current_time = clock_now()
    assert current_time.year == 2026
    assert current_time.month == 12
    assert current_time.day == 25
    assert current_time.hour == 9
    assert current_time.minute == 30


def test_ensure_service_day_idempotency_and_distinct_dates():
    """ensure_service_day is idempotent for the same date and creates isolated cabs for new dates."""
    date1 = date(2026, 11, 21)
    date2 = date(2026, 11, 22)

    ensure_service_day(date1, db_url=TEST_DB_URL)
    ensure_service_day(date1, db_url=TEST_DB_URL)

    cabs_date1 = get_cabs("comp_alpha", date1, "drop", db_url=TEST_DB_URL)
    assert len(cabs_date1) == 5

    ensure_service_day(date2, db_url=TEST_DB_URL)
    cabs_date2 = get_cabs("comp_alpha", date2, "drop", db_url=TEST_DB_URL)
    assert len(cabs_date2) == 5

    ids1 = {c["cab_id"] for c in cabs_date1}
    ids2 = {c["cab_id"] for c in cabs_date2}
    assert ids1.isdisjoint(ids2)


def test_successful_shift_change():
    """Applying a shift change atomically increments new cab, decrements old, and updates roster."""
    test_date = date(2026, 11, 20)
    identity = Identity(company_id="comp_alpha", employee_id="emp_005", role="employee")

    old_cab_id = "cab_a_20261120_1900"
    new_cab_id = "cab_a_20261120_2100"

    roster = get_roster(identity, test_date, db_url=TEST_DB_URL)
    assert roster is not None
    assert roster["cab_id"] == old_cab_id

    result = apply_shift_change(
        identity=identity,
        roster_id=roster["roster_id"],
        new_cab_id=new_cab_id,
        idempotency_key="key_success_001",
        db_url=TEST_DB_URL,
    )
    assert result["status"] == "APPLIED"

    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT seats_taken FROM cabs WHERE cab_id = %s", (old_cab_id,))
            assert cur.fetchone()["seats_taken"] == 2

            cur.execute("SELECT seats_taken FROM cabs WHERE cab_id = %s", (new_cab_id,))
            assert cur.fetchone()["seats_taken"] == 1

            cur.execute("SELECT cab_id FROM roster WHERE roster_id = %s", (roster["roster_id"],))
            assert cur.fetchone()["cab_id"] == new_cab_id

            cur.execute("SELECT status FROM shift_requests WHERE idempotency_key = %s", ("key_success_001",))
            assert cur.fetchone()["status"] == "APPLIED"

            cur.execute("SELECT action FROM audit_log WHERE actor_id = %s", (identity.employee_id,))
            assert cur.fetchone()["action"] == "APPLY_SHIFT_CHANGE"


def test_cutoff_violation_in_tool(monkeypatch):
    """If now is inside the cutoff window, apply_shift_change aborts and leaves state untouched."""
    test_date = date(2026, 11, 20)
    identity = Identity(company_id="comp_alpha", employee_id="emp_005", role="employee")

    monkeypatch.setenv("FIXED_NOW", "2026-11-20T18:00:00+05:30")

    roster = get_roster(identity, test_date, db_url=TEST_DB_URL)
    new_cab_id = "cab_a_20261120_2100"

    with pytest.raises(RuleViolation) as exc_info:
        apply_shift_change(
            identity=identity,
            roster_id=roster["roster_id"],
            new_cab_id=new_cab_id,
            idempotency_key="key_violation_001",
            db_url=TEST_DB_URL,
        )
    assert "Manager approval required" in str(exc_info.value)

    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT seats_taken FROM cabs WHERE cab_id = %s", (new_cab_id,))
            assert cur.fetchone()["seats_taken"] == 0

            cur.execute("SELECT COUNT(*) AS cnt FROM shift_requests WHERE idempotency_key = %s", ("key_violation_001",))
            assert cur.fetchone()["cnt"] == 0


def test_full_cab_rejection():
    """Attempting to take a seat in a full cab raises CabFull and aborts without changes."""
    test_date = date(2026, 11, 20)
    identity = Identity(company_id="comp_alpha", employee_id="emp_005", role="employee")
    roster = get_roster(identity, test_date, db_url=TEST_DB_URL)

    full_cab_id = "cab_a_20261120_1800"

    with pytest.raises(CabFull):
        apply_shift_change(
            identity=identity,
            roster_id=roster["roster_id"],
            new_cab_id=full_cab_id,
            idempotency_key="key_full_001",
            db_url=TEST_DB_URL,
        )

    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT seats_taken FROM cabs WHERE cab_id = %s", (full_cab_id,))
            assert cur.fetchone()["seats_taken"] == 4


def test_idempotency_same_key_twice():
    """Submitting the exact same idempotency key twice returns cached result without double booking."""
    test_date = date(2026, 11, 20)
    identity = Identity(company_id="comp_alpha", employee_id="emp_005", role="employee")
    roster = get_roster(identity, test_date, db_url=TEST_DB_URL)
    new_cab_id = "cab_a_20261120_2100"

    res1 = apply_shift_change(
        identity=identity,
        roster_id=roster["roster_id"],
        new_cab_id=new_cab_id,
        idempotency_key="idem_key_42",
        db_url=TEST_DB_URL,
    )
    assert res1["status"] == "APPLIED"
    assert res1["idempotent"] is False

    res2 = apply_shift_change(
        identity=identity,
        roster_id=roster["roster_id"],
        new_cab_id=new_cab_id,
        idempotency_key="idem_key_42",
        db_url=TEST_DB_URL,
    )
    assert res2["status"] == "APPLIED"
    assert res2["idempotent"] is True

    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT seats_taken FROM cabs WHERE cab_id = %s", (new_cab_id,))
            assert cur.fetchone()["seats_taken"] == 1

            cur.execute("SELECT COUNT(*) AS cnt FROM shift_requests WHERE idempotency_key = %s", ("idem_key_42",))
            assert cur.fetchone()["cnt"] == 1


def test_concurrency_race_for_last_seat():
    """Two employees racing concurrently for 1 available seat: exactly one succeeds, one gets CabFull."""
    test_date = date(2026, 11, 20)
    target_cab = "cab_a_20261120_1900"

    emp_a = Identity(company_id="comp_alpha", employee_id="emp_001", role="employee")
    emp_b = Identity(company_id="comp_alpha", employee_id="emp_002", role="employee")

    roster_a = get_roster(emp_a, test_date, db_url=TEST_DB_URL)
    roster_b = get_roster(emp_b, test_date, db_url=TEST_DB_URL)

    results = []
    errors = []

    def book(emp_id_obj, r_id, key):
        try:
            res = apply_shift_change(
                identity=emp_id_obj,
                roster_id=r_id,
                new_cab_id=target_cab,
                idempotency_key=key,
                db_url=TEST_DB_URL,
            )
            results.append(res)
        except Exception as e:
            errors.append(e)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(book, emp_a, roster_a["roster_id"], "race_key_a")
        f2 = pool.submit(book, emp_b, roster_b["roster_id"], "race_key_b")
        f1.result()
        f2.result()

    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], CabFull)

    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT seats_taken, capacity FROM cabs WHERE cab_id = %s", (target_cab,))
            row = cur.fetchone()
            assert row["seats_taken"] == row["capacity"] == 4


def test_concurrency_double_submit_same_key():
    """One employee double-submitting simultaneously with same key produces exactly one booking."""
    test_date = date(2026, 11, 20)
    identity = Identity(company_id="comp_alpha", employee_id="emp_005", role="employee")
    roster = get_roster(identity, test_date, db_url=TEST_DB_URL)
    target_cab = "cab_a_20261120_2000"

    outcomes = []

    def submit():
        try:
            return apply_shift_change(
                identity=identity,
                roster_id=roster["roster_id"],
                new_cab_id=target_cab,
                idempotency_key="concurrent_dup_key",
                db_url=TEST_DB_URL,
            )
        except Exception as e:
            return e

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(submit)
        f2 = pool.submit(submit)
        outcomes.append(f1.result())
        outcomes.append(f2.result())

    assert all(not isinstance(o, Exception) for o in outcomes)

    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM shift_requests WHERE idempotency_key = %s", ("concurrent_dup_key",))
            assert cur.fetchone()["cnt"] == 1


def test_tenant_isolation_tools():
    """Cross-tenant cab booking or roster access is completely blocked."""
    test_date = date(2026, 11, 20)
    alpha_emp = Identity(company_id="comp_alpha", employee_id="emp_005", role="employee")
    roster_alpha = get_roster(alpha_emp, test_date, db_url=TEST_DB_URL)

    beta_cab_id = "cab_b_20261120_1800"
    with pytest.raises(CabFull):
        apply_shift_change(
            identity=alpha_emp,
            roster_id=roster_alpha["roster_id"],
            new_cab_id=beta_cab_id,
            idempotency_key="cross_tenant_key",
            db_url=TEST_DB_URL,
        )

    with pytest.raises(NotFound):
        apply_shift_change(
            identity=alpha_emp,
            roster_id=99999,
            new_cab_id="cab_a_20261120_2100",
            idempotency_key="cross_roster_key",
            db_url=TEST_DB_URL,
        )


def test_run_tool_timeout():
    """A tool function exceeding the timeout limit is terminated with ToolTimeout."""
    def slow_tool():
        time_module.sleep(0.5)
        return "finished"

    with pytest.raises(ToolTimeout) as exc_info:
        run_tool(slow_tool, timeout=0.1)

    assert "Tool execution timed out" in str(exc_info.value)


def test_run_tool_masks_unexpected_exception():
    """Unhandled exceptions are converted to ToolFailure with a safe, plain error message."""
    def buggy_tool():
        raise ZeroDivisionError("division by zero internal stack trace details")

    with pytest.raises(ToolFailure) as exc_info:
        run_tool(buggy_tool, timeout=1.0)

    assert "division by zero" not in str(exc_info.value)
    assert "An unexpected failure occurred while processing your request." in str(exc_info.value)


def test_run_tool_passes_through_domain_exceptions():
    """Domain-typed exceptions (NotFound, RuleViolation) pass through without being masked."""
    def not_found_tool():
        raise NotFound("Roster not found")

    with pytest.raises(NotFound) as exc_info:
        run_tool(not_found_tool)
    assert "Roster not found" in str(exc_info.value)

    def rule_violation_tool():
        raise RuleViolation("Cutoff window passed")

    with pytest.raises(RuleViolation) as exc_info:
        run_tool(rule_violation_tool)
    assert "Cutoff window passed" in str(exc_info.value)
