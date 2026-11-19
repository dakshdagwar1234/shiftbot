<!--
Why this exists:
Overview and setup documentation for the MoveInSync Case Study A Shift-Change Request Agent.
Documents architecture, setup steps, environment configuration, and verification commands.
-->

# Shift-Change Request Agent

An enterprise agent for handling shift change and cab booking requests across multiple client companies with multi-tenant data isolation and deterministic rule enforcement.

## Environment Setup
Before running the application, you must create a `.env` file copied from `.env.example`:
```bash
cp .env.example .env
```
Ensure `JWT_SECRET` is populated in `.env`; the application will refuse to start if `JWT_SECRET` is missing.

### LLM Configuration
Set `LLM_PROVIDER` (groq or gemini) and your API key in `.env`.
Free-tier model names change. Check the provider's `/models` list if you get a 404.
