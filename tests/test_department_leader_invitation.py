"""邀请/编辑成员时指定部门负责人，并让负责人在真实模式拿到团队看板。

契约分三层：
1. 客户侧与平台侧的成员写接口把「部门职务」透传到目录层（`team_role`）。
2. 真实模式下负责人身份只来自本账号绑定的成员关系，且上游 Team 句柄不出网。
3. 前端弹窗、成员列表徽标与静态资源版本号保持同步。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend import main
from backend.auth import hash_password
from backend.auth_store import AuthStore


APP_JS = Path(__file__).parents[1] / "assets" / "app.js"
INDEX_HTML = Path(__file__).parents[1] / "index.html"


class _MemberRepository:
    """记录目录层收到的关键字参数，用来断言职务被透传。"""

    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []

    async def create_member_with_invitation(
        self,
        name: str,
        email: str,
        department_id: str,
        role: str = "member",
        *,
        organization_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.create_calls.append(
            {
                "organizationId": organization_id,
                "name": name,
                "email": email,
                "departmentId": department_id,
                "role": role,
                **kwargs,
            }
        )
        return {"id": "member-1", "organizationId": organization_id, "status": "invited"}

    async def update_member(self, member_id: str, *, organization_id: str, **updates: Any) -> dict[str, Any]:
        self.update_calls.append({"organizationId": organization_id, "memberId": member_id, **updates})
        return {"id": member_id, "organizationId": organization_id, **updates}


def _patch_member_write_routes(monkeypatch: pytest.MonkeyPatch, repository: _MemberRepository) -> None:
    async def no_csrf(_request: object) -> None:
        return None

    async def scoped_call(organization_id: str, method: str, *args: Any, **kwargs: Any) -> Any:
        function = getattr(repository, method)
        return await function(*args, organization_id=organization_id, **kwargs)

    async def customer_manager(_request: object) -> dict[str, Any]:
        return {
            "organizationMember": {
                "organizationId": "org-1",
                "status": "active",
                "role": "admin",
            }
        }

    async def platform_organization(_request: object, organization_id: str) -> dict[str, Any]:
        return {"id": "platform-1", "selectedOrganizationId": organization_id}

    async def auth_call(method: str, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        assert method == "get_user_by_email"
        return {
            "id": "auth-alice",
            "email": "alice@example.com",
            "status": "active",
            "identity_status": "verified",
        }

    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(main, "require_real_organization_capability", lambda: None)
    monkeypatch.setattr(main, "enforce_csrf", no_csrf)
    monkeypatch.setattr(main, "require_organization_demo_manager", customer_manager)
    monkeypatch.setattr(main, "require_platform_organization", platform_organization)
    monkeypatch.setattr(main, "organization_scoped_store_call", scoped_call)
    monkeypatch.setattr(main, "organization_store", lambda: repository)
    monkeypatch.setattr(main, "auth_store_call", auth_call)
    monkeypatch.setattr(main, "invalidate_organization_usage_cache", lambda: None)


@pytest.mark.parametrize("surface", ["customer", "platform"])
def test_member_invitation_carries_the_department_leader_role(
    monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    repository = _MemberRepository()
    _patch_member_write_routes(monkeypatch, repository)

    data = main.OrganizationMemberCreateRequest(
        name="Alice",
        email="alice@example.com",
        departmentId="dept-1",
        role="member",
        teamRole="leader",
    )
    if surface == "customer":
        asyncio.run(main.organization_create_member(data, object()))
    else:
        asyncio.run(main.platform_create_member("org-1", data, object()))

    assert repository.create_calls[0]["team_role"] == "leader"
    # 部门职务不得顺带放大企业角色：负责人默认仍是普通成员。
    assert repository.create_calls[0]["role"] == "member"


def test_member_invitation_defaults_to_a_plain_member(monkeypatch: pytest.MonkeyPatch) -> None:
    repository = _MemberRepository()
    _patch_member_write_routes(monkeypatch, repository)

    data = main.OrganizationMemberCreateRequest(
        name="Alice", email="alice@example.com", departmentId="dept-1"
    )
    asyncio.run(main.organization_create_member(data, object()))

    assert repository.create_calls[0]["team_role"] == "member"


@pytest.mark.parametrize("surface", ["customer", "platform"])
def test_member_update_can_promote_and_demote_the_department_leader(
    monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    repository = _MemberRepository()
    _patch_member_write_routes(monkeypatch, repository)

    for team_role in ("leader", "member"):
        data = main.OrganizationMemberUpdateRequest(teamRole=team_role)
        if surface == "customer":
            asyncio.run(main.organization_update_member("member-1", data, object()))
        else:
            asyncio.run(main.platform_update_member("org-1", "member-1", data, object()))

    assert [call["team_role"] for call in repository.update_calls] == ["leader", "member"]
    # 只提交职务时不得夹带其他字段，否则会把未填的姓名/部门清空。
    assert set(repository.update_calls[0]) == {"organizationId", "memberId", "team_role"}


def test_invalid_department_role_is_rejected() -> None:
    with pytest.raises(ValidationError):
        main.OrganizationMemberCreateRequest(
            name="Alice",
            email="alice@example.com",
            departmentId="dept-1",
            teamRole="admin",
        )
    with pytest.raises(ValidationError):
        main.OrganizationMemberUpdateRequest(teamRole="owner")


def _real_scope_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    team_role: str = "leader",
    member_status: str = "active",
    department: dict[str, Any] | None = None,
) -> list[tuple[str, tuple[Any, ...]]]:
    """真实模式下的最小成员目录替身，返回被调用的目录方法记录。"""

    calls: list[tuple[str, tuple[Any, ...]]] = []
    resolved_department = department if department is not None else {
        "id": "dept-1",
        "name": "研发部",
        "status": "active",
        "upstreamTeamId": "upstream-team-1",
        "activeMemberCount": 3,
    }

    async def memberships(_user: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "organizationId": "org-1",
                "status": member_status,
                "organizationStatus": "active",
                "role": "member",
                "teamRole": team_role,
                "departmentId": "dept-1",
            }
        ]

    async def scoped_call(organization_id: str, method: str, *args: Any, **_kwargs: Any) -> Any:
        calls.append((method, (organization_id, *args)))
        assert method == "get_department"
        return resolved_department

    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(main, "organization_enabled", lambda: True)
    monkeypatch.setattr(main, "organization_memberships_for_user", memberships)
    monkeypatch.setattr(main, "organization_scoped_store_call", scoped_call)
    monkeypatch.setattr(main, "usage_backend_ids", lambda: ["primary", "her"])
    return calls


def test_real_department_leader_gets_its_own_department_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    _real_scope_environment(monkeypatch)

    scope = asyncio.run(main.real_customer_team_scope({"id": "local-1", "email": "alice@example.com"}))

    assert scope["isTeamLeader"] is True
    assert scope["teamBoardStatus"] == "single"
    assert scope["team"]["name"] == "研发部"
    assert scope["team"]["teamRef"] == "real-org-1-dept-1"
    assert [(item["backend"], item["id"]) for item in scope["team"]["teamScopes"]] == [
        ("primary", "upstream-team-1"),
        ("her", "upstream-team-1"),
    ]
    assert scope["leaderTeams"] == [scope["team"]]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"team_role": "member"},
        {"member_status": "invited"},
        {"member_status": "suspended"},
        # 部门还没开通上游团队时没有可读的用量范围，入口先不出现。
        {"department": {"id": "dept-1", "name": "研发部", "status": "active", "upstreamTeamId": ""}},
        {"department": {"id": "dept-1", "name": "研发部", "status": "archived", "upstreamTeamId": "upstream-team-1"}},
        # 部门查不到时（已删除或目录不可读）同样不能给出看板。
        {"department": {}},
    ],
)
def test_real_team_scope_stays_empty_without_an_active_leader_department(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
) -> None:
    _real_scope_environment(monkeypatch, **kwargs)

    scope = asyncio.run(main.real_customer_team_scope({"id": "local-1", "email": "alice@example.com"}))

    assert scope == {"isTeamLeader": False, "teamBoardStatus": "none", "team": None, "leaderTeams": []}


def test_real_team_scope_is_empty_when_the_directory_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unavailable(_user: dict[str, Any]) -> list[dict[str, Any]]:
        raise main.HTTPException(status_code=503, detail="企业目录暂时不可用")

    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(main, "organization_memberships_for_user", unavailable)

    # 目录降级不能变成登录失败，只是看板入口暂时消失。
    scope = asyncio.run(main.real_customer_team_scope({"id": "local-1", "email": "alice@example.com"}))

    assert scope["isTeamLeader"] is False


def test_public_team_scope_hides_upstream_team_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    _real_scope_environment(monkeypatch)

    scope = asyncio.run(main.real_customer_team_scope({"id": "local-1", "email": "alice@example.com"}))
    public = main.public_team_scope(scope)

    assert public["team"]["teamRef"] == "real-org-1-dept-1"
    assert "teamScopes" not in public["team"]
    assert "backend" not in public["team"]
    assert all("teamScopes" not in team for team in public["leaderTeams"])


class _FakeUsageStore:
    def __init__(self) -> None:
        self.scope_items: list[Any] = []

    async def connect(self) -> None:
        return None

    async def team_rows(self, scope_items: Any, *_args: Any) -> dict[str, Any]:
        self.scope_items.append(scope_items)
        return {
            "rows": [],
            "summaryRows": [],
            "employees": [],
            "team": {"id": "dept-1", "name": "研发部", "memberCount": 3},
            "lastSyncedAt": None,
        }


def _local_leader_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, _FakeUsageStore]:
    store = AuthStore(tmp_path / "auth.sqlite3")
    user = store.create_user(
        "alice@example.com", "Alice", hash_password("password-123"), email_verified=True
    )
    store.set_provisioning_status(user["id"], "provisioned", "primary", f"local-{user['id']}")
    monkeypatch.setattr(main, "_auth_store", store)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("PASSWORD_LOGIN_ENABLED", "true")
    monkeypatch.setenv("APP_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("AUTH_ALLOWED_EMAIL_DOMAINS", "example.com")

    usage_store = _FakeUsageStore()
    monkeypatch.setattr(main, "usage_store", lambda: usage_store)

    async def no_refresh(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(main, "prepare_usage_refresh", no_refresh)
    main.team_auth_cache.clear()
    main.team_usage_cache.clear()
    main.team_member_usage_cache.clear()

    client = TestClient(main.app)
    csrf = client.get("/api/auth/csrf").json()["csrfToken"]
    logged_in = client.post(
        "/api/auth/login",
        json={"email": "alice@example.com", "password": "password-123"},
        headers={"X-CSRF-Token": csrf},
    )
    assert logged_in.status_code == 200
    return client, usage_store


def test_real_leader_sees_the_team_board_through_its_own_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, usage_store = _local_leader_client(tmp_path, monkeypatch)
    _real_scope_environment(monkeypatch)

    me = client.get("/api/auth/me")
    scope = client.get("/api/auth/scope")
    usage = client.get("/api/team/usage?start_date=2026-08-01&end_date=2026-08-05")

    assert me.status_code == 200
    # 看板入口在 /api/auth/me 就能定论，不必等 /api/auth/scope。
    assert me.json()["isTeamLeader"] is True
    assert me.json()["teamBoardStatus"] == "single"
    assert "teamScopes" not in me.json()["team"]
    assert scope.status_code == 200
    assert scope.json()["isTeamLeader"] is True
    assert scope.json()["team"]["teamRef"] == "real-org-1-dept-1"
    assert usage.status_code == 200
    assert usage.json()["team"]["teamRef"] == "real-org-1-dept-1"
    # 用量只覆盖本部门对应的上游团队。
    assert [(item["backend"], item["id"]) for item in usage_store.scope_items[0]] == [
        ("primary", "upstream-team-1"),
        ("her", "upstream-team-1"),
    ]


def test_real_leader_cannot_read_another_teams_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, usage_store = _local_leader_client(tmp_path, monkeypatch)
    _real_scope_environment(monkeypatch)

    forged = client.get("/api/team/usage?team_ref=real-org-2-dept-9")

    assert forged.status_code == 403
    assert usage_store.scope_items == []


def test_real_plain_member_still_has_no_team_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, usage_store = _local_leader_client(tmp_path, monkeypatch)
    _real_scope_environment(monkeypatch, team_role="member")

    me = client.get("/api/auth/me")
    usage = client.get("/api/team/usage")
    member_usage = client.get("/api/team/member/usage?employee=bob@example.com")

    assert me.json()["isTeamLeader"] is False
    assert me.json()["teamBoardStatus"] == "none"
    assert usage.status_code == 403
    assert member_usage.status_code == 403
    assert usage_store.scope_items == []


def test_local_account_without_real_mode_keeps_the_hard_team_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _local_leader_client(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "organization_real_enabled", lambda: False)

    async def must_not_resolve_same_email(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("local password account must not resolve the matching SSO identity")

    monkeypatch.setattr(main, "cached_resolve_user", must_not_resolve_same_email)

    usage = client.get("/api/team/usage")
    member_usage = client.get("/api/team/member/usage?employee=bob@example.com")

    for response in (usage, member_usage):
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "AUTH_TEAM_SCOPE_UNAVAILABLE"


def test_real_leader_scope_is_never_resolved_by_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """同邮箱的 SSO 主体不得继承客户负责人的看板范围。"""

    directory_calls = _real_scope_environment(monkeypatch)
    main.team_auth_cache.clear()

    async def fake_resolve_user(email: str, name: str | None = None, refresh: bool = False):
        return {
            "matched_user_ids": ["sso-alice"],
            "matched_accounts": [{"backend": "primary", "user_id": "sso-alice"}],
        }, {"hit": False, "ttlSeconds": 0}

    class _UpstreamOnlyClient:
        async def team_leader_scope(self, _upstream_user: dict[str, Any]) -> dict[str, Any]:
            return {"isTeamLeader": False, "teamBoardStatus": "none", "team": None, "leaderTeams": []}

    monkeypatch.setattr(main, "cached_resolve_user", fake_resolve_user)
    monkeypatch.setattr(main, "client", lambda: _UpstreamOnlyClient())

    # SSO 会话没有本地账号 id，团队范围只能来自上游团队角色。
    sso_scope = asyncio.run(main.team_scope_for_user({"email": "alice@example.com", "name": "Alice"}))

    assert sso_scope["isTeamLeader"] is False
    assert directory_calls == []


def test_member_modal_and_roster_expose_the_department_role() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    assert 'id="organizationMemberTeamRoleInput"' in markup
    assert '<option value="leader">部门负责人</option>' in markup
    assert "部门负责人可查看本部门的团队看板，但看不到企业全员数据。" in markup
    # 邀请与编辑共用一个弹窗，两个请求体都要带上职务。
    assert 'const teamRole = el("organizationMemberTeamRoleInput").value;' in source
    assert "{ name, email, departmentId, role, teamRole }" in source
    assert source.count("teamRole,\n") >= 1
    assert 'organizationField(member || {}, "teamRole", "team_role")' in source
    # 成员表已经有 6 列，职务只做部门名后的徽标。
    assert '<span class="organization-team-role">负责人</span>' in source
    assert ".organization-team-role {" in markup


def test_static_asset_version_is_refreshed_for_the_new_control() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")

    assert "/assets/app.js?v=20260812-observability-filters" in markup
    assert "20260805-team-member-keys" not in markup
    assert "20260805-owner-identity" not in markup
    assert "20260805-department-leader" not in markup
