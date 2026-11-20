# Shiftbot: Shift-Change Request Agent

An automated shift-change and cab booking agent for a multi-tenant B2B transport desk. Built with **FastAPI**, **LangGraph** (persistent `PostgresSaver`), **PostgreSQL 16**, and **Groq** (or any OpenAI-compatible LLM).

Start everything with **one command**:

```bash
docker compose up --build
```

---

## 1. Overview & Setup

Shiftbot manages employee shift-change requests (pickup and drop cab rescheduling) across companies. It enforces cut-off rules in code, books seats atomically, pauses for manager approval, and keeps each company's data isolated.

### One-Command Start
```bash
docker compose up --build
```
No `.env` file is needed to start.
- The database creates its schema and seeds cabs for today's real date (or for `FIXED_NOW` when set).
- **Tests and eval need no API key.** They use a fake LLM.
- **The live `/chat` walkthrough needs a free Groq key.** Without one, `/chat` replies that the assistant is not configured and changes nothing. It does not crash.
- `JWT_SECRET` defaults to `demo-only-secret-change-me` (demo only; override it in production).

After `docker compose up`, wait a few seconds and check:
```bash
curl http://localhost:8000/health
```
Expected: `{"status":"ok","db":"ok"}`. A call made in the first second can fail while the app is still starting.

### Optional: Enable the Live LLM
Create a `.env` file (never commit it) with:
```dotenv
GROQ_API_KEY=gsk_your_groq_api_key_here
LLM_MODEL=openai/gpt-oss-20b
FIXED_NOW=2026-11-20T17:00:00+05:30
```
Then run `docker compose up -d --force-recreate`.

---

## 2. Quick Try (Reviewer in a Hurry)

> [!IMPORTANT]
> **Walkthrough clock.** Seed data has cabs at 18:00, 19:00, 20:00, 21:00 and 22:00 on the service day. By default `app.db.ensure_service_day()` seeds the real current date and `app.clock.now()` uses the real clock, so at some hours the seeded cabs are already in the past or inside the cut-off. For a predictable walkthrough, start with a pinned clock (5:00 PM IST):
>
> **Bash / macOS / Linux:**
> ```bash
> FIXED_NOW=2026-11-20T17:00:00+05:30 docker compose up -d
> ```
> **PowerShell (Windows):**
> ```powershell
> $env:FIXED_NOW = "2026-11-20T17:00:00+05:30"
> docker compose up -d
> ```
> Pytest and `eval/run_eval.py` pin their own time and do not need this.
>
> The walkthrough below also needs `GROQ_API_KEY` in `.env` (see section 1).

### 1. Issue an employee token
Alice is `comp_alpha` / `emp_001` / employee.

**Bash:**
```bash
ALICE_TOKEN=$(docker compose exec -T app python -c "from app.auth import issue_token; print(issue_token('comp_alpha', 'emp_001', 'employee'))")
```
**PowerShell:**
```powershell
$AliceToken = (docker compose exec -T app python -c "from app.auth import issue_token; print(issue_token('comp_alpha', 'emp_001', 'employee'))").Trim()
```

### 2. Ask to move the drop to 9 PM
**Bash:**
```bash
curl -X POST http://localhost:8000/chat \
  -H "Authorization: Bearer $ALICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"conversation_id": "review_conv_01", "message": "can I move my drop to 9 PM?"}'
```
**PowerShell:**
```powershell
$h = @{ Authorization = "Bearer $AliceToken" }
$b = @{ conversation_id = "review_conv_01"; message = "can I move my drop to 9 PM?" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:8000/chat -Headers $h -ContentType "application/json" -Body $b
```
**Expected:** a rejection. Emp_001's 6 PM trip is inside Alpha's 2-hour cut-off at the pinned clock, so the change is not applied and the only option offered is the manager.

