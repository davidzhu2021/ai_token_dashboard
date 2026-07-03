import asyncio
from typing import Any

from fastapi import HTTPException

from backend.litellm_client import LiteLLMBackend, LiteLLMClient


def make_client() -> LiteLLMClient:
    client = object.__new__(LiteLLMClient)
    primary = LiteLLMBackend(id="primary", label="Primary", base_url="https://primary.test", admin_key="primary-key")
    her = LiteLLMBackend(id="her", label="Her", base_url="https://her.test", admin_key="her-key", source="Her")
    client.backends = [primary, her]
    client._backend_map = {item.id: item for item in client.backends}
    return client


def test_admin_usage_rows_merges_her_logs_into_employees() -> None:
    client = make_client()

    async def fake_users(backend: LiteLLMBackend | None = None) -> list[dict[str, Any]]:
        if backend and backend.id == "her":
            return [{"user_id": "carher-001", "user_email": "alice@carher.net", "user_alias": "Alice"}]
        return []

    async def fake_her_account_index(backend: LiteLLMBackend) -> dict[str, Any]:
        return {"emails": {}, "names": {}, "profiles": {"carher-001": {"email": "alice@carher.net", "name": "Alice"}}}

    async def fake_request_backend(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        if path == "/spend/logs/v2" and backend.id == "her":
            return {
                "logs": [
                    {
                        "user": "carher-001",
                        "startTime": "2026-06-15T10:00:00Z",
                        "model": "gpt-4o",
                        "prompt_tokens": 120,
                        "completion_tokens": 30,
                        "total_tokens": 150,
                        "spend": 0.12,
                        "status": "success",
                    }
                ],
                "total_pages": 1,
                "total": 1,
            }
        if path == "/spend/logs/v2":
            return {"logs": [], "total_pages": 1, "total": 0}
        if path == "/user/daily/activity/aggregated":
            raise HTTPException(status_code=404, detail="no summary")
        raise AssertionError(f"unexpected call {backend.id} {method} {path}")

    client.users = fake_users  # type: ignore[assignment]
    client.her_account_index = fake_her_account_index  # type: ignore[assignment]
    client.request_backend = fake_request_backend  # type: ignore[assignment]

    payload = asyncio.run(client.admin_usage_rows("2026-06-01", "2026-06-30", "all", None))
    assert payload["rows"]
    assert payload["rows"][0]["source"] == "Her"
    assert payload["employees"][0]["employeeEmail"] == "alice@carher.net"
    assert payload["employees"][0]["totalTokens"] == 150


def test_department_usage_keeps_her_unassigned_department() -> None:
    client = make_client()

    async def fake_users(backend: LiteLLMBackend | None = None) -> list[dict[str, Any]]:
        if backend and backend.id == "her":
            return [{"user_id": "carher-002", "user_alias": "Bob"}]
        return []

    async def fake_team_map(backend: LiteLLMBackend | None = None) -> dict[str, dict[str, str]]:
        return {}

    async def fake_team_daily_activity_rows(
        start_date: str,
        end_date: str,
        department: str | None,
        team_map: dict[str, dict[str, str]],
        backend: LiteLLMBackend | None = None,
    ) -> list[dict[str, Any]]:
        return []

    async def fake_her_account_index(backend: LiteLLMBackend) -> dict[str, Any]:
        return {"emails": {}, "names": {}, "profiles": {"carher-002": {"email": "", "name": "Bob"}}}

    async def fake_request_backend(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        if path == "/spend/logs/v2" and backend.id == "her":
            return {
                "logs": [
                    {
                        "user": "carher-002",
                        "startTime": "2026-06-16T09:00:00Z",
                        "model": "claude-3-7-sonnet",
                        "prompt_tokens": 80,
                        "completion_tokens": 20,
                        "total_tokens": 100,
                        "spend": 0.09,
                        "status": "success",
                    }
                ],
                "total_pages": 1,
                "total": 1,
            }
        if path == "/spend/logs/v2":
            return {"logs": [], "total_pages": 1, "total": 0}
        if path == "/user/daily/activity/aggregated":
            raise HTTPException(status_code=404, detail="no summary")
        raise AssertionError(f"unexpected call {backend.id} {method} {path}")

    client.users = fake_users  # type: ignore[assignment]
    client.team_map = fake_team_map  # type: ignore[assignment]
    client._team_daily_activity_rows = fake_team_daily_activity_rows  # type: ignore[assignment]
    client.her_account_index = fake_her_account_index  # type: ignore[assignment]
    client.request_backend = fake_request_backend  # type: ignore[assignment]

    payload = asyncio.run(client.admin_department_usage_rows("2026-06-01", "2026-06-30", "all", None))
    assert payload["rows"][0]["source"] == "Her"
    assert payload["rows"][0]["departmentId"] == "unassigned"
    assert payload["departments"][0]["totalTokens"] == 100


def test_team_usage_with_her_team_ref_only_reads_authorized_team() -> None:
    client = make_client()
    her_backend = client._backend_map["her"]

    async def fake_teams(backend: LiteLLMBackend | None = None, include_details: bool = True) -> list[dict[str, Any]]:
        if backend and backend.id == "her":
            return [
                {
                    "team_id": "team-her",
                    "team_alias": "Her Team",
                    "members_with_roles": [{"user_id": "carher-003", "role": "admin"}],
                }
            ]
        return []

    async def fake_users(backend: LiteLLMBackend | None = None) -> list[dict[str, Any]]:
        if backend and backend.id == "her":
            return [{"user_id": "carher-003", "user_alias": "Carol"}]
        return []

    async def fake_her_account_index(backend: LiteLLMBackend) -> dict[str, Any]:
        return {"emails": {}, "names": {}, "profiles": {"carher-003": {"email": "", "name": "Carol"}}}

    async def fake_request_backend(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        if backend.id == "primary" and path == "/spend/logs/v2":
            return {"logs": [], "total_pages": 1, "total": 0}
        if backend.id == "her" and path == "/spend/logs/v2":
            return {
                "logs": [
                    {
                        "user": "carher-003",
                        "team_id": "team-her",
                        "startTime": "2026-06-17T01:00:00Z",
                        "model": "gpt-4.1",
                        "prompt_tokens": 60,
                        "completion_tokens": 40,
                        "total_tokens": 100,
                        "spend": 0.08,
                        "status": "success",
                    }
                ],
                "total_pages": 1,
                "total": 1,
            }
        raise AssertionError(f"unexpected call {backend.id} {method} {path}")

    async def fake_team_daily_activity_rows(
        start_date: str,
        end_date: str,
        department: str | None,
        team_map: dict[str, dict[str, str]],
        backend: LiteLLMBackend | None = None,
    ) -> list[dict[str, Any]]:
        assert backend == her_backend
        assert department == "team-her"
        return []

    client.teams = fake_teams  # type: ignore[assignment]
    client.users = fake_users  # type: ignore[assignment]
    client.her_account_index = fake_her_account_index  # type: ignore[assignment]
    client.request_backend = fake_request_backend  # type: ignore[assignment]
    client._team_daily_activity_rows = fake_team_daily_activity_rows  # type: ignore[assignment]

    payload = asyncio.run(client.team_usage_rows("her", "team-her", "2026-06-01", "2026-06-30", "all"))
    assert payload["team"]["backend"] == "her"
    assert payload["rows"][0]["source"] == "Her"
    assert payload["employees"][0]["employeeName"] == "Carol"


def test_models_merges_multi_backend_and_deduplicates_per_backend() -> None:
    client = make_client()

    async def fake_request_backend(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        assert method == "GET"
        assert path == "/models"
        if backend.id == "primary":
            return {"data": [{"id": "gpt-4o", "model_name": "gpt-4o"}, {"id": "gpt-4o", "model_name": "gpt-4o"}]}
        return {"data": [{"id": "gpt-4o", "model_name": "gpt-4o"}, {"id": "claude-3-7", "model_name": "claude-3-7"}]}

    class DummyCache:
        def __init__(self) -> None:
            self.data: dict[str, Any] = {}

        def get(self, key: str) -> tuple[bool, Any, int]:
            if key in self.data:
                return True, self.data[key], 1
            return False, None, 0

        def set(self, key: str, value: Any, _ttl: int) -> None:
            self.data[key] = value

    client._model_cache = DummyCache()
    client.request_backend = fake_request_backend  # type: ignore[assignment]

    models = asyncio.run(client.models())
    assert any(item["modelName"] == "gpt-4o" for item in models)
    assert any(item["modelName"] == "claude-3-7" for item in models)
    # gpt-4o appears once per backend (primary + Her)
    assert len([item for item in models if item["modelName"] == "gpt-4o"]) == 2


def test_source_filter_behavior_for_her_and_all() -> None:
    client = make_client()

    async def fake_users(backend: LiteLLMBackend | None = None) -> list[dict[str, Any]]:
        if backend and backend.id == "her":
            return [{"user_id": "carher-004", "user_alias": "Dora"}]
        return []

    async def fake_her_account_index(backend: LiteLLMBackend) -> dict[str, Any]:
        return {"emails": {}, "names": {}, "profiles": {"carher-004": {"email": "", "name": "Dora"}}}

    async def fake_request_backend(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        if path == "/spend/logs/v2" and backend.id == "her":
            return {
                "logs": [
                    {
                        "user": "carher-004",
                        "startTime": "2026-06-10T00:00:00Z",
                        "model": "gpt-4o-mini",
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                        "spend": 0.01,
                        "status": "success",
                    }
                ],
                "total_pages": 1,
                "total": 1,
            }
        if path == "/spend/logs/v2":
            return {"logs": [], "total_pages": 1, "total": 0}
        if path == "/user/daily/activity/aggregated":
            raise HTTPException(status_code=404, detail="no summary")
        raise AssertionError(f"unexpected call {backend.id} {method} {path}")

    client.users = fake_users  # type: ignore[assignment]
    client.her_account_index = fake_her_account_index  # type: ignore[assignment]
    client.request_backend = fake_request_backend  # type: ignore[assignment]

    only_her = asyncio.run(client.admin_usage_rows("2026-06-01", "2026-06-30", "Her", None))
    all_sources = asyncio.run(client.admin_usage_rows("2026-06-01", "2026-06-30", "all", None))
    only_cursor = asyncio.run(client.admin_usage_rows("2026-06-01", "2026-06-30", "Cursor", None))

    assert sum(item["totalTokens"] for item in only_her["rows"]) == 15
    assert sum(item["totalTokens"] for item in all_sources["rows"]) == 15
    assert only_cursor["rows"] == []
