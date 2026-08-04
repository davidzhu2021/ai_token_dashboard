"""Unit coverage for the real PostgreSQL organization repository."""

from __future__ import annotations

import hashlib
import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from backend.organization_repository import (
    ORGANIZATION_SCHEMA,
    PostgreSQLOrganizationRepository,
)
from backend.organization_validation import OrganizationConflictError
from backend.organization_validation import OrganizationValidationError


def test_repository_is_disabled_without_real_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USAGE_DATABASE_URL", raising=False)
    monkeypatch.setenv("ORGANIZATION_MODE", "demo")
    assert PostgreSQLOrganizationRepository.from_environment() is None


def test_repository_uses_usage_database_in_real_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORGANIZATION_MODE", "real")
    monkeypatch.setenv("USAGE_DATABASE_URL", "postgresql://example.invalid/db")
    repository = PostgreSQLOrganizationRepository.from_environment()
    assert repository is not None
    assert repository.dsn.endswith("/db")


def test_invitation_hash_is_stable_and_secret_aware(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "one-time-invite"
    expected = hashlib.sha256(token.encode()).hexdigest()
    monkeypatch.delenv("ORGANIZATION_INVITATION_SECRET", raising=False)
    assert PostgreSQLOrganizationRepository.invitation_hash(token) == expected
    monkeypatch.setenv("ORGANIZATION_INVITATION_SECRET", "unit-test-secret")
    signed = PostgreSQLOrganizationRepository.invitation_hash(token)
    assert signed != expected
    assert signed == PostgreSQLOrganizationRepository.invitation_hash(token)


def test_signed_invitation_token_can_be_recreated_for_outbox_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORGANIZATION_INVITATION_SECRET", "delivery-secret")
    invitation_id = "0123456789abcdef0123456789abcdef"
    first = PostgreSQLOrganizationRepository._invitation_token(invitation_id)
    second = PostgreSQLOrganizationRepository._invitation_token(invitation_id)
    assert first == second
    assert first.startswith(f"{invitation_id}.")


def test_schema_contains_durable_security_and_idempotency_constraints() -> None:
    assert "customer_invitation" in ORGANIZATION_SCHEMA
    assert "token_hash TEXT NOT NULL UNIQUE" in ORGANIZATION_SCHEMA
    assert "consumed_at IS NULL" in ORGANIZATION_SCHEMA
    assert "customer_outbox" in ORGANIZATION_SCHEMA
    assert "UNIQUE(organization_id, idempotency_key)" in ORGANIZATION_SCHEMA
    assert "WHERE external_reference <> ''" in ORGANIZATION_SCHEMA
    assert "upstream_organization_id" in ORGANIZATION_SCHEMA
    assert "upstream_team_id" in ORGANIZATION_SCHEMA


def test_schema_treats_existing_unique_constraint_indexes_as_idempotent() -> None:
    # PostgreSQL reports an existing UNIQUE constraint's backing relation as
    # duplicate_table on reconnect, while ADD CONSTRAINT may use
    # duplicate_object. Real-mode startup must tolerate both.
    assert "WHEN duplicate_object OR duplicate_table THEN NULL;" in ORGANIZATION_SCHEMA
    assert "customer_principal_upstream_identity" in ORGANIZATION_SCHEMA
    assert "customer_principal_identity_org_principal_fk" in ORGANIZATION_SCHEMA
    assert "customer_usage_key_identity_org_principal_fk" in ORGANIZATION_SCHEMA
    assert "principal_id TEXT NOT NULL" in ORGANIZATION_SCHEMA
    assert "customer_usage_identity" in ORGANIZATION_SCHEMA
    assert "customer_usage_key_identity" in ORGANIZATION_SCHEMA
    assert "customer_adoption_operation" in ORGANIZATION_SCHEMA
    assert "operation_key TEXT NOT NULL UNIQUE" in ORGANIZATION_SCHEMA
    assert "UNIQUE (backend_id, upstream_key_hash)" in ORGANIZATION_SCHEMA
    assert "CHECK (upstream_key_hash ~ '^[0-9a-f]{64}$')" in ORGANIZATION_SCHEMA
    assert "effective_from TIMESTAMPTZ NOT NULL" in ORGANIZATION_SCHEMA
    assert "effective_through TIMESTAMPTZ NOT NULL" in ORGANIZATION_SCHEMA
    assert "mode <> 'report_only' OR billing_eligible = FALSE" in ORGANIZATION_SCHEMA
    assert "customer_member_org_id_unique" in ORGANIZATION_SCHEMA
    assert "customer_department_org_id_unique" in ORGANIZATION_SCHEMA
    assert "billing_balance_usd NUMERIC(16,6) NOT NULL DEFAULT 0" in ORGANIZATION_SCHEMA
    assert "billing_effective_at TIMESTAMPTZ" in ORGANIZATION_SCHEMA
    assert "ORDER BY l.created_at DESC, l.id DESC" in ORGANIZATION_SCHEMA
    assert "audit_action" in ORGANIZATION_SCHEMA
    assert "upstream_user_id" in ORGANIZATION_SCHEMA
    assert "auth_user_id" in ORGANIZATION_SCHEMA
    assert "customer_token_alias_idx" in ORGANIZATION_SCHEMA
    assert "ON customer_access_token(upstream_key_alias)" in ORGANIZATION_SCHEMA
    assert "last_sent_at" in ORGANIZATION_SCHEMA
    assert "upstream_team_id" in ORGANIZATION_SCHEMA
    assert "locked_at TIMESTAMPTZ" in ORGANIZATION_SCHEMA
    assert "lease_token TEXT NOT NULL DEFAULT ''" in ORGANIZATION_SCHEMA


def test_payload_helpers_are_json_friendly() -> None:
    row = {
        "id": "token-1",
        "organization_id": "org-1",
        "member_id": None,
        "department_id": None,
        "upstream_team_id": "",
        "name": "shared",
        "models": ["gpt-5"],
        "status": "active",
        "daily_budget_usd": Decimal("12.50"),
        "duration": "never",
        "upstream_key_id": "key-1",
        "upstream_key_hash": "sk-...abcd",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "expires_at": None,
        "revoked_at": None,
    }
    payload = PostgreSQLOrganizationRepository._token_payload(row, secret=None)
    assert payload["masked"] == "sk-...abcd"
    assert payload["dailyBudgetUsd"] == 12.5
    assert payload["memberId"] == ""
    assert payload["memberName"] == ""
    assert payload["departmentName"] == ""
    assert payload["isShared"] is True


def test_token_payload_includes_joined_member_and_department_fields() -> None:
    row = {
        "id": "token-1",
        "organization_id": "org-1",
        "member_id": "member-1",
        "member_name": "Alice",
        "member_email": "alice@example.com",
        "department_id": "dept-1",
        "department_name": "Engineering",
        "upstream_team_id": "team-1",
        "name": "alice-key",
        "models": ["gpt-5"],
        "status": "active",
        "daily_budget_usd": Decimal("8.00"),
        "duration": "never",
        "upstream_key_id": "key-1",
        "upstream_key_hash": "abcd",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "expires_at": None,
        "revoked_at": None,
    }

    payload = PostgreSQLOrganizationRepository._token_payload(row, secret=None)

    assert payload["memberName"] == "Alice"
    assert payload["memberEmail"] == "alice@example.com"
    assert payload["departmentName"] == "Engineering"
    assert payload["isShared"] is False


def test_compatibility_aliases_and_resolvers_are_async() -> None:
    import inspect

    assert inspect.iscoroutinefunction(PostgreSQLOrganizationRepository.organization_snapshot)
    assert inspect.iscoroutinefunction(PostgreSQLOrganizationRepository.resolve_member_by_email)
    assert inspect.iscoroutinefunction(PostgreSQLOrganizationRepository.resolve_members_by_email)
    assert inspect.iscoroutinefunction(PostgreSQLOrganizationRepository.bind_member_account)
    assert inspect.iscoroutinefunction(PostgreSQLOrganizationRepository.ensure_principal)
    assert inspect.iscoroutinefunction(PostgreSQLOrganizationRepository.attach_principal_upstream_identity)
    assert inspect.iscoroutinefunction(PostgreSQLOrganizationRepository.link_principal_member)


def test_list_organizations_includes_directory_card_stats() -> None:
    now = datetime(2026, 8, 4, tzinfo=timezone.utc)

    class Pool:
        async def fetchval(self, query, *args):
            assert "FROM customer_organization o" in query
            assert args == ("%baic%", "active")
            return 1

        async def fetch(self, query, *args):
            assert "AS department_count" in query
            assert "AS active_member_count" in query
            assert "d.status='active'" in query
            assert "o.name ILIKE $1" in query
            assert "o.status = $2" in query
            assert args == ("%baic%", "active", 12, 12)
            return [{
                "id": "org-1", "name": "BAIC", "status": "active",
                "billing_status": "active", "billing_balance_usd": Decimal("100.00"),
                "billing_effective_at": None, "upstream_organization_id": "upstream-org-1",
                "upstream_status": "active", "created_at": now, "updated_at": now,
                "archived_at": None, "department_count": 2, "member_count": 4,
                "active_member_count": 3, "invited_member_count": 1,
                "suspended_member_count": 0, "active_admin_count": 1,
            }]

    repository = PostgreSQLOrganizationRepository("postgresql://unused")
    repository.pool = Pool()
    result = asyncio.run(repository.list_organizations(
        keyword="baic", status="active", page=2, page_size=12
    ))

    assert result["total"] == 1
    assert result["page"] == 2
    assert result["items"][0]["stats"] == {
        "departmentCount": 2, "memberCount": 4, "activeMemberCount": 3,
        "invitedMemberCount": 1, "suspendedMemberCount": 0, "activeAdminCount": 1,
    }


def test_billing_operation_validation_happens_before_database_access() -> None:
    repository = PostgreSQLOrganizationRepository("postgresql://unused")
    with pytest.raises(OrganizationConflictError, match="unsupported"):
        asyncio.run(repository.adjust_billing(
            "org-1", operation="cash", amount_usd="1.00", idempotency_key="bad-op"
        ))
    with pytest.raises(OrganizationConflictError, match="0.01"):
        asyncio.run(repository.adjust_billing(
            "org-1", operation="grant", amount_usd="0.001", idempotency_key="bad-money"
        ))


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _PrincipalConnection:
    def __init__(self) -> None:
        now = datetime(2026, 8, 3, tzinfo=timezone.utc)
        self.organization = {"id": "org-1"}
        self.principals: dict[str, dict] = {}
        self.identities: dict[tuple[str, str], dict] = {}
        self.now = now

    def transaction(self):
        return _Transaction()

    async def fetchrow(self, query, *args):
        if "FROM customer_organization" in query:
            return self.organization if args[0] == "org-1" else None
        if "FROM customer_principal WHERE organization_id=$1 AND id=$2" in query:
            row = self.principals.get(args[1])
            return dict(row) if row and row["organization_id"] == args[0] else None
        if "FROM customer_principal WHERE organization_id=$1 AND (id=$2" in query:
            organization_id, principal_id, name = args
            for row in self.principals.values():
                if row["organization_id"] == organization_id and (
                    row["id"] == principal_id or row["name"].casefold() == name.casefold()
                ):
                    return dict(row)
            return None
        if "INSERT INTO customer_principal" in query:
            row = {
                "id": args[0],
                "organization_id": args[1],
                "member_id": args[2],
                "name": args[3],
                "status": args[4],
                "created_at": self.now,
                "updated_at": self.now,
            }
            self.principals[row["id"]] = row
            return dict(row)
        if "SELECT * FROM customer_principal WHERE id=$1" in query:
            row = self.principals.get(args[0])
            if row is None or (len(args) > 1 and row["organization_id"] != args[1]):
                return None
            return dict(row)
        if "FROM customer_principal_upstream_identity i" in query:
            row = self.identities.get((args[0], args[1]))
            if row is None:
                return None
            principal = self.principals[row["principal_id"]]
            return {**row, "principal_organization_id": principal["organization_id"]}
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def fetch(self, query, *args):
        if "FROM customer_principal_upstream_identity" in query:
            return [
                {"upstream_user_id": row["upstream_user_id"]}
                for row in self.identities.values()
                if row["organization_id"] == args[0] and row["principal_id"] == args[1]
            ]
        raise AssertionError(f"unexpected fetch: {query}")

    async def execute(self, query, *args):
        if "INSERT INTO customer_principal_upstream_identity" in query:
            row = {
                "id": args[0],
                "organization_id": args[1],
                "principal_id": args[2],
                "backend_id": args[3],
                "upstream_user_id": args[4],
            }
            self.identities[(args[3], args[4])] = row
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute: {query}")


class _PrincipalPool:
    def __init__(self, connection: _PrincipalConnection) -> None:
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)

    async def fetchrow(self, query, *args):
        if "WHERE organization_id=$1 AND id=$2" in query:
            row = self.connection.principals.get(args[1])
            return dict(row) if row and row["organization_id"] == args[0] else None
        return await self.connection.fetchrow(query, *args)

    async def fetch(self, query, *args):
        return await self.connection.fetch(query, *args)


