"""Why this exists:
Manages PostgreSQL schema initialization, service-day seeding, and tenant-isolated data queries.
Guarantees every database query enforces strict company_id isolation.
"""
from datetime import date, time
import os
from typing import Any
import psycopg
from psycopg.rows import dict_row

from app.clock import now as clock_now

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://shift:shift_secret@localhost:5432/shift_db")


def get_connection(db_url: str | None = None) -> psycopg.Connection:
    """Returns a new psycopg database connection configured with dict row factory."""
    url = db_url or os.getenv("DATABASE_URL", DATABASE_URL)
    return psycopg.connect(url, row_factory=dict_row)


# ---------------------------------------------------------------------------
# SCHEMA DEFINITIONS
# ---------------------------------------------------------------------------

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS companies (
    company_id VARCHAR(50) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    cutoff_minutes INT NOT NULL,
    seats_per_cab INT NOT NULL
);

CREATE TABLE IF NOT EXISTS employees (
    company_id VARCHAR(50) NOT NULL REFERENCES companies(company_id),
    employee_id VARCHAR(50) NOT NULL,
    name VARCHAR(100) NOT NULL,
    PRIMARY KEY (company_id, employee_id)
);

CREATE TABLE IF NOT EXISTS cabs (
    cab_id VARCHAR(50) PRIMARY KEY,
    company_id VARCHAR(50) NOT NULL REFERENCES companies(company_id),
    route VARCHAR(100) NOT NULL,
    trip_type VARCHAR(20) NOT NULL CHECK (trip_type IN ('pickup', 'drop')),
    service_date DATE NOT NULL,
    slot_time TIME NOT NULL,
    capacity INT NOT NULL,
    seats_taken INT NOT NULL DEFAULT 0,
    CHECK (seats_taken >= 0 AND seats_taken <= capacity)
);

CREATE TABLE IF NOT EXISTS roster (
    roster_id SERIAL PRIMARY KEY,
    company_id VARCHAR(50) NOT NULL REFERENCES companies(company_id),
    employee_id VARCHAR(50) NOT NULL,
    cab_id VARCHAR(50) REFERENCES cabs(cab_id),
    service_date DATE NOT NULL,
    trip_type VARCHAR(20) NOT NULL CHECK (trip_type IN ('pickup', 'drop'))
);