### 3. Send it to the manager (same conversation)
**Bash:**
```bash
curl -X POST http://localhost:8000/chat \
  -H "Authorization: Bearer $ALICE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"conversation_id": "review_conv_01", "message": "send it to my manager"}'
```
**PowerShell:**
```powershell
$b = @{ conversation_id = "review_conv_01"; message = "send it to my manager" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:8000/chat -Headers $h -ContentType "application/json" -Body $b
```
**Expected:** "submitted for manager approval". The workflow is now paused, and the pause survives an app restart.

### 4. Manager lists and decides
Alpha's manager is `comp_alpha` / `mgr_alpha` / manager.

**Bash:**
```bash
MGR_TOKEN=$(docker compose exec -T app python -c "from app.auth import issue_token; print(issue_token('comp_alpha', 'mgr_alpha', 'manager'))")

# List pending requests and note the request_id
curl http://localhost:8000/manager/requests -H "Authorization: Bearer $MGR_TOKEN"

# Approve it (use the id from the list; it is not necessarily 1)
curl -X POST http://localhost:8000/manager/decision \
  -H "Authorization: Bearer $MGR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"request_id": <id from the list>, "decision": "approve", "reason": "Operational approval"}'
```
**PowerShell:**
```powershell
$m = (docker compose exec -T app python -c "from app.auth import issue_token; print(issue_token('comp_alpha', 'mgr_alpha', 'manager'))").Trim()
$mh = @{ Authorization = "Bearer $m" }
Invoke-RestMethod -Uri http://localhost:8000/manager/requests -Headers $mh

# Replace ID with the request_id from the list
$d = @{ request_id = ID; decision = "approve"; reason = "Operational approval" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:8000/manager/decision -Headers $mh -ContentType "application/json" -Body $d
```

### 5. Check the employee's status
**Bash:**
```bash
curl "http://localhost:8000/chat/status?conversation_id=review_conv_01" -H "Authorization: Bearer $ALICE_TOKEN"
```
**PowerShell:**
```powershell
Invoke-RestMethod -Uri "http://localhost:8000/chat/status?conversation_id=review_conv_01" -Headers $h
```
**Expected:** approved, with the cab moved to the requested slot.

### Seeded accounts
| Company ID | Employee ID | Role | Initial cab |
| :--- | :--- | :--- | :--- |
| `comp_alpha` | `emp_001` .. `emp_004` | employee | `cab_a_<DATE>_1800` (18:00 drop) |
| `comp_alpha` | `emp_005` | employee | `cab_a_<DATE>_1900` (19:00 drop) |
| `comp_alpha` | `mgr_alpha` | manager | Alpha transport manager |
| `comp_beta` | `emp_101` .. `emp_105` | employee | `cab_b_<DATE>_1900` (19:00 drop) |
| `comp_beta` | `mgr_beta` | manager | Beta transport manager |

Alpha's cut-off is 120 minutes; Beta's is 60 minutes.

### Automated end-to-end demo (PowerShell)
```powershell
.\scripts\demo.ps1 -Fresh
```
Without `-Fresh`, the script expects a fresh stack already running with `FIXED_NOW=2026-11-20T17:00:00+05:30`, and a real `GROQ_API_KEY`. It runs 9 self-checks:
1. Health check.
2. Clarification and booking: `emp_005` asks "can I go a bit late?", answers "9 PM", and is booked at 21:00 with `X-LLM-Calls: 1`.
3. Cut-off: `emp_001` asks for 8 PM on an 18:00 drop, which is rejected; only the manager route is offered.
4. Escalation: `emp_001` insists, the workflow pauses as `PENDING_APPROVAL`.
5. Paused thread: "any update?" returns a pending notice with `X-LLM-Calls: 0`.
6. Isolation: `mgr_beta` sees an empty queue and gets 404 when deciding Alpha's request.
7. Restart: `docker compose restart app` while paused.
8. Approval: `mgr_alpha` approves and the workflow resumes from Postgres.
9. Status: `/chat/status` confirms the cab moved to 8:00 PM, then the DB audit rows are printed.

