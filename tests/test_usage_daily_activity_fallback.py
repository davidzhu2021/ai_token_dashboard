import asyncio
from typing import Any

import pytest
from fastapi import HTTPException

from backend.cache import TTLCache
from backend.litellm_client import LiteLLMBackend, LiteLLMClient


def make_client() -> tuple[LiteLLMClient, LiteLLMBackend]:
    client = object.__new__(LiteLLMClient)
    backend = LiteLLMBackend(
        id="primary",
        label="Primary",
        base_url="https://primary.test",
        admin_key="primary-key",
    )
    client.backends = [backend]
    client._backend_map = {backend.id: backend}
    client._model_cache = TTLCache()
    client._deployment_model_maps = {backend.id: {}}
    return client, backend


@pytest.mark.parametrize("status_code", [404, 405, 501])
def test_daily_activity_falls_back_only_when_aggregated_endpoint_is_unavailable(
    status_code: int,
) -> None:
    client, backend = make_client()
    calls: list[str] = []

    async def fake_request_backend(
        _backend: LiteLLMBackend, _method: str, path: str, **_kwargs: Any
    ) -> dict[str, Any]:
        calls.append(path)
        if path == "/user/daily/activity/aggregated":
            raise HTTPException(status_code=status_code, detail="unsupported")
        return {"results": []}

    client.request_backend = fake_request_backend  # type: ignore[assignment]
    async def skip_model_map(_backend: LiteLLMBackend) -> dict[str, str]:
        return {}

    client._ensure_deployment_model_map = skip_model_map  # type: ignore[method-assign]

    rows = asyncio.run(
        client._usage_from_daily_activity(
            "user-1", "2026-08-01", "2026-08-02", "all", backend=backend
        )
    )

    assert rows == []
    assert calls == [
        "/user/daily/activity/aggregated",
        "/user/daily/activity",
    ]


@pytest.mark.parametrize("status_code", [400, 429, 500, 502, 503, 504])
def test_daily_activity_does_not_retry_other_aggregated_failures(
    status_code: int,
) -> None:
    client, backend = make_client()
    calls: list[str] = []

    async def fake_request_backend(
        _backend: LiteLLMBackend, _method: str, path: str, **_kwargs: Any
    ) -> dict[str, Any]:
        calls.append(path)
        raise HTTPException(status_code=status_code, detail="upstream failure")

    client.request_backend = fake_request_backend  # type: ignore[assignment]
    async def skip_model_map(_backend: LiteLLMBackend) -> dict[str, str]:
        return {}

    client._ensure_deployment_model_map = skip_model_map  # type: ignore[method-assign]

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            client._usage_from_daily_activity(
                "user-1", "2026-08-01", "2026-08-02", "all", backend=backend
            )
        )

    assert exc_info.value.status_code == status_code
    assert calls == ["/user/daily/activity/aggregated"]
