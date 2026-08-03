import asyncio

from backend.organization_provisioning import OrganizationProvisioningService


class Repo:
    def __init__(self):
        self.org = {"id": "org-local", "name": "Acme"}
        self.departments = [{"id": "dept-local", "name": "Engineering"}]
        self.member = {"id": "member-local", "name": "Alice", "email": "alice@example.com", "departmentId": "dept-local", "role": "admin", "teamRole": "leader"}
        self.calls = []
        self.rows = []

    async def get_organization(self, organization_id):
        return self.org if organization_id == "org-local" else None

    async def list_departments(self, *, organization_id, include_archived=False):
        return self.departments

    async def get_member(self, member_id, *, organization_id=None):
        return self.member if member_id == "member-local" else None

    async def set_upstream_organization(self, organization_id, upstream_id, *, status="active"):
        self.org["upstreamOrganizationId"] = upstream_id
        self.org["upstreamStatus"] = status
        self.calls.append(("set_org", upstream_id, status))
        return self.org

    async def set_upstream_team(self, organization_id, department_id, upstream_team_id):
        self.departments[0]["upstreamTeamId"] = upstream_team_id
        self.calls.append(("set_team", upstream_team_id))
        return self.departments[0]

    async def set_upstream_member(self, organization_id, member_id, upstream_user_id, *, status=None):
        self.member["upstreamUserId"] = upstream_user_id
        self.calls.append(("set_member", upstream_user_id))
        return self.member

    async def activate_member_upstream(self, organization_id, member_id, upstream_user_id):
        self.member["status"] = "active"
        self.calls.append(("activate", upstream_user_id))
        return self.member

    async def invitation_token_for_delivery(self, invitation_id):
        return "signed-token"

    async def billing_payload(self, organization_id, *, page=1, page_size=20):
        return {
            "account": {
                "availableBalanceUsd": 50.0,
                "billingStatus": "active",
            }
        }

    async def claim_outbox(self, *, limit=20):
        return self.rows[:limit]

    async def complete_outbox(self, outbox_id, *, error=""):
        self.calls.append(("complete", outbox_id, error))
        return True


class Upstream:
    def __init__(self):
        self.calls = []

    async def list_organizations(self, **kwargs):
        self.calls.append(("list_org", kwargs))
        return []

    async def create_organization(self, alias, **kwargs):
        self.calls.append(("create_org", alias, kwargs))
        return {"organization_id": "org-upstream"}

    async def create_team(self, alias, organization_id, **kwargs):
        self.calls.append(("create_team", alias, organization_id, kwargs))
        return {"team_id": "team-upstream"}

    async def create_internal_user(self, user_id, email, name=None, **kwargs):
        self.calls.append(("create_user", user_id, email, name))
        return {"user_id": "user-upstream"}

    async def add_organization_member(self, organization_id, role, **kwargs):
        self.calls.append(("org_member", organization_id, role, kwargs))
        return {}

    async def add_team_member(self, team_id, role, **kwargs):
        self.calls.append(("team_member", team_id, role, kwargs))
        return {}

    async def organization_info(self, organization_id):
        self.calls.append(("organization_info", organization_id))
        return {"organization_id": organization_id, "spend": 5.0}

    async def update_organization(self, organization_id, **kwargs):
        self.calls.append(("update_organization", organization_id, kwargs))
        return {"organization_id": organization_id, **kwargs}

    async def revoke_organization_key(self, key_id, **kwargs):
        self.calls.append(("revoke_key", key_id, kwargs))
        return {"id": key_id, "deleted": True}


def test_provisions_org_default_team_and_member_memberships():
    repo, upstream = Repo(), Upstream()
    service = OrganizationProvisioningService(repo, upstream)
    result = asyncio.run(service.provision_organization("org-local"))
    assert result["upstreamOrganizationId"] == "org-upstream"
    assert result["departments"] == 1
    assert repo.calls[0] == ("set_org", "org-upstream", "provisioning")
    assert ("set_org", "org-upstream", "active") in repo.calls
    assert (
        "update_organization",
        "org-upstream",
        {
            "max_budget": 55.0,
            "blocked": False,
            "changed_by": "organization-provisioner",
        },
    ) in upstream.calls
    asyncio.run(service.provision_member("org-local", "member-local", auth_user_id="auth-1"))
    assert any(call[0] == "org_member" for call in upstream.calls)
    assert any(call[0] == "team_member" for call in upstream.calls)
    assert repo.member["status"] == "active"


