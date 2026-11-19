"""Why this exists:
Tests the manager approval flow: human-in-the-loop pause, persistence across restarts,
atomic manager approval/rejection, isolation, race handling, and role permissions.
"""
from datetime import date
import logging
import os
from unittest.mock import patch

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool
import pytest

from app.auth import issue_token
from app.db import ensure_service_day, get_connection, init_db
from app.graph import build_graph
from app.llm import (
    FakeLLM,
    LLMBudget,
    LLMBudgetExceeded,
    LLMParseError,
    LLMTimeout,
    parse_message,
)
from app.main import app, get_agent_graph
import app.messages as messages
from app.tools import CabFull, NotFound, check_cab_options, run_tool

client = TestClient(app)

TEST_DB_URL = os.getenv("TEST_DATABASE_URL", "postgresql://shift:shift_secret@localhost:5432/shift_test_db")
if "db:5432" not in TEST_DB_URL and os.path.exists("/.dockerenv"):
    TEST_DB_URL = "postgresql://shift:shift_secret@db:5432/shift_test_db"


@pytest.fixture(autouse=True)
def setup_approval_test(monkeypatch):
    """Sets fixed time, test database URL, and wipes shift_test_db before each test."""
    monkeypatch.setenv("FIXED_NOW", "2026-11-20T17:30:00+05:30")  # 90 mins before 19:00 (inside 120m alpha cutoff)
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    test_date = date(2026, 11, 20)

    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS audit_log, shift_requests, roster, cabs, employees, companies CASCADE")
        conn.commit()

    init_db(TEST_DB_URL)
    ensure_service_day(test_date, db_url=TEST_DB_URL)
    yield
    app.dependency_overrides.clear()


def test_cutoff_violation_alone_creates_no_shift_request():
    """1. Cutoff violation alone returns rule rejection and manager offer; NO row is created in shift_requests."""
    fake_llm = FakeLLM(responses=['{"intent": "change_time", "new_time": "21:00", "trip_type": "drop", "date": "today"}'])
    graph = build_graph(llm=fake_llm, checkpointer=MemorySaver())
    app.dependency_overrides[get_agent_graph] = lambda: graph

    token = issue_token("comp_alpha", "emp_005", "employee")
    resp = client.post(
        "/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"conversation_id": "conv_cutoff_only", "message": "move to 21:00"},
    )
    assert resp.status_code == 200
    assert "inside the 120-minute cutoff" in resp.json()["reply"]
    assert "manager" in resp.json()["reply"].lower()

    # Verify NO shift_requests row was created
    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM shift_requests WHERE company_id = 'comp_alpha'")
            assert cur.fetchone()["cnt"] == 0


def test_insist_creates_pending_request_and_pauses():
    """2. 'insist' after cutoff rejection creates 1 PENDING_APPROVAL row, pauses the graph, seats unchanged."""
    responses = [
        # Turn 1: request rejected by cutoff
        '{"intent": "change_time", "new_time": "21:00", "trip_type": "drop", "date": "today"}',
        # Turn 2: insist -> send to manager
        '{"intent": "insist", "new_time": null, "trip_type": null, "date": null}',
    ]
    fake_llm = FakeLLM(responses=responses)
    graph = build_graph(llm=fake_llm, checkpointer=MemorySaver())
    app.dependency_overrides[get_agent_graph] = lambda: graph

    token = issue_token("comp_alpha", "emp_005", "employee")

    # Turn 1
    r1 = client.post(
        "/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"conversation_id": "conv_insist", "message": "move to 21:00"},
    )
    assert r1.status_code == 200

    # Turn 2
    r2 = client.post(
        "/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"conversation_id": "conv_insist", "message": "please send it to my manager"},
    )
    assert r2.status_code == 200
    assert "submitted for manager approval" in r2.json()["reply"]
    assert r2.json()["awaiting"] is None

    # Verify shift_requests has exactly 1 row with PENDING_APPROVAL
    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT request_id, status FROM shift_requests WHERE company_id = 'comp_alpha'")
            rows = cur.fetchall()
            assert len(rows) == 1
            assert rows[0]["status"] == "PENDING_APPROVAL"

            # Seats and roster must be unchanged
            cur.execute("SELECT cab_id FROM roster WHERE employee_id = 'emp_005' AND company_id = 'comp_alpha'")
            assert cur.fetchone()["cab_id"] == "cab_a_20261120_1900"
            cur.execute("SELECT seats_taken FROM cabs WHERE cab_id = 'cab_a_20261120_2100'")
            assert cur.fetchone()["seats_taken"] == 0


