"""Backend contracts for the local-only Mock customer enterprise billing."""

import base64
import json
import os
from typing import Any

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from backend import main
from backend.auth import CSRF_SESSION_KEY, SESSION_USER_KEY
from backend.organization_store import InMemoryOrganizationStore


CSRF_TOKEN = "organization-billing-csrf"
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
    user: dict[str, Any] = {
        "email": email,
        "name": "Mock Billing User",
        "avatar": "B",
        "department": "Engineering",
        "isAdmin": platform_admin,
    }
    if auth_type:
        user["authType"] = auth_type
    client = TestClient(main.app, raise_server_exceptions=False)
    client.cookies.set(
        main.SESSION_COOKIE_NAME,
        signed_session({SESSION_USER_KEY: user, CSRF_SESSION_KEY: CSRF_TOKEN}),
    )
    return client


def csrf_headers() -> dict[str, str]:
    return {"X-CSRF-Token": CSRF_TOKEN}


def error_code(response) -> str:
    return response.json()["detail"]["code"]


def assert_opening_balance(payload: dict[str, Any], organization_id: str) -> None:
    account = payload["account"]
    assert payload["organization"]["id"] == organization_id
    assert account == {
        "initialBalanceUsd": 5000.0,
        "totalTopupsUsd": 0.0,
        "totalCreditsUsd": 5000.0,
        "availableBalanceUsd": 5000.0,
    }
    assert payload["isDemo"] is True
    assert payload["usageDoesNotAffectBalance"] is True
    assert set(payload["usageSummary"]) == {"today", "last7Days", "last30Days"}
    for summary in payload["usageSummary"].values():
        assert {"spend", "tokens", "requests", "totalTokens", "requestCount"} <= set(summary)
    assert payload["records"] == {
        "items": [
            {
                "id": "billing-initial-credit",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "type": "initial_credit",
                "amountUsd": 5000.0,
                "balanceAfterUsd": 5000.0,
                "operator": "System",
                "operatorEmail": "",
                "status": "completed",
            }
        ],
        "total": 1,
        "page": 1,
        "pageSize": 20,
    }


def test_enterprise_admins_receive_billing_capabilities_but_platform_does_not(monkeypatch) -> None:
    store = InMemoryOrganizationStore()
    platform = demo_client(monkeypatch, PLATFORM_EMAIL, platform_admin=True, store=store)

    platform_me = platform.get("/api/auth/me")
    assert platform_me.status_code == 200
    assert platform_me.json()["isPlatformAdmin"] is True
    assert platform_me.json()["organizationRole"] is None
    assert platform_me.json()["canViewOrganizationBilling"] is False
    assert platform_me.json()["canSimulateOrganizationTopup"] is False

    for email in ("owner@demo.example", "admin@demo.example"):
        customer = demo_client(monkeypatch, email, store=store)
        me = customer.get("/api/auth/me")

        assert me.status_code == 200
        assert me.json()["isAdmin"] is False
        assert me.json()["isPlatformAdmin"] is False
        assert me.json()["organizationRole"] == "admin"
        assert me.json()["canViewOrganizationBilling"] is True
        assert me.json()["canSimulateOrganizationTopup"] is True
        # 额度能力与组织管理能力同源，都来自启用的企业管理员身份。
        assert me.json()["canManageOrganization"] is True

    member = demo_client(monkeypatch, "avery.chen@demo.example", store=store)
    member_me = member.get("/api/auth/me")
    assert member_me.status_code == 200
    assert member_me.json()["canViewOrganizationBilling"] is False
    assert member_me.json()["canSimulateOrganizationTopup"] is False


def test_scope_response_carries_the_same_billing_capabilities(monkeypatch) -> None:
    owner = demo_client(monkeypatch, "owner@demo.example")
    platform = demo_client(monkeypatch, PLATFORM_EMAIL, platform_admin=True)

    owner_scope = owner.get("/api/auth/scope")
    platform_scope = platform.get("/api/auth/scope")

    assert owner_scope.status_code == 200
    assert owner_scope.json()["canViewOrganizationBilling"] is True
    assert owner_scope.json()["canSimulateOrganizationTopup"] is True
    assert platform_scope.status_code == 200
    assert platform_scope.json()["canViewOrganizationBilling"] is False
    assert platform_scope.json()["canSimulateOrganizationTopup"] is False
    # 演示客户走企业额度合约，个人自助充值入口对其恒不可见——侧边栏靠这个字段
    # 决定「充值中心」，不能让它反过来暴露个人充值页。
    assert owner_scope.json()["billingAvailable"] is False


