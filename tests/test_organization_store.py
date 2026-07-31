from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.organization_store import (
    DuplicateMemberEmailError,
    InMemoryOrganizationStore,
    OrganizationConflictError,
    OrganizationNotFoundError,
    OrganizationValidationError,
)


def test_seed_is_deterministic_and_reset_restores_it() -> None:
    store = InMemoryOrganizationStore()

    initial = store.get_current()

    assert initial["organization"]["id"] == "org-demo"
    assert initial["organization"]["isDemo"] is True
    assert [department["id"] for department in initial["departments"]] == [
        "dept-engineering",
        "dept-product",
        "dept-operations",
    ]
    assert initial["stats"] == {
        "departmentCount": 3,
        "memberCount": 12,
        "activeMemberCount": 7,
        "invitedMemberCount": 3,
        "suspendedMemberCount": 2,
        "activeAdminCount": 2,
    }
    members = store.list_members(page_size=50)["items"]
    # 甲方角色只剩企业管理员与成员，种子里不再有「企业主」这一层。
    assert {member["role"] for member in members} == {"admin", "member"}
    assert {member["status"] for member in members} == {"active", "invited", "suspended"}

    store.create_department("Temporary")
    store.create_member("New User", "new.user@demo.example", "dept-engineering")
    reset = store.reset()

    assert reset == initial


def test_department_create_rename_and_archive_rules() -> None:
    store = InMemoryOrganizationStore()

    department = store.create_department("Customer Success")
    renamed = store.update_department(department["id"], "Customer Experience")

    assert renamed["name"] == "Customer Experience"
    assert renamed["status"] == "active"
    with pytest.raises(OrganizationConflictError, match="already exists"):
        store.create_department(" customer experience ")
    with pytest.raises(OrganizationConflictError, match="move or suspend"):
        store.archive_department("dept-engineering")

    archived = store.archive_department(department["id"])
    assert archived["status"] == "archived"
    assert archived["archivedAt"]
    assert department["id"] not in {item["id"] for item in store.list_departments()}
    assert store.archive_department(department["id"])["status"] == "archived"
    with pytest.raises(OrganizationConflictError, match="archived"):
        store.update_department(department["id"], "Nope")


def test_member_creation_validates_and_normalizes_email() -> None:
    store = InMemoryOrganizationStore()

    invited = store.create_member(
        "  Morgan   Doe ", " Morgan.Doe@Example.COM ", "dept-product", role="admin"
    )

    assert invited["name"] == "Morgan Doe"
    assert invited["email"] == "morgan.doe@example.com"
    assert invited["role"] == "admin"
    assert invited["status"] == "invited"
    assert invited["departmentName"] == "Product"
    assert store.get_member_by_email("MORGAN.DOE@EXAMPLE.COM")["id"] == invited["id"]
    with pytest.raises(DuplicateMemberEmailError):
        store.create_member("Duplicate", "morgan.doe@example.com", "dept-engineering")
    with pytest.raises(OrganizationValidationError):
        store.create_member("Bad", "not-an-email", "dept-engineering")
    with pytest.raises(OrganizationValidationError):
        store.create_member("Bad", "person..name@example.com", "dept-engineering")
    with pytest.raises(OrganizationValidationError):
        store.create_member("Bad", "bad@example.com", "dept-engineering", role="superadmin")
    with pytest.raises(OrganizationNotFoundError):
        store.create_member("Bad", "bad@example.com", "missing-department")


def test_member_update_supports_department_role_and_status() -> None:
    store = InMemoryOrganizationStore()
    invited = store.create_member("Riley User", "riley.user@example.com", "dept-engineering")

    updated = store.update_member(
        invited["id"],
        name="Riley Updated",
        department_id="dept-operations",
        role="admin",
        status="active",
    )

    assert updated["name"] == "Riley Updated"
    assert updated["departmentId"] == "dept-operations"
    assert updated["role"] == "admin"
    assert updated["status"] == "active"
    assert store.get_member(invited["id"]) == updated
    with pytest.raises(OrganizationValidationError, match="at least one"):
        store.update_member(invited["id"])
    with pytest.raises(OrganizationValidationError):
        store.update_member(invited["id"], status="deleted")


def test_archived_department_rejects_new_live_members() -> None:
    store = InMemoryOrganizationStore()
    department = store.create_department("Archive Me")
    store.archive_department(department["id"])

    with pytest.raises(OrganizationConflictError, match="archived"):
        store.create_member("Riley User", "riley.user@example.com", department["id"])

    suspended = store.get_member("member-009")
    store.update_member("member-003", department_id="dept-engineering")
    store.update_member("member-008", department_id="dept-engineering")
    store.archive_department("dept-operations")
    assert store.get_member("member-009")["departmentStatus"] == "archived"
    with pytest.raises(OrganizationConflictError, match="archived"):
        store.update_member(suspended["id"], status="active")


def test_last_active_enterprise_administrator_cannot_be_removed_or_demoted() -> None:
    """企业里必须始终留一名启用的企业管理员，否则甲方会把自己锁在门外。"""

    store = InMemoryOrganizationStore()

    # 种子里有两名启用管理员，先把其中一名降级，剩下的那名就成为最后一道防线。
    store.update_member("member-admin", role="member")
    assert store.get_current()["stats"]["activeAdminCount"] == 1

    with pytest.raises(OrganizationConflictError, match="active enterprise administrator"):
        store.update_member("member-admin-primary", role="member")
    with pytest.raises(OrganizationConflictError, match="active enterprise administrator"):
        store.update_member("member-admin-primary", status="suspended")

    # 新增并启用第二名管理员后即可完成交接。
    successor = store.create_member("Second Admin", "second.admin@example.com", "dept-product", role="admin")
    store.update_member(successor["id"], status="active")
    handed_over = store.update_member("member-admin-primary", status="suspended")
    assert handed_over["status"] == "suspended"

    # 交接完成后，接任者又变成最后一名启用管理员，同样受保护。
    with pytest.raises(OrganizationConflictError, match="active enterprise administrator"):
        store.update_member(successor["id"], role="member")


def test_member_filters_and_pagination_are_scoped_to_the_one_tenant() -> None:
    store = InMemoryOrganizationStore()

    page = store.list_members(keyword="avery", department_id="dept-engineering", page=1, page_size=1)

    assert page["total"] == 1
    assert page["items"][0]["email"] == "avery.chen@demo.example"
    assert page["page"] == 1
    assert page["pageSize"] == 1
    assert store.list_members(status="suspended", page_size=50)["total"] == 2
    assert store.list_members(role="admin", page_size=50)["total"] == 4
    assert store.list_members(role="member", page_size=50)["total"] == 8
    with pytest.raises(OrganizationValidationError):
        store.list_members(role="owner", page_size=50)
    with pytest.raises(OrganizationNotFoundError):
        store.list_members(department_id="missing", page_size=50)
    with pytest.raises(OrganizationValidationError):
        store.list_members(page=0)
    with pytest.raises(OrganizationValidationError):
        store.list_members(page_size=101)
    assert store.get_member("missing") is None
    assert store.get_department("missing") is None


def test_duplicate_invites_are_atomic_across_threads() -> None:
    store = InMemoryOrganizationStore()

    def invite(index: int):
        try:
            return store.create_member(f"Concurrent {index}", "same@example.com", "dept-product")
        except DuplicateMemberEmailError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(invite, range(8)))

    assert sum(result is not None for result in results) == 1
    assert store.get_member_by_email("same@example.com") is not None
