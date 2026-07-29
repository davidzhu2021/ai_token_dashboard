"""Route-level security tests for the in-memory enterprise organization demo."""

import base64
import json
import os
from typing import Any

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from backend import main
from backend.auth import CSRF_SESSION_KEY, SESSION_USER_KEY
from backend.organization_store import InMemoryOrganizationStore


def signed_session(payload: dict[str, Any]) -> str:
    data = base64.b64encode(json.dumps(payload).encode("utf-8"))
    return TimestampSigner(os.getenv("SESSION_SECRET", "dev-session-secret-change-me")).sign(data).decode("utf-8")


def demo_client(monkeypatch, email: str, *, is_admin: bool = False) -> TestClient:
    monkeypatch.setenv("ORGANIZATION_DEMO_ENABLED", "true")
    monkeypatch.setenv("ADMIN_EMAILS", email if is_admin else "platform-admin@example.test")
    monkeypatch.setattr(main, "_organization_store", InMemoryOrganizationStore())
    client = TestClient(main.app)
    client.cookies.set(
        main.SESSION_COOKIE_NAME,
        signed_session(
            {
                SESSION_USER_KEY: {
                    "email": email,
                    "name": "Organization Test User",
                    "avatar": "O",
                    "department": "Engineering",
                    "isAdmin": is_admin,
                },
                CSRF_SESSION_KEY: "organization-routes-csrf",
            }
        ),
    )
    return client


def csrf_headers() -> dict[str, str]:
    return {"X-CSRF-Token": "organization-routes-csrf"}


def test_unmatched_non_admin_cannot_access_another_demo_organization(monkeypatch) -> None:
    client = demo_client(monkeypatch, "outside@demo.example")

    current = client.get("/api/organization/current")
    members = client.get("/api/organization/current/members")
    write = client.post(
        "/api/organization/current/departments", json={"name": "Outside"}, headers=csrf_headers()
    )

    for response in (current, members, write):
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "ORGANIZATION_MEMBERSHIP_REQUIRED"


def test_invited_and_suspended_members_cannot_access_demo_data(monkeypatch) -> None:
    for email in ("flynn.gao@demo.example", "indigo.xu@demo.example"):
        client = demo_client(monkeypatch, email)

        me = client.get("/api/auth/me")
        response = client.get("/api/organization/current")

        assert me.json()["organizationRole"] is None
        assert me.json()["canManageOrganization"] is False
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "ORGANIZATION_MEMBER_INACTIVE"


def test_inactive_seed_membership_wins_over_platform_admin_demo_fallback(monkeypatch) -> None:
    client = demo_client(monkeypatch, "jules.qian@demo.example", is_admin=True)

    me = client.get("/api/auth/me")
    response = client.get("/api/organization/current")

    assert me.json()["organizationRole"] is None
    assert me.json()["canManageOrganization"] is False
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "ORGANIZATION_MEMBER_INACTIVE"


def test_admin_member_can_mutate_and_member_cannot_even_with_valid_csrf(monkeypatch) -> None:
    admin_client = demo_client(monkeypatch, "admin@demo.example")
    created = admin_client.post(
        "/api/organization/current/departments", json={"name": "Partner Team"}, headers=csrf_headers()
    )
    assert created.status_code == 200

    member_client = demo_client(monkeypatch, "avery.chen@demo.example")
    denied = member_client.patch(
        "/api/organization/current/departments/dept-product",
        json={"name": "Changed By Member"},
        headers=csrf_headers(),
    )

    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "ORGANIZATION_MANAGE_FORBIDDEN"


def test_routes_do_not_send_mail_or_call_an_upstream_client(monkeypatch) -> None:
    client = demo_client(monkeypatch, "owner@demo.example")

    async def unexpected_email(*_args, **_kwargs) -> None:
        raise AssertionError("organization demo must not send invitation email")

    def unexpected_client() -> Any:
        raise AssertionError("organization demo must not call an upstream client")

    monkeypatch.setattr(main, "send_auth_email", unexpected_email)
    monkeypatch.setattr(main, "client", unexpected_client)

    response = client.post(
        "/api/organization/current/members",
        json={
            "name": "No Side Effects",
            "email": "no.side.effects@demo.example",
            "departmentId": "dept-product",
            "role": "member",
        },
        headers=csrf_headers(),
    )

    assert response.status_code == 200
    assert response.json()["member"]["status"] == "invited"


def test_password_identity_cannot_inherit_a_matching_demo_member(monkeypatch) -> None:
    client = demo_client(monkeypatch, "admin@demo.example")
    session = {
        SESSION_USER_KEY: {
            "id": "local-admin",
            "email": "admin@demo.example",
            "name": "Local Password Identity",
            "avatar": "L",
            "department": "Engineering",
            "isAdmin": False,
            "authType": "password",
        },
        CSRF_SESSION_KEY: "organization-routes-csrf",
    }
    client.cookies.set(main.SESSION_COOKIE_NAME, signed_session(session))

    response = client.get("/api/organization/current")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "ORGANIZATION_SSO_REQUIRED"


def test_member_update_rejects_unknown_ids_and_extra_fields(monkeypatch) -> None:
    client = demo_client(monkeypatch, "owner@demo.example")

    missing = client.patch(
        "/api/organization/current/members/member-from-other-organization",
        json={"status": "suspended"},
        headers=csrf_headers(),
    )
    extra = client.patch(
        "/api/organization/current/members/member-001",
        json={"status": "suspended", "organizationId": "org-other"},
        headers=csrf_headers(),
    )

    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "ORGANIZATION_NOT_FOUND"
    assert extra.status_code == 422


def test_bodyless_organization_mutations_reject_client_organization_ids(monkeypatch) -> None:
    client = demo_client(monkeypatch, "owner@demo.example")

    archive = client.post(
        "/api/organization/current/departments/dept-engineering/archive",
        json={"organizationId": "org-other"},
        headers=csrf_headers(),
    )
    reset = client.post(
        "/api/organization/current/demo/reset",
        json={"organizationId": "org-other"},
        headers=csrf_headers(),
    )

    assert archive.status_code == 422
    assert reset.status_code == 422


def test_dev_login_returns_organization_capability_and_rotated_csrf(monkeypatch) -> None:
    monkeypatch.setenv("ORGANIZATION_DEMO_ENABLED", "true")
    monkeypatch.setenv("DEV_LOGIN_ENABLED", "true")
    monkeypatch.setenv("APP_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAIN", "demo.example")
    monkeypatch.setattr(main, "_organization_store", InMemoryOrganizationStore())
    client = TestClient(main.app)
    csrf = client.get("/api/auth/csrf").json()["csrfToken"]

    response = client.post(
        "/api/auth/dev-login",
        json={"email": "admin@demo.example"},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["organizationDemoEnabled"] is True
    assert payload["organizationRole"] == "admin"
    assert payload["canManageOrganization"] is True
    assert payload["csrfToken"]