def test_multiple_upstream_user_ids_merge_into_one_principal() -> None:
    connection = _PrincipalConnection()
    repository = PostgreSQLOrganizationRepository("postgresql://unused")
    repository.pool = _PrincipalPool(connection)

    principal = asyncio.run(repository.ensure_principal("org-1", "梁海强"))
    asyncio.run(
        repository.attach_principal_upstream_identity(
            principal["id"],
            organization_id="org-1",
            backend_id="primary",
            upstream_user_id="claude-code-lianghaiqiang",
        )
    )
    result = asyncio.run(
        repository.attach_principal_upstream_identity(
            principal["id"],
            organization_id="org-1",
            backend_id="primary",
            upstream_user_id="cursor-lianghaiqiang",
        )
    )

    assert result["organizationId"] == "org-1"
    assert result["upstreamUserIds"] == [
        "claude-code-lianghaiqiang",
        "cursor-lianghaiqiang",
    ]


def test_upstream_identity_cannot_move_between_organizations() -> None:
    connection = _PrincipalConnection()
    connection.principals.update(
        {
            "principal-1": {
                "id": "principal-1",
                "organization_id": "org-1",
                "member_id": None,
                "name": "梁海强",
                "status": "pending",
                "created_at": connection.now,
                "updated_at": connection.now,
            },
            "principal-2": {
                "id": "principal-2",
                "organization_id": "org-2",
                "member_id": None,
                "name": "Other",
                "status": "pending",
                "created_at": connection.now,
                "updated_at": connection.now,
            },
        }
    )
    connection.identities[("primary", "same-user-id")] = {
        "id": "identity-1",
        "organization_id": "org-1",
        "principal_id": "principal-1",
        "backend_id": "primary",
        "upstream_user_id": "same-user-id",
    }
    repository = PostgreSQLOrganizationRepository("postgresql://unused")
    repository.pool = _PrincipalPool(connection)

    with pytest.raises(OrganizationConflictError, match="another organization"):
        asyncio.run(
            repository.attach_principal_upstream_identity(
                "principal-2",
                organization_id="org-2",
                backend_id="primary",
                upstream_user_id="same-user-id",
            )
        )


