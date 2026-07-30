"""Security boundaries for the Mock V2 seller/customer organization routes."""

import base64
import json
import os
from typing import Any

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from backend import main
from backend.auth import CSRF_SESSION_KEY, SESSION_USER_KEY
from backend.organization_store import InMemoryOrganizationStore


CSRF_TOKEN = "organization-v2-routes-csrf"
PLATFORM_EMAIL = "platform-admin@example.test"


def signed_session(payload: dict[str, Any]) -> str:
    data = base64.b64encode(json.dumps(payload).encode("utf-8"))
    return TimestampSigner(os.getenv("SESSION_SECRET", "dev-session-secret-change-me")).sign(data).decode("utf-8")


def demo_client(
    monkeypatch,
    email: str,
    *,
    platform_admin: bool = False,
    store: InMemoryOrganizationStore | None = None,
    auth_type: str | None = None,
) -> TestClient:
    monkeypatch.setenv("ORGANIZATION_DEMO_ENABLED", "true")
    monkeypatch.setenv("ADMIN_EMAILS", PLATFORM_EMAIL)
    monkeypatch.setattr(main, "_organization_store", store or InMemoryOrganizationStore())
    client = TestClient(main.app, raise_server_exceptions=False)
    user = {
        "email": email,
        "name": "Organization Test User",
        "avatar": "O",
        "department": "Engineering",
        "isAdmin": platform_admin,
    }
    if auth_type:
        user["authType"] = auth_type
    client.cookies.set(
        main.SESSION_COOKIE_NAME,
        signed_session(
            {
                SESSION_USER_KEY: user,
                CSRF_SESSION_KEY: CSRF_TOKEN,
            }
        ),
    )
    return client


def csrf_headers() -> dict[str, str]:
    return {"X-CSRF-Token": CSRF_TOKEN}


def error_code(response) -> str:
    return response.json()["detail"]["code"]


def test_non_member_cannot_access_customer_data_or_platform_customer_directory(monkeypatch) -> None:
    client = demo_client(monkeypatch, "outside@demo.example")

    current = client.get("/api/organization/current")
    members = client.get("/api/organization/current/members")
    platform = client.get("/api/platform/organizations")

    for response in (current, members):
        assert response.status_code == 403
        assert error_code(response) == "ORGANIZATION_MEMBERSHIP_REQUIRED"
    assert platform.status_code == 403


def test_pending_and_suspended_members_cannot_access_customer_data(monkeypatch) -> None:
    for email in ("flynn.gao@demo.example", "indigo.xu@demo.example"):
        client = demo_client(monkeypatch, email)

        me = client.get("/api/auth/me")
        current = client.get("/api/organization/current")
        personal_usage = client.get("/api/me/usage")
        scope = client.get("/api/auth/scope")

        assert me.status_code == 200
        assert me.json()["organizationRole"] is None
        assert me.json()["organizationId"] is None
        assert me.json()["canViewOrganizationUsage"] is False
        assert me.json()["isKnownDemoCustomerIdentity"] is True
        assert me.json()["organizationAccessStatus"] == (
            "invited" if email == "flynn.gao@demo.example" else "suspended"
        )
        assert current.status_code == 403
        assert error_code(current) == "ORGANIZATION_MEMBER_INACTIVE"
        assert personal_usage.status_code != 200
        assert scope.status_code == 403
        assert error_code(scope) == "ORGANIZATION_MEMBER_INACTIVE"


def test_regular_customer_members_cannot_read_the_company_directory(monkeypatch) -> None:
    client = demo_client(monkeypatch, "avery.chen@demo.example")

    current = client.get("/api/organization/current")
    members = client.get("/api/organization/current/members")

    assert current.status_code == 403
    assert error_code(current) == "ORGANIZATION_DIRECTORY_FORBIDDEN"
    assert members.status_code == 403
    assert error_code(members) == "ORGANIZATION_DIRECTORY_FORBIDDEN"


