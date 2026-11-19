"""Why this exists:
The HTTP entry point for the Shift-Change Request Agent.
Provides service health, identity endpoints, and the POST /chat agent interaction endpoint.
"""
from contextlib import asynccontextmanager
import logging
import os
import re
import sys
from typing import Annotated

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,
)

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
import psycopg
from pydantic import BaseModel

from app.auth import Identity, get_identity, require_employee
from app.db import init_db
from app.graph import build_graph, get_checkpointer
from app.llm import LLMBudget, get_llm
import app.messages as messages

logger = logging.getLogger(__name__)
CONVERSATION_ID_REGEX = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://shift:shift_secret@localhost:5432/shift_db")


class ChatRequest(BaseModel):
    conversation_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str
    awaiting: str | None = None


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
    config = {
        "configurable": {
            "thread_id": thread_id,
            "identity": identity,
            "budget": LLMBudget(),
        }
    }

    result = agent.invoke({"message": cleaned_message}, config=config)
    return ChatResponse(
        reply=result.get("reply", messages.generic_failure()),
        awaiting=result.get("awaiting"),
    )
