"""Why this exists:
The HTTP entry point for the Shift-Change Request Agent.
Provides service health, identity endpoints, and the POST /chat agent interaction endpoint.
"""
from contextlib import asynccontextmanager
import logging
import os
import re
import sys
from typing import Annotated, Literal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,
)

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from langgraph.types import Command
import psycopg
from pydantic import BaseModel

from app.auth import Identity, get_identity, require_employee, require_manager
from app.db import init_db
from app.graph import build_graph, get_checkpointer
from app.llm import LLMBudget, get_llm
import app.messages as messages
from app.tools import (
    AlreadyDecided,
    NotFound,
    decide_request,
    get_latest_request,
    list_pending_requests,
    run_tool,
)

logger = logging.getLogger(__name__)
CONVERSATION_ID_REGEX = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://shift:shift_secret@localhost:5432/shift_db")


class ChatRequest(BaseModel):
    conversation_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str
    awaiting: str | None = None


class ManagerDecisionRequest(BaseModel):
    request_id: int
    decision: Literal["approve", "reject"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initializes tables, seeds, and LangGraph checkpointer on startup."""
    init_db()
    # Initialize checkpointer tables
    try:
        get_checkpointer()
    except Exception as exc:
        logger.warning("Checkpointer init warning: %s", exc)
    yield


app = FastAPI(title="Shift-Change Agent", lifespan=lifespan)


# ---------------------------------------------------------------------------
# GLOBAL EXCEPTION HANDLER (Zero stack trace leaks)
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled server exception processing %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"reply": messages.generic_failure(), "awaiting": None},
    )


# ---------------------------------------------------------------------------
# GRAPH INJECTION FACTORY (Overridable by tests)
# ---------------------------------------------------------------------------

def get_agent_graph():
    """Returns the production agent graph compiled with default LLM and Postgres checkpointer."""
    if not hasattr(app.state, "graph") or app.state.graph is None:
        app.state.graph = build_graph()
    return app.state.graph


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------

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


@app.get("/whoami")
def whoami(identity: Annotated[Identity, Depends(get_identity)]):
    """Temporary verification endpoint returning authenticated caller identity."""
    return {
        "company_id": identity.company_id,
        "employee_id": identity.employee_id,
        "role": identity.role,
    }


@app.post("/chat", response_model=ChatResponse)
def chat(
    request: ChatRequest,
    identity: Annotated[Identity, Depends(require_employee)],
    response: Response,
    agent=Depends(get_agent_graph),
):
    """Conversational endpoint for employee shift change requests."""
    # Validate conversation_id
    if not CONVERSATION_ID_REGEX.match(request.conversation_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid conversation_id. Must be 1-64 alphanumeric, dash, or underscore characters.",
        )

    # Validate message length
    cleaned_message = request.message.strip()
    if not cleaned_message or len(cleaned_message) > 500:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Message must be between 1 and 500 characters.",
        )

    # Server-derived thread_id: company_id:employee_id:conversation_id
    thread_id = f"{identity.company_id}:{identity.employee_id}:{request.conversation_id}"
    budget = LLMBudget()
    config = {
        "configurable": {
            "thread_id": thread_id,
            "identity": identity,
            "budget": budget,
        }
    }

    # Before invoking graph: if thread is paused waiting for manager, return pending template with 0 LLM calls
    try:
        thread_state = agent.get_state(config)
        if thread_state and thread_state.next:
            response.headers["X-LLM-Calls"] = "0"
            return ChatResponse(
                reply=messages.request_pending(),
                awaiting=None,
            )
    except Exception as exc:
        logger.warning("Error inspecting thread state for %s: %s", thread_id, exc)

    result = agent.invoke({"message": cleaned_message}, config=config)
    response.headers["X-LLM-Calls"] = str(budget.calls)
    return ChatResponse(
        reply=result.get("reply", messages.generic_failure()),
        awaiting=result.get("awaiting"),
    )


@app.get("/chat/status")
def chat_status(
    conversation_id: str,
    identity: Annotated[Identity, Depends(require_employee)],
    agent=Depends(get_agent_graph),
):
    """Returns thread status, latest reply, and awaiting state for authenticated employee."""
    if not CONVERSATION_ID_REGEX.match(conversation_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid conversation_id.",
        )

    thread_id = f"{identity.company_id}:{identity.employee_id}:{conversation_id}"
    config = {"configurable": {"thread_id": thread_id, "identity": identity}}

    try:
        thread_state = agent.get_state(config)
        if thread_state and thread_state.values:
            vals = thread_state.values
            return {
                "status": "PAUSED" if thread_state.next else "COMPLETED",
                "reply": vals.get("reply"),
                "awaiting": vals.get("awaiting"),
            }
    except Exception as exc:
        logger.warning("Error fetching thread state for %s: %s", thread_id, exc)

    # Fallback to latest request in database
    latest = run_tool(get_latest_request, identity)
    if latest:
        return {
            "status": latest["status"],
            "reply": f"Your latest request status is {latest['status']}.",
            "awaiting": None,
        }

    return {
        "status": "UNKNOWN",
        "reply": "No conversation found.",
        "awaiting": None,
    }


@app.get("/manager/requests")
def get_manager_requests(
    identity: Annotated[Identity, Depends(require_manager)],
):
    """Returns all pending shift change requests for the authenticated manager's company."""
    requests = run_tool(list_pending_requests, identity)
    return {"requests": requests}


@app.post("/manager/decision")
def post_manager_decision(
    body: ManagerDecisionRequest,
    identity: Annotated[Identity, Depends(require_manager)],
    agent=Depends(get_agent_graph),
):
    """Claims and applies or rejects an employee shift change request, resuming paused agent."""
    # 1. Claim row atomically
    try:
        claimed_row = run_tool(decide_request, identity, body.request_id, body.decision)
    except NotFound:
        # Cross-company or non-existent request: return identical 404
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shift request not found.",
        )
    except AlreadyDecided:
        # Already decided: return 409 Conflict, graph is NOT resumed twice
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Shift request has already been decided.",
        )

    thread_id = claimed_row["thread_id"]
    # Rebuild employee identity server-side from request row
    emp_identity = Identity(
        company_id=claimed_row["company_id"],
        employee_id=claimed_row["employee_id"],
        role="employee",
    )
    config = {
        "configurable": {
            "thread_id": thread_id,
            "identity": emp_identity,
            "budget": LLMBudget(),
        }
    }

    # 2. Resume the paused graph
    try:
        agent.invoke(
            Command(resume={"decision": body.decision, "manager_id": identity.employee_id}),
            config=config,
        )
    except Exception as exc:
        # Unexpected resume failure: put row back to PENDING_APPROVAL and log real error
        logger.exception("Unexpected error resuming thread %s for request %s: %s", thread_id, body.request_id, exc)
        try:
            with psycopg.connect(DATABASE_URL) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE shift_requests
                           SET status = 'PENDING_APPROVAL', decided_by = NULL, decided_at = NULL
                           WHERE request_id = %s AND company_id = %s""",
                        (body.request_id, identity.company_id),
                    )
                conn.commit()
        except Exception as rollback_exc:
            logger.exception("Failed resetting request status after resume crash: %s", rollback_exc)

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed processing manager decision. Please try again.",
        )

    return {
        "status": "ok",
        "message": f"Request {body.request_id} has been {body.decision}d.",
    }
