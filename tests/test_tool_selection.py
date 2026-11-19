"""Why this exists:
Tests tool business logic, atomic seat booking, idempotency, and concurrency racing.
Uses a dedicated test database (shift_test_db) to keep demo seed data completely untouched.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
import os
from typing import Any
import pytest

from app.auth import Identity
from app.clock import KOLKATA_TZ, now as clock_now
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
    apply_shift_change,
    check_cab_options,
    get_roster,
)

TEST_DB_URL = os.getenv("TEST_DATABASE_URL", "postgresql://shift:shift_secret@localhost:5432/shift_test_db")
if "db:5432" not in TEST_DB_URL and os.path.exists("/.dockerenv"):
    TEST_DB_URL = "postgresql://shift:shift_secret@db:5432/shift_test_db"


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    """Sets up fresh tables and seed data in shift_test_db before each test."""
    # Set a fixed clock far before cutoff for normal tests: 14:00 on 2026-11-20
    monkeypatch.setenv("FIXED_NOW", "2026-11-20T14:00:00+05:30")
    test_date = date(2026, 11, 20)

    # Wipe and freshly re-seed test database to ensure complete test isolation
    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS audit_log, shift_requests, roster, cabs, employees, companies CASCADE")
        conn.commit()

    # Initialize schema and seed data in test database
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
    # Re-running for date1 should be a no-op
    ensure_service_day(date1, db_url=TEST_DB_URL)

    cabs_date1 = get_cabs("comp_alpha", date1, "drop", db_url=TEST_DB_URL)
    assert len(cabs_date1) == 5

    # Run for date2
    ensure_service_day(date2, db_url=TEST_DB_URL)
    cabs_date2 = get_cabs("comp_alpha", date2, "drop", db_url=TEST_DB_URL)
    assert len(cabs_date2) == 5

    # Distinct cab IDs across dates
    ids1 = {c["cab_id"] for c in cabs_date1}
    ids2 = {c["cab_id"] for c in cabs_date2}
    assert ids1.isdisjoint(ids2)


def test_successful_shift_change():
    """Applying a shift change atomically increments new cab, decrements old, and updates roster."""
    test_date = date(2026, 11, 20)
    identity = Identity(company_id="comp_alpha", employee_id="emp_005", role="employee")

    # emp_005 is initially on 19:00 cab (cab_a_20261120_1900, seats_taken=3)
    # Target cab is 21:00 cab (cab_a_20261120_2100, seats_taken=0)
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

    # Verify seats in DB
    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            # Old cab seat count decremented from 3 to 2
            cur.execute("SELECT seats_taken FROM cabs WHERE cab_id = %s", (old_cab_id,))
            assert cur.fetchone()["seats_taken"] == 2

            # New cab seat count incremented from 0 to 1
            cur.execute("SELECT seats_taken FROM cabs WHERE cab_id = %s", (new_cab_id,))
            assert cur.fetchone()["seats_taken"] == 1

            # Roster updated
            cur.execute("SELECT cab_id FROM roster WHERE roster_id = %s", (roster["roster_id"],))
            assert cur.fetchone()["cab_id"] == new_cab_id

            # Shift request logged as APPLIED
            cur.execute("SELECT status FROM shift_requests WHERE idempotency_key = %s", ("key_success_001",))
            assert cur.fetchone()["status"] == "APPLIED"

            # Audit log written
            cur.execute("SELECT action FROM audit_log WHERE actor_id = %s", (identity.employee_id,))
            assert cur.fetchone()["action"] == "APPLY_SHIFT_CHANGE"


def test_cutoff_violation_in_tool(monkeypatch):
    """If now is inside the cutoff window, apply_shift_change aborts and leaves state untouched."""
    test_date = date(2026, 11, 20)
    identity = Identity(company_id="comp_alpha", employee_id="emp_005", role="employee")

    # emp_005 has trip at 19:00. Cutoff is 120m.
    # Set time to 18:00 (60m before trip -> inside cutoff)
    monkeypatch.setenv("FIXED_NOW", "2026-11-20T18:00:00+05:30")

    roster = get_roster(identity, test_date, db_url=TEST_DB_URL)
    old_cab_id = roster["cab_id"]
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

    # Verify no state was altered
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

    # 18:00 cab is completely full (4/4)
    full_cab_id = "cab_a_20261120_1800"

    with pytest.raises(CabFull):
        apply_shift_change(
            identity=identity,
            roster_id=roster["roster_id"],
            new_cab_id=full_cab_id,
            idempotency_key="key_full_001",
            db_url=TEST_DB_URL,
        )

    # Verify old cab seats and roster unchanged
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

    # First attempt
    res1 = apply_shift_change(
        identity=identity,
        roster_id=roster["roster_id"],
        new_cab_id=new_cab_id,
        idempotency_key="idem_key_42",
        db_url=TEST_DB_URL,
    )
    assert res1["status"] == "APPLIED"
    assert res1["idempotent"] is False

    # Second attempt with same key
    res2 = apply_shift_change(
        identity=identity,
        roster_id=roster["roster_id"],
        new_cab_id=new_cab_id,
        idempotency_key="idem_key_42",
        db_url=TEST_DB_URL,
    )
    assert res2["status"] == "APPLIED"
    assert res2["idempotent"] is True

    # Seats on new cab must be 1, NOT 2
    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT seats_taken FROM cabs WHERE cab_id = %s", (new_cab_id,))
            assert cur.fetchone()["seats_taken"] == 1

            cur.execute("SELECT COUNT(*) AS cnt FROM shift_requests WHERE idempotency_key = %s", ("idem_key_42",))
            assert cur.fetchone()["cnt"] == 1


def test_concurrency_race_for_last_seat():
    """Two employees racing concurrently for 1 available seat: exactly one succeeds, one gets CabFull."""
    test_date = date(2026, 11, 20)
    # Comp Alpha 19:00 cab currently has 3/4 seats taken (1 free seat!)
    target_cab = "cab_a_20261120_1900"

    # Two employees currently on other cabs
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

    # Exactly one succeeded and exactly one got CabFull
    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], CabFull)

    # Seats taken must be exactly 4 (capacity), never 5
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

    # Both return successfully (one fresh, one idempotent)
    assert all(not isinstance(o, Exception) for o in outcomes)

    # Verify only 1 shift_requests row in DB
    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM shift_requests WHERE idempotency_key = %s", ("concurrent_dup_key",))
            assert cur.fetchone()["cnt"] == 1


def test_tenant_isolation_tools():
    """Cross-tenant cab booking or roster access is completely blocked."""
    test_date = date(2026, 11, 20)
    alpha_emp = Identity(company_id="comp_alpha", employee_id="emp_005", role="employee")
    roster_alpha = get_roster(alpha_emp, test_date, db_url=TEST_DB_URL)

    # Alpha employee trying to book Beta's cab
    beta_cab_id = "cab_b_20261120_1800"
    with pytest.raises(CabFull):  # Zero rows updated because company_id does not match
        apply_shift_change(
            identity=alpha_emp,
            roster_id=roster_alpha["roster_id"],
            new_cab_id=beta_cab_id,
            idempotency_key="cross_tenant_key",
            db_url=TEST_DB_URL,
        )

    # Alpha employee trying to use another employee's roster_id
    with pytest.raises(NotFound):
        apply_shift_change(
            identity=alpha_emp,
            roster_id=99999,  # Non-existent or other employee's roster
            new_cab_id="cab_a_20261120_2100",
            idempotency_key="cross_roster_key",
            db_url=TEST_DB_URL,
        )