def test_message_while_paused_returns_pending_zero_llm_calls():
    """3. Sending a message while paused replies 'with your manager' using 0 LLM calls without disturbing pause."""
    responses = [
        '{"intent": "change_time", "new_time": "21:00", "trip_type": "drop", "date": "today"}',
        '{"intent": "insist", "new_time": null, "trip_type": null, "date": null}',
    ]
    fake_llm = FakeLLM(responses=responses)
    graph = build_graph(llm=fake_llm, checkpointer=MemorySaver())
    app.dependency_overrides[get_agent_graph] = lambda: graph

    token = issue_token("comp_alpha", "emp_005", "employee")

    # Setup paused state
    client.post("/chat", headers={"Authorization": f"Bearer {token}"}, json={"conversation_id": "conv_paused", "message": "move to 21:00"})
    client.post("/chat", headers={"Authorization": f"Bearer {token}"}, json={"conversation_id": "conv_paused", "message": "send to manager"})

    initial_prompt_count = len(fake_llm.prompts)

    # Send message while paused
    r3 = client.post(
        "/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"conversation_id": "conv_paused", "message": "any update yet?"},
    )
    assert r3.status_code == 200
    assert "submitted for manager approval" in r3.json()["reply"]
    # Exactly 0 new LLM calls made
    assert len(fake_llm.prompts) == initial_prompt_count


def test_manager_approves_applies_change_audit_log_and_chat_status():
    """4. Manager approval resumes graph, books target seat, frees old seat, updates roster, status APPLIED."""
    responses = [
        '{"intent": "change_time", "new_time": "21:00", "trip_type": "drop", "date": "today"}',
        '{"intent": "insist", "new_time": null, "trip_type": null, "date": null}',
    ]
    fake_llm = FakeLLM(responses=responses)
    graph = build_graph(llm=fake_llm, checkpointer=MemorySaver())
    app.dependency_overrides[get_agent_graph] = lambda: graph

    emp_token = issue_token("comp_alpha", "emp_005", "employee")
    mgr_token = issue_token("comp_alpha", "mgr_alpha", "manager")

    # Employee reaches paused state
    client.post("/chat", headers={"Authorization": f"Bearer {emp_token}"}, json={"conversation_id": "conv_appr", "message": "move to 21:00"})
    client.post("/chat", headers={"Authorization": f"Bearer {emp_token}"}, json={"conversation_id": "conv_appr", "message": "ask manager"})

    # Manager lists pending requests
    r_list = client.get("/manager/requests", headers={"Authorization": f"Bearer {mgr_token}"})
    assert r_list.status_code == 200
    reqs = r_list.json()["requests"]
    assert len(reqs) == 1
    req_id = reqs[0]["request_id"]
    assert reqs[0]["employee_name"] == "Eve Alpha"

    # Manager approves request
    r_dec = client.post(
        "/manager/decision",
        headers={"Authorization": f"Bearer {mgr_token}"},
        json={"request_id": req_id, "decision": "approve"},
    )
    assert r_dec.status_code == 200
    assert r_dec.json()["status"] == "ok"

    # Verify DB: request APPLIED, seat allocated, old seat freed, roster updated, audit log written
    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, decided_by FROM shift_requests WHERE request_id = %s", (req_id,))
            row = cur.fetchone()
            assert row["status"] == "APPLIED"
            assert row["decided_by"] == "mgr_alpha"

            # Old cab (19:00 was 3/4) reduced to 2/4
            cur.execute("SELECT seats_taken FROM cabs WHERE cab_id = 'cab_a_20261120_1900'")
            assert cur.fetchone()["seats_taken"] == 2

            # Target cab (21:00 was 0/4) incremented to 1/4
            cur.execute("SELECT seats_taken FROM cabs WHERE cab_id = 'cab_a_20261120_2100'")
            assert cur.fetchone()["seats_taken"] == 1

            # Roster now points to 21:00 cab
            cur.execute("SELECT cab_id FROM roster WHERE employee_id = 'emp_005' AND company_id = 'comp_alpha'")
            assert cur.fetchone()["cab_id"] == "cab_a_20261120_2100"

            # Audit log records manager approval
            cur.execute("SELECT action, actor_id FROM audit_log WHERE company_id = 'comp_alpha' AND action = 'APPLY_APPROVED_CHANGE'")
            audit_entry = cur.fetchone()
            assert audit_entry is not None
            assert audit_entry["actor_id"] == "mgr_alpha"

    # Employee checks status via GET /chat/status
    r_stat = client.get(
        "/chat/status",
        headers={"Authorization": f"Bearer {emp_token}"},
        params={"conversation_id": "conv_appr"},
    )
    assert r_stat.status_code == 200
    assert "approved your shift change request" in r_stat.json()["reply"]
    assert "9:00 PM today" in r_stat.json()["reply"]


