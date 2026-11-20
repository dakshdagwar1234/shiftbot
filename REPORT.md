# Shiftbot: System Evaluation & Benchmark Report

## 1. Executive Summary & Verification Metrics

Shiftbot is a deterministic, multi-tenant B2B shift-change agent built on FastAPI, LangGraph (`PostgresSaver`), PostgreSQL 16, and Groq (`openai/gpt-oss-20b`). The LLM functions strictly as an untrusted natural language parser; all business rules, cut-off validations, atomic seat transactions, tenant isolation boundaries, and replies are executed in plain Python code and static templates.

### Consolidated Results Table

| Metric / Category | Count / Value | Pass Rate | Key Verification Details |
| :--- | :--- | :--- | :--- |
| **Pytest Test Suite** | **85 / 85** | **100.0%** | Unit & integration tests against isolated test database (`shift_test_db`) |
| ├─ Cross-Company Isolation | 4 / 4 | 100.0% | Tenant data leak prevention; cross-company manager decision returns 404 |
| ├─ Cut-Off Policy Enforcement | 5 / 5 | 100.0% | Boundary checks (120m Alpha, 60m Beta) for original trip and requested slot |
| ├─ Resume After Restart | 2 / 2 | 100.0% | `interrupt()` state rehydrated from `PostgresSaver` across container reboot |
| ├─ Tool & Intent Selection | 7 / 7 | 100.0% | Accurate intent classification across change, accept, insist, decline, status, same-slot |
| ├─ Tool & Service Failure | 5 / 5 | 100.0% | Graceful handling for DB error, timeout, full cab, retry on invalid JSON |
| ├─ Clarification Dialogs | 3 / 3 | 100.0% | Ambiguous queries prompt for time; follow-up inherits pending context |
| └─ Concurrency & Idempotency | 2 / 2 | 100.0% | Idempotency key deduplication; race for last seat (3/4 -> 4/4) without overbooking |
| **Overall Deterministic Eval** | **28 / 28** | **100.0%** | Verified HTTP response AND live PostgreSQL database row mutations |
| **Live LLM Parse Accuracy** | **9 / 9** | **100.0%** | Real Groq API turns; zero HTTP 429 rate limit errors encountered |
| **Total Evaluation Cases** | **37 / 37** | **100.0%** | 28 deterministic + 9 live scenarios across all operational flows |
| **Latency Benchmark (p50 / p95 / max)** | **982.5 / 2232.7 / 3040.2 ms** | 100.0% | 20 sequential calls via `scripts/measure_latency.py`; 0 API errors |
| **LLM Calls Per Message** | **1 normal, max 2 retry, 0 paused** | N/A | Hard budget cap 4; paused manager threads consume exactly 0 LLM calls |

---

## 2. The Two-Sided Cut-Off Policy Rule

An automatic shift change is permitted if and only if **both** conditions are met:
1. **Original Trip Window**: The employee's currently scheduled departure must be $\ge$ `cutoff_minutes` in the future. (An employee cannot cancel or change a cab that is already within the dispatch cut-off window).
2. **Requested Slot Window**: The newly requested target slot must also depart $\ge$ `cutoff_minutes` in the future. (An employee cannot spontaneously book an unrostered cab departing in under `cutoff_minutes`).

If either departure is $< \text{cutoff\_minutes}$ from `clock.now()`:
- Automatic booking is rejected and no database rows are created.
- The employee receives an immediate cut-off notice offering manager escalation only.
- Policy differs by tenant: `comp_alpha` enforces 120 minutes (2 hours); `comp_beta` enforces 60 minutes. Exactly on the boundary (e.g. 120m for Alpha, 60m for Beta), changes are allowed.

---

## 3. Known Failures & How They Were Resolved

1. **Three Eval-Authoring Discrepancies in Step 8**:
   - *Problem*: In `cutoff_02` and `clarif_03`, expected seat counts were hardcoded to 1, but underlying cabs had 1 seat pre-seeded (actual: 2). In `tool_fail_cab_filled_meanwhile`, scenario expected `"submitted for manager approval"` instead of the post-approval race notice `"seat was taken in the meantime"`.
   - *Fix*: Aligned scenario assertions with real database states.
2. **Follow-Up Message Parse Bug (Step 6.1)**:
   - *Problem*: When replying to an ambiguous follow-up with bare time text like `"9 PM"`, the LLM parsed `trip_type` as null. The graph node looked up cabs with `trip_type=None`, causing an uncaught exception.
   - *Fix*: Implemented state inheritance in `app/graph.py` so follow-up turns preserve `pending_roster_id` and `pending_trip_type`.
3. **Contradictory Cut-Off Message (Step 8 Part A)**:
   - *Problem*: When a request broke the cut-off window, the system rejected it but paradoxically offered an alternative slot (`"Would you like to take that slot instead?."`), confusing users.
   - *Fix*: Because cut-off applies to the trip itself, no automatic alternative helps. Removed the alternative slot offer and double punctuation, offering manager escalation only.
4. **Swallowed Errors During Early Development**:
   - *Problem*: Broad exception catch blocks returned generic failure templates without logging stack traces, hiding bugs.
   - *Fix*: Structured Python logging at all catch points with timestamps, thread IDs, node names, and full tracebacks.

---

## 4. Stated Architectural Limitations

- **Single Intent Per Message**: The parser handles one operational intent per turn. Compound messages (*"cancel my morning cab and book a 9 PM drop"*) are not decomposed.
- **Discrete 15-Minute Slot Alignment**: Booking logic maps requests to discrete seeded cab slots (e.g., 18:00, 19:00). Arbitrary off-slot times (e.g., 18:23) map to the nearest available cab.
- **Asynchronous Polling Notification**: Decision notifications rely on client polling via `GET /chat/status` rather than real-time WebSockets or push notifications.
- **Upstream Rate Limits**: Free-tier cloud LLM providers can emit HTTP 429 under high burst loads. Mitigated via exponential backoff.

---

## 5. Architecture Trade-Offs

### 1. Parse-Only LLM vs. Agentic Tool Calling
In autonomous agent architectures (e.g. ReAct), the LLM decides which tools to invoke and when to loop. In enterprise transport dispatch, this is unacceptable: models hallucinate booking parameters, miss subtle cut-off policies, or trigger infinite loops. Restricting the LLM to parsing natural language guarantees that business policies, seat calculations, and database mutations are executed by deterministic, testable Python code.

### 2. LangGraph `PostgresSaver` Checkpointing vs. Custom Relational State Machine
Building a bespoke state machine in PostgreSQL requires tracking transitions, serializing intermediate state, and writing complex resume queries. LangGraph's native `interrupt()` primitive suspends workflow execution and persists serialized checkpoints directly to Postgres. When a manager acts hours later, the graph resumes with complete context intact.

### 3. Strict Schema Validation vs. Conversational Flexibility
We rejected free-form LLM response generation in favor of Pydantic-validated JSON extraction paired with deterministic reply templates. While this eliminates conversational chitchat, it completely prevents prompt injection exploits, eliminates text generation latency, and guarantees compliance with transport desk policies.
