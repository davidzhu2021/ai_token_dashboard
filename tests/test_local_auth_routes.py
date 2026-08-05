import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import main
from backend.auth import hash_auth_token, hash_password
from backend.auth_store import AuthStore


class FakeProvisioningClient:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.user_models: list[str] = ["no-default-models"]
        self.key_or_model_calls: list[str] = []

    async def create_internal_user(self, user_id, email, name=None):
        self.created.append({"user_id": user_id, "email": email, "name": name})
        return {"user_id": user_id, "user_email": email}

    async def user_info(self, user_id):
        return {"user_id": user_id, "models": self.user_models, "max_budget": None}

    async def usage_rows_for_user_ids(self, *_args, **_kwargs):
        self.key_or_model_calls.append("usage_rows_for_user_ids")
        return []

    def __getattr__(self, name: str):
        if name in {
            "available_key_models",
            "keys_for_user_ids",
            "create_key",
            "supports_atomic_key_regeneration",
            "regenerate_key",
            "create_replacement_key",
            "disable_pending_old_key",
            "delete_key",
            "models",
        }:
            async def blocked(*_args, **_kwargs):
                self.key_or_model_calls.append(name)
                raise AssertionError(f"inactive local user reached upstream {name}")

            return blocked
        raise AttributeError(name)


def auth_client(tmp_path, monkeypatch, *, signup=True):
    store = AuthStore(tmp_path / "auth.sqlite3")
    upstream = FakeProvisioningClient()
    monkeypatch.setattr(main, "_auth_store", store)
    monkeypatch.setattr(main, "_litellm_client", upstream)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("PASSWORD_LOGIN_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_SIGNUP_ENABLED", "true" if signup else "false")
    monkeypatch.setenv("EMAIL_VERIFICATION_REQUIRED", "true")
    monkeypatch.setenv("AUTH_EMAIL_DEBUG", "true")
    monkeypatch.setenv("APP_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", "example.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.com")
    monkeypatch.setenv("TURNSTILE_ENABLED", "true")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "site-key")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "secret-key")
    async def accept_turnstile(_request, _token):
        return None
    monkeypatch.setattr(main, "verify_turnstile", accept_turnstile)
    async def accept_email(_recipient, _subject, _body):
        return None
    monkeypatch.setattr(main, "send_auth_email", accept_email)
    return TestClient(main.app), store, upstream


def csrf(client: TestClient) -> dict[str, str]:
    token = client.get("/api/auth/csrf").json()["csrfToken"]
    return {"X-CSRF-Token": token}


def test_https_runtime_rejects_default_session_secret(monkeypatch) -> None:
    monkeypatch.setenv("APP_BASE_URL", "https://dashboard.example.com")
    monkeypatch.setenv("SESSION_SECRET", "dev-session-secret-change-me")

    try:
        main.validate_runtime_auth_config()
    except RuntimeError as exc:
        assert "SESSION_SECRET" in str(exc)
    else:
        raise AssertionError("HTTPS deployment must reject the default session secret")