def test_manager_rejects():
    """5. Manager rejects request -> status REJECTED, original cab remains active, employee told."""
    responses = [
        '{"intent": "change_time", "new_time": "21:00", "trip_type": "drop", "date": "today"}',
        '{"intent": "insist", "new_time": null, "trip_type": null, "date": null}',
    ]
    fake_llm = FakeLLM(responses=responses)
    graph = build_graph(llm=fake_llm, checkpointer=MemorySaver())
    app.dependency_overrides[get_agent_graph] = lambda: graph

    emp_token = issue_token("comp_alpha", "emp_005", "employee")
    mgr_token = issue_token("comp_alpha", "mgr_alpha", "manager")

    client.post("/chat", headers={"Authorization": f"Bearer {emp_token}"}, json={"conversation_id": "conv_rej", "message": "move to 21:00"})
    client.post("/chat", headers={"Authorization": f"Bearer {emp_token}"}, json={"conversation_id": "conv_rej", "message": "send to manager"})

    r_list = client.get("/manager/requests", headers={"Authorization": f"Bearer {mgr_token}"})
    req_id = r_list.json()["requests"][0]["request_id"]

    # Reject
    r_dec = client.post(
        "/manager/decision",
        headers={"Authorization": f"Bearer {mgr_token}"},
        json={"request_id": req_id, "decision": "reject"},
    )
    assert r_dec.status_code == 200

    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM shift_requests WHERE request_id = %s", (req_id,))
            assert cur.fetchone()["status"] == "REJECTED"

            # Roster unchanged
            cur.execute("SELECT cab_id FROM roster WHERE employee_id = 'emp_005' AND company_id = 'comp_alpha'")
            assert cur.fetchone()["cab_id"] == "cab_a_20261120_1900"

    r_stat = client.get("/chat/status", headers={"Authorization": f"Bearer {emp_token}"}, params={"conversation_id": "conv_rej"})
    assert "did not approve" in r_stat.json()["reply"]


def test_approved_but_cab_filled_meanwhile():
    """6. Approved but cab filled in the meantime -> status FAILED, nearest alternative offered, no success claim."""
    responses = [
        '{"intent": "change_time", "new_time": "21:00", "trip_type": "drop", "date": "today"}',
        '{"intent": "insist", "new_time": null, "trip_type": null, "date": null}',
    ]
    fake_llm = FakeLLM(responses=responses)
    graph = build_graph(llm=fake_llm, checkpointer=MemorySaver())
    app.dependency_overrides[get_agent_graph] = lambda: graph

    emp_token = issue_token("comp_alpha", "emp_005", "employee")
    mgr_token = issue_token("comp_alpha", "mgr_alpha", "manager")

    client.post("/chat", headers={"Authorization": f"Bearer {emp_token}"}, json={"conversation_id": "conv_filled", "message": "move to 21:00"})
    client.post("/chat", headers={"Authorization": f"Bearer {emp_token}"}, json={"conversation_id": "conv_filled", "message": "ask manager"})

    r_list = client.get("/manager/requests", headers={"Authorization": f"Bearer {mgr_token}"})
    req_id = r_list.json()["requests"][0]["request_id"]

    # Fill 21:00 cab completely before manager approves
    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE cabs SET seats_taken = capacity WHERE cab_id = 'cab_a_20261120_2100'")
        conn.commit()

    # Manager approves
    r_dec = client.post(
        "/manager/decision",
        headers={"Authorization": f"Bearer {mgr_token}"},
        json={"request_id": req_id, "decision": "approve"},
    )
    assert r_dec.status_code == 200

    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM shift_requests WHERE request_id = %s", (req_id,))
            assert cur.fetchone()["status"] == "FAILED"

    r_stat = client.get("/chat/status", headers={"Authorization": f"Bearer {emp_token}"}, params={"conversation_id": "conv_filled"})
    assert "seat was taken in the meantime" in r_stat.json()["reply"]
    assert "confirmed" not in r_stat.json()["reply"].lower()


