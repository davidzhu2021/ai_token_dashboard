# Repository Guidelines

## Project Structure & Module Organization

This repository contains a FastAPI backend and a static frontend for the AI usage dashboard.

- `backend/main.py` defines the FastAPI app, routes, session middleware, and static file mounting.
- `backend/auth.py` contains OIDC, session, email-domain, and development-login helpers.
- `backend/litellm_client.py` wraps upstream LiteLLM API calls and usage aggregation.
- `index.html` is the single-page dashboard shell.
- `assets/app.js` contains frontend state, API calls, rendering, and UI interactions.
- `.env.example` documents required local configuration; keep real secrets in `.env`.

There is no dedicated tests directory yet. Add tests under `tests/` when introducing backend behavior that should be repeatably verified.

## Build, Test, and Development Commands

Create and run the local environment:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000` to use the dashboard. For local data checks, copy `.env.example` to `.env`, provide LiteLLM and OIDC values, and temporarily set `DEV_LOGIN_ENABLED=true` only in development.

No automated test command is currently configured. At minimum, verify `GET /api/health`, login flow behavior, and key dashboard views after changes.

## Coding Style & Naming Conventions

Use 4-space indentation and type hints for Python. Keep route handlers small and move reusable authentication or upstream API behavior into `backend/auth.py` or `backend/litellm_client.py`. Raise `HTTPException` with clear user-facing Chinese messages where existing routes do so.

Frontend JavaScript uses plain browser APIs, camelCase names, `const`/`let`, and small rendering helpers. Keep UI copy consistent with the current Chinese product language.

## LiteLLM Reference Requirement

Before making code changes, review the relevant LiteLLM behavior against the local official project checkout at `D:\litellm` and the LiteLLM Proxy UI documentation at `https://docs.litellm.ai/docs/proxy/ui`. Prefer the local source for implementation details and the official documentation for product/API intent. Do not guess LiteLLM endpoint names, request fields, or response shapes when they can be confirmed from those sources.

## Testing Guidelines

When adding tests, prefer `pytest` for backend code and place files as `tests/test_*.py`. Mock upstream LiteLLM and OIDC calls rather than using production services. Cover authentication gates, email-domain validation, date-range handling, and key-regeneration ownership checks.

## Commit & Pull Request Guidelines

Recent commits use short imperative summaries such as `Polish dashboard UI for v1 launch` and `v1`. Keep new commit titles concise and outcome-focused.

Pull requests should include a brief change summary, configuration or migration notes, manual verification steps, and screenshots or screen recordings for visible UI changes. Link related issues when available.

After modifying project files, finish by reviewing `git diff`, committing the intended changes, and pushing them to the configured GitHub remote. Do not include `.env`, secrets, generated logs, virtual environments, or unrelated user changes in those commits.

## Security & Configuration Tips

Never commit `.env`, admin keys, OIDC client secrets, auth tokens, generated audit logs, or full regenerated keys. Production must use SSO with `DEV_LOGIN_ENABLED=false`; debug flags such as `DEBUG_MAPPING_ENABLED` and `DEBUG_OIDC_CLAIMS` should remain disabled outside troubleshooting sessions.
