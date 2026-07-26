import asyncio
from typing import Any

import pytest
from fastapi import HTTPException

from backend.litellm_client import LiteLLMBackend, LiteLLMClient, NO_DEFAULT_MODELS


def provisioning_client() -> tuple[LiteLLMClient, LiteLLMBackend]:
    client = object.__new__(LiteLLMClient)
    backend = LiteLLMBackend(id="primary", label="Primary", base_url="https://example.test", admin_key="test-key")
    client.backends = [backend]
    return client, backend


@pytest.mark.parametrize("configured_role", ["proxy_admin", "proxy_admin_viewer", "typo"])
def test_local_user_provisioning_rejects_elevated_or_invalid_roles(monkeypatch, configured_role: str) -> None:
    client, _ = provisioning_client()
    requests: list[dict[str, Any]] = []

    async def fake_request_backend(_backend, method, path, **kwargs):
        requests.append({"method": method, "path": path, **kwargs})
        return {"user_id": "local-123"}

    monkeypatch.setenv("AUTH_DEFAULT_UPSTREAM_ROLE", configured_role)
    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    asyncio.run(client.create_internal_user("local-123", "Person@Example.COM", "Person"))

    payload = requests[0]["json"]
    assert payload["user_role"] == "internal_user_viewer"
    assert payload["auto_create_key"] is False
    assert "max_budget" not in payload
    assert payload["models"] == [NO_DEFAULT_MODELS]


def test_local_user_provisioning_accepts_internal_user_role(monkeypatch) -> None:
    client, _ = provisioning_client()
    captured: dict[str, Any] = {}

    async def fake_request_backend(_backend, _method, _path, **kwargs):
        captured.update(kwargs["json"])
        return {"user_id": "local-123"}

    monkeypatch.setenv("AUTH_DEFAULT_UPSTREAM_ROLE", "internal_user")
    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    asyncio.run(client.create_internal_user("local-123", "person@example.com"))

    assert captured["user_role"] == "internal_user"


@pytest.mark.parametrize(
    "email",
    ["user@@example.com", "user@example.com:25@other.test", ".user@example.com", "user..name@example.com"],
)
def test_local_user_provisioning_rejects_invalid_email(monkeypatch, email: str) -> None:
    client, _ = provisioning_client()

    async def must_not_request(*_args, **_kwargs):
        raise AssertionError("invalid email reached upstream")

    monkeypatch.setattr(client, "request_backend", must_not_request)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(client.create_internal_user("local-123", email))

    assert exc_info.value.status_code == 400


@pytest.mark.parametrize("unsupported_status", [404, 405, 501])
def test_user_info_falls_back_for_older_upstream_versions(monkeypatch, unsupported_status: int) -> None:
    client, _ = provisioning_client()
    paths: list[str] = []

    async def fake_request_backend(_backend, _method, path, **_kwargs):
        paths.append(path)
        if path == "/v2/user/info":
            raise HTTPException(status_code=unsupported_status, detail="unsupported")
        return {"user_info": {"user_id": "local-123", "models": [NO_DEFAULT_MODELS]}}

    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    result = asyncio.run(client.user_info("local-123"))

    assert paths == ["/v2/user/info", "/user/info"]
    assert result["user_id"] == "local-123"


def test_user_info_does_not_hide_authorization_failures(monkeypatch) -> None:
    client, _ = provisioning_client()

    async def fake_request_backend(_backend, _method, _path, **_kwargs):
        raise HTTPException(status_code=403, detail="forbidden")

    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(client.user_info("local-123"))

    assert exc_info.value.status_code == 403
