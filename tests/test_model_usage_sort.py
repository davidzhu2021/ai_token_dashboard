import asyncio
from datetime import date
from typing import Any

import pytest
from fastapi import HTTPException

from backend.litellm_client import LiteLLMBackend, LiteLLMClient
from backend.cache import TTLCache


class _MemoryCache:
    """Small deterministic cache double for the model catalog tests."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def get(self, key: str) -> tuple[bool, Any, int]:
        if key in self.values:
            return True, self.values[key], 300
        return False, None, 0

    def set(self, key: str, value: Any, _ttl: int) -> None:
        self.values[key] = value


def make_client() -> LiteLLMClient:
    client = object.__new__(LiteLLMClient)
    primary = LiteLLMBackend(
        id="primary",
        label="Primary",
        base_url="https://primary.test",
        admin_key="primary-key",
    )
    secondary = LiteLLMBackend(
        id="secondary",
        label="Secondary",
        base_url="https://secondary.test",
        admin_key="secondary-key",
        source="Secondary",
    )
    client.backends = [primary, secondary]
    client._backend_map = {backend.id: backend for backend in client.backends}
    client._model_cache = _MemoryCache()
    client._model_usage_cache = TTLCache()
    return client


def _daily_item(
    day: str,
    model_groups: dict[str, int] | None = None,
    models: dict[str, int] | None = None,
) -> dict[str, Any]:
    breakdown: dict[str, Any] = {}
    if model_groups is not None:
        breakdown["model_groups"] = {
            name: {
                "metrics": {
                    "api_requests": requests,
                    "successful_requests": max(0, requests - 1),
                    "failed_requests": 1 if requests else 0,
                }
            }
            for name, requests in model_groups.items()
        }
    if models is not None:
        breakdown["models"] = {
            name: {"metrics": {"api_requests": requests}}
            for name, requests in models.items()
        }
    return {"date": day, "breakdown": breakdown}


def test_model_usage_counts_aggregate_model_groups_across_dates_and_backends() -> None:
    client = make_client()
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def fake_request_backend(
        backend: LiteLLMBackend,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        params = dict(kwargs.get("params") or {})
        calls.append((backend.id, path, params))
        assert method == "GET"
        assert path == "/user/daily/activity/aggregated"
        assert "user_id" not in params
        assert params["start_date"] == "2026-07-01"
        assert params["end_date"] == "2026-07-03"
        if backend.id == "primary":
            return {
                "results": [
                    _daily_item("2026-07-01", {" GPT-4O ": 3, "claude-3": 2}),
                    _daily_item("2026-07-02", {"gpt-4o": 4}),
                ]
            }
        return {"results": [_daily_item("2026-07-03", {"GPT-4o": 5, "claude-3": 1})]}

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    counts = asyncio.run(client.model_usage_counts("2026-07-01", "2026-07-03"))

    assert counts == {"gpt-4o": 12, "claude-3": 3}
    assert {backend_id for backend_id, _, _ in calls} == {"primary", "secondary"}


def test_model_usage_counts_prefers_models_over_model_groups() -> None:
    client = make_client()

    async def fake_request_backend(_backend: LiteLLMBackend, _method: str, path: str, **_kwargs: Any) -> Any:
        assert path == "/user/daily/activity/aggregated"
        return {
            "results": [
                _daily_item(
                    "2026-07-01",
                    model_groups={"gpt-4o": 7},
                    models={"gpt-4o": 99, "wrong-fallback": 50},
                )
            ]
        }

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    counts = asyncio.run(client.model_usage_counts("2026-07-01", "2026-07-01"))

    assert counts == {"gpt-4o": 198, "wrong-fallback": 100}


def test_usage_from_logs_prefers_actual_model_id_and_falls_back_in_order() -> None:
    client = make_client()
    client.backends = [client.backends[0]]
    client._backend_map = {backend.id: backend for backend in client.backends}

    async def fake_request_backend(_backend: LiteLLMBackend, _method: str, path: str, **_kwargs: Any) -> Any:
        assert path == "/spend/logs/v2"
        return {
            "logs": [
                {
                    "startTime": "2026-07-01T01:00:00Z",
                    "model": "gpt-4o",
                    "model_group": "auto-router",
                    "model_id": "claude-sonnet-4-deploy",
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
                {
                    "startTime": "2026-07-01T02:00:00Z",
                    "model": "request-model",
                    "model_group": "fallback-group",
                    "litellm_model_name": "provider-model",
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
                {
                    "startTime": "2026-07-01T03:00:00Z",
                    "model_group": "fallback-group",
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            ],
            "total_pages": 1,
        }

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    rows = asyncio.run(client._usage_from_logs("user-1", "2026-07-01", "2026-07-01", "all"))

    assert [(row["model"], row["totalTokens"]) for row in rows] == [
        ("claude-sonnet-4-deploy", 15),
        ("fallback-group", 2),
        ("provider-model", 5),
    ]


def test_daily_activity_expands_actual_models_from_breakdown() -> None:
    client = make_client()

    rows = client._rows_from_daily_activity_item(
        {
            "date": "2026-07-01",
            "model_group": "auto-router",
            "metrics": {"total_tokens": 999, "api_requests": 99},
            "breakdown": {
                "models": {
                    "claude-sonnet-4-deploy": {
                        "metrics": {
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                            "api_requests": 2,
                            "successful_requests": 2,
                            "spend": 0.12,
                        }
                    },
                    "gpt-4o-deploy": {
                        "metrics": {
                            "prompt_tokens": 7,
                            "completion_tokens": 3,
                            "total_tokens": 10,
                            "api_requests": 1,
                            "failed_requests": 1,
                            "spend": 0.08,
                        }
                    },
                }
            },
        },
        "Cursor",
    )

    assert [(row["model"], row["totalTokens"], row["requestCount"]) for row in rows] == [
        ("claude-sonnet-4-deploy", 15, 2),
        ("gpt-4o-deploy", 10, 1),
    ]


def test_daily_activity_falls_back_to_model_groups_when_models_missing() -> None:
    client = make_client()

    rows = client._rows_from_daily_activity_item(
        {
            "date": "2026-07-01",
            "breakdown": {
                "model_groups": {
                    "auto-router": {"metrics": {"total_tokens": 15, "api_requests": 2}},
                }
            },
            "metrics": {"total_tokens": 15, "api_requests": 2},
        },
        "Cursor",
    )

    assert rows[0]["model"] == "auto-router"
    assert rows[0]["totalTokens"] == 15
    assert rows[0]["requestCount"] == 2


def test_model_usage_counts_falls_back_to_legacy_models_breakdown() -> None:
    client = make_client()

    async def fake_request_backend(_backend: LiteLLMBackend, _method: str, path: str, **_kwargs: Any) -> Any:
        assert path == "/user/daily/activity/aggregated"
        return {"results": [_daily_item("2026-07-01", models={"legacy-a": 2, "legacy-b": 4})]}

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    counts = asyncio.run(client.model_usage_counts("2026-07-01", "2026-07-01"))

    assert counts == {"legacy-a": 4, "legacy-b": 8}


def test_model_usage_counts_uses_date_window_cache_and_separates_windows() -> None:
    client = make_client()
    calls: list[tuple[str, str]] = []

    async def fake_request_backend(
        backend: LiteLLMBackend,
        _method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        assert path == "/user/daily/activity/aggregated"
        params = kwargs["params"]
        calls.append((backend.id, f"{params['start_date']}:{params['end_date']}"))
        return {"results": [_daily_item(params["start_date"], {"cached-model": 1})]}

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    first = asyncio.run(client.model_usage_counts("2026-07-01", "2026-07-01"))
    second = asyncio.run(client.model_usage_counts("2026-07-01", "2026-07-01"))
    third = asyncio.run(client.model_usage_counts("2026-07-02", "2026-07-02"))

    assert first == second == {"cached-model": 2}
    assert third == {"cached-model": 2}
    assert calls == [
        ("primary", "2026-07-01:2026-07-01"),
        ("secondary", "2026-07-01:2026-07-01"),
        ("primary", "2026-07-02:2026-07-02"),
        ("secondary", "2026-07-02:2026-07-02"),
    ]


def test_model_usage_counts_caches_partial_success() -> None:
    client = make_client()
    calls: list[str] = []

    async def fake_request_backend(backend: LiteLLMBackend, _method: str, path: str, **_kwargs: Any) -> Any:
        assert path == "/user/daily/activity/aggregated"
        calls.append(backend.id)
        if backend.id == "secondary":
            raise HTTPException(status_code=502, detail="secondary failed")
        return {"results": [_daily_item("2026-07-01", {"partial-model": 3})]}

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    first = asyncio.run(client.model_usage_counts("2026-07-01", "2026-07-01"))
    second = asyncio.run(client.model_usage_counts("2026-07-01", "2026-07-01"))

    assert first == second == {"partial-model": 3}
    assert calls == ["primary", "secondary"]


def test_model_usage_counts_does_not_cache_when_all_backends_fail() -> None:
    client = make_client()
    calls: list[str] = []

    async def fake_request_backend(backend: LiteLLMBackend, _method: str, path: str, **_kwargs: Any) -> Any:
        assert path == "/user/daily/activity/aggregated"
        calls.append(backend.id)
        raise HTTPException(status_code=503, detail="upstream unavailable")

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    first = asyncio.run(client.model_usage_counts("2026-07-01", "2026-07-01"))
    second = asyncio.run(client.model_usage_counts("2026-07-01", "2026-07-01"))

    assert first == second == {}
    assert calls == ["primary", "secondary", "primary", "secondary"]


def test_model_usage_counts_does_not_cache_invalid_activity_envelope() -> None:
    client = make_client()
    calls: list[str] = []

    async def fake_request_backend(backend: LiteLLMBackend, _method: str, path: str, **_kwargs: Any) -> Any:
        assert path == "/user/daily/activity/aggregated"
        calls.append(backend.id)
        return {}

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    first = asyncio.run(client.model_usage_counts("2026-07-01", "2026-07-01"))
    second = asyncio.run(client.model_usage_counts("2026-07-01", "2026-07-01"))

    assert first == second == {}
    assert calls == ["primary", "secondary", "primary", "secondary"]


def test_models_sort_by_usage_ties_by_name_and_deduplicate_backend_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client()
    monkeypatch.setattr("backend.litellm_client.usage_today", lambda: date(2026, 7, 30))

    async def fake_request_backend(backend: LiteLLMBackend, _method: str, path: str, **_kwargs: Any) -> Any:
        if path == "/models":
            if backend.id == "primary":
                return {"data": [{"id": "zeta"}, {"id": "Alpha"}, {"id": "primary-gpt", "model_name": "gpt-4o", "description": "primary"}]}
            return {"data": [{"id": "secondary-gpt", "model_name": "gpt-4o", "description": "secondary"}, {"id": "beta"}]}
        if path == "/user/daily/activity/aggregated":
            if backend.id == "primary":
                return {"results": [_daily_item("2026-07-01", {"gpt-4o": 10, "Alpha": 5})]}
            return {"results": [_daily_item("2026-07-01", {"beta": 5})]}
        raise AssertionError(f"unexpected path: {path}")

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    models = asyncio.run(client.models())

    assert [item["modelName"] for item in models] == ["gpt-4o", "Alpha", "beta", "zeta"]
    assert models[0]["id"] == "primary-gpt"
    assert models[0]["description"] == "primary"
    assert all("usageCount" not in item for item in models)


def test_models_deduplicate_trimmed_casefold_names_but_keep_similar_names(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client()
    monkeypatch.setattr("backend.litellm_client.usage_today", lambda: date(2026, 7, 30))

    async def fake_request_backend(backend: LiteLLMBackend, _method: str, path: str, **_kwargs: Any) -> Any:
        if path == "/models":
            if backend.id == "primary":
                return {"data": [{"id": "primary", "model_name": " GPT-5.5 "}, {"id": "chatgpt-gpt-5.5"}]}
            return {"data": [{"id": "secondary", "model_name": "gpt-5.5"}]}
        if path == "/user/daily/activity/aggregated":
            return {"results": []}
        raise AssertionError(f"unexpected path: {path}")

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    models = asyncio.run(client.models())

    assert [item["modelName"] for item in models] == ["chatgpt-gpt-5.5", "GPT-5.5"]
    assert len([item for item in models if item["modelName"].casefold() == "gpt-5.5"]) == 1


def test_models_return_complete_name_sorted_catalog_when_all_usage_sources_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client()
    monkeypatch.setattr("backend.litellm_client.usage_today", lambda: date(2026, 7, 30))

    async def fake_request_backend(backend: LiteLLMBackend, _method: str, path: str, **_kwargs: Any) -> Any:
        if path == "/models":
            return {"data": [{"id": "zeta"}, {"id": "Alpha"}]} if backend.id == "primary" else {"data": []}
        if path == "/user/daily/activity/aggregated":
            raise HTTPException(status_code=503, detail="upstream unavailable")
        raise AssertionError(f"unexpected path: {path}")

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    models = asyncio.run(client.models())

    assert [item["modelName"] for item in models] == ["Alpha", "zeta"]
    assert all("usageCount" not in item for item in models)


def test_models_keep_catalog_when_one_usage_source_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client()
    monkeypatch.setattr("backend.litellm_client.usage_today", lambda: date(2026, 7, 30))

    async def fake_request_backend(backend: LiteLLMBackend, _method: str, path: str, **_kwargs: Any) -> Any:
        if path == "/models":
            return {"data": [{"id": "low"}, {"id": "high"}]} if backend.id == "primary" else {"data": []}
        if path == "/user/daily/activity/aggregated":
            if backend.id == "secondary":
                raise HTTPException(status_code=502, detail="secondary failed")
            return {"results": [_daily_item("2026-07-01", {"high": 8})]}
        raise AssertionError(f"unexpected path: {path}")

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    models = asyncio.run(client.models())

    assert [item["modelName"] for item in models] == ["high", "low"]