def test_local_runtime_allows_development_session_secret(monkeypatch) -> None:
    monkeypatch.setenv("APP_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("SESSION_SECRET", "dev-session-secret-change-me")

    main.validate_runtime_auth_config()


def test_public_http_runtime_rejects_local_auth(monkeypatch) -> None:
    monkeypatch.setenv("APP_BASE_URL", "http://dashboard.example.com")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("PASSWORD_LOGIN_ENABLED", "true")

    try:
        main.validate_runtime_auth_config()
    except RuntimeError as exc:
        assert "APP_BASE_URL" in str(exc)
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("public password authentication must require HTTPS")


def test_session_cookie_max_age_uses_auth_session_ttl(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_SESSION_TTL_SECONDS", "900")
    assert main.session_cookie_max_age() == 900

    monkeypatch.setenv("AUTH_SESSION_TTL_SECONDS", "30")
    assert main.session_cookie_max_age() == 300

    monkeypatch.setenv("AUTH_SESSION_TTL_SECONDS", "invalid")
    assert main.session_cookie_max_age() == 1_209_600


def test_https_runtime_keeps_sso_available_when_public_signup_is_not_ready(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APP_BASE_URL", "https://dashboard.example.com")
    monkeypatch.setenv("SESSION_SECRET", "x" * 64)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("PASSWORD_LOGIN_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_SIGNUP_ENABLED", "true")
    monkeypatch.setenv("EMAIL_VERIFICATION_REQUIRED", "true")
    monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", "example.com")
    monkeypatch.setenv("AUTH_DATABASE_PATH", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv("TURNSTILE_ENABLED", "true")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "site-key")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "secret-key")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("AUTH_EMAIL_DEBUG", raising=False)
    monkeypatch.delenv("DEV_LOGIN_ENABLED", raising=False)

    main.validate_runtime_auth_config()
    assert main.password_login_unavailable_code() == ""
    assert main.signup_unavailable_code() == "AUTH_SIGNUP_EMAIL_NOT_CONFIGURED"


def test_https_password_login_reports_unavailable_without_turnstile(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("APP_BASE_URL", "https://dashboard.example.com")
    monkeypatch.setenv("SESSION_SECRET", "x" * 64)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("PASSWORD_LOGIN_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_SIGNUP_ENABLED", "false")
    monkeypatch.setenv("AUTH_DATABASE_PATH", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.com")
    monkeypatch.setenv("TURNSTILE_ENABLED", "false")
    monkeypatch.delenv("AUTH_EMAIL_DEBUG", raising=False)
    monkeypatch.delenv("DEV_LOGIN_ENABLED", raising=False)

    main.validate_runtime_auth_config()
    assert main.password_login_unavailable_code() == "AUTH_TURNSTILE_NOT_CONFIGURED"


def test_https_password_login_reports_unavailable_without_explicit_auth_database(monkeypatch) -> None:
    monkeypatch.setenv("APP_BASE_URL", "https://dashboard.example.com")
    monkeypatch.setenv("SESSION_SECRET", "x" * 64)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("PASSWORD_LOGIN_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_SIGNUP_ENABLED", "false")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.com")
    monkeypatch.setenv("TURNSTILE_ENABLED", "true")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "site-key")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "secret-key")
    monkeypatch.delenv("AUTH_DATABASE_PATH", raising=False)
    monkeypatch.delenv("AUTH_DATABASE_URL", raising=False)
    monkeypatch.delenv("AUTH_EMAIL_DEBUG", raising=False)
    monkeypatch.delenv("DEV_LOGIN_ENABLED", raising=False)

    main.validate_runtime_auth_config()
    assert main.password_login_unavailable_code() == "AUTH_DATABASE_NOT_CONFIGURED"


def test_https_password_login_rejects_unsupported_auth_database_url(monkeypatch) -> None:
    monkeypatch.setenv("APP_BASE_URL", "https://dashboard.example.com")
    monkeypatch.setenv("SESSION_SECRET", "x" * 64)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("PASSWORD_LOGIN_ENABLED", "true")
    monkeypatch.setenv("TURNSTILE_ENABLED", "true")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "site-key")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "secret-key")
    monkeypatch.delenv("AUTH_DATABASE_PATH", raising=False)
    monkeypatch.setenv("AUTH_DATABASE_URL", "postgresql://not-supported")

    assert main.password_login_unavailable_code() == "AUTH_DATABASE_NOT_CONFIGURED"


def test_https_runtime_rejects_dev_login(monkeypatch) -> None:
    monkeypatch.setenv("APP_BASE_URL", "https://dashboard.example.com")
    monkeypatch.setenv("SESSION_SECRET", "x" * 64)
    monkeypatch.setenv("DEV_LOGIN_ENABLED", "true")
    monkeypatch.delenv("AUTH_EMAIL_DEBUG", raising=False)

    try:
        main.validate_runtime_auth_config()
    except RuntimeError as exc:
        assert "DEV_LOGIN_ENABLED" in str(exc)
    else:
        raise AssertionError("HTTPS deployment must reject development login")


def put_signup_code(store: AuthStore, email: str, code: str = "123456") -> None:
    store.create_verification_code(
        email,
        "signup",
        hash_auth_token(code),
        datetime.now(timezone.utc) + timedelta(minutes=5),
    )