class _InvitationConnection:
    def __init__(self, *, member_status="invited", invitation=None):
        self.member_status = member_status
        self.invitation = invitation
        self.inserted_invitation = None
        self.executed = []

    def transaction(self):
        return _Transaction()

    async def fetchrow(self, query, *args):
        if "FROM customer_member" in query:
            return {
                "id": args[0],
                "organization_id": args[1],
                "email": "admin@example.com",
                "status": self.member_status,
            }
        if "FROM customer_invitation" in query:
            return self.invitation
        if "INSERT INTO customer_invitation" in query:
            self.inserted_invitation = {
                "id": args[0],
                "organization_id": args[1],
                "member_id": args[2],
                "email": args[3],
                "expires_at": args[5],
            }
            self.invitation = self.inserted_invitation
            return self.inserted_invitation
        raise AssertionError(f"unexpected query: {query}")

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "INSERT 0 1"


class _InvitationPool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


def test_ensure_member_invitation_reuses_valid_link_and_repairs_outbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORGANIZATION_INVITATION_SECRET", "delivery-secret")
    connection = _InvitationConnection()
    repository = PostgreSQLOrganizationRepository("postgresql://unused")
    repository.pool = _InvitationPool(connection)

    first = asyncio.run(repository.ensure_member_invitation("org-1", "member-1"))
    second = asyncio.run(repository.ensure_member_invitation("org-1", "member-1"))

    assert first is not None
    assert first["id"] == second["id"]
    assert first["memberId"] == "member-1"
    assert connection.inserted_invitation is not None
    # Each retry attempts the deterministic outbox insert, but never creates a
    # second invitation row or rotates the token.
    assert sum("INSERT INTO customer_outbox" in query for query, _ in connection.executed) == 2


def test_ensure_member_invitation_skips_non_invited_members() -> None:
    connection = _InvitationConnection(member_status="active")
    repository = PostgreSQLOrganizationRepository("postgresql://unused")
    repository.pool = _InvitationPool(connection)

    result = asyncio.run(repository.ensure_member_invitation("org-1", "member-1"))

    assert result is None
    assert connection.inserted_invitation is None
    assert connection.executed == []


class _ActivationConnection:
    def __init__(self, status: str = "active", upstream_user_id: str = "user-upstream"):
        self.row = {
            "id": "member-1",
            "organization_id": "org-1",
            "status": status,
            "upstream_user_id": upstream_user_id,
        }
        self.updated = 0

    def transaction(self):
        return _Transaction()

    async def fetchrow(self, query, *args):
        if "SELECT * FROM customer_member" in query:
            return dict(self.row)
        if "UPDATE customer_member SET status='active'" in query:
            self.updated += 1
            self.row.update(status="active", upstream_user_id=args[2])
            return dict(self.row)
        raise AssertionError(f"unexpected query: {query}")


class _ActivationPool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


