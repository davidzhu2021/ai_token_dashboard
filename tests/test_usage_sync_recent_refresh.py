import asyncio

from backend import main


def test_run_usage_sync_uses_recent_refresh_guard(monkeypatch) -> None:
    class Store:
        async def connect(self):
            return None

    result = {
        "status": "ok",
        "rowCount": 100,
        "backendCount": 2,
        "errors": [],
        "recentRefresh": {"days": 3, "status": "ok"},
    }
    calls = []

    async def guarded_sync(client, store, days):
        calls.append((client, store, days))
        return result

    client = object()
    store = Store()
    monkeypatch.setattr(main, "usage_store", lambda: store)
    monkeypatch.setattr(main, "client", lambda: client)
    monkeypatch.setattr(main, "run_sync_with_recent_refresh", guarded_sync)
    monkeypatch.setattr(main, "_usage_sync_status", {"status": "ready", "lastRun": None})

    assert asyncio.run(main.run_usage_sync(90)) == result
    assert calls == [(client, store, 90)]
    assert main._usage_sync_status["status"] == "ok"

