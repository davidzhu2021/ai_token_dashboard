"""Contracts for the real enterprise invitation verification flow."""

import asyncio

import pytest
from fastapi import HTTPException

from backend import main


def _invitation() -> dict[str, str]:
    return {
        "id": "inv-1",
        "organizationId": "org-1",
        "organizationName": "Acme",
        "memberId": "member-1",
        "email": "Invitee@Example.com",
        "name": "Invitee",
        "expiresAt": "2030-01-01T00:00:00+00:00",
        "status": "pending",
    }


def test_invitation_verification_exposes_existing_account_capability(monkeypatch) -> None:
    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)

    async def store_call(method, *_args, **kwargs):
        assert method == "verify_invitation"
        assert kwargs.pop("_require_capability") is False
        assert kwargs == {}
        return _invitation()

    async def auth_call(method, *_args, **_kwargs):
        assert method == "get_user_by_email"
        return {"id": "user-1", "email": "invitee@example.com"}

    monkeypatch.setattr(main, "organization_store_call", store_call)
    monkeypatch.setattr(main, "auth_store_call", auth_call)

    payload = asyncio.run(main.verify_organization_invitation("opaque-token"))

    assert payload["existingAccount"] is True
    assert payload["passwordRequired"] is False
    assert payload["invitation"]["email"] == "Invitee@Example.com"


def test_invitation_verification_requires_password_for_new_account(monkeypatch) -> None:
    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)

    async def store_call(method, *_args, **kwargs):
        assert method == "verify_invitation"
        assert kwargs.pop("_require_capability") is False
        assert kwargs == {}
        return _invitation()

    async def auth_call(method, *_args, **_kwargs):
        assert method == "get_user_by_email"
        return None

    monkeypatch.setattr(main, "organization_store_call", store_call)
    monkeypatch.setattr(main, "auth_store_call", auth_call)

    payload = asyncio.run(main.verify_organization_invitation("opaque-token"))

    assert payload["existingAccount"] is False
    assert payload["passwordRequired"] is True


def test_invitation_acceptance_stays_available_during_upstream_outage(
    monkeypatch,
) -> None:
    """Acceptance is a durable local transaction; provisioning remains async."""

    local_user = {
        "id": "user-1",
        "email": "invitee@example.com",
        "name": "Invitee",
        "email_verified": True,
        "status": "active",
    }
    store_calls = []

    async def no_csrf(_request):
        return None

    async def store_call(method, *args, **kwargs):
        store_calls.append((method, args, dict(kwargs)))
        assert kwargs.pop("_require_capability") is False
        assert kwargs == {}
        if method == "verify_invitation":
            return _invitation()
        if method == "accept_invitation":
            assert args == ("opaque-invitation-token", "user-1")
            return {"organizationId": "org-1", "memberId": "member-1"}
        raise AssertionError(method)

    async def auth_call(method, *args, **_kwargs):
        assert method == "get_user_by_email"
        assert args == ("invitee@example.com",)
        return local_user

    async def user_payload(user):
        assert user is local_user
        return {"id": user["id"], "email": user["email"]}

    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(
        main,
        "require_real_organization_capability",
        lambda: (_ for _ in ()).throw(AssertionError("upstream probe must not gate acceptance")),
    )
    monkeypatch.setattr(main, "enforce_csrf", no_csrf)
    monkeypatch.setattr(main, "organization_store_call", store_call)
    monkeypatch.setattr(main, "auth_store_call", auth_call)
    monkeypatch.setattr(main, "auth_user_payload", user_payload)

    payload = asyncio.run(
        main.accept_organization_invitation(
            main.OrganizationInvitationAcceptRequest(
                token="opaque-invitation-token",
            ),
            object(),
        )
    )

    assert payload["status"] == "provisioning"
    assert payload["organizationId"] == "org-1"
    assert [call[0] for call in store_calls] == ["verify_invitation", "accept_invitation"]


def test_real_mode_rejects_direct_member_activation(monkeypatch) -> None:
    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)

    with pytest.raises(HTTPException) as raised:
        main.reject_direct_real_member_activation("active")

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "ORGANIZATION_MEMBER_ACTIVATION_REQUIRES_PROVISIONING"


def test_demo_mode_keeps_manual_member_activation(monkeypatch) -> None:
    monkeypatch.setattr(main, "organization_real_enabled", lambda: False)

    main.reject_direct_real_member_activation("active")


class _InvitationManagementRepository:
    def __init__(self, *, member_status: str = "invited", revoke_result: bool = True) -> None:
        self.member_status = member_status
        self.revoke_result = revoke_result
        self.calls: list[tuple[object, ...]] = []

    async def get_member(self, member_id, *, organization_id=None):
        self.calls.append(("get_member", member_id, organization_id))
        if member_id != "member-1" or organization_id != "org-1":
            return None
        return {"id": member_id, "status": self.member_status}

    async def create_invitation(self, organization_id, member_id):
        self.calls.append(("create_invitation", organization_id, member_id))
        return {
            "id": "inv-new",
            "token": "secret-link-token",
            "organizationId": organization_id,
            "memberId": member_id,
            "status": "pending",
        }

    async def revoke_member_invitation(self, organization_id, member_id):
        self.calls.append(("revoke_member_invitation", organization_id, member_id))
        return self.revoke_result