def test_platform_scope_stays_local_in_organization_demo(monkeypatch) -> None:
    """Seller demo bootstrap must not resolve a legacy upstream team scope."""

    platform = demo_client(monkeypatch, PLATFORM_EMAIL, platform_admin=True)

    async def unexpected_upstream_scope(_user: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("platform demo scope must not call the upstream resolver")

    monkeypatch.setattr(main, "team_scope_for_user", unexpected_upstream_scope)

    response = platform.get("/api/auth/scope")

    assert response.status_code == 200
    assert response.json()["isTeamLeader"] is False
    assert response.json()["teamBoardStatus"] == "none"
    assert response.json()["team"] is None
    assert response.json()["leaderTeams"] == []
    assert response.json()["canManageCustomerOrganizations"] is True


def test_customer_billing_starts_at_opening_credit_and_ignores_client_tenant_query(monkeypatch) -> None:
    client = demo_client(monkeypatch, "owner@demo.example")

    response = client.get("/api/organization/current/billing?organizationId=org-aurora")

    assert response.status_code == 200
    assert_opening_balance(response.json(), "org-demo")


def test_simulated_topup_is_immediate_paginated_and_has_no_real_side_effects(monkeypatch) -> None:
    client = demo_client(monkeypatch, "owner@demo.example")

    def unexpected(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Mock organization billing must not use a real integration")

    # These are distinct production paths; none may be reached by the Mock route.
    monkeypatch.setattr(main, "billing_store", unexpected)
    monkeypatch.setattr(main, "require_billing_store", unexpected)
    monkeypatch.setattr(main, "client", unexpected)
    monkeypatch.setattr(main, "send_auth_email", unexpected)

    missing_csrf = client.post("/api/organization/current/billing/topups", json={"amountUsd": 1234.5})
    topup = client.post(
        "/api/organization/current/billing/topups?page=1&pageSize=1",
        json={"amountUsd": 1234.5},
        headers=csrf_headers(),
    )

    assert missing_csrf.status_code == 403
    assert error_code(missing_csrf) == "AUTH_CSRF_INVALID"
    assert topup.status_code == 200
    payload = topup.json()
    assert payload["ok"] is True
    assert payload["account"] == {
        "initialBalanceUsd": 5000.0,
        "totalTopupsUsd": 1234.5,
        "totalCreditsUsd": 6234.5,
        "availableBalanceUsd": 6234.5,
    }
    assert payload["record"]["type"] == "simulated_topup"
    assert payload["record"]["amountUsd"] == 1234.5
    assert payload["record"]["balanceAfterUsd"] == 6234.5
    assert payload["record"]["operator"] == "Mock Billing User"
    assert payload["record"]["operatorEmail"] == "owner@demo.example"
    assert payload["record"]["status"] == "completed"
    assert payload["records"]["total"] == 2
    assert payload["records"]["items"] == [payload["record"]]

    # Generated usage is only explanatory Mock data and must never reduce credit.
    usage = client.get(
        "/api/organization/current/usage?start_date=2026-01-01&end_date=2026-01-03"
    )
    second_page = client.get("/api/organization/current/billing?page=2&pageSize=1")
    assert usage.status_code == 200
    assert second_page.status_code == 200
    assert second_page.json()["account"]["availableBalanceUsd"] == 6234.5
    assert second_page.json()["records"] == {
        "items": [
            {
                "id": "billing-initial-credit",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "type": "initial_credit",
                "amountUsd": 5000.0,
                "balanceAfterUsd": 5000.0,
                "operator": "System",
                "operatorEmail": "",
                "status": "completed",
            }
        ],
        "total": 2,
        "page": 2,
        "pageSize": 1,
    }


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"amountUsd": 0},
        {"amountUsd": 100000.01},
        {"amountUsd": 1.001},
        {"amountUsd": "100"},
        {"amountUsd": True},
        {"amountUsd": 100, "organizationId": "org-aurora"},
    ],
)
def test_topup_rejects_non_numeric_out_of_range_subcent_and_extra_fields(monkeypatch, body) -> None:
    client = demo_client(monkeypatch, "owner@demo.example")

    response = client.post(
        "/api/organization/current/billing/topups",
        json=body,
        headers=csrf_headers(),
    )
    balance = client.get("/api/organization/current/billing")

    assert response.status_code == 422
    assert balance.status_code == 200
    assert_opening_balance(balance.json(), "org-demo")


@pytest.mark.parametrize(
    ("email", "platform_admin", "auth_type", "expected_code"),
    [
        ("avery.chen@demo.example", False, None, "ORGANIZATION_BILLING_FORBIDDEN"),
        ("flynn.gao@demo.example", False, None, "ORGANIZATION_MEMBER_INACTIVE"),
        ("indigo.xu@demo.example", False, None, "ORGANIZATION_MEMBER_INACTIVE"),
        (PLATFORM_EMAIL, True, None, "ORGANIZATION_MEMBERSHIP_REQUIRED"),
        ("platform-employee@example.test", False, None, "ORGANIZATION_MEMBERSHIP_REQUIRED"),
        ("owner@demo.example", False, "password", "ORGANIZATION_SSO_REQUIRED"),
    ],
)
def test_only_an_active_enterprise_admin_can_use_current_billing(
    monkeypatch, email, platform_admin, auth_type, expected_code
) -> None:
    client = demo_client(
        monkeypatch,
        email,
        platform_admin=platform_admin,
        auth_type=auth_type,
    )

    read = client.get("/api/organization/current/billing")
    write = client.post(
        "/api/organization/current/billing/topups",
        json={"amountUsd": 100},
        headers=csrf_headers(),
    )

    assert read.status_code == 403
    assert write.status_code == 403
    assert error_code(read) == expected_code
    assert error_code(write) == expected_code


