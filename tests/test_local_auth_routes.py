import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from backend import main
from backend.auth import hash_auth_token
from backend.auth_store import AuthStore


class FakeProvisioningClient:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.user_models: list[str] = ["no-default-models"]

    async def create_internal_user(self, user_id, email, name=None):
        self.created.append({"user_id": user_id, "email": email, "name": name})
        return {"user_id": user_id, "user_email": email}

    async def user_info(self, user_id):
        return {"user_id": user_id, "models": self.user_models, "max_budget": None}


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


def test_https_runtime_rejects_incomplete_public_signup(monkeypatch) -> None:
    monkeypatch.setenv("APP_BASE_URL", "https://dashboard.example.com")
    monkeypatch.setenv("SESSION_SECRET", "x" * 64)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("PASSWORD_LOGIN_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_SIGNUP_ENABLED", "true")
    monkeypatch.setenv("EMAIL_VERIFICATION_REQUIRED", "true")
    monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", "example.com")
    monkeypatch.setenv("TURNSTILE_ENABLED", "true")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "site-key")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "secret-key")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("AUTH_EMAIL_DEBUG", raising=False)
    monkeypatch.delenv("DEV_LOGIN_ENABLED", raising=False)

    try:
        main.validate_runtime_auth_config()
    except RuntimeError as exc:
        assert "公开注册" in str(exc)
    else:
        raise AssertionError("HTTPS deployment must reject incomplete public signup configuration")


def test_https_runtime_rejects_password_login_without_turnstile(monkeypatch) -> None:
    monkeypatch.setenv("APP_BASE_URL", "https://dashboard.example.com")
    monkeypatch.setenv("SESSION_SECRET", "x" * 64)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("PASSWORD_LOGIN_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_SIGNUP_ENABLED", "false")
    monkeypatch.setenv("TURNSTILE_ENABLED", "false")
    monkeypatch.delenv("AUTH_EMAIL_DEBUG", raising=False)
    monkeypatch.delenv("DEV_LOGIN_ENABLED", raising=False)

    try:
        main.validate_runtime_auth_config()
    except RuntimeError as exc:
        assert "Turnstile" in str(exc)
    else:
        raise AssertionError("HTTPS password login must require Turnstile")


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
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("AUTH_EMAIL_DEBUG", raising=False)

    config = client.get("/api/auth/config")
    blocked = client.post(
        "/api/auth/register",
        json={"email": "person@example.com", "name": "Person", "password": "password-123", "verificationCode": "123456"},
        headers=csrf(client),
    )

    assert config.json()["publicSignupEnabled"] is False
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "AUTH_SIGNUP_DISABLED"


def test_forgot_password_does_not_reveal_email_delivery_failure(tmp_path, monkeypatch) -> None:
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

    assert existing.status_code == missing.status_code == 200
    assert existing.json() == missing.json()


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
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "AUTH_SIGNUP_DISABLED"


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


def test_registration_does_not_require_code_when_verification_is_disabled(tmp_path, monkeypatch) -> None:
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

    assert response.status_code == 200
    assert response.json()["user"]["email"] == "person@example.com"


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


def test_registration_keeps_valid_code_when_user_creation_fails(tmp_path, monkeypatch) -> None:
    client, store, _ = auth_client(tmp_path, monkeypatch)
    put_signup_code(store, "person@example.com")
    original_create_user = store.create_user

    def fail_once(*args, **kwargs):
        monkeypatch.setattr(store, "create_user", original_create_user)
        raise ValueError("临时写入失败")

    monkeypatch.setattr(store, "create_user", fail_once)
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
