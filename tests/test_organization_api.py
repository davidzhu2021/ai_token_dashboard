"""Mock V2 customer-organization API contracts and identity separation."""

import base64
import json
import os
from typing import Any

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from backend import main
from backend.auth import CSRF_SESSION_KEY, SESSION_USER_KEY
from backend.organization_store import InMemoryOrganizationStore


CSRF_TOKEN = "organization-v2-api-csrf"
PLATFORM_EMAIL = "platform-admin@example.test"


def signed_session(payload: dict[str, Any]) -> str:
    data = base64.b64encode(json.dumps(payload).encode("utf-8"))
    return TimestampSigner(os.getenv("SESSION_SECRET", "dev-session-secret-change-me")).sign(data).decode("utf-8")


def organization_client(monkeypatch, *, email: str, platform_admin: bool = False) -> TestClient:
    monkeypatch.setenv("ORGANIZATION_DEMO_ENABLED", "true")
    monkeypatch.setenv("ADMIN_EMAILS", PLATFORM_EMAIL)
    monkeypatch.setattr(main, "_organization_store", InMemoryOrganizationStore())
    client = TestClient(main.app, raise_server_exceptions=False)
    client.cookies.set(
        main.SESSION_COOKIE_NAME,
        signed_session(
            {
                SESSION_USER_KEY: {
                    "email": email,
                    "name": "Platform Test User" if platform_admin else "Customer Test User",
                    "avatar": "T",
                    "department": "Engineering",
                    "isAdmin": platform_admin,
                },
                CSRF_SESSION_KEY: CSRF_TOKEN,
            }
        ),
    )
    return client


def write_headers() -> dict[str, str]:
    return {"X-CSRF-Token": CSRF_TOKEN}


def error_code(response) -> str:
    return response.json()["detail"]["code"]


def test_organization_demo_routes_are_hidden_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("ORGANIZATION_DEMO_ENABLED", "false")
    client = TestClient(main.app)

    assert client.get("/api/organization/current").status_code == 404
    assert client.get("/api/platform/organizations").status_code == 404


def test_platform_and_customer_roles_are_never_mixed(monkeypatch) -> None:
    platform_client = organization_client(
        monkeypatch, email=PLATFORM_EMAIL, platform_admin=True
    )
    customer_client = organization_client(monkeypatch, email="admin@demo.example")

    platform_me = platform_client.get("/api/auth/me")
    customer_me = customer_client.get("/api/auth/me")

    assert platform_me.status_code == 200
    assert platform_me.json()["isAdmin"] is True
    assert platform_me.json()["isPlatformAdmin"] is True
    assert platform_me.json()["organizationRole"] is None
    assert platform_me.json()["organizationId"] is None
    assert platform_me.json()["canViewOrganizationUsage"] is False
    assert platform_me.json()["canManageOrganization"] is False
    assert platform_me.json()["canManageCustomerOrganizations"] is True
    assert platform_me.json()["isKnownDemoCustomerIdentity"] is False
    assert platform_me.json()["organizationAccessStatus"] is None
    assert customer_me.status_code == 200
    assert customer_me.json()["isAdmin"] is False
    assert customer_me.json()["isPlatformAdmin"] is False
    assert customer_me.json()["organizationRole"] == "admin"
    assert customer_me.json()["organizationId"] == "org-demo"
    assert customer_me.json()["organizationName"] == "Demo Company"
    assert customer_me.json()["organization"]["id"] == "org-demo"
    assert customer_me.json()["organization"]["name"] == "Demo Company"
    assert customer_me.json()["canViewOrganizationUsage"] is True
    # 甲方企业管理员现在可以维护本企业组织资料，但绝不获得乙方的客户目录。
    assert customer_me.json()["canManageOrganization"] is True
    assert customer_me.json()["canManageCustomerOrganizations"] is False
    assert customer_me.json()["isKnownDemoCustomerIdentity"] is True
    assert customer_me.json()["organizationAccessStatus"] == "active"


def test_platform_customer_directory_filters_paginates_and_is_platform_only(monkeypatch) -> None:
    platform_client = organization_client(
        monkeypatch, email=PLATFORM_EMAIL, platform_admin=True
    )
    customer_client = organization_client(monkeypatch, email="admin@demo.example")

    page = platform_client.get("/api/platform/organizations?search=北&page=1&pageSize=1")
    denied = customer_client.get("/api/platform/organizations")

    assert page.status_code == 200
    payload = page.json()
    assert payload["total"] == 1
    assert payload["page"] == 1
    assert payload["pageSize"] == 1
    assert [item["id"] for item in payload["items"]] == ["org-aurora"]
    assert denied.status_code == 403


