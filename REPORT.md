# Shiftbot: System Evaluation & Benchmark Report

## 1. Executive Summary & Verification Metrics

Shiftbot is a deterministic, multi-tenant B2B shift-change agent built on FastAPI, LangGraph (`PostgresSaver`), PostgreSQL 16, and Groq (`openai/gpt-oss-20b`). The LLM functions strictly as an untrusted natural language parser; business rules, cut-off validations, atomic seat transactions, tenant isolation boundaries, and replies are executed in plain Python code and static templates.

### Consolidated Results Table

| Metric / Category | Count / Value | Pass Rate | Key Verification Details |
| :--- | :--- | :--- | :--- |
| **Pytest Test Suite** | **85 / 85** | **100.0%** | Unit & integration tests across 9 files: test_agent (23), test_approval (13), test_auth (10), test_cutoff (8), test_health (2), test_isolation (5), test_tool_failure (7), test_tool_selection (5), test_tools (12) |
| **Overall Deterministic Eval** | **28 / 28** | **100.0%** | Verified HTTP response AND live PostgreSQL database row mutations |
| ├─ Cross-Company Isolation | 4 / 4 | 100.0% | Tenant data leak prevention; cross-company manager decision returns 404 |
| ├─ Cut-Off Policy Enforcement | 5 / 5 | 100.0% | Boundary checks (120m Alpha, 60m Beta) for original trip and requested slot |
| ├─ Resume After Restart | 2 / 2 | 100.0% | `interrupt()` state rehydrated from `PostgresSaver` across container reboot |
| ├─ Tool & Intent Selection | 7 / 7 | 100.0% | Intent classification across change, accept, insist, decline, status, same-slot |
| ├─ Tool & Service Failure | 5 / 5 | 100.0% | Error handling for DB error, timeout, full cab, single retry on invalid JSON |
| ├─ Clarification Dialogs | 3 / 3 | 100.0% | Ambiguous queries prompt for time; follow-up inherits pending context |
| └─ Concurrency & Idempotency | 2 / 2 | 100.0% | Idempotency deduplication; race for last seat without overbooking |
| **Live LLM Parse Accuracy** | **9 / 9** | **100.0%** | Real Groq API turns; zero HTTP 429 rate limit errors encountered |
| **Total Evaluation Cases** | **37 / 37** | **100.0%** | 28 deterministic + 9 live scenarios across all operational flows |
| **Latency Benchmark (p50 / p95 / max)** | **982.5 / 2232.7 / 3040.2 ms** | N/A | 40 sequential real Groq requests via `scripts/latency.py` (2 s pauses, 0 errors, X-LLM-Calls 1 on every message; measured before last small changes with Groq openai/gpt-oss-20b) |
| **LLM Calls Per Message** | **1 normal, max 2 retry, 0 paused** | N/A | Hard budget cap 4; paused manager threads consume exactly 0 LLM calls |

---

## 2. The Two-Sided Cut-Off Policy Rule

An automatic shift change is permitted if and only if **both** conditions are met:
1. **Original Trip Window**: Currently scheduled departure must be $\ge$ `cutoff_minutes` in the future (cannot cancel/change a trip already within cut-off).
2. **Requested Slot Window**: Target departure must also be $\ge$ `cutoff_minutes` in the future (cannot spontaneously book an unrostered cab departing imminently).

If either departure is $< \text{cutoff\_minutes}$ from `clock.now()`:
- Automatic booking is rejected and no database rows are created.
- Employee receives an immediate notice offering manager escalation only.
- Tenants configure distinct policies: `comp_alpha` enforces 120 minutes (2 hours); `comp_beta` enforces 60 minutes. Exactly on the boundary, changes are allowed.

---

## 3. Known Failures & How They Were Resolved

