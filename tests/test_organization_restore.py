"""解除归档：只允许 archived → active，且不能顺带"复活"令牌或挂起状态。"""

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


CSRF_TOKEN = "organization-restore-csrf"
PLATFORM_EMAIL = "platform-admin@example.test"
ORGANIZATION_ID = "org-harbor"


def signed_session(payload: dict[str, Any]) -> str:
    data = base64.b64encode(json.dumps(payload).encode("utf-8"))
    secret = os.getenv("SESSION_SECRET", "dev-session-secret-change-me")
    return TimestampSigner(secret).sign(data).decode("utf-8")


def demo_client(
    monkeypatch,
    *,
    email: str = PLATFORM_EMAIL,
    platform_admin: bool = True,
    store: InMemoryOrganizationStore | None = None,
) -> TestClient:
    monkeypatch.setenv("ORGANIZATION_DEMO_ENABLED", "true")
    monkeypatch.setenv("ADMIN_EMAILS", PLATFORM_EMAIL)
    monkeypatch.setattr(main, "_organization_store", store or InMemoryOrganizationStore())
    client = TestClient(main.app, raise_server_exceptions=False)
    client.cookies.set(
        main.SESSION_COOKIE_NAME,
        signed_session(
            {
                SESSION_USER_KEY: {
                    "email": email,
                    "name": "Restore Tester",
                    "avatar": "R",
                    "department": "Platform",
                    "isAdmin": platform_admin,
                },
                CSRF_SESSION_KEY: CSRF_TOKEN,
            }
        ),
    )
    return client


def csrf_headers() -> dict[str, str]:
    return {"X-CSRF-Token": CSRF_TOKEN}


def error_code(response) -> str:
    return response.json()["detail"]["code"]


def restore_path(organization_id: str = ORGANIZATION_ID) -> str:
    return f"/api/platform/organizations/{organization_id}/restore"


def test_archived_customer_can_be_restored_and_regains_access(monkeypatch) -> None:
    store = InMemoryOrganizationStore()
    platform = demo_client(monkeypatch, store=store)
    customer = demo_client(
        monkeypatch,
        email="lan.xu@harbor.example",
        platform_admin=False,
        store=store,
    )

    archived = platform.post(
        f"/api/platform/organizations/{ORGANIZATION_ID}/archive",
        json={},
        headers=csrf_headers(),
    )
    blocked = customer.get("/api/organization/current")
    restored = platform.post(restore_path(), json={}, headers=csrf_headers())
    detail = platform.get(f"/api/platform/organizations/{ORGANIZATION_ID}")
    recovered = customer.get("/api/organization/current")

    assert archived.status_code == 200
    assert archived.json()["organization"]["status"] == "archived"
    assert blocked.status_code == 403
    assert restored.status_code == 200
    assert restored.json()["organization"]["status"] == "active"
    # 归档时间必须清空，否则详情页会同时显示"正常"和一个归档日期。
    assert restored.json()["organization"]["archivedAt"] is None
    assert detail.json()["organization"]["status"] == "active"
    assert recovered.status_code == 200


def test_active_customer_cannot_be_restored(monkeypatch) -> None:
    client = demo_client(monkeypatch)

    response = client.post(restore_path(), json={}, headers=csrf_headers())

    assert response.status_code == 409
    assert error_code(response) == "ORGANIZATION_NOT_ARCHIVED"


def test_restore_never_clears_a_deliberate_suspension(monkeypatch) -> None:
    """挂起是运营的独立决定，恢复归档不能顺手把它一起解掉。"""

    store = InMemoryOrganizationStore()
    store._organizations[ORGANIZATION_ID].organization["status"] = "suspended"
    client = demo_client(monkeypatch, store=store)

    response = client.post(restore_path(), json={}, headers=csrf_headers())

    assert response.status_code == 409
    assert error_code(response) == "ORGANIZATION_NOT_ARCHIVED"
    assert store._organizations[ORGANIZATION_ID].organization["status"] == "suspended"


def test_restore_requires_csrf_and_platform_admin(monkeypatch) -> None:
    store = InMemoryOrganizationStore()
    platform = demo_client(monkeypatch, store=store)
    platform.post(
        f"/api/platform/organizations/{ORGANIZATION_ID}/archive",
        json={},
        headers=csrf_headers(),
    )
    member = demo_client(
        monkeypatch,
        email="lan.xu@harbor.example",
        platform_admin=False,
        store=store,
    )

    without_csrf = platform.post(restore_path(), json={})
    as_member = member.post(restore_path(), json={}, headers=csrf_headers())

    assert without_csrf.status_code == 403
    assert as_member.status_code == 403
    assert store._organizations[ORGANIZATION_ID].organization["status"] == "archived"


def test_unknown_customer_restore_reports_not_found(monkeypatch) -> None:
    client = demo_client(monkeypatch)

    response = client.post(restore_path("org-missing"), json={}, headers=csrf_headers())

    assert response.status_code == 404
    assert error_code(response) == "ORGANIZATION_NOT_FOUND"


