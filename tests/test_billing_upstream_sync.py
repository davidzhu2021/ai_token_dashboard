"""充值额度写入上游的测试：用户总额度、模型授权、密钥日限额。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import HTTPException

from backend.cache import TTLCache
from backend.litellm_client import LiteLLMBackend, LiteLLMClient


def make_client() -> LiteLLMClient:
    client = object.__new__(LiteLLMClient)
    primary = LiteLLMBackend(id="primary", label="Primary", base_url="https://primary.test", admin_key="primary-key")
    client.backends = [primary]
    client._backend_map = {item.id: item for item in client.backends}
    client._key_cache = TTLCache()
    return client


def run(coro):
    return asyncio.run(coro)


# ---- 用户级总额度 ----


def test_set_user_budget_writes_cumulative_total() -> None:
    client = make_client()
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_request(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        calls.append((path, kwargs.get("json") or {}))
        return {}

    client.request_backend = fake_request  # type: ignore[assignment]

    run(client.set_user_budget("local-1", 120.5))

    assert calls == [("/user/update", {"user_id": "local-1", "max_budget": 120.5})]


def test_set_user_budget_clamps_negative() -> None:
    client = make_client()
    captured: list[dict[str, Any]] = []

    async def fake_request(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        captured.append(kwargs.get("json") or {})
        return {}

    client.request_backend = fake_request  # type: ignore[assignment]

    run(client.set_user_budget("local-1", -50))

    # 上游把 0 视为"额度已耗尽"，负值必须先夹到 0 而不是原样透传。
    assert captured[0]["max_budget"] == 0.0


def test_set_user_budget_requires_account_id() -> None:
    client = make_client()

    async def fake_request(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("不应发起上游请求")

    client.request_backend = fake_request  # type: ignore[assignment]

    with pytest.raises(HTTPException) as excinfo:
        run(client.set_user_budget("   ", 10))
    assert excinfo.value.status_code == 400


def test_set_user_budget_invalidates_key_cache() -> None:
    client = make_client()
    client._key_cache.set("keys:primary:local-1", [{"id": "sk-old"}], 300)

    async def fake_request(*args: Any, **kwargs: Any) -> Any:
        return {}

    client.request_backend = fake_request  # type: ignore[assignment]

    run(client.set_user_budget("local-1", 10))

    hit, _, _ = client._key_cache.get("keys:primary:local-1")
    assert hit is False


# ---- 模型授权 ----


def test_grant_default_models_replaces_no_default_placeholder() -> None:
    client = make_client()
    writes: list[dict[str, Any]] = []

    async def fake_request(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        if path == "/v2/user/info":
            return {"models": ["no-default-models"]}
        writes.append(kwargs.get("json") or {})
        return {}

    client.request_backend = fake_request  # type: ignore[assignment]

    granted = run(client.grant_default_models("local-1", ["gpt-4o", "claude-sonnet-4-6"]))

    assert granted == ["claude-sonnet-4-6", "gpt-4o"]
    assert writes == [{"user_id": "local-1", "models": ["claude-sonnet-4-6", "gpt-4o"]}]


def test_grant_default_models_keeps_existing_wider_grant() -> None:
    client = make_client()
    writes: list[str] = []

    async def fake_request(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        if path == "/v2/user/info":
            return {"models": ["gpt-5-pro", "claude-opus-4-7"]}
        writes.append(path)
        return {}

    client.request_backend = fake_request  # type: ignore[assignment]

    granted = run(client.grant_default_models("local-1", ["gpt-4o"]))

    # 已被管理员开通过模型的账号不能被充值动作收窄权限。
    assert granted == []
    assert writes == []


def test_grant_default_models_ignores_empty_request() -> None:
    client = make_client()

    async def fake_request(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("不应发起上游请求")

    client.request_backend = fake_request  # type: ignore[assignment]

    assert run(client.grant_default_models("local-1", [])) == []
    assert run(client.grant_default_models("local-1", ["  ", ""])) == []
    assert run(client.grant_default_models("", ["gpt-4o"])) == []


# ---- 密钥日限额 ----


def _key_payload(token: str, max_budget: Any, status_blocked: bool = False) -> dict[str, Any]:
    return {
        "token": token,
        "key_name": f"sk-...{token[-4:]}",
        "key_alias": "个人密钥",
        "models": ["gpt-4o"],
        "max_budget": max_budget,
        "blocked": status_blocked,
        "spend": 0,
    }


def test_raise_key_daily_budgets_only_lifts_lower_limits() -> None:
    client = make_client()
    updates: list[dict[str, Any]] = []

    async def fake_request(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        if path == "/key/list":
            return {
                "keys": [
                    _key_payload("aaaa1111", 50),
                    _key_payload("bbbb2222", 200),
                    _key_payload("cccc3333", None),
                ]
            }
        if path == "/key/update":
            updates.append(kwargs.get("json") or {})
            return {}
        return {}

    client.request_backend = fake_request  # type: ignore[assignment]

    adjusted = run(client.raise_key_daily_budgets("local-1", 100.0))

    # 只升不降：200 的那把不能被压到 100。
    targets = {item["key"]: item["max_budget"] for item in updates}
    assert set(targets) == {"aaaa1111", "cccc3333"}
    assert all(value == 100.0 for value in targets.values())
    assert sorted(adjusted) == ["aaaa1111", "cccc3333"]
    assert all(item["budget_duration"] == "1d" for item in updates)


def test_raise_key_daily_budgets_skips_disabled_keys() -> None:
    client = make_client()
    updates: list[dict[str, Any]] = []

    async def fake_request(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        if path == "/key/list":
            return {"keys": [_key_payload("dead0000", 10, status_blocked=True)]}
        if path == "/key/update":
            updates.append(kwargs.get("json") or {})
        return {}

    client.request_backend = fake_request  # type: ignore[assignment]

    assert run(client.raise_key_daily_budgets("local-1", 100.0)) == []
    assert updates == []


def test_raise_key_daily_budgets_handles_no_keys() -> None:
    client = make_client()

    async def fake_request(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        if path == "/key/list":
            return {"keys": []}
        raise AssertionError("没有密钥时不应写入")

    client.request_backend = fake_request  # type: ignore[assignment]

    assert run(client.raise_key_daily_budgets("local-1", 100.0)) == []


def test_raise_key_daily_budgets_ignores_non_positive_target() -> None:
    client = make_client()

    async def fake_request(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("不应发起上游请求")

    client.request_backend = fake_request  # type: ignore[assignment]

    assert run(client.raise_key_daily_budgets("local-1", 0)) == []
    assert run(client.raise_key_daily_budgets("local-1", -10)) == []
    assert run(client.raise_key_daily_budgets("", 100)) == []


def test_raise_key_daily_budgets_tolerates_unparseable_budget() -> None:
    client = make_client()
    updates: list[dict[str, Any]] = []

    async def fake_request(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        if path == "/key/list":
            return {"keys": [_key_payload("eeee4444", "not-a-number")]}
        if path == "/key/update":
            updates.append(kwargs.get("json") or {})
        return {}

    client.request_backend = fake_request  # type: ignore[assignment]

    # 脏数据当作 0 处理，宁可多写一次也不要因为解析失败漏掉抬额。
    assert run(client.raise_key_daily_budgets("local-1", 100.0)) == ["eeee4444"]
    assert updates[0]["max_budget"] == 100.0
