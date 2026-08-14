from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from backend import main


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
        main._observability_refresh_tasks.clear()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def builder():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {"data": {"value": 2}}

        first, second = await asyncio.gather(
            main._cached_observability_dashboard("cost", {"month": "2026-08"}, builder),
            main._cached_observability_dashboard("cost", {"month": "2026-08"}, builder),
        )
        await started.wait()
        assert first["data"]["value"] == second["data"]["value"] == 1
        assert first["cache"]["refreshing"] is True
        assert calls == 1
        release.set()
        await asyncio.gather(*list(main._observability_refresh_tasks.values()))

    asyncio.run(run())


def test_cost_frontend_uses_single_overview_request() -> None:
    source = open("assets/app.js", encoding="utf-8").read()
    start = source.index("async function loadCostOverview")
    end = source.index("function focusDrawer", start)
    loader = source[start:end]
    assert "/api/admin/costs/overview?" in loader
    assert "/api/admin/costs/annual?" not in loader
    assert 'api("/api/admin/costs/budgets")' not in loader


def test_governance_workbench_does_not_preload_full_overviews() -> None:
    source = open("assets/app.js", encoding="utf-8").read()
    start = source.index('if (view === "governance-workbench")')
    end = source.index("function observabilityCapabilities", start)
    block = source[start:end]
    assert "loadGovernanceWorkbench()" in block
    assert "loadStabilityOverview()" not in block
    assert "loadCostOverview()" not in block
