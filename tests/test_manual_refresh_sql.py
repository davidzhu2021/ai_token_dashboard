import asyncio
from typing import Any

from backend import main


def test_manual_refresh_skips_upstream_sync(monkeypatch) -> None:
    called = False

    async def fail_if_called(days: int) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(main, "run_usage_sync", fail_if_called)
    monkeypatch.setattr(main, "trigger_usage_refresh", fail_if_called)

    asyncio.run(main.prepare_usage_refresh("2026-07-01", "2026-07-23", force=True))

    assert called is False


def test_normal_usage_refresh_stays_background(monkeypatch) -> None:
    calls: list[tuple[str, str, bool]] = []

    def record_refresh(start_date: str, end_date: str, force: bool = False) -> None:
        calls.append((start_date, end_date, force))

    monkeypatch.setattr(main, "trigger_usage_refresh", record_refresh)

    asyncio.run(main.prepare_usage_refresh("2026-07-01", "2026-07-23", force=False))

    assert calls == [("2026-07-01", "2026-07-23", False)]


def test_manual_refresh_database_unavailable_is_not_upstream_fallback() -> None:
    error = main.manual_refresh_database_unavailable()

    assert error.status_code == 503


def test_personal_manual_refresh_reads_sql_and_repopulates_cache(monkeypatch) -> None:
    class FakeStore:
        async def connect(self) -> None:
            return None

        async def personal_rows(self, email: str, start_date: str, end_date: str, source: str, backend_ids: list[str]) -> dict[str, Any]:
            return {
                "rows": [{"date": end_date, "source": "Codex", "model": "gpt-test", "totalTokens": 12}],
                "lastSyncedAt": None,
            }

    async def fail_upstream(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("manual refresh must not call upstream")

    fake_store = FakeStore()
    monkeypatch.setattr(main, "usage_store", lambda: fake_store)
    monkeypatch.setattr(main, "usage_backend_ids", lambda: ["primary"])
    monkeypatch.setattr(main, "cached_resolve_user", fail_upstream)
    main.personal_usage_cache.clear()
    user = {"email": "user@example.com", "name": "User"}

    payload = asyncio.run(main.personal_usage_payload(user, "2026-07-01", "2026-07-23", "all", refresh=True))

    assert payload["rows"][0]["totalTokens"] == 12
    assert payload["cache"]["hit"] is False
    cached = asyncio.run(main.personal_usage_payload(user, "2026-07-01", "2026-07-23", "all"))
    assert cached["cache"]["hit"] is True
