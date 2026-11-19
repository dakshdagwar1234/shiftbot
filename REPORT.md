<!--
Why this exists:
Chronological audit log and progress reports for each implementation step.
Tracks changes, command runs, decisions made, and viva questions (Steps 1-2).
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
  - Complete project skeleton and placeholder files with 2-3 line documentation comments.
- What does not:
  - Agent business logic, graph workflows, authentication, and database migrations are not yet implemented (deferred to future steps).

### Decisions you made that I did not specify
1. Added `pytest.ini` with `pythonpath = .` so running `pytest` directly inside the container or workspace discovers the `app` module without needing `PYTHONPATH=.`.
2. Structured `test_health.py` with mock-based tests for both the success (`200 OK`) and failure (`503 Service Unavailable`) scenarios so tests can run isolated without relying on live external state.
3. Added fallback environment variable defaults in `docker-compose.yml` (`${VAR:-default}`) to ensure containers start smoothly even before user-customized `.env` files are provided.

---

## STEP 2: Repo hygiene + JWT auth + GitHub push

### Files created/changed
- `app/auth.py`
- `app/main.py`
- `scripts/issue_token.py`
- `tests/test_auth.py`
- `REPORT.md`

### Commands run and their real output (trimmed)
1. `git check-ignore .env`
   Output: `.env`
2. `git ls-files | findstr /i ".env"`
   Output: `.env.example`
3. `docker compose exec app pytest`
   Output:
   ```
   tests/test_auth.py .......... [ 83%]
   tests/test_health.py ..       [100%]
   12 passed, 1 warning in 1.48s
   ```
4. `docker compose exec app python scripts/issue_token.py --company comp_alpha --employee emp_007 --role employee`
   Output: `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...`
5. `curl.exe -s http://localhost:8000/whoami`
   Output: `{"detail":"Invalid or missing authentication token"}`
6. `curl.exe -s -H "Authorization: Bearer <token>" http://localhost:8000/whoami`
   Output: `{"company_id":"comp_alpha","employee_id":"emp_007","role":"employee"}`

### What works / what does not
- Works:
  - Cryptographically signed JWT tokens issued via `app/auth.py` and CLI utility `scripts/issue_token.py`.
  - Immutable `Identity` Pydantic model (`company_id`, `employee_id`, `role`).
  - FastAPI dependencies `get_identity`, `require_employee`, and `require_manager` enforcing strict role boundaries.
  - Algorithm locked to `HS256` explicitly; `alg: none`, forged signatures, expired tokens, and malformed strings are securely rejected with 401 and zero internal leaks.
  - Endpoint `GET /whoami` strictly reads identity from verified tokens; attempts to tamper via query params or request body are ignored.
  - 10 comprehensive auth unit tests covering all security edge cases.
- What does not:
  - LLM parsing, LangGraph state machine, database tables, and manager approval flows are scheduled for upcoming steps.

### Decisions you made that I did not specify
1. Chose a hybrid Docker strategy: Dockerfile copies the entire directory (`COPY . .`) ensuring the image is completely self-contained for standalone execution on fresh clones, while `docker-compose.yml` mounts `.:/app` as a development volume for instant code and test reflection without rebuild overhead.
2. In `app/auth.py`, wrapped `Identity` with `ConfigDict(frozen=True)` to make it strictly immutable across downstream function calls and graph transitions.
3. Added CLI argument `--expires-in-minutes` parameter support to `issue_token()` for reproducible test and demo tokens.

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
1. `docker compose exec db psql -U shift -d shift_db -c "SELECT 'employees' AS table_name, company_id, count(*) FROM employees GROUP BY company_id UNION ALL SELECT 'cabs', company_id, count(*) FROM cabs GROUP BY company_id UNION ALL SELECT 'roster', company_id, count(*) FROM roster GROUP BY company_id ORDER BY table_name, company_id;"`
   Output:
   ```
    table_name | company_id | count 
   ------------+------------+-------
    cabs       | comp_alpha |     5
    cabs       | comp_beta  |     5
    employees  | comp_alpha |     6
    employees  | comp_beta  |     6
    roster     | comp_alpha |     5
    roster     | comp_beta  |     5
   (6 rows)
   ```
