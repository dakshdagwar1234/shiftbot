<!--
Why this exists:
Chronological audit log and progress reports for each implementation step.
Tracks changes, command runs, decisions made, and viva questions.
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

### 3 viva questions about this step with short answers
1. **Q: Why does the `/health` endpoint execute a `SELECT 1` query instead of just returning `{"status": "ok"}`?**
   *A:* A shallow health check only verifies that Uvicorn can receive HTTP requests. Executing `SELECT 1` verifies full end-to-end connectivity between FastAPI and the PostgreSQL database, ensuring the service is truly ready to handle operational traffic.
2. **Q: Why is `depends_on: condition: service_healthy` used in Docker Compose instead of just `depends_on: [db]`?**
   *A:* By default, `depends_on` only waits for the database container process to start, not for PostgreSQL to finish socket and recovery initialization. Using `service_healthy` along with `pg_isready` guarantees that PostgreSQL is accepting connections before the app container launches, preventing startup crash loops.
3. **Q: Why do our tests mock `psycopg.connect` rather than connecting to a live database?**
   *A:* Unit tests must be fast, deterministic, and isolated. Mocking the database connection allows us to verify both normal response handling and failure edge cases (like network dropouts or timeouts) instantly without external dependencies or side effects.

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
   Output: `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjb21wYW55X2lkIjoiY29tcF9hbHBoYSIsImVtcGxveWVlX2lkIjoiZW1wXzAwNyIsInJvbGUiOiJlbXBsb3llZSIsImlhdCI6MTc5NTExNjE3NCwiZXhwIjoxNzk1MjAyNTc0fQ.1t9pAXWh9eUpdKcXzTK9rrxa-2b6OkZM7Ilgx12YGUE`
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

### 3 viva questions about this step with short answers
1. **Q: Why must `jwt.decode` explicitly specify `algorithms=["HS256"]` rather than allowing PyJWT to determine the algorithm from the token header?**  
   *A:* If the algorithm is not pinned, attackers can exploit algorithm confusion attacks (e.g. sending a token with `"alg": "none"` to bypass verification, or supplying an HMAC signature using an RSA public key). Explicitly locking the algorithm to `["HS256"]` completely neutralizes these attack vectors.
2. **Q: Why must tenant identity (`company_id`, `employee_id`, `role`) never be parsed from the request body or query parameters?**  
   *A:* Request bodies and query parameters are unverified user input that any malicious client can manipulate to impersonate another employee or another tenant company. The JWT signature is cryptographically verifiable by the server, ensuring identity cannot be forged or altered in transit.
3. **Q: Why do authentication errors return a generic message rather than specific failure reasons (e.g. 'expired' vs 'signature mismatch')?**  
   *A:* Providing detailed error breakdowns or stack traces leaks internal implementation details to potential attackers (known as information leakage). Returning a consistent, generic `401 Unauthorized` prevents attackers from probing the system to distinguish between expired tokens, brute-forced signatures, or malformed claims.