def test_platform_create_sets_default_department_and_active_admin(monkeypatch) -> None:
    client = organization_client(monkeypatch, email=PLATFORM_EMAIL, platform_admin=True)

    created = client.post(
        "/api/platform/organizations",
        json={
            "name": "新客户企业",
            "adminName": "Initial Admin",
            "adminEmail": "initial.admin@customer.example",
        },
        headers=write_headers(),
    )

    assert created.status_code == 200
    payload = created.json()
    assert payload["department"]["name"] == "企业管理"
    assert payload["admin"]["role"] == "admin"
    assert payload["admin"]["status"] == "active"
    assert "owner" not in payload
    assert payload["stats"]["departmentCount"] == 1
    assert payload["stats"]["activeMemberCount"] == 1


def test_platform_create_rejects_the_removed_owner_fields(monkeypatch) -> None:
    """`ownerName` / `ownerEmail` 已被 `adminName` / `adminEmail` 取代，不留兼容层。"""

    client = organization_client(monkeypatch, email=PLATFORM_EMAIL, platform_admin=True)

    legacy = client.post(
        "/api/platform/organizations",
        json={
            "name": "Legacy Owner Customer",
            "ownerName": "Legacy Owner",
            "ownerEmail": "legacy.owner@customer.example",
        },
        headers=write_headers(),
    )

    assert legacy.status_code == 422
    assert client.get("/api/platform/organizations?search=Legacy").json()["total"] == 0


def test_platform_create_rejects_client_organization_id_and_requires_csrf(monkeypatch) -> None:
    client = organization_client(monkeypatch, email=PLATFORM_EMAIL, platform_admin=True)
    body = {
        "name": "Strict Customer",
        "adminName": "Strict Admin",
        "adminEmail": "strict.admin@customer.example",
    }

    missing_csrf = client.post("/api/platform/organizations", json=body)
    extra_field = client.post(
        "/api/platform/organizations",
        json={**body, "organizationId": "org-other"},
        headers=write_headers(),
    )

    assert missing_csrf.status_code == 403
    assert error_code(missing_csrf) == "AUTH_CSRF_INVALID"
    assert extra_field.status_code == 422


def test_platform_customer_usage_never_calls_upstream(monkeypatch) -> None:
    client = organization_client(monkeypatch, email=PLATFORM_EMAIL, platform_admin=True)

    def unexpected_client() -> Any:
        raise AssertionError("Mock customer usage must not initialize the upstream client")

    monkeypatch.setattr(main, "client", unexpected_client)
    usage = client.get(
        "/api/platform/organizations/org-aurora/usage?start_date=2026-01-01&end_date=2026-01-03"
    )
    departments = client.get(
        "/api/platform/organizations/org-aurora/departments/usage?start_date=2026-01-01&end_date=2026-01-03"
    )

    assert usage.status_code == 200
    assert departments.status_code == 200
    assert usage.json()["organization"]["id"] == "org-aurora"
    assert departments.json()["organization"]["id"] == "org-aurora"
    assert {row["organizationId"] for row in usage.json()["rows"]} == {"org-aurora"}
    assert {row["organizationId"] for row in departments.json()["rows"]} == {"org-aurora"}


def test_customer_usage_scope_is_derived_from_the_session(monkeypatch) -> None:
    client = organization_client(monkeypatch, email="admin@demo.example")

    usage = client.get(
        "/api/organization/current/usage?organizationId=org-aurora&start_date=2026-01-01&end_date=2026-01-03"
    )
    departments = client.get(
        "/api/organization/current/departments/usage?organizationId=org-aurora&start_date=2026-01-01&end_date=2026-01-03"
    )

    assert usage.status_code == 200
    assert departments.status_code == 200
    assert usage.json()["organization"]["id"] == "org-demo"
    assert departments.json()["organization"]["id"] == "org-demo"
    assert {row["organizationId"] for row in usage.json()["rows"]} == {"org-demo"}
    assert {row["organizationId"] for row in departments.json()["rows"]} == {"org-demo"}