# ----------------------------------------------------------------------
# Real repository
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


class _RestoreConnection:
    """只回应 restore_organization 真正会发出的三条语句。"""

    def __init__(self, *, status: str = "archived", exists: bool = True) -> None:
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)
        self.exists = exists
        self.row = {
            "id": "org-baic",
            "name": "北汽集团",
            "status": status,
            "upstream_organization_id": "org-baic-research-institute",
            "upstream_status": "active",
            "created_at": now,
            "updated_at": now,
            "archived_at": now,
        }
        self.updates = 0
        self.outbox: list[tuple[str, str, dict[str, Any]]] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetchrow(self, query: str, *_args: Any) -> dict[str, Any] | None:
        if "SELECT status FROM customer_organization" in query:
            assert "FOR UPDATE" in query, "状态判定必须锁行，否则并发归档会被覆盖"
            return {"status": self.row["status"]} if self.exists else None
        if "UPDATE customer_organization SET status='active'" in query:
            assert "archived_at=NULL" in query
            self.updates += 1
            self.row.update(status="active", archived_at=None)
            return dict(self.row)
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def fetch(self, query: str, *_args: Any) -> list[dict[str, Any]]:
        raise AssertionError(f"unexpected fetch: {query}")

    async def execute(self, query: str, *args: Any) -> str:
        if "INSERT INTO customer_outbox" in query:
            self.outbox.append((str(args[1]), str(args[2]), json.loads(args[3])))
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute: {query}")


class _RestorePool:
    def __init__(self, connection: _RestoreConnection) -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


def restore_with(connection: _RestoreConnection) -> dict[str, Any]:
    repository = PostgreSQLOrganizationRepository("postgresql://organization-restore-tests")
    repository.pool = _RestorePool(connection)
    return asyncio.run(repository.restore_organization("org-baic"))


def test_repository_restore_pushes_active_upstream_without_reviving_tokens() -> None:
    connection = _RestoreConnection()

    organization = restore_with(connection)

    assert organization["status"] == "active"
    assert organization["archivedAt"] is None
    assert connection.updates == 1
    kinds = [kind for kind, _aggregate, _payload in connection.outbox]
    # 归档把令牌在上游真实吊销了，恢复只能回推企业状态；再排一次
    # 令牌任务只会重复吊销，不会把令牌变回可用。
    assert kinds == ["organization.sync"]
    payload = connection.outbox[0][2]
    assert payload["status"] == "active"
    assert payload["upstreamOrganizationId"] == "org-baic-research-institute"


def test_repository_restore_rejects_a_customer_that_is_not_archived() -> None:
    connection = _RestoreConnection(status="active")

    with pytest.raises(OrganizationConflictError, match="not archived"):
        restore_with(connection)

    assert connection.updates == 0
    assert connection.outbox == []


def test_repository_restore_reports_a_missing_customer() -> None:
    connection = _RestoreConnection(exists=False)

    with pytest.raises(OrganizationNotFoundError):
        restore_with(connection)

    assert connection.updates == 0


# ----------------------------------------------------------------------
# Audit trail
# ----------------------------------------------------------------------


class _AuditRepository(PostgreSQLOrganizationRepository):
    """real 模式下的最小仓储替身，只关心审计是否落库。"""

    def __init__(self) -> None:
        super().__init__("postgresql://organization-restore-audit")
        self.audits: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def get_organization(self, organization_id: str) -> dict[str, Any]:
        return {"id": organization_id, "name": "北汽集团", "status": "archived"}

    async def restore_organization(self, organization_id: str) -> dict[str, Any]:
        return {"id": organization_id, "name": "北汽集团", "status": "active", "archivedAt": None}

    async def record_audit(self, *args: Any, **kwargs: Any) -> None:
        self.audits.append((args, kwargs))


def test_restore_is_recorded_in_the_audit_log(monkeypatch) -> None:
    repository = _AuditRepository()
    monkeypatch.setenv("ORGANIZATION_MODE", "real")
    monkeypatch.setenv("ADMIN_EMAILS", PLATFORM_EMAIL)
    monkeypatch.setattr(main, "_organization_store", repository)
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
                    "email": PLATFORM_EMAIL,
                    "name": "Restore Tester",
                    "avatar": "R",
                    "department": "Platform",
                    "isAdmin": True,
                },
                CSRF_SESSION_KEY: CSRF_TOKEN,
            }
        ),
    )

    response = client.post(restore_path("org-baic"), json={}, headers=csrf_headers())

    assert response.status_code == 200
    assert response.json()["organization"]["status"] == "active"
    assert len(repository.audits) == 1
    args, kwargs = repository.audits[0]
    assert args[0] == "org-baic"
    assert args[1] == "organization.restored"
    assert kwargs["actor"] == PLATFORM_EMAIL
    assert kwargs["target_id"] == "org-baic"
    assert kwargs["details"]["toStatus"] == "active"
