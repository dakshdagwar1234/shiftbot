"""Why this exists:
LangGraph agent defining the workflow for shift-change requests and conversation checkpointer.
Enforces that identity comes strictly from JWT config, while the LLM only performs parameter parsing.
"""
from datetime import timedelta
import hashlib
import os
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg_pool import ConnectionPool

from app.auth import Identity
from app.clock import now as clock_now
from app.llm import (
    LLMBudget,
    LLMBudgetExceeded,
    LLMClient,
    LLMError,
    LLMParseError,
    LLMTimeout,
    LLMUnavailable,
    get_llm,
    parse_message,
)
import app.messages as messages
from app.tools import (
    CabFull,
    NotFound,
    RuleViolation,
    ToolFailure,
    ToolTimeout,
    apply_shift_change,
    check_cab_options,
    create_pending_request,
    get_latest_request,
    get_roster,
    run_tool,
)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://shift:shift_secret@localhost:5432/shift_db")

_pool: ConnectionPool | None = None
_checkpointer: PostgresSaver | None = None


def get_checkpointer(db_url: str | None = None) -> PostgresSaver:
    """Returns a singleton PostgresSaver instance configured with connection pooling."""
    global _pool, _checkpointer
    if _checkpointer is None:
        url = db_url or DATABASE_URL
        _pool = ConnectionPool(conninfo=url, max_size=10, kwargs={"autocommit": True})
        _checkpointer = PostgresSaver(_pool)
        _checkpointer.setup()
    return _checkpointer


# ---------------------------------------------------------------------------
# GRAPH STATE (Contains NO identity or cross-tenant data)
# ---------------------------------------------------------------------------

class AgentState(TypedDict, total=False):
    message: str
    reply: str
    awaiting: Literal["time", "which_trip", "confirm_offer"] | None
    pending_roster_id: int | str | None
    pending_date: str | None
    pending_trip_type: str | None
    pending_new_time: str | None
    offered: dict[str, Any] | None
    parsed: dict[str, Any] | None
    next_action: str | None


# ---------------------------------------------------------------------------
# GRAPH NODES
# ---------------------------------------------------------------------------

def parse_node(state: AgentState, config: dict[str, Any], llm: LLMClient) -> dict[str, Any]:
    """Node 1: Parses employee message into structured parameters using the LLM."""
    budget: LLMBudget = config.get("configurable", {}).get("budget") or LLMBudget()
    context = {
        "today": clock_now().date().isoformat(),
        "awaiting": state.get("awaiting"),
        "offered_slot": state.get("offered", {}).get("slot_time") if state.get("offered") else None,
        "pending_new_time": state.get("pending_new_time"),
    }

    try:
        parsed = parse_message(llm, budget, state.get("message", ""), context=context)
        return {"parsed": parsed.model_dump(), "reply": ""}
    except (LLMTimeout, LLMUnavailable):
        return {"reply": messages.llm_unavailable(), "parsed": None}
    except LLMParseError:
        return {"reply": messages.could_not_understand(), "parsed": None}
    except LLMBudgetExceeded:
        return {"reply": messages.generic_failure(), "parsed": None}
    except Exception:
        return {"reply": messages.generic_failure(), "parsed": None}