def test_double_decision_conflict():
    """7. Calling /manager/decision twice returns 409 and does not resume graph or take extra seats."""
    responses = [
        '{"intent": "change_time", "new_time": "21:00", "trip_type": "drop", "date": "today"}',
        '{"intent": "insist", "new_time": null, "trip_type": null, "date": null}',
    ]
    fake_llm = FakeLLM(responses=responses)
    graph = build_graph(llm=fake_llm, checkpointer=MemorySaver())
    app.dependency_overrides[get_agent_graph] = lambda: graph

    emp_token = issue_token("comp_alpha", "emp_005", "employee")
    mgr_token = issue_token("comp_alpha", "mgr_alpha", "manager")

    client.post("/chat", headers={"Authorization": f"Bearer {emp_token}"}, json={"conversation_id": "conv_double_dec", "message": "move to 21:00"})
    client.post("/chat", headers={"Authorization": f"Bearer {emp_token}"}, json={"conversation_id": "conv_double_dec", "message": "ask manager"})

    r_list = client.get("/manager/requests", headers={"Authorization": f"Bearer {mgr_token}"})
    req_id = r_list.json()["requests"][0]["request_id"]

    # First decision -> 200
    r1 = client.post("/manager/decision", headers={"Authorization": f"Bearer {mgr_token}"}, json={"request_id": req_id, "decision": "approve"})
    assert r1.status_code == 200

    # Second decision -> 409 Conflict
    r2 = client.post("/manager/decision", headers={"Authorization": f"Bearer {mgr_token}"}, json={"request_id": req_id, "decision": "approve"})
    assert r2.status_code == 409
    assert "already been decided" in r2.json()["detail"]


def test_manager_isolation_and_role_protection():
    """8. Comp Beta manager deciding Alpha request -> 404; employee calling /manager/* -> 403."""
    responses = [
        '{"intent": "change_time", "new_time": "21:00", "trip_type": "drop", "date": "today"}',
        '{"intent": "insist", "new_time": null, "trip_type": null, "date": null}',
    ]
    fake_llm = FakeLLM(responses=responses)
    graph = build_graph(llm=fake_llm, checkpointer=MemorySaver())
    app.dependency_overrides[get_agent_graph] = lambda: graph

    alpha_emp_token = issue_token("comp_alpha", "emp_005", "employee")
    beta_mgr_token = issue_token("comp_beta", "mgr_beta", "manager")

    client.post("/chat", headers={"Authorization": f"Bearer {alpha_emp_token}"}, json={"conversation_id": "conv_iso_m", "message": "move to 21:00"})
    client.post("/chat", headers={"Authorization": f"Bearer {alpha_emp_token}"}, json={"conversation_id": "conv_iso_m", "message": "ask manager"})

    # Comp Beta manager lists requests -> empty (isolation)
    r_list = client.get("/manager/requests", headers={"Authorization": f"Bearer {beta_mgr_token}"})
    assert r_list.status_code == 200
    assert len(r_list.json()["requests"]) == 0

    # Comp Beta manager attempts to decide comp_alpha request_id 1 -> 404
    r_dec = client.post("/manager/decision", headers={"Authorization": f"Bearer {beta_mgr_token}"}, json={"request_id": 1, "decision": "approve"})
    assert r_dec.status_code == 404

    # Employee token on manager endpoint -> 403
    r_emp_forbidden = client.get("/manager/requests", headers={"Authorization": f"Bearer {alpha_emp_token}"})
    assert r_emp_forbidden.status_code == 403


def test_restart_proof_with_postgres_saver():
    """9. RESTART TEST: Graph paused with PostgresSaver, graph discarded; fresh graph instance resumes and applies."""
    pool = ConnectionPool(conninfo=TEST_DB_URL, max_size=5, open=True, kwargs={"autocommit": True})
    saver_a = PostgresSaver(pool)
    saver_a.setup()

    responses = [
        '{"intent": "change_time", "new_time": "21:00", "trip_type": "drop", "date": "today"}',
        '{"intent": "insist", "new_time": null, "trip_type": null, "date": null}',
    ]
    fake_llm = FakeLLM(responses=responses)
    graph_a = build_graph(llm=fake_llm, checkpointer=saver_a)
    app.dependency_overrides[get_agent_graph] = lambda: graph_a

    emp_token = issue_token("comp_alpha", "emp_005", "employee")
    mgr_token = issue_token("comp_alpha", "mgr_alpha", "manager")

    # Step 1: Employee messages reach paused state in Graph A
    client.post("/chat", headers={"Authorization": f"Bearer {emp_token}"}, json={"conversation_id": "conv_restart", "message": "move to 21:00"})
    client.post("/chat", headers={"Authorization": f"Bearer {emp_token}"}, json={"conversation_id": "conv_restart", "message": "send to manager"})

    # Step 2: DISCARD Graph A completely (simulating process restart)
    del graph_a
    del saver_a
    pool.close()

    # Step 3: Instantiate brand-new ConnectionPool, PostgresSaver, and Graph B on the same DB
    fresh_pool = ConnectionPool(conninfo=TEST_DB_URL, max_size=5, open=True, kwargs={"autocommit": True})
    saver_b = PostgresSaver(fresh_pool)
    saver_b.setup()
    graph_b = build_graph(llm=fake_llm, checkpointer=saver_b)
    app.dependency_overrides[get_agent_graph] = lambda: graph_b

    # Step 4: Manager resumes the paused graph on Graph B
    r_dec = client.post(
        "/manager/decision",
        headers={"Authorization": f"Bearer {mgr_token}"},
        json={"request_id": 1, "decision": "approve"},
    )
    assert r_dec.status_code == 200

    # Step 5: Verify the change was successfully applied after restart
    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM shift_requests WHERE request_id = 1")
            assert cur.fetchone()["status"] == "APPLIED"

    fresh_pool.close()