def test_signup_disabled_blocks_verification_email(tmp_path, monkeypatch) -> None:
    client, store, _ = auth_client(tmp_path, monkeypatch, signup=False)

    response = client.post(
        "/api/auth/verification/request",
        json={"email": "person@example.com", "purpose": "signup"},
        headers=csrf(client),
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "AUTH_SIGNUP_DISABLED"
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM auth_verification_codes").fetchone()[0] == 0


def test_signup_disabled_keeps_password_login_and_recovery_available(tmp_path, monkeypatch) -> None:
    client, _, _ = auth_client(tmp_path, monkeypatch, signup=False)

    config = client.get("/api/auth/config")

    assert config.status_code == 200
    assert config.json()["passwordLoginEnabled"] is True
    assert config.json()["passwordLoginAvailable"] is True
    assert config.json()["publicSignupEnabled"] is False
    assert config.json()["publicSignupAvailable"] is False
    assert config.json()["publicSignupUnavailableCode"] == "AUTH_SIGNUP_DISABLED"
    assert config.json()["passwordRecoveryEnabled"] is True
    assert config.json()["passwordRecoveryAvailable"] is True


def test_https_password_login_remains_available_without_smtp(tmp_path, monkeypatch) -> None:
    client, store, _ = auth_client(tmp_path, monkeypatch)
    monkeypatch.setenv("APP_BASE_URL", "https://dashboard.example.com")
    monkeypatch.setenv("SESSION_SECRET", "x" * 64)
    monkeypatch.setenv("AUTH_DATABASE_PATH", str(tmp_path / "auth.sqlite3"))
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.delenv("AUTH_EMAIL_DEBUG", raising=False)
    store.create_user("person@example.com", "Person", hash_password("password-123"), email_verified=True)

    config = client.get("/api/auth/config")
    logged_in = client.post(
        "/api/auth/login",
        json={"email": "person@example.com", "password": "password-123"},
        headers=csrf(client),
    )

    assert config.status_code == 200
    assert config.json()["passwordLoginAvailable"] is True
    assert config.json()["publicSignupAvailable"] is False
    assert config.json()["publicSignupUnavailableCode"] == "AUTH_SIGNUP_EMAIL_NOT_CONFIGURED"
    assert config.json()["passwordRecoveryAvailable"] is False
    assert config.json()["passwordRecoveryUnavailableCode"] == "AUTH_PASSWORD_EMAIL_NOT_CONFIGURED"
    assert logged_in.status_code == 200


def test_register_login_session_and_entitlement_refresh(tmp_path, monkeypatch) -> None:
    client, store, upstream = auth_client(tmp_path, monkeypatch)
    headers = csrf(client)
    put_signup_code(store, "person@example.com")

    registered = client.post(
        "/api/auth/register",
        json={
            "email": "person@example.com",
            "name": "Person",
            "password": "password-123",
            "verificationCode": "123456",
        },
        headers=headers,
    )
    assert registered.status_code == 200
    assert registered.json()["user"]["accountStatus"] == "provisioned"
    assert registered.json()["user"]["entitlementStatus"] == "inactive"
    assert upstream.created[0]["user_id"].startswith("local-")

    logged_in = client.post(
        "/api/auth/login",
        json={"email": "person@example.com", "password": "password-123"},
        headers=headers,
    )
    assert logged_in.status_code == 200
    assert logged_in.json()["user"]["entitlementStatus"] == "inactive"
    cookie_value = client.cookies.get(main.SESSION_COOKIE_NAME)
    assert cookie_value and "person@example.com" not in cookie_value

    upstream.user_models = ["gpt-5"]
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["entitlementStatus"] == "active"

    logout = client.post(
        "/api/auth/logout",
        json={},
        headers={"X-CSRF-Token": me.json()["csrfToken"]},
    )
    assert logout.status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_empty_upstream_model_list_does_not_activate_local_signup(tmp_path, monkeypatch) -> None:
    client, store, upstream = auth_client(tmp_path, monkeypatch)
    user = store.create_user("person@example.com", "Person", hash_auth_token("password"), email_verified=True)
    store.set_provisioning_status(user["id"], "provisioned", "primary", f"local-{user['id']}")
    upstream.user_models = []

    payload = asyncio.run(main.auth_user_payload(user, refresh_entitlement=True))

    assert payload["accountStatus"] == "provisioned"
    assert payload["entitlementStatus"] == "inactive"


def test_customer_membership_activates_an_email_registered_account(tmp_path, monkeypatch) -> None:
    """成员绑定的是邮箱注册账号时，权限和资料都得跟着成员关系走。

    这类账号有一个注册时建立的个人上游映射，但那个映射从来没有被授予模型，所以
    只看它会一直判成"等待开通"，哪怕这个人在客户企业下早已有消费。名字同理：注册
    时自己填的那个名字不是企业花名册上的姓名。
    """

    _client, store, upstream = auth_client(tmp_path, monkeypatch)
    user = store.create_user("person@example.com", "Yida Zhu", hash_auth_token("password"), email_verified=True)
    store.set_provisioning_status(user["id"], "provisioned", "primary", f"local-{user['id']}")
    upstream.user_models = []
    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(main, "require_real_organization_capability", lambda: None)

    class MembershipStore:
        async def resolve_members_by_auth_user_id(self, auth_user_id):
            assert auth_user_id == str(user["id"])
            return [
                {
                    "organizationId": "org-baic",
                    "organization": {"id": "org-baic", "status": "active"},
                    "member": {
                        "id": "member-1",
                        "name": "梁海强",
                        "status": "active",
                        "upstreamUserId": "",
                        "principalIds": ["principal-lianghaiqiang"],
                    },
                }
            ]

    monkeypatch.setattr(main, "organization_store", lambda: MembershipStore())

    payload = asyncio.run(main.auth_user_payload(user, refresh_entitlement=True))

    assert payload["entitlementStatus"] == "active"
    assert payload["name"] == "梁海强"
    assert payload["avatar"] == "梁"


def test_signup_name_survives_when_no_membership_owns_the_account(tmp_path, monkeypatch) -> None:
    """没有成员关系时仍然用注册时的名字，别把个人账号也改掉。"""

    _client, store, upstream = auth_client(tmp_path, monkeypatch)
    user = store.create_user("person@example.com", "Yida Zhu", hash_auth_token("password"), email_verified=True)
    store.set_provisioning_status(user["id"], "provisioned", "primary", f"local-{user['id']}")
    upstream.user_models = ["gpt-5"]
    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(main, "require_real_organization_capability", lambda: None)

    class EmptyMembershipStore:
        async def resolve_members_by_auth_user_id(self, _auth_user_id):
            return []

    monkeypatch.setattr(main, "organization_store", lambda: EmptyMembershipStore())

    payload = asyncio.run(main.auth_user_payload(user, refresh_entitlement=True))

    assert payload["entitlementStatus"] == "active"
    assert payload["name"] == "Yida Zhu"


def test_password_identity_never_inherits_platform_admin_flags(tmp_path, monkeypatch) -> None:
    _client, store, _upstream = auth_client(tmp_path, monkeypatch)
    monkeypatch.setenv("ADMIN_EMAILS", "seller-admin@example.com")
    user = store.create_user(
        "seller-admin@example.com",
        "Seller Admin Password Account",
        hash_auth_token("password"),
        email_verified=True,
    )

    payload = asyncio.run(main.auth_user_payload(user))

    assert payload["isAdmin"] is False
    assert payload["isPlatformAdmin"] is False


def test_inactive_local_user_cannot_query_or_manage_keys_or_models(tmp_path, monkeypatch) -> None:
    client, store, upstream = auth_client(tmp_path, monkeypatch)
    user = store.create_user("person@example.com", "Person", hash_password("password-123"), email_verified=True)
    store.set_provisioning_status(user["id"], "provisioned", "primary", f"local-{user['id']}")
    logged_in = client.post(
        "/api/auth/login",
        json={"email": "person@example.com", "password": "password-123"},
        headers=csrf(client),
    )
    assert logged_in.status_code == 200
    headers = {"X-CSRF-Token": logged_in.json()["csrfToken"]}
    requests = [
        ("get", "/api/me/keys", {}),
        ("post", "/api/me/keys", {"json": {"name": "test key", "purpose": "", "duration": "never", "models": []}}),
        ("post", "/api/me/keys/key-1/regenerate", {}),
        ("post", "/api/me/keys/key-1/disable-old", {"json": {"replacementKeyId": "key-2"}}),
        ("delete", "/api/me/keys/key-1", {}),
        ("post", "/api/me/keys/key-1/reveal", {}),
        ("get", "/api/models", {}),
        ("get", "/api/me/usage", {}),
        ("get", "/api/me/usage/logs", {}),
    ]

    for method, path, kwargs in requests:
        response = getattr(client, method)(path, headers=headers if method != "get" else None, **kwargs)
        assert response.status_code == 403, path
        assert response.json()["detail"]["code"] == "AUTH_ENTITLEMENT_INACTIVE", path

    assert upstream.key_or_model_calls == []


def test_local_password_account_never_inherits_same_email_sso_team_or_debug_scope(tmp_path, monkeypatch) -> None:
    client, store, upstream = auth_client(tmp_path, monkeypatch)
    user = store.create_user("leader@example.com", "Leader", hash_password("password-123"), email_verified=True)
    store.set_provisioning_status(user["id"], "provisioned", "primary", f"local-{user['id']}")
    upstream.user_models = ["gpt-5"]
    logged_in = client.post(
        "/api/auth/login",
        json={"email": "leader@example.com", "password": "password-123"},
        headers=csrf(client),
    )
    assert logged_in.status_code == 200

    mapping_attempts = 0

    async def must_not_resolve_same_email(*_args, **_kwargs):
        nonlocal mapping_attempts
        mapping_attempts += 1
        raise AssertionError("local password account must not resolve the matching SSO identity")

    monkeypatch.setattr(main, "cached_resolve_user", must_not_resolve_same_email)
    monkeypatch.setenv("DEBUG_MAPPING_ENABLED", "true")

    scope = client.get("/api/auth/scope")
    team_usage = client.get("/api/team/usage")
    mapping = client.get("/api/debug/me-mapping")
    usage_debug = client.get("/api/debug/me-usage-compare")

    assert scope.status_code == 200
    assert scope.json() == {
        "isTeamLeader": False,
        "teamBoardStatus": "none",
        "team": None,
        "leaderTeams": [],
        # 侧边栏靠这一个响应决定整栏可见性；本地账号在未配置充值时拿不到入口。
        "billingAvailable": False,
    }
    for response in (team_usage,):
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "AUTH_TEAM_SCOPE_UNAVAILABLE"
    for response in (mapping, usage_debug):
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "AUTH_LOCAL_DEBUG_UNAVAILABLE"
    assert mapping_attempts == 0


def test_active_local_user_usage_uses_its_local_mapping_not_same_email_sso_identity(tmp_path, monkeypatch) -> None:
    client, store, upstream = auth_client(tmp_path, monkeypatch)
    user = store.create_user("person@example.com", "Person", hash_password("password-123"), email_verified=True)
    store.set_provisioning_status(user["id"], "provisioned", "primary", f"local-{user['id']}")
    upstream.user_models = ["gpt-5"]
    logged_in = client.post(
        "/api/auth/login",
        json={"email": "person@example.com", "password": "password-123"},
        headers=csrf(client),
    )
    assert logged_in.status_code == 200

    async def must_not_resolve_same_email(*_args, **_kwargs):
        raise AssertionError("active local account must use its provisioned local mapping")

    monkeypatch.setattr(main, "cached_resolve_user", must_not_resolve_same_email)
    monkeypatch.setattr(main, "_usage_store", None)

    usage = client.get("/api/me/usage")
    logs = client.get("/api/me/usage/logs")

    assert usage.status_code == logs.status_code == 200
    assert upstream.key_or_model_calls == ["usage_rows_for_user_ids"]
    assert logs.json()["cache"]["hit"] is True


def test_registration_domain_allowlist_restricts_public_signup(tmp_path, monkeypatch) -> None:
    client, store, _ = auth_client(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", "example.com")
    headers = csrf(client)

    blocked = client.post(
        "/api/auth/verification/request",
        json={"email": "person@other.test", "purpose": "signup"},
        headers=headers,
    )
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "AUTH_EMAIL_DOMAIN_NOT_ALLOWED"

    allowed = client.post(
        "/api/auth/verification/request",
        json={"email": "person@example.com", "purpose": "signup"},
        headers=headers,
    )
    assert allowed.status_code == 200
    assert store.get_user_by_email("person@example.com") is None


def test_public_signup_stays_closed_until_production_dependencies_are_ready(tmp_path, monkeypatch) -> None:
    client, _, _ = auth_client(tmp_path, monkeypatch)
    monkeypatch.setenv("APP_BASE_URL", "https://dashboard.example.com")
    monkeypatch.setenv("SESSION_SECRET", "x" * 64)
    monkeypatch.setenv("AUTH_DATABASE_PATH", str(tmp_path / "auth.sqlite3"))
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.delenv("AUTH_EMAIL_DEBUG", raising=False)

    config = client.get("/api/auth/config")
    blocked = client.post(
        "/api/auth/register",
        json={"email": "person@example.com", "name": "Person", "password": "password-123", "verificationCode": "123456"},
        headers=csrf(client),
    )

    assert config.json()["publicSignupEnabled"] is True
    assert config.json()["passwordLoginAvailable"] is True
    assert config.json()["publicSignupConfigured"] is False
    assert config.json()["publicSignupUnavailableCode"] == "AUTH_SIGNUP_EMAIL_NOT_CONFIGURED"
    assert config.json()["passwordRecoveryEnabled"] is True
    assert config.json()["passwordRecoveryAvailable"] is False
    assert config.json()["passwordRecoveryUnavailableCode"] == "AUTH_PASSWORD_EMAIL_NOT_CONFIGURED"
    assert blocked.status_code == 503
    assert blocked.json()["detail"]["code"] == "AUTH_SIGNUP_EMAIL_NOT_CONFIGURED"


def test_forgot_password_missing_smtp_is_equally_unavailable_for_existing_and_missing_email(tmp_path, monkeypatch) -> None:
    client, store, _ = auth_client(tmp_path, monkeypatch)
    store.create_user("person@example.com", "Person", "pbkdf2_sha256$1$bad$bad", email_verified=True)
    monkeypatch.delenv("AUTH_EMAIL_DEBUG", raising=False)
    monkeypatch.delenv("DEV_LOGIN_ENABLED", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    headers = csrf(client)

    existing = client.post(
        "/api/auth/password/forgot",
        json={"email": "person@example.com"},
        headers=headers,
    )
    missing = client.post(
        "/api/auth/password/forgot",
        json={"email": "missing@example.com"},
        headers=headers,
    )

    assert existing.status_code == missing.status_code == 503
    assert existing.json() == missing.json()
    assert existing.json()["detail"]["code"] == "AUTH_PASSWORD_EMAIL_NOT_CONFIGURED"


def test_forgot_password_invalid_email_keeps_generic_response(tmp_path, monkeypatch) -> None:
    client, _, _ = auth_client(tmp_path, monkeypatch)
    headers = csrf(client)

    invalid = client.post(
        "/api/auth/password/forgot",
        json={"email": "person @example.com"},
        headers=headers,
    )
    missing = client.post(
        "/api/auth/password/forgot",
        json={"email": "missing@example.com"},
        headers=headers,
    )

    assert invalid.status_code == missing.status_code == 200
    assert invalid.json() == missing.json()


def test_dev_login_is_rejected_on_https_even_without_startup_validation(tmp_path, monkeypatch) -> None:
    client, _, _ = auth_client(tmp_path, monkeypatch)
    monkeypatch.setenv("DEV_LOGIN_ENABLED", "true")
    monkeypatch.setenv("APP_BASE_URL", "https://dashboard.example.com")

    response = client.post(
        "/api/auth/dev-login",
        json={"email": "admin@example.com"},
        headers=csrf(client),
    )

    assert response.status_code == 403


def test_dev_login_requires_csrf_in_local_development(tmp_path, monkeypatch) -> None:
    client, _, _ = auth_client(tmp_path, monkeypatch)
    monkeypatch.setenv("DEV_LOGIN_ENABLED", "true")
    monkeypatch.setenv("APP_BASE_URL", "http://127.0.0.1:8000")

    response = client.post("/api/auth/dev-login", json={"email": "person@example.com"})

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "AUTH_CSRF_INVALID"


def test_managed_enterprise_account_cannot_use_personal_upstream_key_scope(
    monkeypatch,
) -> None:
    request = type(
        "RequestStub",
        (),
        {
            "session": {
                "user": {
                    "id": "managed-user-1",
                    "authType": "password",
                    "accountType": "enterprise_managed",
                    "email": None,
                    "name": "梁海强",
                }
            }
        },
    )()

    async def false_demo(_user):
        return False

    async def no_inactive_membership(_user):
        return None

    async def auth_call(method, *_args, **_kwargs):
        if method == "get_user":
            return {
                "id": "managed-user-1",
                "email": None,
                "login_name": "lianghaiqiang",
                "name": "梁海强",
                "status": "active",
                "account_type": "enterprise_managed",
                "identity_status": "verified",
            }
        if method == "get_upstream_account":
            raise AssertionError("managed account must not use personal upstream mapping")
        return None

    async def user_payload(user, **_kwargs):
        return user

    monkeypatch.setattr(main, "is_demo_customer_user", false_demo)
    monkeypatch.setattr(
        main, "require_non_inactive_demo_identity", no_inactive_membership
    )
    monkeypatch.setattr(main, "auth_store_call", auth_call)
    monkeypatch.setattr(main, "auth_user_payload", user_payload)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main.current_upstream_user(request))

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "ORGANIZATION_UPSTREAM_FORBIDDEN"


def test_managed_enterprise_account_without_active_membership_has_no_personal_usage(
    monkeypatch,
) -> None:
    app_user = {
        "id": "managed-user-1",
        "email": None,
        "loginName": "lianghaiqiang",
        "accountType": "enterprise_managed",
        "accountStatus": "provisioned",
        "entitlementStatus": "active",
        "organizationAccessStatus": "provisioning",
    }

    async def no_memberships(_user):
        return []

    async def personal_scope_must_not_run(*_args, **_kwargs):
        raise AssertionError("managed account reached personal usage scope")

    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(main, "organization_memberships_for_user", no_memberships)
    monkeypatch.setattr(
        main, "local_personal_usage_payload", personal_scope_must_not_run
    )

    payload = asyncio.run(
        main.personal_usage_payload(
            app_user, "2026-08-01", "2026-08-03", "all"
        )
    )

    assert payload["rows"] == []
    assert payload["summary"]["rangeTotal"]["spend"] == 0
    assert payload["organizationAccessStatus"] == "provisioning"


def test_forgot_password_delivery_failure_keeps_previous_reset_token(tmp_path, monkeypatch) -> None:
    client, store, _ = auth_client(tmp_path, monkeypatch)
    user = store.create_user("person@example.com", "Person", "old-hash", email_verified=True)
    previous_token = "previous-reset-token-that-is-long-enough"
    store.create_password_reset_token(user["id"], hash_auth_token(previous_token), datetime.now(timezone.utc) + timedelta(minutes=5))

    async def fail_email(_recipient, _subject, _body):
        raise main.auth_http_error(503, "邮件暂时无法发送，请稍后重试", "AUTH_EMAIL_UNAVAILABLE")

    monkeypatch.setattr(main, "send_auth_email", fail_email)
    response = client.post(
        "/api/auth/password/forgot",
        json={"email": "person@example.com"},
        headers=csrf(client),
    )

    assert response.status_code == 200
    assert store.consume_password_reset_token(previous_token) is not None


def test_turnstile_enabled_without_keys_degrades_health_and_blocks_auth(tmp_path, monkeypatch) -> None:
    client, _, _ = auth_client(tmp_path, monkeypatch)
    monkeypatch.setenv("APP_BASE_URL", "https://dashboard.example.com")
    monkeypatch.setenv("SESSION_SECRET", "x" * 64)
    monkeypatch.setenv("AUTH_DATABASE_PATH", str(tmp_path / "auth.sqlite3"))
    monkeypatch.delenv("AUTH_EMAIL_DEBUG", raising=False)
    monkeypatch.setenv("TURNSTILE_ENABLED", "true")
    monkeypatch.delenv("TURNSTILE_SITE_KEY", raising=False)
    monkeypatch.delenv("TURNSTILE_SECRET_KEY", raising=False)

    health = client.get("/api/health")
    assert health.json()["status"] == "degraded"
    assert health.json()["turnstile"]["configured"] is False

    blocked = client.post(
        "/api/auth/verification/request",
        json={"email": "person@example.com", "purpose": "signup"},
        headers=csrf(client),
    )
    assert blocked.status_code == 503
    assert blocked.json()["detail"]["code"] == "AUTH_TURNSTILE_NOT_CONFIGURED"


def test_health_exposes_closed_signup_readiness_without_advertising_a_broken_form(tmp_path, monkeypatch) -> None:
    client, _, _ = auth_client(tmp_path, monkeypatch)
    monkeypatch.setenv("APP_BASE_URL", "https://dashboard.example.com")
    monkeypatch.setenv("SESSION_SECRET", "x" * 64)
    monkeypatch.setenv("AUTH_DATABASE_PATH", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv("PUBLIC_SIGNUP_ENABLED", "true")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)

    health = client.get("/api/health")

    assert health.status_code == 200
    assert health.json()["status"] == "degraded"
    assert health.json()["authReadiness"]["passwordLogin"] == {
        "enabled": True,
        "available": True,
        "unavailableCode": "",
    }
    assert health.json()["authReadiness"]["publicSignup"] == {
        "enabled": True,
        "available": False,
        "unavailableCode": "AUTH_SIGNUP_EMAIL_NOT_CONFIGURED",
    }


def test_verification_delivery_failure_preserves_previous_code(tmp_path, monkeypatch) -> None:
    client, store, _ = auth_client(tmp_path, monkeypatch)
    put_signup_code(store, "person@example.com", "111111")
    async def fail_email(_recipient, _subject, _body):
        raise main.auth_http_error(503, "邮件暂时无法发送，请稍后重试", "AUTH_EMAIL_UNAVAILABLE")
    monkeypatch.setattr(main, "send_auth_email", fail_email)

    failed = client.post(
        "/api/auth/verification/request",
        json={"email": "person@example.com", "purpose": "signup"},
        headers=csrf(client),
    )

    assert failed.status_code == 503
    assert store.consume_verification_code("person@example.com", "signup", "111111") is True


def test_smtp_credentials_require_tls(monkeypatch) -> None:
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "noreply@example.com")
    monkeypatch.setenv("SMTP_USERNAME", "user")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("SMTP_SSL", "false")
    monkeypatch.setenv("SMTP_STARTTLS", "false")

    try:
        main.send_auth_email_sync("person@example.com", "Subject", "Body")
    except RuntimeError as exc:
        assert "TLS" in str(exc)
    else:
        raise AssertionError("SMTP credentials must not be sent without TLS")


def test_production_signup_requires_email_verification(tmp_path, monkeypatch) -> None:
    client, _, _ = auth_client(tmp_path, monkeypatch)
    monkeypatch.setenv("EMAIL_VERIFICATION_REQUIRED", "false")

    response = client.post(
        "/api/auth/register",
        json={
            "email": "person@example.com",
            "name": "Person",
            "password": "password-123",
        },
        headers=csrf(client),
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "AUTH_SIGNUP_EMAIL_VERIFICATION_REQUIRED"


def test_password_reset_routes_are_disabled_with_password_login(tmp_path, monkeypatch) -> None:
    client, store, _ = auth_client(tmp_path, monkeypatch)
    monkeypatch.setenv("PASSWORD_LOGIN_ENABLED", "false")
    user = store.create_user("person@example.com", "Person", "old-hash", email_verified=True)
    token = "reset-token-that-is-long-enough"
    store.create_password_reset_token(user["id"], hash_auth_token(token), datetime.now(timezone.utc) + timedelta(minutes=5))
    headers = csrf(client)

    forgot = client.post("/api/auth/password/forgot", json={"email": "person@example.com"}, headers=headers)
    reset = client.post("/api/auth/password/reset", json={"token": token, "newPassword": "new-password-456"}, headers=headers)

    assert forgot.status_code == reset.status_code == 403
    assert forgot.json()["detail"]["code"] == reset.json()["detail"]["code"] == "AUTH_PASSWORD_LOGIN_DISABLED"


def test_csrf_origin_must_match_configured_scheme_and_host(tmp_path, monkeypatch) -> None:
    client, _, _ = auth_client(tmp_path, monkeypatch)
    monkeypatch.setenv("APP_BASE_URL", "https://dashboard.example.com")
    headers = {
        **csrf(client),
        "Origin": "http://dashboard.example.com",
    }

    response = client.post("/api/auth/logout", json={}, headers=headers)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "AUTH_ORIGIN_INVALID"


def test_provisioning_conflict_does_not_bind_mismatched_upstream_account(tmp_path, monkeypatch) -> None:
    client, _store, upstream = auth_client(tmp_path, monkeypatch)
    upstream.created = []
    async def conflict_info(_user_id):
        return {
            "user_id": "local-conflict",
            "user_email": "other@example.com",
            "metadata": {"created_via": "other-system", "local_user_id": "local-conflict"},
        }
    async def fail_create(*_args, **_kwargs):
        raise main.HTTPException(status_code=409, detail="duplicate")
    monkeypatch.setattr(upstream, "create_internal_user", fail_create)
    monkeypatch.setattr(upstream, "user_info", conflict_info)
    user = _store.create_user("person@example.com", "Person", hash_auth_token("password"), email_verified=True)

    account = asyncio.run(main.provision_local_user(user))

    assert account["status"] == "provisioning_failed"


def test_provisioning_unexpected_failure_is_queued(tmp_path, monkeypatch) -> None:
    _client, store, upstream = auth_client(tmp_path, monkeypatch)
    async def fail_create(*_args, **_kwargs):
        raise RuntimeError("temporary failure")
    monkeypatch.setattr(upstream, "create_internal_user", fail_create)
    user = store.create_user("person@example.com", "Person", hash_auth_token("password"), email_verified=True)

    account = asyncio.run(main.provision_local_user(user))

    assert account["status"] == "provisioning_failed"
    assert len(store.pending_provisioning()) == 1


def test_registration_consumes_valid_code_only_with_atomic_user_creation(tmp_path, monkeypatch) -> None:
    client, store, _ = auth_client(tmp_path, monkeypatch)
    put_signup_code(store, "person@example.com")
    original_create_user = store.create_user_from_verification

    def fail_once(*args, **kwargs):
        monkeypatch.setattr(store, "create_user_from_verification", original_create_user)
        raise ValueError("临时写入失败")

    monkeypatch.setattr(store, "create_user_from_verification", fail_once)
    headers = csrf(client)
    failed = client.post(
        "/api/auth/register",
        json={"email": "person@example.com", "name": "Person", "password": "password-123", "verificationCode": "123456"},
        headers=headers,
    )
    retried = client.post(
        "/api/auth/register",
        json={"email": "person@example.com", "name": "Person", "password": "password-123", "verificationCode": "123456"},
        headers=headers,
    )

    assert failed.status_code == 400
    assert retried.status_code == 200


def test_reset_hash_failure_does_not_consume_token(tmp_path, monkeypatch) -> None:
    client, store, _ = auth_client(tmp_path, monkeypatch)
    user = store.create_user("person@example.com", "Person", "old-hash", email_verified=True)
    raw_token = "reset-token-that-is-long-enough"
    store.create_password_reset_token(user["id"], hash_auth_token(raw_token), datetime.now(timezone.utc) + timedelta(minutes=5))

    def fail_hash(_password: str) -> str:
        raise ValueError("密码不可用")

    monkeypatch.setattr(main, "hash_password", fail_hash)
    failed = client.post(
        "/api/auth/password/reset",
        json={"token": raw_token, "newPassword": "new-password-456"},
        headers=csrf(client),
    )

    assert failed.status_code == 400
    assert store.consume_password_reset_token(hash_auth_token(raw_token)) is not None


def test_request_ip_only_trusts_forwarded_header_from_configured_proxy(monkeypatch) -> None:
    class RequestStub:
        def __init__(self, peer: str, forwarded: str) -> None:
            self.client = type("Client", (), {"host": peer})()
            self.headers = {"x-forwarded-for": forwarded}

    monkeypatch.delenv("AUTH_TRUSTED_PROXY_IPS", raising=False)
    assert main.request_ip(RequestStub("203.0.113.10", "198.51.100.7")) == "203.0.113.10"

    monkeypatch.setenv("AUTH_TRUSTED_PROXY_IPS", "10.0.0.0/8")
    assert main.request_ip(RequestStub("10.1.2.3", "198.51.100.7, 10.2.3.4")) == "198.51.100.7"


def test_key_mutation_requires_csrf_before_handler_work(monkeypatch) -> None:
    async def must_not_run(_request):
        raise AssertionError("handler reached without CSRF")

    monkeypatch.setattr(main, "current_upstream_user", must_not_run)
    with TestClient(main.app) as client:
        response = client.post(
            "/api/me/keys",
            json={"name": "test key", "purpose": "", "duration": "never", "models": []},
        )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "AUTH_CSRF_INVALID"
