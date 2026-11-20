"""Judging rule:
A case passes only if BOTH the reply/awaiting state AND the database state match the expectation.
Reply text is matched on key facts (time, reason, next step), not exact strings.

Why this exists:
Evaluation runner executing deterministic scenarios through the real FastAPI application.
Outputs structured evaluation results to eval/cases.jsonl and renders summary table.
"""
import argparse
from datetime import date
import json
import logging
import os
import sys
import warnings

warnings.filterwarnings("ignore")
warnings.simplefilter("ignore")
try:
    from langchain_core._api.deprecation import LangChainPendingDeprecationWarning
    warnings.filterwarnings("ignore", category=LangChainPendingDeprecationWarning)
except ImportError:
    pass

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import time
from typing import Any
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool

from app.auth import issue_token
from app.db import ensure_service_day, ensure_test_db, get_connection, init_db
from app.graph import build_graph
from app.llm import FakeLLM, LLMBudget, LLMTimeout, get_llm, parse_message
from app.main import app, get_agent_graph
from app.tools import ToolTimeout
from eval.scenarios import SCENARIOS, Scenario

TEST_DB_URL = os.getenv("TEST_DATABASE_URL", "postgresql://shift:shift_secret@localhost:5432/shift_test_db")
if "db:5432" not in TEST_DB_URL and os.path.exists("/.dockerenv"):
    TEST_DB_URL = "postgresql://shift:shift_secret@db:5432/shift_test_db"

client = TestClient(app)

_test_db_ensured = False


def reset_database(test_date: date = date(2026, 11, 20)) -> None:
    """Drops and re-seeds shift_test_db for deterministic scenario execution."""
    global _test_db_ensured
    if not _test_db_ensured:
        ensure_test_db()
        _test_db_ensured = True

    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DROP TABLE IF EXISTS checkpoints, checkpoint_blobs, checkpoint_writes, checkpoint_migrations, "
                "audit_log, shift_requests, roster, cabs, employees, companies CASCADE"
            )
        conn.commit()

    init_db(TEST_DB_URL)
    ensure_service_day(test_date, db_url=TEST_DB_URL)


def check_db_state(
    company_id: str,
    employee_id: str,
    expected_roster_cab: str | None,
    expected_seats: dict[str, int],
    expected_request_status: str | None,
) -> tuple[bool, str]:
    """Verifies database invariants: roster assignment, seat counts, and shift request status."""
    mismatches = []
    verified_facts = []
    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            if expected_roster_cab:
                cur.execute(
                    "SELECT cab_id FROM roster WHERE company_id = %s AND employee_id = %s",
                    (company_id, employee_id),
                )
                row = cur.fetchone()
                actual_cab = row["cab_id"] if row else None
                if actual_cab != expected_roster_cab:
                    mismatches.append(f"roster cab expected {expected_roster_cab}, got {actual_cab}")
                else:
                    verified_facts.append(f"roster={actual_cab}")

            for cab_id, exp_seats in expected_seats.items():
                cur.execute("SELECT seats_taken FROM cabs WHERE cab_id = %s", (cab_id,))
                row = cur.fetchone()
                act_seats = row["seats_taken"] if row else None
                if act_seats != exp_seats:
                    mismatches.append(f"cab {cab_id} seats expected {exp_seats}, got {act_seats}")
                else:
                    verified_facts.append(f"seats {cab_id}={act_seats}")

            if expected_request_status is not None:
                cur.execute(
                    "SELECT status FROM shift_requests WHERE company_id = %s AND employee_id = %s ORDER BY request_id DESC LIMIT 1",
                    (company_id, employee_id),
                )
                row = cur.fetchone()
                act_status = row["status"] if row else "NONE"
                if act_status != expected_request_status:
                    mismatches.append(f"request status expected {expected_request_status}, got {act_status}")
                else:
                    verified_facts.append(f"status={act_status}")

    if mismatches:
        return False, "; ".join(mismatches)
    fact_str = "; ".join(verified_facts) if verified_facts else "DB state untouched"
    return True, fact_str