def route_node(state: AgentState, config: dict[str, Any]) -> dict[str, Any]:
    """Node 2: Evaluates intent and conversational state to decide next action."""
    identity: Identity = config["configurable"]["identity"]
    thread_id: str = config["configurable"]["thread_id"]
    parsed = state.get("parsed") or {}

    intent = parsed.get("intent", "other")
    new_time = parsed.get("new_time")
    date_str = parsed.get("date")

    # Follow-up input when awaiting time
    if state.get("awaiting") == "time" and new_time:
        roster_id = state.get("pending_roster_id")
        if not roster_id:
            target_date = clock_now().date()
            roster = run_tool(get_roster, identity, target_date)
            if roster:
                roster_id = roster["roster_id"]
        return {
            "pending_roster_id": roster_id,
            "pending_new_time": new_time,
            "awaiting": None,
            "next_action": "check_and_apply",
        }

    # Intent: change_time
    if intent == "change_time":
        target_date = clock_now().date() + timedelta(days=1) if date_str == "tomorrow" else clock_now().date()
        try:
            roster = run_tool(get_roster, identity, target_date)
        except Exception:
            return {"reply": messages.generic_failure(), "next_action": "end"}

        if not roster:
            return {
                "reply": "I couldn't find a scheduled trip for that day. Please verify your roster with the transport desk.",
                "awaiting": None,
                "next_action": "end",
            }

        if not new_time and not state.get("pending_new_time"):
            return {
                "awaiting": "time",
                "pending_roster_id": roster["roster_id"],
                "pending_date": target_date.isoformat(),
                "pending_trip_type": roster["trip_type"],
                "reply": messages.ask_for_time_clarification(),
                "next_action": "end",
            }

        target_time = new_time or state.get("pending_new_time")
        return {
            "pending_roster_id": roster["roster_id"],
            "pending_date": target_date.isoformat(),
            "pending_trip_type": roster["trip_type"],
            "pending_new_time": target_time,
            "awaiting": None,
            "next_action": "check_and_apply",
        }

    # Intent: accept_option
    if intent == "accept_option":
        if state.get("offered"):
            return {"next_action": "check_and_apply"}
        return {
            "reply": "There is no pending alternative offer to accept. What shift time would you like to request?",
            "awaiting": None,
            "next_action": "end",
        }

    # Intent: decline
    if intent == "decline":
        return {
            "reply": "Understood. Your original trip booking remains unchanged. Let me know if you need anything else!",
            "awaiting": None,
            "offered": None,
            "pending_roster_id": None,
            "pending_new_time": None,
            "next_action": "end",
        }

    # Intent: insist (manager approval escalation)
    if intent == "insist":
        roster_id = state.get("pending_roster_id")
        req_time = state.get("pending_new_time")
        if roster_id and req_time:
            key = f"insist_{thread_id}_{roster_id}"
            try:
                run_tool(
                    create_pending_request,
                    identity,
                    roster_id,
                    req_time,
                    "Employee requested manager approval",
                    thread_id,
                    key,
                )
            except Exception:
                return {"reply": messages.generic_failure(), "next_action": "end"}

            # TODO (Step 7): interrupt() for manager approval workflow
            return {
                "reply": messages.request_pending(),
                "awaiting": None,
                "next_action": "end",
            }
        return {
            "reply": "There is no pending request to submit for manager approval.",
            "awaiting": None,
            "next_action": "end",
        }

    # Intent: status
    if intent == "status":
        try:
            latest = run_tool(get_latest_request, identity)
        except Exception:
            return {"reply": messages.generic_failure(), "next_action": "end"}

        if not latest:
            reply = "You do not have any shift change requests on file."
        elif latest["status"] == "PENDING_APPROVAL":
            reply = messages.request_pending()
        elif latest["status"] == "APPROVED":
            reply = messages.request_approved(str(latest["requested_slot_time"]))
        elif latest["status"] == "REJECTED":
            reply = messages.request_rejected(latest.get("reason"))
        elif latest["status"] == "APPLIED":
            reply = messages.change_applied(str(latest["requested_slot_time"]))
        else:
            reply = f"Your latest request status is {latest['status']}."

        return {"reply": reply, "next_action": "end"}

    # Intent: other
    return {
        "reply": "I can help you change your shift cab booking or check request status. For example, you can say 'move my drop to 9 PM' or 'what is the status of my request?'.",
        "next_action": "end",
    }