CREATE TABLE IF NOT EXISTS shift_requests (
    request_id SERIAL PRIMARY KEY,
    company_id VARCHAR(50) NOT NULL REFERENCES companies(company_id),
    employee_id VARCHAR(50) NOT NULL,
    roster_id INT REFERENCES roster(roster_id),
    old_cab_id VARCHAR(50) REFERENCES cabs(cab_id),
    requested_slot_time TIME NOT NULL,
    status VARCHAR(30) NOT NULL CHECK (status IN ('PENDING_APPROVAL', 'APPROVED', 'REJECTED', 'APPLIED', 'FAILED')),
    reason TEXT,
    idempotency_key TEXT UNIQUE,
    thread_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decided_by VARCHAR(50),
    decided_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    company_id VARCHAR(50) NOT NULL REFERENCES companies(company_id),
    actor_id VARCHAR(50) NOT NULL,
    actor_role VARCHAR(50) NOT NULL,
    action VARCHAR(100) NOT NULL,
    detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


# ---------------------------------------------------------------------------
# SEED DATA BLOCKS (Companies & Employees seeded once, Service-Day seeded per date)
# ---------------------------------------------------------------------------

def seed_companies_and_employees(conn: psycopg.Connection) -> None:
    """Inserts foundational company and employee rows if companies table is empty."""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM companies")
        row = cur.fetchone()
        if row and row["cnt"] > 0:
            return  # Idempotent: already seeded

        companies = [
            ("comp_alpha", "Alpha Technologies", 120, 4),  # 2h cutoff, 4 seats/cab
            ("comp_beta", "Beta Solutions", 60, 6),        # 1h cutoff, 6 seats/cab
        ]
        for c in companies:
            cur.execute(
                "INSERT INTO companies (company_id, name, cutoff_minutes, seats_per_cab) VALUES (%s, %s, %s, %s)",
                c,
            )

        employees = [
            # Alpha employees
            ("comp_alpha", "emp_001", "Alice Alpha"),
            ("comp_alpha", "emp_002", "Bob Alpha"),
            ("comp_alpha", "emp_003", "Charlie Alpha"),
            ("comp_alpha", "emp_004", "David Alpha"),
            ("comp_alpha", "emp_005", "Eve Alpha"),
            ("comp_alpha", "mgr_alpha", "Manager Alpha"),
            # Beta employees
            ("comp_beta", "emp_101", "Alex Beta"),
            ("comp_beta", "emp_102", "Brian Beta"),
            ("comp_beta", "emp_103", "Clara Beta"),
            ("comp_beta", "emp_104", "Daniel Beta"),
            ("comp_beta", "emp_105", "Emma Beta"),
            ("comp_beta", "mgr_beta", "Manager Beta"),
        ]
        for emp in employees:
            cur.execute(
                "INSERT INTO employees (company_id, employee_id, name) VALUES (%s, %s, %s)",
                emp,
            )


def ensure_service_day(service_date: date, db_url: str | None = None) -> None:
    """Idempotently generates cabs and employee rosters for a specific service date."""
    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            # Check if this date already has cabs seeded
            cur.execute("SELECT COUNT(*) AS cnt FROM cabs WHERE service_date = %s", (service_date,))
            row = cur.fetchone()
            if row and row["cnt"] > 0:
                return  # Idempotent: cabs for this date already exist

            date_str = service_date.strftime("%Y%m%d")

            # Comp Alpha: cap=4; 18:00 full (4/4), 19:00 nearly full (3/4), 20:00 (1/4), 21:00 (0/4), 22:00 (0/4)
            alpha_cabs = [
                (f"cab_a_{date_str}_1800", "comp_alpha", "Route North", "drop", service_date, time(18, 0), 4, 4),
                (f"cab_a_{date_str}_1900", "comp_alpha", "Route North", "drop", service_date, time(19, 0), 4, 3),
                (f"cab_a_{date_str}_2000", "comp_alpha", "Route North", "drop", service_date, time(20, 0), 4, 1),
                (f"cab_a_{date_str}_2100", "comp_alpha", "Route North", "drop", service_date, time(21, 0), 4, 0),
                (f"cab_a_{date_str}_2200", "comp_alpha", "Route North", "drop", service_date, time(22, 0), 4, 0),
            ]
            for cab in alpha_cabs:
                cur.execute(
                    """INSERT INTO cabs (cab_id, company_id, route, trip_type, service_date, slot_time, capacity, seats_taken)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    cab,
                )

            # Comp Beta: cap=6; 18:00 (0/6), 19:00 full (6/6), 20:00 nearly full (5/6), 21:00 (1/6), 22:00 (0/6)
            beta_cabs = [
                (f"cab_b_{date_str}_1800", "comp_beta", "Route South", "drop", service_date, time(18, 0), 6, 0),
                (f"cab_b_{date_str}_1900", "comp_beta", "Route South", "drop", service_date, time(19, 0), 6, 6),
                (f"cab_b_{date_str}_2000", "comp_beta", "Route South", "drop", service_date, time(20, 0), 6, 5),
                (f"cab_b_{date_str}_2100", "comp_beta", "Route South", "drop", service_date, time(21, 0), 6, 1),
                (f"cab_b_{date_str}_2200", "comp_beta", "Route South", "drop", service_date, time(22, 0), 6, 0),
            ]
            for cab in beta_cabs:
                cur.execute(
                    """INSERT INTO cabs (cab_id, company_id, route, trip_type, service_date, slot_time, capacity, seats_taken)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    cab,
                )

            # Comp Alpha Rosters: emp_001..emp_004 on 18:00, emp_005 on 19:00
            alpha_rosters = [
                ("comp_alpha", "emp_001", f"cab_a_{date_str}_1800", service_date, "drop"),
                ("comp_alpha", "emp_002", f"cab_a_{date_str}_1800", service_date, "drop"),
                ("comp_alpha", "emp_003", f"cab_a_{date_str}_1800", service_date, "drop"),
                ("comp_alpha", "emp_004", f"cab_a_{date_str}_1800", service_date, "drop"),
                ("comp_alpha", "emp_005", f"cab_a_{date_str}_1900", service_date, "drop"),
            ]
            for r in alpha_rosters:
                cur.execute(
                    """INSERT INTO roster (company_id, employee_id, cab_id, service_date, trip_type)
                       VALUES (%s, %s, %s, %s, %s)""",
                    r,
                )

            # Comp Beta Rosters: emp_101..emp_105 on 19:00
            beta_rosters = [
                ("comp_beta", "emp_101", f"cab_b_{date_str}_1900", service_date, "drop"),
                ("comp_beta", "emp_102", f"cab_b_{date_str}_1900", service_date, "drop"),
                ("comp_beta", "emp_103", f"cab_b_{date_str}_1900", service_date, "drop"),
                ("comp_beta", "emp_104", f"cab_b_{date_str}_1900", service_date, "drop"),
                ("comp_beta", "emp_105", f"cab_b_{date_str}_1900", service_date, "drop"),
            ]
            for r in beta_rosters:
                cur.execute(
                    """INSERT INTO roster (company_id, employee_id, cab_id, service_date, trip_type)
                       VALUES (%s, %s, %s, %s, %s)""",
                    r,
                )
        conn.commit()


def init_db(db_url: str | None = None) -> None:
    """Initializes schema and runs initial seeding using clock.now()."""
    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLES_SQL)
        seed_companies_and_employees(conn)
        conn.commit()

    # Seed cabs and roster for today's service day
    ensure_service_day(clock_now().date(), db_url=db_url)