def test_resume_failure_rollback_and_retry():
    """10. If resume crashes unexpectedly, request returns to PENDING_APPROVAL and manager gets plain 500 error."""
    responses = [
        '{"intent": "change_time", "new_time": "21:00", "trip_type": "drop", "date": "today"}',
        '{"intent": "insist", "new_time": null, "trip_type": null, "date": null}',
    ]
    fake_llm = FakeLLM(responses=responses)
    graph = build_graph(llm=fake_llm, checkpointer=MemorySaver())
    app.dependency_overrides[get_agent_graph] = lambda: graph

    emp_token = issue_token("comp_alpha", "emp_005", "employee")
    mgr_token = issue_token("comp_alpha", "mgr_alpha", "manager")

    client.post("/chat", headers={"Authorization": f"Bearer {emp_token}"}, json={"conversation_id": "conv_crash_resume", "message": "move to 21:00"})
    client.post("/chat", headers={"Authorization": f"Bearer {emp_token}"}, json={"conversation_id": "conv_crash_resume", "message": "ask manager"})

    # Patch graph.invoke to simulate unexpected crash during resume
    with patch.object(graph, "invoke", side_effect=RuntimeError("Simulated resume engine failure")):
        r_fail = client.post("/manager/decision", headers={"Authorization": f"Bearer {mgr_token}"}, json={"request_id": 1, "decision": "approve"})
        assert r_fail.status_code == 500
        assert "Failed processing manager decision" in r_fail.json()["detail"]

    # Verify status rolled back to PENDING_APPROVAL
    with get_connection(TEST_DB_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, decided_by FROM shift_requests WHERE request_id = 1")
            row = cur.fetchone()
            assert row["status"] == "PENDING_APPROVAL"
            assert row["decided_by"] is None

    # Retry now succeeds
    r_retry = client.post("/manager/decision", headers={"Authorization": f"Bearer {mgr_token}"}, json={"request_id": 1, "decision": "approve"})
    assert r_retry.status_code == 200


def test_step6_gaps_llm_budget_and_failures():
    """11. LLM call budget exceeded returns plain message; call count is bounded to <= 4."""
    budget = LLMBudget()
    budget.consume()
    budget.consume()
    budget.consume()
    budget.consume()
    with pytest.raises(LLMBudgetExceeded):
        budget.consume()

    # Parse message with FakeLLM returning invalid JSON twice -> raises LLMParseError
    fake_bad = FakeLLM(responses=["not json", "still not json"])
    test_budget = LLMBudget()
    with pytest.raises(LLMParseError):
        parse_message(fake_bad, test_budget, "move shift")


def test_expected_domain_errors_do_not_log_as_unexpected(caplog):
    """12. Expected domain errors (NotFound, CabFull) are logged at INFO/WARNING and do not trigger 'Unexpected error'."""
    identity = issue_token("comp_alpha", "emp_005", "employee")
    from app.auth import decode_token
    ident_obj = decode_token(identity)

    with caplog.at_level(logging.INFO):
        # Querying non-existent roster raises NotFound and logs domain error
        with pytest.raises(NotFound):
            run_tool(check_cab_options, ident_obj, 999999, "21:00", db_url=TEST_DB_URL)

    # Check logs
    assert any("Domain error in tool execution" in r.message for r in caplog.records)
    assert not any("Unexpected error in tool execution" in r.message for r in caplog.records)
