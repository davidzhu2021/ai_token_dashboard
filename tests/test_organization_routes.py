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


def reset_organization_usage_cache() -> None:
    main.organization_usage_cache.clear()


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


def test_customer_admins_get_company_boards_and_scoped_management(monkeypatch) -> None:
    """合并后每名甲方管理员都同时拥有企业看板和本企业组织维护权。"""

    for index, email in enumerate(("owner@demo.example", "admin@demo.example")):
        client = demo_client(monkeypatch, email)

        usage = client.get(
            "/api/organization/current/usage?start_date=2026-01-01&end_date=2026-01-03"
        )
        department_usage = client.get(
            "/api/organization/current/departments/usage?start_date=2026-01-01&end_date=2026-01-03"
        )
        mutation = client.post(
            "/api/organization/current/departments",
            json={"name": f"Customer Managed Department {index}"},
            headers=csrf_headers(),
        )
        me = client.get("/api/auth/me")

        assert me.json()["organizationRole"] == "admin"
        assert me.json()["isAdmin"] is False
        assert me.json()["isPlatformAdmin"] is False
        assert me.json()["canViewOrganizationUsage"] is True
        assert me.json()["canViewOrganizationBilling"] is True
        assert me.json()["canSimulateOrganizationTopup"] is True
        assert me.json()["canManageOrganization"] is True
        assert me.json()["canManageCustomerOrganizations"] is False
        assert usage.status_code == 200
        assert department_usage.status_code == 200
        assert {row["organizationId"] for row in usage.json()["rows"]} == {"org-demo"}
        assert {row["organizationId"] for row in department_usage.json()["rows"]} == {"org-demo"}
        assert mutation.status_code == 200
        assert mutation.json()["department"]["name"] == f"Customer Managed Department {index}"


def test_customer_admin_can_run_the_full_scoped_directory_lifecycle(monkeypatch) -> None:
    """甲方管理员在本企业内可完成部门与成员的增改归档、角色与状态变更。"""

    client = demo_client(monkeypatch, "admin@demo.example")

    department = client.post(
        "/api/organization/current/departments",
        json={"name": "客户成功部"},
        headers=csrf_headers(),
    )
    department_id = department.json()["department"]["id"]
    renamed = client.patch(
        f"/api/organization/current/departments/{department_id}",
        json={"name": "客户成功中心"},
        headers=csrf_headers(),
    )
    invited = client.post(
        "/api/organization/current/members",
        json={
            "name": "新同事",
            "email": "new.colleague@demo.example",
            "departmentId": department_id,
            "role": "member",
        },
        headers=csrf_headers(),
    )
    member_id = invited.json()["member"]["id"]
    promoted = client.patch(
        f"/api/organization/current/members/{member_id}",
        json={"role": "admin", "status": "active"},
        headers=csrf_headers(),
    )
    demoted = client.patch(
        f"/api/organization/current/members/{member_id}",
        json={"role": "member"},
        headers=csrf_headers(),
    )
    suspended = client.patch(
        f"/api/organization/current/members/{member_id}",
        json={"status": "suspended"},
        headers=csrf_headers(),
    )
    restored = client.patch(
        f"/api/organization/current/members/{member_id}",
        json={"status": "active"},
        headers=csrf_headers(),
    )
    # 归档前要先把成员移出该部门，否则存储层会拒绝。
    client.patch(
        f"/api/organization/current/members/{member_id}",
        json={"departmentId": "dept-engineering"},
        headers=csrf_headers(),
    )
    archived = client.post(
        f"/api/organization/current/departments/{department_id}/archive",
        json={},
        headers=csrf_headers(),
    )

    assert department.status_code == 200
    assert renamed.json()["department"]["name"] == "客户成功中心"
    assert invited.json()["member"]["status"] == "invited"
    assert promoted.json()["member"]["role"] == "admin"
    assert demoted.json()["member"]["role"] == "member"
    assert suspended.json()["member"]["status"] == "suspended"
    assert restored.json()["member"]["status"] == "active"
    assert archived.json()["department"]["status"] == "archived"


