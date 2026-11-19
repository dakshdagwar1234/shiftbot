"""Why this exists:
The HTTP entry point for the Shift-Change Request Agent.
Provides service health verification and future routes for chat and manager decisions.
"""
import os
from fastapi import FastAPI, Response, status
import psycopg

app = FastAPI(title="Shift-Change Agent")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://shift:shift_secret@localhost:5432/shift_db")


@app.get("/health")
def health(response: Response):
    """Checks service health and confirms database connectivity."""
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return {"status": "ok", "db": "ok"}
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "error", "db": "unreachable"}