def test_activate_member_upstream_is_idempotent_after_partial_success(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _ActivationConnection(status="active", upstream_user_id="user-upstream")
    repository = PostgreSQLOrganizationRepository("postgresql://unused")
    repository.pool = _ActivationPool(connection)
    monkeypatch.setattr(
        PostgreSQLOrganizationRepository,
        "_member_payload",
        staticmethod(lambda row: dict(row)),
    )

    result = asyncio.run(
        repository.activate_member_upstream("org-1", "member-1", "user-upstream")
    )

    assert result["status"] == "active"
    assert connection.updated == 0


def test_activate_member_upstream_rejects_conflicting_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _ActivationConnection(status="active", upstream_user_id="different-user")
    repository = PostgreSQLOrganizationRepository("postgresql://unused")
    repository.pool = _ActivationPool(connection)

    with pytest.raises(OrganizationConflictError, match="mapping conflicts"):
        asyncio.run(
            repository.activate_member_upstream("org-1", "member-1", "user-upstream")
        )


class _SettlementPool:
    def __init__(self):
        self.calls = []

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        if "upstream_organization_id" in query:
            if args[0] == "org-upstream":
                return {
                    "id": "org-local",
                    "billing_effective_at": datetime(
                        2026, 7, 29, 12, tzinfo=timezone.utc
                    ),
                }
            return None
        raise AssertionError(f"unexpected query: {query}")


def test_settle_usage_rows_maps_upstream_ids_and_replays_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = PostgreSQLOrganizationRepository("postgresql://unused")
    repository.pool = _SettlementPool()
    calls = []

    async def settle(organization_id, usage_date, amount_usd, **kwargs):
        calls.append((organization_id, usage_date, amount_usd, kwargs))
        return {"idempotent": len(calls) > 1}

    monkeypatch.setattr(repository, "settle_usage", settle)
    rows = [
        {
            "upstreamOrganizationId": "org-upstream",
            "usageDate": "2026-07-30",
            "spendUsd": "3.21",
        },
        {
            "upstreamOrganizationId": "org-missing",
            "usageDate": "2026-07-30",
            "spendUsd": "1.00",
        },
    ]

    first = asyncio.run(repository.settle_usage_rows(rows))
    second = asyncio.run(repository.settle_usage_rows(rows[:1]))

    assert first == {"processed": 1, "settled": 1, "idempotent": 0, "unmapped": 1}
    assert second == {"processed": 1, "settled": 0, "idempotent": 1, "unmapped": 0}
    assert calls[0] == (
        "org-local",
        "2026-07-30",
        "3.21",
        {"upstream_organization_id": "org-upstream"},
    )


def test_settle_usage_rows_skips_credit_day_and_pre_credit_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = PostgreSQLOrganizationRepository("postgresql://unused")
    repository.pool = _SettlementPool()
    calls = []

    async def settle(*args, **kwargs):
        calls.append((args, kwargs))
        return {"idempotent": False}

    monkeypatch.setattr(repository, "settle_usage", settle)
    result = asyncio.run(
        repository.settle_usage_rows(
            [
                {
                    "upstreamOrganizationId": "org-upstream",
                    "usageDate": "2026-07-28",
                    "spendUsd": "9.05",
                },
                {
                    "upstreamOrganizationId": "org-upstream",
                    "usageDate": "2026-07-29",
                    "spendUsd": "1.00",
                },
            ]
        )
    )

    assert result == {
        "processed": 0,
        "settled": 0,
        "idempotent": 0,
        "unmapped": 0,
        "skipped": 1,
        "skipReasons": {"needs_event_time": 1},
    }
    assert calls == []


def test_settlement_health_uses_six_decimal_precision() -> None:
    class Pool:
        async def fetchrow(self, query, *_args):
            assert "ROUND(COALESCE(sum(u.spend), 0)::numeric, 6)" in query
            return {
                "settled_count": 1,
                "latest_date": None,
                "settled_total": Decimal("1.234567"),
                "mismatch_count": 1,
                "mismatch_total": Decimal("0.000001"),
            }

    repository = PostgreSQLOrganizationRepository("postgresql://unused")
    repository.pool = Pool()

    result = asyncio.run(repository.settlement_health())

    assert result["settledTotalUsd"] == 1.234567
    assert result["reconciliationDifferenceUsd"] == 0.000001


class _OscillationSettlementConnection:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 3, tzinfo=timezone.utc)
        self.organization = {
            "id": "org-local",
            "status": "active",
            "billing_status": "active",
            "billing_balance_usd": Decimal("100.000000"),
            "billing_effective_at": datetime(
                2026, 7, 29, 12, tzinfo=timezone.utc
            ),
            "upstream_organization_id": "org-upstream",
        }
        self.settlement = None
        self.ledger: dict[str, dict] = {}
        self.outbox_ids: set[str] = set()

    def transaction(self):
        return _Transaction()

    async def fetchrow(self, query, *args):
        if "FROM customer_organization WHERE id=$1 FOR UPDATE" in query:
            return dict(self.organization)
        if "FROM customer_usage_settlement" in query:
            return dict(self.settlement) if self.settlement else None
        if "INSERT INTO customer_billing_ledger" in query:
            if "ON CONFLICT (organization_id,idempotency_key)" not in query:
                (
                    row_id,
                    organization_id,
                    amount,
                    balance,
                    reason,
                    operator,
                    external_reference,
                    idempotency_key,
                ) = args
                operation = "charge"
            else:
                (
                    row_id,
                    organization_id,
                    operation,
                    amount,
                    balance,
                    reason,
                    operator,
                    idempotency_key,
                ) = args
                external_reference = idempotency_key
                assert "ON CONFLICT (organization_id,idempotency_key) DO UPDATE" in query
            existing = self.ledger.get(idempotency_key)
            if existing is None:
                existing = {
                    "id": row_id,
                    "organization_id": organization_id,
                    "operation": operation,
                    "amount_usd": Decimal(str(amount)),
                    "balance_after_usd": Decimal(str(balance)),
                    "reason": reason,
                    "operator": operator,
                    "operator_email": "",
                    "external_reference": external_reference,
                    "idempotency_key": idempotency_key,
                    "created_at": self.now,
                }
                self.ledger[idempotency_key] = existing
            else:
                existing["amount_usd"] += Decimal(str(amount))
                existing["operation"] = (
                    "charge" if existing["amount_usd"] < 0 else "credit"
                )
                existing["balance_after_usd"] = Decimal(str(balance))
                existing["reason"] = reason
                existing["operator"] = operator
            return dict(existing)
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def fetchval(self, query, *_args):
        if "customer_usage_key_identity" in query:
            return False
        raise AssertionError(f"unexpected fetchval: {query}")

    async def execute(self, query, *args):
        if query.startswith("INSERT INTO customer_usage_settlement"):
            self.settlement = {
                "organization_id": args[0],
                "usage_date": args[1],
                "upstream_organization_id": args[2],
                "settled_amount_usd": Decimal(str(args[3])),
            }
            return "INSERT 0 1"
        if query.startswith("UPDATE customer_usage_settlement"):
            self.settlement["settled_amount_usd"] = Decimal(str(args[2]))
            self.settlement["upstream_organization_id"] = args[3]
            return "UPDATE 1"
        if query.startswith("UPDATE customer_organization SET billing_status"):
            self.organization["billing_status"] = args[1]
            self.organization["billing_balance_usd"] = Decimal(str(args[2]))
            return "UPDATE 1"
        if query.startswith("INSERT INTO customer_outbox"):
            self.outbox_ids.add(args[0])
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute: {query}")