def test_customer_admin_cannot_strand_the_company_without_an_administrator(monkeypatch) -> None:
    # 用 member-admin-primary 的身份登录，才能先降级另一名管理员而不自断权限。
    client = demo_client(monkeypatch, "owner@demo.example")

    # Demo Company 种子有两名启用管理员，先降级一名。
    first = client.patch(
        "/api/organization/current/members/member-admin",
        json={"role": "member"},
        headers=csrf_headers(),
    )
    last_demotion = client.patch(
        "/api/organization/current/members/member-admin-primary",
        json={"role": "member"},
        headers=csrf_headers(),
    )
    last_suspension = client.patch(
        "/api/organization/current/members/member-admin-primary",
        json={"status": "suspended"},
        headers=csrf_headers(),
    )

    assert first.status_code == 200
    assert last_demotion.status_code == 409
    assert last_suspension.status_code == 409


def test_customer_member_role_only_accepts_admin_or_member(monkeypatch) -> None:
    client = demo_client(monkeypatch, "admin@demo.example")

    invited = client.post(
        "/api/organization/current/members",
        json={
            "name": "越权角色",
            "email": "rejected.owner@demo.example",
            "departmentId": "dept-engineering",
            "role": "owner",
        },
        headers=csrf_headers(),
    )
    updated = client.patch(
        "/api/organization/current/members/member-001",
        json={"role": "owner"},
        headers=csrf_headers(),
    )

    assert invited.status_code == 422
    assert updated.status_code == 422


def test_regular_customer_member_keeps_no_organization_management(monkeypatch) -> None:
    client = demo_client(monkeypatch, "avery.chen@demo.example")

    me = client.get("/api/auth/me")
    directory = client.get("/api/organization/current")
    members = client.get("/api/organization/current/members")
    billing = client.get("/api/organization/current/billing")
    mutation = client.post(
        "/api/organization/current/departments",
        json={"name": "Blocked Member Write"},
        headers=csrf_headers(),
    )

    assert me.json()["organizationRole"] == "member"
    assert me.json()["canManageOrganization"] is False
    assert me.json()["canViewOrganizationUsage"] is False
    assert me.json()["canViewOrganizationBilling"] is False
    assert directory.status_code == 403
    assert error_code(directory) == "ORGANIZATION_DIRECTORY_FORBIDDEN"
    assert members.status_code == 403
    assert billing.status_code == 403
    assert mutation.status_code == 403
    assert error_code(mutation) == "ORGANIZATION_MANAGE_FORBIDDEN"


def test_platform_admin_assists_across_customers_without_becoming_a_member(monkeypatch) -> None:
    store = InMemoryOrganizationStore()
    client = demo_client(monkeypatch, PLATFORM_EMAIL, platform_admin=True, store=store)

    me = client.get("/api/auth/me")
    demo_write = client.post(
        "/api/platform/organizations/org-demo/departments",
        json={"name": "Seller Assist Demo"},
        headers=csrf_headers(),
    )
    aurora_write = client.post(
        "/api/platform/organizations/org-aurora/departments",
        json={"name": "Seller Assist Aurora"},
        headers=csrf_headers(),
    )
    aurora_role_change = client.patch(
        "/api/platform/organizations/org-aurora/members/member-aurora-001",
        json={"role": "admin", "status": "active"},
        headers=csrf_headers(),
    )
    aurora_billing = client.get("/api/platform/organizations/org-aurora/billing")

    # 跨企业协助成立，但平台管理员自己没有任何客户成员身份。
    assert me.json()["organizationRole"] is None
    assert me.json()["organizationId"] is None
    assert me.json()["canManageOrganization"] is False
    assert demo_write.status_code == 200
    assert aurora_write.status_code == 200
    assert aurora_role_change.json()["member"]["role"] == "admin"
    assert aurora_billing.status_code == 200


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
        "adminName": "No Side Effects Admin",
        "adminEmail": "no.side.effects.admin@customer.example",
    }

    missing_csrf = client.post("/api/platform/organizations", json=body)
    created = client.post("/api/platform/organizations", json=body, headers=csrf_headers())

    assert missing_csrf.status_code == 403
    assert error_code(missing_csrf) == "AUTH_CSRF_INVALID"
    assert created.status_code == 200
    assert created.json()["admin"]["status"] == "active"
    assert created.json()["admin"]["role"] == "admin"


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