def test_customer_member_cannot_access_company_boards_or_platform_directory(monkeypatch) -> None:
    client = organization_client(monkeypatch, email="avery.chen@demo.example")

    current = client.get("/api/organization/current")
    usage = client.get("/api/organization/current/usage")
    departments = client.get("/api/organization/current/departments/usage")
    customers = client.get("/api/platform/organizations")

    assert current.status_code == 403
    assert error_code(current) == "ORGANIZATION_DIRECTORY_FORBIDDEN"
    assert usage.status_code == 403
    assert error_code(usage) == "ORGANIZATION_USAGE_FORBIDDEN"
    assert departments.status_code == 403
    assert error_code(departments) == "ORGANIZATION_USAGE_FORBIDDEN"
    assert customers.status_code == 403


def test_both_parties_can_maintain_customer_master_data(monkeypatch) -> None:
    """甲方管理员维护本企业资料，乙方平台管理员保留跨企业协助入口。"""

    customer_client = organization_client(monkeypatch, email="admin@demo.example")
    platform_client = organization_client(
        monkeypatch, email=PLATFORM_EMAIL, platform_admin=True
    )

    customer_write = customer_client.post(
        "/api/organization/current/departments",
        json={"name": "Customer Managed Department"},
        headers=write_headers(),
    )
    platform_write = platform_client.post(
        "/api/platform/organizations/org-demo/departments",
        json={"name": "Seller Managed Department"},
        headers=write_headers(),
    )

    assert customer_write.status_code == 200
    assert customer_write.json()["department"]["name"] == "Customer Managed Department"
    assert platform_write.status_code == 200
    assert platform_write.json()["department"]["name"] == "Seller Managed Department"


def test_customer_admin_writes_stay_inside_the_session_tenant(monkeypatch) -> None:
    """甲方管理员的写操作范围由服务端从会话解析，客户端无法指定别家企业。"""

    client = organization_client(monkeypatch, email="admin@demo.example")

    created = client.post(
        "/api/organization/current/departments",
        json={"name": "Scoped Department", "organizationId": "org-aurora"},
        headers=write_headers(),
    )
    cross_tenant_member = client.patch(
        "/api/organization/current/members/member-aurora-001",
        json={"status": "suspended"},
        headers=write_headers(),
    )

    # 多余的 organizationId 被请求模型拒绝，不会静默落到别家企业。
    assert created.status_code == 422
    # 北辰的成员 id 在本企业范围内查不到。
    assert cross_tenant_member.status_code == 404
    assert error_code(cross_tenant_member) == "ORGANIZATION_NOT_FOUND"


def test_customer_admin_cannot_touch_customer_lifecycle(monkeypatch) -> None:
    """企业创建、改名、状态、归档和全局演示重置仍然只属于乙方。"""

    client = organization_client(monkeypatch, email="admin@demo.example")

    create = client.post(
        "/api/platform/organizations",
        json={
            "name": "甲方越权开户",
            "adminName": "Escalated Admin",
            "adminEmail": "escalated.admin@customer.example",
        },
        headers=write_headers(),
    )
    rename = client.patch(
        "/api/platform/organizations/org-demo",
        json={"name": "Renamed By Customer"},
        headers=write_headers(),
    )
    archive = client.post(
        "/api/platform/organizations/org-demo/archive", json={}, headers=write_headers()
    )
    reset = client.post(
        "/api/platform/organizations/demo/reset", json={}, headers=write_headers()
    )

    assert create.status_code == 403
    assert rename.status_code == 403
    assert archive.status_code == 403
    assert reset.status_code == 403


def test_platform_cross_customer_member_id_is_not_found(monkeypatch) -> None:
    client = organization_client(monkeypatch, email=PLATFORM_EMAIL, platform_admin=True)

    response = client.patch(
        "/api/platform/organizations/org-aurora/members/member-001",
        json={"status": "suspended"},
        headers=write_headers(),
    )

    assert response.status_code == 404
    assert error_code(response) == "ORGANIZATION_NOT_FOUND"


def test_customer_admin_removes_a_suspended_member(monkeypatch) -> None:
    """删除对已启用成员关闭，成功后默认名册看不到、可按已移除筛选回查。"""

    client = organization_client(monkeypatch, email="admin@demo.example")

    active_removal = client.delete(
        "/api/organization/current/members/member-001", headers=write_headers()
    )
    suspended = client.patch(
        "/api/organization/current/members/member-001",
        json={"status": "suspended"},
        headers=write_headers(),
    )
    removed = client.delete(
        "/api/organization/current/members/member-001", headers=write_headers()
    )
    repeat = client.delete(
        "/api/organization/current/members/member-001", headers=write_headers()
    )
    default_list = client.get("/api/organization/current/members?pageSize=50")
    removed_list = client.get("/api/organization/current/members?status=removed&pageSize=50")

    assert active_removal.status_code == 409
    assert error_code(active_removal) == "ORGANIZATION_MEMBER_REMOVE_NOT_ALLOWED"
    assert suspended.status_code == 200
    assert removed.status_code == 200
    assert removed.json()["member"]["status"] == "removed"
    assert removed.json()["member"]["removedAt"]
    assert repeat.status_code == 409
    assert error_code(repeat) == "ORGANIZATION_MEMBER_ALREADY_REMOVED"
    assert "member-001" not in {item["id"] for item in default_list.json()["items"]}
    assert [item["id"] for item in removed_list.json()["items"]] == ["member-001"]