def test_billing_sync_does_not_govern_imported_report_only_assets():
    repo, upstream = Repo(), Upstream()
    service = OrganizationProvisioningService(repo, upstream)

    result = asyncio.run(
        service.sync_billing(
            {
                "upstreamOrganizationId": "org-upstream",
                "balanceUsd": "5000.00",
                "billingStatus": "active",
                "preserveReportOnlyAssets": True,
            }
        )
    )

    assert result["enforcementMode"] == "managed_tokens_only"
    assert result["upstreamOrganizationUnchanged"] is True
    assert not any(call[0] == "organization_info" for call in upstream.calls)
    assert not any(call[0] == "update_organization" for call in upstream.calls)


def test_outbox_invitation_mail_failure_is_retried():
    repo, upstream = Repo(), Upstream()
    repo.rows = [{"id": "out-1", "kind": "organization.invitation.created", "payload": {"invitationId": "inv-1", "email": "A@EXAMPLE.COM"}}]
    sent = []

    async def mailer(email, token, payload):
        sent.append((email, token))
        raise RuntimeError("smtp unavailable")

    service = OrganizationProvisioningService(repo, upstream, mailer=mailer)
    stats = asyncio.run(service.process_outbox())
    assert stats == {"claimed": 1, "completed": 0, "retried": 1}
    assert sent == [("a@example.com", "signed-token")]
    assert repo.calls[-1][2] == "smtp unavailable"


def test_outbox_malformed_json_is_retried_without_aborting_batch():
    repo, upstream = Repo(), Upstream()
    repo.rows = [{"id": "out-json", "kind": "organization.provision", "payload": "{"}]
    service = OrganizationProvisioningService(repo, upstream)

    stats = asyncio.run(service.process_outbox())

    assert stats == {"claimed": 1, "completed": 0, "retried": 1}
    assert repo.calls[-1][0:2] == ("complete", "out-json")


class TokenRepo:
    def __init__(self, *, status="active"):
        self.org = {
            "id": "org-local",
            "upstreamOrganizationId": "org-upstream",
            "status": "active",
            "billingStatus": "active",
        }
        self.token = {
            "id": "token-local",
            "organizationId": "org-local",
            "upstreamKeyId": "hash-upstream",
            "upstreamKeyAlias": "ai-org-stable",
            "upstreamTeamId": "team-upstream",
            "status": status,
        }
        self.rows = []
        self.calls = []

    async def get_token(self, organization_id, token_id):
        self.calls.append(("get_token", organization_id, token_id))
        if (
            self.token is not None
            and organization_id == self.token["organizationId"]
            and token_id == self.token["id"]
        ):
            return self.token
        return None

    async def get_organization(self, organization_id):
        return self.org if organization_id == self.org["id"] else None

    async def mark_token_revoked(self, organization_id, token_id):
        self.calls.append(("mark_revoked", organization_id, token_id))
        self.token["status"] = "revoked"
        return self.token

    async def finalize_token_record(
        self,
        token_id,
        *,
        upstream_key_id,
        upstream_key_hash="",
        status="active",
        plaintext_token=None,
    ):
        self.calls.append(
            (
                "finalize",
                token_id,
                upstream_key_id,
                upstream_key_hash,
                status,
                plaintext_token,
            )
        )
        self.token["upstreamKeyId"] = upstream_key_id
        self.token["status"] = status
        return self.token

    async def claim_outbox(self, *, limit=20):
        return self.rows[:limit]

    async def complete_outbox(self, outbox_id, *, error=""):
        self.calls.append(("complete", outbox_id, error))
        return True


class TokenUpstream:
    def __init__(self, *, fail=False, key_record=None):
        self.fail = fail
        self.key_record = key_record
        self.calls = []

    async def find_organization_key_by_alias(
        self, organization_id, key_alias, **kwargs
    ):
        self.calls.append(("find_key", organization_id, key_alias, kwargs))
        return self.key_record

    def organization_key_identity(self, record):
        return {
            "id": str(record.get("token") or record.get("token_id") or ""),
            "hash": str(record.get("key_hash") or record.get("token") or ""),
            "token": str(record.get("key") or ""),
        }

    async def revoke_organization_key(self, key_id, **kwargs):
        self.calls.append((key_id, kwargs))
        if self.fail:
            raise RuntimeError("upstream timeout")
        return {"id": key_id, "deleted": True}

    async def organization_info(self, organization_id):
        self.calls.append(("organization_info", organization_id))
        return {"organization_id": organization_id, "spend": 10.0}

    async def update_organization(self, organization_id, **kwargs):
        self.calls.append(("update_organization", organization_id, kwargs))
        return {"organization_id": organization_id, **kwargs}