def test_resend_real_invitation_is_scoped_and_never_returns_secret(monkeypatch) -> None:
    repository = _InvitationManagementRepository()
    monkeypatch.setattr(main, "require_real_organization_capability", lambda: None)
    monkeypatch.setattr(main, "organization_store", lambda: repository)
    monkeypatch.setattr(main, "PostgreSQLOrganizationRepository", _InvitationManagementRepository)

    payload = asyncio.run(main.resend_real_member_invitation("org-1", "member-1"))

    assert payload == {
        "id": "inv-new",
        "organizationId": "org-1",
        "memberId": "member-1",
        "status": "pending",
    }
    assert repository.calls == [
        ("get_member", "member-1", "org-1"),
        ("create_invitation", "org-1", "member-1"),
    ]


def test_platform_resend_invitation_returns_the_rotated_public_record(monkeypatch) -> None:
    invitation = {
        "id": "inv-new",
        "organizationId": "org-1",
        "memberId": "member-1",
        "status": "pending",
    }

    async def no_csrf(_request):
        return None

    async def platform_organization(_request, organization_id):
        assert organization_id == "org-1"
        return {"selectedOrganizationId": organization_id}

    async def resend(organization_id, member_id):
        assert (organization_id, member_id) == ("org-1", "member-1")
        return invitation

    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(main, "enforce_csrf", no_csrf)
    monkeypatch.setattr(main, "require_platform_organization", platform_organization)
    monkeypatch.setattr(main, "resend_real_member_invitation", resend)

    payload = asyncio.run(
        main.platform_resend_member_invitation("org-1", "member-1", object())
    )

    assert payload == {"ok": True, "invitation": invitation}


def test_invitation_management_rejects_cross_tenant_and_non_pending_members(monkeypatch) -> None:
    repository = _InvitationManagementRepository(member_status="active")
    monkeypatch.setattr(main, "require_real_organization_capability", lambda: None)
    monkeypatch.setattr(main, "organization_store", lambda: repository)
    monkeypatch.setattr(main, "PostgreSQLOrganizationRepository", _InvitationManagementRepository)

    with pytest.raises(HTTPException) as cross_tenant:
        asyncio.run(main.resend_real_member_invitation("org-2", "member-1"))
    with pytest.raises(HTTPException) as active_member:
        asyncio.run(main.revoke_real_member_invitation("org-1", "member-1"))

    assert cross_tenant.value.status_code == 404
    assert cross_tenant.value.detail["code"] == "ORGANIZATION_MEMBER_NOT_FOUND"
    assert active_member.value.status_code == 409
    assert active_member.value.detail["code"] == "ORGANIZATION_INVITATION_MEMBER_NOT_PENDING"


def test_revoke_real_invitation_requires_a_current_pending_link(monkeypatch) -> None:
    repository = _InvitationManagementRepository(revoke_result=False)
    monkeypatch.setattr(main, "require_real_organization_capability", lambda: None)
    monkeypatch.setattr(main, "organization_store", lambda: repository)
    monkeypatch.setattr(main, "PostgreSQLOrganizationRepository", _InvitationManagementRepository)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(main.revoke_real_member_invitation("org-1", "member-1"))

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "ORGANIZATION_INVITATION_NOT_PENDING"


def test_invitation_management_routes_cover_customer_and_platform_scopes() -> None:
    paths = {route.path for route in main.app.routes}

    assert "/api/organization/current/members/{member_id}/invitation/resend" in paths
    assert "/api/organization/current/members/{member_id}/invitation/revoke" in paths
    assert (
        "/api/platform/organizations/{organization_id}/members/{member_id}/invitation/resend"
        in paths
    )
    assert (
        "/api/platform/organizations/{organization_id}/members/{member_id}/invitation/revoke"
        in paths
    )


def test_auth_me_does_not_generic_provision_invitation_bound_account(monkeypatch) -> None:
    local_user = {
        "id": "user-1",
        "email": "invitee@example.com",
        "name": "Invitee",
        "status": "active",
    }
    invited_membership = {
        "organizationId": "org-1",
        "status": "invited",
        "organizationStatus": "active",
    }
    calls = {"genericProvisioning": 0}

    async def current_user(_request):
        return local_user

    async def memberships(user):
        assert user is local_user
        return [invited_membership]

    async def retry(_user):
        calls["genericProvisioning"] += 1

    async def user_payload(user, *, refresh_entitlement=False):
        assert user is local_user
        assert refresh_entitlement is True
        return {"id": user["id"], "email": user["email"], "authType": "password"}

    async def no_demo(_user):
        return False

    async def access_fields(_user):
        return {"organizationAccessStatus": "invited"}

    monkeypatch.setattr(main, "current_local_auth_user", current_user)
    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(main, "organization_memberships_for_user", memberships)
    monkeypatch.setattr(main, "retry_local_provisioning", retry)
    monkeypatch.setattr(main, "auth_user_payload", user_payload)
    monkeypatch.setattr(main, "is_demo_customer_user", no_demo)
    monkeypatch.setattr(main, "organization_access_fields_for_user", access_fields)
    monkeypatch.setattr(main, "csrf_token", lambda _request: "csrf")

    payload = asyncio.run(main.auth_me(object()))

    assert calls["genericProvisioning"] == 0
    assert payload["organizationAccessStatus"] == "invited"