1. **Follow-Up Message Missing Roster Context**: The first turn (no time) prompted for time without saving the pending roster entry. The follow-up ("9 PM") looked up `roster_id=None`, raising `NotFound` which returned a generic error with the traceback swallowed. *Fix*: Persisted `pending_roster_id` and `pending_trip_type` in graph state with fallback to today's roster, and added `logger.exception` calls on caught errors.
2. **Contradictory Cut-Off Rejection Notice**: Cut-off breach reply notified the user of the violation but offered an alternative slot with double punctuation. *Fix*: Cut-off governs the original trip, so no automatic slot helps; removed alternative slot offer and punctuation glitch, offering manager escalation only.
3. **Deprecated Groq Model Identifier**: Initial default model was retired upstream, returning HTTP 404 on API calls. *Fix*: Updated default model configuration across code and deployment templates to `openai/gpt-oss-20b`.
4. **Insist Intent Misclassification**: Real LLM failed to recognize colloquial insistence ("send it to my manager") as `insist`. *Fix*: Tuned prompt instructions and few-shot examples in `app/llm.py` to map manager escalation to `insist`.
5. **Redundant Same-Slot Mutation**: Requesting the assigned cab slot created an unnecessary shift request row. *Fix*: Added short-circuit in `app/graph.py` notifying employee they are already on that slot with zero DB mutations.
6. **Test DB Missing on Fresh Clone Before Pytest**: Running `eval/run_eval.py` immediately on fresh clone crashed with missing `shift_test_db`. *Fix*: Shared `ensure_test_db()` helper in `app/db.py` called by test harness and eval runner.
7. **Demo Freshness Check Cast Bug and Startup Race**: In `scripts/demo.ps1`, querying count before app lifespan finished `init_db()` threw missing relation errors, and indexing `[-1]` on scalar `"0"` returned a PowerShell `System.Char` (`[int]48 > 0`). *Fix*: Added health wait loop for `/health` before querying, and cast via `[int][string]`, failing only when count is strictly greater than 0.
8. **Eval Authoring Discrepancies**: Expected seat counts in `cutoff_02` and `clarif_03` were hardcoded to 1 despite underlying cabs having 1 pre-seeded seat (actual: 2), and `tool_fail_cab_filled_meanwhile` expected pending escalation instead of the post-approval race notice. *Fix*: Aligned assertions with true seeded DB states.
9. **Swallowed Errors During Early Development**: Broad exception handlers returned generic templates without logging stack traces. *Fix*: Added structured logging with timestamps, thread IDs, node names, and full tracebacks.

---

## 4. Stated Architectural Limitations

- **Single Intent Per Message**: The parser handles one operational intent per turn. Compound messages (*"cancel morning cab and book 9 PM drop"*) are not decomposed.
- **Hourly Seeded Slot Boundaries**: Seed data cabs are hourly (`18:00, 19:00, 20:00, 21:00, 22:00` in `app/db.py`). If an employee requests an off-slot time (e.g., 18:23) or a full cab, the agent offers the nearest available cab slot, which the employee must explicitly accept (`accept_option`) to confirm.
- **Asynchronous Polling Notification**: Decision notifications rely on client polling via `GET /chat/status` rather than real-time WebSockets or push notifications.
- **Upstream Rate Limits & Failures**: Free-tier cloud LLM providers can emit HTTP 429 or timeout under load. The system performs one retry only for invalid JSON; a timeout, HTTP 429, or provider error returns the plain static "try again" message.

---

## 5. Architecture Trade-Offs

### 1. Parse-Only LLM vs. Agentic Tool Calling
Restricting the LLM to parsing natural language guarantees business policies, seat calculations, and database mutations are executed by deterministic Python code, eliminating hallucinated bookings and autonomous tool loops.

### 2. LangGraph `PostgresSaver` Checkpointing vs. Custom Relational State Machine
LangGraph's native `interrupt()` primitive suspends workflow execution and persists serialized checkpoints directly to Postgres. When a manager acts hours later, the graph resumes with complete context intact without custom transition polling code.

### 3. Strict Schema Validation vs. Conversational Flexibility
Deterministic reply templates and Pydantic validation prevent prompt injection exploits from controlling tools or passing identity parameters. Database queries are strictly scoped by the verified JWT `company_id`; an injection payload can at most mis-parse the caller's own request parameters (verified by test `test_prompt_injection_safety_caller_scope` and eval scenario `isol_03`).
