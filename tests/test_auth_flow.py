import base64
import json
import os
from typing import Any

from authlib.integrations.base_client import OAuthError
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from backend import main
from backend.auth import SESSION_USER_KEY


def signed_session(payload: dict[str, Any]) -> str:
    data = base64.b64encode(json.dumps(payload).encode("utf-8"))
    return TimestampSigner(os.getenv("SESSION_SECRET", "dev-session-secret-change-me")).sign(data).decode("utf-8")


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
