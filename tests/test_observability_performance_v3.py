from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from backend import main
from backend.usage_store import UsageStore


class SnapshotStore:
    def __init__(self, record=None) -> None:
        self.record = record
        self.saved = []

    async def get_observability_snapshot(self, dashboard_type, snapshot_key):
        return self.record

    async def save_observability_snapshot(self, dashboard_type, snapshot_key, payload, *, data_revision=""):
        self.saved.append(payload)
        self.record = {
            "payload": payload,
            "generated_at": datetime.now(timezone.utc),
            "data_revision": data_revision,
            "last_refresh_error": "",
        }
        return self.record

    async def mark_observability_snapshot_refresh(self, *args, **kwargs):
        return None

    async def snapshot_state(self):
        return {"revision": "r1"}


def test_fresh_snapshot_returns_without_rebuild(monkeypatch) -> None:
    async def run() -> None:
        store = SnapshotStore({
            "payload": {"data": {"value": 1}},
            "generated_at": datetime.now(timezone.utc),
            "data_revision": "r1",
            "last_refresh_error": "",
        })
        monkeypatch.setattr(main, "_admin_observability_store", lambda: store)
        main._observability_memory_snapshots.clear()
        calls = 0

        async def builder():
            nonlocal calls
            calls += 1
            return {"data": {"value": 2}}

        result = await main._cached_observability_dashboard("cost", {"month": "2026-08"}, builder)
        assert result["data"]["value"] == 1
        assert result["cache"]["state"] == "fresh"
        assert calls == 0

    asyncio.run(run())