def test_token_revoke_outbox_marks_local_only_after_upstream_success():
    repo = TokenRepo()
    repo.rows = [{
        "id": "outbox-token-revoke",
        "kind": "organization.token.revoke",
        "payload": {
            "organizationId": "org-local",
            "tokenId": "token-local",
            "upstreamKeyId": "hash-upstream",
        },
    }]
    upstream = TokenUpstream()
    service = OrganizationProvisioningService(repo, upstream, changed_by="worker@example.com")

    stats = __import__("asyncio").run(service.process_outbox())

    assert stats == {"claimed": 1, "completed": 1, "retried": 0}
    assert repo.token["status"] == "revoked"
    assert repo.calls[-1] == ("complete", "outbox-token-revoke", "")
    assert upstream.calls == [
        (
            "hash-upstream",
            {"changed_by": "worker@example.com", "idempotency_key": "outbox-token-revoke"},
        )
    ]


def test_token_revoke_outbox_failure_keeps_local_token_active_for_retry():
    repo = TokenRepo()
    repo.rows = [{
        "id": "outbox-token-retry",
        "kind": "organization.token.revoke",
        "payload": {"organizationId": "org-local", "tokenId": "token-local"},
    }]
    upstream = TokenUpstream(fail=True)
    service = OrganizationProvisioningService(repo, upstream)

    stats = __import__("asyncio").run(service.process_outbox())

    assert stats == {"claimed": 1, "completed": 0, "retried": 1}
    assert repo.token["status"] == "active"
    assert not any(call[0] == "mark_revoked" for call in repo.calls)
    assert repo.calls[-1] == ("complete", "outbox-token-retry", "upstream timeout")


def test_token_revoke_outbox_recovers_provisioning_key_by_alias():
    repo = TokenRepo(status="provisioning")
    repo.token["upstreamKeyId"] = None
    repo.rows = [{
        "id": "outbox-token-alias-revoke",
        "kind": "organization.token.revoke",
        "payload": {
            "organizationId": "org-local",
            "tokenId": "token-local",
            "upstreamOrganizationId": "org-upstream",
            "upstreamKeyAlias": "ai-org-stable",
            "upstreamTeamId": "team-upstream",
        },
    }]
    upstream = TokenUpstream(
        key_record={"token": "hash-by-alias", "key_alias": "ai-org-stable"}
    )
    service = OrganizationProvisioningService(repo, upstream)

    stats = asyncio.run(service.process_outbox())

    assert stats == {"claimed": 1, "completed": 1, "retried": 0}
    assert repo.token["status"] == "revoked"
    assert upstream.calls[0] == (
        "find_key",
        "org-upstream",
        "ai-org-stable",
        {"team_id": "team-upstream", "user_id": None},
    )
    assert upstream.calls[1][0] == "hash-by-alias"


def test_token_revoke_outbox_is_idempotent_when_local_token_already_revoked():
    repo = TokenRepo(status="revoked")
    repo.rows = [{
        "id": "outbox-token-repeat",
        "kind": "organization.token.revoke",
        "payload": {"organizationId": "org-local", "tokenId": "token-local"},
    }]
    upstream = TokenUpstream()
    service = OrganizationProvisioningService(repo, upstream)

    stats = __import__("asyncio").run(service.process_outbox())

    assert stats == {"claimed": 1, "completed": 1, "retried": 0}
    assert upstream.calls == []
    assert not any(call[0] == "mark_revoked" for call in repo.calls)


def test_token_reconcile_outbox_recovers_upstream_key_without_plaintext():
    repo = TokenRepo(status="provisioning")
    repo.token["upstreamKeyId"] = None
    repo.rows = [{
        "id": "outbox-token-reconcile",
        "kind": "organization.token.reconcile",
        "payload": {
            "organizationId": "org-local",
            "tokenId": "token-local",
            "upstreamOrganizationId": "org-upstream",
            "upstreamTeamId": "team-upstream",
            "upstreamUserId": "user-upstream",
            "upstreamKeyAlias": "ai-org-stable",
        },
    }]
    upstream = TokenUpstream(
        key_record={"token": "hash-upstream", "key_alias": "ai-org-stable"}
    )
    service = OrganizationProvisioningService(repo, upstream)

    stats = asyncio.run(service.process_outbox())

    assert stats == {"claimed": 1, "completed": 1, "retried": 0}
    assert repo.token["status"] == "active"
    assert repo.token["upstreamKeyId"] == "hash-upstream"
    assert (
        "finalize",
        "token-local",
        "hash-upstream",
        "hash-upstream",
        "active",
        None,
    ) in repo.calls
    assert not any(
        isinstance(call, tuple) and call[0] == "revoke_key"
        for call in upstream.calls
    )


