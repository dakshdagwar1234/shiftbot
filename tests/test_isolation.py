"""Why this exists:
Tests multi-tenant data isolation and database seed idempotency against PostgreSQL.
Ensures company_id strictly prevents cross-tenant leakage across all queries.
"""
from datetime import datetime, timezone
import pytest

from app.db import (
    CREATE_TABLES_SQL,
    ensure_service_day,
    get_cabs,
    get_company_rules,
    get_connection,
    get_roster,
    init_db,
    seed_companies_and_employees,
)


@pytest.fixture(scope="module", autouse=True)
def setup_db():
    """Ensures database is initialized before running isolation tests."""
    init_db()


def test_company_rules_isolation():
    """get_company_rules returns strictly the requested company's parameters."""
    alpha_rules = get_company_rules("comp_alpha")
    beta_rules = get_company_rules("comp_beta")

    assert alpha_rules is not None
    assert alpha_rules["company_id"] == "comp_alpha"
    assert alpha_rules["cutoff_minutes"] == 120
    assert alpha_rules["seats_per_cab"] == 4

    assert beta_rules is not None
    assert beta_rules["company_id"] == "comp_beta"
    assert beta_rules["cutoff_minutes"] == 60
    assert beta_rules["seats_per_cab"] == 6

    # Non-existent company returns None
    assert get_company_rules("comp_unknown") is None


def test_get_cabs_tenant_isolation():
    """get_cabs for comp_alpha returns only comp_alpha cabs and no comp_beta cabs."""
    today = datetime.now(timezone.utc).date()
    alpha_cabs = get_cabs("comp_alpha", service_date=today, trip_type="drop")
    beta_cabs = get_cabs("comp_beta", service_date=today, trip_type="drop")

    assert len(alpha_cabs) == 5
    assert len(beta_cabs) == 5

    # Every cab in alpha_cabs must belong to comp_alpha
    for cab in alpha_cabs:
        assert cab["company_id"] == "comp_alpha"

    # Every cab in beta_cabs must belong to comp_beta
    for cab in beta_cabs:
        assert cab["company_id"] == "comp_beta"

    # No cross-tenant intersection
    alpha_ids = {c["cab_id"] for c in alpha_cabs}
    beta_ids = {c["cab_id"] for c in beta_cabs}
    assert alpha_ids.isdisjoint(beta_ids)


def test_get_roster_tenant_isolation():
    """Querying roster with the wrong company_id returns None even with a valid employee_id."""
    today = datetime.now(timezone.utc).date()

    # Alice belongs to comp_alpha
    alpha_roster = get_roster("comp_alpha", "emp_001", service_date=today)
    assert alpha_roster is not None
    assert alpha_roster["company_id"] == "comp_alpha"
    assert alpha_roster["employee_id"] == "emp_001"
    assert "cab_a_" in alpha_roster["cab_id"] and "1800" in alpha_roster["cab_id"]

    # Querying Alice under comp_beta must return None
    cross_tenant_query = get_roster("comp_beta", "emp_001", service_date=today)
    assert cross_tenant_query is None

    # Querying Beta employee (emp_101) under comp_alpha must return None
    cross_tenant_beta = get_roster("comp_alpha", "emp_101", service_date=today)
    assert cross_tenant_beta is None


def test_seed_idempotency_preserves_row_counts():
    """Calling init_db multiple times does not insert duplicate rows or corrupt tables."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM companies")
            companies_before = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(*) AS cnt FROM employees")
            employees_before = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(*) AS cnt FROM cabs")
            cabs_before = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(*) AS cnt FROM roster")
            roster_before = cur.fetchone()["cnt"]

    # Re-run init_db
    init_db()

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM companies")
            assert cur.fetchone()["cnt"] == companies_before

            cur.execute("SELECT COUNT(*) AS cnt FROM employees")
            assert cur.fetchone()["cnt"] == employees_before

            cur.execute("SELECT COUNT(*) AS cnt FROM cabs")
            assert cur.fetchone()["cnt"] == cabs_before

            cur.execute("SELECT COUNT(*) AS cnt FROM roster")
            assert cur.fetchone()["cnt"] == roster_before


def test_isolated_schema_lifecycle():
    """Tests schema creation and seeding in an isolated PostgreSQL test schema to guarantee zero demo contamination."""
    today = datetime.now(timezone.utc).date()
    test_schema = "test_sandbox_schema"

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Create a sandboxed temporary schema
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {test_schema}")
            cur.execute(f"SET search_path TO {test_schema}")
            cur.execute(CREATE_TABLES_SQL)
            conn.commit()

        # Seed the sandboxed schema
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {test_schema}")
        seed_companies_and_employees(conn)
        conn.commit()

        # Verify idempotency within the isolated schema
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {test_schema}")
            cur.execute("SELECT COUNT(*) AS cnt FROM companies")
            assert cur.fetchone()["cnt"] == 2

        # Second seed invocation within isolated schema should be a no-op
        seed_companies_and_employees(conn)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {test_schema}")
            cur.execute("SELECT COUNT(*) AS cnt FROM companies")
            assert cur.fetchone()["cnt"] == 2

            # Clean up sandboxed schema completely
            cur.execute(f"DROP SCHEMA {test_schema} CASCADE")
        conn.commit()
