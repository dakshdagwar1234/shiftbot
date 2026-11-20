# Shiftbot: Enterprise Shift-Change Request Agent

An automated shift-change and cab booking agent built for a multi-tenant B2B transport desk. Powered by **FastAPI**, **LangGraph** (with persistent `PostgresSaver`), **PostgreSQL 16**, and **Groq** (or any OpenAI-compatible LLM).

Reviewers can boot and evaluate the complete system with **ONE command** without configuring any API keys or `.env` file.

```bash
docker compose up --build
```

---

## 1. Overview & Setup

Shiftbot manages employee shift-change requests (pickup and drop cab rescheduling) across corporate tenants. It enforces strict cut-off rules, executes atomic seat transactions, coordinates human-in-the-loop manager approvals, and guarantees cross-tenant isolation.

### One-Command Start (Zero Setup)
On a fresh clone, run:
```bash
docker compose up --build
```
No `.env` file is required. The system starts with safe demo defaults:
- Database automatically initializes schemas and seeds service day data (`2026-11-20`).
- If no `GROQ_API_KEY` is provided, the agent runs in graceful demo mode: `/chat` returns clean, user-friendly notices without crashing, and all deterministic test/eval suites run against local `FakeLLM` mock clients.
- `JWT_SECRET` defaults to `demo-only-secret-change-me` (for review/demo only; production environments must override via environment variable).

### Optional: Live Groq LLM Configuration
To enable live LLM natural language parsing via Groq, create a `.env` file with these 3 lines:
```dotenv
GROQ_API_KEY=gsk_your_groq_api_key_here
LLM_MODEL=openai/gpt-oss-20b
FIXED_NOW=2026-11-20T17:00:00+05:30
```
Then restart the container: `docker compose up -d`.

---

## 2. Quick Try (Reviewer in a Hurry)

### Step 1: Issue an Auth Token
Generate a mock JWT token for Alice (`comp_alpha`, `emp_001`, employee):

**Bash / macOS / Linux:**
```bash
ALICE_TOKEN=$(docker compose exec -T app python -c "from app.auth import issue_token; print(issue_token('comp_alpha', 'emp_001', 'employee'))")
echo $ALICE_TOKEN
```

**PowerShell (Windows):**
```powershell
$AliceToken = (docker compose exec -T app python -c "from app.auth import issue_token; print(issue_token('comp_alpha', 'emp_001', 'employee'))").Trim()
```

### Step 2: Send a Shift-Change Request
Ask to reschedule the drop cab to 9:00 PM:

**curl (Linux / macOS):**
```bash
curl -X POST http://localhost:8000/chat \
  -H "Authorization: Bearer $ALICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"conversation_id": "review_conv_01", "message": "can I move my drop to 9 PM?"}'
```

**PowerShell (Windows):**
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/chat" -Method POST -Headers @{ Authorization = "Bearer $AliceToken"; "Content-Type" = "application/json" } -Body '{"conversation_id": "review_conv_01", "message": "can I move my drop to 9 PM?"}'
```

### Step 3: Check Request Status
Inspect the current status of the conversation thread:

**curl:**
```bash
curl -X GET "http://localhost:8000/chat/status?conversation_id=review_conv_01" \
  -H "Authorization: Bearer $ALICE_TOKEN"
```

### Step 4: Manager Decision (Approve / Reject)
Issue a manager token for Alpha Transport Lead (`comp_alpha`, `mgr_alpha`, manager) and review/decide pending escalations:

**Bash:**
```bash
MGR_TOKEN=$(docker compose exec -T app python -c "from app.auth import issue_token; print(issue_token('comp_alpha', 'mgr_alpha', 'manager'))")

# List pending requests
curl -X GET http://localhost:8000/manager/requests \
  -H "Authorization: Bearer $MGR_TOKEN"

# Decide request (approve/reject)
curl -X POST http://localhost:8000/manager/decision \
  -H "Authorization: Bearer $MGR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"request_id": 1, "decision": "approve", "reason": "Operational approval"}'
