"""Why this exists:
Tests the /health endpoint for application and database readiness.
Uses mocks to verify both healthy and database-failure responses without a real LLM.
"""
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_success():
    """Verifies that /health returns status ok when the database is reachable."""
    with patch("psycopg.connect") as mock_connect:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "db": "ok"}


def test_health_db_failure():
    """Verifies that /health returns 503 error when database connection fails."""
    with patch("psycopg.connect", side_effect=Exception("DB unavailable")):
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json() == {"status": "error", "db": "unreachable"}
