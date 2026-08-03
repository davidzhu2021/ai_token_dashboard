"""Regression contracts for unfinished real-organization behavior."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import main
from backend.organization_repository import PostgreSQLOrganizationRepository


def test_secret_bearing_spa_url_disables_cache_and_referrers() -> None:
    response = TestClient(main.app).get(
        "/?organization_claim=one-time-membership-claim-secret"
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_membership_claim_issue_response_disables_cache_and_referrers() -> None:
    async def call_middleware() -> dict[str, str]:
        from starlette.requests import Request
        from starlette.responses import Response

        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "https",
                "path": "/api/platform/organizations/org-baic/membership-claims",
                "raw_path": b"/api/platform/organizations/org-baic/membership-claims",
                "query_string": b"",
                "headers": [(b"host", b"example.invalid")],
                "client": ("127.0.0.1", 1234),
                "server": ("example.invalid", 443),
            }
        )
        response = await main.protect_secret_bearing_urls(
            request,
            lambda _request: app_response(Response),
        )
        return dict(response.headers)

    async def app_response(response_class: Any) -> Any:
        return response_class(
            '{"activationUrl":"secret"}', media_type="application/json"
        )

    headers = asyncio.run(call_middleware())
    assert headers["cache-control"] == "no-store"
    assert headers["pragma"] == "no-cache"
    assert headers["referrer-policy"] == "no-referrer"


def test_demo_mode_requires_loopback_app_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORGANIZATION_MODE", "demo")
    monkeypatch.setenv("APP_BASE_URL", "https://dashboard.example.com")

    with pytest.raises(RuntimeError, match="ORGANIZATION_MODE=demo"):
        main.validate_runtime_auth_config()


def test_demo_mode_allows_loopback_app_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORGANIZATION_MODE", "demo")
    monkeypatch.setenv("APP_BASE_URL", "http://127.0.0.1:8000")

    main.validate_runtime_auth_config()


class _AtomicMemberRepository:
    def __init__(self) -> None:
        self.atomic_calls: list[tuple[Any, ...]] = []
        self.legacy_create_calls: list[tuple[Any, ...]] = []
        self.invitation_calls: list[tuple[Any, ...]] = []

    async def create_member_with_invitation(
        self,
        name: str,
        email: str,
        department_id: str,
        role: str = "member",
        *,
        organization_id: str,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.atomic_calls.append(
            (organization_id, name, email, department_id, role)
        )
        return {
            "id": "member-1",
            "organizationId": organization_id,
            "status": "invited",
        }

    async def create_member(
        self,
        name: str,
        email: str,
        department_id: str,
        role: str = "member",
        *,
        organization_id: str,
    ) -> dict[str, Any]:
        self.legacy_create_calls.append(
            (organization_id, name, email, department_id, role)
        )
        return {
            "id": "member-1",
            "organizationId": organization_id,
            "status": "invited",
        }

    async def create_invitation(
        self, organization_id: str, member_id: str
    ) -> dict[str, Any]:
        self.invitation_calls.append((organization_id, member_id))
        return {"id": "invitation-1", "memberId": member_id}


@pytest.mark.parametrize("surface", ["customer", "platform"])
def test_real_member_create_uses_one_atomic_member_and_invitation_operation(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    repository = _AtomicMemberRepository()

    async def no_csrf(_request: object) -> None:
        return None

    async def scoped_call(
        organization_id: str, method: str, *args: Any, **kwargs: Any
    ) -> Any:
        function = getattr(repository, method)
        return await function(*args, organization_id=organization_id, **kwargs)

    async def store_call(method: str, *args: Any, **kwargs: Any) -> Any:
        organization_id = str(kwargs.pop("organization_id", "org-1"))
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

    async def platform_organization(
        _request: object, organization_id: str
    ) -> dict[str, Any]:
        return {
            "id": "platform-1",
            "selectedOrganizationId": organization_id,
        }

    async def auth_call(method: str, *args: Any, **_kwargs: Any) -> dict[str, Any]:
        assert method == "get_user_by_email"
        assert args == ("alice@example.com",)
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
    monkeypatch.setattr(main, "organization_store_call", store_call)
    monkeypatch.setattr(main, "organization_store", lambda: repository)
    monkeypatch.setattr(main, "auth_store_call", auth_call)
    # The current routes use this class for their real-store isinstance guard.
    monkeypatch.setattr(main, "PostgreSQLOrganizationRepository", _AtomicMemberRepository)

    request = object()
    data = main.OrganizationMemberCreateRequest(
        name="Alice",
        email="alice@example.com",
        departmentId="dept-1",
        role="member",
    )
    if surface == "customer":
        response = asyncio.run(main.organization_create_member(data, request))
    else:
        response = asyncio.run(
            main.platform_create_member("org-1", data, request)
        )

    assert response["member"]["status"] == "invited"
    assert repository.atomic_calls == [
        ("org-1", "Alice", "alice@example.com", "dept-1", "member")
    ]
    assert repository.legacy_create_calls == []
    assert repository.invitation_calls == []


def test_platform_real_organization_requires_an_existing_verified_first_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_csrf(_request: object) -> None:
        return None

    async def auth_call(method: str, *_args: Any, **_kwargs: Any) -> None:
        assert method == "get_user_by_email"
        return None

    async def unexpected_store_call(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an unknown email must not reserve an enterprise organization")

    monkeypatch.setattr(main, "organization_enabled", lambda: True)
    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(main, "enforce_csrf", no_csrf)
    monkeypatch.setattr(main, "require_platform_admin", lambda _request: {"id": "platform"})
    monkeypatch.setattr(main, "auth_store_call", auth_call)
    monkeypatch.setattr(main, "platform_organization_store_call", unexpected_store_call)

    data = main.PlatformOrganizationCreateRequest(
        name="北汽集团",
        adminName="David Zhu",
        adminEmail="davidzhu2021@163.com",
    )
    with pytest.raises(HTTPException) as raised:
        asyncio.run(main.platform_create_organization(data, object()))

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "ORGANIZATION_ADMIN_ACCOUNT_NOT_FOUND"


class _AdoptionRepository:
    def __init__(self, *, replay: bool = False) -> None:
        self.replay = replay
        self.imports: list[dict[str, Any]] = []
        self.attachments: list[dict[str, str]] = []
        self.completed: list[tuple[str, str, dict[str, Any]]] = []

    async def begin_adoption_operation(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"id": "operation-1", "status": "applying", "organizationId": ""}

    async def get_adoption_operation(self, _operation_key: str) -> dict[str, Any] | None:
        if not self.replay:
            return None
        request = _adoption_request()
        public_request = request.model_dump(
            exclude={"previewFingerprint", "idempotencyKey"}, mode="json"
        )
        import hashlib
        import json

        return {
            "id": "operation-1",
            "status": "applied",
            "organizationId": "org-local",
            "requestFingerprint": hashlib.sha256(
                json.dumps(
                    public_request,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "previewFingerprint": "f" * 64,
            "result": {"ok": True, "status": "applied", "replayed": True},
        }

    async def get_organization_by_upstream_id(self, _upstream_id: str) -> None:
        return None

    async def get_department_by_upstream_id(self, _upstream_id: str) -> None:
        return None

    async def list_organizations(self, **_kwargs: Any) -> dict[str, Any]:
        return {"items": []}

    async def create_organization_with_admin(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "organization": {"id": "org-local"},
            "department": {"id": "dept-local"},
            "admin": {"id": "member-david"},
        }

    async def adopt_existing_upstream_scope(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    async def list_members(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "items": [
                {
                    "id": "member-david",
                    "email": "davidzhu2021@163.com",
                    "status": "invited",
                }
            ]
        }

    async def ensure_member_invitation(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"id": "invitation-david"}

    async def ensure_principal(self, organization_id: str, name: str) -> dict[str, Any]:
        assert (organization_id, name) == ("org-local", "梁海强")
        return {"id": "principal-lianghaiqiang", "name": name}

    async def attach_principal_upstream_identity(self, principal_id: str, **kwargs: Any) -> dict[str, Any]:
        self.attachments.append({"principalId": principal_id, **kwargs})
        return {"id": principal_id}

    async def import_report_only_key_identity(self, organization_id: str, **kwargs: Any) -> dict[str, Any]:
        self.imports.append({"organizationId": organization_id, **kwargs})
        return {"id": f"import-{len(self.imports)}", "billingEligible": False}

    async def ensure_usage_backfill(self, organization_id: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "id": f"backfill-{kwargs['usage_key_identity_id']}",
            "organizationId": organization_id,
            "status": "pending",
        }

    async def get_organization(self, organization_id: str) -> dict[str, Any]:
        return {"id": organization_id, "name": "北汽集团"}

    async def record_audit(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def complete_adoption_operation(self, operation_id: str, organization_id: str, result: dict[str, Any]) -> None:
        self.completed.append((operation_id, organization_id, result))

    async def fail_adoption_operation(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("successful adoption must not be marked failed")


class _AdoptionUpstream:
    backends = [type("Backend", (), {"id": "primary"})()]

    async def list_keys_exact(self, *, key_alias: str, backend: Any) -> list[dict[str, Any]]:
        assert backend.id == "primary"
        user_id = "claude-user" if key_alias.startswith("claude") else "cursor-user"
        key_hash = "a" * 64 if key_alias.startswith("claude") else "b" * 64
        return [{"alias": key_alias, "hash": key_hash, "userId": user_id}]

    @staticmethod
    def report_only_key_identity(record: dict[str, Any]) -> dict[str, str]:
        return {
            "hash": str(record["hash"]),
            "organizationId": "org-upstream",
            "teamId": "team-upstream",
            "userId": str(record["userId"]),
        }


def _adoption_preview() -> dict[str, Any]:
    assets = [
        {"alias": "claude-code-lianghaiqiang", "keyHash": "a" * 64, "userId": "claude-user"},
        {"alias": "cursor-lianghaiqiang", "keyHash": "b" * 64, "userId": "cursor-user"},
    ]
    return {
        "previewFingerprint": "f" * 64,
        "_apply": {
            "backendId": "primary",
            "upstreamOrganizationId": "org-upstream",
            "upstreamTeamId": "team-upstream",
            "assets": assets,
        },
    }


def _adoption_request() -> main.OrganizationAdoptionApplyRequest:
    return main.OrganizationAdoptionApplyRequest(
        organizationName="北汽集团",
        departmentName="企业管理",
        adminName="David Zhu",
        adminEmail="davidzhu2021@163.com",
        principalName="梁海强",
        effectiveFrom="2026-01-01",
        effectiveThrough="2026-07-31",
        previewFingerprint="f" * 64,
        idempotencyKey="baic-pilot-adoption-v1",
    )


def test_adoption_imports_legacy_keys_to_principal_not_david(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _AdoptionRepository()
    upstream = _AdoptionUpstream()

    async def no_csrf(_request: object) -> None:
        return None

    async def preview(_data: Any) -> dict[str, Any]:
        return _adoption_preview()

    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(main, "require_real_organization_capability", lambda: None)
    monkeypatch.setattr(main, "enforce_csrf", no_csrf)
    monkeypatch.setattr(main, "require_platform_admin", lambda _request: {"id": "platform"})
    monkeypatch.setattr(main, "organization_adoption_preview_payload", preview)
    monkeypatch.setattr(main, "organization_store", lambda: repository)
    monkeypatch.setattr(main, "PostgreSQLOrganizationRepository", _AdoptionRepository)
    monkeypatch.setattr(main, "client", lambda: upstream)

    result = asyncio.run(
        main.platform_organization_adoption_apply(_adoption_request(), object())
    )

    assert result["status"] == "applied"
    assert len(repository.imports) == 2
    assert {item["principal_id"] for item in repository.imports} == {
        "principal-lianghaiqiang"
    }
    assert {item["member_id"] for item in repository.imports} == {""}
    assert {item["upstream_user_id"] for item in repository.attachments} == {
        "claude-user",
        "cursor-user",
    }
    assert repository.completed[0][1] == "org-local"


def test_adoption_replay_returns_saved_result_without_upstream_or_local_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _AdoptionRepository(replay=True)

    async def no_csrf(_request: object) -> None:
        return None

    async def preview(_data: Any) -> dict[str, Any]:
        return _adoption_preview()

    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(main, "require_real_organization_capability", lambda: None)
    monkeypatch.setattr(main, "enforce_csrf", no_csrf)
    monkeypatch.setattr(main, "require_platform_admin", lambda _request: {"id": "platform"})
    monkeypatch.setattr(main, "organization_adoption_preview_payload", preview)
    monkeypatch.setattr(main, "organization_store", lambda: repository)
    monkeypatch.setattr(main, "PostgreSQLOrganizationRepository", _AdoptionRepository)
    monkeypatch.setattr(main, "client", lambda: _AdoptionUpstream())

    result = asyncio.run(
        main.platform_organization_adoption_apply(_adoption_request(), object())
    )

    assert result == {"ok": True, "status": "applied", "replayed": True}
    assert repository.imports == []
    assert repository.attachments == []


class _BaicCreditRepository:
    def __init__(self, member_status: str = "active") -> None:
        self.member_status = member_status
        self.adjustments: list[dict[str, Any]] = []

    async def list_organizations(self, **_kwargs: Any) -> dict[str, Any]:
        return {"items": [{"id": "org-baic", "name": "北汽集团"}]}

    async def list_members(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "items": [
                {
                    "id": "member-david",
                    "email": "davidzhu2021@163.com",
                    "role": "admin",
                    "status": self.member_status,
                    "upstreamUserId": "user-david" if self.member_status == "active" else "",
                }
            ]
        }

    async def get_organization(self, organization_id: str) -> dict[str, Any]:
        assert organization_id == "org-baic"
        return {
            "id": organization_id,
            "name": "北汽集团",
            "upstreamOrganizationId": "org-upstream",
        }

    async def list_departments(self, **kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs.get("organization_id") == "org-baic"
        return [
            {
                "id": "dept-management",
                "name": "企业管理",
                "upstreamTeamId": "team-upstream",
            }
        ]

    async def adjust_billing(self, organization_id: str, **kwargs: Any) -> dict[str, Any]:
        self.adjustments.append({"organizationId": organization_id, **kwargs})
        return {"id": "credit-1"}


def test_baic_initial_credit_waits_for_active_david_and_uses_stable_idempotency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _BaicCreditRepository(member_status="active")
    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(main, "organization_store", lambda: repository)
    monkeypatch.setattr(main, "PostgreSQLOrganizationRepository", _BaicCreditRepository)
    monkeypatch.setenv(
        "ORGANIZATION_ADOPTION_KEY_ALIASES",
        "claude-code-lianghaiqiang,cursor-lianghaiqiang",
    )
    monkeypatch.setattr(main, "client", lambda: _AdoptionUpstream())

    assert asyncio.run(main.reconcile_baic_pilot_credit()) is True
    assert repository.adjustments == [
        {
            "organizationId": "org-baic",
            "operation": "grant",
            "amount_usd": "5000.00",
            "reason": "北汽集团试点初始授信",
            "operator": "baic-pilot-reconciler",
            "operator_email": "",
            "external_reference": "BAIC-PILOT-INITIAL-5000",
            "idempotency_key": "baic-pilot-initial-credit-v1",
        }
    ]


def test_baic_initial_credit_is_not_granted_while_david_is_invited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _BaicCreditRepository(member_status="invited")
    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(main, "organization_store", lambda: repository)
    monkeypatch.setattr(main, "PostgreSQLOrganizationRepository", _BaicCreditRepository)

    assert asyncio.run(main.reconcile_baic_pilot_credit()) is False
    assert repository.adjustments == []


def test_real_claim_accept_requires_turnstile_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(main, "password_login_configured", lambda: True)
    monkeypatch.setattr(main, "turnstile_enabled", lambda: False)
    monkeypatch.setattr(main, "turnstile_configured", lambda: False)
    data = main.OrganizationClaimAcceptRequest(
        token="x" * 32,
        password="password-123",
        turnstileToken="",
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(main.accept_organization_claim(data, object()))

    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "ORGANIZATION_CLAIM_TURNSTILE_REQUIRED"


class _ClaimReconcileRepository:
    def __init__(self) -> None:
        self.member = {
            "id": "member-liang",
            "status": "invited",
            "organizationId": "org-baic",
        }
        self.linked: list[tuple[str, str, str]] = []

    async def resolve_members_by_auth_user_id(self, _auth_user_id: str) -> list[dict[str, Any]]:
        return [] if self.member["status"] == "invited" else [
            {"organizationId": "org-baic", "member": dict(self.member)}
        ]

    async def create_managed_member(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return dict(self.member)

    async def ensure_principal(self, _organization_id: str, _name: str) -> dict[str, str]:
        return {"id": "principal-liang"}

    async def link_principal_member(self, organization_id: str, principal_id: str, member_id: str) -> None:
        self.linked.append((organization_id, principal_id, member_id))


def test_approved_claim_is_resumed_and_marked_provisioning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _ClaimReconcileRepository()
    calls: list[tuple[str, tuple[Any, ...]]] = []
    claim = {
        "id": "claim-1",
        "status": "approved",
        "authUserId": "auth-liang",
        "organizationId": "org-baic",
        "departmentId": "dept-management",
        "memberName": "梁海强",
        "loginName": "lianghaiqiang",
        "role": "admin",
    }

    async def auth_call(method: str, *args: Any, **_kwargs: Any) -> Any:
        calls.append((method, args))
        if method == "list_membership_claims":
            return [claim]
        return claim

    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(main, "organization_store", lambda: repository)
    monkeypatch.setattr(main, "PostgreSQLOrganizationRepository", _ClaimReconcileRepository)
    monkeypatch.setattr(main, "auth_store_call", auth_call)

    assert asyncio.run(main.reconcile_active_membership_claims()) == 0
    assert any(method == "mark_membership_claim_provisioning" for method, _ in calls)


class _StablePrincipalClaimRepository:
    def __init__(self) -> None:
        self.principal_reads: list[tuple[str, str]] = []
        self.links: list[tuple[str, str, str]] = []

    async def resolve_members_by_auth_user_id(
        self, auth_user_id: str
    ) -> list[dict[str, Any]]:
        assert auth_user_id == "auth-liang"
        return [
            {
                "organizationId": "org-baic",
                "member": {
                    "id": "member-liang",
                    "organizationId": "org-baic",
                    "status": "active",
                },
            }
        ]

    async def get_principal(
        self, organization_id: str, principal_id: str
    ) -> dict[str, Any]:
        self.principal_reads.append((organization_id, principal_id))
        return {
            "id": principal_id,
            "organizationId": organization_id,
            "name": "Historical reporting principal",
        }

    async def ensure_principal(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("a stable claim principal must not be resolved by display name")

    async def link_principal_member(
        self, organization_id: str, principal_id: str, member_id: str
    ) -> dict[str, Any]:
        self.links.append((organization_id, principal_id, member_id))
        return {"id": principal_id, "memberId": member_id}


def test_claim_reconciler_uses_stable_principal_id_instead_of_member_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _StablePrincipalClaimRepository()
    claim = {
        "id": "claim-1",
        "status": "provisioning",
        "authUserId": "auth-liang",
        "organizationId": "org-baic",
        "departmentId": "dept-management",
        "principalId": "principal-imported-liang",
        "memberName": "Liang Haiqiang (renamed display)",
        "loginName": "lianghaiqiang",
        "role": "admin",
    }
    auth_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def auth_call(method: str, *args: Any, **_kwargs: Any) -> Any:
        auth_calls.append((method, args))
        if method == "list_membership_claims":
            return [claim]
        if method == "activate_membership_claim":
            return {**claim, "status": "active"}
        raise AssertionError(method)

    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(main, "organization_store", lambda: repository)
    monkeypatch.setattr(
        main, "PostgreSQLOrganizationRepository", _StablePrincipalClaimRepository
    )
    monkeypatch.setattr(main, "auth_store_call", auth_call)

    assert asyncio.run(main.reconcile_active_membership_claims()) == 1
    assert repository.principal_reads == [
        ("org-baic", "principal-imported-liang")
    ]
    assert repository.links == [
        ("org-baic", "principal-imported-liang", "member-liang")
    ]
    assert any(method == "activate_membership_claim" for method, _ in auth_calls)


class _MembershipClaimRouteRepository:
    def __init__(self) -> None:
        self.audits: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.created_members: list[dict[str, Any]] = []
        self.links: list[tuple[str, str, str]] = []

    async def get_organization(self, organization_id: str) -> dict[str, Any]:
        return {
            "id": organization_id,
            "name": "\u5317\u6c7d\u96c6\u56e2",
            "status": "active",
        }

    async def get_department(
        self, department_id: str, *, organization_id: str
    ) -> dict[str, Any]:
        return {
            "id": department_id,
            "organizationId": organization_id,
            "name": "\u4f01\u4e1a\u7ba1\u7406",
            "status": "active",
        }

    async def ensure_principal(
        self, organization_id: str, name: str
    ) -> dict[str, Any]:
        assert (organization_id, name) == ("org-baic", "\u6881\u6d77\u5f3a")
        return {
            "id": "principal-liang",
            "organizationId": organization_id,
            "name": name,
        }

    async def create_managed_member(
        self,
        name: str,
        login_name: str,
        department_id: str,
        role: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        member = {
            "id": "member-liang",
            "organizationId": kwargs["organization_id"],
            "departmentId": department_id,
            "name": name,
            "loginName": login_name,
            "role": role,
            "status": "invited",
        }
        self.created_members.append(member)
        return member

    async def link_principal_member(
        self, organization_id: str, principal_id: str, member_id: str
    ) -> dict[str, Any]:
        self.links.append((organization_id, principal_id, member_id))
        return {"id": principal_id, "memberId": member_id}

    async def record_audit(self, *args: Any, **kwargs: Any) -> None:
        self.audits.append((args, kwargs))


async def _claim_platform_actor(
    _request: object, organization_id: str
) -> dict[str, Any]:
    return {
        "id": "platform-operator",
        "email": "operator@example.com",
        "selectedOrganizationId": organization_id,
    }


def _membership_claim_create_request() -> main.OrganizationMembershipClaimCreateRequest:
    return main.OrganizationMembershipClaimCreateRequest(
        memberName="\u6881\u6d77\u5f3a",
        loginName="lianghaiqiang",
        departmentId="dept-management",
        role="admin",
    )


def test_claim_approval_persists_and_queues_when_upstream_capability_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _MembershipClaimRouteRepository()
    claim = {
        "id": "claim-approve",
        "organizationId": "org-baic",
        "departmentId": "dept-management",
        "principalId": "principal-liang",
        "memberName": "\u6881\u6d77\u5f3a",
        "loginName": "lianghaiqiang",
        "role": "admin",
        "authUserId": "auth-liang",
        "status": "accepted_pending_approval",
    }
    auth_calls: list[str] = []

    async def no_csrf(_request: object) -> None:
        return None

    async def auth_call(method: str, *_args: Any, **_kwargs: Any) -> Any:
        auth_calls.append(method)
        if method == "get_membership_claim":
            return claim
        if method == "approve_membership_claim":
            claim["status"] = "approved"
            return claim
        if method == "mark_membership_claim_provisioning":
            claim["status"] = "provisioning"
            return claim
        raise AssertionError(method)

    async def no_outbox_work(*_args: Any, **_kwargs: Any) -> int:
        return 0

    def unavailable_capability() -> None:
        raise HTTPException(status_code=503, detail="upstream unavailable")

    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(main, "require_real_organization_capability", unavailable_capability)
    monkeypatch.setattr(main, "enforce_csrf", no_csrf)
    monkeypatch.setattr(main, "request_ip", lambda _request: "127.0.0.1")
    monkeypatch.setattr(main, "require_platform_organization", _claim_platform_actor)
    monkeypatch.setattr(main, "organization_store", lambda: repository)
    monkeypatch.setattr(
        main, "PostgreSQLOrganizationRepository", _MembershipClaimRouteRepository
    )
    monkeypatch.setattr(main, "auth_store_call", auth_call)
    monkeypatch.setattr(main, "organization_outbox_if_available", no_outbox_work)

    response = asyncio.run(
        main.platform_approve_membership_claim(
            "org-baic", "claim-approve", object()
        )
    )

    assert response["status"] == "provisioning"
    assert repository.created_members[0]["status"] == "invited"
    assert auth_calls == [
        "get_membership_claim",
        "approve_membership_claim",
        "mark_membership_claim_provisioning",
        "get_membership_claim",
    ]


def test_platform_claim_mutations_write_sanitized_organization_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _MembershipClaimRouteRepository()
    raw_token = "raw-membership-claim-token-must-not-be-audited"
    claims = {
        "claim-approve": {
            "id": "claim-approve",
            "organizationId": "org-baic",
            "departmentId": "dept-management",
            "principalId": "principal-liang",
            "memberName": "\u6881\u6d77\u5f3a",
            "loginName": "lianghaiqiang",
            "role": "admin",
            "authUserId": "auth-liang",
            "status": "accepted_pending_approval",
        },
        "claim-revoke": {
            "id": "claim-revoke",
            "organizationId": "org-baic",
            "departmentId": "dept-management",
            "principalId": "principal-liang",
            "memberName": "\u6881\u6d77\u5f3a",
            "loginName": "lianghaiqiang",
            "role": "admin",
            "authUserId": None,
            "status": "pending",
        },
    }

    async def no_csrf(_request: object) -> None:
        return None

    async def auth_call(method: str, *args: Any, **kwargs: Any) -> Any:
        if method == "check_rate_limit":
            return {"limited": False}
        if method == "create_membership_claim":
            assert kwargs["principal_id"] == "principal-liang"
            return {
                "id": "claim-create",
                "organizationId": "org-baic",
                "principalId": "principal-liang",
                "status": "pending",
                "token": raw_token,
            }
        if method == "get_membership_claim":
            return claims[str(args[0])]
        if method == "approve_membership_claim":
            claims["claim-approve"]["status"] = "approved"
            return claims["claim-approve"]
        if method == "mark_membership_claim_provisioning":
            claims["claim-approve"]["status"] = "provisioning"
            return claims["claim-approve"]
        if method == "revoke_membership_claim":
            return {**claims["claim-revoke"], "status": "revoked"}
        raise AssertionError((method, args, kwargs))

    async def no_outbox_work(*_args: Any, **_kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(main, "password_login_configured", lambda: True)
    monkeypatch.setattr(main, "turnstile_enabled", lambda: True)
    monkeypatch.setattr(main, "turnstile_configured", lambda: True)
    monkeypatch.setattr(main, "require_real_organization_capability", lambda: None)
    monkeypatch.setattr(main, "enforce_csrf", no_csrf)
    monkeypatch.setattr(main, "request_ip", lambda _request: "127.0.0.1")
    monkeypatch.setattr(main, "require_platform_organization", _claim_platform_actor)
    monkeypatch.setattr(main, "organization_store", lambda: repository)
    monkeypatch.setattr(
        main, "PostgreSQLOrganizationRepository", _MembershipClaimRouteRepository
    )
    monkeypatch.setattr(main, "auth_store_call", auth_call)
    monkeypatch.setattr(main, "organization_outbox_if_available", no_outbox_work)

    created = asyncio.run(
        main.platform_create_membership_claim(
            "org-baic", _membership_claim_create_request(), object()
        )
    )
    approved = asyncio.run(
        main.platform_approve_membership_claim(
            "org-baic", "claim-approve", object()
        )
    )
    revoked = asyncio.run(
        main.platform_revoke_membership_claim(
            "org-baic", "claim-revoke", object()
        )
    )

    assert raw_token in created["activationUrl"]
    assert approved["status"] == "provisioning"
    assert revoked["claim"]["status"] == "revoked"
    actions = [str(args[1]) for args, _kwargs in repository.audits]
    assert actions == [
        "organization.membership_claim.created",
        "organization.membership_claim.approved",
        "organization.membership_claim.revoked",
    ]
    for args, kwargs in repository.audits:
        audit_payload = {"args": args, "kwargs": kwargs}
        assert raw_token not in repr(audit_payload)
        keys: list[str] = []

        def collect_keys(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    keys.append(str(key).casefold())
                    collect_keys(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    collect_keys(item)

        collect_keys(audit_payload)
        assert not any("token" in key or "url" in key for key in keys)


class _UnavailableSnapshotStore:
    pool = object()

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def organization_rows(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((*args, kwargs))
        return None


class _DailyUsageMustNotBeCalled:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def organization_daily_usage(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("organization", *args, kwargs))
        return {"results": [{"date": "2026-07-30", "metrics": {"spend": 1}}]}

    async def team_daily_usage(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("team", *args, kwargs))
        return {"results": [{"date": "2026-07-30", "metrics": {"spend": 1}}]}

    @staticmethod
    def daily_usage_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
        return list(payload.get("results") or [])


@pytest.mark.parametrize("surface", ["organization", "department"])
def test_real_usage_fails_closed_when_postgres_snapshot_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    snapshot = _UnavailableSnapshotStore()
    upstream = _DailyUsageMustNotBeCalled()

    async def scoped_call(
        _organization_id: str, method: str, *args: Any, **kwargs: Any
    ) -> Any:
        if method == "get_organization":
            return {
                "id": "org-1",
                "status": "active",
                "upstreamOrganizationId": "org-upstream",
                "upstreamStatus": "active",
            }
        if method == "list_departments":
            return [
                {
                    "id": "dept-1",
                    "name": "Engineering",
                    "status": "active",
                    "upstreamTeamId": "team-upstream",
                }
            ]
        raise AssertionError((method, args, kwargs))

    monkeypatch.setattr(main, "require_real_organization_capability", lambda: None)
    monkeypatch.setattr(main, "organization_scoped_store_call", scoped_call)
    monkeypatch.setattr(main, "usage_store", lambda: snapshot)
    monkeypatch.setattr(main, "usage_backend_ids", lambda: ["primary"])
    monkeypatch.setattr(main, "client", lambda: upstream)

    error: HTTPException | None = None
    try:
        if surface == "organization":
            asyncio.run(
                main.real_organization_usage_payload(
                    "org-1",
                    start_date="2026-07-01",
                    end_date="2026-07-31",
                    source="all",
                )
            )
        else:
            asyncio.run(
                main.real_organization_department_usage_payload(
                    "org-1",
                    start_date="2026-07-01",
                    end_date="2026-07-31",
                    source="all",
                )
            )
    except HTTPException as exc:
        error = exc

    assert upstream.calls == []
    assert error is not None
    assert error.status_code == 503
    assert isinstance(error.detail, dict)
    assert error.detail.get("code") in {
        "ORGANIZATION_USAGE_UNAVAILABLE",
        "ORGANIZATION_USAGE_SNAPSHOT_UNAVAILABLE",
    }


class _OutboxExhaustionPool:
    def __init__(self) -> None:
        self.row = {
            "id": "outbox-1",
            "status": "processing",
            "attempts": 3,
            "created_at": datetime(2026, 7, 30, tzinfo=timezone.utc),
            "last_error": "",
        }

    def _apply_failed_update(self, query: str, error: str) -> None:
        normalized = " ".join(query.lower().split())
        self.row["last_error"] = error
        max_attempts = int(__import__("os").getenv("ORGANIZATION_OUTBOX_MAX_ATTEMPTS", "3"))
        if "failed" in normalized or self.row["attempts"] >= max_attempts:
            self.row["status"] = "failed"
        else:
            self.row["status"] = "pending"

    async def execute(self, query: str, *args: Any) -> str:
        if "update customer_outbox" in query.lower():
            error = str(args[1]) if len(args) > 1 else ""
            self._apply_failed_update(query, error)
            return "UPDATE 1"
        raise AssertionError(query)

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        normalized = query.lower()
        if "update customer_outbox" in normalized:
            error = str(args[1]) if len(args) > 1 else ""
            self._apply_failed_update(query, error)
            return dict(self.row)
        if "from customer_outbox" in normalized:
            if "attempts" in normalized and "count(" not in normalized:
                return dict(self.row)
            pending = int(self.row["status"] in {"pending", "processing"})
            failed = int(self.row["status"] == "failed")
            oldest = self.row["created_at"] if pending else None
            return {
                "pending": pending,
                "pending_count": pending,
                "oldest": oldest,
                "oldest_pending_at": oldest,
                "failed": failed,
                "failed_count": failed,
            }
        raise AssertionError(query)

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "attempts" in query.lower() and "customer_outbox" in query.lower():
            return self.row["attempts"]
        raise AssertionError(query)


def test_outbox_exhaustion_moves_job_to_failed_and_health_reports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORGANIZATION_OUTBOX_MAX_ATTEMPTS", "3")
    pool = _OutboxExhaustionPool()
    repository = PostgreSQLOrganizationRepository("postgresql://unused")
    repository.pool = pool

    completed = asyncio.run(
        repository.complete_outbox("outbox-1", error="permanent upstream error")
    )
    health = asyncio.run(repository.outbox_health())

    assert completed is True
    assert pool.row["status"] == "failed"
    assert health["pendingCount"] == 0
    assert health["failedCount"] == 1