It exits with code 1 if any check fails.

---

## 3. Architecture & LLM-vs-Code Split

```
CLIENT --POST /chat (Bearer JWT)--> FASTAPI + AUTH
   * verifies JWT, takes identity (company_id, employee_id, role) from the token only
   * builds thread_id = company_id:employee_id:conversation_id
   * allocates a per-request LLM budget (max 4 calls)
        |
        v
LANGGRAPH STATE MACHINE
   Parse (LLM only) -> Route (code) -> Check/Apply (atomic SQL) -> Escalate (interrupt)
        |                  |                     |
   Groq LLM          app/messages.py       PostgreSQL 16
   (JSON fields)     (fixed templates)     (cabs, roster, requests, audit, checkpoints)
```

### Who does what
- **The LLM has one job:** turn the message into a validated JSON object (`intent`, `date`, `new_time`, `trip_type`). Its output is untrusted.
- **Plain code owns everything else:**
  - identity and company isolation
  - cut-off rules (`app/rules.py`)
  - cab availability and nearest-slot search
  - atomic seat updates (`UPDATE cabs ... WHERE seats_taken < capacity`)
  - reply text, rendered from fixed templates in `app/messages.py`
- **Reply integrity:** replies come from fixed templates, so the bot cannot state a change that did not happen. A success reply is produced only after the database write succeeded.

---

## 4. Performance

### How we stay under 4 LLM calls per message
- Normal message: **1** call.
- Invalid JSON from the model: **1** retry, so at most **2**.
- Hard budget: **4** per message, enforced in code.
- Paused request and manager approval: **0** calls.

### Measured latency
`scripts/latency.py`: 40 sequential real Groq requests (`openai/gpt-oss-20b`, 2 s pauses):
- p50 **982.5 ms**, p95 **2232.7 ms**, max **3040.2 ms**
- 0 errors, `X-LLM-Calls: 1` on every message

Measured before the last small changes, on my development machine.

---

## 5. Security & Isolation

1. **JWT identity.** `company_id`, `employee_id` and `role` come from the verified token and go into the graph config. They never come from the model or the request body.
2. **Server-built thread ids.** `thread_id = f"{company_id}:{employee_id}:{conversation_id}"`, so a user cannot touch another user's conversation.
3. **Company-scoped SQL.** Every query filters by `company_id`.
4. **Manager boundary.** `/manager/decision` checks the request belongs to the manager's company. Other companies get the same `404` as a missing id.
5. **Untrusted model output.** Output is validated against a schema. Invalid intents or times lead to a clarifying question, not an action.

---

## 6. Manager Approval & Restart Survival

```
Employee insists
   -> request created, status PENDING_APPROVAL
   -> interrupt(): graph pauses, state saved in Postgres (PostgresSaver)
   -> [app restarts, memory lost, Postgres keeps state]
   -> POST /manager/decision
   -> atomic claim: UPDATE ... WHERE status='PENDING_APPROVAL' RETURNING
   -> resume via Command(): re-check seats, apply change or report the race
```

- **Restart survival:** state lives in Postgres, so the app can restart while a request waits.
- **Race handling:** if the cab filled while the manager was deciding, the original booking stays and the employee is offered the nearest alternative.

---

## 7. Failure Handling

| Failure | System reaction | Employee sees |
| :--- | :--- | :--- |
| LLM down / timeout / 429 | Logged; no retry for provider errors | A plain "couldn't process that, nothing was changed, try again" message |
| No API key configured | Detected up front | "The assistant isn't configured with an AI model key right now. Nothing was changed. Please contact the transport desk." |
| Invalid LLM JSON | One retry; if still invalid, ask again | A clarifying question |
| Database error | Transaction rolled back, logged | "Something went wrong on our side. Nothing was changed." |
| Tool timeout | Operation abandoned | A "couldn't check cab availability, nothing changed" message |
| Cab full | Nearest free slot found | An offer the employee must accept |
| Cut-off violated | No booking, no rows created | Rejection with the manager as the only option |
| Double submit / replay | Idempotency key (SHA-256 of thread, roster and cab) | The original result, no second booking |
| Cross-company access | Query returns no rows | `404` or "no requests on file" |

