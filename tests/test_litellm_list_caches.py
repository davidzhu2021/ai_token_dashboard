import asyncio

from backend.cache import TTLCache
from backend.litellm_client import LiteLLMBackend, LiteLLMClient


def _client_for_test() -> tuple[LiteLLMClient, LiteLLMBackend]:
    client = object.__new__(LiteLLMClient)
    backend = LiteLLMBackend(
        id="primary",
        label="Primary",
        base_url="https://example.test",
        admin_key="test-key",
    )
    client.backends = [backend]
    client._backend_map = {"primary": backend}
    client._users_cache = TTLCache()
    client._teams_cache = TTLCache()
    client._team_map_cache = TTLCache()
    return client, backend


def test_users_are_cached(monkeypatch) -> None:
    client, backend = _client_for_test()
    calls = 0

    async def fake_request_backend(_backend, _method, path, **_kwargs):
        nonlocal calls
        calls += 1
        assert path == "/user/list"
        return {"data": [{"user_id": "user-a"}], "total_pages": 1}

    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    first = asyncio.run(client.users(backend))
    second = asyncio.run(client.users(backend))

    assert first == second == [{"user_id": "user-a"}]
    assert calls == 1


def test_teams_are_cached_by_detail_mode(monkeypatch) -> None:
    client, backend = _client_for_test()
    calls = 0

    async def fake_request_backend(_backend, _method, path, **_kwargs):
        nonlocal calls
        calls += 1
        assert path == "/v2/team/list"
        return {"data": [{"team_id": "team-a", "team_alias": "Team A", "members_with_roles": [{"user_id": "user-a"}]}], "total_pages": 1}

    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    first = asyncio.run(client.teams(backend))
    second = asyncio.run(client.teams(backend))
    list_only = asyncio.run(client.teams(backend, include_details=False))

    assert first == second
    assert list_only == [{"team_id": "team-a", "team_alias": "Team A", "members_with_roles": [{"user_id": "user-a"}]}]
    assert calls == 2


def test_team_map_is_cached(monkeypatch) -> None:
    client, backend = _client_for_test()
    calls = 0

    async def fake_teams(_backend, include_details=True):
        nonlocal calls
        calls += 1
        assert include_details is False
        return [{"team_id": "team-a", "team_alias": "Team A"}]

    monkeypatch.setattr(client, "teams", fake_teams)

    first = asyncio.run(client.team_map(backend))
    second = asyncio.run(client.team_map(backend))

    assert first == second == {"team-a": {"id": "team-a", "name": "Team A"}}
    assert calls == 1
