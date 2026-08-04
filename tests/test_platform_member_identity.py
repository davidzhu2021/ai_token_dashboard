"""平台管理员的身份绑定：登录账号可换绑，用量身份可关联到成员。

成员的登录邮箱会变（临时测试邮箱换成正式邮箱），历史用量又挂在用量身份上而不是
成员建档时生成的合成上游 ID。所以这两件事都必须是可反复执行的界面操作，而且只能
由平台管理员做 —— 让客户管理员任意换绑登录账号等于交出账号接管能力。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from backend import main
from backend.auth import CSRF_SESSION_KEY, SESSION_USER_KEY
from backend.organization_repository import PostgreSQLOrganizationRepository
from backend.organization_store import InMemoryOrganizationStore
from backend.organization_validation import (
    OrganizationConflictError,
    OrganizationNotFoundError,
)


CSRF_TOKEN = "member-identity-csrf"
PLATFORM_EMAIL = "platform-admin@example.test"
ORGANIZATION_ID = "org-baic"
MEMBER_ID = "member-lianghaiqiang"
PRINCIPAL_ID = "principal-lianghaiqiang"


def signed_session(payload: dict[str, Any]) -> str:
    data = base64.b64encode(json.dumps(payload).encode("utf-8"))
    secret = os.getenv("SESSION_SECRET", "dev-session-secret-change-me")
    return TimestampSigner(secret).sign(data).decode("utf-8")


def csrf_headers() -> dict[str, str]:
    return {"X-CSRF-Token": CSRF_TOKEN}


def error_code(response) -> str:
    return response.json()["detail"]["code"]


def identity_path(member_id: str = MEMBER_ID) -> str:
    return f"/api/platform/organizations/{ORGANIZATION_ID}/members/{member_id}/identity"


def account_path(member_id: str = MEMBER_ID) -> str:
    return f"/api/platform/organizations/{ORGANIZATION_ID}/members/{member_id}/account"


def principal_path(principal_id: str = PRINCIPAL_ID) -> str:
    return f"/api/platform/organizations/{ORGANIZATION_ID}/principals/{principal_id}/member"


def member_path(member_id: str = MEMBER_ID) -> str:
    return f"/api/platform/organizations/{ORGANIZATION_ID}/members/{member_id}"


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------


class FakeAuthStore:
    """只回应身份绑定路由用到的三个本地账号操作。"""

    def __init__(self, accounts: list[dict[str, Any]] | None = None) -> None:
        self.accounts = accounts if accounts is not None else [
            {
                "id": "auth-david",
                "email": "davidzhu2021@163.com",
                "login_name": "davidzhu",
                "status": "active",
            }
        ]
        self.revoked: list[str] = []

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        for account in self.accounts:
            if account["id"] == user_id:
                return dict(account)
        return None

    def get_user_by_identifier(self, identifier: str) -> dict[str, Any] | None:
        needle = identifier.strip().casefold()
        for account in self.accounts:
            if needle in {
                str(account.get("email") or "").casefold(),
                str(account.get("login_name") or "").casefold(),
            }:
                return dict(account)
        return None

    def revoke_user_sessions(self, user_id: str) -> int:
        self.revoked.append(user_id)
        return 1


class IdentityRepository(PostgreSQLOrganizationRepository):
    """real 模式下的最小目录替身，记录绑定调用与审计。"""

    def __init__(
        self,
        *,
        auth_user_id: str = "",
        principal_member_id: str = "",
        account_conflict: bool = False,
        login_name_conflict: bool = False,
    ) -> None:
        super().__init__("postgresql://member-identity-tests")
        self.auth_user_id = auth_user_id
        self.principal_member_id = principal_member_id
        self.account_conflict = account_conflict
        self.login_name_conflict = login_name_conflict
        self.account_calls: list[tuple[str, str, str | None]] = []
        self.principal_calls: list[tuple[str, str, str | None]] = []
        self.member_updates: list[dict[str, Any]] = []
        self.audits: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    # --- reads -------------------------------------------------------
    async def get_organization(self, organization_id: str) -> dict[str, Any]:
        return {"id": organization_id, "name": "北汽集团", "status": "active"}

    def member_payload(self) -> dict[str, Any]:
        return {
            "id": MEMBER_ID,
            "name": "梁海强",
            "email": "",
            "loginName": "lianghaiqiang",
            "departmentId": "dept-1",
            "role": "member",
            "status": "active",
            "authUserId": self.auth_user_id or None,
            "upstreamUserId": f"customer-{MEMBER_ID}",
        }

    async def get_member(
        self, member_id: str, *, organization_id: str = ""
    ) -> dict[str, Any] | None:
        if member_id != MEMBER_ID:
            return None
        return self.member_payload()

    def principal_payload(self) -> dict[str, Any]:
        return {
            "id": PRINCIPAL_ID,
            "organizationId": ORGANIZATION_ID,
            "name": "梁海强",
            "status": "active" if self.principal_member_id else "pending",
            "memberId": self.principal_member_id,
            "upstreamUserIds": ["unknown", "cursor-lianghaiqiang"],
        }

    async def get_principal(
        self, organization_id: str, principal_id: str
    ) -> dict[str, Any] | None:
        if principal_id != PRINCIPAL_ID:
            return None
        return self.principal_payload()

    async def list_principals(self, organization_id: str) -> dict[str, Any]:
        payload = self.principal_payload()
        payload["memberName"] = "梁海强" if self.principal_member_id else ""
        return {"items": [payload], "total": 1}

    # --- writes ------------------------------------------------------
    async def set_member_account(
        self, organization_id: str, member_id: str, auth_user_id: str | None
    ) -> dict[str, Any]:
        self.account_calls.append((organization_id, member_id, auth_user_id))
        if self.account_conflict:
            raise OrganizationConflictError("account is already bound to another member")
        if member_id != MEMBER_ID:
            raise OrganizationNotFoundError("member was not found")
        previous = self.auth_user_id
        self.auth_user_id = str(auth_user_id or "")
        member = self.member_payload()
        member["previousAuthUserId"] = previous
        return member

    async def set_principal_member(
        self, organization_id: str, principal_id: str, member_id: str | None
    ) -> dict[str, Any]:
        self.principal_calls.append((organization_id, principal_id, member_id))
        if principal_id != PRINCIPAL_ID:
            raise OrganizationNotFoundError("principal was not found")
        self.principal_member_id = str(member_id or "")
        return self.principal_payload()

    async def update_member(self, member_id: str, **updates: Any) -> dict[str, Any]:
        self.member_updates.append({"memberId": member_id, **updates})
        if self.login_name_conflict:
            raise OrganizationConflictError("login_name is already in use")
        member = self.member_payload()
        if "login_name" in updates:
            member["loginName"] = updates["login_name"]
        return member

    async def record_audit(self, *args: Any, **kwargs: Any) -> None:
        self.audits.append((args, kwargs))

    def audit_actions(self) -> list[str]:
        return [str(args[1]) for args, _kwargs in self.audits]


def real_client(
    monkeypatch,
    repository: IdentityRepository,
    auth: FakeAuthStore | None = None,
    *,
    email: str = PLATFORM_EMAIL,
    platform_admin: bool = True,
) -> TestClient:
    monkeypatch.setenv("ORGANIZATION_MODE", "real")
    monkeypatch.setenv("ADMIN_EMAILS", PLATFORM_EMAIL)
    monkeypatch.setattr(main, "_organization_store", repository)
    monkeypatch.setattr(main, "_auth_store", auth or FakeAuthStore())
    monkeypatch.setattr(
        main,
        "_organization_capability_status",
        {"mode": "real", "status": "ready", "available": True, "lastCheckedAt": None},
    )
    client = TestClient(main.app, raise_server_exceptions=False)
    client.cookies.set(
        main.SESSION_COOKIE_NAME,
        signed_session(
            {
                SESSION_USER_KEY: {
                    "email": email,
                    "name": "Identity Tester",
                    "avatar": "I",
                    "department": "Platform",
                    "isAdmin": platform_admin,
                },
                CSRF_SESSION_KEY: CSRF_TOKEN,
            }
        ),
    )
    return client


# ----------------------------------------------------------------------
# 用量身份
# ----------------------------------------------------------------------


def test_usage_identity_can_be_linked_to_a_member(monkeypatch) -> None:
    repository = IdentityRepository()
    client = real_client(monkeypatch, repository)

    response = client.post(
        principal_path(), json={"memberId": MEMBER_ID}, headers=csrf_headers()
    )

    assert response.status_code == 200
    body = response.json()
    assert body["principal"]["memberId"] == MEMBER_ID
    assert body["principals"]["items"][0]["memberName"] == "梁海强"
    assert repository.principal_calls == [(ORGANIZATION_ID, PRINCIPAL_ID, MEMBER_ID)]
    assert repository.audit_actions() == ["organization.principal.bound"]
    details = repository.audits[0][1]["details"]
    assert details["memberId"] == MEMBER_ID
    assert details["previousMemberId"] == ""


def test_usage_identity_rebind_records_the_previous_member(monkeypatch) -> None:
    repository = IdentityRepository(principal_member_id="member-old")
    client = real_client(monkeypatch, repository)

    response = client.post(
        principal_path(), json={"memberId": MEMBER_ID}, headers=csrf_headers()
    )

    assert response.status_code == 200
    assert repository.audits[0][1]["details"]["previousMemberId"] == "member-old"


def test_usage_identity_can_be_released(monkeypatch) -> None:
    repository = IdentityRepository(principal_member_id=MEMBER_ID)
    client = real_client(monkeypatch, repository)

    response = client.post(principal_path(), json={"memberId": ""}, headers=csrf_headers())

    assert response.status_code == 200
    assert response.json()["principal"]["memberId"] == ""
    # 空串是"解绑"意图，必须以 NULL 落库，而不是写一个空字符串成员 ID。
    assert repository.principal_calls == [(ORGANIZATION_ID, PRINCIPAL_ID, None)]
    assert repository.audit_actions() == ["organization.principal.unbound"]


def test_unknown_usage_identity_reports_not_found(monkeypatch) -> None:
    repository = IdentityRepository()
    client = real_client(monkeypatch, repository)

    response = client.post(
        principal_path("principal-missing"),
        json={"memberId": MEMBER_ID},
        headers=csrf_headers(),
    )

    assert response.status_code == 404
    assert repository.audits == []


def test_usage_identity_binding_requires_csrf_and_platform_admin(monkeypatch) -> None:
    repository = IdentityRepository()
    client = real_client(monkeypatch, repository)
    customer = real_client(
        monkeypatch,
        repository,
        email="lianghaiqiang@baic.example",
        platform_admin=False,
    )

    without_csrf = client.post(principal_path(), json={"memberId": MEMBER_ID})
    as_customer = customer.post(
        principal_path(), json={"memberId": MEMBER_ID}, headers=csrf_headers()
    )

    assert without_csrf.status_code == 403
    assert as_customer.status_code == 403
    assert repository.principal_calls == []


# ----------------------------------------------------------------------
# 登录账号
# ----------------------------------------------------------------------


def test_login_account_can_be_bound_by_email(monkeypatch) -> None:
    repository = IdentityRepository()
    auth = FakeAuthStore()
    client = real_client(monkeypatch, repository, auth)

    response = client.post(
        account_path(), json={"identifier": "davidzhu2021@163.com"}, headers=csrf_headers()
    )

    assert response.status_code == 200
    body = response.json()
    assert body["account"]["email"] == "davidzhu2021@163.com"
    assert body["account"]["loginName"] == "davidzhu"
    assert body["accountMissing"] is False
    assert repository.account_calls == [(ORGANIZATION_ID, MEMBER_ID, "auth-david")]
    assert repository.audit_actions() == ["organization.member.account_bound"]
    # 首次绑定没有旧账号，不能凭空吊销别人的会话。
    assert auth.revoked == []


def test_login_account_rebind_revokes_the_replaced_sessions(monkeypatch) -> None:
    """换邮箱之后旧账号的浏览器不能继续读这家企业的数据。"""

    repository = IdentityRepository(auth_user_id="auth-david")
    auth = FakeAuthStore(
        [
            {
                "id": "auth-david",
                "email": "davidzhu2021@163.com",
                "login_name": "davidzhu",
                "status": "active",
            },
            {
                "id": "auth-real",
                "email": "lianghaiqiang@baic.example",
                "login_name": "lianghaiqiang",
                "status": "active",
            },
        ]
    )
    client = real_client(monkeypatch, repository, auth)

    response = client.post(
        account_path(),
        json={"identifier": "lianghaiqiang@baic.example"},
        headers=csrf_headers(),
    )

    assert response.status_code == 200
    assert response.json()["account"]["id"] == "auth-real"
    assert auth.revoked == ["auth-david"]
    details = repository.audits[0][1]["details"]
    assert details["authUserId"] == "auth-real"
    assert details["previousAuthUserId"] == "auth-david"
    # 审计里不能出现口令或令牌，只记身份 ID 与来源 IP。
    assert set(details) == {"memberId", "authUserId", "previousAuthUserId", "ipAddress"}


def test_login_account_can_be_released(monkeypatch) -> None:
    repository = IdentityRepository(auth_user_id="auth-david")
    auth = FakeAuthStore()
    client = real_client(monkeypatch, repository, auth)

    response = client.post(account_path(), json={"identifier": ""}, headers=csrf_headers())

    assert response.status_code == 200
    assert response.json()["account"] is None
    assert repository.account_calls == [(ORGANIZATION_ID, MEMBER_ID, "")]
    assert repository.audit_actions() == ["organization.member.account_unbound"]
    assert auth.revoked == ["auth-david"]


def test_binding_an_unknown_login_account_is_refused(monkeypatch) -> None:
    repository = IdentityRepository()
    client = real_client(monkeypatch, repository)

    response = client.post(
        account_path(), json={"identifier": "nobody@example.test"}, headers=csrf_headers()
    )

    assert response.status_code == 404
    assert error_code(response) == "ORGANIZATION_ACCOUNT_NOT_FOUND"
    assert repository.account_calls == []


def test_binding_a_disabled_login_account_is_refused(monkeypatch) -> None:
    repository = IdentityRepository()
    auth = FakeAuthStore(
        [
            {
                "id": "auth-suspended",
                "email": "left@baic.example",
                "login_name": "left",
                "status": "suspended",
            }
        ]
    )
    client = real_client(monkeypatch, repository, auth)

    response = client.post(
        account_path(), json={"identifier": "left@baic.example"}, headers=csrf_headers()
    )

    assert response.status_code == 409
    assert error_code(response) == "ORGANIZATION_ACCOUNT_NOT_ACTIVE"
    assert repository.account_calls == []


def test_login_account_already_bound_elsewhere_reports_a_clear_conflict(monkeypatch) -> None:
    repository = IdentityRepository(account_conflict=True)
    client = real_client(monkeypatch, repository)

    response = client.post(
        account_path(), json={"identifier": "davidzhu2021@163.com"}, headers=csrf_headers()
    )

    assert response.status_code == 409
    assert error_code(response) == "ORGANIZATION_ACCOUNT_ALREADY_BOUND"
    assert repository.audits == []


def test_login_account_binding_requires_csrf_and_platform_admin(monkeypatch) -> None:
    repository = IdentityRepository()
    client = real_client(monkeypatch, repository)
    customer = real_client(
        monkeypatch,
        repository,
        email="lianghaiqiang@baic.example",
        platform_admin=False,
    )

    without_csrf = client.post(account_path(), json={"identifier": "davidzhu2021@163.com"})
    as_customer = customer.post(
        account_path(), json={"identifier": "davidzhu2021@163.com"}, headers=csrf_headers()
    )

    assert without_csrf.status_code == 403
    assert as_customer.status_code == 403
    assert repository.account_calls == []


# ----------------------------------------------------------------------
# 身份概览与登录名
# ----------------------------------------------------------------------


def test_identity_overview_lists_the_account_and_every_usage_identity(monkeypatch) -> None:
    repository = IdentityRepository(auth_user_id="auth-david")
    client = real_client(monkeypatch, repository)

    response = client.get(identity_path())

    assert response.status_code == 200
    body = response.json()
    assert body["member"]["id"] == MEMBER_ID
    assert body["account"]["email"] == "davidzhu2021@163.com"
    assert body["accountMissing"] is False
    # 浏览器侧只看到中性的「历史来源」，不带上游字段名。
    assert body["principals"]["items"][0]["historySources"] == [
        "unknown",
        "cursor-lianghaiqiang",
    ]
    assert "upstreamUserIds" not in body["principals"]["items"][0]


def test_identity_overview_flags_a_binding_whose_account_is_gone(monkeypatch) -> None:
    """账号被删掉后如果只显示"未绑定"，运营就解释不了这个人为什么没有用量。"""

    repository = IdentityRepository(auth_user_id="auth-deleted")
    client = real_client(monkeypatch, repository)

    response = client.get(identity_path())

    assert response.status_code == 200
    assert response.json()["account"] is None
    assert response.json()["accountMissing"] is True


def test_identity_overview_reports_a_missing_member(monkeypatch) -> None:
    repository = IdentityRepository()
    client = real_client(monkeypatch, repository)

    response = client.get(identity_path("member-missing"))

    assert response.status_code == 404
    assert error_code(response) == "ORGANIZATION_MEMBER_NOT_FOUND"


def test_platform_admin_can_change_a_managed_login_name(monkeypatch) -> None:
    repository = IdentityRepository()
    client = real_client(monkeypatch, repository)

    response = client.patch(
        member_path(), json={"loginName": "lianghaiqiang"}, headers=csrf_headers()
    )

    assert response.status_code == 200
    assert response.json()["member"]["loginName"] == "lianghaiqiang"
    assert repository.member_updates == [
        {
            "memberId": MEMBER_ID,
            "login_name": "lianghaiqiang",
            "organization_id": ORGANIZATION_ID,
        }
    ]


def test_duplicate_login_name_reports_a_specific_conflict(monkeypatch) -> None:
    repository = IdentityRepository(login_name_conflict=True)
    client = real_client(monkeypatch, repository)

    response = client.patch(
        member_path(), json={"loginName": "lianghaiqiang"}, headers=csrf_headers()
    )

    assert response.status_code == 409
    assert error_code(response) == "ORGANIZATION_LOGIN_NAME_TAKEN"


def test_customer_admins_cannot_change_a_login_name(monkeypatch) -> None:
    """改别人的登录名等于账号接管能力，客户侧路由必须忽略这个字段。"""

    repository = IdentityRepository()
    real_client(monkeypatch, repository)
    client = TestClient(main.app, raise_server_exceptions=False)
    client.cookies.set(
        main.SESSION_COOKIE_NAME,
        signed_session(
            {
                SESSION_USER_KEY: {
                    "email": "boss@baic.example",
                    "name": "Customer Admin",
                    "avatar": "C",
                    "department": "IT",
                    "isAdmin": False,
                },
                CSRF_SESSION_KEY: CSRF_TOKEN,
            }
        ),
    )

    response = client.patch(
        f"/api/organization/current/members/{MEMBER_ID}",
        json={"loginName": "hijacked"},
        headers=csrf_headers(),
    )

    assert response.status_code in {401, 403}
    assert repository.member_updates == []


# ----------------------------------------------------------------------
# Mock 目录
# ----------------------------------------------------------------------


def demo_client(monkeypatch) -> TestClient:
    monkeypatch.setenv("ORGANIZATION_DEMO_ENABLED", "true")
    monkeypatch.setenv("ADMIN_EMAILS", PLATFORM_EMAIL)
    monkeypatch.setattr(main, "_organization_store", InMemoryOrganizationStore())
    client = TestClient(main.app, raise_server_exceptions=False)
    client.cookies.set(
        main.SESSION_COOKIE_NAME,
        signed_session(
            {
                SESSION_USER_KEY: {
                    "email": PLATFORM_EMAIL,
                    "name": "Identity Tester",
                    "avatar": "I",
                    "department": "Platform",
                    "isAdmin": True,
                },
                CSRF_SESSION_KEY: CSRF_TOKEN,
            }
        ),
    )
    return client


def test_demo_directory_refuses_identity_binding(monkeypatch) -> None:
    """绑定是长期记录，重启就消失的假绑定比一句明确的拒绝更糟。"""

    client = demo_client(monkeypatch)

    overview = client.get("/api/platform/organizations/org-harbor/members/member-1/identity")
    bind_account = client.post(
        "/api/platform/organizations/org-harbor/members/member-1/account",
        json={"identifier": "davidzhu2021@163.com"},
        headers=csrf_headers(),
    )
    bind_principal = client.post(
        "/api/platform/organizations/org-harbor/principals/principal-1/member",
        json={"memberId": "member-1"},
        headers=csrf_headers(),
    )

    for response in (overview, bind_account, bind_principal):
        assert response.status_code == 503
        assert error_code(response) == "ORGANIZATION_IDENTITY_UNAVAILABLE"


# ----------------------------------------------------------------------
# Repository SQL
# ----------------------------------------------------------------------


class _Acquire:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    async def __aenter__(self) -> Any:
        return self.connection

    async def __aexit__(self, *_args: Any) -> bool:
        return False


class _Transaction:
    async def __aenter__(self) -> "_Transaction":
        return self

    async def __aexit__(self, *_args: Any) -> bool:
        return False


class _Pool:
    """只暴露 acquire：成员载荷因此不会去 JOIN 部门表。"""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


NOW = datetime(2026, 8, 4, tzinfo=timezone.utc)


class _PrincipalConnection:
    def __init__(self, *, status: str = "pending", member_id: str | None = None) -> None:
        self.row = {
            "id": PRINCIPAL_ID,
            "organization_id": ORGANIZATION_ID,
            "name": "梁海强",
            "status": status,
            "member_id": member_id,
            "created_at": NOW,
            "updated_at": NOW,
        }
        self.member_locks: list[str] = []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.missing_member = False

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "FROM customer_principal WHERE" in query and "FOR UPDATE" in query:
            return dict(self.row)
        if "FROM customer_member WHERE" in query:
            assert "FOR UPDATE" in query, "目标成员必须锁行，否则并发删除会留下悬空绑定"
            self.member_locks.append(str(args[1]))
            return None if self.missing_member else {"id": args[1]}
        if "FROM customer_principal WHERE" in query:
            return dict(self.row)
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def fetch(self, query: str, *_args: Any) -> list[dict[str, Any]]:
        if "customer_principal_upstream_identity" in query:
            return [{"upstream_user_id": "unknown"}, {"upstream_user_id": "cursor-lianghaiqiang"}]
        raise AssertionError(f"unexpected fetch: {query}")

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        if "UPDATE customer_principal SET member_id" in query:
            self.row["member_id"] = args[2]
            if args[2] is not None and self.row["status"] == "pending":
                self.row["status"] = "active"
            return "UPDATE 1"
        if "UPDATE customer_usage_key_identity SET member_id" in query:
            return "UPDATE 2"
        raise AssertionError(f"unexpected execute: {query}")


def principal_repository(connection: _PrincipalConnection) -> PostgreSQLOrganizationRepository:
    repository = PostgreSQLOrganizationRepository("postgresql://member-identity-sql")
    repository.pool = _Pool(connection)
    return repository


def test_repository_binding_moves_imported_key_ownership_to_the_new_member() -> None:
    """改绑后不能有残留行还指向旧成员，否则资产列表两边都对不上。"""

    connection = _PrincipalConnection(status="active", member_id="member-old")
    repository = principal_repository(connection)

    principal = asyncio.run(
        repository.set_principal_member(ORGANIZATION_ID, PRINCIPAL_ID, MEMBER_ID)
    )

    assert principal["memberId"] == MEMBER_ID
    assert connection.member_locks == [MEMBER_ID]
    keys = [
        (query, args)
        for query, args in connection.executed
        if "customer_usage_key_identity" in query
    ]
    assert len(keys) == 1
    assert "member_id IS NULL" not in keys[0][0]
    assert keys[0][1] == (ORGANIZATION_ID, PRINCIPAL_ID, MEMBER_ID)


def test_repository_binding_activates_a_pending_identity() -> None:
    connection = _PrincipalConnection(status="pending")
    repository = principal_repository(connection)

    principal = asyncio.run(
        repository.set_principal_member(ORGANIZATION_ID, PRINCIPAL_ID, MEMBER_ID)
    )

    assert principal["status"] == "active"


def test_repository_release_clears_the_member_without_touching_status() -> None:
    connection = _PrincipalConnection(status="active", member_id=MEMBER_ID)
    repository = principal_repository(connection)

    principal = asyncio.run(
        repository.set_principal_member(ORGANIZATION_ID, PRINCIPAL_ID, "")
    )

    assert principal["memberId"] == ""
    assert principal["status"] == "active"
    # 解绑时不去锁成员表，也不能保留旧的密钥归属。
    assert connection.member_locks == []
    keys = [args for query, args in connection.executed if "customer_usage_key_identity" in query]
    assert keys == [(ORGANIZATION_ID, PRINCIPAL_ID, None)]


def test_repository_binding_rejects_a_member_from_another_customer() -> None:
    connection = _PrincipalConnection()
    connection.missing_member = True
    repository = principal_repository(connection)

    with pytest.raises(OrganizationNotFoundError):
        asyncio.run(repository.set_principal_member(ORGANIZATION_ID, PRINCIPAL_ID, "member-x"))

    assert connection.executed == []


class _AccountConnection:
    def __init__(self, *, previous: str = "", duplicate: bool = False, exists: bool = True) -> None:
        self.previous = previous
        self.duplicate = duplicate
        self.exists = exists
        self.updates: list[tuple[Any, ...]] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "SELECT auth_user_id FROM customer_member" in query:
            assert "FOR UPDATE" in query, "读旧绑定必须锁行，否则并发换绑会丢会话吊销"
            # status 过滤会让已激活成员改不了登录账号，这正是本次要放开的点。
            assert "status" not in query
            return {"auth_user_id": self.previous} if self.exists else None
        if "UPDATE customer_member SET auth_user_id" in query:
            if self.duplicate:
                raise RuntimeError(
                    'duplicate key value violates unique constraint "customer_member_auth_user_idx"'
                )
            self.updates.append(args)
            return {
                "id": MEMBER_ID,
                "name": "梁海强",
                "email": "",
                "login_name": "lianghaiqiang",
                "department_id": "dept-1",
                "department_name": "研发",
                "role": "member",
                "status": "active",
                "team_role": "member",
                "auth_user_id": args[2],
                "upstream_user_id": f"customer-{MEMBER_ID}",
                "created_at": NOW,
                "updated_at": NOW,
            }
        raise AssertionError(f"unexpected fetchrow: {query}")


def account_repository(connection: _AccountConnection) -> PostgreSQLOrganizationRepository:
    repository = PostgreSQLOrganizationRepository("postgresql://member-identity-account")
    repository.pool = _Pool(connection)
    return repository


def test_repository_account_rebind_returns_the_replaced_binding() -> None:
    connection = _AccountConnection(previous="auth-david")
    repository = account_repository(connection)

    member = asyncio.run(
        repository.set_member_account(ORGANIZATION_ID, MEMBER_ID, "auth-real")
    )

    assert member["authUserId"] == "auth-real"
    # 路由靠这个值吊销旧账号的会话，丢了它旧浏览器就还能读数据。
    assert member["previousAuthUserId"] == "auth-david"
    assert connection.updates == [(MEMBER_ID, ORGANIZATION_ID, "auth-real")]


def test_repository_account_release_writes_an_empty_binding() -> None:
    connection = _AccountConnection(previous="auth-david")
    repository = account_repository(connection)

    member = asyncio.run(repository.set_member_account(ORGANIZATION_ID, MEMBER_ID, None))

    assert member["authUserId"] is None
    assert connection.updates == [(MEMBER_ID, ORGANIZATION_ID, "")]


def test_repository_account_conflict_is_reported_as_a_conflict() -> None:
    connection = _AccountConnection(duplicate=True)
    repository = account_repository(connection)

    with pytest.raises(OrganizationConflictError, match="already bound"):
        asyncio.run(repository.set_member_account(ORGANIZATION_ID, MEMBER_ID, "auth-taken"))


def test_repository_account_reports_a_missing_member() -> None:
    connection = _AccountConnection(exists=False)
    repository = account_repository(connection)

    with pytest.raises(OrganizationNotFoundError):
        asyncio.run(repository.set_member_account(ORGANIZATION_ID, MEMBER_ID, "auth-david"))

    assert connection.updates == []