```

### Seeded Accounts & Clock Pinning
| Company ID | Employee ID | Role | Seeded Initial Cab |
| :--- | :--- | :--- | :--- |
| `comp_alpha` | `emp_001` | `employee` | 18:00 Drop Cab (`Route North`) |
| `comp_alpha` | `emp_002` .. `emp_004` | `employee` | 18:00 Drop Cab |
| `comp_alpha` | `emp_005` | `employee` | 19:00 Drop Cab |
| `comp_alpha` | `mgr_alpha` | `manager` | Transport Manager (Alpha) |
| `comp_beta` | `emp_101` .. `emp_105` | `employee` | 19:00 Drop Cab (`Route South`) |
| `comp_beta` | `mgr_beta` | `manager` | Transport Manager (Beta) |

> [!NOTE]
> **What `FIXED_NOW` does**: When `FIXED_NOW=2026-11-20T17:00:00+05:30` is set, all internal time calculations call `app.clock.now()` which returns this exact moment (5:00 PM IST). This guarantees that demo and eval interactions consistently evaluate against the seeded 18:00..22:00 service day cabs without drift.

### Automated End-to-End Demo Script (PowerShell)
Reviewers can run the full 10-step realistic story with automated self-checking:
```powershell
.\scripts\demo.ps1 -Fresh
```
- **What it demonstrates**:
  1. Server health check verification (`GET /health`).
  2. Multi-turn clarification dialog & booking (`emp_005` asks *"can I go a bit late?"*, clarifies *"9 PM"*, applied at 21:00; prints `X-LLM-Calls: 1`).
  3. Cut-off window enforcement on 18:00 drop (`emp_001` requests 8 PM, departing in 60m < 120m cutoff; rejected, offering only manager route).
  4. Human-in-the-loop escalation (`emp_001` insists -> paused at `interrupt()` with `PENDING_APPROVAL`).
  5. Paused thread query (`emp_001` asks *"any update?"* -> returns pending notice with exactly `X-LLM-Calls: 0`).
  6. Cross-tenant isolation (`mgr_beta` sees 0 requests in queue; returns HTTP 404 attempting to decide Alpha's request).
  7. Container crash survival (`docker compose restart app` mid-pause; state rehydrated from `PostgresSaver`).
  8. Manager decision (`mgr_alpha` lists request, approves it; resumes workflow).
  9. Employee status check (`emp_001` queries `/chat/status`, confirms cab applied for 8:00 PM).
  10. Database audit log and shift request inspection via PostgreSQL CLI.
- **Runtime**: Runs fully automated in ~15 seconds. If any check fails, it exits with code 1. Without `-Fresh`, it assumes a fresh stack with `FIXED_NOW=2026-11-20T17:00:00+05:30` is already running.

---

## 3. Architecture & LLM-vs-Code Split

```
+-----------------------------------------------------------------------------------+
|                                 CLIENT INVOCATION                                 |
+-----------------------------------------------------------------------------------+
                                          |
                                          v  [HTTP POST /chat with Bearer JWT]
+-----------------------------------------------------------------------------------+
|                        FASTAPI APPLICATION & AUTH LAYER                           |
|  * Verifies JWT signature and extracts Identity (company_id, employee_id, role)   |
|  * Builds server-side thread_id: f"{company_id}:{employee_id}:{conversation_id}" |
|  * Enforces rate limiting and allocates single-request LLMBudget                  |
+-----------------------------------------------------------------------------------+
                                          |
                                          v
+-----------------------------------------------------------------------------------+
|                           LANGGRAPH STATE MACHINE ENGINE                          |
|                                                                                   |
|   +-------------------+       +--------------------+       +------------------+   |
|   |    Parse Node     | ----> |     Route Node     | ----> | Check/Apply Node |   |
|   |  (LLM Parse Only) |       | (Code State Logic) |       |  (Atomic Tools)  |   |
|   +-------------------+       +--------------------+       +------------------+   |
|             |                           |                           |             |
|             |                           |                           v             |
|             |                           |                 +-------------------+   |
|             |                           |                 | Escalation/Pause  |   |
|             |                           |                 |  interrupt() Node |   |
|             |                           |                 +-------------------+   |
+-----------------------------------------------------------------------------------+
       |                                  |                           |
       | LLM parses text to JSON          | Deterministic Templates   | Atomic SQL
       v                                  v                           v
