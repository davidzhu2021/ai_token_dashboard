import asyncio
from typing import Any

from backend.litellm_client import LiteLLMBackend, LiteLLMClient


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
