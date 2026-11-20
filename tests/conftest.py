"""Why this exists:
Pytest global configuration and fixtures.
Automatically provisions the isolated shift_test_db database if it does not already exist.
"""
import os
from app.db import ensure_test_db

TEST_DB_URL = os.getenv("TEST_DATABASE_URL", "postgresql://shift:shift_secret@localhost:5432/shift_test_db")
if "db:5432" not in TEST_DB_URL and os.path.exists("/.dockerenv"):
    TEST_DB_URL = "postgresql://shift:shift_secret@db:5432/shift_test_db"


def pytest_sessionstart(session):
    """Automatically ensures shift_test_db exists before any tests execute."""
    ensure_test_db()
    # Direct all test runs to the isolated test database
    os.environ["DATABASE_URL"] = TEST_DB_URL