def test_organization_balances_are_isolated_and_platform_billing_is_read_only(monkeypatch) -> None:
    store = InMemoryOrganizationStore()
    demo_owner = demo_client(monkeypatch, "owner@demo.example", store=store)
    aurora_owner = demo_client(monkeypatch, "ning.shen@aurora.example", store=store)
    platform = demo_client(monkeypatch, PLATFORM_EMAIL, platform_admin=True, store=store)

    credited = demo_owner.post(
        "/api/organization/current/billing/topups",
        json={"amountUsd": 500},
        headers=csrf_headers(),
    )
    aurora = aurora_owner.get("/api/organization/current/billing")
    demo_history = platform.get("/api/platform/organizations/org-demo/billing")
    aurora_history = platform.get("/api/platform/organizations/org-aurora/billing")
    customer_platform_read = demo_owner.get("/api/platform/organizations/org-demo/billing")
    platform_write = platform.post(
        "/api/platform/organizations/org-demo/billing",
        json={"amountUsd": 1},
        headers=csrf_headers(),
    )
    missing_customer = platform.get("/api/platform/organizations/org-missing/billing")

    assert credited.status_code == 200
    assert aurora.status_code == 200
    assert aurora.json()["account"]["availableBalanceUsd"] == 5000.0
    assert demo_history.status_code == 200
    assert demo_history.json()["account"]["availableBalanceUsd"] == 5500.0
    assert aurora_history.status_code == 200
    assert aurora_history.json()["account"]["availableBalanceUsd"] == 5000.0
    assert customer_platform_read.status_code == 403
    assert platform_write.status_code == 405
    assert missing_customer.status_code == 404
    assert error_code(missing_customer) == "ORGANIZATION_NOT_FOUND"


def test_archived_customer_is_blocked_but_platform_can_read_its_billing_history(monkeypatch) -> None:
    store = InMemoryOrganizationStore()
    platform = demo_client(monkeypatch, PLATFORM_EMAIL, platform_admin=True, store=store)
    harbor_owner = demo_client(monkeypatch, "lan.xu@harbor.example", store=store)

    archived = platform.post(
        "/api/platform/organizations/org-harbor/archive",
        json={},
        headers=csrf_headers(),
    )
    platform_history = platform.get("/api/platform/organizations/org-harbor/billing")
    customer_read = harbor_owner.get("/api/organization/current/billing")
    customer_write = harbor_owner.post(
        "/api/organization/current/billing/topups",
        json={"amountUsd": 100},
        headers=csrf_headers(),
    )

    assert archived.status_code == 200
    assert platform_history.status_code == 200
    assert_opening_balance(platform_history.json(), "org-harbor")
    assert customer_read.status_code == 403
    assert customer_write.status_code == 403
    assert error_code(customer_read) == "ORGANIZATION_MEMBER_INACTIVE"
    assert error_code(customer_write) == "ORGANIZATION_MEMBER_INACTIVE"


def test_created_customer_gets_opening_credit_and_platform_reset_restores_seed(monkeypatch) -> None:
    store = InMemoryOrganizationStore()
    platform = demo_client(monkeypatch, PLATFORM_EMAIL, platform_admin=True, store=store)
    demo_owner = demo_client(monkeypatch, "owner@demo.example", store=store)

    topup = demo_owner.post(
        "/api/organization/current/billing/topups",
        json={"amountUsd": 7.25},
        headers=csrf_headers(),
    )
    created = platform.post(
        "/api/platform/organizations",
        json={
            "name": "Billing Seed Customer",
            "adminName": "Billing Seed Admin",
            "adminEmail": "billing.seed.admin@customer.example",
        },
        headers=csrf_headers(),
    )
    assert topup.status_code == 200
    assert created.status_code == 200

    organization_id = created.json()["organization"]["id"]
    created_billing = platform.get(f"/api/platform/organizations/{organization_id}/billing")
    reset = platform.post("/api/platform/organizations/demo/reset", json={}, headers=csrf_headers())
    restored = demo_owner.get("/api/organization/current/billing")
    removed_customer = platform.get(f"/api/platform/organizations/{organization_id}/billing")

    assert created_billing.status_code == 200
    assert_opening_balance(created_billing.json(), organization_id)
    assert reset.status_code == 200
    assert_opening_balance(restored.json(), "org-demo")
    assert removed_customer.status_code == 404


def test_billing_routes_are_hidden_when_the_mock_switch_is_off(monkeypatch) -> None:
    monkeypatch.setenv("ORGANIZATION_DEMO_ENABLED", "false")
    client = TestClient(main.app, raise_server_exceptions=False)

    assert client.get("/api/organization/current/billing").status_code == 404
    assert client.get("/api/platform/organizations/org-demo/billing").status_code == 404
