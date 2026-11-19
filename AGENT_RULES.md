## Rules
1. Work in small steps. Do ONLY the step I give you. Do not build ahead or add features I did not ask for.
2. Simple beats clever. No extra frameworks, no abstractions "for later", no over-engineering.
3. Every file starts with a 2-3 line comment: why this file exists. Keep functions short.
4. Never commit secrets. Use .env (git-ignored) and .env.example.
5. All business rules, identity and data isolation live in plain code. The LLM output is untrusted and only produces structured JSON.
6. Tests must use a FakeLLM. They must never call a real API.
7. After each step, run what you built and verify it. Do not say "done" unless the commands actually passed. If something fails, say so honestly with the real error.
8. Do not silently change the architecture. If you see a real problem with it, stop and tell me instead.
9. Finish every step with a REPORT in exactly this format:
   - Files created/changed (list)
   - Commands run and their real output (trimmed)
   - What works / what does not
   - Decisions you made that I did not specify
   - 3 viva questions about this step with short answers

## Architecture (fixed)
- Stack: Python 3.11+, FastAPI, LangGraph, Postgres (data AND LangGraph checkpointer), PyJWT, Pydantic, pytest, Docker Compose. LLM: Gemini or Groq (free tier) behind one small interface in llm.py, plus FakeLLM for tests.
- No frontend. Use FastAPI /docs and a demo script.
- Folder structure:
  app/ (main.py, auth.py, graph.py, llm.py, rules.py, tools.py, db.py, messages.py)
  tests/ (test_isolation.py, test_cutoff.py, test_resume_restart.py, test_tool_selection.py, test_tool_failure.py)
  eval/ (cases.jsonl, run_eval.py)
  docker-compose.yml, Dockerfile, requirements.txt, .env.example, .gitignore, README.md, REPORT.md
- Identity (company_id, employee_id, role) comes ONLY from the JWT, passed into the graph config. Never from the user message or the model. Every DB query filters by company_id.
- thread_id = company_id:employee_id:conversation_id, built on the server.
- The LLM does one job: parse the message into {intent, new_time, date, trip_type} (validated with Pydantic). Missing fields lead to a clarifying question. All replies are code templates, never LLM text.
- Max 4 LLM calls per message, enforced by a counter in code. Reply p95 under 5 seconds.
- Flow: parse (LLM) -> load roster -> rules check -> find cab/seat -> apply, or offer nearest option; if the employee insists, create a PENDING_APPROVAL request and interrupt() -> POST /manager/decision resumes -> re-check seat -> apply or reject.
- Rules per company (two companies with different cut-off minutes and seats per cab) live in rules.py as plain functions: check_cutoff, free_seats, nearest_slot.
- Seat taking is atomic: UPDATE cabs SET seats_taken = seats_taken+1 WHERE id=? AND seats_taken < capacity. shift_requests has a unique idempotency_key.
- Failures (LLM timeout or bad JSON, tool timeout, cab filled, request in approval) return plain messages saying what happened and what to do next. Never a stack trace, never a silent hang.
