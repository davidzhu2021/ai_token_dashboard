import base64
import json
import os
from typing import Any

from authlib.integrations.base_client import OAuthError
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from backend import main
from backend.auth import CSRF_SESSION_KEY, SESSION_USER_KEY


def signed_session(payload: dict[str, Any]) -> str:
    data = base64.b64encode(json.dumps(payload).encode("utf-8"))
    return TimestampSigner(os.getenv("SESSION_SECRET", "dev-session-secret-change-me")).sign(data).decode("utf-8")


def decoded_session(cookie: str) -> dict[str, Any]:
    signed = TimestampSigner(os.getenv("SESSION_SECRET", "dev-session-secret-change-me")).unsign(cookie.encode("utf-8"))
    return json.loads(base64.b64decode(signed))


def sso_client(monkeypatch) -> TestClient:
    class SuccessfulCompanyOAuth:
        async def authorize_access_token(self, request):
            return {
                "userinfo": {
                    "email": "employee@auto-link.com.cn",
                    "displayName": "Employee",
                    "department": "Engineering",
                }
            }

    class FakeOAuth:
        company = SuccessfulCompanyOAuth()

    monkeypatch.setattr(main, "oauth", FakeOAuth())
    monkeypatch.setattr(main, "oidc_configured", lambda: True)
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAIN", "auto-link.com.cn")
    return TestClient(main.app)


def test_sso_callback_persists_identity_and_csrf_in_same_cookie(monkeypatch) -> None:
    client = sso_client(monkeypatch)

    response = client.get("/api/auth/callback?code=test-code&state=test-state", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/?auth_callback=success"
    cookie = response.cookies.get(main.SESSION_COOKIE_NAME)
    assert cookie
    session = decoded_session(cookie)
    assert session[SESSION_USER_KEY]["email"] == "employee@auto-link.com.cn"
    assert session[CSRF_SESSION_KEY]


def test_logout_immediately_after_sso_callback_succeeds(monkeypatch) -> None:
    client = sso_client(monkeypatch)
    callback = client.get("/api/auth/callback?code=test-code&state=test-state", follow_redirects=False)
    session = decoded_session(callback.cookies.get(main.SESSION_COOKIE_NAME))

    logout = client.post(
        "/api/auth/logout",
        json={},
        headers={"X-CSRF-Token": session[CSRF_SESSION_KEY]},
    )

    assert logout.status_code == 200
    assert logout.json() == {"ok": True}
    assert client.get("/api/auth/me").status_code == 401


def test_logout_with_stale_csrf_preserves_sso_session(monkeypatch) -> None:
    client = sso_client(monkeypatch)
    callback = client.get("/api/auth/callback?code=test-code&state=test-state", follow_redirects=False)

    logout = client.post(
        "/api/auth/logout",
        json={},
        headers={"X-CSRF-Token": "stale-token"},
    )

    assert logout.status_code == 403
    assert logout.json()["detail"]["code"] == "AUTH_CSRF_INVALID"
    assert client.get("/api/auth/me").json()["email"] == "employee@auto-link.com.cn"


def test_oidc_state_mismatch_does_not_clear_existing_login(monkeypatch) -> None:
    class FailingCompanyOAuth:
        async def authorize_access_token(self, request):
            raise OAuthError("mismatching_state", "CSRF Warning! State not equal in request and response.")

    class FakeOAuth:
        company = FailingCompanyOAuth()

    monkeypatch.setattr(main, "oauth", FakeOAuth())
    monkeypatch.setattr(main, "oidc_configured", lambda: True)

    client = TestClient(main.app)
    session = {
        SESSION_USER_KEY: {
            "email": "leader@auto-link.com.cn",
            "name": "Leader",
            "avatar": "L",
            "department": "Engineering",
            "isAdmin": False,
        }
    }
    client.cookies.set(main.SESSION_COOKIE_NAME, signed_session(session))

    response = client.get("/api/auth/callback?code=test-code&state=wrong-state")

    assert response.status_code == 400
    session_cookies = [cookie.value for cookie in client.cookies.jar if cookie.name == main.SESSION_COOKIE_NAME]
    assert session_cookies
    assert "null" not in session_cookies