def test_platform_admin_has_no_implicit_customer_membership(monkeypatch) -> None:
    client = demo_client(monkeypatch, PLATFORM_EMAIL, platform_admin=True)

    me = client.get("/api/auth/me")
    current = client.get("/api/organization/current")
    platform = client.get("/api/platform/organizations")

    assert me.status_code == 200
    assert me.json()["isPlatformAdmin"] is True
    assert me.json()["organizationRole"] is None
    assert me.json()["organizationId"] is None
    assert me.json()["canManageCustomerOrganizations"] is True
    assert current.status_code == 403
    assert error_code(current) == "ORGANIZATION_MEMBERSHIP_REQUIRED"
    assert platform.status_code == 200
    assert platform.json()["total"] == 3


def test_customer_owner_and_admin_can_view_company_usage_but_cannot_manage_data(monkeypatch) -> None:
    for email, expected_role in (("owner@demo.example", "owner"), ("admin@demo.example", "admin")):
        client = demo_client(monkeypatch, email)

        usage = client.get(
            "/api/organization/current/usage?start_date=2026-01-01&end_date=2026-01-03"
        )
        department_usage = client.get(
            "/api/organization/current/departments/usage?start_date=2026-01-01&end_date=2026-01-03"
        )
        mutation = client.post(
            "/api/organization/current/departments",
            json={"name": "Blocked Customer Write"},
            headers=csrf_headers(),
        )
        me = client.get("/api/auth/me")

        assert me.json()["organizationRole"] == expected_role
        assert me.json()["isAdmin"] is False
        assert me.json()["canViewOrganizationUsage"] is True
        assert me.json()["canManageOrganization"] is False
        assert usage.status_code == 200
        assert department_usage.status_code == 200
        assert {row["organizationId"] for row in usage.json()["rows"]} == {"org-demo"}
        assert {row["organizationId"] for row in department_usage.json()["rows"]} == {"org-demo"}
        assert mutation.status_code == 403
        assert error_code(mutation) == "ORGANIZATION_MANAGE_FORBIDDEN"


def test_platform_writes_require_csrf_and_have_no_mail_or_upstream_side_effects(monkeypatch) -> None:
    client = demo_client(monkeypatch, PLATFORM_EMAIL, platform_admin=True)

    async def unexpected_email(*_args, **_kwargs) -> None:
        raise AssertionError("Mock customer routes must not send invitation email")

    def unexpected_client() -> Any:
        raise AssertionError("Mock customer routes must not initialize an upstream client")

    monkeypatch.setattr(main, "send_auth_email", unexpected_email)
    monkeypatch.setattr(main, "client", unexpected_client)
    body = {
        "name": "No Side Effects Customer",
        "ownerName": "No Side Effects Owner",
        "ownerEmail": "no.side.effects.owner@customer.example",
    }

    missing_csrf = client.post("/api/platform/organizations", json=body)
    created = client.post("/api/platform/organizations", json=body, headers=csrf_headers())

    assert missing_csrf.status_code == 403
    assert error_code(missing_csrf) == "AUTH_CSRF_INVALID"
    assert created.status_code == 200
    assert created.json()["owner"]["status"] == "active"


def test_password_identity_cannot_inherit_a_matching_mock_customer_membership(monkeypatch) -> None:
    client = demo_client(monkeypatch, "admin@demo.example", auth_type="password")

    current = client.get("/api/organization/current")
    me = client.get("/api/auth/me")

    assert current.status_code == 403
    assert error_code(current) == "ORGANIZATION_SSO_REQUIRED"
    assert me.json()["organizationRole"] is None
    assert me.json()["canViewOrganizationUsage"] is False


