"""Why this exists:
Handles JWT issuance, extraction, and validation for multi-tenant identity.
Ensures company_id, employee_id, and role are verified strictly via cryptographically signed tokens.
"""
from datetime import datetime, timedelta, timezone
import os
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
import jwt
from pydantic import BaseModel, ConfigDict

JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError("Configuration error: JWT_SECRET environment variable is missing.")

JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_MINUTES = int(os.getenv("JWT_EXPIRATION_MINUTES", "1440"))
ALLOWED_ROLES = {"employee", "manager"}


class Identity(BaseModel):
    """Immutable caller identity extracted strictly from verified JWT claims."""
    model_config = ConfigDict(frozen=True)

    company_id: str
    employee_id: str
    role: str


def issue_token(company_id: str, employee_id: str, role: str, expires_in_minutes: int | None = None) -> str:
    """Issues a signed JWT token with identity claims and expiration time."""
    if role not in ALLOWED_ROLES:
        raise ValueError(f"Invalid role: {role}. Allowed roles: {ALLOWED_ROLES}")
    if not company_id or not employee_id:
        raise ValueError("company_id and employee_id must be non-empty strings")

    duration = expires_in_minutes if expires_in_minutes is not None else JWT_EXPIRATION_MINUTES
    now = datetime.now(timezone.utc)
    payload = {
        "company_id": company_id,
        "employee_id": employee_id,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=duration)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> Identity:
    """Decodes and validates a JWT token using HS256, returning an immutable Identity."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except Exception:
        raise ValueError("Invalid, malformed, or expired token")

    company_id = payload.get("company_id")
    employee_id = payload.get("employee_id")
    role = payload.get("role")

    if not company_id or not employee_id or not role:
        raise ValueError("Token missing required identity claims")
    if role not in ALLOWED_ROLES:
        raise ValueError(f"Invalid role claim: {role}")

    return Identity(company_id=company_id, employee_id=employee_id, role=role)


def get_identity(authorization: Annotated[str | None, Header()] = None) -> Identity:
    """FastAPI dependency extracting and verifying Identity from the Authorization header."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing authentication token",
        )
    token = authorization.removeprefix("Bearer ").strip()
    try:
        return decode_token(token)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing authentication token",
        )


def require_employee(identity: Annotated[Identity, Depends(get_identity)]) -> Identity:
    """FastAPI dependency enforcing that the authenticated caller has the employee role."""
    if identity.role != "employee":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: employee role required",
        )
    return identity


def require_manager(identity: Annotated[Identity, Depends(get_identity)]) -> Identity:
    """FastAPI dependency enforcing that the authenticated caller has the manager role."""
    if identity.role != "manager":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: manager role required",
        )
    return identity
