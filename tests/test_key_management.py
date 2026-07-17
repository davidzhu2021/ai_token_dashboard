import asyncio
import hashlib
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import main
from backend.cache import TTLCache
from backend.litellm_client import LiteLLMBackend, LiteLLMClient


def make_client() -> tuple[LiteLLMClient, LiteLLMBackend]:
    client = object.__new__(LiteLLMClient)
    backend = LiteLLMBackend(id="primary", label="Primary", base_url="https://example.test", admin_key="test-key")
    client.backends = [backend]
    client._backend_map = {backend.id: backend}
    client._key_cache = TTLCache()
    return client, backend


def test_key_list_only_returns_mask_and_hash_identifier(monkeypatch) -> None:
    client, backend = make_client()
    raw_key = "sk-super-secret-ABCD"

    async def fake_request_backend(_backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        assert method == "GET"
        assert path == "/key/list"
        return {
            "keys": [
                {
                    "token": raw_key,
                    "key_name": "sk-...ABCD",
                    "key_alias": "internal-alias",
                    "metadata": {"display_name": "我的 Codex", "purpose": "本机使用"},
                    "models": ["gpt-5"],
                    "created_at": "2026-07-01T01:02:03Z",
                    "last_used_at": "2026-07-10T01:02:03Z",
                    "expires": None,
                    "spend": 1.25,
                }
            ]
        }

    monkeypatch.setattr(client, "request_backend", fake_request_backend)
    keys = asyncio.run(client.keys_for_user("user-1", backend))

    assert keys[0]["id"] == hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    assert keys[0]["masked"] == "sk-...ABCD"
    assert raw_key not in str(keys)
    assert keys[0]["name"] == "我的 Codex"
    assert keys[0]["models"] == ["gpt-5"]
    assert keys[0]["_backendId"] == "primary"
    assert keys[0]["_userId"] == "user-1"


def test_key_list_does_not_fake_suffix_from_hash(monkeypatch) -> None:
    client, backend = make_client()
    token_hash = "a" * 60 + "BEEF"

    async def fake_request_backend(_backend: LiteLLMBackend, _method: str, _path: str, **_kwargs: Any) -> dict[str, Any]:
        return {"keys": [{"token": token_hash, "key_alias": "old-key", "metadata": {}}]}

    monkeypatch.setattr(client, "request_backend", fake_request_backend)
    keys = asyncio.run(client.keys_for_user("user-1", backend))

    assert keys[0]["id"] == token_hash
    assert keys[0]["masked"] == "sk-...----"
    assert "BEEF" not in keys[0]["masked"]


@pytest.mark.parametrize(
    ("duration", "expected_duration"),
    [("never", None), ("30d", "30d"), ("90d", "90d")],
)
def test_create_key_uses_primary_user_llm_type_duration_and_clears_cache(
    monkeypatch, duration: str, expected_duration: str | None
) -> None:
    client, backend = make_client()
    captured: dict[str, Any] = {}
    client._key_cache.set("keys:primary:user-primary", [{"id": "cached"}], 300)

    async def fake_available_models(_user_id: str, _backend: LiteLLMBackend | None = None) -> tuple[list[str], bool]:
        return ["gpt-5", "claude-sonnet"], False

    async def fake_request_backend(_backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        captured.update({"backend": _backend, "method": method, "path": path, **kwargs})
        return {
            "key": "sk-created-secret-WXYZ",
            "token_id": "hash-created",
            "expires": "2026-10-11T00:00:00Z" if duration != "never" else None,
        }

    monkeypatch.setattr(client, "available_key_models", fake_available_models)
    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    created = asyncio.run(
        client.create_key(
            "user-primary",
            "我的密钥",
            "用于本机",
            duration,
            [],
            "employee@example.com",
        )
    )

    body = captured["json"]
    assert captured["method"] == "POST"
    assert captured["path"] == "/key/generate"
    assert body["user_id"] == "user-primary"
    assert body["key_type"] == "llm_api"
    assert body["models"] == ["gpt-5", "claude-sonnet"]
    assert body["max_budget"] == 100
    assert body["budget_duration"] == "1d"
    assert body["key_alias"].startswith("ai-usage-")
    assert body["metadata"]["display_name"] == "我的密钥"
    assert body.get("duration") == expected_duration
    assert created["masked"] == "sk-...WXYZ"
    assert client._key_cache.get("keys:primary:user-primary")[0] is False


def test_create_key_requires_model_subset(monkeypatch) -> None:
    client, _ = make_client()
    requested = False

    async def fake_available_models(_user_id: str, _backend: LiteLLMBackend | None = None) -> tuple[list[str], bool]:
        return ["gpt-5"], False

    async def fake_request_backend(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal requested
        requested = True
        return {}

    monkeypatch.setattr(client, "available_key_models", fake_available_models)
    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(client.create_key("user-primary", "我的密钥", "", "never", ["claude-sonnet"], "employee@example.com"))

    assert exc.value.status_code == 400
    assert requested is False


def test_unrestricted_user_may_create_all_model_key(monkeypatch) -> None:
    client, _ = make_client()
    captured: dict[str, Any] = {}

    async def fake_available_models(_user_id: str, _backend: LiteLLMBackend | None = None) -> tuple[list[str], bool]:
        return ["gpt-5", "claude-sonnet"], True

    async def fake_request_backend(_backend: LiteLLMBackend, _method: str, _path: str, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"key": "sk-unrestricted-1234", "token_id": "hash-new"}

    monkeypatch.setattr(client, "available_key_models", fake_available_models)
    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    asyncio.run(client.create_key("user-primary", "全部模型", "", "never", [], "employee@example.com"))
    assert captured["json"]["models"] == ["gpt-5", "claude-sonnet"]


def test_all_proxy_models_expands_to_real_models_for_new_key(monkeypatch) -> None:
    client, backend = make_client()
    requests: list[tuple[str, str]] = []
    captured: dict[str, Any] = {}

    async def fake_request_backend(_backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        requests.append((method, path))
        if path == "/v2/user/info":
            return {"models": ["all-proxy-models"]}
        if path == "/models":
            return {"data": [{"id": "gpt-5"}, {"model_name": "claude-sonnet"}]}
        if path == "/key/generate":
            captured.update(kwargs)
            return {"key": "sk-created-ABCD", "token_id": "hash-new"}
        raise AssertionError(f"unexpected request {method} {path}")

    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    asyncio.run(client.create_key("user-primary", "全部模型", "", "never", [], "employee@example.com"))

    assert ("GET", "/v2/user/info") in requests
    assert ("GET", "/models") in requests
    assert captured["json"]["models"] == ["claude-sonnet", "gpt-5"]


def test_no_default_models_uses_team_models_for_new_key(monkeypatch) -> None:
    client, _ = make_client()
    captured: dict[str, Any] = {}

    async def fake_request_backend(_backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if path == "/v2/user/info":
            return {"models": ["no-default-models"], "teams": ["team-a"]}
        if path == "/team/info":
            assert kwargs["params"] == {"team_id": "team-a"}
            return {"team_info": {"team_id": "team-a", "models": ["team-gpt", "team-claude"]}}
        if path == "/key/generate":
            captured.update(kwargs)
            return {"key": "sk-team-ABCD", "token_id": "hash-team"}
        raise AssertionError(f"unexpected request {method} {path}")

    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    asyncio.run(client.create_key("user-primary", "团队模型", "", "never", [], "employee@example.com"))

    assert captured["json"]["models"] == ["team-claude", "team-gpt"]


def test_no_default_models_without_team_models_rejects_new_key(monkeypatch) -> None:
    client, _ = make_client()
    requested = False

    async def fake_request_backend(_backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        nonlocal requested
        if path == "/v2/user/info":
            return {"models": ["no-default-models"], "teams": ["team-a"]}
        if path == "/team/info":
            return {"team_info": {"team_id": "team-a", "models": []}}
        if path == "/key/generate":
            requested = True
            return {}
        raise AssertionError(f"unexpected request {method} {path}")

    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(client.create_key("user-primary", "无权限", "", "never", [], "employee@example.com"))

    assert exc.value.status_code == 403
    assert "当前账号没有可用于创建访问密钥的模型权限" in str(exc.value.detail)
    assert requested is False


def test_regenerate_checks_fresh_ownership_immediately_revokes_and_clears_cache(monkeypatch) -> None:
    client, backend = make_client()
    client._key_cache.set("keys:primary:user-primary", [{"id": "stale-hash"}], 300)
    captured: dict[str, Any] = {}

    async def fake_keys_for_user(_user_id: str, _backend: LiteLLMBackend | None = None, refresh: bool = False):
        assert (_user_id, _backend, refresh) == ("user-primary", backend, True)
        return [{"id": "owned-hash"}]

    async def fake_request_backend(_backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        captured.update({"method": method, "path": path, **kwargs})
        return {"key": "sk-regenerated-EFGH"}

    monkeypatch.setattr(client, "keys_for_user", fake_keys_for_user)
    monkeypatch.setattr(client, "request_backend", fake_request_backend)
    regenerated = asyncio.run(client.regenerate_key("owned-hash", "user-primary", "employee@example.com"))

    assert regenerated == {"key": "sk-regenerated-EFGH", "id": hashlib.sha256(b"sk-regenerated-EFGH").hexdigest()}
    assert captured["path"] == "/key/owned-hash/regenerate"
    assert "params" not in captured
    assert captured["json"] == {"grace_period": "0s"}
    assert client._key_cache.get("keys:primary:user-primary")[0] is False


def test_regenerate_uses_encoded_official_path(monkeypatch) -> None:
    client, backend = make_client()
    captured: dict[str, Any] = {}

    async def fake_keys_for_user(_user_id: str, _backend: LiteLLMBackend | None = None, refresh: bool = False):
        assert refresh is True
        return [{"id": "hash/with space"}]

    async def fake_request_backend(_backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        captured.update({"method": method, "path": path, **kwargs})
        return {"key": "sk-regenerated-EFGH"}

    monkeypatch.setattr(client, "keys_for_user", fake_keys_for_user)
    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    asyncio.run(client.regenerate_key("hash/with space", "user-primary", "employee@example.com"))

    assert captured["path"] == "/key/hash%2Fwith%20space/regenerate"
    assert captured["json"] == {"grace_period": "0s"}


def test_regenerate_rejects_unowned_key(monkeypatch) -> None:
    client, backend = make_client()
    client._key_cache.set("keys:primary:user-primary", [{"id": "owned-hash"}], 300)
    requested = False

    async def fake_keys_for_user(_user_id: str, _backend: LiteLLMBackend | None = None, refresh: bool = False):
        assert (_user_id, _backend, refresh) == ("user-primary", backend, True)
        return [{"id": "owned-hash"}]

    async def fake_request_backend(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal requested
        requested = True
        return {}

    monkeypatch.setattr(client, "keys_for_user", fake_keys_for_user)
    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(client.regenerate_key("other-hash", "user-primary", "employee@example.com"))

    assert exc.value.status_code == 403
    assert requested is False


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"has_license": True, "license_type": "enterprise"}, True),
        ({"has_license": False, "license_type": "community"}, False),
    ],
)
def test_atomic_regeneration_capability_uses_license_type(monkeypatch, payload, expected) -> None:
    client, _ = make_client()

    async def fake_request_backend(*_args, **_kwargs):
        return payload

    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    assert asyncio.run(client.supports_atomic_key_regeneration("user-1")) is expected


def test_atomic_regeneration_capability_does_not_hide_server_failure(monkeypatch) -> None:
    client, _ = make_client()

    async def fake_request_backend(*_args, **_kwargs):
        raise HTTPException(status_code=503, detail="unavailable")

    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(client.supports_atomic_key_regeneration("user-1"))

    assert exc.value.status_code == 503


def test_delete_checks_fresh_ownership_calls_upstream_and_clears_cache(monkeypatch) -> None:
    client, backend = make_client()
    client._key_cache.set("keys:primary:user-primary", [{"id": "stale"}], 300)
    captured: dict[str, Any] = {}

    async def fake_keys_for_user(_user_id: str, _backend: LiteLLMBackend | None = None, refresh: bool = False):
        assert (_user_id, _backend, refresh) == ("user-primary", backend, True)
        return [{"id": "owned-hash"}]

    async def fake_request_backend(_backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        captured.update({"backend": _backend, "method": method, "path": path, **kwargs})
        return {"deleted_keys": ["owned-hash"]}

    monkeypatch.setattr(client, "keys_for_user", fake_keys_for_user)
    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    deleted = asyncio.run(client.delete_key("owned-hash", "user-primary", "employee@example.com"))

    assert deleted == {"id": "owned-hash"}
    assert captured["backend"] == backend
    assert captured["method"] == "POST"
    assert captured["path"] == "/key/delete"
    assert captured["json"] == {"keys": ["owned-hash"]}
    assert captured["headers"] == {"litellm-changed-by": "employee@example.com"}
    assert client._key_cache.get("keys:primary:user-primary")[0] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"deleted_keys": {"deleted_keys": ["owned-hash"]}},
        {"deleted_keys": 1},
    ],
)
def test_delete_accepts_compatible_upstream_confirmation_shapes(monkeypatch, payload) -> None:
    client, _ = make_client()

    async def fake_keys_for_user(*_args: Any, **_kwargs: Any):
        return [{"id": "owned-hash"}]

    async def fake_request_backend(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return payload

    monkeypatch.setattr(client, "keys_for_user", fake_keys_for_user)
    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    assert asyncio.run(client.delete_key("owned-hash", "user-primary", "employee@example.com")) == {"id": "owned-hash"}


def test_delete_rejects_unowned_key_without_upstream_request(monkeypatch) -> None:
    client, backend = make_client()
    requested = False

    async def fake_keys_for_user(_user_id: str, _backend: LiteLLMBackend | None = None, refresh: bool = False):
        assert refresh is True
        return [{"id": "owned-hash"}]

    async def fake_request_backend(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal requested
        requested = True
        return {"deleted_keys": ["other-hash"]}

    monkeypatch.setattr(client, "keys_for_user", fake_keys_for_user)
    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(client.delete_key("other-hash", "user-primary", "employee@example.com"))

    assert exc.value.status_code == 403
    assert requested is False


def test_delete_requires_upstream_confirmation(monkeypatch) -> None:
    client, _ = make_client()

    async def fake_keys_for_user(*_args: Any, **_kwargs: Any):
        return [{"id": "owned-hash"}]

    async def fake_request_backend(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"deleted_keys": []}

    monkeypatch.setattr(client, "keys_for_user", fake_keys_for_user)
    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(client.delete_key("owned-hash", "user-primary", "employee@example.com"))

    assert exc.value.status_code == 502


def test_primary_account_selection_ignores_history_backends() -> None:
    upstream_user = {
        "matched_user_ids": ["history:old-user", "user-primary"],
        "matched_accounts": [
            {"backend": "history", "user_id": "old-user"},
            {"backend": "primary", "user_id": "user-primary"},
        ],
    }
    assert main.primary_upstream_user_id(upstream_user) == "user-primary"


def test_keys_endpoint_merges_pending_rotation_state(monkeypatch) -> None:
    class FakeClient:
        async def available_key_models(self, user_id):
            assert user_id == "user-1"
            return ["gpt-5"], False

        async def keys_for_user_ids(self, user_ids, refresh=False):
            assert user_ids == ["user-1"]
            assert refresh is False
            return [
                {
                    "_backendId": "primary",
                    "_userId": "user-1",
                    "id": "new-hash",
                    "name": "工作密钥",
                    "purpose": "Codex",
                    "status": "正常",
                }
            ]

    class FakeVault:
        def has(self, backend_id, user_id, key_id):
            return (backend_id, user_id, key_id) == ("primary", "user-1", "new-hash")

        def pending_rotations(self, backend_id, user_id):
            assert (backend_id, user_id) == ("primary", "user-1")
            return [
                {
                    "oldKeyId": "old-hash",
                    "replacementKeyId": "new-hash",
                    "cleanupTarget": "old",
                    "lastError": "delete failed",
                    "createdAt": "2026-07-14T00:00:00+00:00",
                    "updatedAt": "2026-07-14T00:00:00+00:00",
                }
            ]

    async def fake_current_upstream_user(_request):
        return {"email": "employee@example.com"}, {"matched_user_ids": ["user-1"]}

    monkeypatch.setattr(main, "client", lambda: FakeClient())
    monkeypatch.setattr(main, "key_vault", lambda: FakeVault())
    monkeypatch.setattr(main, "current_upstream_user", fake_current_upstream_user)

    with TestClient(main.app) as app_client:
        response = app_client.get("/api/me/keys")

    assert response.status_code == 200
    key = response.json()["keys"][0]
    assert key["id"] == "new-hash"
    assert key["revealable"] is True
    assert key["cleanupRequired"] is True
    assert key["recoveryRequired"] is False
    assert key["oldKeyId"] == "old-hash"
    assert key["replacementKeyId"] == "new-hash"


def test_key_audit_never_writes_plain_key(monkeypatch, tmp_path) -> None:
    class RequestStub:
        client = type("Client", (), {"host": "127.0.0.1"})()

    raw_key = "sk-audit-secret-IJKL"
    monkeypatch.setattr(main, "ROOT_DIR", tmp_path)
    main.write_key_audit("create", "employee@example.com", raw_key, RequestStub(), "success")  # type: ignore[arg-type]

    content = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert raw_key not in content
    assert "sk-" not in content
    assert hashlib.sha256(raw_key.encode("utf-8")).hexdigest() in content


def test_public_key_only_adds_revealable_and_removes_internal_scope() -> None:
    result = main.public_key(
        {"id": "hash-1", "masked": "sk-...ABCD", "_backendId": "primary", "_userId": "user-1"},
        True,
    )

    assert result == {"id": "hash-1", "masked": "sk-...ABCD", "revealable": True}


def test_reveal_endpoint_returns_owned_vaulted_key_without_cache(monkeypatch) -> None:
    key_id = "owned-hash"
    plaintext = "sk-revealed-secret-ABCD"

    class FakeClient:
        async def keys_for_user_ids(self, user_ids, refresh=False):
            assert user_ids == ["user-1"]
            assert refresh is True
            return [{"id": key_id, "_backendId": "primary", "_userId": "user-1"}]

    class FakeVault:
        def reveal(self, backend_id, user_id, requested_key_id):
            assert (backend_id, user_id, requested_key_id) == ("primary", "user-1", key_id)
            return plaintext

    async def fake_current_upstream_user(_request):
        return {"email": "employee@example.com"}, {"matched_user_ids": ["user-1"]}

    audits: list[tuple[str, str]] = []
    monkeypatch.setattr(main, "client", lambda: FakeClient())
    monkeypatch.setattr(main, "key_vault", lambda: FakeVault())
    monkeypatch.setattr(main, "current_upstream_user", fake_current_upstream_user)
    monkeypatch.setattr(main, "write_key_audit", lambda event, _email, _key_id, _request, result: audits.append((event, result)))

    with TestClient(main.app) as app_client:
        response = app_client.post(f"/api/me/keys/{key_id}/reveal")

    assert response.status_code == 200
    assert response.json() == {"key": plaintext}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert audits == [("reveal", "success")]


def test_reveal_endpoint_rejects_unowned_key(monkeypatch) -> None:
    class FakeClient:
        async def keys_for_user_ids(self, _user_ids, refresh=False):
            assert refresh is True
            return [{"id": "owned-hash", "_backendId": "primary", "_userId": "user-1"}]

    async def fake_current_upstream_user(_request):
        return {"email": "employee@example.com"}, {"matched_user_ids": ["user-1"]}

    monkeypatch.setattr(main, "client", lambda: FakeClient())
    monkeypatch.setattr(main, "current_upstream_user", fake_current_upstream_user)
    monkeypatch.setattr(main, "write_key_audit", lambda *_args: None)

    with TestClient(main.app) as app_client:
        response = app_client.post("/api/me/keys/other-hash/reveal")

    assert response.status_code == 403


def test_reveal_endpoint_explains_legacy_key_is_not_stored(monkeypatch) -> None:
    class FakeClient:
        async def keys_for_user_ids(self, _user_ids, refresh=False):
            return [{"id": "owned-hash", "_backendId": "primary", "_userId": "user-1"}]

    class FakeVault:
        def reveal(self, *_args):
            return None

    async def fake_current_upstream_user(_request):
        return {"email": "employee@example.com"}, {"matched_user_ids": ["user-1"]}

    monkeypatch.setattr(main, "client", lambda: FakeClient())
    monkeypatch.setattr(main, "key_vault", lambda: FakeVault())
    monkeypatch.setattr(main, "current_upstream_user", fake_current_upstream_user)
    monkeypatch.setattr(main, "write_key_audit", lambda *_args: None)

    with TestClient(main.app) as app_client:
        response = app_client.post("/api/me/keys/owned-hash/reveal")

    assert response.status_code == 404
    assert "再生成后查看" in response.json()["detail"]


def test_store_created_key_failure_returns_warning(monkeypatch) -> None:
    class BrokenVault:
        def store(self, *_args):
            raise main.KeyVaultError("write failed")

    monkeypatch.setattr(main, "key_vault", lambda: BrokenVault())
    warning = main.store_created_key("user-1", {"id": "hash-1", "key": "sk-secret-ABCD"})

    assert "加密保管失败" in warning
    assert "sk-secret-ABCD" not in warning


def test_create_endpoint_stores_key_and_reports_revealable(monkeypatch) -> None:
    class FakeClient:
        async def create_key(self, user_id, name, purpose, duration, models, changed_by):
            assert user_id == "user-1"
            return {"key": "sk-created-ABCD", "id": "hash-new", "masked": "sk-...ABCD", "expiresAt": "永不过期"}

    class FakeVault:
        def __init__(self) -> None:
            self.stored = None

        def store(self, backend_id, user_id, key_id, plaintext):
            self.stored = (backend_id, user_id, key_id, plaintext)

    vault = FakeVault()

    async def fake_current_upstream_user(_request):
        return {"email": "employee@example.com"}, {
            "matched_user_ids": ["user-1"],
            "matched_accounts": [{"backend": "primary", "user_id": "user-1"}],
        }

    monkeypatch.setattr(main, "client", lambda: FakeClient())
    monkeypatch.setattr(main, "key_vault", lambda: vault)
    monkeypatch.setattr(main, "current_upstream_user", fake_current_upstream_user)
    monkeypatch.setattr(main, "write_key_audit", lambda *_args: None)

    with TestClient(main.app) as app_client:
        response = app_client.post("/api/me/keys", json={"name": "我的密钥", "purpose": "", "duration": "never", "models": []})

    assert response.status_code == 200
    assert response.json()["revealable"] is True
    assert response.json()["warning"] == ""
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert vault.stored == ("primary", "user-1", "hash-new", "sk-created-ABCD")


def test_create_endpoint_returns_plaintext_and_warning_when_vault_fails(monkeypatch) -> None:
    class FakeClient:
        async def create_key(self, *_args):
            return {"key": "sk-created-ABCD", "id": "hash-new", "masked": "sk-...ABCD", "expiresAt": "永不过期"}

    class BrokenVault:
        def store(self, *_args):
            raise main.KeyVaultError("write failed")

    async def fake_current_upstream_user(_request):
        return {"email": "employee@example.com"}, {
            "matched_user_ids": ["user-1"],
            "matched_accounts": [{"backend": "primary", "user_id": "user-1"}],
        }

    monkeypatch.setattr(main, "client", lambda: FakeClient())
    monkeypatch.setattr(main, "key_vault", lambda: BrokenVault())
    monkeypatch.setattr(main, "current_upstream_user", fake_current_upstream_user)
    monkeypatch.setattr(main, "write_key_audit", lambda *_args: None)

    with TestClient(main.app) as app_client:
        response = app_client.post("/api/me/keys", json={"name": "我的密钥", "purpose": "", "duration": "never", "models": []})

    payload = response.json()
    assert response.status_code == 200
    assert payload["key"] == "sk-created-ABCD"
    assert payload["revealable"] is False
    assert "加密保管失败" in payload["warning"]


def test_regenerate_endpoint_replaces_old_vault_record(monkeypatch) -> None:
    class FakeClient:
        def _decode_account_id(self, user_id):
            return LiteLLMBackend(id="primary", label="Primary", base_url="", admin_key=""), user_id

        async def regenerate_key(self, key_id, user_id, changed_by):
            assert (key_id, user_id) == ("old-hash", "user-1")
            return {"key": "sk-regenerated-EFGH", "id": "new-hash"}

        async def supports_atomic_key_regeneration(self, _user_id):
            return True

    class FakeVault:
        def __init__(self) -> None:
            self.replaced = None

        def replace(self, backend_id, user_id, old_key_id, new_key_id, plaintext):
            self.replaced = (backend_id, user_id, old_key_id, new_key_id, plaintext)

        def pending_rotation(self, *_args):
            return None

    vault = FakeVault()

    async def fake_current_upstream_user(_request):
        return {"email": "employee@example.com"}, {
            "matched_user_ids": ["user-1"],
            "matched_accounts": [{"backend": "primary", "user_id": "user-1"}],
        }

    monkeypatch.setattr(main, "client", lambda: FakeClient())
    monkeypatch.setattr(main, "key_vault", lambda: vault)
    monkeypatch.setattr(main, "current_upstream_user", fake_current_upstream_user)
    monkeypatch.setattr(main, "write_key_audit", lambda *_args: None)

    with TestClient(main.app) as app_client:
        response = app_client.post("/api/me/keys/old-hash/regenerate")

    assert response.status_code == 200
    assert response.json()["revealable"] is True
    assert response.json()["rotationMode"] == "atomic"
    assert response.json()["oldKeyDisabled"] is True
    assert response.json()["cleanupRequired"] is False
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert vault.replaced == ("primary", "user-1", "old-hash", "new-hash", "sk-regenerated-EFGH")


def test_replacement_key_intersects_models_inherits_limits_and_forces_llm_type(monkeypatch) -> None:
    client, backend = make_client()
    captured: dict[str, Any] = {}

    async def fake_keys_for_user(*_args, **_kwargs):
        return [{
            "id": "old-hash",
            "status": "正常",
            "name": "工作密钥",
            "purpose": "Codex",
            "_rotation": {
                "models": ["gpt-5", "removed-model"],
                "metadata": {"owner": "me"},
                "max_budget": 10,
                "spend": 3.25,
                "budget_duration": "1d",
                "rpm_limit": 20,
                "allowed_routes": ["llm_api_routes"],
            },
        }]

    async def fake_available_models(*_args, **_kwargs):
        return ["gpt-5", "claude-sonnet"], False

    async def fake_request_backend(_backend, method, path, **kwargs):
        captured.update({"backend": _backend, "method": method, "path": path, **kwargs})
        return {"key": "sk-replacement-EFGH", "token_id": "new-hash"}

    monkeypatch.setattr(client, "keys_for_user", fake_keys_for_user)
    monkeypatch.setattr(client, "available_key_models", fake_available_models)
    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    result = asyncio.run(client.create_replacement_key("old-hash", "user-1", "employee@example.com"))

    assert result == {"key": "sk-replacement-EFGH", "id": "new-hash", "expiresAt": "永不过期"}
    body = captured["json"]
    assert captured["path"] == "/key/generate"
    assert body["key_type"] == "llm_api"
    assert body["models"] == ["gpt-5"]
    assert body["max_budget"] == 10
    assert body["spend"] == 3.25
    assert body["budget_duration"] == "1d"
    assert body["rpm_limit"] == 20
    assert body["metadata"]["display_name"] == "工作密钥"
    assert body["metadata"]["purpose"] == "Codex"


def test_replacement_key_rejects_custom_routes(monkeypatch) -> None:
    client, _ = make_client()

    async def fake_keys_for_user(*_args, **_kwargs):
        return [{"id": "old-hash", "status": "正常", "_rotation": {"models": ["gpt-5"], "allowed_routes": ["/key/info"]}}]

    monkeypatch.setattr(client, "keys_for_user", fake_keys_for_user)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(client.create_replacement_key("old-hash", "user-1", "employee@example.com"))

    assert exc.value.status_code == 409


def test_replacement_key_rejects_budget_windows(monkeypatch) -> None:
    client, _ = make_client()

    async def fake_keys_for_user(*_args, **_kwargs):
        return [{
            "id": "old-hash",
            "status": "正常",
            "_rotation": {
                "models": ["gpt-5"],
                "max_budget": 100,
                "budget_duration": "1d",
                "budget_limits": [{"max_budget": 10, "budget_duration": "1h"}],
            },
        }]

    monkeypatch.setattr(client, "keys_for_user", fake_keys_for_user)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(client.create_replacement_key("old-hash", "user-1", "employee@example.com"))

    assert exc.value.status_code == 409


def test_replacement_endpoint_persists_cleanup_when_old_delete_fails(monkeypatch) -> None:
    class FakeClient:
        def _decode_account_id(self, user_id):
            return LiteLLMBackend(id="primary", label="Primary", base_url="", admin_key=""), user_id

        async def supports_atomic_key_regeneration(self, _user_id):
            return False

        async def create_replacement_key(self, *_args):
            return {"key": "sk-replacement-EFGH", "id": "new-hash"}

        async def delete_key(self, key_id, *_args):
            assert key_id == "old-hash"
            raise HTTPException(status_code=502, detail="delete failed")

    class FakeVault:
        def __init__(self):
            self.pending = None

        def pending_rotation(self, *_args):
            return None

        def store(self, *_args):
            return None

        def record_pending_rotation(self, *args):
            self.pending = args

    vault = FakeVault()

    async def fake_current_upstream_user(_request):
        return {"email": "employee@example.com"}, {"matched_user_ids": ["user-1"]}

    monkeypatch.setattr(main, "client", lambda: FakeClient())
    monkeypatch.setattr(main, "key_vault", lambda: vault)
    monkeypatch.setattr(main, "current_upstream_user", fake_current_upstream_user)
    monkeypatch.setattr(main, "write_key_audit", lambda *_args: None)

    with TestClient(main.app) as app_client:
        response = app_client.post("/api/me/keys/old-hash/regenerate")

    assert response.status_code == 200
    assert response.json()["cleanupRequired"] is True
    assert response.json()["oldKeyDisabled"] is False
    assert response.json()["replacementKeyId"] == "new-hash"
    assert vault.pending[:5] == ("primary", "user-1", "old-hash", "new-hash", "old")


def test_replacement_endpoint_compensates_new_key_when_vault_fails(monkeypatch) -> None:
    deleted = []

    class FakeClient:
        def _decode_account_id(self, user_id):
            return LiteLLMBackend(id="primary", label="Primary", base_url="", admin_key=""), user_id

        async def supports_atomic_key_regeneration(self, _user_id):
            return False

        async def create_replacement_key(self, *_args):
            return {"key": "sk-replacement-EFGH", "id": "new-hash", "expiresAt": "永不过期"}

        async def delete_key(self, key_id, *_args):
            deleted.append(key_id)
            return {"id": key_id}

    class BrokenVault:
        def pending_rotation(self, *_args):
            return None

        def store(self, *_args):
            raise main.KeyVaultError("write failed")

    async def fake_current_upstream_user(_request):
        return {"email": "employee@example.com"}, {"matched_user_ids": ["user-1"]}

    monkeypatch.setattr(main, "client", lambda: FakeClient())
    monkeypatch.setattr(main, "key_vault", lambda: BrokenVault())
    monkeypatch.setattr(main, "current_upstream_user", fake_current_upstream_user)
    monkeypatch.setattr(main, "write_key_audit", lambda *_args: None)

    with TestClient(main.app) as app_client:
        response = app_client.post("/api/me/keys/old-hash/regenerate")

    assert response.status_code == 503
    assert deleted == ["new-hash"]


def test_replacement_endpoint_returns_plaintext_if_vault_and_compensation_fail(monkeypatch) -> None:
    class FakeClient:
        def _decode_account_id(self, user_id):
            return LiteLLMBackend(id="primary", label="Primary", base_url="", admin_key=""), user_id

        async def supports_atomic_key_regeneration(self, _user_id):
            return False

        async def create_replacement_key(self, *_args):
            return {"key": "sk-replacement-EFGH", "id": "new-hash", "expiresAt": "永不过期"}

        async def delete_key(self, *_args):
            raise HTTPException(status_code=502, detail="delete failed")

    class BrokenVault:
        def pending_rotation(self, *_args):
            return None

        def store(self, *_args):
            raise main.KeyVaultError("write failed")

    async def fake_current_upstream_user(_request):
        return {"email": "employee@example.com"}, {"matched_user_ids": ["user-1"]}

    monkeypatch.setattr(main, "client", lambda: FakeClient())
    monkeypatch.setattr(main, "key_vault", lambda: BrokenVault())
    monkeypatch.setattr(main, "current_upstream_user", fake_current_upstream_user)
    monkeypatch.setattr(main, "write_key_audit", lambda *_args: None)

    with TestClient(main.app) as app_client:
        response = app_client.post("/api/me/keys/old-hash/regenerate")

    payload = response.json()
    assert response.status_code == 200
    assert payload["key"] == "sk-replacement-EFGH"
    assert payload["oldKeyDisabled"] is False
    assert payload["cleanupRequired"] is False
    assert payload["recoveryRequired"] is True
    assert payload["revealable"] is False


def test_disable_old_endpoint_uses_persisted_replacement(monkeypatch) -> None:
    calls = []

    class FakeClient:
        def _decode_account_id(self, user_id):
            return LiteLLMBackend(id="primary", label="Primary", base_url="", admin_key=""), user_id

        async def disable_pending_old_key(self, old_id, replacement_id, user_id, changed_by):
            calls.append((old_id, replacement_id, user_id, changed_by))

    class FakeVault:
        def pending_rotation(self, *_args):
            return {"cleanupTarget": "old", "replacementKeyId": "new-hash"}

        def complete_pending_rotation(self, *args):
            calls.append(args)

    async def fake_current_upstream_user(_request):
        return {"email": "employee@example.com"}, {"matched_user_ids": ["user-1"]}

    monkeypatch.setattr(main, "client", lambda: FakeClient())
    monkeypatch.setattr(main, "key_vault", lambda: FakeVault())
    monkeypatch.setattr(main, "current_upstream_user", fake_current_upstream_user)
    monkeypatch.setattr(main, "write_key_audit", lambda *_args: None)

    with TestClient(main.app) as app_client:
        response = app_client.post("/api/me/keys/old-hash/disable-old", json={"replacementKeyId": "new-hash"})

    assert response.status_code == 200
    assert response.json()["cleanupRequired"] is False
    assert calls[0] == ("old-hash", "new-hash", "user-1", "employee@example.com")


def test_delete_endpoint_finds_owner_cleans_vault_and_audits(monkeypatch) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls = []

        async def delete_key(self, key_id, user_id, changed_by):
            self.calls.append((key_id, user_id, changed_by))
            if user_id == "history:old-user":
                raise HTTPException(status_code=403, detail="不能删除不属于自己的访问密钥")
            return {"id": key_id}

        def _decode_account_id(self, user_id):
            assert user_id == "user-1"
            return LiteLLMBackend(id="primary", label="Primary", base_url="", admin_key=""), user_id

    class FakeVault:
        def __init__(self) -> None:
            self.deleted = None

        def delete(self, backend_id, user_id, key_id):
            self.deleted = (backend_id, user_id, key_id)

    fake_client = FakeClient()
    vault = FakeVault()
    audits = []

    async def fake_current_upstream_user(_request):
        return {"email": "employee@example.com"}, {"matched_user_ids": ["history:old-user", "user-1"]}

    monkeypatch.setattr(main, "client", lambda: fake_client)
    monkeypatch.setattr(main, "key_vault", lambda: vault)
    monkeypatch.setattr(main, "current_upstream_user", fake_current_upstream_user)
    monkeypatch.setattr(main, "write_key_audit", lambda event, email, key_id, _request, result: audits.append((event, email, key_id, result)))

    with TestClient(main.app) as app_client:
        response = app_client.delete("/api/me/keys/owned-hash")

    assert response.status_code == 200
    assert response.json() == {"deleted": True, "warning": ""}
    assert fake_client.calls == [
        ("owned-hash", "history:old-user", "employee@example.com"),
        ("owned-hash", "user-1", "employee@example.com"),
    ]
    assert vault.deleted == ("primary", "user-1", "owned-hash")
    assert audits == [("delete", "employee@example.com", "owned-hash", "success")]


def test_delete_endpoint_rejects_unowned_key_without_vault_cleanup(monkeypatch) -> None:
    class FakeClient:
        async def delete_key(self, *_args):
            raise HTTPException(status_code=403, detail="不能删除不属于自己的访问密钥")

    class FakeVault:
        def delete(self, *_args):
            raise AssertionError("unowned key must not touch the vault")

    audits = []

    async def fake_current_upstream_user(_request):
        return {"email": "employee@example.com"}, {"matched_user_ids": ["user-1"]}

    monkeypatch.setattr(main, "client", lambda: FakeClient())
    monkeypatch.setattr(main, "key_vault", lambda: FakeVault())
    monkeypatch.setattr(main, "current_upstream_user", fake_current_upstream_user)
    monkeypatch.setattr(main, "write_key_audit", lambda event, _email, _key_id, _request, result: audits.append((event, result)))

    with TestClient(main.app) as app_client:
        response = app_client.delete("/api/me/keys/other-hash")

    assert response.status_code == 403
    assert audits == [("delete", "failed")]


def test_delete_endpoint_upstream_failure_does_not_clean_vault(monkeypatch) -> None:
    class FakeClient:
        async def delete_key(self, *_args):
            raise HTTPException(status_code=502, detail="上游删除失败")

    class FakeVault:
        def delete(self, *_args):
            raise AssertionError("failed upstream deletion must not touch the vault")

    async def fake_current_upstream_user(_request):
        return {"email": "employee@example.com"}, {"matched_user_ids": ["user-1"]}

    monkeypatch.setattr(main, "client", lambda: FakeClient())
    monkeypatch.setattr(main, "key_vault", lambda: FakeVault())
    monkeypatch.setattr(main, "current_upstream_user", fake_current_upstream_user)
    monkeypatch.setattr(main, "write_key_audit", lambda *_args: None)

    with TestClient(main.app) as app_client:
        response = app_client.delete("/api/me/keys/owned-hash")

    assert response.status_code == 502


def test_delete_endpoint_reports_vault_cleanup_warning_after_upstream_success(monkeypatch) -> None:
    class FakeClient:
        async def delete_key(self, key_id, user_id, changed_by):
            return {"id": key_id}

        def _decode_account_id(self, user_id):
            return LiteLLMBackend(id="primary", label="Primary", base_url="", admin_key=""), user_id

    class BrokenVault:
        def delete(self, *_args):
            raise main.KeyVaultError("delete failed")

    audits = []

    async def fake_current_upstream_user(_request):
        return {"email": "employee@example.com"}, {"matched_user_ids": ["user-1"]}

    monkeypatch.setattr(main, "client", lambda: FakeClient())
    monkeypatch.setattr(main, "key_vault", lambda: BrokenVault())
    monkeypatch.setattr(main, "current_upstream_user", fake_current_upstream_user)
    monkeypatch.setattr(main, "write_key_audit", lambda event, _email, _key_id, _request, result: audits.append((event, result)))

    with TestClient(main.app) as app_client:
        response = app_client.delete("/api/me/keys/owned-hash")

    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert "本地加密保管记录清理失败" in response.json()["warning"]
    assert audits == [("delete", "success_vault_failed")]