def test_platform_member_mutation_returns_404_for_another_customer_and_rejects_body_tenant_id(monkeypatch) -> None:
    client = demo_client(monkeypatch, PLATFORM_EMAIL, platform_admin=True)

    other_customer_member = client.patch(
        "/api/platform/organizations/org-aurora/members/member-001",
        json={"status": "suspended"},
        headers=csrf_headers(),
    )
    client_tenant_id = client.patch(
        "/api/platform/organizations/org-demo/members/member-001",
        json={"status": "suspended", "organizationId": "org-aurora"},
        headers=csrf_headers(),
    )
    body_tenant_id = client.post(
        "/api/platform/organizations/org-demo/departments/dept-engineering/archive",
        json={"organizationId": "org-aurora"},
        headers=csrf_headers(),
    )

    assert other_customer_member.status_code == 404
    assert error_code(other_customer_member) == "ORGANIZATION_NOT_FOUND"
    assert client_tenant_id.status_code == 422
    assert body_tenant_id.status_code == 422


def test_archived_customer_blocks_customer_access_but_platform_can_read_history(monkeypatch) -> None:
    store = InMemoryOrganizationStore()
    platform_client = demo_client(
        monkeypatch,
        PLATFORM_EMAIL,
        platform_admin=True,
        store=store,
    )
    customer_client = demo_client(monkeypatch, "lan.xu@harbor.example", store=store)

    archived = platform_client.post(
        "/api/platform/organizations/org-harbor/archive",
        json={},
        headers=csrf_headers(),
    )
    rename = platform_client.patch(
        "/api/platform/organizations/org-harbor",
        json={"name": "Should Stay Archived"},
        headers=csrf_headers(),
    )
    detail = platform_client.get("/api/platform/organizations/org-harbor")
    history = platform_client.get(
        "/api/platform/organizations/org-harbor/usage?start_date=2026-01-01&end_date=2026-01-03"
    )
    customer_current = customer_client.get("/api/organization/current")
    customer_usage = customer_client.get("/api/me/usage")

    assert archived.status_code == 200
    assert archived.json()["organization"]["status"] == "archived"
    assert rename.status_code == 409
    assert error_code(rename) == "ORGANIZATION_CONFLICT"
    assert detail.status_code == 200
    assert detail.json()["organization"]["status"] == "archived"
    assert history.status_code == 200
    assert history.json()["rows"]
    assert customer_current.status_code == 403
    assert error_code(customer_current) == "ORGANIZATION_MEMBER_INACTIVE"
    assert customer_usage.status_code != 200


def test_mock_team_leader_uses_only_their_customer_team(monkeypatch) -> None:
    client = demo_client(monkeypatch, "owner@demo.example")

    scope = client.get("/api/auth/scope")
    team_usage = client.get(
        "/api/team/usage?start_date=2026-01-01&end_date=2026-01-03&include_member_rankings=true"
    )

    assert scope.status_code == 200
    assert scope.json()["isTeamLeader"] is True
    assert scope.json()["team"]["organizationId"] == "org-demo"
    assert team_usage.status_code == 200
    assert {row["organizationId"] for row in team_usage.json()["rows"]} == {"org-demo"}


def test_dev_login_allows_known_mock_customer_member_on_loopback_only(monkeypatch) -> None:
    monkeypatch.setenv("ORGANIZATION_DEMO_ENABLED", "true")
    monkeypatch.setenv("DEV_LOGIN_ENABLED", "true")
    monkeypatch.setenv("APP_BASE_URL", "http://127.0.0.1:8001")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAIN", "auto-link.com.cn")
    monkeypatch.setenv("ADMIN_EMAILS", PLATFORM_EMAIL)
    monkeypatch.setattr(main, "_organization_store", InMemoryOrganizationStore())
    client = TestClient(main.app, base_url="http://127.0.0.1:8001")
    csrf = client.get("/api/auth/csrf").json()["csrfToken"]

    accepted = client.post(
        "/api/auth/dev-login",
        json={"email": "ning.shen@aurora.example"},
        headers={"X-CSRF-Token": csrf},
    )
    rejected = client.post(
        "/api/auth/dev-login",
        json={"email": "unseeded@aurora.example"},
        headers={"X-CSRF-Token": accepted.json()["csrfToken"]},
    )

    assert accepted.status_code == 200
    assert accepted.json()["organizationId"] == "org-aurora"
    assert accepted.json()["organizationRole"] == "owner"
    assert accepted.json()["canViewOrganizationUsage"] is True
    assert accepted.json()["canManageOrganization"] is False
    assert rejected.status_code == 403


