import asyncio
from typing import Any

from backend.cache import TTLCache
from backend.litellm_client import LiteLLMBackend, LiteLLMClient


def make_client() -> tuple[LiteLLMClient, LiteLLMBackend]:
    client = object.__new__(LiteLLMClient)
    backend = LiteLLMBackend(id="primary", label="Primary", base_url="https://primary.test", admin_key="primary-key")
    client.backends = [backend]
    client._backend_map = {backend.id: backend}
    client._spend_log_scan_cache = TTLCache()
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
