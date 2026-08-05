"""「我的用量」必须覆盖成员名下的全部用量身份。

企业接入前产生的历史消费挂在用量身份（principal）上，而不是建档时生成的合成
upstream user id；同一个人的历史用量还常常分裂在多个 upstream user id 上。所以按
单个 id 查会查出 0 行，看起来像"这个人没有用量"。这组用例锁定聚合口径。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import HTTPException

from backend import main


class RecordingUsageStore:
    """Capture exactly which identities the payload asks the store for."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[dict[str, Any]] = []

    async def connect(self) -> None:
        return None

    async def organization_identity_rows(
        self,
        organization_id: str,
        upstream_user_ids: list[str],
        principal_ids: list[str],
        start_date: str,
        end_date: str,
        source: str,
        backend_ids: list[str],
    ) -> dict[str, Any] | None:
        self.calls.append(
            {
                "organizationId": organization_id,
                "upstreamUserIds": list(upstream_user_ids),
                "principalIds": list(principal_ids),
                "startDate": start_date,
                "endDate": end_date,
                "source": source,
            }
        )
        return {
            "rows": list(self.rows),
            "principalIds": list(principal_ids),
            "upstreamUserIds": list(upstream_user_ids),
            "lastSyncedAt": None,
        }


def membership(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "member-1",
        "organizationId": "org-baic",
        "organization": {
            "id": "org-baic",
            "name": "北汽集团",
            "upstreamOrganizationId": "org-upstream-baic",
        },
        "upstreamUserId": "customer-member-1",
        "principalIds": [],
    }
    base.update(overrides)
    return base


def usage_row(spend: float, source: str = "Claude Code") -> dict[str, Any]:
    return {
        "date": "2026-08-03",
        "source": source,
        "model": "claude-opus-5",
        "promptTokens": 10,
        "completionTokens": 20,
        "totalTokens": 30,
        "requestCount": 3,
        "successCount": 3,
        "failureCount": 0,
        "spend": spend,
    }


def member_usage(
    monkeypatch: pytest.MonkeyPatch,
    store: RecordingUsageStore | None,
    member: dict[str, Any],
) -> dict[str, Any]:
    monkeypatch.setattr(main, "require_real_organization_capability", lambda: None)
    monkeypatch.setattr(main, "usage_store", lambda: store)
    monkeypatch.setattr(main, "usage_backend_ids", lambda: ["primary"])

    async def no_refresh(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(main, "prepare_usage_refresh", no_refresh)
    return asyncio.run(
        main.real_organization_member_usage_payload(
            {"email": "davidzhu2021@example.invalid"},
            member,
            start_date="2026-07-30",
            end_date="2026-08-03",
            source="all",
        )
    )


def test_member_usage_queries_upstream_id_and_usage_identities_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RecordingUsageStore([usage_row(184.42), usage_row(49.95, "Cursor")])

    payload = member_usage(
        monkeypatch,
        store,
        membership(principalIds=["principal-lianghaiqiang"]),
    )

    assert store.calls == [
        {
            "organizationId": "org-upstream-baic",
            "upstreamUserIds": ["customer-member-1"],
            "principalIds": ["principal-lianghaiqiang"],
            "startDate": "2026-07-30",
            "endDate": "2026-08-03",
            "source": "all",
        }
    ]
    assert payload["dataQuality"]["memberIdentityMatch"] == "principal"
    assert payload["summary"]["rangeTotal"]["spend"] == pytest.approx(234.37)
    assert {row["source"] for row in payload["rows"]} == {"Claude Code", "Cursor"}


def test_member_usage_without_usage_identity_still_reads_its_upstream_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RecordingUsageStore([usage_row(12.5)])

    payload = member_usage(monkeypatch, store, membership())

    assert store.calls[0]["upstreamUserIds"] == ["customer-member-1"]
    assert store.calls[0]["principalIds"] == []
    assert payload["dataQuality"]["memberIdentityMatch"] == "upstream_user_id"


def test_member_usage_ignores_blank_usage_identity_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RecordingUsageStore()

    member_usage(
        monkeypatch,
        store,
        membership(principalIds=["  ", "principal-1", ""]),
    )

    assert store.calls[0]["principalIds"] == ["principal-1"]


def test_member_usage_reads_identities_when_the_upstream_id_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成员还没拿到 upstream id，但历史用量身份已经挂好时不能报"开通中"。"""

    store = RecordingUsageStore([usage_row(20.0)])

    payload = member_usage(
        monkeypatch,
        store,
        membership(upstreamUserId="", principalIds=["principal-1"]),
    )

    assert store.calls[0]["upstreamUserIds"] == []
    assert store.calls[0]["principalIds"] == ["principal-1"]
    assert payload["dataQuality"]["memberIdentityMatch"] == "principal"


def test_member_usage_reports_provisioning_when_no_identity_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RecordingUsageStore()

    with pytest.raises(HTTPException) as error:
        member_usage(
            monkeypatch,
            store,
            membership(upstreamUserId="", principalIds=[]),
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "ORGANIZATION_MEMBER_PROVISIONING"
    assert store.calls == []


class RecordingOrganizationStore:
    """Answer membership lookups the way the real repository does."""

    def __init__(self, memberships: list[dict[str, Any]]) -> None:
        self.memberships = memberships

    async def resolve_members_by_auth_user_id(self, auth_user_id: str) -> list[dict[str, Any]]:
        return [
            {
                "organizationId": item.get("organizationId"),
                "organization": item.get("organization") or {"status": "active"},
                "member": item,
            }
            for item in self.memberships
        ]


def entitlement_for(
    monkeypatch: pytest.MonkeyPatch, memberships: list[dict[str, Any]]
) -> str:
    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(main, "require_real_organization_capability", lambda: None)
    monkeypatch.setattr(
        main, "organization_store", lambda: RecordingOrganizationStore(memberships)
    )
    return asyncio.run(main.member_account_entitlement_status({"id": "auth-user-1"}))


def member_record(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "member-1",
        "status": "active",
        "organizationId": "org-baic",
        "organization": {"id": "org-baic", "status": "active"},
        "upstreamUserId": "customer-member-1",
        "principalIds": [],
    }
    base.update(overrides)
    return base


def test_entitlement_follows_an_active_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    assert entitlement_for(monkeypatch, [member_record()]) == "active"


def test_entitlement_accepts_a_member_carrying_only_usage_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成员只挂着历史用量身份时也算已开通，否则界面会一直停在"等待开通"。"""

    member = member_record(upstreamUserId="", principalIds=["principal-lianghaiqiang"])

    assert entitlement_for(monkeypatch, [member]) == "active"


def test_entitlement_stays_inactive_without_any_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = member_record(upstreamUserId="", principalIds=[])

    assert entitlement_for(monkeypatch, [member]) == "inactive"


def test_entitlement_ignores_an_invited_or_suspended_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert entitlement_for(monkeypatch, [member_record(status="invited")]) == "inactive"
    assert entitlement_for(monkeypatch, [member_record(status="suspended")]) == "inactive"


def test_entitlement_ignores_a_member_of_a_suspended_customer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member = member_record(organization={"id": "org-baic", "status": "suspended"})

    assert entitlement_for(monkeypatch, [member]) == "inactive"


def test_entitlement_is_inactive_when_real_mode_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "organization_real_enabled", lambda: False)

    assert asyncio.run(main.member_account_entitlement_status({"id": "auth-user-1"})) == "inactive"