2. `docker compose exec db psql -U shift -d shift_db -c "SELECT cab_id, company_id, slot_time, capacity, seats_taken FROM cabs ORDER BY company_id, slot_time;"`
   Output:
   ```
      cab_id   | company_id | slot_time | capacity | seats_taken 
   ------------+------------+-----------+----------+-------------
    cab_a_1800 | comp_alpha | 18:00:00  |        4 |           4
    cab_a_1900 | comp_alpha | 19:00:00  |        4 |           3
    cab_a_2000 | comp_alpha | 20:00:00  |        4 |           1
    cab_a_2100 | comp_alpha | 21:00:00  |        4 |           0
    cab_a_2200 | comp_alpha | 22:00:00  |        4 |           0
    cab_b_1800 | comp_beta  | 18:00:00  |        6 |           0
    cab_b_1900 | comp_beta  | 19:00:00  |        6 |           6
    cab_b_2000 | comp_beta  | 20:00:00  |        6 |           5
    cab_b_2100 | comp_beta  | 21:00:00  |        6 |           1
    cab_b_2200 | comp_beta  | 22:00:00  |        6 |           0
   (10 rows)
   ```
3. `docker compose exec app pytest -v`
   Output:
   ```
   ============================= test session starts ==============================
   platform linux -- Python 3.12.16, pytest-8.3.4, pluggy-1.7.0
   rootdir: /app
   configfile: pytest.ini
   collected 25 items

   tests/test_auth.py ..........                                            [ 40%]
   tests/test_cutoff.py ........                                            [ 72%]
   tests/test_health.py ..                                                  [ 80%]
   tests/test_isolation.py .....                                            [100%]

   ======================== 25 passed, 1 warning in 1.47s =========================
   ```
4. `git check-ignore .env`
   Output: `.env`
5. `git push -u origin main`

### What works / what does not
- Works:
  - Database schema initialized with plain SQL using `psycopg` without ORM.
  - Idempotent `init_db()` automatically runs on app startup lifecycle; multiple runs leave seed rows untouched.
  - Multi-tenant data for `comp_alpha` (120m cutoff, 4 seats/cab) and `comp_beta` (60m cutoff, 6 seats/cab), with 6 employees each, full/nearly full evening drop cabs on "today's" date, and assigned rosters.
  - Read-only data access functions (`get_roster`, `get_cabs`, `get_company_rules`) strictly enforcing `company_id` filter.
  - Pure business rules engine in `app/rules.py` (`check_cutoff`, `free_seats`, `nearest_slot`) with zero DB or LLM dependencies.
  - Nearest slot selection skips full cabs, correctly picks the closest time, and cleanly breaks equidistant ties in favor of the earlier slot.
  - 25 automated unit and DB-isolation tests passing in the Docker container.
  - Pushed to GitHub `origin main`.
- What does not:
  - LangGraph workflow and LLM parsing are not yet connected (reserved for subsequent steps).

### Decisions you made that I did not specify
1. **Isolated Schema for DB Mutation Testing**: For seed lifecycle verification in `test_isolation.py`, executed table creation and duplicate seed checks inside a dedicated, isolated PostgreSQL schema (`test_sandbox_schema`), dropping it on teardown. This keeps the demo seed data completely untouched.
2. **Modern FastAPI Lifespan**: Replaced legacy `@app.on_event("startup")` with FastAPI's `lifespan` async context manager to invoke `init_db()` cleanly on startup.
3. **Flexible slot time comparison in `nearest_slot`**: Supported both `datetime.time` objects and `"HH:MM"` string representations when calculating time distances, ensuring interoperability between database objects and parsed JSON inputs.
