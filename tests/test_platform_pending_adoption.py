"""待接管企业列表：只读、可降级，且绝不泄露上游原始记录。"""

from __future__ import annotations

import base64
import json
import os
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from backend import main
from backend.auth import CSRF_SESSION_KEY, SESSION_USER_KEY
from backend.organization_repository import PostgreSQLOrganizationRepository


PLATFORM_EMAIL = "platform-admin@example.test"
CSRF_TOKEN = "pending-adoption-csrf"

BAIC_ID = "org-baic-research-institute"
SHANGHAI_ID = "org-shanghai-chelian"
CHERY_ID = "org-chery"
WUXI_ID = "org-wuxi-chelian"


def upstream_records() -> list[dict[str, Any]]:
    """上游返回的原始形状，含绝不能透传到浏览器的内部字段。"""

    return [
        {
            "organization_id": SHANGHAI_ID,
            "organization_alias": "上海车联",
            "spend": 3610.1449,
            "created_at": "2026-03-01T00:00:00Z",
            "members": [{"user_id": f"user-{index}"} for index in range(232)],
            "teams": [{"team_id": "team-shanghai"}],
            "object_permission": {"vector_stores": ["internal"]},
        },
        {
            "organization_id": CHERY_ID,
            "organization_alias": "奇瑞",
            "spend": 92.114,
            "created_at": "2026-05-06T00:00:00Z",
            "members": [{"user_id": "user-a"}, {"user_id": "user-b"}],
            "teams": [{"team_id": "team-chery"}],
            "object_permission": {},
        },
        {
            "organization_id": BAIC_ID,
            "organization_alias": "北汽集团",
            "spend": 0,
            "created_at": "2026-07-20T00:00:00Z",
            "members": [{"user_id": "user-c"}, {"user_id": "user-d"}],
            "teams": [{"team_id": "team-8656ed00614014a1"}],
        },
        {
            "organization_id": WUXI_ID,
            "organization_alias": "无锡车联",
            "spend": 807264.93,
            "created_at": "2025-11-02T00:00:00Z",
            "members": [{"user_id": f"seller-{index}"} for index in range(847)],
            "teams": [{"team_id": f"team-{index}"} for index in range(80)],
        },
    ]


class FakeUpstream:
    """只实现待接管列表用到的上游读接口，并记录调用次数。"""

    def __init__(self, records: list[dict[str, Any]] | Exception) -> None:
        self.records = records
        self.calls = 0
        self.backends = [SimpleNamespace(id="primary"), SimpleNamespace(id="her")]

    async def list_organizations(self, *, backend: Any = None, **_kwargs: Any) -> list[dict[str, Any]]:
        self.calls += 1
        assert backend is not None and backend.id == "primary"
        if isinstance(self.records, Exception):
            raise self.records
        return self.records


class FakeRepository(PostgreSQLOrganizationRepository):
    """继承真实仓储类型，让 real 模式的类型判断按生产路径走。"""

    def __init__(
        self,
        *,
        adopted: set[str],
        directory: dict[str, Any] | None = None,
        adopted_error: Exception | None = None,
    ) -> None:
        super().__init__("postgresql://organization-tests")
        self.adopted = adopted
        self.directory = directory or {"items": [], "total": 0, "page": 1, "pageSize": 20}
        self.adopted_error = adopted_error

    async def adopted_upstream_organization_ids(self) -> set[str]:
        if self.adopted_error is not None:
            raise self.adopted_error
        return set(self.adopted)

    async def list_organizations(self, **_kwargs: Any) -> dict[str, Any]:
        return self.directory


def signed_session(payload: dict[str, Any]) -> str:
    data = base64.b64encode(json.dumps(payload).encode("utf-8"))
    secret = os.getenv("SESSION_SECRET", "dev-session-secret-change-me")
    return TimestampSigner(secret).sign(data).decode("utf-8")


