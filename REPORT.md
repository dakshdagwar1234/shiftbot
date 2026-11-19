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
1. `git init` -> `Initialized empty Git repository`
2. `docker compose up --build -d` -> Containers healthy and started
3. `curl.exe -s http://localhost:8000/health` -> `{"status":"ok","db":"ok"}`
4. `docker compose exec app pytest` -> `2 passed, 1 warning in 0.41s`

### What works / what does not
- Works: Multi-container setup, Postgres healthcheck, `/health` endpoint, initial structure.
- What does not: Application logic.

### Decisions you made that I did not specify
1. Added `pytest.ini` with `pythonpath = .`.
2. Mock-isolated `/health` test.
3. Fallback defaults in `docker-compose.yml`.

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
2. `docker compose exec app pytest` -> `12 passed, 1 warning in 1.48s`
3. `docker compose exec app python scripts/issue_token.py ...` -> Token generated
4. `curl.exe -s -H "Authorization: Bearer <token>" http://localhost:8000/whoami` -> Identity returned

### What works / what does not
- Works: Cryptographically signed JWT auth, frozen Identity model, role enforcement, 12 tests passing.
- What does not: Business logic, graph.

### Decisions you made that I did not specify
1. Hybrid Docker strategy (`COPY . .` + bind mount).
2. Frozen Pydantic `Identity` model.
3. Added CLI argument `--expires-in-minutes`.

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
1. `docker compose exec db psql ...` -> Verified seeded data.
2. `docker compose exec app pytest -v` -> `25 passed, 1 warning in 1.47s`
3. `git push origin main` -> Commit `e81a454` pushed.

### What works / what does not
- Works: Plain SQL database schema, multi-tenant seed data, read queries enforcing `company_id`, rules engine.
- What does not: LangGraph workflow.

### Decisions you made that I did not specify
1. Isolated PostgreSQL schema for duplicate seed testing.
2. Modern FastAPI lifespan.
3. Flexible slot time comparison.

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
1. `docker compose exec app pytest -v` -> `37 passed, 1 warning in 3.45s`
2. `git push origin main` -> Commit `91bdd6c` pushed.

### What works / what does not
- Works: Controllable clock, idempotent daily service seeding, atomic seat reservation with concurrency safety, typed errors, response templates.
- What does not: LangGraph state machine.

### Decisions you made that I did not specify
1. Dedicated PostgreSQL test database (`shift_test_db`).
2. `ON CONFLICT (idempotency_key) DO NOTHING` for concurrent requests.
3. Clean table reset fixture between test executions.

---

## STEP 5: Test DB auto-setup, LLM layer (parse only), FakeLLM

### Files created/changed
- `tests/conftest.py`
- `tests/test_tools.py`
- `tests/test_tool_selection.py`
- `tests/test_tool_failure.py`
- `tests/test_isolation.py`
- `app/llm.py`
- `app/messages.py`
- `scripts/llm_smoke.py`
- `.env.example`
- `docker-compose.yml`
- `REPORT.md`

### Commands run and their real output (trimmed)
1. `docker compose down -v`
   Output:
   ```
   Container shiftbot-app-1 Stopped
   Container shiftbot-app-1 Removed
   Container shiftbot-db-1 Stopped
   Container shiftbot-db-1 Removed
   Volume shiftbot_pgdata Removed
   Network shiftbot_default Removed
   ```
2. `docker compose up --build -d`
   Output:
   ```
   Volume shiftbot_pgdata Created
   Network shiftbot_default Created
   Container shiftbot-db-1 Started
   Container shiftbot-db-1 Healthy
   Container shiftbot-app-1 Started
   ```
3. `docker compose exec app pytest`
   Output:
   ```
   ============================= test session starts ==============================
   platform linux -- Python 3.12.16, pytest-8.3.4, pluggy-1.7.0
   rootdir: /app
   configfile: pytest.ini
   plugins: anyio-4.16.0, langsmith-0.15.0
   collected 49 items

   tests/test_auth.py ..........                                            [ 20%]
   tests/test_cutoff.py ........                                            [ 36%]
   tests/test_health.py ..                                                  [ 40%]
   tests/test_isolation.py .....                                            [ 51%]
   tests/test_tool_failure.py .......                                       [ 65%]
   tests/test_tool_selection.py .....                                       [ 75%]
   tests/test_tools.py ............                                         [100%]

   ======================== 49 passed, 1 warning in 3.79s =========================
   ```
4. `git check-ignore .env`
   Output: `.env`
5. `git push origin main`

### What works / what does not
- Works:
  - Fresh-clone test database auto-provisioning via `tests/conftest.py` (`pytest_sessionstart` connects to PostgreSQL and creates `shift_test_db` if not found with zero manual psql commands).
  - Clean test file reorganization: `tests/test_tools.py` owns all business tool execution tests; `tests/test_tool_selection.py` and `tests/test_tool_failure.py` test LLM parsing and failure handling.
  - Strict `ParsedRequest` Pydantic model with extra fields ignored, regex-validated 24-hour time (`HH:MM`), allowed intents, and dates.
  - Pluggable `LLMClient` interface with `GroqLLM`, `GeminiLLM`, and scriptable `FakeLLM` using plain REST HTTP calls without extra SDKs.
  - `LLMBudget` per-message token counter enforcing maximum 4 LLM calls before raising `LLMBudgetExceeded`.
  - Robust `parse_message` function with prompt injection isolation (`<user_message>` data delimiters), markdown fence stripping, and automatic 1-turn retry with validation error hints.
  - Standardized error templates `llm_unavailable()` and `could_not_understand()` added to `app/messages.py`.
  - Manual live smoke test script `scripts/llm_smoke.py` created (excluded from pytest).
  - 49 automated tests passing with no real API calls.
- What does not:
  - LangGraph workflow node wiring and `/chat` endpoint (scheduled for Step 6).

### Decisions you made that I did not specify
1. **`tests/conftest.py` with `pytest_sessionstart` hook**: Chose auto-provisioning `shift_test_db` in `conftest.py` over Docker init shell scripts because Python code runs consistently across Docker, local developer machines, and CI pipelines without requiring container rebuilds or directory-mounted volume dependencies.
2. **Scoping of retry error hints**: Stored `last_error` in an explicit local variable in `parse_message` to avoid Python's standard `UnboundLocalError` when referencing exceptions outside the `except` clause.
3. **Regex-driven time validation**: Enforced 24-hour time boundaries using `^([01]\d|2[0-3]):[0-5]\d$` in `ParsedRequest.validate_time_format` so times like "25:99" or "8pm" are immediately rejected and retried with clear validation hints.