+---------------------+       +----------------------+     +------------------------+
| Groq LLM Completion |       | app/messages.py      |     | PostgreSQL 16 (psycopg)|
| (intent, date,      |       | (Standardized Jinja- |     | * Cabs, Roster, Shifts |
|  time, trip_type)   |       |  free text responses)|     | * PostgresSaver State  |
+---------------------+       +----------------------+     +------------------------+
```

### The LLM vs. Code Split
- **The LLM has exactly ONE job**: Parse raw, ambiguous user text into a strictly validated JSON structure (`intent`, `date`, `new_time`, `trip_type`). The model is treated as an untrusted parser.
- **Plain Python Code owns everything else**:
  - Identity verification and tenant isolation.
  - Cut-off calculations (`app/rules.py`).
  - Cab availability lookups and nearest slot search.
  - Atomic database transactions (`UPDATE cabs ... WHERE seats_taken < capacity`).
  - Response text generation: Replies are rendered from deterministic, policy-approved string templates in `app/messages.py`.
- **Zero Hallucination Guarantee**: The LLM never decides if a change is permitted, never accesses database connections, and never synthesizes arbitrary user-facing responses.

---

## 4. Key Performance Metrics

### LLM Calls Per Interaction
- **Standard single-turn message**: Exactly **1 LLM call**.
- **Malformed model response**: Max **2 LLM calls** (1 automatic retry instructing the model to repair invalid JSON).
- **Hard budget cap**: **4 LLM calls** maximum across an entire turn.
- **Paused manager approval**: Exactly **0 LLM calls** on resume (all state restoration and seat allocations run purely in compiled code).

### Real Measured Latency Benchmark (20 Sequential Invocations)
Benchmarked via `scripts/measure_latency.py` hitting Groq's live API:
- **p50 Latency**: `982.5 ms`
- **p95 Latency**: `2232.7 ms`
- **Max Latency**: `3040.2 ms`
- **Total API Errors**: `0` (100% success rate)

---

## 5. Security & Isolation

1. **JWT-Enforced Identity**: Authentication tokens cannot be forged without `JWT_SECRET`. Identity fields (`company_id`, `employee_id`, `role`) are injected into LangGraph execution config (`config["configurable"]["identity"]`), never loaded from model text or client headers.
2. **Server-Side Thread IDs**: The conversation key is strictly formed as:
   ```python
   thread_id = f"{identity.company_id}:{identity.employee_id}:{conversation_id}"
   ```
   A malicious user cannot read, hijack, or corrupt another employee's state machine.
3. **Tenant-Scoped SQL Operations**: Every database query and transaction filters strictly by `company_id`.
4. **Manager Boundary Defense**: `/manager/decision` verifies that the target request belongs to the authenticated manager's company. Cross-tenant access attempts return identical `404 Not Found` responses to prevent ID enumeration.
5. **Untrusted LLM Guardrails**: Model output is validated using Pydantic regex validators. Invalid intents, malformed times, or hallucinated payloads trigger clean clarification templates.

---

## 6. Manager Approval & Restart Survival

```
[Employee Insists] 
       │
       ▼
[Create Request in DB] ──> Status: PENDING_APPROVAL
       │
       ▼
[interrupt() Node] ──────> Execution Halts; State Persisted to PostgresSaver
       │
 [CONTAINER REBOOT] ────> App reboots; Memory wiped; Postgres state preserved intact!
       │
       ▼
[POST /manager/decision] ─> Atomic Claim (SELECT FOR UPDATE)
       │
       ▼