Exact reply texts are in `app/messages.py`.

---

## 8. Bonus: Concurrency and Idempotency

1. **Atomic seat update**
   ```sql
   UPDATE cabs
   SET seats_taken = seats_taken + 1
   WHERE cab_id = %s AND company_id = %s AND seats_taken < capacity
   RETURNING cab_id, slot_time;
   ```
   Only one of two simultaneous requests for the last seat succeeds.
2. **Idempotency key** built from `thread_id`, `roster_id` and target `cab_id`. A repeated submit returns the first result and creates one request.
3. **Eval set:** 37 cases (28 deterministic + 9 live), checking both replies and database state.

---

## 9. Tests and Evaluation

### Pytest (85 tests)
```bash
docker compose exec app pytest
```

### Deterministic eval (28 cases, no key needed)
```bash
docker compose exec app python eval/run_eval.py
```
Runs right after `docker compose up`; it creates its own test database.

### Live LLM eval (9 cases, needs `GROQ_API_KEY`)
```bash
docker compose exec app python eval/run_eval.py --live
```
On Windows PowerShell, if the `--live` flag is swallowed, use `--% --live`.

### Judging rule
A case passes only if **both** the reply/awaiting state **and** the database state match the expectation. Reply text is matched on key facts (time, reason, next step), not exact strings.

### Case file
`eval/cases.jsonl` has one line per case with `id`, `input`, `expected`, `actual`, `pass`, `notes`:
```json
{"id": "cutoff_01", "input": "move my drop to 21:00", "expected": "Cutoff violation rejected", "actual": "Your drop trip scheduled for 7:00 PM is within the 2 hours cut-off...", "pass": true, "notes": "Reply verified; awaiting=confirm_offer; DB state untouched"}
```

---

## 10. Design Decisions & Trade-Offs

1. **Plain psycopg instead of an ORM.** Seat booking is concurrency-critical. Explicit conditional `UPDATE ... RETURNING` statements are easy to reason about and avoid hidden session caching.
2. **Parse-only LLM instead of agentic tool calling.** Letting the model call tools risks loops, latency spikes and rule violations. Trade-off: less flexible conversation, but rules always hold.
3. **Fixed reply templates instead of generated text.** Replies are predictable and cannot claim something that did not happen. Trade-off: less natural wording.
4. **Server-built thread ids.** Isolation is enforced by construction, not by trusting the client.
5. **LangGraph checkpointing instead of a hand-built state machine.** `interrupt()` and `PostgresSaver` give pause and resume across restarts with little custom code.
6. **Graceful demo mode.** The stack starts and the tests run with no key. Trade-off: the live walkthrough needs a key.
7. **Two-sided cut-off.** An automatic change needs both the current trip and the requested slot to be at least `cutoff_minutes` away.
8. **Same-slot short-circuit.** If the employee asks for the cab they are already on, the agent says so and changes nothing.

---

## 11. Known Limitations

- **One intent per message.** Compound requests are not split.
- **Hourly slots.** Seeded cabs are hourly (18:00 to 22:00). For an off-slot time or a full cab, the agent offers the nearest free slot, which the employee must accept.
- **Polling, not push.** Employees check `GET /chat/status`; there are no webhooks or WhatsApp.
- **LLM provider errors.** One retry only for invalid JSON. A timeout, HTTP 429 or provider error returns the plain "try again" message.
- **Live demo needs a key.** Without `GROQ_API_KEY`, `/chat` is disabled by design; tests and eval still run.