def check_and_apply_node(state: AgentState, config: dict[str, Any]) -> dict[str, Any]:
    """Node 3: Evaluates seat availability, executes atomic seat taking, or offers alternatives."""
    identity: Identity = config["configurable"]["identity"]
    thread_id: str = config["configurable"]["thread_id"]

    # 1. Handling accepted offer
    if state.get("offered") and state.get("parsed", {}).get("intent") == "accept_option":
        offered = state["offered"]
        target_cab_id = offered["cab_id"]
        roster_id = offered["roster_id"]
        idem_key = hashlib.sha256(f"{thread_id}:{roster_id}:{target_cab_id}".encode()).hexdigest()[:32]

        try:
            run_tool(apply_shift_change, identity, roster_id, target_cab_id, idem_key)
            return {
                "reply": messages.change_applied(offered["slot_time"]),
                "awaiting": None,
                "offered": None,
                "pending_roster_id": None,
                "pending_new_time": None,
            }
        except CabFull:
            # Cab filled while deciding!
            try:
                options = run_tool(check_cab_options, identity, roster_id, offered["slot_time"])
            except Exception:
                return {"reply": messages.generic_failure(), "awaiting": None, "offered": None}

            if options.get("nearest_cab"):
                next_cab = options["nearest_cab"]
                new_offer = {"cab_id": next_cab["cab_id"], "slot_time": str(next_cab["slot_time"]), "roster_id": roster_id}
                msg = f"The cab at {offered['slot_time']} was just taken. The nearest available cab is at {next_cab['slot_time']}. Would you like to switch to that slot?"
                return {"reply": msg, "awaiting": "confirm_offer", "offered": new_offer}
            return {"reply": messages.cab_full(offered["slot_time"]), "awaiting": None, "offered": None}
        except Exception:
            return {"reply": messages.generic_failure(), "awaiting": None, "offered": None}

    # 2. Standard shift change request
    roster_id = state.get("pending_roster_id")
    req_time = state.get("pending_new_time")

    try:
        options = run_tool(check_cab_options, identity, roster_id, req_time)
    except (NotFound, RuleViolation, ToolTimeout, ToolFailure) as err:
        return {"reply": messages.generic_failure(), "awaiting": None}
    except Exception:
        return {"reply": messages.generic_failure(), "awaiting": None}

    # Cutoff violated: do not apply, offer escalation
    if not options["cutoff"].ok:
        nearest_slot_str = str(options["nearest_cab"]["slot_time"]) if options.get("nearest_cab") else None
        return {
            "reply": messages.rule_rejected(options["cutoff"].reason, nearest_time=nearest_slot_str),
            "awaiting": "confirm_offer",
        }

    # Requested slot is free: attempt atomic booking
    if options.get("requested_cab"):
        target_cab = options["requested_cab"]
        idem_key = hashlib.sha256(f"{thread_id}:{roster_id}:{target_cab['cab_id']}".encode()).hexdigest()[:32]
        try:
            run_tool(apply_shift_change, identity, roster_id, target_cab["cab_id"], idem_key)
            return {
                "reply": messages.change_applied(str(target_cab["slot_time"])),
                "awaiting": None,
                "offered": None,
                "pending_roster_id": None,
                "pending_new_time": None,
            }
        except CabFull:
            # Filled concurrently at apply time
            if options.get("nearest_cab"):
                next_cab = options["nearest_cab"]
                offer = {"cab_id": next_cab["cab_id"], "slot_time": str(next_cab["slot_time"]), "roster_id": roster_id}
                msg = f"The cab at {req_time} was just taken. The nearest available cab is at {next_cab['slot_time']}. Would you like to switch to that slot?"
                return {"reply": msg, "awaiting": "confirm_offer", "offered": offer}
            return {"reply": messages.cab_full(req_time), "awaiting": None, "offered": None}
        except Exception:
            return {"reply": messages.generic_failure(), "awaiting": None}

    # Requested slot is full: offer nearest free slot
    if options.get("nearest_cab"):
        next_cab = options["nearest_cab"]
        offer = {"cab_id": next_cab["cab_id"], "slot_time": str(next_cab["slot_time"]), "roster_id": roster_id}
        return {
            "reply": messages.cab_full(req_time, next_available_time=str(next_cab["slot_time"])),
            "awaiting": "confirm_offer",
            "offered": offer,
        }

    return {"reply": messages.cab_full(req_time), "awaiting": None, "offered": None}


# ---------------------------------------------------------------------------
# GRAPH COMPILATION
# ---------------------------------------------------------------------------

def build_graph(
    llm: LLMClient | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
):
    """Assembles and compiles the StateGraph workflow."""
    active_llm = llm if llm is not None else get_llm()
    builder = StateGraph(AgentState)

    # Bind node functions
    builder.add_node("parse", lambda state, config: parse_node(state, config, active_llm))
    builder.add_node("route", route_node)
    builder.add_node("check_and_apply", check_and_apply_node)

    # Wire edges
    builder.add_edge(START, "parse")

    def parse_condition(state: AgentState) -> str:
        if state.get("reply") and not state.get("parsed"):
            return END
        return "route"

    builder.add_conditional_edges("parse", parse_condition, {"route": "route", END: END})

    def route_condition(state: AgentState) -> str:
        if state.get("next_action") == "check_and_apply":
            return "check_and_apply"
        return END

    builder.add_conditional_edges("route", route_condition, {"check_and_apply": "check_and_apply", END: END})
    builder.add_edge("check_and_apply", END)

    saver = checkpointer if checkpointer is not None else get_checkpointer()
    return builder.compile(checkpointer=saver)
