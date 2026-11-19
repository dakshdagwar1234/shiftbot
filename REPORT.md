<!--
Why this exists:
Chronological audit log and progress reports for each implementation step.
Tracks changes, command runs, decisions made, and verification.
-->

# Implementation Reports

## STEP 1: Skeleton and Docker

### Files created/changed
- `AGENT_RULES.md`
- `.gitignore`
- `.env.example`
- `.env`
- `requirements.txt`
- `Dockerfile`
- `docker-compose.yml`
- `pytest.ini`
- `README.md`
- `REPORT.md`
- `app/__init__.py`
- `app/main.py`
- `app/auth.py`
- `app/graph.py`
- `app/llm.py`
- `app/rules.py`
- `app/tools.py`
- `app/db.py`
- `app/messages.py`
- `tests/test_health.py`
- `tests/test_isolation.py`
- `tests/test_cutoff.py`
- `tests/test_resume_restart.py`
- `tests/test_tool_selection.py`
- `tests/test_tool_failure.py`
- `eval/cases.jsonl`
- `eval/run_eval.py`

### Commands run and their real output (trimmed)
1. `git init`
   Output: `Initialized empty Git repository in C:/Users/daksh/OneDrive/Desktop/shiftbot/.git/`
2. `docker compose up --build -d`
   Output:
   ```
   Container shiftbot-db-1 Healthy
   Container shiftbot-app-1 Started
   ```
3. `curl.exe -s http://localhost:8000/health`
   Output: `{"status":"ok","db":"ok"}`
4. `docker compose exec app pytest`
   Output:
   ```
   tests/test_health.py .. [100%]
   2 passed, 1 warning in 0.41s
   ```

### What works / what does not
- Works:
  - Multi-container environment with PostgreSQL 16 and FastAPI running under Docker Compose.
  - PostgreSQL healthcheck confirms DB is ready before app starts.
  - `GET /health` executes a live `SELECT 1` query via `psycopg` and returns `{"status":"ok","db":"ok"}`.
  - Automated tests with `pytest` testing both healthy and database failure paths without calling any LLM.
- What does not:
  - Agent business logic, graph workflows, authentication, and database migrations are not yet implemented.

### Decisions you made that I did not specify
1. Added `pytest.ini` with `pythonpath = .`.
2. Structured `test_health.py` with mock-based tests for both success and failure scenarios.
3. Added fallback environment variable defaults in `docker-compose.yml`.

---

## STEP 2: Repo hygiene + JWT auth + GitHub push

### Files created/changed
- `app/auth.py`
- `app/main.py`
- `scripts/issue_token.py`
- `tests/test_auth.py`
- `REPORT.md`

### Commands run and their real output (trimmed)
1. `git check-ignore .env` -> `.env`
2. `git ls-files | findstr /i ".env"` -> `.env.example`
3. `docker compose exec app pytest` -> `12 passed, 1 warning in 1.48s`
4. `docker compose exec app python scripts/issue_token.py --company comp_alpha --employee emp_007 --role employee` -> Token generated
5. `curl.exe -s http://localhost:8000/whoami` -> `{"detail":"Invalid or missing authentication token"}`
6. `curl.exe -s -H "Authorization: Bearer <token>" http://localhost:8000/whoami` -> `{"company_id":"comp_alpha","employee_id":"emp_007","role":"employee"}`

### What works / what does not
- Works:
  - Cryptographically signed JWT tokens issued via `app/auth.py` and CLI utility `scripts/issue_token.py`.
  - Immutable `Identity` Pydantic model (`company_id`, `employee_id`, `role`).
  - FastAPI dependencies `get_identity`, `require_employee`, and `require_manager`.
  - Algorithm locked to `HS256` explicitly; `alg: none` and forged tokens rejected with 401.
  - Endpoint `GET /whoami` strictly reads identity from verified tokens.
- What does not:
  - LLM parsing, LangGraph state machine, database tables, and manager approval flows.

### Decisions you made that I did not specify
1. Hybrid Docker strategy (`COPY . .` in Dockerfile + `.:/app` volume in compose).
2. `ConfigDict(frozen=True)` on `Identity`.
3. Added CLI argument `--expires-in-minutes` to `issue_token()`.

---

## STEP 3: Database schema, seed data, and rules engine

### Files created/changed
- `AGENT_RULES.md`
- `README.md`
- `docker-compose.yml`
- `app/db.py`
- `app/rules.py`
- `app/main.py`
- `tests/test_cutoff.py`
- `tests/test_isolation.py`
- `REPORT.md`

### Commands run and their real output (trimmed)
1. `docker compose exec db psql -U shift -d shift_db -c "SELECT ..."` -> Verified 6 employees, 5 cabs, 5 roster entries per company.
2. `docker compose exec app pytest -v` -> `25 passed, 1 warning in 1.47s`
3. `git push origin main` -> Pushed commit `e81a454`

