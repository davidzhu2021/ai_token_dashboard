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
