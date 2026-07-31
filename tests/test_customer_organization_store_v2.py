"""Store-level isolation rules for the Mock V2 customer enterprise center."""

import pytest

from backend.organization_store import (
    DuplicateMemberEmailError,
    InMemoryOrganizationStore,
    OrganizationConflictError,
    OrganizationNotFoundError,
)


def test_v2_seed_has_three_independent_customer_organizations() -> None:
    store = InMemoryOrganizationStore()

    organizations = store.list_organizations(page_size=50)

    assert organizations["total"] == 3
    assert {item["id"] for item in organizations["items"]} == {
        "org-demo",
        "org-aurora",
        "org-harbor",
    }
    demo = store.get_organization_snapshot("org-demo")
    aurora = store.get_organization_snapshot("org-aurora")
    harbor = store.get_organization_snapshot("org-harbor")
    assert demo["stats"]["departmentCount"] == 3
    assert demo["stats"]["memberCount"] == 12
    for snapshot in (demo, aurora, harbor):
        members = store.list_members(organization_id=snapshot["organization"]["id"], page_size=50)["items"]
        # 每家演示企业都只有企业管理员和成员两级，且至少留两名启用管理员。
        assert {item["role"] for item in members} == {"admin", "member"}
        assert {item["status"] for item in members} == {"active", "invited", "suspended"}
        assert snapshot["stats"]["activeAdminCount"] == 2


def test_member_lookup_preserves_customer_scope_and_global_email_uniqueness() -> None:
    store = InMemoryOrganizationStore()

    resolved = store.resolve_member_by_email("NING.SHEN@AURORA.EXAMPLE")

    assert resolved is not None
    assert resolved["organizationId"] == "org-aurora"
    assert resolved["organization"]["name"] == "北辰智造有限公司"
    assert resolved["member"]["role"] == "admin"
    with pytest.raises(DuplicateMemberEmailError):
        store.create_member(
            "Conflicting Demo Email",
            "avery.chen@demo.example",
            "dept-aurora-research",
            organization_id="org-aurora",
        )


def test_department_and_member_ids_cannot_cross_customer_boundaries() -> None:
    store = InMemoryOrganizationStore()

    assert store.get_department("dept-engineering", organization_id="org-aurora") is None
    assert store.get_member("member-001", organization_id="org-aurora") is None
    with pytest.raises(OrganizationNotFoundError):
        store.update_member(
            "member-001",
            status="suspended",
            organization_id="org-aurora",
        )
    with pytest.raises(OrganizationNotFoundError):
        store.create_member(
            "Wrong Department",
            "wrong.department@aurora.example",
            "dept-engineering",
            organization_id="org-aurora",
        )


def test_mock_usage_rows_are_organization_scoped_and_created_member_has_no_history() -> None:
    store = InMemoryOrganizationStore()
    start_date = "2026-01-01"
    end_date = "2026-01-03"

    demo_usage = store.mock_organization_usage("org-demo", start_date, end_date)
    aurora_usage = store.mock_organization_usage("org-aurora", start_date, end_date)
    added = store.create_member(
        "No History Yet",
        "no.history@aurora.example",
        "dept-aurora-research",
        organization_id="org-aurora",
    )
    refreshed_usage = store.mock_organization_usage("org-aurora", start_date, end_date)

    assert {row["organizationId"] for row in demo_usage["rows"]} == {"org-demo"}
    assert {row["organizationId"] for row in aurora_usage["rows"]} == {"org-aurora"}
    assert all(item["employeeId"] != added["id"] for item in refreshed_usage["employees"])


def test_created_admin_and_invited_member_keep_empty_mock_usage_after_activation() -> None:
    store = InMemoryOrganizationStore()
    created = store.create_organization_with_admin(
        "No History Customer",
        "Fresh Admin",
        "fresh.admin@customer.example",
        organization_id="org-no-history",
    )
    organization_id = created["organization"]["id"]
    # 开户直接生成首位启用的企业管理员，不再有单独的「企业主」层级。
    assert created["admin"]["role"] == "admin"
    assert created["admin"]["status"] == "active"
    admin_id = created["admin"]["id"]
    invited = store.create_member(
        "Fresh Invitee",
        "fresh.invitee@customer.example",
        created["department"]["id"],
        organization_id=organization_id,
    )
    store.update_member(invited["id"], status="active", organization_id=organization_id)

    usage = store.mock_organization_usage(organization_id, "2026-01-01", "2026-01-03")

    assert usage["rows"] == []
    assert usage["employees"] == []
    assert admin_id != invited["id"]


def test_archive_preserves_platform_history_but_prevents_customer_mutation() -> None:
    store = InMemoryOrganizationStore()

    archived = store.archive_organization("org-harbor")
    historical = store.get_organization_snapshot("org-harbor")
    historical_usage = store.mock_organization_usage(
        "org-harbor", "2026-01-01", "2026-01-03"
    )

    assert archived["status"] == "archived"
    assert historical["stats"]["memberCount"] == 8
    assert historical_usage["rows"]
    with pytest.raises(OrganizationConflictError):
        store.create_department("Blocked After Archive", organization_id="org-harbor")
    with pytest.raises(OrganizationConflictError):
        store.update_member(
            "member-harbor-001",
            status="suspended",
            organization_id="org-harbor",
        )


def test_last_active_management_protection_is_per_customer() -> None:
    """最后一名启用管理员的保护按企业各自计算，不会被别家企业的管理员数量抵消。"""

    store = InMemoryOrganizationStore()

    # 北辰只剩一名启用管理员时不能再停用，即使 Demo Company 还有两名。
    store.update_member(
        "member-aurora-admin",
        role="member",
        organization_id="org-aurora",
    )
    with pytest.raises(OrganizationConflictError, match="active enterprise administrator"):
        store.update_member(
            "member-aurora-admin-primary",
            status="suspended",
            organization_id="org-aurora",
        )

    # Demo Company 的管理员照常可以停用，因为它自己还有启用管理员剩余。
    updated_demo_admin = store.update_member(
        "member-admin-primary",
        status="suspended",
        organization_id="org-demo",
    )

    assert updated_demo_admin["status"] == "suspended"
    assert store.get_member("member-aurora-admin-primary", organization_id="org-aurora")["status"] == "active"


def test_reset_all_restores_all_customer_seeds_after_a_platform_change() -> None:
    store = InMemoryOrganizationStore()
    created = store.create_organization_with_admin(
        "Transient Customer",
        "Transient Admin",
        "transient.admin@customer.example",
    )
    organization_id = created["organization"]["id"]

    assert store.list_organizations(page_size=50)["total"] == 4
    store.reset_all()

    assert store.get_organization(organization_id) is None
    assert store.list_organizations(page_size=50)["total"] == 3
