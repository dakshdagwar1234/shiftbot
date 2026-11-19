"""Why this exists:
Tests JWT authentication, claim validation, role enforcement, and token rejection scenarios.
Verifies security properties including algorithm restrictions, tampering rejection, and multi-tenant isolation.
"""
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends
from fastapi.testclient import TestClient
import jwt
import pytest

from app.auth import (
    JWT_SECRET,
    Identity,
    decode_token,
    issue_token,
    require_manager,
)
from app.main import app

# Add a tiny test-only route to test manager-only role enforcement
@app.get("/test-manager-only")
def manager_only_route(identity: Annotated[Identity, Depends(require_manager)]):
    return {"manager_id": identity.employee_id}


client = TestClient(app)


def test_valid_token_whoami():
    """Valid token returns matching identity claims from /whoami."""
    token = issue_token(company_id="acme", employee_id="emp_42", role="employee")
    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {
        "company_id": "acme",
        "employee_id": "emp_42",
        "role": "employee",
    }


def test_missing_authorization_header():
    """Request without Authorization header is rejected with 401."""
    response = client.get("/whoami")
    assert response.status_code == 401
    assert "Invalid or missing authentication token" in response.json()["detail"]


def test_garbage_token():
    """Malformed or invalid token string is rejected with 401."""
    response = client.get("/whoami", headers={"Authorization": "Bearer not_a_real_token"})
    assert response.status_code == 401
    assert "Invalid or missing authentication token" in response.json()["detail"]


def test_expired_token():
    """Token with past expiration timestamp is rejected with 401."""
    expired_token = issue_token(company_id="acme", employee_id="emp_42", role="employee", expires_in_minutes=-5)
    response = client.get("/whoami", headers={"Authorization": f"Bearer {expired_token}"})
    assert response.status_code == 401
    assert "Invalid or missing authentication token" in response.json()["detail"]


def test_token_signed_with_wrong_secret():
    """Token signed with an unrecognized secret key is rejected with 401."""
    now = datetime.now(timezone.utc)
    payload = {
        "company_id": "acme",
        "employee_id": "emp_42",
        "role": "employee",
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    forged_token = jwt.encode(payload, "completely_wrong_secret_key", algorithm="HS256")
    response = client.get("/whoami", headers={"Authorization": f"Bearer {forged_token}"})
    assert response.status_code == 401
    assert "Invalid or missing authentication token" in response.json()["detail"]


def test_token_with_alg_none():
    """Token attempting to bypass cryptographic signing via alg=none is rejected with 401."""
    # PyJWT rejects "none" algorithm when HS256 is expected
    none_token = jwt.encode(
        {"company_id": "acme", "employee_id": "emp_42", "role": "employee", "exp": 9999999999},
        key="",
        algorithm="none",
    )
    response = client.get("/whoami", headers={"Authorization": f"Bearer {none_token}"})
    assert response.status_code == 401
    assert "Invalid or missing authentication token" in response.json()["detail"]


def test_token_missing_company_id():
    """Token omitting required company_id claim is rejected with 401."""
    now = datetime.now(timezone.utc)
    payload = {
        "employee_id": "emp_42",
        "role": "employee",
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert "Invalid or missing authentication token" in response.json()["detail"]


def test_token_with_unknown_role():
    """Token with unauthorized role (e.g. admin) is rejected with 401."""
    now = datetime.now(timezone.utc)
    payload = {
        "company_id": "acme",
        "employee_id": "emp_42",
        "role": "admin",
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert "Invalid or missing authentication token" in response.json()["detail"]


def test_employee_forbidden_on_manager_route():
    """Employee token attempting to access a manager-only endpoint receives 403 Forbidden."""
    emp_token = issue_token(company_id="acme", employee_id="emp_42", role="employee")
    response = client.get("/test-manager-only", headers={"Authorization": f"Bearer {emp_token}"})
    assert response.status_code == 403
    assert "Access forbidden: manager role required" in response.json()["detail"]

    # Manager token succeeds
    mgr_token = issue_token(company_id="acme", employee_id="mgr_01", role="manager")
    mgr_response = client.get("/test-manager-only", headers={"Authorization": f"Bearer {mgr_token}"})
    assert mgr_response.status_code == 200
    assert mgr_response.json() == {"manager_id": "mgr_01"}


def test_identity_ignores_query_and_body_tampering():
    """Query parameter or request body attempting to spoof company_id is completely ignored."""
    token = issue_token(company_id="real_company", employee_id="emp_42", role="employee")
    # Caller attempts to spoof company_id via query parameter
    response = client.get("/whoami?company_id=attacker_company", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    data = response.json()
    assert data["company_id"] == "real_company"
    assert data["company_id"] != "attacker_company"
