import asyncio
import inspect
from pathlib import Path
from unittest.mock import patch

import backend.main as main
from backend.main import (
    _build_stability_overview,
    _cached_observability_dashboard,
    _observability_pending_payload,
)


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


def test_stability_overview_builder_does_not_scan_full_spend_logs() -> None:
    source = inspect.getsource(_build_stability_overview)
    assert "fetch_rows(" not in source
    assert "stability_spendlog_events" not in source
    assert "build_error_governance(" not in source
    assert "stability_overview_aggregates" in source


def test_stability_overview_skips_litellm_reader_and_mirror_row_scan() -> None:
    class Store:
        def __init__(self) -> None:
            self.mirror_calls = 0
            self.aggregate_calls = 0

        async def stability_spendlog_events(self, start_date: str, end_date: str, model: str = ""):
            self.mirror_calls += 1
            raise AssertionError("overview must not load mirrored spend-log rows")

        async def stability_overview_aggregates(self, start_date: str, end_date: str, model: str = ""):
            self.aggregate_calls += 1
            return {
                "overall": {"request_count": 2, "status_count": 2, "failure_known_count": 2, "failure_count": 0, "ttft_sample_count": 0},
                "daily": [],
                "models": [],
                "modelAttempts": [],
                "scenarios": [],
                "attempts": {},
                "dailyAttempts": [],
            }

        async def stability_sync_states(self):
            return []

    class Reader:
        pool = object()

        async def fetch_rows(self, start_date: str, end_date: str, model: str = ""):
            raise AssertionError("overview must not scan LiteLLM spend logs")

    store = Store()
    with patch.object(main, "_admin_observability_store", return_value=store), \
         patch.object(main, "_litellm_stability_reader", Reader()), \
         patch.object(main, "usage_backend_ids", return_value=set()):
        payload = asyncio.run(_build_stability_overview("2026-08-13", "2026-08-19", ""))

    assert store.mirror_calls == 0
    assert store.aggregate_calls == 1
    assert payload["data"]["overview"]["requestCount"] == 2
    assert payload["data"]["modelRankings"] == []
    assert payload["data"]["topScenarios"] == []