def test_token_reconcile_outbox_cleans_key_when_local_record_disappeared():
    repo = TokenRepo(status="provisioning")
    repo.token = None
    repo.rows = [{
        "id": "outbox-token-orphan",
        "kind": "organization.token.reconcile",
        "payload": {
            "organizationId": "org-local",
            "tokenId": "token-local",
            "upstreamOrganizationId": "org-upstream",
            "upstreamKeyAlias": "ai-org-stable",
        },
    }]
    upstream = TokenUpstream(key_record={"token": "hash-orphan"})
    service = OrganizationProvisioningService(repo, upstream)

    stats = asyncio.run(service.process_outbox())

    assert stats == {"claimed": 1, "completed": 1, "retried": 0}
    assert upstream.calls[-1] == (
        "hash-orphan",
        {
            "changed_by": "organization-provisioner",
            "idempotency_key": "outbox-token-orphan",
        },
    )


def test_billing_sync_extends_lifetime_ceiling_by_available_balance():
    repo = TokenRepo()
    upstream = TokenUpstream()
    service = OrganizationProvisioningService(repo, upstream)

    result = asyncio.run(
        service.sync_billing(
            {
                "upstreamOrganizationId": "org-upstream",
                "balanceUsd": "90.00",
                "billingStatus": "active",
            }
        )
    )

    assert result == {
        "upstreamOrganizationId": "org-upstream",
        "upstreamSpend": 10.0,
        "maxBudget": 100.0,
        "blocked": False,
    }
    assert upstream.calls[-1] == (
        "update_organization",
        "org-upstream",
        {
            "max_budget": 100.0,
            "blocked": False,
            "changed_by": "organization-provisioner",
        },
    )


def test_zero_balance_billing_sync_keeps_ceiling_at_current_spend():
    repo = TokenRepo()
    upstream = TokenUpstream()
    service = OrganizationProvisioningService(repo, upstream)

    result = asyncio.run(
        service.sync_billing(
            {
                "upstreamOrganizationId": "org-upstream",
                "balanceUsd": "0",
                "billingStatus": "past_due",
            }
        )
    )

    assert result["maxBudget"] == 10.0
    assert result["blocked"] is True
    assert upstream.calls[-1][2]["max_budget"] == 10.0


def test_billing_sync_retries_when_upstream_spend_is_unavailable():
    class BrokenBillingUpstream(TokenUpstream):
        async def organization_info(self, organization_id):
            raise RuntimeError("proxy unavailable")

    repo = TokenRepo()
    repo.rows = [{
        "id": "outbox-billing-retry",
        "kind": "organization.billing.sync",
        "payload": {
            "upstreamOrganizationId": "org-upstream",
            "balanceUsd": "50",
            "billingStatus": "active",
        },
    }]
    service = OrganizationProvisioningService(repo, BrokenBillingUpstream())

    stats = asyncio.run(service.process_outbox())

    assert stats == {"claimed": 1, "completed": 0, "retried": 1}
    assert repo.calls[-1] == (
        "complete",
        "outbox-billing-retry",
        "proxy unavailable",
    )


def test_invited_member_loses_upstream_access_until_invitation_is_accepted():
    class InvitationRemovalUpstream(Upstream):
        async def delete_organization_member(self, organization_id, **kwargs):
            self.calls.append(("delete_org_member", organization_id, kwargs))
            return {}

        async def delete_team_member(self, team_id, **kwargs):
            self.calls.append(("delete_team_member", team_id, kwargs))
            return {}

    repo = Repo()
    repo.org["upstreamOrganizationId"] = "org-upstream"
    repo.departments[0]["upstreamTeamId"] = "team-upstream"
    repo.member["status"] = "invited"
    repo.member["upstreamUserId"] = "user-upstream"
    upstream = InvitationRemovalUpstream()
    service = OrganizationProvisioningService(repo, upstream)

    result = asyncio.run(service.sync_member({
        "organizationId": "org-local",
        "memberId": "member-local",
        "status": "invited",
        "upstreamUserId": "user-upstream",
    }))

    assert result["status"] == "invited"
    assert upstream.calls == [
        (
            "delete_org_member",
            "org-upstream",
            {"user_id": "user-upstream", "changed_by": "organization-provisioner"},
        ),
        (
            "delete_team_member",
            "team-upstream",
            {"user_id": "user-upstream", "changed_by": "organization-provisioner"},
        ),
    ]