def test_platform_member_removal_cuts_that_member_off_from_the_customer(monkeypatch) -> None:
    """乙方删除成员后，对方的会话身份不再解析到这家企业，归档企业则不允许删除。"""

    store = InMemoryOrganizationStore()
    platform_client = demo_client(
        monkeypatch, PLATFORM_EMAIL, platform_admin=True, store=store
    )
    member_client = demo_client(monkeypatch, "indigo.xu@demo.example", store=store)

    before = member_client.get("/api/auth/me")
    removed = platform_client.delete(
        "/api/platform/organizations/org-demo/members/member-009",
        headers=csrf_headers(),
    )
    after = member_client.get("/api/auth/me")

    # 暂停期间身份仍然解析得到这家企业，只是没有访问权。
    assert before.json()["isKnownDemoCustomerIdentity"] is True
    assert before.json()["organizationAccessStatus"] == "suspended"
    assert removed.status_code == 200
    assert removed.json()["member"]["status"] == "removed"
    # 墓碑不再参与身份解析，离职成员的登录身份彻底与企业脱钩。
    assert after.json()["isKnownDemoCustomerIdentity"] is False
    assert after.json()["organizationAccessStatus"] is None
    assert after.json()["organizationId"] is None
    assert after.json()["organizationRole"] is None
    assert after.json()["canViewOrganizationUsage"] is False

    platform_client.post(
        "/api/platform/organizations/org-demo/archive", json={}, headers=csrf_headers()
    )
    archived_removal = platform_client.delete(
        "/api/platform/organizations/org-demo/members/member-010",
        headers=csrf_headers(),
    )

    assert archived_removal.status_code == 409
    assert error_code(archived_removal) == "ORGANIZATION_CONFLICT"


def test_platform_nested_department_routes_hide_cross_customer_resources(monkeypatch) -> None:
    """Every nested customer route must resolve its resource inside the URL scope."""

    client = demo_client(monkeypatch, PLATFORM_EMAIL, platform_admin=True)
    headers = csrf_headers()
    cross_customer_department = "dept-engineering"

    rename = client.patch(
        f"/api/platform/organizations/org-aurora/departments/{cross_customer_department}",
        json={"name": "Should Not Be Renamed"},
        headers=headers,
    )
    archive = client.post(
        f"/api/platform/organizations/org-aurora/departments/{cross_customer_department}/archive",
        json={},
        headers=headers,
    )
    members = client.get(
        f"/api/platform/organizations/org-aurora/members?departmentId={cross_customer_department}"
    )
    usage = client.get(
        "/api/platform/organizations/org-aurora/departments/usage"
        "?start_date=2026-01-01&end_date=2026-01-03"
        f"&department={cross_customer_department}"
    )

    for response in (rename, archive, members, usage):
        assert response.status_code == 404
        assert error_code(response) == "ORGANIZATION_NOT_FOUND"


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


def test_mock_team_usage_rejects_non_leaders_and_other_customer_team_refs(monkeypatch) -> None:
    member_client = demo_client(monkeypatch, "avery.chen@demo.example")
    leader_client = demo_client(monkeypatch, "owner@demo.example")

    non_leader = member_client.get(
        "/api/team/usage?start_date=2026-01-01&end_date=2026-01-03"
    )
    cross_customer_ref = leader_client.get(
        "/api/team/usage?start_date=2026-01-01&end_date=2026-01-03"
        "&team_ref=mock-org-aurora-dept-aurora-research"
    )

    assert non_leader.status_code == 403
    assert error_code(non_leader) == "ORGANIZATION_SCOPE_FORBIDDEN"
    assert cross_customer_ref.status_code == 403
    assert error_code(cross_customer_ref) == "ORGANIZATION_SCOPE_FORBIDDEN"


def test_mock_team_member_usage_rejects_cross_customer_team_refs_without_upstream(monkeypatch) -> None:
    leader_client = demo_client(monkeypatch, "owner@demo.example")

    def unexpected_client() -> Any:
        raise AssertionError("Mock team member usage must not initialize an upstream client")

    monkeypatch.setattr(main, "client", unexpected_client)
    response = leader_client.get(
        "/api/team/member/usage?start_date=2026-01-01&end_date=2026-01-03"
        "&team_ref=mock-org-aurora-dept-aurora-research"
        "&employee=ning.shen%40aurora.example"
    )

    assert response.status_code == 403
    assert error_code(response) == "ORGANIZATION_SCOPE_FORBIDDEN"


