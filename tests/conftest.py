"""Why this exists:
Pytest global configuration and fixtures.
Automatically provisions the isolated shift_test_db database if it does not already exist.
"""
import os
import psycopg

MAINTENANCE_DB_URL = os.getenv("DATABASE_URL", "postgresql://shift:shift_secret@localhost:5432/shift_db")
if "db:5432" not in MAINTENANCE_DB_URL and os.path.exists("/.dockerenv"):
    MAINTENANCE_DB_URL = "postgresql://shift:shift_secret@db:5432/shift_db"

TEST_DB_URL = os.getenv("TEST_DATABASE_URL", "postgresql://shift:shift_secret@localhost:5432/shift_test_db")
if "db:5432" not in TEST_DB_URL and os.path.exists("/.dockerenv"):
    TEST_DB_URL = "postgresql://shift:shift_secret@db:5432/shift_test_db"


def pytest_sessionstart(session):
    """Automatically ensures shift_test_db exists before any tests execute."""
    try:
        with psycopg.connect(MAINTENANCE_DB_URL, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = 'shift_test_db'")
                if not cur.fetchone():
                    cur.execute("CREATE DATABASE shift_test_db")
    except Exception:
        # If creation fails or already exists, individual test connections will report state
        pass

    # Direct all test runs to the isolated test database
    os.environ["DATABASE_URL"] = TEST_DB_URL
