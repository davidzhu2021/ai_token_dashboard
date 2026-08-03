"""Durable outbox worker for real customer-organization provisioning.

The worker deliberately depends on small repository/upstream protocols.  This
keeps HTTP routes thin and makes retries safe to exercise with fakes in tests.
No plaintext invitation or API key is persisted by this module.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Awaitable, Callable, Protocol

logger = logging.getLogger("ai-token-dashboard.organization-provisioning")


class ProvisioningRepository(Protocol):
    async def get_organization(self, organization_id: str) -> dict[str, Any] | None: ...
    async def list_departments(self, *, organization_id: str, include_archived: bool = False) -> list[dict[str, Any]]: ...
    async def get_member(self, member_id: str, *, organization_id: str | None = None) -> dict[str, Any] | None: ...
    async def set_upstream_organization(self, organization_id: str, upstream_id: str, *, status: str = "active") -> dict[str, Any]: ...
    async def set_upstream_team(self, organization_id: str, department_id: str, upstream_team_id: str) -> dict[str, Any]: ...
    async def set_upstream_member(self, organization_id: str, member_id: str, upstream_user_id: str, *, status: str | None = None) -> dict[str, Any]: ...
    async def activate_member_upstream(self, organization_id: str, member_id: str, upstream_user_id: str) -> dict[str, Any]: ...
    async def ensure_member_invitation(self, organization_id: str, member_id: str, *, expires_in_hours: int = 72) -> dict[str, Any] | None: ...
    async def invitation_token_for_delivery(self, invitation_id: str) -> str | None: ...
    async def mark_invitation_sent(self, invitation_id: str) -> bool: ...
    async def get_token(self, organization_id: str, token_id: str) -> dict[str, Any] | None: ...
    async def finalize_token_record(self, token_id: str, *, upstream_key_id: str, upstream_key_hash: str = "", status: str = "active", plaintext_token: str | None = None) -> dict[str, Any]: ...
    async def mark_token_revoked(self, organization_id: str, token_id: str) -> dict[str, Any]: ...
    async def claim_outbox(self, *, limit: int = 20) -> list[dict[str, Any]]: ...
    async def complete_outbox(self, outbox_id: str, *, error: str = "") -> bool: ...


class ProvisioningUpstream(Protocol):
    async def create_organization(self, organization_alias: str, **kwargs: Any) -> dict[str, Any]: ...
    async def list_organizations(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    async def create_team(self, team_alias: str, organization_id: str, **kwargs: Any) -> dict[str, Any]: ...
    async def update_organization(self, organization_id: str, **kwargs: Any) -> dict[str, Any]: ...
    async def update_team(self, team_id: str, **kwargs: Any) -> dict[str, Any]: ...
    async def list_teams(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    async def create_internal_user(self, user_id: str, email: str | None, name: str | None = None, **kwargs: Any) -> dict[str, Any]: ...
    async def user_info(self, user_id: str, **kwargs: Any) -> dict[str, Any]: ...
    async def organization_info(self, organization_id: str, **kwargs: Any) -> dict[str, Any]: ...
    async def team_info(self, backend: Any, team_id: str) -> dict[str, Any] | None: ...
    async def add_organization_member(self, organization_id: str, role: str, **kwargs: Any) -> dict[str, Any]: ...
    async def update_organization_member(self, organization_id: str, **kwargs: Any) -> dict[str, Any]: ...
    async def add_team_member(self, team_id: str, role: str, **kwargs: Any) -> dict[str, Any]: ...
    async def update_team_member(self, team_id: str, **kwargs: Any) -> dict[str, Any]: ...
    async def delete_organization_member(self, organization_id: str, **kwargs: Any) -> dict[str, Any]: ...
    async def delete_team_member(self, team_id: str, **kwargs: Any) -> dict[str, Any]: ...
    async def find_organization_key_by_alias(self, organization_id: str, key_alias: str, **kwargs: Any) -> dict[str, Any] | None: ...
    def organization_key_identity(self, record: dict[str, Any]) -> dict[str, str]: ...
    async def revoke_organization_key(self, key_id: str, **kwargs: Any) -> dict[str, Any]: ...


Mailer = Callable[[str, str, dict[str, Any]], Awaitable[Any] | Any]


def _id(payload: Any, *keys: str) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in keys:
        value = payload.get(key)
        if value:
            return str(value).strip()
    return ""


def _metadata(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    value = payload.get("metadata")
    return value if isinstance(value, dict) else {}


def _member_entries(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _not_found_error(exc: BaseException) -> bool:
    return getattr(exc, "status_code", None) == 404


class OrganizationProvisioningService:
    """Provision organizations and invitation emails from PostgreSQL outbox rows."""

    def __init__(self, repository: ProvisioningRepository, upstream: ProvisioningUpstream, *, mailer: Mailer | None = None, changed_by: str = "organization-provisioner") -> None:
        self.repository = repository
        self.upstream = upstream
        self.mailer = mailer
        self.changed_by = changed_by

    async def provision_organization(self, organization_id: str, *, admin_member_id: str = "") -> dict[str, Any]:
        org = await self.repository.get_organization(organization_id)
        if not org:
            raise ValueError("organization was not found")
        org_status = str(org.get("status") or "")
        if org_status in {"suspended", "archived"}:
            raise RuntimeError(f"organization is {org_status}")
        upstream_org_id = _id(org, "upstreamOrganizationId", "upstream_organization_id")
        if not upstream_org_id:
            # The local id is carried as metadata for reconciliation, never used
            # as the upstream primary key.
            matches = await self.upstream.list_organizations(organization_alias=str(org["name"]))
            for candidate in matches:
                metadata = candidate.get("metadata") if isinstance(candidate, dict) else None
                if isinstance(metadata, dict) and str(metadata.get("local_id")) == organization_id:
                    upstream_org_id = _id(candidate, "organization_id", "organizationId", "id")
                    break
            if not upstream_org_id:
                created = await self.upstream.create_organization(
                    str(org["name"]), metadata={"local_id": organization_id, "created_via": "ai-token-dashboard"}, changed_by=self.changed_by
                )
                upstream_org_id = _id(created, "organization_id", "organizationId", "id")
            if not upstream_org_id:
                raise RuntimeError("upstream organization id was not returned")
            # Keep the local organization in provisioning until every
            # department has a durable upstream mapping.
            await self.repository.set_upstream_organization(
                organization_id,
                upstream_org_id,
                status="provisioning",
            )

        departments = await self.repository.list_departments(organization_id=organization_id, include_archived=False)
        provisioned_departments = 0
        for department in departments:
            team_id = _id(department, "upstreamTeamId", "upstream_team_id")
            if not team_id:
                local_department_id = str(department["id"])
                list_teams = getattr(self.upstream, "list_teams", None)
                matches = (
                    await list_teams(
                        organization_id=upstream_org_id,
                        team_alias=str(department["name"]),
                    )
                    if callable(list_teams)
                    else []
                )
                for candidate in matches:
                    metadata = _metadata(candidate)
                    if str(metadata.get("local_id") or "") == local_department_id:
                        team_id = _id(candidate, "team_id", "teamId", "id")
                        break
                if not team_id:
                    created = await self.upstream.create_team(
                        str(department["name"]), upstream_org_id,
                        metadata={"local_id": local_department_id, "organization_id": organization_id},
                        changed_by=self.changed_by,
                    )
                    team_id = _id(created, "team_id", "teamId", "id")
                if not team_id:
                    raise RuntimeError("upstream team id was not returned")
                await self.repository.set_upstream_team(organization_id, local_department_id, team_id)
            provisioned_departments += 1
        # Transition only after all upstream objects are reconciled. This
        # prevents a partially provisioned organization from receiving access.
        await self.repository.set_upstream_organization(
            organization_id,
            upstream_org_id,
            status="active",
        )
        # Credit may have been granted before upstream provisioning finished.
        # Project the current durable account as soon as the organization id
        # exists instead of waiting for a later ledger mutation.
        billing_payload = getattr(self.repository, "billing_payload", None)
        if callable(billing_payload):
            billing = await billing_payload(organization_id, page=1, page_size=1)
            account = billing.get("account") if isinstance(billing, dict) else None
            if isinstance(account, dict):
                preserve_report_only = False
                report_only_loader = getattr(
                    self.repository, "organization_has_report_only_assets", None
                )
                if callable(report_only_loader):
                    preserve_report_only = bool(
                        await report_only_loader(organization_id)
                    )
                await self.sync_billing(
                    {
                        "upstreamOrganizationId": upstream_org_id,
                        "balanceUsd": account.get("availableBalanceUsd", 0),
                        "billingStatus": account.get("billingStatus", "past_due"),
                        "preserveReportOnlyAssets": preserve_report_only,
                    }
                )
        if admin_member_id:
            await self.repository.ensure_member_invitation(
                organization_id,
                admin_member_id,
            )
        return {"organizationId": organization_id, "upstreamOrganizationId": upstream_org_id, "departments": provisioned_departments}

    async def sync_organization(self, payload: dict[str, Any]) -> dict[str, Any]:
        organization_id = str(payload.get("organizationId") or "").strip()
        upstream_id = str(payload.get("upstreamOrganizationId") or "").strip()
        if not organization_id or not upstream_id:
            raise ValueError("organization sync payload is incomplete")
        status = str(payload.get("status") or "active").strip().lower()
        await self.upstream.update_organization(
            upstream_id,
            organization_alias=str(payload.get("name") or "").strip() or None,
            blocked=status in {"suspended", "archived"},
            changed_by=self.changed_by,
        )
        return {"organizationId": organization_id, "upstreamOrganizationId": upstream_id, "status": status}

    async def sync_department(self, payload: dict[str, Any]) -> dict[str, Any]:
        organization_id = str(payload.get("organizationId") or "").strip()
        department_id = str(payload.get("departmentId") or "").strip()
        upstream_org_id = str(payload.get("upstreamOrganizationId") or "").strip()
        if not organization_id or not department_id or not upstream_org_id:
            raise ValueError("department sync payload is incomplete")
        upstream_team_id = str(payload.get("upstreamTeamId") or "").strip()
        if not upstream_team_id:
            department = next(
                (
                    item
                    for item in await self.repository.list_departments(
                        organization_id=organization_id,
                        include_archived=True,
                    )
                    if str(item.get("id") or "") == department_id
                ),
                None,
            )
            upstream_team_id = _id(department, "upstreamTeamId", "upstream_team_id")
        if not upstream_team_id:
            result = await self.provision_organization(organization_id)
            department = next(
                (
                    item
                    for item in await self.repository.list_departments(
                        organization_id=organization_id,
                        include_archived=True,
                    )
                    if str(item.get("id") or "") == department_id
                ),
                None,
            )
            upstream_team_id = _id(department, "upstreamTeamId", "upstream_team_id")
            if not upstream_team_id:
                raise RuntimeError(f"department was not provisioned: {result}")
        status = str(payload.get("status") or "active").strip().lower()
        await self.upstream.update_team(
            upstream_team_id,
            team_alias=str(payload.get("name") or "").strip() or None,
            organization_id=upstream_org_id,
            blocked=status == "archived",
            changed_by=self.changed_by,
        )
        return {"organizationId": organization_id, "departmentId": department_id, "upstreamTeamId": upstream_team_id, "status": status}

    async def provision_member(self, organization_id: str, member_id: str, *, auth_user_id: str = "") -> dict[str, Any]:
        member = await self.repository.get_member(member_id, organization_id=organization_id)
        if not member:
            raise ValueError("member was not found")
        member_status = str(member.get("status") or "")
        if member_status in {"suspended", "archived"}:
            raise RuntimeError(f"member is {member_status}")
        user_id = _id(member, "upstreamUserId", "upstream_user_id")
        # Stable member ids, rather than an email/login alias, are the upstream
        # identity for both invited email accounts and offline-managed accounts.
        local_user_id = f"customer-member-{member_id}"
        if not user_id:
            user_info = getattr(self.upstream, "user_info", None)
            if callable(user_info):
                try:
                    existing = await user_info(local_user_id, backend=None)
                except Exception as exc:
                    if not _not_found_error(exc):
                        raise
                    existing = {}
            else:
                existing = {}
            existing_id = _id(existing, "user_id", "userId", "id")
            if existing_id:
                existing_email = str(existing.get("user_email") or existing.get("userEmail") or "").strip().lower()
                expected_email = str(member.get("email") or "").strip().lower()
                metadata = _metadata(existing)
                local_mapping = str(metadata.get("local_user_id") or "")
                if existing_id != local_user_id or (
                    expected_email and existing_email and existing_email != expected_email
                ):
                    raise RuntimeError("upstream user mapping conflicts with invitation account")
                if local_mapping and local_mapping != member_id:
                    raise RuntimeError("upstream user metadata conflicts with invitation account")
                user_id = existing_id
            else:
                created = await self.upstream.create_internal_user(
                    local_user_id,
                    str(member.get("email") or "") or None,
                    str(member.get("name") or ""),
                    metadata={
                        "local_user_id": member_id,
                        "auth_user_id": auth_user_id
                        or _id(member, "authUserId", "auth_user_id"),
                    },
                    backend=None,
                )
                user_id = _id(created, "user_id", "userId", "id") or local_user_id
            await self.repository.set_upstream_member(organization_id, member_id, user_id)

        org = await self.repository.get_organization(organization_id)
        if not org:
            raise ValueError("organization was not found")
        upstream_org_id = _id(org, "upstreamOrganizationId", "upstream_organization_id")
        if not upstream_org_id:
            await self.provision_organization(organization_id)
            org = await self.repository.get_organization(organization_id)
            upstream_org_id = _id(org, "upstreamOrganizationId", "upstream_organization_id")
        if not upstream_org_id:
            raise RuntimeError("upstream organization is not ready")
        role = "enterprise_admin" if str(member.get("role")) == "admin" else "member"
        organization_info_fn = getattr(self.upstream, "organization_info", None)
        organization_info = await organization_info_fn(upstream_org_id) if callable(organization_info_fn) else {}
        organization_members = _member_entries(
            organization_info,
            "members",
            "organization_memberships",
            "updated_organization_memberships",
        )
        organization_member = next(
            (
                item
                for item in organization_members
                if _id(item, "user_id", "userId", "id") == user_id
                or _id(item.get("user"), "user_id", "userId", "id") == user_id
            ),
            None,
        )
        update_organization_member = getattr(self.upstream, "update_organization_member", None)
        if organization_member and callable(update_organization_member):
            await update_organization_member(
                upstream_org_id,
                user_id=user_id,
                role=role,
                changed_by=self.changed_by,
            )
        else:
            await self.upstream.add_organization_member(upstream_org_id, role, user_id=user_id, changed_by=self.changed_by)

        departments = await self.repository.list_departments(organization_id=organization_id, include_archived=True)
        department = next((item for item in departments if str(item.get("id")) == str(member.get("departmentId") or member.get("department_id"))), None)
        team_id = _id(department, "upstreamTeamId", "upstream_team_id") if department else ""
        if team_id:
            team_role = "admin" if str(member.get("teamRole")) == "leader" else "member"
            team_info: dict[str, Any] | None = None
            list_teams = getattr(self.upstream, "list_teams", None)
            if callable(list_teams):
                candidates = await list_teams(team_id=team_id)
                team_info = next(
                    (candidate for candidate in candidates if _id(candidate, "team_id", "teamId", "id") == team_id),
                    candidates[0] if candidates else None,
                )
            if team_info is None:
                team_info_fn = getattr(self.upstream, "team_info", None)
                if callable(team_info_fn):
                    if hasattr(self.upstream, "backends"):
                        team_info = await team_info_fn(self.upstream.backends[0], team_id)
                    else:
                        team_info = await team_info_fn(None, team_id)
            team_members = _member_entries(team_info, "members_with_roles", "team_memberships", "members")
            team_member = next(
                (
                    item
                    for item in team_members
                    if _id(item, "user_id", "userId", "id") == user_id
                    or _id(item.get("user"), "user_id", "userId", "id") == user_id
                ),
                None,
            )
            update_team_member = getattr(self.upstream, "update_team_member", None)
            if team_member and callable(update_team_member):
                await update_team_member(
                    team_id,
                    user_id=user_id,
                    role=team_role,
                    changed_by=self.changed_by,
                )
            else:
                await self.upstream.add_team_member(team_id, team_role, user_id=user_id, changed_by=self.changed_by)
        await self.repository.activate_member_upstream(organization_id, member_id, user_id)
        return {"organizationId": organization_id, "memberId": member_id, "upstreamUserId": user_id, "upstreamTeamId": team_id}

    async def sync_member(self, payload: dict[str, Any]) -> dict[str, Any]:
        organization_id = str(payload.get("organizationId") or "").strip()
        member_id = str(payload.get("memberId") or "").strip()
        if not organization_id or not member_id:
            raise ValueError("member sync payload is incomplete")
        member = await self.repository.get_member(member_id, organization_id=organization_id)
        if not member:
            raise ValueError("member was not found")
        upstream_user_id = _id(member, "upstreamUserId", "upstream_user_id")
        if not upstream_user_id:
            # Invited members have no upstream access yet; the invitation
            # acceptance flow owns their first membership creation.
            if str(member.get("status") or "") == "invited":
                return {"organizationId": organization_id, "memberId": member_id, "status": "invited"}
            raise RuntimeError("member upstream user is unavailable")
        organization = await self.repository.get_organization(organization_id)
        upstream_org_id = _id(organization, "upstreamOrganizationId", "upstream_organization_id")
        if not upstream_org_id:
            raise RuntimeError("upstream organization is unavailable")
        departments = await self.repository.list_departments(organization_id=organization_id, include_archived=True)
        target_department_id = str(member.get("departmentId") or member.get("department_id") or "")
        target_team_id = ""
        for department in departments:
            team_id = _id(department, "upstreamTeamId", "upstream_team_id")
            if not team_id:
                continue
            if str(department.get("id") or "") == target_department_id:
                target_team_id = team_id
                continue
            # Team moves are historical in usage, but access follows the
            # current department immediately.
            try:
                await self.upstream.delete_team_member(
                    team_id,
                    user_id=upstream_user_id,
                    changed_by=self.changed_by,
                )
            except Exception as exc:
                if not _not_found_error(exc):
                    raise
        status = str(member.get("status") or "").strip().lower()
        if status in {"invited", "suspended"}:
            try:
                await self.upstream.delete_organization_member(
                    upstream_org_id,
                    user_id=upstream_user_id,
                    changed_by=self.changed_by,
                )
            except Exception as exc:
                if not _not_found_error(exc):
                    raise
            if target_team_id:
                try:
                    await self.upstream.delete_team_member(
                        target_team_id,
                        user_id=upstream_user_id,
                        changed_by=self.changed_by,
                    )
                except Exception as exc:
                    if not _not_found_error(exc):
                        raise
            return {"organizationId": organization_id, "memberId": member_id, "status": status}
        role = "enterprise_admin" if str(member.get("role") or "") == "admin" else "member"
        await self.upstream.update_organization_member(
            upstream_org_id,
            user_id=upstream_user_id,
            role=role,
            changed_by=self.changed_by,
        )
        if target_team_id:
            team_role = "admin" if str(member.get("teamRole") or "") == "leader" else "member"
            try:
                await self.upstream.update_team_member(
                    target_team_id,
                    user_id=upstream_user_id,
                    role=team_role,
                    changed_by=self.changed_by,
                )
            except Exception as exc:
                if not _not_found_error(exc):
                    raise
                await self.upstream.add_team_member(
                    target_team_id,
                    team_role,
                    user_id=upstream_user_id,
                    changed_by=self.changed_by,
                )
        return {"organizationId": organization_id, "memberId": member_id, "status": status, "upstreamTeamId": target_team_id}

    async def sync_billing(self, payload: dict[str, Any]) -> dict[str, Any]:
        upstream_org_id = str(payload.get("upstreamOrganizationId") or "").strip()
        if not upstream_org_id:
            raise ValueError("billing sync payload is incomplete")
        try:
            balance = max(0.0, float(payload.get("balanceUsd") or 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("billing balance is invalid") from exc
        billing_status = str(payload.get("billingStatus") or "past_due").strip().lower()
        if bool(payload.get("preserveReportOnlyAssets")):
            # Imported keys retain their original upstream Organization scope.
            # Updating that Organization's budget would therefore govern and
            # potentially block assets explicitly marked report-only. Keep the
            # local ledger authoritative and enforce exhaustion by revoking only
            # dashboard-managed tokens through the durable revocation outbox.
            return {
                "upstreamOrganizationId": upstream_org_id,
                "localBalance": balance,
                "billingStatus": billing_status,
                "enforcementMode": "managed_tokens_only",
                "upstreamOrganizationUnchanged": True,
            }
        # LiteLLM's organization max_budget is a lifetime spend ceiling, not a
        # remaining balance. Preserve the already-recorded spend and extend the
        # ceiling only by the locally available credit; otherwise each daily
        # settlement would subtract the same spend a second time upstream.
        organization = await self.upstream.organization_info(upstream_org_id)
        try:
            upstream_spend = max(0.0, float(organization.get("spend") or 0))
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError("upstream organization spend is unavailable") from exc
        ceiling = upstream_spend + balance
        await self.upstream.update_organization(
            upstream_org_id,
            max_budget=ceiling,
            blocked=billing_status != "active",
            changed_by=self.changed_by,
        )
        return {
            "upstreamOrganizationId": upstream_org_id,
            "upstreamSpend": upstream_spend,
            "maxBudget": ceiling,
            "blocked": billing_status != "active",
        }

    async def dispatch_invitation(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.mailer is None:
            raise RuntimeError("invitation mailer is not configured")
        invitation_id = str(payload.get("invitationId") or payload.get("invitation_id") or "")
        email = str(payload.get("email") or "").strip().lower()
        if not invitation_id or not email:
            raise ValueError("invitation payload is incomplete")
        token = await self.repository.invitation_token_for_delivery(invitation_id)
        if not token:
            raise RuntimeError("invitation token is unavailable or expired")
        result = self.mailer(email, token, payload)
        if inspect.isawaitable(result):
            await result
        mark_sent = getattr(self.repository, "mark_invitation_sent", None)
        if callable(mark_sent):
            marked = mark_sent(invitation_id)
            if inspect.isawaitable(marked):
                await marked
        return {"invitationId": invitation_id, "email": email, "sent": True}

    async def revoke_token(self, payload: dict[str, Any], *, outbox_id: str) -> dict[str, Any]:
        organization_id = str(payload.get("organizationId") or payload.get("organization_id") or "").strip()
        token_id = str(payload.get("tokenId") or payload.get("token_id") or "").strip()
        if not organization_id or not token_id or not outbox_id:
            raise ValueError("token revocation payload is incomplete")
        token = await self.repository.get_token(organization_id, token_id)
        if not token:
            raise ValueError("token was not found")
        if str(token.get("status") or "") == "revoked":
            return {"organizationId": organization_id, "tokenId": token_id, "revoked": True}
        upstream_key_id = _id(token, "upstreamKeyId", "upstream_key_id")
        payload_key_id = str(payload.get("upstreamKeyId") or payload.get("upstream_key_id") or "").strip()
        if payload_key_id and upstream_key_id and payload_key_id != upstream_key_id:
            raise RuntimeError("token upstream key id changed before revocation")
        upstream_key_id = upstream_key_id or payload_key_id
        if not upstream_key_id:
            upstream_org_id = str(payload.get("upstreamOrganizationId") or "").strip()
            alias = str(payload.get("upstreamKeyAlias") or token.get("upstreamKeyAlias") or "").strip()
            if not upstream_org_id:
                organization = await self.repository.get_organization(organization_id)
                upstream_org_id = _id(
                    organization,
                    "upstreamOrganizationId",
                    "upstream_organization_id",
                )
            if not upstream_org_id or not alias:
                raise RuntimeError("token upstream key id is unavailable")
            upstream_record = await self.upstream.find_organization_key_by_alias(
                upstream_org_id,
                alias,
                team_id=str(payload.get("upstreamTeamId") or token.get("upstreamTeamId") or "").strip() or None,
                user_id=str(payload.get("upstreamUserId") or "").strip() or None,
            )
            if upstream_record is None:
                # A create may still be in flight or the list projection may
                # lag. Keep this job retryable rather than marking the local
                # record revoked while an unknown upstream key can appear.
                raise RuntimeError("upstream token is not visible yet")
            identity = self.upstream.organization_key_identity(upstream_record)
            upstream_key_id = str(identity.get("id") or "").strip()
            if not upstream_key_id:
                raise RuntimeError("upstream token id is unavailable")
        await self.upstream.revoke_organization_key(
            upstream_key_id,
            changed_by=self.changed_by,
            idempotency_key=outbox_id,
        )
        await self.repository.mark_token_revoked(organization_id, token_id)
        return {"organizationId": organization_id, "tokenId": token_id, "revoked": True}

    async def reconcile_token(self, payload: dict[str, Any], *, outbox_id: str) -> dict[str, Any]:
        """Recover a key created upstream before its local finalize committed."""

        organization_id = str(payload.get("organizationId") or "").strip()
        token_id = str(payload.get("tokenId") or "").strip()
        upstream_org_id = str(payload.get("upstreamOrganizationId") or "").strip()
        alias = str(payload.get("upstreamKeyAlias") or "").strip()
        team_id = str(payload.get("upstreamTeamId") or "").strip()
        user_id = str(payload.get("upstreamUserId") or "").strip()
        if not all((organization_id, token_id, upstream_org_id, alias, outbox_id)):
            raise ValueError("token reconciliation payload is incomplete")

        token = await self.repository.get_token(organization_id, token_id)
        upstream_record = await self.upstream.find_organization_key_by_alias(
            upstream_org_id,
            alias,
            team_id=team_id or None,
            user_id=user_id or None,
        )
        if upstream_record is None:
            if token is None or str(token.get("status") or "") in {"active", "revoked", "expired"}:
                return {
                    "organizationId": organization_id,
                    "tokenId": token_id,
                    "reconciled": token is not None and str(token.get("status") or "") == "active",
                    "orphanCleaned": False,
                }
            # Key listing may lag a successful create, so keep provisioning
            # records retryable instead of declaring the upstream key absent.
            raise RuntimeError("upstream token is not visible yet")

        identity = self.upstream.organization_key_identity(upstream_record)
        upstream_key_id = str(identity.get("id") or "").strip()
        if not upstream_key_id:
            raise RuntimeError("upstream token id is unavailable")

        async def clean_orphan() -> dict[str, Any]:
            await self.upstream.revoke_organization_key(
                upstream_key_id,
                changed_by=self.changed_by,
                idempotency_key=outbox_id,
            )
            return {
                "organizationId": organization_id,
                "tokenId": token_id,
                "reconciled": False,
                "orphanCleaned": True,
            }

        if token is None:
            return await clean_orphan()

        status = str(token.get("status") or "").strip().lower()
        local_upstream_id = str(token.get("upstreamKeyId") or "").strip()
        if status == "active" and local_upstream_id == upstream_key_id:
            return {
                "organizationId": organization_id,
                "tokenId": token_id,
                "reconciled": True,
                "orphanCleaned": False,
            }
        if status != "provisioning" or local_upstream_id:
            return await clean_orphan()

        key_hash = str(identity.get("hash") or "").strip()
        listed_token = str(identity.get("token") or "").strip()
        if not key_hash and listed_token:
            if listed_token.startswith("sk-"):
                import hashlib

                key_hash = hashlib.sha256(listed_token.encode("utf-8")).hexdigest()
            else:
                key_hash = listed_token
        key_hash = key_hash or upstream_key_id
        try:
            await self.repository.finalize_token_record(
                token_id,
                upstream_key_id=upstream_key_id,
                upstream_key_hash=key_hash,
                status="active",
                plaintext_token=None,
            )
        except Exception:
            # A concurrent delete/revoke must not leave the discovered key
            # orphaned. Genuine transient DB failures remain retryable.
            current = await self.repository.get_token(organization_id, token_id)
            if current is None:
                return await clean_orphan()
            current_status = str(current.get("status") or "").strip().lower()
            current_upstream_id = str(current.get("upstreamKeyId") or "").strip()
            if current_status == "active" and current_upstream_id == upstream_key_id:
                return {
                    "organizationId": organization_id,
                    "tokenId": token_id,
                    "reconciled": True,
                    "orphanCleaned": False,
                }
            if current_status != "provisioning" or current_upstream_id:
                return await clean_orphan()
            raise
        return {
            "organizationId": organization_id,
            "tokenId": token_id,
            "reconciled": True,
            "orphanCleaned": False,
        }

    async def process_outbox(self, *, limit: int = 20) -> dict[str, int]:
        rows = await self.repository.claim_outbox(limit=limit)
        stats = {"claimed": len(rows), "completed": 0, "retried": 0}
        for row in rows:
            outbox_id = str(row.get("id") or "")
            kind = str(row.get("kind") or "")
            payload = row.get("payload") or {}
            try:
                if isinstance(payload, str):
                    import json

                    payload = json.loads(payload)
                if not isinstance(payload, dict):
                    raise ValueError("outbox payload must be an object")
                if kind in {"organization.provision", "organization.created"}:
                    await self.provision_organization(
                        str(payload.get("organizationId") or row.get("aggregate_id")),
                        admin_member_id=str(payload.get("adminMemberId") or ""),
                    )
                elif kind == "organization.member.provision":
                    await self.provision_member(str(payload["organizationId"]), str(payload["memberId"]), auth_user_id=str(payload.get("authUserId") or ""))
                elif kind == "organization.sync":
                    await self.sync_organization(payload)
                elif kind == "department.sync":
                    await self.sync_department(payload)
                elif kind == "organization.member.sync":
                    await self.sync_member(payload)
                elif kind == "organization.billing.sync":
                    await self.sync_billing(payload)
                elif kind == "organization.invitation.created":
                    await self.dispatch_invitation(payload)
                elif kind == "organization.token.revoke":
                    await self.revoke_token(payload, outbox_id=outbox_id)
                elif kind == "organization.token.reconcile":
                    await self.reconcile_token(payload, outbox_id=outbox_id)
                else:
                    raise ValueError(f"unsupported outbox kind: {kind}")
            except Exception as exc:
                logger.warning("organization outbox retry id=%s kind=%s error=%s", outbox_id, kind, exc)
                await self.repository.complete_outbox(outbox_id, error=str(exc))
                stats["retried"] += 1
            else:
                await self.repository.complete_outbox(outbox_id)
                stats["completed"] += 1
        return stats