def platform_client(
    monkeypatch,
    *,
    mode: str = "real",
    repository: Any = None,
    upstream: FakeUpstream | None = None,
    internal_ids: str = "",
    email: str = PLATFORM_EMAIL,
    platform_admin: bool = True,
) -> TestClient:
    monkeypatch.setenv("ORGANIZATION_MODE", mode)
    monkeypatch.setenv("ADMIN_EMAILS", PLATFORM_EMAIL)
    monkeypatch.setenv("ORGANIZATION_INTERNAL_UPSTREAM_IDS", internal_ids)
    monkeypatch.setenv("ORGANIZATION_ADOPTION_BACKEND_ID", "primary")
    monkeypatch.setattr(main, "_organization_store", repository)
    monkeypatch.setattr(
        main,
        "_organization_capability_status",
        {"mode": mode, "status": "ready", "available": True, "lastCheckedAt": None},
    )
    if upstream is not None:
        monkeypatch.setattr(main, "client", lambda: upstream)
    main.pending_adoption_cache.clear()
    client = TestClient(main.app, raise_server_exceptions=False)
    client.cookies.set(
        main.SESSION_COOKIE_NAME,
        signed_session(
            {
                SESSION_USER_KEY: {
                    "email": email,
                    "name": "Pending Adoption Tester",
                    "avatar": "P",
                    "department": "Platform",
                    "isAdmin": platform_admin,
                },
                CSRF_SESSION_KEY: CSRF_TOKEN,
            }
        ),
    )
    return client


def test_only_unadopted_customer_companies_are_listed(monkeypatch) -> None:
    upstream = FakeUpstream(upstream_records())
    client = platform_client(
        monkeypatch,
        repository=FakeRepository(adopted={BAIC_ID}),
        upstream=upstream,
        internal_ids=WUXI_ID,
    )

    response = client.get("/api/platform/organizations")

    assert response.status_code == 200
    pending = response.json()["pendingAdoption"]
    assert pending["unavailable"] is False
    # 已建档的北汽和自家主体无锡车联都不该出现，剩下两家按消费从高到低排。
    assert [item["upstreamId"] for item in pending["items"]] == [SHANGHAI_ID, CHERY_ID]
    assert pending["items"][0]["name"] == "上海车联"
    assert pending["items"][0]["memberCount"] == 232
    assert pending["items"][0]["teamCount"] == 1
    assert pending["items"][0]["spendUsd"] == 3610.14
    assert upstream.calls == 1


def test_pending_items_never_carry_raw_upstream_fields(monkeypatch) -> None:
    """上游记录里有成员数组和权限对象，投影必须只保留可展示的计数字段。"""

    client = platform_client(
        monkeypatch,
        repository=FakeRepository(adopted={BAIC_ID}),
        upstream=FakeUpstream(upstream_records()),
        internal_ids=WUXI_ID,
    )

    response = client.get("/api/platform/organizations")

    assert response.status_code == 200
    items = response.json()["pendingAdoption"]["items"]
    assert items
    for item in items:
        assert set(item) == {
            "upstreamId",
            "name",
            "memberCount",
            "teamCount",
            "spendUsd",
            "createdAt",
        }
    body = response.text
    assert "object_permission" not in body
    assert "user_id" not in body


def test_upstream_failure_degrades_without_breaking_the_directory(monkeypatch) -> None:
    directory = {
        "items": [{"id": "local-baic", "name": "北汽集团", "status": "active"}],
        "total": 1,
        "page": 1,
        "pageSize": 20,
    }
    client = platform_client(
        monkeypatch,
        repository=FakeRepository(adopted={BAIC_ID}, directory=directory),
        upstream=FakeUpstream(RuntimeError("upstream unavailable")),
    )

    response = client.get("/api/platform/organizations")

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"] == directory["items"]
    assert payload["pendingAdoption"] == {"items": [], "unavailable": True}