def run_scenario(scenario: Scenario) -> dict[str, Any]:
    """Runs a single evaluation scenario in-process and returns structured evaluation record."""
    os.environ["FIXED_NOW"] = scenario.fixed_now
    os.environ["DATABASE_URL"] = TEST_DB_URL
    reset_database()
    app.dependency_overrides.clear()

    conv_id = f"eval_{scenario.id}_{uuid.uuid4().hex[:6]}"
    token = issue_token(scenario.company_id, scenario.employee_id, "employee")
    headers = {"Authorization": f"Bearer {token}"}

    # Special handling for restart scenarios using real PostgresSaver
    if scenario.id in ("restart_01", "restart_02"):
        pool_a = ConnectionPool(conninfo=TEST_DB_URL, max_size=5, open=True, kwargs={"autocommit": True})
        saver_a = PostgresSaver(pool_a)
        saver_a.setup()
        fake_llm = FakeLLM(responses=list(scenario.fake_responses))
        graph_a = build_graph(llm=fake_llm, checkpointer=saver_a)
        app.dependency_overrides[get_agent_graph] = lambda: graph_a

        # Send messages to reach paused state
        r1 = client.post("/chat", headers=headers, json={"conversation_id": conv_id, "message": scenario.messages[0]})
        r2 = client.post("/chat", headers=headers, json={"conversation_id": conv_id, "message": scenario.messages[1]})
        last_reply = r2.json().get("reply", "")
        last_awaiting = r2.json().get("awaiting")

        # Discard Graph A
        del graph_a
        del saver_a
        pool_a.close()

        # Build Graph B simulating restart
        pool_b = ConnectionPool(conninfo=TEST_DB_URL, max_size=5, open=True, kwargs={"autocommit": True})
        saver_b = PostgresSaver(pool_b)
        saver_b.setup()
        graph_b = build_graph(llm=fake_llm, checkpointer=saver_b)
        app.dependency_overrides[get_agent_graph] = lambda: graph_b

        # Manager decision
        mgr_token = issue_token(scenario.company_id, "mgr_alpha", "manager")
        decision = "approve" if scenario.id == "restart_01" else "reject"
        client.post(
            "/manager/decision",
            headers={"Authorization": f"Bearer {mgr_token}"},
            json={"request_id": 1, "decision": decision},
        )
        pool_b.close()

    elif scenario.id == "iso_04":
        # Comp Beta manager decides Alpha request
        alpha_emp_token = issue_token("comp_alpha", "emp_005", "employee")
        fake_llm = FakeLLM(responses=[
            '{"intent": "change_time", "new_time": "21:00", "trip_type": "drop", "date": "today"}',
            '{"intent": "insist", "new_time": null, "trip_type": null, "date": null}',
        ])
        graph = build_graph(llm=fake_llm, checkpointer=MemorySaver())
        app.dependency_overrides[get_agent_graph] = lambda: graph

        client.post("/chat", headers={"Authorization": f"Bearer {alpha_emp_token}"}, json={"conversation_id": "conv_iso_m", "message": "move to 21:00"})
        client.post("/chat", headers={"Authorization": f"Bearer {alpha_emp_token}"}, json={"conversation_id": "conv_iso_m", "message": "ask manager"})

        beta_mgr_token = issue_token("comp_beta", "mgr_beta", "manager")
        r_dec = client.post("/manager/decision", headers={"Authorization": f"Bearer {beta_mgr_token}"}, json={"request_id": 1, "decision": "approve"})
        last_reply = f"status_{r_dec.status_code}_isolated"
        last_awaiting = None

    elif scenario.id == "tool_fail_cab_filled_meanwhile":
        fake_llm = FakeLLM(responses=list(scenario.fake_responses))
        graph = build_graph(llm=fake_llm, checkpointer=MemorySaver())
        app.dependency_overrides[get_agent_graph] = lambda: graph

        client.post("/chat", headers=headers, json={"conversation_id": conv_id, "message": scenario.messages[0]})
        client.post("/chat", headers=headers, json={"conversation_id": conv_id, "message": scenario.messages[1]})

        # Fill target cab completely before manager decision
        with get_connection(TEST_DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE cabs SET seats_taken = capacity WHERE cab_id = 'cab_a_20261120_2100'")
            conn.commit()

        mgr_token = issue_token(scenario.company_id, "mgr_alpha", "manager")
        client.post("/manager/decision", headers={"Authorization": f"Bearer {mgr_token}"}, json={"request_id": 1, "decision": "approve"})

        # Check employee status
        r_stat = client.get("/chat/status", headers=headers, params={"conversation_id": conv_id})
        last_reply = r_stat.json().get("reply", "")
        last_awaiting = r_stat.json().get("awaiting")

    elif scenario.id == "concur_02":
        fake_llm = FakeLLM(responses=['{"intent": "change_time", "new_time": "19:00", "trip_type": "drop", "date": "today"}'])
        graph = build_graph(llm=fake_llm, checkpointer=MemorySaver())
        app.dependency_overrides[get_agent_graph] = lambda: graph

        # emp_001 takes 4th seat
        r1 = client.post("/chat", headers=headers, json={"conversation_id": conv_id, "message": "move to 19:00"})
        last_reply = r1.json().get("reply", "")
        last_awaiting = r1.json().get("awaiting")

        # emp_002 attempts to take seat in now-full 19:00 cab
        token2 = issue_token("comp_alpha", "emp_002", "employee")
        fake_llm2 = FakeLLM(responses=['{"intent": "change_time", "new_time": "19:00", "trip_type": "drop", "date": "today"}'])
        graph2 = build_graph(llm=fake_llm2, checkpointer=MemorySaver())
        app.dependency_overrides[get_agent_graph] = lambda: graph2
        r2 = client.post("/chat", headers={"Authorization": f"Bearer {token2}"}, json={"conversation_id": f"{conv_id}_2", "message": "move to 19:00"})
        if "full" in r2.json().get("reply", "").lower():
            last_reply += " | second_user_cab_full"

    elif scenario.id == "tool_fail_timeout":
        fake_llm = FakeLLM(responses=list(scenario.fake_responses))
        graph = build_graph(llm=fake_llm, checkpointer=MemorySaver())
        app.dependency_overrides[get_agent_graph] = lambda: graph
        with patch("app.graph.check_cab_options", side_effect=ToolTimeout("Simulated timeout")):
            resp = client.post("/chat", headers=headers, json={"conversation_id": conv_id, "message": scenario.messages[0]})
            last_reply = resp.json().get("reply", "")
            last_awaiting = resp.json().get("awaiting")

    elif scenario.id == "tool_fail_exception":
        fake_llm = FakeLLM(responses=list(scenario.fake_responses))
        graph = build_graph(llm=fake_llm, checkpointer=MemorySaver())
        app.dependency_overrides[get_agent_graph] = lambda: graph
        with patch("app.graph.check_cab_options", side_effect=RuntimeError("Simulated tool crash")):
            resp = client.post("/chat", headers=headers, json={"conversation_id": conv_id, "message": scenario.messages[0]})
            last_reply = resp.json().get("reply", "")
            last_awaiting = resp.json().get("awaiting")

    elif scenario.id == "tool_fail_llm_timeout":
        fake_llm = FakeLLM(responses=[LLMTimeout("Groq timeout")])
        graph = build_graph(llm=fake_llm, checkpointer=MemorySaver())
        app.dependency_overrides[get_agent_graph] = lambda: graph
        resp = client.post("/chat", headers=headers, json={"conversation_id": conv_id, "message": scenario.messages[0]})
        last_reply = resp.json().get("reply", "")
        last_awaiting = resp.json().get("awaiting")

    elif scenario.id == "tool_fail_llm_garbage_twice":
        fake_llm = FakeLLM(responses=["garbage 1", "garbage 2"])
        graph = build_graph(llm=fake_llm, checkpointer=MemorySaver())
        app.dependency_overrides[get_agent_graph] = lambda: graph
        resp = client.post("/chat", headers=headers, json={"conversation_id": conv_id, "message": scenario.messages[0]})
        last_reply = resp.json().get("reply", "")
        last_awaiting = resp.json().get("awaiting")

    else:
        # Standard sequential message execution
        fake_llm = FakeLLM(responses=list(scenario.fake_responses))
        graph = build_graph(llm=fake_llm, checkpointer=MemorySaver())
        app.dependency_overrides[get_agent_graph] = lambda: graph

        last_reply = ""
        last_awaiting = None
        for msg in scenario.messages:
            resp = client.post("/chat", headers=headers, json={"conversation_id": conv_id, "message": msg})
            data = resp.json()
            last_reply = data.get("reply", "")
            last_awaiting = data.get("awaiting")

    # Judging
    reply_ok = all(kw.lower() in last_reply.lower() for kw in scenario.expected_reply_keywords)
    awaiting_ok = (last_awaiting == scenario.expected_awaiting)
    db_ok, db_msg = check_db_state(
        company_id=scenario.company_id,
        employee_id=scenario.employee_id,
        expected_roster_cab=scenario.expected_roster_cab,
        expected_seats=scenario.expected_seats,
        expected_request_status=scenario.expected_request_status,
    )

    passed = reply_ok and awaiting_ok and db_ok
    if passed:
        kw_str = ", ".join(f"'{k}'" for k in scenario.expected_reply_keywords)
        notes = f"Reply verified ({kw_str}); awaiting={scenario.expected_awaiting}; {db_msg}"
    else:
        errs = []
        if not reply_ok:
            errs.append(f"Reply missing keywords: {scenario.expected_reply_keywords}")
        if not awaiting_ok:
            errs.append(f"Awaiting mismatch: expected {scenario.expected_awaiting}, got {last_awaiting}")
        if not db_ok:
            errs.append(db_msg)
        notes = "; ".join(errs)

    clean_reply = last_reply[:60].rstrip(".")
    actual_desc = f"{clean_reply}... (awaiting={last_awaiting})"
    if scenario.expected_roster_cab or scenario.expected_seats or scenario.expected_request_status:
        actual_desc += f" | {db_msg}"

    input_val = scenario.messages[0] if len(scenario.messages) == 1 else scenario.messages
    return {
        "id": scenario.id,
        "input": input_val,
        "expected": scenario.expected_summary,
        "actual": actual_desc,
        "pass": passed,
        "notes": notes,
        "category": scenario.category,
    }


def run_live_eval(scenarios: list[Scenario]) -> list[dict[str, Any]]:
    """Runs live evaluation using real LLM from .env on tool-selection and clarification inputs."""
    llm = get_llm()
    results = []

    print("\n--- Running optional live LLM evaluation (--live) ---")
    for s in scenarios:
        if s.category not in ("tool selection", "clarification") or s.id == "tool_sel_same_slot":
            continue

        target_message = s.messages[-1]
        ctx = None
        if s.id in ("clarif_02", "clarif_03"):
            ctx = {"awaiting": "time"}
        elif s.id in ("tool_sel_accept_option", "tool_sel_decline"):
            ctx = {"awaiting": "confirm_offer", "offered_slot": "19:00"}
        elif s.id == "tool_sel_insist":
            ctx = {"awaiting": "confirm_offer", "pending_new_time": "21:00"}

        print(f"Testing live LLM parse for [{s.id}]: '{target_message}'...")
        budget = LLMBudget()
        t0 = time.time()
        try:
            parsed = parse_message(llm, budget, target_message, context=ctx)
            duration_ms = int((time.time() - t0) * 1000)
            intent_match = (parsed.intent == s.expected_intent)
            time_match = (s.expected_time is None or parsed.new_time == s.expected_time)
            passed = intent_match and time_match

            notes = f"live parse latency: {duration_ms}ms; intent={parsed.intent}, time={parsed.new_time}"
            results.append({
                "id": f"{s.id}_live",
                "input": target_message,
                "expected": f"intent={s.expected_intent}, time={s.expected_time}",
                "actual": f"intent={parsed.intent}, time={parsed.new_time}",
                "pass": passed,
                "notes": notes,
                "category": f"{s.category} (live)",
            })
        except Exception as exc:
            duration_ms = int((time.time() - t0) * 1000)
            err_str = str(exc)
            is_429 = "429" in err_str or "rate limit" in err_str.lower()
            note_str = f"rate limited (HTTP 429)" if is_429 else f"live LLM error: {exc}"
            results.append({
                "id": f"{s.id}_live",
                "input": target_message,
                "expected": f"intent={s.expected_intent}, time={s.expected_time}",
                "actual": "ERROR",
                "pass": False,
                "notes": f"{note_str} (latency: {duration_ms}ms)",
                "category": f"{s.category} (live)",
            })

        # Respect free-tier limits with 2s pause
        time.sleep(2.0)

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Shift Change Evaluation Suite Runner")
    parser.add_argument("--live", action="store_true", help="Run live LLM evaluation on tool selection and clarification")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logs (restores INFO/WARNING/ERROR logging)")
    args = parser.parse_args()

    if not args.verbose:
        logging.disable(logging.CRITICAL)
        logging.getLogger().setLevel(logging.CRITICAL)
    else:
        logging.disable(logging.NOTSET)
        logging.getLogger().setLevel(logging.INFO)

    results = []
    for s in SCENARIOS:
        rec = run_scenario(s)
        results.append(rec)
        status_mark = "PASS" if rec["pass"] else "FAIL"
        print(f"[{status_mark}] {rec['id']:<30} ({rec['category']})")

    if args.live:
        live_results = run_live_eval(SCENARIOS)
        results.extend(live_results)

    # Write EXACT required schema to eval/cases.jsonl
    cases_file = os.path.join(os.path.dirname(__file__), "cases.jsonl")
    with open(cases_file, "w", encoding="utf-8") as f:
        for r in results:
            clean_record = {
                "id": r["id"],
                "input": r["input"],
                "expected": r["expected"],
                "actual": r["actual"],
                "pass": r["pass"],
                "notes": r["notes"],
            }
            f.write(json.dumps(clean_record) + "\n")

    # Render summary table by category
    categories: dict[str, list[bool]] = {}
    for r in results:
        cat = r["category"]
        categories.setdefault(cat, []).append(r["pass"])

    print("\n" + "=" * 65)
    print(f"{'Category':<32} {'Total':<8} {'Passed':<8} {'Pass Rate'}")
    print("=" * 65)
    total_cases = len(results)
    total_passed = sum(1 for r in results if r["pass"])

    for cat, passes in categories.items():
        n = len(passes)
        p = sum(1 for v in passes if v)
        rate = (p / n) * 100
        print(f"{cat:<32} {n:<8} {p:<8} {rate:6.1f}%")

    print("-" * 65)
    overall_rate = (total_passed / total_cases) * 100
    print(f"{'OVERALL':<32} {total_cases:<8} {total_passed:<8} {overall_rate:6.1f}%\n")
    print(f"Results written to: {cases_file}")

    if total_passed < total_cases:
        sys.exit(1)


if __name__ == "__main__":
    main()
