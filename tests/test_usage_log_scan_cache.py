import asyncio
from typing import Any

import backend.main as main
from backend.cache import TTLCache
from backend.litellm_client import LiteLLMBackend, LiteLLMClient


def make_client() -> tuple[LiteLLMClient, LiteLLMBackend]:
    client = object.__new__(LiteLLMClient)
    backend = LiteLLMBackend(id="primary", label="Primary", base_url="https://primary.test", admin_key="primary-key")
    client.backends = [backend]
    client._backend_map = {backend.id: backend}
    client._spend_log_scan_cache = TTLCache()
    client._spend_log_scan_tasks = {}
    return client, backend


def test_usage_boards_reuse_spend_log_scan_cache(monkeypatch) -> None:
    client, backend = make_client()
    calls = 0

    async def fake_request_backend(_backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        nonlocal calls
        assert _backend == backend
        assert method == "GET"
        assert path == "/spend/logs/v2"
        calls += 1
        return {
            "logs": [
                {
                    "user": "user-a",
                    "team_id": "team-a",
                    "startTime": "2026-06-15T10:00:00Z",
                    "model": "gpt-4o",
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                    "spend": 0.12,
                    "status": "success",
                }
            ],
            "total_pages": 1,
            "total": 1,
        }

    async def fake_users(_backend: LiteLLMBackend | None = None) -> list[dict[str, Any]]:
        return [{"user_id": "user-a", "user_email": "alice@example.com", "user_alias": "Alice"}]

    async def fake_team_map(_backend: LiteLLMBackend | None = None) -> dict[str, dict[str, str]]:
        return {"team-a": {"id": "team-a", "name": "Team A"}}

    async def fake_teams(_backend: LiteLLMBackend | None = None, include_details: bool = True) -> list[dict[str, Any]]:
        return [{"team_id": "team-a", "team_alias": "Team A", "members_with_roles": [{"user_id": "user-a"}]}]

    async def empty_admin_daily_activity(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(client, "request_backend", fake_request_backend)
    monkeypatch.setattr(client, "users", fake_users)
    monkeypatch.setattr(client, "team_map", fake_team_map)
    monkeypatch.setattr(client, "teams", fake_teams)
    monkeypatch.setattr(client, "admin_daily_activity_rows", empty_admin_daily_activity)
    monkeypatch.setattr(client, "_team_daily_activity_rows", empty_admin_daily_activity)

    admin_payload = asyncio.run(client.admin_usage_rows("2026-06-01", "2026-06-30", "all"))
    department_payload = asyncio.run(client.admin_department_usage_rows("2026-06-01", "2026-06-30", "all"))
    team_payload = asyncio.run(client.team_usage_rows("primary", "team-a", "2026-06-01", "2026-06-30", "all"))

    assert calls == 1
    assert admin_payload["employees"][0]["totalTokens"] == 150
    assert department_payload["departments"][0]["departmentId"] == "team-a"
    assert team_payload["employees"][0]["employeeEmail"] == "alice@example.com"


def test_usage_log_scan_refresh_bypasses_cache(monkeypatch) -> None:
    client, backend = make_client()
    calls = 0

    async def fake_request_backend(_backend: LiteLLMBackend, _method: str, path: str, **_kwargs: Any) -> Any:
        nonlocal calls
        assert _backend == backend
        assert path == "/spend/logs/v2"
        calls += 1
        return {"logs": [], "total_pages": 1, "total": 0}

    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    asyncio.run(client._spend_log_scan_rows("2026-06-01", "2026-06-30", "all"))
    asyncio.run(client._spend_log_scan_rows("2026-06-01", "2026-06-30", "all"))
    asyncio.run(client._spend_log_scan_rows("2026-06-01", "2026-06-30", "all", refresh=True))

    assert calls == 2


def test_usage_log_scan_reads_pages_concurrently_after_first_page(monkeypatch) -> None:
    client, backend = make_client()
    captured_pages: list[int] = []

    async def fake_request_backend(_backend: LiteLLMBackend, _method: str, path: str, **kwargs: Any) -> Any:
        assert _backend == backend
        assert path == "/spend/logs/v2"
        page = int(kwargs.get("params", {}).get("page", 1))
        captured_pages.append(page)
        return {
            "logs": [
                {
                    "user": f"user-{page}",
                    "startTime": f"2026-06-{page:02d}T10:00:00Z",
                    "model": "gpt-4o",
                    "total_tokens": page,
                }
            ],
            "total_pages": 3,
            "total": 3,
        }

    monkeypatch.setattr(client, "request_backend", fake_request_backend)
    monkeypatch.setenv("ADMIN_USAGE_LOG_MAX_PAGES", "30")
    monkeypatch.setenv("SPEND_LOG_SCAN_PAGE_CONCURRENCY", "2")

    payload = asyncio.run(client._spend_log_scan_rows("2026-06-01", "2026-06-30", "all"))

    assert captured_pages == [1, 2, 3]
    assert [entry["log"]["user"] for entry in payload["entries"]] == ["user-1", "user-2", "user-3"]
    assert payload["pagesRead"] == 3
    assert payload["totalPages"] == 3
    assert payload["truncated"] is False


def test_usage_log_scan_respects_page_limit(monkeypatch) -> None:
    client, backend = make_client()
    captured_pages: list[int] = []

    async def fake_request_backend(_backend: LiteLLMBackend, _method: str, path: str, **kwargs: Any) -> Any:
        assert _backend == backend
        assert path == "/spend/logs/v2"
        page = int(kwargs.get("params", {}).get("page", 1))
        captured_pages.append(page)
        return {
            "logs": [{"user": f"user-{page}", "startTime": "2026-06-15T10:00:00Z", "model": "gpt-4o", "total_tokens": 1}],
            "total_pages": 5,
            "total": 5,
        }

    monkeypatch.setattr(client, "request_backend", fake_request_backend)
    monkeypatch.setenv("ADMIN_USAGE_LOG_MAX_PAGES", "2")

    payload = asyncio.run(client._spend_log_scan_rows("2026-06-01", "2026-06-30", "all"))

    assert captured_pages == [1, 2]
    assert payload["pagesRead"] == 2
    assert payload["totalPages"] == 5
    assert payload["truncated"] is True


def test_usage_log_scan_merges_in_flight_requests(monkeypatch) -> None:
    client, backend = make_client()
    calls = 0

    async def fake_request_backend(_backend: LiteLLMBackend, _method: str, path: str, **_kwargs: Any) -> Any:
        nonlocal calls
        assert _backend == backend
        assert path == "/spend/logs/v2"
        calls += 1
        await asyncio.sleep(0.01)
        return {
            "logs": [{"user": "user-a", "startTime": "2026-06-15T10:00:00Z", "model": "gpt-4o", "total_tokens": 1}],
            "total_pages": 1,
            "total": 1,
        }

    async def run_scan_pair() -> tuple[dict[str, Any], dict[str, Any]]:
        return await asyncio.gather(
            client._spend_log_scan_rows("2026-06-01", "2026-06-30", "all"),
            client._spend_log_scan_rows("2026-06-01", "2026-06-30", "all"),
        )

    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    first, second = asyncio.run(run_scan_pair())

    assert calls == 1
    assert first["entries"] == second["entries"]
    assert client._spend_log_scan_tasks == {}


def test_usage_log_scan_refresh_writes_new_cache(monkeypatch) -> None:
    client, backend = make_client()
    calls = 0

    async def fake_request_backend(_backend: LiteLLMBackend, _method: str, path: str, **_kwargs: Any) -> Any:
        nonlocal calls
        assert _backend == backend
        assert path == "/spend/logs/v2"
        calls += 1
        return {
            "logs": [{"user": f"user-{calls}", "startTime": "2026-06-15T10:00:00Z", "model": "gpt-4o", "total_tokens": calls}],
            "total_pages": 1,
            "total": 1,
        }

    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    first = asyncio.run(client._spend_log_scan_rows("2026-06-01", "2026-06-30", "all"))
    refreshed = asyncio.run(client._spend_log_scan_rows("2026-06-01", "2026-06-30", "all", refresh=True))
    cached = asyncio.run(client._spend_log_scan_rows("2026-06-01", "2026-06-30", "all"))

    assert calls == 2
    assert first["entries"][0]["log"]["user"] == "user-1"
    assert refreshed["entries"][0]["log"]["user"] == "user-2"
    assert cached["entries"][0]["log"]["user"] == "user-2"
    assert cached["cache"]["hit"] is True


def test_admin_usage_summary_does_not_read_spend_logs(monkeypatch) -> None:
    client, backend = make_client()

    async def fake_request_backend(_backend: LiteLLMBackend, _method: str, path: str, **_kwargs: Any) -> Any:
        assert _backend == backend
        assert path == "/user/daily/activity/aggregated"
        return {
            "data": [
                {
                    "date": "2026-06-15",
                    "total_tokens": 200,
                    "prompt_tokens": 120,
                    "completion_tokens": 80,
                    "request_count": 2,
                }
            ]
        }

    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    payload = asyncio.run(client.admin_usage_summary_rows("2026-06-01", "2026-06-30", "all"))

    assert payload["summaryRows"][0]["totalTokens"] == 200
    assert payload["dataQuality"]["summarySource"] == "official_daily_activity"


def test_department_usage_summary_does_not_read_spend_logs(monkeypatch) -> None:
    client, backend = make_client()

    async def fake_team_map(_backend: LiteLLMBackend | None = None) -> dict[str, dict[str, str]]:
        return {"team-a": {"id": "team-a", "name": "Team A"}}

    async def fake_team_daily_activity_rows(
        start_date: str,
        end_date: str,
        department: str | None,
        team_map: dict[str, dict[str, str]],
        backend_arg: LiteLLMBackend | None = None,
    ) -> list[dict[str, Any]]:
        assert backend_arg == backend
        assert department is None
        assert team_map["team-a"]["name"] == "Team A"
        return [
            {
                "date": "2026-06-15",
                "departmentId": "team-a",
                "departmentName": "Team A",
                "departmentBindStatus": "bound",
                "source": "其他",
                "model": "全量",
                "promptTokens": 100,
                "completionTokens": 50,
                "totalTokens": 150,
                "requestCount": 3,
                "successCount": 3,
                "failureCount": 0,
                "spend": 0.1,
            }
        ]

    monkeypatch.setattr(client, "team_map", fake_team_map)
    monkeypatch.setattr(client, "_team_daily_activity_rows", fake_team_daily_activity_rows)

    payload = asyncio.run(client.admin_department_usage_summary_rows("2026-06-01", "2026-06-30", "all"))

    assert payload["summaryRows"][0]["totalTokens"] == 150
    assert payload["departments"][0]["departmentId"] == "team-a"
    assert payload["dataQuality"]["summarySource"] == "team_daily_activity"


def test_usage_scan_prewarm_reads_default_sources(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    class FakeClient:
        async def _spend_log_scan_rows(self, start_date: str, end_date: str, source: str) -> dict[str, Any]:
            calls.append((start_date, end_date, source))
            return {"entries": [], "pagesRead": 0, "totalPages": 0, "cache": {"hit": False}}

    monkeypatch.setenv("USAGE_SCAN_PREWARM_ENABLED", "true")
    monkeypatch.setenv("USAGE_SCAN_PREWARM_DELAY_SECONDS", "0")
    monkeypatch.setenv("USAGE_SCAN_PREWARM_SOURCES", "all,Claude Code")
    monkeypatch.setattr(main, "client", lambda: FakeClient())
    monkeypatch.setattr(main, "default_date_range", lambda: ("2026-06-01", "2026-06-30"))

    asyncio.run(main.prewarm_usage_scan())

    assert calls == [("2026-06-01", "2026-06-30", "all"), ("2026-06-01", "2026-06-30", "Claude Code")]


def test_usage_scan_prewarm_swallows_failures(monkeypatch) -> None:
    class FakeClient:
        async def _spend_log_scan_rows(self, _start_date: str, _end_date: str, _source: str) -> dict[str, Any]:
            raise RuntimeError("upstream unavailable")

    monkeypatch.setenv("USAGE_SCAN_PREWARM_ENABLED", "true")
    monkeypatch.setenv("USAGE_SCAN_PREWARM_DELAY_SECONDS", "0")
    monkeypatch.setenv("USAGE_SCAN_PREWARM_SOURCES", "all")
    monkeypatch.setattr(main, "client", lambda: FakeClient())
    monkeypatch.setattr(main, "default_date_range", lambda: ("2026-06-01", "2026-06-30"))

    asyncio.run(main.prewarm_usage_scan())
