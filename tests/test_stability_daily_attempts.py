"""Trend-chart daily upstream-attempt metrics from the SQL aggregate branch.

The stability overview can be served from compact SQL aggregates
(stability_overview_aggregates) instead of per-event Python math. The daily
rows of that aggregate branch originally carried no attempt-level counters,
so the trend chart's upstream-exception line (blue) was always missing even
when the attempt event table had data. These tests pin the fix: daily
attempt counters are joined back per day, and days without attempt data keep
the "no data" (None) semantics instead of drawing a fake zero.
"""

from __future__ import annotations

import asyncio
from datetime import date
from unittest import mock

import backend.main as main
from backend.main import _build_stability_overview, _stability_metrics_from_aggregate


class _FakeAggregateStore:
    def __init__(self) -> None:
        self.daily = [
            {"dimension": date(2026, 8, 18), "request_count": 50, "status_count": 50,
             "explicit_count": 50, "explicit_failure_count": 2,
             "failure_known_count": 50, "failure_count": 2,
             "retry_known_count": 50, "retry_count": 3,
             "ttft_sample_count": 50, "ttft_p95_ms": 1200.0,
             "collected_at": None},
            {"dimension": date(2026, 8, 19), "request_count": 60, "status_count": 60,
             "explicit_count": 60, "explicit_failure_count": 5,
             "failure_known_count": 60, "failure_count": 5,
             "retry_known_count": 60, "retry_count": 4,
             "ttft_sample_count": 60, "ttft_p95_ms": 1500.0,
             "collected_at": None},
        ]
        self.daily_attempts = [
            {"dimension": date(2026, 8, 19), "attempt_count": 48,
             "attempt_status_count": 48, "failed_attempt_count": 9},
        ]

    async def stability_overview_aggregates(self, start_date: str, end_date: str, model: str = "") -> dict:
        return {
            "overall": {"request_count": 110, "status_count": 110,
                        "explicit_count": 110, "explicit_failure_count": 7,
                        "failure_known_count": 110, "failure_count": 7,
                        "retry_known_count": 110, "retry_count": 7,
                        "ttft_sample_count": 110, "ttft_p95_ms": 1350.0,
                        "collected_at": None},
            "attempts": {"attempt_count": 90, "attempt_status_count": 90,
                         "failed_attempt_count": 12,
                         "fallback_count": 2, "fallback_recovered_count": 1,
                         "retry_count": 3, "retry_recovered_count": 2,
                         "available_from": None},
            "daily": self.daily,
            "dailyAttempts": self.daily_attempts,
            "models": [],
            "modelAttempts": [],
            "scenarios": [],
        }

    async def stability_sync_states(self) -> list:
        return []

    async def list_stability_actions(self, model: str = "") -> list:
        return []

    async def list_stability_regressions(self) -> list:
        return []


def test_daily_attempt_counters_join_into_trend_days() -> None:
    store = _FakeAggregateStore()
    with mock.patch.object(main, "_admin_observability_store", return_value=store), \
         mock.patch.object(main, "usage_backend_ids", return_value=set()):
        payload = asyncio.run(_build_stability_overview("2026-08-13", "2026-08-19", ""))
    daily = {row["date"]: row for row in payload["data"]["daily"]}

    assert daily["2026-08-19"]["upstreamExceptionCount"] == 9
    assert daily["2026-08-19"]["upstreamAttemptCount"] == 48
    assert daily["2026-08-19"]["upstreamExceptionRate"] == 9 / 48
    # Day without attempt events keeps the "no data" semantics: never a fake 0.
    assert daily["2026-08-18"]["upstreamExceptionCount"] is None
    assert daily["2026-08-18"]["upstreamAttemptCount"] is None


def test_aggregate_metrics_without_attempts_stay_none() -> None:
    row = {"request_count": 10, "status_count": 10, "explicit_count": 10,
           "explicit_failure_count": 1, "failure_known_count": 10,
           "failure_count": 1, "retry_known_count": 10, "retry_count": 0,
           "ttft_sample_count": 10, "ttft_p95_ms": 500.0, "collected_at": None}
    metrics = _stability_metrics_from_aggregate(
        row, None, period={"startDate": "2026-08-13", "endDate": "2026-08-19"}, as_of="2026-08-19"
    )
    assert metrics["upstreamExceptionCount"] is None
    assert metrics["upstreamExceptionRate"] is None

    metrics = _stability_metrics_from_aggregate(
        row, {"attempt_count": 8, "attempt_status_count": 8, "failed_attempt_count": 3},
        period={"startDate": "2026-08-13", "endDate": "2026-08-19"}, as_of="2026-08-19"
    )
    assert metrics["upstreamExceptionCount"] == 3
    assert metrics["upstreamExceptionRate"] == 3 / 8