class _OscillationSettlementPool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


def test_daily_settlement_upserts_stable_adjustment_through_a_b_a_oscillation() -> None:
    connection = _OscillationSettlementConnection()
    repository = PostgreSQLOrganizationRepository("postgresql://unused")
    repository.pool = _OscillationSettlementPool(connection)

    for amount in ("1.000000", "2.000000", "1.000000", "2.000000"):
        asyncio.run(
            repository.settle_usage(
                "org-local",
                "2026-07-30",
                amount,
                upstream_organization_id="org-upstream",
            )
        )

    assert connection.organization["billing_balance_usd"] == Decimal("98.000000")
    assert connection.settlement["settled_amount_usd"] == Decimal("2.000000")
    assert set(connection.ledger) == {
        "usage:org-local:2026-07-30",
        "usage-adjustment:org-local:2026-07-30",
    }
    assert connection.ledger[
        "usage-adjustment:org-local:2026-07-30"
    ]["amount_usd"] == Decimal("-1.000000")
    # Every real balance transition gets a new projection job even though the
    # accounting adjustment itself uses one stable org-day idempotency key.
    assert len(connection.outbox_ids) == 4


def test_direct_settlement_exposes_needs_event_time_on_credit_day() -> None:
    connection = _OscillationSettlementConnection()
    repository = PostgreSQLOrganizationRepository("postgresql://unused")
    repository.pool = _OscillationSettlementPool(connection)

    result = asyncio.run(
        repository.settle_usage(
            "org-local",
            "2026-07-29",
            "1.000000",
            upstream_organization_id="org-upstream",
        )
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "needs_event_time"
    assert result["amountUsd"] == 0.0
    assert connection.ledger == {}
    assert connection.settlement is None


def test_atomic_balance_snapshot_is_used_for_token_and_ledger_writes() -> None:
    import inspect

    create_source = inspect.getsource(PostgreSQLOrganizationRepository.create_token_record)
    finalize_source = inspect.getsource(PostgreSQLOrganizationRepository.finalize_token_record)
    adjust_source = inspect.getsource(PostgreSQLOrganizationRepository.adjust_billing)
    settle_source = inspect.getsource(PostgreSQLOrganizationRepository.settle_usage)

    assert "billing_balance_usd AS balance" in create_source
    assert "billing_balance_usd AS balance" in finalize_source
    assert 'organization["billing_balance_usd"]' in adjust_source
    assert "billing_balance_usd=$3" in adjust_source
    assert "billing_effective_at=CASE" in adjust_source
    assert "beforeBillingEffectiveAt" not in adjust_source
    assert 'organization["billing_balance_usd"]' in settle_source
    assert "billing_balance_usd=$3" in settle_source
    assert "billing_effective_at" in settle_source
    assert "beforeBillingEffectiveAt" in settle_source
    # Once the organization row is locked, ledger tail ordering must no longer
    # be the authority for the current balance.
    assert "SELECT balance_after_usd" not in adjust_source
    assert "SELECT balance_after_usd" not in settle_source


def test_billing_adjustment_rejects_changed_idempotent_payload() -> None:
    import inspect

    source = inspect.getsource(PostgreSQLOrganizationRepository.adjust_billing)

    assert "existing_amount != delta" in source
    assert 'existing["operation"]' in source
    assert 'existing["reason"]' in source
    assert 'existing["external_reference"]' in source
    assert "already used for another adjustment" in source


def test_member_department_move_revokes_keys_bound_to_the_previous_team() -> None:
    import inspect

    source = inspect.getsource(PostgreSQLOrganizationRepository.update_member)

    assert "SELECT status, department_id FROM customer_member" in source
    assert 'revocation_reason = "member_department_changed"' in source
    assert "member_id=member_id" in source


def test_upstream_organization_projection_fails_closed_for_disabled_tenants() -> None:
    import inspect

    source = inspect.getsource(PostgreSQLOrganizationRepository.set_upstream_organization)

    assert "FOR UPDATE" in source
    assert 'local_status in {"suspended", "archived"}' in source
    assert 'next_local_status = "active" if upstream_status == "active"' in source


def test_invitation_queries_require_active_upstream_organization() -> None:
    import inspect

    verify_source = inspect.getsource(PostgreSQLOrganizationRepository.verify_invitation)
    accept_source = inspect.getsource(PostgreSQLOrganizationRepository.accept_invitation)

    assert "o.status='active'" in verify_source
    assert "o.upstream_status='active'" in verify_source
    assert "FOR UPDATE OF i, o, m" in accept_source
    assert 'row["upstream_status"]' in accept_source


def test_invitation_accept_rejects_an_auth_account_already_bound_elsewhere() -> None:
    import inspect

    source = inspect.getsource(PostgreSQLOrganizationRepository.accept_invitation)

    assert "WHERE auth_user_id=$1 AND auth_user_id<>'' FOR UPDATE" in source
    assert "customer_member_auth_user_idx" in source
    assert "auth account already belongs to another customer organization" in source


def test_baic_reconciliation_is_strict_local_atomic_and_does_not_sync_upstream() -> None:
    import inspect

    source = inspect.getsource(PostgreSQLOrganizationRepository.reconcile_baic_pilot_state)
    assert "expected_current_upstream_organization_id" in source
    assert "expected_current_upstream_team_id" in source
    assert "target_upstream_organization_id" in source
    assert "target_upstream_team_id" in source
    assert "customer_organization WHERE id=$1 FOR UPDATE" in source
    assert "customer_department " in source and "FOR UPDATE" in source
    assert "customer_member " in source and "FOR UPDATE" in source
    assert "customer_access_token" in source
    assert "managed tokens" in source
    assert "customer_usage_settlement" in source
    assert "settled usage" in source
    assert "expected_report_only_keys" in source
    assert "_validate_baic_key_ownership" in source
    assert "_baic_pilot_credit_to_adopt" in source
    credit_source = inspect.getsource(
        PostgreSQLOrganizationRepository._baic_pilot_credit_to_adopt
    )
    assert "billing balance does not match the ledger" in credit_source
    assert "BAIC billing ledger is not the expected untouched pilot credit" in credit_source
    assert "baic-pilot-initial-credit-v1" in source
    assert "BAIC-PILOT-INITIAL-5000" in source
    assert "adoptedInitialCreditId" in source
    assert "organization.baic_pilot.reconciled" in source
    assert "organization.member.provision" in source
    # Mapping repair must not enqueue a normal org/dept/billing projection:
    # those jobs could mutate the old upstream scope before report-only import.
    assert '"organization.billing.sync"' not in source
    assert '"organization.sync"' not in source
    assert '"department.sync"' not in source


def _expected_baic_keys() -> list[dict[str, str]]:
    return [
        {
            "backendId": "primary",
            "upstreamKeyHash": "a" * 64,
            "upstreamKeyId": "key-a",
        },
        {
            "backendId": "primary",
            "upstreamKeyHash": "b" * 64,
            "upstreamKeyId": "key-b",
        },
    ]


def _baic_reconciliation_kwargs(
    expected_keys: list[dict[str, str]],
) -> dict[str, object]:
    return {
        "organization_id": "org-baic",
        "department_id": "dept-baic",
        "member_id": "member-david",
        "expected_organization_name": "北汽",
        "expected_admin_email": "davidzhu2021@163.com",
        "expected_current_upstream_organization_id": "org-old",
        "expected_current_upstream_team_id": "team-old",
        "target_upstream_organization_id": "org-target",
        "target_upstream_team_id": "team-target",
        "organization_name": "北汽集团",
        "department_name": "企业管理",
        "member_name": "David Zhu",
        "operation_key": "baic-pilot-adoption-v1",
        "expected_report_only_keys": expected_keys,
    }


@pytest.mark.parametrize(
    "expected_keys",
    [
        [],
        [_expected_baic_keys()[0]],
        [*_expected_baic_keys(), {**_expected_baic_keys()[0], "upstreamKeyHash": "c" * 64}],
    ],
)
def test_baic_reconciliation_requires_exactly_two_expected_keys(
    expected_keys: list[dict[str, str]],
) -> None:
    repository = PostgreSQLOrganizationRepository("postgresql://unused")

    with pytest.raises(OrganizationValidationError):
        asyncio.run(
            repository.reconcile_baic_pilot_state(
                **_baic_reconciliation_kwargs(expected_keys)
            )
        )


@pytest.mark.parametrize(
    "second_key",
    [
        {**_expected_baic_keys()[1], "upstreamKeyHash": "a" * 64},
        {**_expected_baic_keys()[1], "upstreamKeyId": "key-a"},
        {**_expected_baic_keys()[1], "upstreamKeyId": "a" * 64},
    ],
)
def test_baic_reconciliation_rejects_duplicate_stable_key_identifiers(
    second_key: dict[str, str],
) -> None:
    repository = PostgreSQLOrganizationRepository("postgresql://unused")

    with pytest.raises(OrganizationConflictError):
        asyncio.run(
            repository.reconcile_baic_pilot_state(
                **_baic_reconciliation_kwargs(
                    [_expected_baic_keys()[0], second_key]
                )
            )
        )


def test_baic_key_ownership_rejects_split_hash_and_id_rows() -> None:
    expected = _expected_baic_keys()[0]
    rows = [
        {
            "organization_id": "org-baic",
            "backend_id": "primary",
            "record_source": "usage",
            "upstream_key_hash": expected["upstreamKeyHash"],
            "upstream_key_id": "",
        },
        {
            "organization_id": "org-baic",
            "backend_id": "primary",
            "record_source": "usage",
            "upstream_key_hash": "c" * 64,
            "upstream_key_id": expected["upstreamKeyId"],
        },
    ]

    with pytest.raises(OrganizationConflictError, match="multiple local records"):
        PostgreSQLOrganizationRepository._validate_baic_key_ownership(
            expected, rows, organization_id="org-baic"
        )


def test_baic_key_ownership_rejects_any_cross_tenant_match() -> None:
    expected = _expected_baic_keys()[0]
    rows = [
        {
            "organization_id": "org-baic",
            "backend_id": "primary",
            "record_source": "usage",
            "upstream_key_hash": expected["upstreamKeyHash"],
            "upstream_key_id": expected["upstreamKeyId"],
        },
        {
            "organization_id": "org-other",
            "backend_id": "primary",
            "record_source": "usage",
            "upstream_key_hash": expected["upstreamKeyHash"],
            "upstream_key_id": "",
        },
    ]

    with pytest.raises(OrganizationConflictError, match="another customer"):
        PostgreSQLOrganizationRepository._validate_baic_key_ownership(
            expected, rows, organization_id="org-baic"
        )


@pytest.mark.parametrize(
    "row",
    [
        {
            "organization_id": "org-baic",
            "backend_id": "other-backend",
            "record_source": "usage",
            "upstream_key_hash": "a" * 64,
            "upstream_key_id": "key-a",
        },
        {
            "organization_id": "org-baic",
            "backend_id": "primary",
            "record_source": "usage",
            "upstream_key_hash": "c" * 64,
            "upstream_key_id": "key-a",
        },
        {
            "organization_id": "org-baic",
            "record_source": "managed",
            "upstream_key_hash": "a" * 64,
            "upstream_key_id": "",
        },
    ],
)
def test_baic_key_ownership_requires_complete_stable_identity(
    row: dict[str, str],
) -> None:
    with pytest.raises(OrganizationConflictError):
        PostgreSQLOrganizationRepository._validate_baic_key_ownership(
            _expected_baic_keys()[0], [row], organization_id="org-baic"
        )


def test_baic_key_ownership_allows_hash_only_when_expected_id_is_empty() -> None:
    expected = {**_expected_baic_keys()[0], "upstreamKeyId": ""}
    row = {
        "organization_id": "org-baic",
        "backend_id": "primary",
        "record_source": "usage",
        "upstream_key_hash": expected["upstreamKeyHash"],
        "upstream_key_id": "stored-id-not-required-by-preview",
    }

    PostgreSQLOrganizationRepository._validate_baic_key_ownership(
        expected, [row], organization_id="org-baic"
    )


class _BaicOwnershipConnection:
    def __init__(self, ownership_rows: list[dict[str, str]]) -> None:
        self.ownership_rows = ownership_rows
        self.executed: list[str] = []

    def transaction(self):
        return _Transaction()

    async def fetchrow(self, query, *args):
        if "FROM customer_adoption_operation" in query:
            return None
        if "FROM customer_organization WHERE id=$1" in query:
            return {
                "id": "org-baic",
                "name": "北汽",
                "upstream_organization_id": "org-old",
                "status": "active",
                "upstream_status": "active",
            }
        if "FROM customer_department" in query and "WHERE id=$1" in query:
            return {
                "id": "dept-baic",
                "organization_id": "org-baic",
                "upstream_team_id": "team-old",
                "status": "active",
            }
        if "FROM customer_member" in query:
            return {
                "id": "member-david",
                "organization_id": "org-baic",
                "department_id": "dept-baic",
                "name": "David",
                "email": "davidzhu2021@163.com",
                "role": "admin",
                "team_role": "leader",
                "status": "invited",
                "auth_user_id": "auth-david",
                "upstream_user_id": "customer-member-member-david",
            }
        if "upstream_organization_id=$1 AND id<>$2" in query:
            return None
        if "upstream_team_id=$1 AND id<>$2" in query:
            return None
        if "customer_usage_key_identity" in query or "customer_access_token" in query:
            raise AssertionError("ownership checks must fetch every matching row")
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def fetch(self, query, *args):
        if "FROM customer_invitation" in query:
            return [
                {
                    "id": "invitation-1",
                    "email": "davidzhu2021@163.com",
                    "consumed_at": datetime(2026, 8, 4, tzinfo=timezone.utc),
                    "revoked_at": None,
                }
            ]
        if "FROM customer_usage_key_identity" in query:
            if args[1] == "a" * 64:
                return list(self.ownership_rows)
            return []
        if "FROM customer_access_token" in query:
            return []
        raise AssertionError(f"unexpected fetch: {query}")

    async def execute(self, query, *_args):
        self.executed.append(query)
        return "INSERT 0 1"


class _BaicOwnershipPool:
    def __init__(self, connection: _BaicOwnershipConnection) -> None:
        self.connection = connection

    def acquire(self):
        return _Acquire(self.connection)


@pytest.mark.parametrize(
    "ownership_rows",
    [
        [
            {
                "organization_id": "org-baic",
                "backend_id": "primary",
                "record_source": "usage",
                "upstream_key_hash": "a" * 64,
                "upstream_key_id": "",
            },
            {
                "organization_id": "org-baic",
                "backend_id": "primary",
                "record_source": "usage",
                "upstream_key_hash": "c" * 64,
                "upstream_key_id": "key-a",
            },
        ],
        [
            {
                "organization_id": "org-other",
                "backend_id": "primary",
                "record_source": "usage",
                "upstream_key_hash": "a" * 64,
                "upstream_key_id": "key-a",
            }
        ],
    ],
)
def test_baic_reconciliation_fetches_all_key_owners_before_remap(
    ownership_rows: list[dict[str, str]],
) -> None:
    connection = _BaicOwnershipConnection(ownership_rows)
    repository = PostgreSQLOrganizationRepository("postgresql://unused")
    repository.pool = _BaicOwnershipPool(connection)

    with pytest.raises(OrganizationConflictError):
        asyncio.run(
            repository.reconcile_baic_pilot_state(
                **_baic_reconciliation_kwargs(_expected_baic_keys())
            )
        )

    assert not any(
        "UPDATE customer_organization SET name" in query
        or "UPDATE customer_department SET name" in query
        for query in connection.executed
    )


def _legacy_baic_credit(**overrides):
    row = {
        "id": "ledger-gift",
        "operation": "grant",
        "amount_usd": Decimal("5000.00"),
        "balance_after_usd": Decimal("5000.00"),
        "reason": "赠送",
        "external_reference": "",
        "idempotency_key": "manual-random-key",
    }
    row.update(overrides)
    return row


def test_baic_credit_metadata_adoption_accepts_only_the_known_untouched_grant() -> None:
    row = _legacy_baic_credit()

    adopted = PostgreSQLOrganizationRepository._baic_pilot_credit_to_adopt(
        [row],
        balance=Decimal("5000.00"),
        billing_status="active",
        billing_effective_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
    )

    assert adopted is row


@pytest.mark.parametrize(
    ("rows", "balance", "billing_status", "billing_effective_at"),
    [
        (
            [_legacy_baic_credit(), _legacy_baic_credit(id="extra")],
            Decimal("5000"),
            "active",
            datetime(2026, 8, 4, tzinfo=timezone.utc),
        ),
        (
            [_legacy_baic_credit(reason="其他授信")],
            Decimal("5000"),
            "active",
            datetime(2026, 8, 4, tzinfo=timezone.utc),
        ),
        (
            [_legacy_baic_credit(amount_usd=Decimal("4999"))],
            Decimal("5000"),
            "active",
            datetime(2026, 8, 4, tzinfo=timezone.utc),
        ),
        (
            [_legacy_baic_credit()],
            Decimal("4999"),
            "active",
            datetime(2026, 8, 4, tzinfo=timezone.utc),
        ),
        (
            [_legacy_baic_credit()],
            Decimal("5000"),
            "past_due",
            datetime(2026, 8, 4, tzinfo=timezone.utc),
        ),
        ([_legacy_baic_credit()], Decimal("5000"), "active", None),
        (
            [
                _legacy_baic_credit(
                    idempotency_key="baic-pilot-initial-credit-v1"
                )
            ],
            Decimal("5000"),
            "active",
            datetime(2026, 8, 4, tzinfo=timezone.utc),
        ),
    ],
)
def test_baic_credit_metadata_adoption_rejects_any_ambiguous_state(
    rows,
    balance,
    billing_status,
    billing_effective_at,
) -> None:
    with pytest.raises(OrganizationConflictError):
        PostgreSQLOrganizationRepository._baic_pilot_credit_to_adopt(
            rows,
            balance=balance,
            billing_status=billing_status,
            billing_effective_at=billing_effective_at,
        )


def test_baic_reconciliation_schema_supports_superseded_outbox_and_idempotency() -> None:
    assert "customer_adoption_operation" in ORGANIZATION_SCHEMA
    assert "operation_key TEXT NOT NULL UNIQUE" in ORGANIZATION_SCHEMA
    assert "customer_outbox" in ORGANIZATION_SCHEMA
    # Outbox status is intentionally open-ended so a durable superseded
    # marker can preserve the original processing history without replay.
    assert "status TEXT NOT NULL DEFAULT 'pending'" in ORGANIZATION_SCHEMA


def test_report_only_import_idempotency_uses_stable_ownership_fields() -> None:
    import inspect

    source = inspect.getsource(
        PostgreSQLOrganizationRepository.import_report_only_key_identity
    )

    fingerprint = source[
        source.index("fingerprint_payload = {") : source.index(
            "request_fingerprint =", source.index("fingerprint_payload = {")
        )
    ]
    assert "upstreamKeyHash" in fingerprint
    assert "upstreamUserIdSnapshot" in fingerprint
    assert "spendUsdSnapshot" not in fingerprint
    assert "modelsSnapshot" not in fingerprint
    assert "reportingRequestedThrough" not in fingerprint
    assert "UPDATE customer_usage_key_identity SET" in source
    assert "reporting_requested_through=GREATEST" in source


def test_report_only_snapshot_amounts_quantize_upstream_float_noise() -> None:
    import inspect

    source = inspect.getsource(
        PostgreSQLOrganizationRepository.import_report_only_key_identity
    )

    assert "ROUND_HALF_UP" in source
    assert 'Decimal("0.000001")' in source
    assert "parsed.as_tuple().exponent < -6" not in source


def test_usage_backfill_claim_reclaims_stale_running_leases() -> None:
    import inspect

    source = inspect.getsource(
        PostgreSQLOrganizationRepository.claim_usage_backfill_window
    )

    assert "ORGANIZATION_BACKFILL_LEASE_SECONDS" in source
    assert "stale backfill lease reclaimed" in source
    assert "status='running' AND locked_at IS NOT NULL" in source
    assert "locked_at=now()" in source
    assert "lease_token" in source
    assert "secrets.token_urlsafe" in source


def test_usage_backfill_completion_and_failure_require_the_current_lease() -> None:
    import inspect

    complete_source = inspect.getsource(
        PostgreSQLOrganizationRepository.complete_usage_backfill_window
    )
    fail_source = inspect.getsource(
        PostgreSQLOrganizationRepository.fail_usage_backfill_window
    )

    assert "lease_token: str" in complete_source
    assert "AND lease_token=$4" in complete_source
    assert "lease_token=''" in complete_source
    assert "lease_token: str" in fail_source
    assert "AND lease_token=$3" in fail_source
    assert "usage backfill lease is no longer current" in fail_source


def test_stale_usage_backfill_worker_cannot_finish_a_reclaimed_lease() -> None:
    class Pool:
        status = "running"
        lease_token = "new-lease"

        async def fetchrow(self, query, *args):
            assert "AND lease_token=$4" in query
            backfill_id, covered_from, covered_through, lease_token = args
            assert backfill_id == "backfill-1"
            if self.status != "running" or lease_token != self.lease_token:
                return None
            self.status = "complete"
            self.lease_token = ""
            return {
                "id": backfill_id,
                "status": self.status,
                "covered_from": covered_from,
                "covered_through": covered_through,
                "next_date": covered_through,
            }

        async def execute(self, query, *args):
            assert "AND lease_token=$3" in query
            _backfill_id, _error, lease_token = args
            if self.status != "running" or lease_token != self.lease_token:
                return "UPDATE 0"
            self.status = "failed"
            self.lease_token = ""
            return "UPDATE 1"

    repository = PostgreSQLOrganizationRepository("postgresql://unused")
    pool = Pool()
    repository.pool = pool
    today = datetime.now(timezone.utc).date()

    with pytest.raises(OrganizationConflictError):
        asyncio.run(
            repository.complete_usage_backfill_window(
                "backfill-1",
                lease_token="stale-lease",
                covered_from=today,
                covered_through=today,
            )
        )
    with pytest.raises(OrganizationConflictError):
        asyncio.run(
            repository.fail_usage_backfill_window(
                "backfill-1", "old worker failed", lease_token="stale-lease"
            )
        )

    result = asyncio.run(
        repository.complete_usage_backfill_window(
            "backfill-1",
            lease_token="new-lease",
            covered_from=today,
            covered_through=today,
        )
    )
    assert result["status"] == "complete"