def test_dev_login_rejects_a_non_loopback_peer_even_with_a_loopback_host_header(monkeypatch) -> None:
    monkeypatch.setenv("ORGANIZATION_DEMO_ENABLED", "true")
    monkeypatch.setenv("DEV_LOGIN_ENABLED", "true")
    monkeypatch.setenv("APP_BASE_URL", "http://127.0.0.1:8001")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAIN", "auto-link.com.cn")
    monkeypatch.setattr(main, "_organization_store", InMemoryOrganizationStore())
    client = TestClient(main.app, base_url="http://127.0.0.1:8001")
    csrf = client.get("/api/auth/csrf").json()["csrfToken"]

    response = client.post(
        "/api/auth/dev-login",
        json={"email": "ning.shen@aurora.example"},
        headers={"X-CSRF-Token": csrf},
    )

    # TestClient represents the in-process peer as loopback. Exercise the
    # actual guard directly to ensure a Host header cannot substitute for it.
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "path": "/api/auth/dev-login",
        "raw_path": b"/api/auth/dev-login",
        "query_string": b"",
        "headers": [(b"host", b"127.0.0.1:8001")],
        "client": ("203.0.113.10", 51515),
        "server": ("127.0.0.1", 8001),
    }
    forged = Request(scope)

    assert response.status_code == 200
    assert main.is_loopback_request_peer(forged) is False


def test_demo_customer_debug_routes_fail_closed_without_an_upstream_client(monkeypatch) -> None:
    client = demo_client(monkeypatch, "owner@demo.example")
    monkeypatch.setenv("DEBUG_MAPPING_ENABLED", "true")

    def unexpected_client() -> Any:
        raise AssertionError("Mock customer debug requests must not initialize an upstream client")

    monkeypatch.setattr(main, "client", unexpected_client)

    mapping = client.get("/api/debug/me-mapping")
    comparison = client.get(
        "/api/debug/me-usage-compare?start_date=2026-01-01&end_date=2026-01-03"
    )

    assert mapping.status_code == 403
    assert error_code(mapping) == "ORGANIZATION_UPSTREAM_FORBIDDEN"
    assert comparison.status_code == 403
    assert error_code(comparison) == "ORGANIZATION_UPSTREAM_FORBIDDEN"


def test_mock_customer_models_keys_and_billing_are_blocked_without_upstream_calls(monkeypatch) -> None:
    client = demo_client(monkeypatch, "owner@demo.example")

    def unexpected_client() -> Any:
        raise AssertionError("Mock customer routes must not initialize an upstream client")

    monkeypatch.setattr(main, "client", unexpected_client)
    models = client.get("/api/models")
    keys = client.get("/api/me/keys")
    billing = client.get("/api/me/billing")

    assert models.status_code == 403
    assert error_code(models) == "ORGANIZATION_MODELS_FORBIDDEN"
    assert keys.status_code == 403
    assert error_code(keys) == "ORGANIZATION_UPSTREAM_FORBIDDEN"
    # A disabled billing ledger returns its existing 404 before identity
    # resolution; an enabled ledger reaches the explicit demo-identity gate.
    # Neither branch may initialize the upstream client.
    assert billing.status_code in {403, 404}
    if billing.status_code == 403:
        assert error_code(billing) == "ORGANIZATION_BILLING_FORBIDDEN"
