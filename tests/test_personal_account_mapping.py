import asyncio
from typing import Any

from backend.litellm_client import LiteLLMBackend, LiteLLMClient
from backend.cache import TTLCache


def make_client() -> LiteLLMClient:
    client = object.__new__(LiteLLMClient)
    primary = LiteLLMBackend(id="primary", label="Primary", base_url="https://primary.test", admin_key="primary-key")
    client.backends = [primary]
    client._backend_map = {primary.id: primary}
    return client


def test_resolve_user_matches_tool_prefixed_accounts() -> None:
    client = make_client()

    async def fake_request_backend(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        if path == "/user/list":
            return {
                "users": [
                    {"user_id": "cursor-zhuyida", "user_alias": "cursor-zhuyida"},
                    {"user_id": "claude-code-zhuyida", "user_alias": "claude-code-zhuyida"},
                ],
                "total_pages": 1,
            }
        if path == "/key/list":
            return {"keys": [], "total_pages": 1}
        if path == "/spend/logs/v2":
            return {"logs": [], "total_pages": 1}
        raise AssertionError(f"unexpected call {backend.id} {method} {path}")

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    user = asyncio.run(client.resolve_user("zhuyida@auto-link.com.cn", "Zhuyida"))

    assert user["matched_user_ids"] == ["claude-code-zhuyida", "cursor-zhuyida"]
    assert {account["user_id"] for account in user["matched_accounts"]} == {"cursor-zhuyida", "claude-code-zhuyida"}
    assert all("tool_account_alias" in account["matchSources"] for account in user["matched_accounts"])


def test_resolve_user_falls_back_to_recent_logs() -> None:
    client = make_client()

    async def fake_request_backend(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        if path == "/user/list":
            return {"users": [], "total_pages": 1}
        if path == "/key/list":
            return {"keys": [], "total_pages": 1}
        if path == "/spend/logs/v2":
            return {
                "logs": [
                    {"user": "cursor-zhuyida", "startTime": "2026-07-10T10:00:00Z"},
                    {"user": "claude-code-zhuyida", "startTime": "2026-07-11T10:00:00Z"},
                ],
                "total_pages": 1,
            }
        raise AssertionError(f"unexpected call {backend.id} {method} {path}")

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    user = asyncio.run(client.resolve_user("zhuyida@auto-link.com.cn", "Zhuyida"))

    assert user["matched_user_ids"] == ["claude-code-zhuyida", "cursor-zhuyida"]
    assert all("recent_usage_log" in account["matchSources"] for account in user["matched_accounts"])


def test_resolve_user_matches_tool_key_alias_with_suffix() -> None:
    client = make_client()
    key_list_calls: list[dict[str, Any]] = []

    async def fake_request_backend(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        if path == "/user/list":
            return {"users": [], "total_pages": 1}
        if path == "/key/list":
            params = kwargs["params"]
            key_list_calls.append(params)
            if params.get("substring_matching") != "true":
                return {"keys": [], "total_pages": 1}
            if params["key_alias"] == "cursor-zhuyida":
                return {"keys": [{"user_id": "cursor-zhuyida", "key_alias": "cursor-zhuyida-3pfs"}], "total_pages": 1}
            if params["key_alias"] == "claude-code-zhuyida":
                return {"keys": [{"user_id": "claude-code-zhuyida", "key_alias": "claude-code-zhuyida-3sth"}], "total_pages": 1}
            return {"keys": [], "total_pages": 1}
        if path == "/spend/logs/v2":
            return {"logs": [], "total_pages": 1}
        raise AssertionError(f"unexpected call {backend.id} {method} {path}")

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    user = asyncio.run(client.resolve_user("zhuyida@auto-link.com.cn", "Zhuyida"))

    assert user["matched_user_ids"] == ["claude-code-zhuyida", "cursor-zhuyida"]
    assert {account["user_id"] for account in user["matched_accounts"]} == {"cursor-zhuyida", "claude-code-zhuyida"}
    assert all("key_alias" in account["matchSources"] for account in user["matched_accounts"])
    assert not any(call.get("key_alias") == "zhuyida" and call.get("substring_matching") == "true" for call in key_list_calls)


def test_suffix_key_alias_matching_rejects_unrelated_substrings() -> None:
    client = make_client()

    async def fake_request_backend(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        if path == "/user/list":
            return {"users": [], "total_pages": 1}
        if path == "/key/list":
            params = kwargs["params"]
            if params.get("substring_matching") == "true" and params["key_alias"] == "claude-code-zhuyida":
                return {
                    "keys": [
                        {"user_id": "not-zhuyida", "key_alias": "team-claude-code-zhuyida"},
                        {"user_id": "also-not-zhuyida", "key_alias": "claude-code-zhuyidaextra"},
                    ],
                    "total_pages": 1,
                }
            return {"keys": [], "total_pages": 1}
        if path == "/spend/logs/v2":
            return {"logs": [], "total_pages": 1}
        raise AssertionError(f"unexpected call {backend.id} {method} {path}")

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    user_ids = asyncio.run(client.user_ids_from_key_alias("zhuyida"))

    assert user_ids == []


def test_resolve_user_reads_user_list_metadata_total_pages() -> None:
    client = make_client()
    pages: list[int] = []

    async def fake_request_backend(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        if path == "/user/list":
            page = int(kwargs["params"]["page"])
            pages.append(page)
            if page == 2:
                return {
                    "users": [{"user_id": "cursor-zhuyida", "user_alias": "cursor-zhuyida"}],
                    "metadata": {"total_pages": 2},
                }
            return {"users": [], "metadata": {"total_pages": 2}}
        if path == "/key/list":
            return {"keys": [], "total_pages": 1}
        if path == "/spend/logs/v2":
            return {"logs": [], "total_pages": 1}
        raise AssertionError(f"unexpected call {backend.id} {method} {path}")

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    user = asyncio.run(client.resolve_user("zhuyida@auto-link.com.cn", "Zhuyida"))

    assert pages == [1, 2]
    assert user["matched_user_ids"] == ["cursor-zhuyida"]


def test_user_ids_from_key_alias_reads_multiple_key_pages() -> None:
    client = make_client()
    pages: list[int] = []

    async def fake_request_backend(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        assert path == "/key/list"
        params = kwargs["params"]
        if params["key_alias"] != "cursor-zhuyida" or params.get("substring_matching") != "true":
            return {"keys": [], "total_pages": 1}
        page = int(params["page"])
        pages.append(page)
        if page == 2:
            return {
                "keys": [{"user_id": "cursor-zhuyida", "key_alias": "cursor-zhuyida-tool"}],
                "metadata": {"total_pages": 2},
            }
        return {"keys": [], "metadata": {"total_pages": 2}}

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    user_ids = asyncio.run(client.user_ids_from_key_alias("zhuyida"))

    assert pages == [1, 2]
    assert user_ids == ["cursor-zhuyida"]


def test_keys_for_user_reads_multiple_key_pages() -> None:
    client = make_client()
    client._key_cache = TTLCache()
    pages: list[int] = []

    async def fake_request_backend(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        assert path == "/key/list"
        page = int(kwargs["params"]["page"])
        pages.append(page)
        if page == 2:
            return {
                "keys": [{"token": "sk-page-two", "key_name": "sk-...two2", "key_alias": "Page Two"}],
                "metadata": {"total_pages": 2},
            }
        return {
            "keys": [{"token": "sk-page-one", "key_name": "sk-...one1", "key_alias": "Page One"}],
            "metadata": {"total_pages": 2},
        }

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    keys = asyncio.run(client.keys_for_user("zhuyida"))

    assert pages == [1, 2]
    assert [key["name"] for key in keys] == ["Page One", "Page Two"]