### What works / what does not
- Works:
  - Database schema initialized with plain SQL using `psycopg` (no ORM).
  - Multi-tenant seed data for `comp_alpha` and `comp_beta`.
  - Read-only data access functions (`get_roster`, `get_cabs`, `get_company_rules`) enforcing `company_id`.
  - Pure rules engine in `app/rules.py` (`check_cutoff`, `free_seats`, `nearest_slot`).
- What does not:
  - LangGraph workflow and LLM parsing are not connected yet.

### Decisions you made that I did not specify
1. Isolated PostgreSQL schema for duplicate seed testing.
2. FastAPI `lifespan` handler instead of deprecated `@app.on_event("startup")`.
3. Supported both `datetime.time` and `"HH:MM"` string parsing in `nearest_slot`.

---

## STEP 4: Clock, service-day seeding, and the tools

### Files created/changed
- `app/clock.py`
- `.env.example`
- `app/db.py`
- `app/tools.py`
- `app/messages.py`
- `tests/test_tool_selection.py`
- `tests/test_tool_failure.py`
- `tests/test_isolation.py`
- `REPORT.md`

### Commands run and their real output (trimmed)
1. `docker compose exec db psql -U shift -d shift_db -c "CREATE DATABASE shift_test_db;"`
   Output: `CREATE DATABASE`
2. `docker compose up --build -d`
   Output:
   ```
   Container shiftbot-db-1 Running
   Container shiftbot-app-1 Recreate
   Container shiftbot-app-1 Recreated
   Container shiftbot-app-1 Started
   ```
3. `docker compose exec app pytest -v`
   Output:
   ```
   ============================= test session starts ==============================
   platform linux -- Python 3.12.16, pytest-8.3.4, pluggy-1.7.0
   rootdir: /app
   configfile: pytest.ini
   collected 37 items

   tests/test_auth.py ..........                                            [ 27%]
   tests/test_cutoff.py ........                                            [ 48%]
   tests/test_health.py ..                                                  [ 54%]
   tests/test_isolation.py .....                                            [ 67%]
   tests/test_tool_failure.py ...                                           [ 75%]
   tests/test_tool_selection.py .........                                   [100%]

   ======================== 37 passed, 1 warning in 3.45s =========================
   ```
4. `docker compose exec db psql -U shift -d shift_db -c "SELECT cab_id, company_id, service_date, slot_time, capacity, seats_taken FROM cabs ORDER BY company_id, slot_time;"`
   Output: Verified cabs exist for service date 2026-11-20 for both comp_alpha and comp_beta.
5. `git check-ignore .env`
   Output: `.env`
6. `git push origin main`

### What works / what does not
- Works:
  - Centralized, controllable clock in `app/clock.py` pinned to `Asia/Kolkata` with `FIXED_NOW` override capability.
  - Split database seeding: companies and employees are seeded once; `ensure_service_day(date)` idempotently seeds cabs and rosters for any date.
  - Pure execution tools in `app/tools.py` (`get_roster`, `check_cab_options`, `apply_shift_change`, `create_pending_request`, `run_tool`).
  - Mandatory `Identity` parameter on all tools; `company_id` and `employee_id` are never separate parameters.
  - Atomic seat allocation (`UPDATE cabs SET seats_taken = seats_taken + 1 WHERE ... AND seats_taken < capacity`) prevents overbooking.
  - Concurrency verified: thread race for the last seat guarantees exactly 1 booking succeeds and 1 gets `CabFull`; concurrent double submission with the same idempotency key produces exactly 1 request and 1 seat.
  - Clean error masking in `run_tool`: timeouts raise `ToolTimeout`, unexpected internal exceptions raise `ToolFailure` with zero stack traces leaked to callers.
  - Plain English code templates in `app/messages.py` for all agent dialogue outcomes.
  - 37 automated tests passing in Docker container without touching real LLM.
- What does not:
  - LangGraph state graph wiring and LLM parsing (scheduled for subsequent steps).

### Decisions you made that I did not specify
1. **Dedicated PostgreSQL test database (`shift_test_db`)**: Created a separate test database on the local PostgreSQL instance so tests run with full schema isolation, multithreading concurrency safety, and zero interference with the main demo database `shift_db`.
2. **`ON CONFLICT (idempotency_key) DO NOTHING` for concurrency**: In `apply_shift_change` and `create_pending_request`, used PostgreSQL's atomic constraint resolution so concurrent requests with the identical idempotency key resolve cleanly without crashing transactions.
3. **Automatic test DB table wipe between tests**: Implemented an automated fixture in `test_tool_selection.py` to reset `shift_test_db` tables before each test, ensuring tests never inherit dirty state from previous test runs.