# ---------------------------------------------------------------------------
# TENANT-ISOLATED DATA ACCESS QUERIES (Mandatory company_id enforcement)
# ---------------------------------------------------------------------------

def get_roster(company_id: str, employee_id: str, service_date: date, db_url: str | None = None) -> dict[str, Any] | None:
    """Retrieves the scheduled roster record for an employee on a given service date."""
    if not company_id:
        raise ValueError("company_id is required for multi-tenant data access")

    query = """
    SELECT r.roster_id, r.company_id, r.employee_id, r.cab_id, r.service_date, r.trip_type,
           c.slot_time, c.route, c.capacity, c.seats_taken
    FROM roster r
    LEFT JOIN cabs c ON r.cab_id = c.cab_id AND r.company_id = c.company_id
    WHERE r.company_id = %s AND r.employee_id = %s AND r.service_date = %s
    """
    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (company_id, employee_id, service_date))
            return cur.fetchone()


def get_cabs(company_id: str, service_date: date, trip_type: str, db_url: str | None = None) -> list[dict[str, Any]]:
    """Retrieves all cabs for a company on a given service date and trip type."""
    if not company_id:
        raise ValueError("company_id is required for multi-tenant data access")

    query = """
    SELECT cab_id, company_id, route, trip_type, service_date, slot_time, capacity, seats_taken
    FROM cabs
    WHERE company_id = %s AND service_date = %s AND trip_type = %s
    ORDER BY slot_time ASC
    """
    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (company_id, service_date, trip_type))
            return cur.fetchall()


def get_company_rules(company_id: str, db_url: str | None = None) -> dict[str, Any] | None:
    """Retrieves the company configuration including cutoff minutes and cab capacity."""
    if not company_id:
        raise ValueError("company_id is required for multi-tenant data access")

    query = """
    SELECT company_id, name, cutoff_minutes, seats_per_cab
    FROM companies
    WHERE company_id = %s
    """
    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (company_id,))
            return cur.fetchone()
