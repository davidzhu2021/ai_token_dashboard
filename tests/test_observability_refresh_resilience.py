from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from backend import main


class Store:
    def __init__(self, record=None) -> None:
        self.record = record

    async def get_observability_snapshot(self, *_args):
        return self.record

    async def save_observability_snapshot(self, _kind, _key, payload, *, data_revision=""):
        self.record = {
            "payload": payload,
            "generated_at": datetime.now(timezone.utc),
            "data_revision": data_revision,
            "last_refresh_error": "",
        }
        return self.record

    async def snapshot_state(self):
        return {"revision": "r1"}

    async def mark_observability_snapshot_refresh(self, *_args, **_kwargs):
        return None


def test_stability_refresh_uses_120_second_default_timeout(monkeypatch):
    async def run():
        store = Store()
        monkeypatch.setattr(main, "_admin_observability_store", lambda: store)
        monkeypatch.delenv("STABILITY_REFRESH_TIMEOUT_SECONDS", raising=False)
        seen = []
        monkeypatch.setattr(main, "env_int", lambda name, default: (seen.append((name, default)) or default))
        main._observability_refresh_tasks.clear()
        main._observability_memory_snapshots.clear()
        started = asyncio.Event()

        async def builder():
            started.set()
            await asyncio.sleep(0.05)
            return {"data": {"ok": True}}

        task = await main._start_observability_refresh("stability", "k", builder)
        await started.wait()
        result = await task
        assert result["data"]["ok"] is True
        assert ("STABILITY_REFRESH_TIMEOUT_SECONDS", 120) in seen

    asyncio.run(run())


def test_stability_refreshes_are_globally_single_slot(monkeypatch):
    async def run():
        store = Store()
        monkeypatch.setattr(main, "_admin_observability_store", lambda: store)
        main._observability_refresh_tasks.clear()
        main._observability_memory_snapshots.clear()
        active = 0
        max_active = 0
        release = asyncio.Event()

        async def builder():
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await release.wait()
            active -= 1
            return {"data": {"ok": True}}

        first = await main._start_observability_refresh("stability", "a", builder)
        second = await main._start_observability_refresh("stability", "b", builder)
        await asyncio.sleep(0.05)
        assert max_active == 1
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(run())


def test_cold_refresh_failure_returns_pending_envelope_with_error_type(monkeypatch):
    async def run():
        store = Store()
        monkeypatch.setattr(main, "_admin_observability_store", lambda: store)
        main._observability_refresh_tasks.clear()
        main._observability_memory_snapshots.clear()
        main._observability_refresh_failures.clear()

        async def builder():
            raise TimeoutError("slow")

        result = await main._cached_observability_dashboard(
            "stability", {"startDate": "2026-09-01", "endDate": "2026-09-07", "model": ""}, builder
        )
        task = next(iter(main._observability_refresh_tasks.values()))
        await asyncio.gather(task, return_exceptions=True)
        failed = await main._cached_observability_dashboard(
            "stability", {"startDate": "2026-09-01", "endDate": "2026-09-07", "model": ""}, builder
        )
        assert result["freshness"]["status"] == "pending"
        assert failed["cache"]["lastRefreshError"] == "TimeoutError"

    asyncio.run(run())


def test_iso_generated_at_is_used_for_snapshot_age(monkeypatch):
    async def run():
        generated = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        store = Store({"payload": {"data": {"value": 1}}, "generated_at": generated, "last_refresh_error": ""})
        monkeypatch.setattr(main, "_admin_observability_store", lambda: store)
        main._observability_memory_snapshots.clear()
        async def builder():
            return {"data": {"value": 2}}
        result = await main._cached_observability_dashboard("cost", {"month": "2026-09"}, builder)
        assert result["data"]["value"] == 1
        assert result["cache"]["ageSeconds"] is not None

    asyncio.run(run())


def test_stale_snapshot_refresh_failure_preserves_payload_and_exposes_error(monkeypatch):
    async def run():
        generated = datetime.now(timezone.utc) - timedelta(seconds=120)
        original = {
            "payload": {"data": {"value": 1}},
            "generated_at": generated,
            "data_revision": "r1",
            "last_refresh_error": "",
        }
        store = Store(original)
        monkeypatch.setattr(main, "_admin_observability_store", lambda: store)
        monkeypatch.setattr(
            main,
            "env_int",
            lambda name, default: 30 if name == "OBSERVABILITY_CACHE_FRESH_SECONDS" else default,
        )
        main._observability_refresh_tasks.clear()
        main._observability_memory_snapshots.clear()
        main._observability_refresh_failures.clear()

        async def builder():
            raise TimeoutError("refresh timed out")

        first = await main._cached_observability_dashboard("cost", {"month": "2026-09"}, builder)
        assert first["data"]["value"] == 1
        task = next(iter(main._observability_refresh_tasks.values()))
        await asyncio.gather(task, return_exceptions=True)
        second = await main._cached_observability_dashboard("cost", {"month": "2026-09"}, builder)
        assert second["data"]["value"] == 1
        assert second["cache"]["lastRefreshError"] == "TimeoutError"

    asyncio.run(run())


def test_recent_refresh_failure_backoff_reuses_snapshot_without_restarting_builder(monkeypatch):
    async def run():
        generated = datetime.now(timezone.utc) - timedelta(seconds=120)
        store = Store({
            "payload": {"data": {"value": 1}},
            "generated_at": generated,
            "data_revision": "r1",
            "last_refresh_error": "TimeoutError",
        })
        monkeypatch.setattr(main, "_admin_observability_store", lambda: store)
        main._observability_refresh_tasks.clear()
        main._observability_memory_snapshots.clear()
        main._observability_refresh_failures.clear()
        key_payload = {"month": "2026-09"}
        snapshot_key = main._observability_snapshot_key(key_payload)
        main._observability_refresh_failures[f"cost:{snapshot_key}"] = (
            asyncio.get_running_loop().time(),
            "TimeoutError",
        )
        calls = 0

        async def builder():
            nonlocal calls
            calls += 1
            return {"data": {"value": 2}}

        result = await main._cached_observability_dashboard("cost", key_payload, builder)
        assert result["data"]["value"] == 1
        assert result["cache"]["lastRefreshError"] == "TimeoutError"
        assert calls == 0

    asyncio.run(run())


def test_stability_overview_logs_window_model_counts_and_timing(monkeypatch):
    class AggregateStore(Store):
        async def stability_overview_aggregates(self, *_args):
            return {
                "overall": {"request_count": 3},
                "attempts": {},
                "dailyAttempts": [], "daily": [], "modelAttempts": [],
                "models": [], "scenarios": [],
            }

        async def stability_sync_states(self):
            return []

    async def run():
        monkeypatch.setattr(main, "_admin_observability_store", lambda: AggregateStore())
        monkeypatch.setattr(main, "usage_backend_ids", lambda: set())
        messages = []
        monkeypatch.setattr(main.logger, "info", lambda message, *args: messages.append(message % args if args else message))
        await main._build_stability_overview("2026-09-01", "2026-09-07", "gpt-5")
        rendered = "\n".join(messages)
        assert "start_date=2026-09-01" in rendered
        assert "end_date=2026-09-07" in rendered
        assert "model=gpt-5" in rendered
        assert "eventCount=3" in rendered
        assert "stage_ms=" in rendered

    asyncio.run(run())
