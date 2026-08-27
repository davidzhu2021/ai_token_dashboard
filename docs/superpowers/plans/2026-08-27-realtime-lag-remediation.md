# Realtime Lag Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent realtime worker recovery failures and increase settlement throughput without publishing incomplete usage windows.

**Architecture:** Keep PostgreSQL as the dashboard source of truth and retain the rule that only complete closed windows advance the settlement watermark. Make recovery bounded and non-fatal, run backend settlement independently, and reserve the live loop from long background work.

**Tech Stack:** Python 3.12, asyncio, FastAPI, asyncpg, pytest, LiteLLM `/spend/logs/v2`.

**Spec:** Production diagnosis approved in chat on 2026-08-27.

## Global Constraints

- Never publish or advance a realtime watermark for an incomplete upstream page set.
- Never print secrets, Kubernetes Secrets, provider keys, database credentials, or server `.env` contents.
- Production changes only through local commit, GitHub push, and the documented sync procedure.
- Use local `D:\litellm` as the implementation reference for `/spend/logs/v2` pagination and fields.
- Preserve concurrent user edits and unrelated worktree files.

### Task 1: Bounded recovery

**Files:**
- Modify: `backend/usage_store.py`
- Modify: `backend/usage_realtime_worker.py`
- Test: `tests/test_realtime_worker.py`

- [ ] Write tests proving recovery continues when request-ID restoration times out and that the query is bounded to the current day.
- [ ] Run the focused tests and confirm failure before implementation.
- [ ] Add a bounded recovery helper with a narrow time range and catch timeout as degraded recovery rather than process-fatal.
- [ ] Run focused tests and confirm pass.

### Task 2: Settlement throughput and fairness

**Files:**
- Modify: `backend/usage_realtime_worker.py`
- Test: `tests/test_realtime_worker.py`

- [ ] Write tests proving backend settlement attempts are isolated and incomplete windows never advance.
- [ ] Run focused tests and confirm failure before implementation.
- [ ] Schedule independent backend settlement tasks with bounded concurrency while retaining per-backend watermark ordering.
- [ ] Ensure live polling/publication remains ahead of background settlement and background work cannot block the live loop.
- [ ] Run focused tests and confirm pass.

### Task 3: Adversarial verification

**Files:**
- Modify: `tests/test_realtime_worker.py`
- Create: `tests/test_usage_store_recovery.py` if needed

- [ ] Add tests for timeout, dense pagination, worker restart, duplicate events, and stale watermark reporting.
- [ ] Run the full pytest suite, Python compile checks, and diff review.
- [ ] Run production read-only health and recent worker logs; compare watermark and latest-event timestamps.
- [ ] Request an independent code review and resolve all critical/important findings.