def test_stale_snapshot_is_returned_and_refresh_is_singleflight(monkeypatch) -> None:
    async def run() -> None:
        store = SnapshotStore({
            "payload": {"data": {"value": 1}},
            "generated_at": datetime.now(timezone.utc) - timedelta(seconds=120),
            "data_revision": "r1",
            "last_refresh_error": "",
        })
        monkeypatch.setattr(main, "_admin_observability_store", lambda: store)
        original_env_int = main.env_int
        monkeypatch.setattr(
            main,
            "env_int",
            lambda name, default: 30 if name == "OBSERVABILITY_CACHE_FRESH_SECONDS" else original_env_int(name, default),
        )
        main._observability_refresh_tasks.clear()
        main._observability_memory_snapshots.clear()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def builder():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {"data": {"value": 2}}

        try:
            first, second = await asyncio.gather(
                main._cached_observability_dashboard("cost", {"month": "2026-08"}, builder),
                main._cached_observability_dashboard("cost", {"month": "2026-08"}, builder),
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            assert first["data"]["value"] == second["data"]["value"] == 1
            assert first["cache"]["refreshing"] is True
            assert calls == 1
            release.set()
        finally:
            tasks = list(main._observability_refresh_tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())


def test_cost_cold_request_returns_pending_without_waiting_for_builder(monkeypatch) -> None:
    async def run() -> None:
        store = SnapshotStore()
        monkeypatch.setattr(main, "_admin_observability_store", lambda: store)
        main._observability_refresh_tasks.clear()
        main._observability_memory_snapshots.clear()

        async def builder():
            await asyncio.sleep(60)
            return {"data": {"value": 9}}

        result = await asyncio.wait_for(
            main._cached_observability_dashboard("cost", {"month": "2026-08"}, builder),
            timeout=0.5,
        )
        assert result["cache"]["state"] == "refreshing"
        assert result["freshness"]["status"] == "pending"
        task = next(iter(main._observability_refresh_tasks.values()))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_cost_frontend_uses_single_overview_request() -> None:
    source = open("assets/app.js", encoding="utf-8").read()
    start = source.index("async function loadCostOverview")
    end = source.index("function focusDrawer", start)
    loader = source[start:end]
    assert "/api/admin/costs/overview?" in loader
    assert "/api/admin/costs/annual?" not in loader
    assert 'api("/api/admin/costs/budgets")' not in loader


def test_cost_frontend_retries_pending_snapshot() -> None:
    source = open("assets/app.js", encoding="utf-8").read()
    start = source.index("async function loadCostOverview")
    end = source.index("function focusDrawer", start)
    loader = source[start:end]
    assert "STABILITY_OVERVIEW_MAX_RETRIES" in loader
    assert 'nextOverview?.cache?.state === "refreshing"' in loader
    assert "setTimeout" in loader


def test_observability_warmup_uses_default_seven_day_windows(monkeypatch) -> None:
    async def run() -> None:
        store = SnapshotStore()
        monkeypatch.setattr(main, "_admin_observability_store", lambda: store)
        monkeypatch.setattr(main, "env_bool", lambda name, default=False: True if name == "ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED" else default)
        monkeypatch.setattr(main, "usage_today", lambda: datetime(2026, 9, 10, tzinfo=timezone.utc).date())
        main._observability_refresh_tasks.clear()
        main._observability_memory_snapshots.clear()
        started: list[tuple[str, dict]] = []

        async def cached(dashboard_type, key_payload, builder, *, refresh=False):
            started.append((dashboard_type, dict(key_payload)))
            await builder()
            return {"cache": {"state": "fresh"}}

        monkeypatch.setattr(main, "_cached_observability_dashboard", cached)
        monkeypatch.setattr(main, "_build_stability_overview", lambda *args, **kwargs: asyncio.sleep(0, result={"ok": True}))
        monkeypatch.setattr(main, "_build_costs_overview", lambda *args, **kwargs: asyncio.sleep(0, result={"ok": True}))
        await main.warmup_default_observability_snapshots()
        kinds = {item[0] for item in started}
        assert kinds == {"stability", "cost"}
        stability_key = next(item[1] for item in started if item[0] == "stability")
        cost_key = next(item[1] for item in started if item[0] == "cost")
        assert stability_key["startDate"] == "2026-09-04"
        assert stability_key["endDate"] == "2026-09-10"
        assert cost_key["startDate"] == "2026-09-04"
        assert cost_key["endDate"] == "2026-09-10"
        assert cost_key["asOf"] == "2026-09-10"

    asyncio.run(run())


def test_observability_warmup_is_skipped_without_usage_store(monkeypatch) -> None:
    monkeypatch.setattr(main, "env_bool", lambda name, default=False: True if name == "ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED" else default)
    monkeypatch.setattr(main, "usage_store", lambda: None)
    main._observability_warmup_task = None
    main.start_observability_warmup()
    assert main._observability_warmup_task is None


def test_governance_workbench_does_not_preload_full_overviews() -> None:
    source = open("assets/app.js", encoding="utf-8").read()
    start = source.index('if (view === "governance-workbench")')
    end = source.index("function observabilityCapabilities", start)
    block = source[start:end]
    assert "loadGovernanceWorkbench()" in block
    assert "loadStabilityOverview()" not in block
    assert "loadCostOverview()" not in block


def test_stability_cold_budget_uses_stability_specific_override(monkeypatch) -> None:
    async def run() -> None:
        store = SnapshotStore()
        monkeypatch.setattr(main, "_admin_observability_store", lambda: store)
        monkeypatch.setattr(main, "env_int", lambda name, default: 2200 if name == "STABILITY_COLD_QUERY_BUDGET_MS" else default)
        main._observability_refresh_tasks.clear()
        main._observability_memory_snapshots.clear()

        async def builder():
            return {"data": {"value": 3}}

        result = await asyncio.wait_for(
            main._cached_observability_dashboard("stability", {"day": "2026-08-25"}, builder),
            timeout=0.5,
        )
        assert result["cache"]["state"] == "refreshing"
        assert result["freshness"]["status"] == "pending"
        task = next(iter(main._observability_refresh_tasks.values()))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_refresh_claim_casts_timestamp_and_interval_parameters() -> None:
    class Pool:
        query = ""

        async def fetch(self, query, *_args):
            self.query = query
            return []

    async def run() -> None:
        store = UsageStore("postgresql://unused")
        pool = Pool()
        store.pool = pool
        await store.claim_refresh_requests()
        assert "$1::timestamptz" in pool.query
        assert "$2::double precision * INTERVAL '1 second'" in pool.query

    asyncio.run(run())