def test_customer_admin_deletes_a_member_whose_invitation_never_succeeded(monkeypatch) -> None:
    """邀请没被接受的成员可以直接删除，不用先暂停。"""

    client = organization_client(monkeypatch, email="admin@demo.example")

    created = client.post(
        "/api/organization/current/members",
        json={
            "name": "Robin Pending",
            "email": "robin.pending@demo.example",
            "departmentId": "dept-product",
        },
        headers=write_headers(),
    )
    member_id = created.json()["member"]["id"]
    assert created.json()["member"]["status"] == "invited"

    removed = client.delete(
        f"/api/organization/current/members/{member_id}", headers=write_headers()
    )
    default_list = client.get("/api/organization/current/members?pageSize=50")

    assert removed.status_code == 200
    assert removed.json()["member"]["status"] == "removed"
    assert member_id not in {item["id"] for item in default_list.json()["items"]}


def test_member_removal_requires_csrf_and_management_rights(monkeypatch) -> None:
    admin_client = organization_client(monkeypatch, email="admin@demo.example")
    member_client = organization_client(monkeypatch, email="avery.chen@demo.example")

    missing_csrf = admin_client.delete("/api/organization/current/members/member-001")
    forbidden = member_client.delete(
        "/api/organization/current/members/member-001", headers=write_headers()
    )
    missing_member = admin_client.delete(
        "/api/organization/current/members/member-missing", headers=write_headers()
    )

    assert missing_csrf.status_code == 403
    assert forbidden.status_code == 403
    assert missing_member.status_code == 404
    assert error_code(missing_member) == "ORGANIZATION_NOT_FOUND"


def test_member_edit_cannot_write_the_removed_status(monkeypatch) -> None:
    """移除必须走删除接口，那里才会撤销令牌、作废邀请并解绑登录账号。"""

    client = organization_client(monkeypatch, email="admin@demo.example")

    response = client.patch(
        "/api/organization/current/members/member-001",
        json={"status": "removed"},
        headers=write_headers(),
    )

    assert response.status_code == 400
    assert error_code(response) == "ORGANIZATION_MEMBER_REMOVE_REQUIRED"


def test_platform_admin_removes_a_suspended_customer_member(monkeypatch) -> None:
    platform_client = organization_client(
        monkeypatch, email=PLATFORM_EMAIL, platform_admin=True
    )
    customer_client = organization_client(monkeypatch, email="admin@demo.example")

    platform_client.patch(
        "/api/platform/organizations/org-demo/members/member-002",
        json={"status": "suspended"},
        headers=write_headers(),
    )
    removed = platform_client.delete(
        "/api/platform/organizations/org-demo/members/member-002", headers=write_headers()
    )
    cross_tenant = platform_client.delete(
        "/api/platform/organizations/org-aurora/members/member-002", headers=write_headers()
    )
    customer_attempt = customer_client.delete(
        "/api/platform/organizations/org-demo/members/member-003", headers=write_headers()
    )

    assert removed.status_code == 200
    assert removed.json()["member"]["status"] == "removed"
    assert cross_tenant.status_code == 404
    assert customer_attempt.status_code == 403


def test_platform_reset_restores_all_seed_customers(monkeypatch) -> None:
    client = organization_client(monkeypatch, email=PLATFORM_EMAIL, platform_admin=True)
    created = client.post(
        "/api/platform/organizations",
        json={
            "name": "Temporary Customer",
            "adminName": "Temporary Admin",
            "adminEmail": "temporary.admin@customer.example",
        },
        headers=write_headers(),
    )
    reset = client.post("/api/platform/organizations/demo/reset", json={}, headers=write_headers())

    assert created.status_code == 200
    assert reset.status_code == 200
    payload = reset.json()
    assert payload["ok"] is True
    assert payload["total"] == 3
    assert {item["id"] for item in payload["items"]} == {
        "org-demo",
        "org-aurora",
        "org-harbor",
    }
