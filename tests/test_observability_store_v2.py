from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import pytest

from backend.usage_store import USAGE_SCHEMA, UsageStore


def run(coro):
    return asyncio.run(coro)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)

    async def fetch(self, sql, *args):
        return await self.connection.fetch(sql, *args)


def test_schema_contains_auditable_stability_and_cost_entities() -> None:
    for table in (
        "stability_attempt_events",
        "stability_actions",
        "stability_regressions",
        "cost_plan_versions",
        "savings_measurements",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in USAGE_SCHEMA
    assert "cost_plan_versions_active_baseline_year_idx" in USAGE_SCHEMA
    assert "plan_version_id TEXT" in USAGE_SCHEMA
    assert "source_evidence TEXT" in USAGE_SCHEMA
    assert "SET status='pending_evidence'" in USAGE_SCHEMA


def test_attempt_event_record_is_content_free_and_derives_retry_fallback_flags() -> None:
    record = UsageStore._stability_attempt_record(
        {
            "backend_id": "primary",
            "event_id": "evt-1",
            "request_id": "req-1",
            "trace_id": "trace-1",
            "attempt_index": 2,
            "retry_index": 1,
            "requested_model_group": "gpt",
            "actual_model": "gpt-5",
            "route": "route-a",
            "event_type": "fallback_success",
            "status": "success",
            "started_at": "2026-08-12T00:00:00Z",
            "ended_at": "2026-08-12T00:00:01Z",
            "fallback_from": "route-old",
            "fallback_to": "route-a",
            "prompt": "must not be stored",
            "response": "must not be stored",
        },
        datetime(2026, 8, 12, 0, 0, 2, tzinfo=timezone.utc),
    )

    assert record[0:5] == ("primary", "evt-1", "req-1", "trace-1", "")
    assert record[26] is True
    assert record[27] is True
    assert "must not be stored" not in record


def test_actual_cost_items_force_actual_status_and_as_of_cutoff() -> None:
    class Connection:
        async def fetch(self, sql, *args):
            self.sql = sql
            self.args = args
            return []

    connection = Connection()
    store = UsageStore("postgresql://unused")
    store.pool = _Pool(connection)

    run(store.list_actual_cost_items("2026-08-12", model="gpt-5"))

    assert "service_start_date <= $1::date" in connection.sql
    assert "recognition_status=$6" in connection.sql
    assert connection.args[0] == date(2026, 8, 12)
    assert connection.args[1] == "gpt-5"
    assert connection.args[5] == "actual"


def test_activate_plan_rejects_non_approved_or_incomplete_plan() -> None:
    class Connection:
        def transaction(self):
            return _Transaction()

        async def fetchrow(self, sql, *args):
            return {
                "id": "plan-1",
                "year": 2026,
                "status": "approved",
                "scenario": "baseline",
                "coverage_complete": False,
            }

    store = UsageStore("postgresql://unused")
    store.pool = _Pool(Connection())

    with pytest.raises(ValueError, match="覆盖不完整"):
        run(store.activate_cost_plan_version("plan-1", "finance@example.com"))


def test_verified_savings_requires_evidence_and_reviewer() -> None:
    data = {
        "scope": "provider:a",
        "baselineStart": "2026-07-01",
        "baselineEnd": "2026-07-31",
        "measurementStart": "2026-08-01",
        "measurementEnd": "2026-08-12",
        "baselineAmountUsd": 100,
        "actualAmountUsd": 80,
        "status": "reviewed",
    }

    with pytest.raises(ValueError, match="证据链接和财务复核人"):
        UsageStore._savings_measurement_values(data)


def test_verified_savings_rejects_overlapping_scope() -> None:
    class Connection:
        async def fetchval(self, sql, *args):
            self.sql = sql
            self.args = args
            return True

    values = UsageStore._savings_measurement_values(
        {
            "scope": "provider:a",
            "baselineStart": "2026-07-01",
            "baselineEnd": "2026-07-31",
            "measurementStart": "2026-08-01",
            "measurementEnd": "2026-08-12",
            "baselineAmountUsd": 100,
            "actualAmountUsd": 80,
            "status": "reviewed",
            "evidenceUrl": "https://example.test/evidence",
            "financeReviewer": "finance@example.com",
        }
    )

    with pytest.raises(ValueError, match="重叠"):
        run(UsageStore._assert_savings_measurement_not_overlapping(Connection(), values, "measurement-2"))