def test_repository_failure_degrades_without_breaking_the_directory(monkeypatch) -> None:
    upstream = FakeUpstream(upstream_records())
    client = platform_client(
        monkeypatch,
        repository=FakeRepository(
            adopted=set(), adopted_error=RuntimeError("database unavailable")
        ),
        upstream=upstream,
    )

    response = client.get("/api/platform/organizations")

    assert response.status_code == 200
    assert response.json()["pendingAdoption"] == {"items": [], "unavailable": True}
    # 不知道谁已建档时不能猜，宁可不展示也不能把已接管企业当成候选。
    assert upstream.calls == 0


def test_archived_customer_is_not_offered_for_adoption_again(monkeypatch) -> None:
    """归档企业仍然是已接管企业，不能重新出现在待接管列表里。"""

    directory = {
        "items": [{"id": "local-baic", "name": "北汽集团", "status": "archived"}],
        "total": 1,
        "page": 1,
        "pageSize": 20,
    }
    client = platform_client(
        monkeypatch,
        repository=FakeRepository(adopted={BAIC_ID}, directory=directory),
        upstream=FakeUpstream(upstream_records()),
        internal_ids=WUXI_ID,
    )

    response = client.get("/api/platform/organizations")

    assert response.status_code == 200
    upstream_ids = {item["upstreamId"] for item in response.json()["pendingAdoption"]["items"]}
    assert BAIC_ID not in upstream_ids
    assert upstream_ids == {SHANGHAI_ID, CHERY_ID}


def test_demo_mode_never_reads_upstream_companies(monkeypatch) -> None:
    upstream = FakeUpstream(upstream_records())
    client = platform_client(monkeypatch, mode="demo", upstream=upstream)

    response = client.get("/api/platform/organizations")

    assert response.status_code == 200
    assert response.json()["pendingAdoption"] == {"items": [], "unavailable": False}
    assert upstream.calls == 0


def test_filtered_and_paged_requests_skip_the_candidate_lookup(monkeypatch) -> None:
    upstream = FakeUpstream(upstream_records())
    client = platform_client(
        monkeypatch,
        repository=FakeRepository(adopted={BAIC_ID}),
        upstream=upstream,
        internal_ids=WUXI_ID,
    )

    searched = client.get("/api/platform/organizations?search=%E8%BD%A6%E8%81%94")
    filtered = client.get("/api/platform/organizations?status=archived")
    paged = client.get("/api/platform/organizations?page=2")

    for response in (searched, filtered, paged):
        assert response.status_code == 200
        assert "pendingAdoption" not in response.json()
    assert upstream.calls == 0


def test_candidate_list_is_cached_until_adoption_changes(monkeypatch) -> None:
    upstream = FakeUpstream(upstream_records())
    repository = FakeRepository(adopted={BAIC_ID})
    client = platform_client(
        monkeypatch,
        repository=repository,
        upstream=upstream,
        internal_ids=WUXI_ID,
    )

    first = client.get("/api/platform/organizations")
    cached = client.get("/api/platform/organizations")
    assert upstream.calls == 1
    assert first.json()["pendingAdoption"] == cached.json()["pendingAdoption"]

    # 刚接管完的企业必须立刻从候选里消失，不能等缓存过期。
    repository.adopted = {BAIC_ID, CHERY_ID}
    after_adoption = client.get("/api/platform/organizations")

    assert upstream.calls == 2
    assert [item["upstreamId"] for item in after_adoption.json()["pendingAdoption"]["items"]] == [
        SHANGHAI_ID
    ]


def test_non_platform_admin_cannot_see_candidates(monkeypatch) -> None:
    upstream = FakeUpstream(upstream_records())
    client = platform_client(
        monkeypatch,
        repository=FakeRepository(adopted={BAIC_ID}),
        upstream=upstream,
        email="member@example.test",
        platform_admin=False,
    )

    response = client.get("/api/platform/organizations")

    assert response.status_code == 403
    assert upstream.calls == 0