def test_customer_board_cache_is_scoped_to_its_organization_and_request_filters(monkeypatch) -> None:
    store = InMemoryOrganizationStore()
    demo_owner = demo_client(monkeypatch, "owner@demo.example", store=store)
    aurora_owner = demo_client(monkeypatch, "ning.shen@aurora.example", store=store)
    query = "start_date=2026-01-01&end_date=2026-01-03&source=all"
    reset_organization_usage_cache()

    demo_first = demo_owner.get(f"/api/organization/current/usage?{query}")
    demo_cached = demo_owner.get(f"/api/organization/current/usage?{query}")
    aurora = aurora_owner.get(f"/api/organization/current/usage?{query}")
    demo_filtered = demo_owner.get(
        f"/api/organization/current/usage?{query}&employee=avery.chen%40demo.example"
    )

    assert demo_first.status_code == 200
    assert demo_first.json()["cache"]["hit"] is False
    assert demo_cached.status_code == 200
    assert demo_cached.json()["cache"]["hit"] is True
    assert aurora.status_code == 200
    assert aurora.json()["cache"]["hit"] is False
    assert {row["organizationId"] for row in demo_cached.json()["rows"]} == {"org-demo"}
    assert {row["organizationId"] for row in aurora.json()["rows"]} == {"org-aurora"}
    assert {row["employeeEmail"] for row in demo_filtered.json()["rows"]} == {"avery.chen@demo.example"}


def test_customer_board_cache_version_invalidates_after_a_direct_store_change(monkeypatch) -> None:
    """The store revision is a second defense when a write bypasses HTTP cache clearing."""

    store = InMemoryOrganizationStore()
    client = demo_client(monkeypatch, PLATFORM_EMAIL, platform_admin=True, store=store)
    query = "start_date=2026-01-01&end_date=2026-01-03&source=all"
    reset_organization_usage_cache()

    first = client.get(f"/api/platform/organizations/org-demo/usage?{query}")
    cached = client.get(f"/api/platform/organizations/org-demo/usage?{query}")
    store.update_member("member-001", status="suspended", organization_id="org-demo")
    after_direct_change = client.get(f"/api/platform/organizations/org-demo/usage?{query}")

    assert first.status_code == 200
    assert first.json()["cache"]["hit"] is False
    assert cached.status_code == 200
    assert cached.json()["cache"]["hit"] is True
    assert after_direct_change.status_code == 200
    assert after_direct_change.json()["cache"]["hit"] is False
    assert "avery.chen@demo.example" not in {
        row["employeeEmail"] for row in after_direct_change.json()["rows"]
    }


def test_platform_mutation_and_refresh_bypass_stale_customer_board_cache(monkeypatch) -> None:
    store = InMemoryOrganizationStore()
    platform = demo_client(monkeypatch, PLATFORM_EMAIL, platform_admin=True, store=store)
    query = "start_date=2026-01-01&end_date=2026-01-03&source=all"
    reset_organization_usage_cache()

    first = platform.get(f"/api/platform/organizations/org-demo/usage?{query}")
    cached = platform.get(f"/api/platform/organizations/org-demo/usage?{query}")
    refreshed = platform.get(f"/api/platform/organizations/org-demo/usage?{query}&refresh=1")
    suspended = platform.patch(
        "/api/platform/organizations/org-demo/members/member-001",
        json={"status": "suspended"},
        headers=csrf_headers(),
    )
    after_mutation = platform.get(f"/api/platform/organizations/org-demo/usage?{query}")

    assert first.status_code == 200
    assert first.json()["cache"]["hit"] is False
    assert cached.status_code == 200
    assert cached.json()["cache"]["hit"] is True
    assert refreshed.status_code == 200
    assert refreshed.json()["cache"]["hit"] is False
    assert suspended.status_code == 200
    assert after_mutation.status_code == 200
    assert after_mutation.json()["cache"]["hit"] is False
    assert "avery.chen@demo.example" not in {
        row["employeeEmail"] for row in after_mutation.json()["rows"]
    }


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
    assert accepted.json()["organizationRole"] == "admin"
    assert accepted.json()["canViewOrganizationUsage"] is True
    # 甲方管理员登录即带本企业管理权，但仍拿不到乙方的客户目录。
    assert accepted.json()["canManageOrganization"] is True
    assert accepted.json()["canManageCustomerOrganizations"] is False
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