[Resume via Command()] ──> Re-verifies seats -> Allocates cab or reports race condition
```

- **Persistence Across Crashes**: Because state is managed by `langgraph.checkpoint.postgres.PostgresSaver`, the app container can be stopped, restarted, or rescheduled at any point during a manager escalation. When the manager acts, state is rehydrated seamlessly.
- **Race Condition Immunity**: When a manager approves a request hours later, seats on that cab may have filled. The agent re-checks capacity atomically. If full, it leaves the original booking intact and prompts the employee with the nearest alternative.

---

## 7. Failure Handling Matrix

| Failure Type | System Reaction | User Experience |
| :--- | :--- | :--- |
| **LLM Down / 503** | Catches `httpx` connection error; logs traceback | *"Our AI service is temporarily unavailable. Please try again in a moment."* |
| **Invalid LLM JSON** | Budgeted retry; if still invalid, resets awaiting state | *"I didn't understand that. Could you tell me the new time, like '9 PM'?"* |
| **Database Down** | Catches `psycopg.Error`; initiates transaction rollback | *"Something went wrong on our side. No changes were made to your schedule."* |
| **Tool Execution Timeout**| `ThreadPoolExecutor` timeout terminates operation | *"The transport system took too long to respond. Your current cab booking remains active."* |
| **Cab Full (No Seats)** | Computes nearest available cab with free seats | *"The cab at 6:00 PM is completely full. The nearest available cab is at 7:00 PM. Would you like to switch to that slot?"* |
| **Cut-off Violated** | Verifies time diff < cutoff; suppresses auto-booking | *"Your drop trip scheduled for 6:00 PM is within the 2 hours cut-off. It is too late to change it automatically. If you still need it, tell me and I'll send it to your manager."* |
| **Double Click / Replay** | Verified via deterministic UUIDv5 idempotency key | Exactly 1 seat allocated; second request returns idempotent cached confirmation. |
| **Cross-Company Access** | SQL query returns 0 rows; raises `NotFound` | Returns `404 Not Found` or clean message: *"You do not have any shift change requests on file."* |

---

## 8. Bonuses Implemented

1. **Atomic Concurrency-Safe Seat Updates**:
   ```sql
   UPDATE cabs 
   SET seats_taken = seats_taken + 1 
   WHERE cab_id = %s AND company_id = %s AND seats_taken < capacity 
   RETURNING cab_id, slot_time;
   ```
   Guarantees zero overbooking under high-concurrency race conditions.
2. **Deterministic Tool Idempotency Keys**:
   Every mutation generates a unique idempotency key from `thread_id`, `roster_id`, and target `cab_id`. Replays return the original result without double-booking.
3. **Comprehensive Evaluation Suite**:
   Features a 28-case deterministic eval harness (`eval/run_eval.py`) verifying both HTTP responses and real database state, plus an automated `--live` runner testing real LLM parse accuracy and latency.

---

## 9. Tests and Evaluation

### Run Unit and Integration Tests (Pytest)
```bash
docker compose exec app pytest
```
*Executes all 85 automated test cases across auth, tools, rules, agent graph, approval flow, requested slot rules, same-slot handling, and tenant isolation.*

### Run Deterministic Evaluation Suite (28 Scenarios)
```bash
docker compose exec app python eval/run_eval.py
```
*Executes 28 end-to-end benchmark scenarios across 7 core categories. Verifies both user replies and underlying database tables.*

### Run Live LLM Accuracy Evaluation (9 Scenarios)
```bash
docker compose exec app python eval/run_eval.py --% --live
```
*Hits live Groq API to evaluate natural language parse accuracy, extraction fidelity, latency per call, and rate-limit handling.*

### Judging Rule & Evaluation Format
- **Judging Criteria**: Tool selection, tenant isolation, cut-off rejection, clarification dialogs, and atomic concurrency must achieve **100% pass rates**.
- **Case Audit Log (`eval/cases.jsonl`)**:
  Each evaluated scenario records a structured entry:
  ```json
  {"id": "cutoff_01", "input": "move my drop to 21:00", "expected": "Cutoff violation rejected", "actual": "Your drop trip scheduled for 7:00 PM is within the 2 hours cut-off...", "pass": true, "notes": "Reply verified ('within the 2 hours cut-off'); awaiting=confirm_offer; DB state untouched"}
  ```

---

## 10. Design Decisions & Trade-Offs

1. **Plain Psycopg over Heavy ORM (SQLAlchemy)**:
   - *Reason*: Shift booking is concurrency-critical. Explicit SQL queries with `FOR UPDATE` and atomic conditional updates eliminate hidden ORM session caching and state desynchronization.
2. **Parse-Only LLM vs. Agentic Tool Calling**:
   - *Reason*: Giving the LLM direct tool-calling agency leads to non-deterministic loops, latency spikes, and potential policy violations. Restricting the LLM to parsing natural language guarantees strict business policy compliance.
3. **Deterministic Response Templates over Natural Language Generation**:
   - *Reason*: In enterprise transport, replies must be concise, unambiguous, and legally accurate. Templated replies prevent prompt injection leakage and save thousands of output tokens.
4. **Server-Constructed Thread Identifiers**:
   - *Reason*: Trusting client-supplied conversation keys allows cross-user thread hijacking. Prefacing the key with verified JWT identity parameters (`company_id:employee_id`) enforces isolation at the runtime level.
5. **LangGraph Checkpointing over Custom Relational State Machine**:
   - *Reason*: LangGraph's `interrupt()` primitive natively suspends workflows and writes state checkpoints to PostgreSQL. This eliminated hundreds of lines of custom polling code.
6. **Graceful Demo Defaults on Fresh Clone**:
   - *Reason*: Facilitates instant code review without configuration friction. The system boots cleanly and falls back to mock intelligence when external API credentials are absent.
7. **Two-Sided Cut-Off Rule (Original Trip AND Requested Target Slot)**:
   - *Policy*: Automatic shift change is allowed if and only if *both* the employee's currently scheduled cab and the newly requested target slot depart at least `cutoff_minutes` in the future.
   - *Reason*: Rejecting changes when an existing trip is under cut-off is only half the business requirement. An employee cannot spontaneously book an unrostered cab departing in under `cutoff_minutes` either. Checking both original and requested slots guarantees dispatch operational readiness.
8. **Same-Slot Request Short-Circuit**: If an employee requests the exact cab slot they are already rostered on, the agent immediately informs them they are already on that cab and that nothing was changed, touching no seats and creating no database rows.

---

## 11. Known Limitations

- **Single-Intent Per Message**: The parser is optimized for single-intent actions (e.g., reschedule or status). Compound requests ("cancel my morning cab and book 9 PM drop") are not currently decomposed.
- **Fixed 15-Minute Slot Boundaries**: Booking logic maps requests to discrete seeded cab slots (e.g., 18:00, 19:00, 20:00). Arbitrary on-demand times (e.g., 18:23) map to the nearest available cab.
- **Push Notification Channel**: Employee notifications currently rely on REST polling (`GET /chat/status`). Integrating webhooks or WhatsApp transport endpoints remains future work.
- **Upstream LLM Rate Limits**: Free-tier Groq API endpoints can occasionally emit HTTP 429 during rapid traffic spikes. The system handles this via exponential backoff, which introduces momentary latency.
