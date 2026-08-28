import asyncio
from pathlib import Path
from unittest.mock import patch

from backend.main import _cached_observability_dashboard, _observability_pending_payload


ROOT = Path(__file__).resolve().parents[1]
USAGE_STORE = (ROOT / "backend" / "usage_store.py").read_text(encoding="utf-8")


def test_stability_terminal_attempt_query_projects_only_required_columns():
    """The large stability CTE must not materialize every attempt-event column."""

    start = USAGE_STORE.index("terminal_attempts = f\"\"\"")
    end = USAGE_STORE.index("attempt_summary_query = pool.fetchrow", start)
    query = USAGE_STORE[start:end]

    assert "SELECT DISTINCT ON" in query
    assert "SELECT DISTINCT ON (\n                backend_id" in query
    assert ")\n                backend_id, event_id, event_date" in query
    assert ") *" not in query


def test_stability_overview_includes_spendlog_error_code_aggregate_query():
    start = USAGE_STORE.index("async def stability_overview_aggregates")
    end = USAGE_STORE.index("async def stability_scenario_samples", start)
    query = USAGE_STORE[start:end]

    assert "error_code" in query
    assert "GROUP BY error_code" in query
    assert "error_code_count" in query


def test_stability_cold_placeholder_is_shape_compatible_and_non_error():
    payload = _observability_pending_payload(
        "stability",
        {"startDate": "2026-08-22", "endDate": "2026-08-28", "model": ""},
    )

    assert payload["cache"]["state"] == "refreshing"
    assert payload["freshness"]["status"] == "pending"
    assert payload["coverage"]["incomplete"] is True
    assert payload["data"]["overview"] == {}
    assert payload["data"]["daily"] == []


def test_stability_cold_request_returns_before_refresh_task_finishes():
    async def run() -> None:
        task = asyncio.create_task(asyncio.sleep(60))

        async def optional(_store, names, *args, default=None, **kwargs):
            return None

        async def start(_dashboard_type, _snapshot_key, _builder):
            return task

        try:
            with patch("backend.main._call_store_optional", optional), patch(
                "backend.main._start_observability_refresh", start
            ):
                payload = await asyncio.wait_for(
                    _cached_observability_dashboard(
                        "stability",
                        {"startDate": "2026-08-22", "endDate": "2026-08-28", "model": ""},
                        lambda: None,
                    ),
                    timeout=0.5,
                )
            assert payload["cache"]["state"] == "refreshing"
            assert payload["freshness"]["status"] == "pending"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
