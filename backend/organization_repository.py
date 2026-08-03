"""Persistent customer organization repository backed by PostgreSQL.

This module intentionally contains no HTTP or LiteLLM client code.  It owns
the durable local projection of organizations and the records needed to
provision upstream objects.  Callers may safely retry invitation, token and
billing operations using the idempotency fields provided here.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

try:  # Keep importing the application in environments without asyncpg.
    import asyncpg
except ModuleNotFoundError:  # pragma: no cover
    asyncpg = None  # type: ignore[assignment]

from .organization_validation import (
    DEFAULT_TOKEN_DAILY_BUDGET_USD,
    DuplicateMemberEmailError,
    MAX_TOKENS_PER_ORGANIZATION,
    OrganizationConflictError,
    OrganizationNotFoundError,
    OrganizationValidationError,
    OrganizationValidationMixin,
    _UNSET,
)


ORGANIZATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS customer_organization (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'provisioning',
    billing_status TEXT NOT NULL DEFAULT 'past_due'
        CHECK (billing_status IN ('active', 'past_due', 'suspended')),
    billing_balance_usd NUMERIC(16,6) NOT NULL DEFAULT 0,
    billing_effective_at TIMESTAMPTZ,
    upstream_organization_id TEXT NOT NULL DEFAULT '',
    upstream_status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS customer_organization_name_idx
    ON customer_organization (lower(name)) WHERE status <> 'archived';
CREATE UNIQUE INDEX IF NOT EXISTS customer_organization_upstream_idx
    ON customer_organization(upstream_organization_id)
    WHERE upstream_organization_id <> '';
ALTER TABLE customer_organization ADD COLUMN IF NOT EXISTS billing_status TEXT NOT NULL DEFAULT 'past_due';
ALTER TABLE customer_organization ADD COLUMN IF NOT EXISTS billing_balance_usd NUMERIC(16,6) NOT NULL DEFAULT 0;
ALTER TABLE customer_organization ADD COLUMN IF NOT EXISTS billing_effective_at TIMESTAMPTZ;
DO $$
BEGIN
    ALTER TABLE customer_organization
        ADD CONSTRAINT customer_organization_billing_status_check
        CHECK (billing_status IN ('active', 'past_due', 'suspended'));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- Imported reporting identities may exist before the person has a login.
CREATE TABLE IF NOT EXISTS customer_principal (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES customer_organization(id),
    member_id TEXT,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'active', 'suspended', 'archived')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS customer_principal_org_name_idx
    ON customer_principal(organization_id, lower(name))
    WHERE status <> 'archived';
CREATE TABLE IF NOT EXISTS customer_principal_upstream_identity (
    id TEXT PRIMARY KEY,
    organization_id TEXT REFERENCES customer_organization(id),
    principal_id TEXT NOT NULL REFERENCES customer_principal(id),
    backend_id TEXT NOT NULL,
    upstream_user_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (backend_id, upstream_user_id)
);
ALTER TABLE customer_principal ADD COLUMN IF NOT EXISTS member_id TEXT;
ALTER TABLE customer_principal_upstream_identity
    ADD COLUMN IF NOT EXISTS organization_id TEXT;
UPDATE customer_principal_upstream_identity identity
SET organization_id = principal.organization_id
FROM customer_principal principal
WHERE identity.principal_id = principal.id
  AND identity.organization_id IS NULL;
ALTER TABLE customer_principal_upstream_identity
    ALTER COLUMN organization_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS customer_principal_identity_idx
    ON customer_principal_upstream_identity(organization_id, principal_id, created_at DESC);

CREATE TABLE IF NOT EXISTS customer_department (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES customer_organization(id),
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    upstream_team_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS customer_department_name_idx
    ON customer_department (organization_id, lower(name)) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS customer_department_org_idx ON customer_department(organization_id);
CREATE UNIQUE INDEX IF NOT EXISTS customer_department_upstream_idx
    ON customer_department(upstream_team_id) WHERE upstream_team_id <> '';

CREATE TABLE IF NOT EXISTS customer_member (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES customer_organization(id),
    department_id TEXT NOT NULL REFERENCES customer_department(id),
    name TEXT NOT NULL,
    email TEXT,
    login_name TEXT,
    role TEXT NOT NULL DEFAULT 'member',
    status TEXT NOT NULL DEFAULT 'invited',
    team_role TEXT NOT NULL DEFAULT 'member',
    auth_user_id TEXT NOT NULL DEFAULT '',
    upstream_user_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    suspended_at TIMESTAMPTZ
);
DROP INDEX IF EXISTS customer_member_email_idx;
CREATE UNIQUE INDEX IF NOT EXISTS customer_member_email_idx
    ON customer_member(lower(email)) WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS customer_member_org_idx ON customer_member(organization_id);
ALTER TABLE customer_member ADD COLUMN IF NOT EXISTS auth_user_id TEXT NOT NULL DEFAULT '';
ALTER TABLE customer_member ADD COLUMN IF NOT EXISTS login_name TEXT;
ALTER TABLE customer_member ALTER COLUMN email DROP NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS customer_member_login_name_idx
    ON customer_member(lower(login_name)) WHERE login_name IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS customer_member_auth_user_idx
    ON customer_member(auth_user_id) WHERE auth_user_id <> '';

CREATE TABLE IF NOT EXISTS customer_invitation (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES customer_organization(id),
    member_id TEXT NOT NULL REFERENCES customer_member(id),
    email TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    consumed_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    last_sent_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS customer_invitation_lookup_idx
    ON customer_invitation(token_hash, expires_at) WHERE consumed_at IS NULL AND revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS customer_outbox (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS customer_outbox_pending_idx
    ON customer_outbox(available_at, created_at) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS customer_access_token (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES customer_organization(id),
    member_id TEXT REFERENCES customer_member(id),
    department_id TEXT REFERENCES customer_department(id),
    name TEXT NOT NULL,
    models JSONB NOT NULL DEFAULT '[]'::jsonb,
    upstream_key_id TEXT NOT NULL DEFAULT '',
    upstream_key_hash TEXT NOT NULL DEFAULT '',
    upstream_key_alias TEXT NOT NULL DEFAULT '',
    upstream_team_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'provisioning',
    daily_budget_usd NUMERIC(16,6) NOT NULL,
    duration TEXT NOT NULL DEFAULT 'never',
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS customer_token_name_idx
    ON customer_access_token(organization_id, lower(name)) WHERE status NOT IN ('revoked', 'expired');
ALTER TABLE customer_access_token ADD COLUMN IF NOT EXISTS upstream_key_alias TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS customer_token_alias_idx
    ON customer_access_token(upstream_key_alias) WHERE upstream_key_alias <> '';
CREATE UNIQUE INDEX IF NOT EXISTS customer_token_upstream_id_idx
    ON customer_access_token(upstream_key_id) WHERE upstream_key_id <> '';
CREATE UNIQUE INDEX IF NOT EXISTS customer_token_upstream_hash_idx
    ON customer_access_token(upstream_key_hash) WHERE upstream_key_hash <> '';
CREATE INDEX IF NOT EXISTS customer_token_org_idx ON customer_access_token(organization_id, created_at DESC);
ALTER TABLE customer_access_token ADD COLUMN IF NOT EXISTS department_id TEXT REFERENCES customer_department(id);
ALTER TABLE customer_access_token ADD COLUMN IF NOT EXISTS upstream_team_id TEXT NOT NULL DEFAULT '';
ALTER TABLE customer_access_token ADD COLUMN IF NOT EXISTS principal_id TEXT REFERENCES customer_principal(id);

-- Composite tenant keys let PostgreSQL enforce that a member or department
-- referenced by a mapping belongs to the same organization.
DO $$
BEGIN
    ALTER TABLE customer_member
        ADD CONSTRAINT customer_member_org_id_unique UNIQUE (organization_id, id);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
DO $$
BEGIN
    ALTER TABLE customer_department
        ADD CONSTRAINT customer_department_org_id_unique UNIQUE (organization_id, id);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
DO $$
BEGIN
    ALTER TABLE customer_principal
        ADD CONSTRAINT customer_principal_org_id_unique UNIQUE (organization_id, id);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS customer_principal_org_member_idx
    ON customer_principal(organization_id, member_id)
    WHERE member_id IS NOT NULL;
DO $$
BEGIN
    ALTER TABLE customer_principal
        ADD CONSTRAINT customer_principal_org_member_fk
        FOREIGN KEY (organization_id, member_id)
        REFERENCES customer_member(organization_id, id);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
DO $$
BEGIN
    ALTER TABLE customer_principal_upstream_identity
        ADD CONSTRAINT customer_principal_identity_org_principal_fk
        FOREIGN KEY (organization_id, principal_id)
        REFERENCES customer_principal(organization_id, id);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
DO $$
BEGIN
    ALTER TABLE customer_access_token
        ADD CONSTRAINT customer_access_token_org_principal_fk
        FOREIGN KEY (organization_id, principal_id)
        REFERENCES customer_principal(organization_id, id);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- Historical upstream identities are reporting evidence, not credentials.
-- Keep them separate from customer_access_token so importing an existing key
-- can never grant, revoke, rotate, or otherwise manage that key.
CREATE TABLE IF NOT EXISTS customer_usage_identity (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES customer_organization(id),
    member_id TEXT NOT NULL,
    backend_id TEXT NOT NULL,
    upstream_user_id TEXT NOT NULL,
    effective_from TIMESTAMPTZ NOT NULL,
    effective_through TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (effective_from <= effective_through),
    FOREIGN KEY (organization_id, member_id)
        REFERENCES customer_member(organization_id, id),
    UNIQUE (backend_id, upstream_user_id)
);
CREATE INDEX IF NOT EXISTS customer_usage_identity_member_idx
    ON customer_usage_identity(organization_id, member_id, created_at DESC);

CREATE TABLE IF NOT EXISTS customer_usage_key_identity (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES customer_organization(id),
    principal_id TEXT NOT NULL REFERENCES customer_principal(id),
    member_id TEXT,
    department_id TEXT,
    backend_id TEXT NOT NULL,
    upstream_key_hash TEXT NOT NULL,
    upstream_key_id TEXT NOT NULL DEFAULT '',
    key_alias_snapshot TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL CHECK (mode IN ('managed', 'report_only')),
    upstream_organization_id_snapshot TEXT NOT NULL DEFAULT '',
    upstream_team_id_snapshot TEXT NOT NULL DEFAULT '',
    upstream_user_id_snapshot TEXT NOT NULL DEFAULT '',
    models_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,
    max_budget_usd_snapshot NUMERIC(16,6),
    spend_usd_snapshot NUMERIC(16,6),
    budget_duration_snapshot TEXT NOT NULL DEFAULT '',
    expires_at_snapshot TIMESTAMPTZ,
    blocked_snapshot BOOLEAN NOT NULL DEFAULT FALSE,
    import_batch_id TEXT NOT NULL DEFAULT '',
    reporting_requested_through DATE,
    effective_from TIMESTAMPTZ NOT NULL,
    effective_through TIMESTAMPTZ,
    billing_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    idempotency_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (upstream_key_hash ~ '^[0-9a-f]{64}$'),
    CHECK (effective_through IS NULL OR effective_from <= effective_through),
    CHECK (mode <> 'report_only' OR billing_eligible = FALSE),
    FOREIGN KEY (organization_id, member_id)
        REFERENCES customer_member(organization_id, id),
    FOREIGN KEY (organization_id, department_id)
        REFERENCES customer_department(organization_id, id),
    UNIQUE (backend_id, upstream_key_hash),
    UNIQUE (organization_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS customer_usage_key_identity_org_idx
    ON customer_usage_key_identity(organization_id, created_at DESC);
CREATE INDEX IF NOT EXISTS customer_usage_key_identity_member_idx
    ON customer_usage_key_identity(member_id, created_at DESC);
ALTER TABLE customer_usage_key_identity ADD COLUMN IF NOT EXISTS principal_id TEXT
    REFERENCES customer_principal(id);
ALTER TABLE customer_usage_key_identity ADD COLUMN IF NOT EXISTS upstream_key_id TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS customer_usage_key_identity_backend_key_id_idx
    ON customer_usage_key_identity(backend_id, upstream_key_id)
    WHERE upstream_key_id <> '';
ALTER TABLE customer_usage_key_identity ADD COLUMN IF NOT EXISTS upstream_user_id_snapshot TEXT NOT NULL DEFAULT '';
ALTER TABLE customer_usage_key_identity ADD COLUMN IF NOT EXISTS models_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE customer_usage_key_identity ADD COLUMN IF NOT EXISTS max_budget_usd_snapshot NUMERIC(16,6);
ALTER TABLE customer_usage_key_identity ADD COLUMN IF NOT EXISTS spend_usd_snapshot NUMERIC(16,6);
ALTER TABLE customer_usage_key_identity ADD COLUMN IF NOT EXISTS budget_duration_snapshot TEXT NOT NULL DEFAULT '';
ALTER TABLE customer_usage_key_identity ADD COLUMN IF NOT EXISTS expires_at_snapshot TIMESTAMPTZ;
ALTER TABLE customer_usage_key_identity ADD COLUMN IF NOT EXISTS blocked_snapshot BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE customer_usage_key_identity ADD COLUMN IF NOT EXISTS import_batch_id TEXT NOT NULL DEFAULT '';
ALTER TABLE customer_usage_key_identity ADD COLUMN IF NOT EXISTS reporting_requested_through DATE;
-- Deployments that imported member-bound report-only keys before principals
-- existed are upgraded deterministically without guessing by name or email.
INSERT INTO customer_principal(id, organization_id, member_id, name, status)
SELECT 'principal-member-' || md5(k.organization_id || ':' || k.member_id),
       k.organization_id,
       k.member_id,
       COALESCE(m.name, 'Imported principal') || ' [' || substr(md5(k.member_id), 1, 8) || ']',
       CASE WHEN m.status = 'active' THEN 'active' ELSE 'pending' END
FROM customer_usage_key_identity k
LEFT JOIN customer_member m
  ON m.organization_id = k.organization_id AND m.id = k.member_id
WHERE k.principal_id IS NULL AND k.member_id IS NOT NULL
ON CONFLICT DO NOTHING;
UPDATE customer_usage_key_identity k
SET principal_id = p.id
FROM customer_principal p
WHERE k.principal_id IS NULL
  AND k.member_id IS NOT NULL
  AND p.organization_id = k.organization_id
  AND p.member_id = k.member_id;
INSERT INTO customer_principal(id, organization_id, name, status)
SELECT 'principal-key-' || md5(k.organization_id || ':' || k.id),
       k.organization_id,
       'Imported principal [' || substr(md5(k.id), 1, 8) || ']',
       'pending'
FROM customer_usage_key_identity k
WHERE k.principal_id IS NULL
ON CONFLICT DO NOTHING;
UPDATE customer_usage_key_identity k
SET principal_id = 'principal-key-' || md5(k.organization_id || ':' || k.id)
WHERE k.principal_id IS NULL;
ALTER TABLE customer_usage_key_identity ALTER COLUMN principal_id SET NOT NULL;
ALTER TABLE customer_usage_key_identity ALTER COLUMN effective_through DROP NOT NULL;
UPDATE customer_usage_key_identity
SET effective_through = NULL
WHERE mode = 'report_only' AND effective_through IS NOT NULL;
DO $$
BEGIN
    ALTER TABLE customer_usage_key_identity
        ADD CONSTRAINT customer_usage_key_identity_org_principal_fk
        FOREIGN KEY (organization_id, principal_id)
        REFERENCES customer_principal(organization_id, id);
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS customer_usage_backfill (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES customer_organization(id),
    principal_id TEXT NOT NULL REFERENCES customer_principal(id),
    usage_key_identity_id TEXT NOT NULL REFERENCES customer_usage_key_identity(id),
    backend_id TEXT NOT NULL,
    requested_from DATE NOT NULL,
    requested_through DATE NOT NULL,
    covered_from DATE,
    covered_through DATE,
    next_date DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'partial', 'complete', 'failed')),
    last_error TEXT NOT NULL DEFAULT '',
    last_synced_at TIMESTAMPTZ,
    import_batch_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (requested_from <= requested_through),
    UNIQUE (usage_key_identity_id, requested_from, requested_through)
);
CREATE INDEX IF NOT EXISTS customer_usage_backfill_pending_idx
    ON customer_usage_backfill(status, next_date, created_at)
    WHERE status IN ('pending', 'partial', 'failed');

CREATE TABLE IF NOT EXISTS usage_event_attribution (
    backend_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    event_time TIMESTAMPTZ NOT NULL,
    usage_date DATE NOT NULL,
    raw_user_id TEXT NOT NULL DEFAULT '',
    organization_id TEXT NOT NULL DEFAULT '',
    team_id TEXT NOT NULL DEFAULT '',
    key_id TEXT NOT NULL DEFAULT '',
    principal_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    prompt_tokens BIGINT NOT NULL DEFAULT 0,
    completion_tokens BIGINT NOT NULL DEFAULT 0,
    total_tokens BIGINT NOT NULL DEFAULT 0,
    request_count BIGINT NOT NULL DEFAULT 0,
    success_count BIGINT NOT NULL DEFAULT 0,
    failure_count BIGINT NOT NULL DEFAULT 0,
    spend NUMERIC(16,6) NOT NULL DEFAULT 0,
    attribution_source TEXT NOT NULL DEFAULT 'unattributed',
    billing_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (backend_id, request_id)
);
CREATE INDEX IF NOT EXISTS usage_event_attribution_org_time_idx
    ON usage_event_attribution(organization_id, event_time)
    WHERE organization_id <> '';
CREATE INDEX IF NOT EXISTS usage_event_attribution_key_time_idx
    ON usage_event_attribution(key_id, event_time)
    WHERE key_id <> '';

CREATE TABLE IF NOT EXISTS customer_adoption_operation (
    id TEXT PRIMARY KEY,
    organization_id TEXT REFERENCES customer_organization(id),
    operation_key TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    preview_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'applying'
        CHECK (status IN ('applying', 'applied', 'failed')),
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_error TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS customer_adoption_operation_org_idx
    ON customer_adoption_operation(organization_id, created_at DESC);

CREATE TABLE IF NOT EXISTS customer_billing_ledger (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES customer_organization(id),
    operation TEXT NOT NULL,
    amount_usd NUMERIC(16,6) NOT NULL,
    balance_after_usd NUMERIC(16,6) NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    operator TEXT NOT NULL DEFAULT '',
    operator_email TEXT NOT NULL DEFAULT '',
    external_reference TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(organization_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS customer_billing_org_idx
    ON customer_billing_ledger(organization_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS customer_billing_external_ref_idx
    ON customer_billing_ledger(organization_id, external_reference)
    WHERE external_reference <> '';

CREATE OR REPLACE VIEW customer_billing_ledger_latest AS
SELECT l.*
FROM customer_billing_ledger l
ORDER BY l.created_at DESC, l.id DESC;

CREATE TABLE IF NOT EXISTS customer_usage_settlement (
    organization_id TEXT NOT NULL REFERENCES customer_organization(id),
    usage_date DATE NOT NULL,
    upstream_organization_id TEXT NOT NULL,
    settled_amount_usd NUMERIC(16,6) NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, usage_date)
);
CREATE INDEX IF NOT EXISTS customer_usage_settlement_upstream_idx
    ON customer_usage_settlement(upstream_organization_id, usage_date);

CREATE TABLE IF NOT EXISTS customer_audit_log (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES customer_organization(id),
    audit_action TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    target_type TEXT NOT NULL DEFAULT '',
    target_id TEXT NOT NULL DEFAULT '',
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS customer_audit_org_idx
    ON customer_audit_log(organization_id, created_at DESC);
"""


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _money(value: Any) -> float:
    return float(Decimal(str(value or 0)).quantize(Decimal("0.01")))


def _settlement_money(value: Any) -> float:
    """Keep usage reconciliation aligned with the six-decimal ledger schema."""

    return float(Decimal(str(value or 0)).quantize(Decimal("0.000001")))


def _json(value: Any) -> Any:
    if isinstance(value, str):
        return value
    return value


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    """Read asyncpg.Record/dict values without relying on ``dict.get``."""

    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


class PostgreSQLOrganizationRepository(OrganizationValidationMixin):
    """Async PostgreSQL implementation for real organization mode."""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 5) -> None:
        self.dsn = dsn
        self.min_size = min_size
        self.max_size = max_size
        self.pool: Any = None
        self._connect_lock = asyncio.Lock()

    @classmethod
    def from_environment(cls) -> PostgreSQLOrganizationRepository | None:
        dsn = os.getenv("USAGE_DATABASE_URL", "").strip()
        mode = os.getenv("ORGANIZATION_MODE", "disabled").strip().lower()
        if mode != "real" or not dsn:
            return None
        return cls(dsn)

    async def connect(self) -> None:
        if self.pool is not None:
            return
        if asyncpg is None:
            raise RuntimeError("real organization mode requires asyncpg")
        async with self._connect_lock:
            if self.pool is None:
                pool = await asyncpg.create_pool(self.dsn, min_size=self.min_size, max_size=self.max_size, command_timeout=30)
                try:
                    await pool.execute(ORGANIZATION_SCHEMA)
                except Exception:
                    await pool.close()
                    raise
                self.pool = pool

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    def _require_pool(self) -> Any:
        if self.pool is None:
            raise RuntimeError("organization database is not connected")
        return self.pool

    @staticmethod
    def _outbox_max_attempts() -> int:
        """Bound retries so permanent provisioning errors become observable."""

        try:
            configured = int(os.getenv("ORGANIZATION_OUTBOX_MAX_ATTEMPTS", "8"))
        except (TypeError, ValueError):
            configured = 8
        return max(1, min(configured, 100))

    @staticmethod
    def _id(value: str | None = None) -> str:
        return value or uuid.uuid4().hex

    @staticmethod
    def _outbox_id(kind: str, aggregate_id: str, version: Any) -> str:
        return hashlib.sha256(f"{kind}:{aggregate_id}:{version}".encode()).hexdigest()

    @classmethod
    async def _enqueue_projection_sync(
        cls,
        connection: Any,
        kind: str,
        aggregate_id: str,
        payload: dict[str, Any],
        *,
        version: Any,
    ) -> None:
        import json

        await connection.execute(
            "INSERT INTO customer_outbox(id,kind,aggregate_id,payload) "
            "VALUES($1,$2,$3,$4::jsonb) ON CONFLICT (id) DO NOTHING",
            cls._outbox_id(kind, aggregate_id, version),
            kind,
            aggregate_id,
            json.dumps(payload, ensure_ascii=False),
        )

    @classmethod
    async def _enqueue_token_revocations(
        cls,
        connection: Any,
        organization_id: str,
        *,
        reason: str,
        member_id: str = "",
        version: Any | None = None,
    ) -> int:
        query = (
            "SELECT t.id, t.upstream_key_id, t.upstream_key_alias, t.upstream_team_id, t.member_id, "
            "       o.upstream_organization_id, COALESCE(m.upstream_user_id, '') AS upstream_user_id "
            "FROM customer_access_token t "
            "JOIN customer_organization o ON o.id=t.organization_id "
            "LEFT JOIN customer_member m ON m.id=t.member_id "
            "WHERE t.organization_id=$1 AND t.status IN ('active','provisioning')"
        )
        args: list[Any] = [organization_id]
        if member_id:
            query += " AND t.member_id=$2"
            args.append(member_id)
        rows = await connection.fetch(query, *args)
        for token in rows:
            token_id = str(token["id"])
            await cls._enqueue_projection_sync(
                connection,
                "organization.token.revoke",
                token_id,
                {
                    "organizationId": organization_id,
                    "tokenId": token_id,
                    "upstreamKeyId": str(token["upstream_key_id"] or ""),
                    "upstreamKeyAlias": str(token["upstream_key_alias"] or ""),
                    "upstreamTeamId": str(token["upstream_team_id"] or ""),
                    "memberId": str(token["member_id"] or ""),
                    "upstreamOrganizationId": str(token["upstream_organization_id"] or ""),
                    "upstreamUserId": str(token["upstream_user_id"] or ""),
                    "reason": reason,
                },
                # A token can become usable again after an earlier revocation
                # event was already delivered (for example after credit is
                # restored). Every independent suspension/credit transition
                # therefore needs its own durable job id.
                version=version or cls._id(),
            )
        return len(rows)

    @staticmethod
    def invitation_hash(token: str) -> str:
        secret = os.getenv("ORGANIZATION_INVITATION_SECRET", "").strip()
        data = str(token).encode("utf-8")
        if secret:
            return hmac.new(secret.encode("utf-8"), data, hashlib.sha256).hexdigest()
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _invitation_token(invitation_id: str) -> str:
        """Create a reproducible signed token without persisting plaintext.

        Reproducibility lets an outbox worker retry delivery after a restart.
        The identifier supplies 128 bits of randomness and the signature stops
        callers from fabricating a token from an observed invitation id.
        """

        secret = os.getenv("ORGANIZATION_INVITATION_SECRET", "").strip()
        if not secret:
            # Local/test deployments retain one-time semantics, but real mode
            # should configure the secret so delivery retries are deterministic.
            return f"{invitation_id}.{secrets.token_urlsafe(32)}"
        signature = hmac.new(
            secret.encode("utf-8"), invitation_id.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"{invitation_id}.{signature}"

    @staticmethod
    def _org_payload(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"], "name": row["name"], "status": row["status"],
            "billingStatus": _row_value(row, "billing_status", "past_due") or "past_due",
            "billingBalanceUsd": _money(_row_value(row, "billing_balance_usd", 0)),
            "billingEffectiveAt": _iso(_row_value(row, "billing_effective_at")),
            "upstreamOrganizationId": row["upstream_organization_id"] or None,
            "upstreamStatus": row["upstream_status"], "isDemo": False,
            "createdAt": _iso(row["created_at"]), "updatedAt": _iso(row["updated_at"]),
            "archivedAt": _iso(row["archived_at"]),
        }

    @staticmethod
    def _dept_payload(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"], "name": row["name"], "status": row["status"],
            "upstreamTeamId": row["upstream_team_id"] or None,
            "memberCount": int(_row_value(row, "member_count", 0) or 0),
            "activeMemberCount": int(_row_value(row, "active_member_count", 0) or 0),
            "invitedMemberCount": int(_row_value(row, "invited_member_count", 0) or 0),
            "suspendedMemberCount": int(_row_value(row, "suspended_member_count", 0) or 0),
            "createdAt": _iso(row["created_at"]), "updatedAt": _iso(row["updated_at"]),
            "archivedAt": _iso(row["archived_at"]),
        }

    async def get_organization(self, organization_id: str) -> dict[str, Any] | None:
        row = await self._require_pool().fetchrow("SELECT * FROM customer_organization WHERE id=$1", organization_id)
        return self._org_payload(row) if row else None

    async def get_current(self, organization_id: str | None = None) -> dict[str, Any]:
        if not organization_id:
            raise OrganizationNotFoundError("organization was not found")
        organization = await self.get_organization(organization_id)
        if organization is None:
            raise OrganizationNotFoundError("organization was not found")
        return await self.get_organization_snapshot(organization_id)

    async def get_organization_snapshot(self, organization_id: str) -> dict[str, Any]:
        organization = await self.get_organization(organization_id)
        if organization is None:
            raise OrganizationNotFoundError("organization was not found")
        departments = await self.list_departments(organization_id=organization_id)
        pool = self._require_pool()
        member_stats = await pool.fetchrow(
            "SELECT count(*) AS total, "
            "count(*) FILTER (WHERE status='active') AS active, "
            "count(*) FILTER (WHERE status='invited') AS invited, "
            "count(*) FILTER (WHERE status='suspended') AS suspended, "
            "count(*) FILTER (WHERE status='active' AND role='admin') AS admins "
            "FROM customer_member WHERE organization_id=$1",
            organization_id,
        )
        return {
            "organization": organization,
            "departments": departments,
            "stats": {
                "departmentCount": len(departments),
                "memberCount": int(member_stats["total"] or 0),
                "activeMemberCount": int(member_stats["active"] or 0),
                "invitedMemberCount": int(member_stats["invited"] or 0),
                "suspendedMemberCount": int(member_stats["suspended"] or 0),
                "activeAdminCount": int(member_stats["admins"] or 0),
            },
        }

    async def organization_snapshot(self, organization_id: str) -> dict[str, Any]:
        """Compatibility alias used by the application-facing store wrapper."""

        return await self.get_organization_snapshot(organization_id)

    async def usage_cache_fingerprint(self, organization_id: str) -> str:
        row = await self._require_pool().fetchrow(
            "SELECT id, updated_at FROM customer_organization WHERE id=$1", organization_id
        )
        if row is None:
            raise OrganizationNotFoundError("organization was not found")
        return f"postgres:{row['id']}:{_iso(row['updated_at'])}"

    async def list_organizations(self, *, keyword: str = "", status: str = "", page: int = 1, page_size: int = 50, include_archived: bool = True) -> dict[str, Any]:
        page = self._page_value(page, "page", 100000)
        page_size = self._page_value(page_size, "page_size", 100)
        clauses, args = [], []
        if keyword:
            args.append(f"%{keyword.strip()}%")
            clauses.append(f"(name ILIKE ${len(args)} OR id ILIKE ${len(args)})")
        if status:
            args.append(status)
            clauses.append(f"status = ${len(args)}")
        elif not include_archived:
            clauses.append("status <> 'archived'")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        pool = self._require_pool()
        total = await pool.fetchval(f"SELECT count(*) FROM customer_organization{where}", *args)
        args.extend([page_size, (page - 1) * page_size])
        rows = await pool.fetch(f"SELECT * FROM customer_organization{where} ORDER BY created_at DESC LIMIT ${len(args)-1} OFFSET ${len(args)}", *args)
        return {"items": [self._org_payload(row) for row in rows], "total": int(total or 0), "page": page, "pageSize": page_size}

    async def get_organization_by_upstream_id(
        self, upstream_organization_id: str
    ) -> dict[str, Any] | None:
        row = await self._require_pool().fetchrow(
            "SELECT * FROM customer_organization WHERE upstream_organization_id=$1",
            str(upstream_organization_id or "").strip(),
        )
        return self._org_payload(row) if row else None

    async def get_department_by_upstream_id(
        self, upstream_team_id: str
    ) -> dict[str, Any] | None:
        row = await self._require_pool().fetchrow(
            "SELECT d.*, d.organization_id AS organization_id, "
            "0 AS member_count, 0 AS active_member_count, "
            "0 AS invited_member_count, 0 AS suspended_member_count "
            "FROM customer_department d WHERE upstream_team_id=$1",
            str(upstream_team_id or "").strip(),
        )
        if row is None:
            return None
        payload = self._dept_payload(row)
        payload["organizationId"] = str(row["organization_id"])
        return payload

    async def adopt_existing_upstream_scope(
        self,
        organization_id: str,
        department_id: str,
        *,
        upstream_organization_id: str,
        upstream_team_id: str,
    ) -> dict[str, Any]:
        """Attach preflighted upstream objects without enqueuing create jobs."""

        upstream_organization_id = self._required_text(
            upstream_organization_id, "upstream_organization_id", 128
        )
        upstream_team_id = self._required_text(
            upstream_team_id, "upstream_team_id", 128
        )
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                organization = await conn.fetchrow(
                    "SELECT * FROM customer_organization WHERE id=$1 FOR UPDATE",
                    organization_id,
                )
                department = await conn.fetchrow(
                    "SELECT * FROM customer_department WHERE id=$1 AND organization_id=$2 FOR UPDATE",
                    department_id,
                    organization_id,
                )
                if organization is None or department is None:
                    raise OrganizationNotFoundError("organization scope was not found")
                current_org_id = str(organization["upstream_organization_id"] or "")
                current_team_id = str(department["upstream_team_id"] or "")
                if current_org_id and current_org_id != upstream_organization_id:
                    raise OrganizationConflictError(
                        "organization is already mapped to another upstream object"
                    )
                if current_team_id and current_team_id != upstream_team_id:
                    raise OrganizationConflictError(
                        "department is already mapped to another upstream object"
                    )
                org_owner = await conn.fetchrow(
                    "SELECT id FROM customer_organization "
                    "WHERE upstream_organization_id=$1 AND id<>$2 FOR UPDATE",
                    upstream_organization_id,
                    organization_id,
                )
                team_owner = await conn.fetchrow(
                    "SELECT id, organization_id FROM customer_department "
                    "WHERE upstream_team_id=$1 AND id<>$2 FOR UPDATE",
                    upstream_team_id,
                    department_id,
                )
                if org_owner is not None or team_owner is not None:
                    raise OrganizationConflictError(
                        "upstream scope is already mapped to another customer"
                    )
                await conn.execute(
                    "UPDATE customer_organization SET upstream_organization_id=$2, "
                    "upstream_status='active', status='active', updated_at=now() WHERE id=$1",
                    organization_id,
                    upstream_organization_id,
                )
                await conn.execute(
                    "UPDATE customer_department SET upstream_team_id=$3, updated_at=now() "
                    "WHERE id=$1 AND organization_id=$2",
                    department_id,
                    organization_id,
                    upstream_team_id,
                )
                await conn.execute(
                    "UPDATE customer_outbox SET status='sent', sent_at=now(), locked_at=NULL, "
                    "last_error='adopted existing upstream scope' "
                    "WHERE kind='organization.provision' AND aggregate_id=$1 "
                    "AND status IN ('pending','processing')",
                    organization_id,
                )
        return await self.get_organization_snapshot(organization_id)

    async def create_organization_with_admin(self, name: str, admin_name: str, admin_email: str, *, default_department_name: str = "企业管理", organization_id: str | None = None) -> dict[str, Any]:
        name = self._required_text(name, "name", 128)
        admin_name = self._required_text(admin_name, "admin_name", 128)
        admin_email = self.normalize_email(admin_email)
        department_name = self._required_text(default_department_name, "default_department_name", 128)
        org_id, dept_id, member_id = self._id(organization_id), self._id(), self._id()
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                try:
                    org = await conn.fetchrow("INSERT INTO customer_organization(id,name) VALUES($1,$2) RETURNING *", org_id, name)
                except Exception as exc:
                    if "customer_organization_name_idx" in str(exc) or "duplicate key" in str(exc).lower():
                        raise OrganizationConflictError("organization name is already in use") from exc
                    raise
                dept = await conn.fetchrow("INSERT INTO customer_department(id,organization_id,name) VALUES($1,$2,$3) RETURNING *", dept_id, org_id, department_name)
                member = await conn.fetchrow("INSERT INTO customer_member(id,organization_id,department_id,name,email,role,status,team_role) VALUES($1,$2,$3,$4,$5,'admin','invited','leader') RETURNING *", member_id, org_id, dept_id, admin_name, admin_email)
                # Provisioning is asynchronous so an upstream outage never
                # loses the durable organization/admin creation request.
                await conn.execute(
                    "INSERT INTO customer_outbox(id,kind,aggregate_id,payload) VALUES($1,$2,$3,$4::jsonb)",
                    hashlib.sha256(f"organization.provision:{org_id}".encode()).hexdigest(),
                    "organization.provision", org_id,
                    __import__('json').dumps({"organizationId": org_id, "adminMemberId": member_id}, ensure_ascii=False),
                )
        member_payload = self._member_payload(member)
        return {
            "organization": self._org_payload(org),
            "department": self._dept_payload(dept),
            "member": member_payload,
            # Keep the established seller-side response key while real mode
            # transitions the first administrator through an invitation.
            "admin": member_payload,
            "invitationStatus": "pending_provisioning",
        }

    @staticmethod
    def _member_payload(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"], "name": row["name"], "email": row["email"],
            "loginName": _row_value(row, "login_name", None),
            "displayIdentifier": row["email"] or _row_value(row, "login_name", None),
            "departmentId": row["department_id"],
            "departmentName": str(_row_value(row, "department_name", "") or ""),
            "role": row["role"], "status": row["status"], "teamRole": row["team_role"], "isTeamLeader": row["team_role"] == "leader",
            "authUserId": _row_value(row, "auth_user_id", "") or None,
            "upstreamUserId": row["upstream_user_id"] or None, "createdAt": _iso(row["created_at"]), "updatedAt": _iso(row["updated_at"]),
        }

    async def update_organization(self, organization_id: str, name: Any = _UNSET, *, status: Any = _UNSET) -> dict[str, Any]:
        fields, args = [], [organization_id]
        if name is not _UNSET:
            fields.append("name = $%d" % (len(args) + 1)); args.append(self._required_text(name, "name", 128))
        if status is not _UNSET:
            fields.append("status = $%d" % (len(args) + 1)); args.append(self._validate_organization_status(status))
        if not fields:
            result = await self.get_organization(organization_id)
            if result is None: raise OrganizationNotFoundError("organization was not found")
            return result
        fields.append("updated_at = now()")
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                previous = await conn.fetchrow(
                    "SELECT status FROM customer_organization WHERE id=$1 FOR UPDATE",
                    organization_id,
                )
                if previous is None:
                    raise OrganizationNotFoundError("organization was not found")
                row = await conn.fetchrow(
                    f"UPDATE customer_organization SET {', '.join(fields)} WHERE id=$1 RETURNING *",
                    *args,
                )
                if row is None:
                    raise OrganizationNotFoundError("organization was not found")
                next_status = str(row["status"] or "")
                # Keep the upstream projection convergent.  The outbox is
                # written in the same transaction as the local change, so a
                # proxy outage cannot leave a silent permission drift.
                upstream_id = str(_row_value(row, "upstream_organization_id", "") or "")
                if upstream_id:
                    await self._enqueue_projection_sync(
                        conn,
                        "organization.sync",
                        organization_id,
                        {
                            "organizationId": organization_id,
                            "upstreamOrganizationId": upstream_id,
                            "name": row["name"],
                            "status": next_status,
                        },
                        version=_iso(row["updated_at"]) or next_status,
                    )
                if status is not _UNSET and next_status in {"suspended", "archived"}:
                    await self._enqueue_token_revocations(
                        conn,
                        organization_id,
                        reason=f"organization_{next_status}",
                        version=_iso(row["updated_at"]) or self._id(),
                    )
        return self._org_payload(row)

    async def archive_organization(self, organization_id: str) -> dict[str, Any]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "UPDATE customer_organization SET status='archived', archived_at=now(), updated_at=now() "
                    "WHERE id=$1 RETURNING *",
                    organization_id,
                )
                if row is None:
                    raise OrganizationNotFoundError("organization was not found")
                upstream_id = str(_row_value(row, "upstream_organization_id", "") or "")
                if upstream_id:
                    await self._enqueue_projection_sync(
                        conn,
                        "organization.sync",
                        organization_id,
                        {
                            "organizationId": organization_id,
                            "upstreamOrganizationId": upstream_id,
                            "name": row["name"],
                            "status": "archived",
                        },
                        version=_iso(row["updated_at"]) or "archived",
                    )
                await self._enqueue_token_revocations(
                    conn,
                    organization_id,
                    reason="organization_archived",
                    version=_iso(row["updated_at"]) or self._id(),
                )
        return self._org_payload(row)

    async def set_upstream_organization(self, organization_id: str, upstream_id: str, *, status: str = "active") -> dict[str, Any]:
        """Persist an upstream mapping without resurrecting a disabled tenant.

        Provisioning retries may race with an operator suspension/archive. Only
        a local ``provisioning`` organization may transition to ``active``;
        active retries stay active, while disabled states fail closed.
        """

        upstream_id = str(upstream_id or "").strip()
        if not upstream_id:
            raise OrganizationConflictError("upstream organization id is required")
        upstream_status = str(status or "").strip().lower() or "active"
        if upstream_status not in {"provisioning", "pending", "active"}:
            raise OrganizationValidationError("upstream organization status is invalid")
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchrow(
                    "SELECT * FROM customer_organization WHERE id=$1 FOR UPDATE",
                    organization_id,
                )
                if current is None:
                    raise OrganizationNotFoundError("organization was not found")
                local_status = str(current["status"] or "provisioning")
                if local_status in {"suspended", "archived"}:
                    raise OrganizationConflictError(
                        f"organization is {local_status} and cannot be provisioned"
                    )
                next_local_status = "active" if upstream_status == "active" else local_status
                row = await conn.fetchrow(
                    "UPDATE customer_organization SET upstream_organization_id=$2, "
                    "upstream_status=$3, status=$4, updated_at=now() "
                    "WHERE id=$1 RETURNING *",
                    organization_id,
                    upstream_id,
                    upstream_status,
                    next_local_status,
                )
                if row is None:
                    raise OrganizationNotFoundError("organization was not found")
        return self._org_payload(row)

    async def list_departments(self, *, include_archived: bool = False, organization_id: str | None = None) -> list[dict[str, Any]]:
        clauses, args = [], []
        if organization_id: args.append(organization_id); clauses.append(f"d.organization_id=${len(args)}")
        if not include_archived: clauses.append("d.status='active'")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self._require_pool().fetch(
            "SELECT d.*, count(m.id) AS member_count, "
            "count(m.id) FILTER (WHERE m.status='active') AS active_member_count, "
            "count(m.id) FILTER (WHERE m.status='invited') AS invited_member_count, "
            "count(m.id) FILTER (WHERE m.status='suspended') AS suspended_member_count "
            f"FROM customer_department d LEFT JOIN customer_member m ON m.department_id=d.id{where} "
            "GROUP BY d.id ORDER BY d.created_at",
            *args,
        )
        return [self._dept_payload(r) for r in rows]

    async def get_department(
        self, department_id: str, *, organization_id: str | None = None
    ) -> dict[str, Any] | None:
        """Return one department while enforcing the optional tenant scope."""

        query = "SELECT d.*, count(m.id) AS member_count, " \
            "count(m.id) FILTER (WHERE m.status='active') AS active_member_count, " \
            "count(m.id) FILTER (WHERE m.status='invited') AS invited_member_count, " \
            "count(m.id) FILTER (WHERE m.status='suspended') AS suspended_member_count " \
            "FROM customer_department d LEFT JOIN customer_member m ON m.department_id=d.id " \
            "WHERE d.id=$1"
        args: list[Any] = [department_id]
        if organization_id:
            args.append(organization_id)
            query += " AND d.organization_id=$2"
        query += " GROUP BY d.id"
        row = await self._require_pool().fetchrow(query, *args)
        return self._dept_payload(row) if row else None

    async def create_department(self, name: str, *, organization_id: str) -> dict[str, Any]:
        name = self._required_text(name, "name", 128)
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "INSERT INTO customer_department(id,organization_id,name) "
                    "SELECT $1,id,$3 FROM customer_organization WHERE id=$2 AND status <> 'archived' RETURNING *",
                    self._id(), organization_id, name,
                )
                if row is not None:
                    organization = await conn.fetchrow(
                        "SELECT upstream_organization_id FROM customer_organization WHERE id=$1",
                        organization_id,
                    )
                    upstream_org_id = str(_row_value(organization, "upstream_organization_id", "") or "")
                    if upstream_org_id:
                        await self._enqueue_projection_sync(
                            conn,
                            "department.sync",
                            str(row["id"]),
                            {
                                "organizationId": organization_id,
                                "departmentId": str(row["id"]),
                                "upstreamOrganizationId": upstream_org_id,
                                "name": row["name"],
                                "status": row["status"],
                            },
                            version=_iso(row["updated_at"]) or "created",
                        )
        if row is None:
            raise OrganizationNotFoundError("organization was not found")
        return self._dept_payload(row)

    async def update_department(self, department_id: str, name: str, *, organization_id: str) -> dict[str, Any]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("UPDATE customer_department SET name=$3, updated_at=now() WHERE id=$1 AND organization_id=$2 RETURNING *", department_id, organization_id, self._required_text(name, "name", 128))
                if row is not None and str(row["upstream_team_id"] or ""):
                    organization = await conn.fetchrow("SELECT upstream_organization_id FROM customer_organization WHERE id=$1", organization_id)
                    await self._enqueue_projection_sync(
                        conn,
                        "department.sync",
                        department_id,
                        {
                            "organizationId": organization_id,
                            "departmentId": department_id,
                            "upstreamOrganizationId": str(_row_value(organization, "upstream_organization_id", "") or ""),
                            "upstreamTeamId": str(row["upstream_team_id"] or ""),
                            "name": row["name"],
                            "status": row["status"],
                        },
                        version=_iso(row["updated_at"]) or "updated",
                    )
        if row is None: raise OrganizationNotFoundError("department was not found")
        return self._dept_payload(row)

    async def archive_department(self, department_id: str, *, organization_id: str) -> dict[str, Any]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow("UPDATE customer_department SET status='archived', archived_at=now(), updated_at=now() WHERE id=$1 AND organization_id=$2 RETURNING *", department_id, organization_id)
                if row is not None and str(row["upstream_team_id"] or ""):
                    organization = await conn.fetchrow("SELECT upstream_organization_id FROM customer_organization WHERE id=$1", organization_id)
                    await self._enqueue_projection_sync(
                        conn,
                        "department.sync",
                        department_id,
                        {
                            "organizationId": organization_id,
                            "departmentId": department_id,
                            "upstreamOrganizationId": str(_row_value(organization, "upstream_organization_id", "") or ""),
                            "upstreamTeamId": str(row["upstream_team_id"] or ""),
                            "name": row["name"],
                            "status": "archived",
                        },
                        version=_iso(row["updated_at"]) or "archived",
                    )
        if row is None: raise OrganizationNotFoundError("department was not found")
        return self._dept_payload(row)

    async def list_members(self, *, organization_id: str, department_id: str = "", keyword: str = "", role: str = "", status: str = "", page: int = 1, page_size: int = 50) -> dict[str, Any]:
        page, page_size = self._page_value(page, "page", 100000), self._page_value(page_size, "page_size", 100)
        clauses, args = ["m.organization_id=$1"], [organization_id]
        for field, value in (("department_id", department_id), ("role", role), ("status", status)):
            if value: args.append(value); clauses.append(f"m.{field}=${len(args)}")
        if keyword:
            args.append(f"%{keyword.strip()}%")
            clauses.append(f"(m.name ILIKE ${len(args)} OR m.email ILIKE ${len(args)})")
        where = " AND ".join(clauses); pool = self._require_pool()
        total = await pool.fetchval(
            f"SELECT count(*) FROM customer_member m JOIN customer_department d "
            f"ON d.id=m.department_id WHERE {where}",
            *args,
        )
        args.extend([page_size, (page - 1) * page_size])
        rows = await pool.fetch(
            f"SELECT m.*, d.name AS department_name FROM customer_member m "
            f"JOIN customer_department d ON d.id=m.department_id "
            f"WHERE {where} ORDER BY m.created_at LIMIT ${len(args)-1} OFFSET ${len(args)}",
            *args,
        )
        return {"items": [self._member_payload(r) for r in rows], "total": int(total or 0), "page": page, "pageSize": page_size}

    async def get_member(self, member_id: str, *, organization_id: str | None = None) -> dict[str, Any] | None:
        if organization_id:
            row = await self._require_pool().fetchrow(
                "SELECT m.*, d.name AS department_name FROM customer_member m "
                "JOIN customer_department d ON d.id=m.department_id "
                "WHERE m.id=$1 AND m.organization_id=$2",
                member_id,
                organization_id,
            )
        else:
            row = await self._require_pool().fetchrow(
                "SELECT m.*, d.name AS department_name FROM customer_member m "
                "JOIN customer_department d ON d.id=m.department_id WHERE m.id=$1",
                member_id,
            )
        return self._member_payload(row) if row else None

    async def _member_payload_with_department(
        self, row: Any, *, organization_id: str
    ) -> dict[str, Any]:
        """Hydrate a mutation-returned row whose UPDATE omitted the join."""

        if _row_value(row, "department_name", None) is not None:
            return self._member_payload(row)
        pool = self._require_pool()
        fetchrow = getattr(pool, "fetchrow", None)
        if not callable(fetchrow):
            # Small repository fakes used by offline tests may only implement
            # the transaction surface. Preserve their payload contract while
            # production asyncpg pools still hydrate the department name.
            return self._member_payload(row)
        hydrated = await fetchrow(
            "SELECT m.*, d.name AS department_name FROM customer_member m "
            "JOIN customer_department d ON d.id=m.department_id "
            "WHERE m.id=$1 AND m.organization_id=$2",
            _row_value(row, "id", ""),
            organization_id,
        )
        return self._member_payload(hydrated or row)

    async def set_upstream_member(
        self,
        organization_id: str,
        member_id: str,
        upstream_user_id: str,
        *,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Persist an upstream identity without implicitly activating an invite."""

        fields = ["upstream_user_id=$3", "updated_at=now()"]
        args: list[Any] = [member_id, organization_id, str(upstream_user_id).strip()]
        if status is not None:
            fields.append("status=$4")
            args.append(self._validate_status(status))
        row = await self._require_pool().fetchrow(
            f"UPDATE customer_member SET {', '.join(fields)} "
            "WHERE id=$1 AND organization_id=$2 RETURNING *",
            *args,
        )
        if row is None:
            raise OrganizationNotFoundError("member was not found")
        return await self._member_payload_with_department(row, organization_id=organization_id)

    async def get_member_by_email(self, email: str, *, organization_id: str | None = None) -> dict[str, Any] | None:
        email = self.normalize_email(email)
        query = (
            "SELECT m.*, d.name AS department_name FROM customer_member m "
            "JOIN customer_department d ON d.id=m.department_id WHERE lower(m.email)=lower($1)"
            + (" AND m.organization_id=$2" if organization_id else "")
        )
        row = await self._require_pool().fetchrow(query, email, organization_id) if organization_id else await self._require_pool().fetchrow(query, email)
        return self._member_payload(row) if row else None

    async def resolve_member_by_email(self, email: str) -> dict[str, Any] | None:
        try:
            normalized = self.normalize_email(email)
        except Exception:
            return None
        row = await self._require_pool().fetchrow(
            "SELECT m.*, o.name AS organization_name, o.status AS organization_status, "
            "o.upstream_organization_id, o.upstream_status, o.created_at AS organization_created_at, "
            "o.updated_at AS organization_updated_at, o.archived_at AS organization_archived_at, "
            "d.name AS department_name, d.status AS department_status "
            "FROM customer_member m JOIN customer_organization o ON o.id=m.organization_id "
            "JOIN customer_department d ON d.id=m.department_id WHERE lower(m.email)=lower($1)",
            normalized,
        )
        if row is None:
            return None
        member = self._member_payload(row)
        member["departmentName"] = row["department_name"]
        member["departmentStatus"] = row["department_status"]
        organization = {
            "id": row["organization_id"],
            "name": row["organization_name"],
            "status": row["organization_status"],
            "upstreamOrganizationId": row["upstream_organization_id"] or None,
            "upstreamStatus": row["upstream_status"],
            "isDemo": False,
            "createdAt": _iso(row["organization_created_at"]),
            "updatedAt": _iso(row["organization_updated_at"]),
            "archivedAt": _iso(row["organization_archived_at"]),
        }
        return {
            "organizationId": row["organization_id"],
            "organization_id": row["organization_id"],
            "organization": organization,
            "member": member,
        }

    async def resolve_members_by_email(self, email: str) -> list[dict[str, Any]]:
        match = await self.resolve_member_by_email(email)
        return [match] if match is not None else []

    async def resolve_members_by_auth_user_id(self, auth_user_id: str) -> list[dict[str, Any]]:
        """Resolve real memberships from the invitation-bound local account.

        Email is display data only in real mode.  Using it as a tenant key
        would let an unrelated SSO/local principal with the same address gain
        access after a member becomes active.
        """

        user_id = str(auth_user_id or "").strip()
        if not user_id:
            return []
        row = await self._require_pool().fetchrow(
            "SELECT m.*, o.name AS organization_name, o.status AS organization_status, "
            "o.upstream_organization_id, o.upstream_status, o.created_at AS organization_created_at, "
            "o.updated_at AS organization_updated_at, o.archived_at AS organization_archived_at, "
            "d.name AS department_name, d.status AS department_status "
            "FROM customer_member m JOIN customer_organization o ON o.id=m.organization_id "
            "JOIN customer_department d ON d.id=m.department_id WHERE m.auth_user_id=$1",
            user_id,
        )
        if row is None:
            return []
        member = self._member_payload(row)
        member["departmentName"] = row["department_name"]
        member["departmentStatus"] = row["department_status"]
        organization = {
            "id": row["organization_id"], "name": row["organization_name"],
            "status": row["organization_status"],
            "upstreamOrganizationId": row["upstream_organization_id"] or None,
            "upstreamStatus": row["upstream_status"], "isDemo": False,
            "createdAt": _iso(row["organization_created_at"]),
            "updatedAt": _iso(row["organization_updated_at"]),
            "archivedAt": _iso(row["organization_archived_at"]),
        }
        return [{"organizationId": row["organization_id"], "organization_id": row["organization_id"], "organization": organization, "member": member}]

    async def create_member(self, name: str, email: str, department_id: str, role: str = "member", *, team_role: str = "member", organization_id: str) -> dict[str, Any]:
        name = self._required_text(name, "name", 128)
        email = self.normalize_email(email)
        role = self._validate_role(role)
        team_role = self._validate_team_role(team_role)
        pool = self._require_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        "INSERT INTO customer_member(id,organization_id,department_id,name,email,role,status,team_role) "
                        "SELECT $1,$2,id,$4,$5,$6,'invited',$7 FROM customer_department "
                        "WHERE id=$3 AND organization_id=$2 AND status='active' RETURNING *",
                        self._id(), organization_id, department_id, name, email, role, team_role,
                    )
        except Exception as exc:
            if "customer_member_email_idx" in str(exc) or (
                "duplicate key" in str(exc).lower() and "email" in str(exc).lower()
            ):
                raise DuplicateMemberEmailError(
                    "a member with this email already exists in a customer organization"
                ) from exc
        if row is None:
            raise OrganizationNotFoundError("department was not found")
        return await self._member_payload_with_department(row, organization_id=organization_id)

    async def create_managed_member(
        self,
        name: str,
        login_name: str,
        department_id: str,
        role: str = "admin",
        *,
        auth_user_id: str,
        team_role: str = "leader",
        organization_id: str,
    ) -> dict[str, Any]:
        """Create a claim-approved member without inventing an email address.

        The member remains invited until the outbox has created the upstream
        user and both Organization/Team memberships. Only that worker may
        activate customer access.
        """

        import re

        name = self._required_text(name, "name", 128)
        login_name = self._required_identifier(login_name, "login_name").casefold()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", login_name):
            raise OrganizationValidationError("login_name contains invalid characters")
        role = self._validate_role(role)
        team_role = self._validate_team_role(team_role)
        auth_user_id = self._required_identifier(auth_user_id, "auth_user_id")
        pool = self._require_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    existing = await conn.fetchrow(
                        "SELECT * FROM customer_member WHERE auth_user_id=$1 FOR UPDATE",
                        auth_user_id,
                    )
                    if existing is not None:
                        if (
                            str(existing["organization_id"]) != organization_id
                            or str(_row_value(existing, "login_name", "") or "").casefold()
                            != login_name
                        ):
                            raise OrganizationConflictError(
                                "auth account already belongs to another customer organization"
                            )
                        return await self._member_payload_with_department(
                            existing, organization_id=organization_id
                        )
                    row = await conn.fetchrow(
                        "INSERT INTO customer_member("
                        "id,organization_id,department_id,name,email,login_name,role,status,team_role,auth_user_id"
                        ") SELECT $1,$2,id,$4,NULL,$5,$6,'invited',$7,$8 FROM customer_department "
                        "WHERE id=$3 AND organization_id=$2 AND status='active' RETURNING *",
                        self._id(), organization_id, department_id, name,
                        login_name, role, team_role, auth_user_id,
                    )
                    if row is None:
                        raise OrganizationNotFoundError("department was not found")
                    await self._enqueue_projection_sync(
                        conn,
                        "organization.member.provision",
                        str(row["id"]),
                        {
                            "organizationId": organization_id,
                            "memberId": str(row["id"]),
                            "authUserId": auth_user_id,
                            "loginName": login_name,
                        },
                        version=f"managed-claim:{auth_user_id}",
                    )
        except (OrganizationConflictError, OrganizationNotFoundError):
            raise
        except Exception as exc:
            message = str(exc).lower()
            if (
                "customer_member_login_name_idx" in message
                or "customer_member_auth_user_idx" in message
            ):
                raise OrganizationConflictError(
                    "managed account is already bound to a customer organization"
                ) from exc
            raise
        return await self._member_payload_with_department(row, organization_id=organization_id)

    async def create_member_with_invitation(
        self,
        name: str,
        email: str,
        department_id: str,
        role: str = "member",
        *,
        team_role: str = "member",
        organization_id: str,
        expires_in_hours: int = 72,
    ) -> dict[str, Any]:
        """Create an invited member and its delivery outbox atomically.

        Keeping the member row, one-time invitation and mail outbox in the
        same transaction prevents a transient database failure from leaving a
        globally reserved email with no recoverable invitation.
        """

        name = self._required_text(name, "name", 128)
        email = self.normalize_email(email)
        role = self._validate_role(role)
        team_role = self._validate_team_role(team_role)
        invitation_id = self._id()
        token = self._invitation_token(invitation_id)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=max(1, int(expires_in_hours)))
        token_hash = self.invitation_hash(token)
        import json

        pool = self._require_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        "INSERT INTO customer_member(id,organization_id,department_id,name,email,role,status,team_role) "
                        "SELECT $1,$2,id,$4,$5,$6,'invited',$7 FROM customer_department "
                        "WHERE id=$3 AND organization_id=$2 AND status='active' RETURNING *",
                        self._id(), organization_id, department_id, name, email, role, team_role,
                    )
                    if row is None:
                        raise OrganizationNotFoundError("department was not found")
                    invitation = await conn.fetchrow(
                        "INSERT INTO customer_invitation("
                        "id,organization_id,member_id,email,token_hash,expires_at,last_sent_at"
                        ") VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING *",
                        invitation_id,
                        organization_id,
                        row["id"],
                        email,
                        token_hash,
                        expires,
                        now,
                    )
                    await conn.execute(
                        "INSERT INTO customer_outbox(id,kind,aggregate_id,payload) "
                        "VALUES($1,'organization.invitation.created',$2,$3::jsonb) "
                        "ON CONFLICT (id) DO NOTHING",
                        hashlib.sha256(f"invitation:{invitation_id}".encode()).hexdigest(),
                        invitation_id,
                        json.dumps(
                            {
                                "invitationId": invitation_id,
                                "organizationId": organization_id,
                                "memberId": row["id"],
                                "email": email,
                            },
                            ensure_ascii=False,
                        ),
                    )
        except (OrganizationNotFoundError, DuplicateMemberEmailError):
            raise
        except Exception as exc:
            if "customer_member_email_idx" in str(exc) or (
                "duplicate key" in str(exc).lower() and "email" in str(exc).lower()
            ):
                raise DuplicateMemberEmailError(
                    "a member with this email already exists in a customer organization"
                ) from exc
            raise
        # The plaintext invitation is returned only to the caller that just
        # created the member; the outbox stores only a reproducible id.
        result = await self._member_payload_with_department(row, organization_id=organization_id)
        result["invitationStatus"] = "pending"
        return result

    async def update_member(self, member_id: str, *, organization_id: str, name: Any = _UNSET, department_id: Any = _UNSET, role: Any = _UNSET, status: Any = _UNSET, team_role: Any = _UNSET) -> dict[str, Any]:
        fields, args = [], [member_id, organization_id]
        for column, value in (("name", name), ("department_id", department_id), ("role", role), ("status", status), ("team_role", team_role)):
            if value is _UNSET: continue
            if column == "name": value = self._required_text(value, "name", 128)
            elif column == "role": value = self._validate_role(value)
            elif column == "status": value = self._validate_status(value)
            elif column == "team_role": value = self._validate_team_role(value)
            fields.append(f"{column}=${len(args)+1}"); args.append(value)
        if not fields: return (await self.get_member(member_id, organization_id=organization_id)) or (_ for _ in ()).throw(OrganizationNotFoundError("member was not found"))
        fields.extend(["updated_at=now()"])
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if department_id is not _UNSET:
                    valid_department = await conn.fetchval(
                        "SELECT true FROM customer_department WHERE id=$1 AND organization_id=$2 AND status='active'",
                        department_id,
                        organization_id,
                    )
                    if not valid_department:
                        raise OrganizationNotFoundError("department was not found")
                previous = await conn.fetchrow(
                    "SELECT status, department_id FROM customer_member WHERE id=$1 AND organization_id=$2 FOR UPDATE",
                    member_id,
                    organization_id,
                )
                if previous is None:
                    raise OrganizationNotFoundError("member was not found")
                row = await conn.fetchrow(
                    f"UPDATE customer_member SET {', '.join(fields)} WHERE id=$1 AND organization_id=$2 RETURNING *",
                    *args,
                )
                if row is None:
                    raise OrganizationNotFoundError("member was not found")
                next_status = str(row["status"] or "")
                if str(row["upstream_user_id"] or ""):
                    await self._enqueue_projection_sync(
                        conn,
                        "organization.member.sync",
                        member_id,
                        {
                            "organizationId": organization_id,
                            "memberId": member_id,
                            "status": next_status,
                            "role": row["role"],
                            "teamRole": row["team_role"],
                            "departmentId": row["department_id"],
                            "upstreamUserId": row["upstream_user_id"],
                        },
                        version=_iso(row["updated_at"]) or next_status,
                    )
                department_changed = (
                    department_id is not _UNSET
                    and str(previous["department_id"] or "")
                    != str(row["department_id"] or "")
                )
                revocation_reason = ""
                if status is not _UNSET and next_status in {"invited", "suspended"}:
                    revocation_reason = f"member_{next_status}"
                elif department_changed:
                    # Keys retain the Team captured when issued. Revoke them on
                    # a move so no credential keeps the previous department's
                    # access; usage history remains attributed to the old Team.
                    revocation_reason = "member_department_changed"
                if revocation_reason:
                    await self._enqueue_token_revocations(
                        conn,
                        organization_id,
                        reason=revocation_reason,
                        member_id=member_id,
                        version=_iso(row["updated_at"]) or self._id(),
                    )
        return await self._member_payload_with_department(row, organization_id=organization_id)

    async def create_invitation(self, organization_id: str, member_id: str, *, expires_in_hours: int = 72) -> dict[str, Any]:
        invitation_id = self._id(); token = self._invitation_token(invitation_id)
        now = datetime.now(timezone.utc); expires = now + timedelta(hours=max(1, int(expires_in_hours)))
        token_hash = self.invitation_hash(token); pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE customer_invitation SET revoked_at=$1 WHERE organization_id=$2 AND member_id=$3 "
                    "AND consumed_at IS NULL AND revoked_at IS NULL",
                    now, organization_id, member_id,
                )
                row = await conn.fetchrow(
                    "INSERT INTO customer_invitation(id,organization_id,member_id,email,token_hash,expires_at,last_sent_at) "
                    "SELECT $1,$2,id,email,$3,$4,$5 FROM customer_member "
                    "WHERE id=$6 AND organization_id=$2 AND status='invited' RETURNING *",
                    invitation_id, organization_id, token_hash, expires, now, member_id,
                )
                if row is not None:
                    outbox_id = hashlib.sha256(f"invitation:{invitation_id}".encode()).hexdigest()
                    import json
                    await conn.execute(
                        "INSERT INTO customer_outbox(id,kind,aggregate_id,payload) "
                        "VALUES($1,'organization.invitation.created',$2,$3::jsonb) "
                        "ON CONFLICT (id) DO NOTHING",
                        outbox_id,
                        invitation_id,
                        json.dumps(
                            {
                                "invitationId": invitation_id,
                                "organizationId": organization_id,
                                "memberId": member_id,
                                "email": row["email"],
                            },
                            ensure_ascii=False,
                        ),
                    )
        if row is None: raise OrganizationNotFoundError("member was not found")
        return {"id": row["id"], "token": token, "organizationId": row["organization_id"], "memberId": row["member_id"], "email": row["email"], "expiresAt": _iso(row["expires_at"]), "status": "pending"}

    async def ensure_member_invitation(
        self,
        organization_id: str,
        member_id: str,
        *,
        expires_in_hours: int = 72,
    ) -> dict[str, Any] | None:
        """Ensure one durable invitation/outbox exists for an invited member.

        Organization provisioning can be retried after the upstream object was
        created successfully.  Reusing a still-valid invitation here prevents
        each retry from rotating the link, while the outbox insert repairs a
        crash between invitation creation and enqueueing its delivery.
        """

        hours = max(1, int(expires_in_hours))
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=hours)
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                member = await conn.fetchrow(
                    "SELECT * FROM customer_member "
                    "WHERE id=$1 AND organization_id=$2 FOR UPDATE",
                    member_id,
                    organization_id,
                )
                if member is None:
                    raise OrganizationNotFoundError("member was not found")
                if str(member["status"] or "") != "invited":
                    # An accepted/suspended member no longer needs an initial
                    # invitation.  Returning None keeps retries harmless.
                    return None

                invitation = await conn.fetchrow(
                    "SELECT * FROM customer_invitation "
                    "WHERE organization_id=$1 AND member_id=$2 "
                    "AND consumed_at IS NULL AND revoked_at IS NULL "
                    "AND expires_at > now() "
                    "ORDER BY created_at DESC, id DESC LIMIT 1 FOR UPDATE",
                    organization_id,
                    member_id,
                )
                if invitation is None:
                    invitation_id = self._id()
                    token = self._invitation_token(invitation_id)
                    invitation = await conn.fetchrow(
                        "INSERT INTO customer_invitation("
                        "id,organization_id,member_id,email,token_hash,expires_at,last_sent_at"
                        ") VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING *",
                        invitation_id,
                        organization_id,
                        member_id or None,
                        member["email"],
                        self.invitation_hash(token),
                        expires,
                        now,
                    )
                else:
                    invitation_id = str(invitation["id"])

                # The deterministic outbox id makes this repair safe if a
                # process crashed after inserting the invitation.
                import json

                outbox_id = hashlib.sha256(f"invitation:{invitation_id}".encode()).hexdigest()
                await conn.execute(
                    "INSERT INTO customer_outbox(id,kind,aggregate_id,payload) "
                    "VALUES($1,'organization.invitation.created',$2,$3::jsonb) "
                    "ON CONFLICT (id) DO NOTHING",
                    outbox_id,
                    invitation_id,
                    json.dumps(
                        {
                            "invitationId": invitation_id,
                            "organizationId": organization_id,
                            "memberId": member_id,
                            "email": member["email"],
                        },
                        ensure_ascii=False,
                    ),
                )

                return {
                    "id": invitation_id,
                    "organizationId": organization_id,
                    "memberId": member_id,
                    "email": member["email"],
                    "expiresAt": _iso(invitation["expires_at"]),
                    "status": "pending",
                }

    async def invitation_token_for_delivery(self, invitation_id: str) -> str | None:
        row = await self._require_pool().fetchrow(
            "SELECT token_hash FROM customer_invitation WHERE id=$1 AND consumed_at IS NULL "
            "AND revoked_at IS NULL AND expires_at > now()",
            invitation_id,
        )
        if row is None or not os.getenv("ORGANIZATION_INVITATION_SECRET", "").strip():
            return None
        token = self._invitation_token(invitation_id)
        if not hmac.compare_digest(self.invitation_hash(token), row["token_hash"]):
            return None
        return token

    async def verify_invitation(self, token: str) -> dict[str, Any] | None:
        row = await self._require_pool().fetchrow("SELECT i.*, m.name, m.status AS member_status, o.name AS organization_name FROM customer_invitation i JOIN customer_member m ON m.id=i.member_id JOIN customer_organization o ON o.id=i.organization_id WHERE i.token_hash=$1 AND i.consumed_at IS NULL AND i.revoked_at IS NULL AND i.expires_at > now() AND m.status='invited' AND o.status='active' AND o.upstream_status='active'", self.invitation_hash(token))
        if row is None: return None
        return {"id": row["id"], "organizationId": row["organization_id"], "organizationName": row["organization_name"], "memberId": row["member_id"], "email": row["email"], "name": row["name"], "expiresAt": _iso(row["expires_at"]), "status": "pending"}

    async def consume_invitation(self, token: str) -> dict[str, Any] | None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "UPDATE customer_invitation SET consumed_at=now() WHERE token_hash=$1 "
                    "AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at > now() RETURNING *",
                    self.invitation_hash(token),
                )
        if row is None: return None
        return {"id": row["id"], "organizationId": row["organization_id"], "memberId": row["member_id"], "email": row["email"], "consumedAt": _iso(row["consumed_at"])}

    async def accept_invitation(self, token: str, auth_user_id: str) -> dict[str, Any] | None:
        """Consume, bind, and enqueue provisioning in one PostgreSQL transaction."""

        pool = self._require_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        "SELECT i.*, o.status AS organization_status, o.upstream_status, "
                        "m.status AS member_status FROM customer_invitation i "
                        "JOIN customer_organization o ON o.id=i.organization_id "
                        "JOIN customer_member m ON m.id=i.member_id "
                        "WHERE i.token_hash=$1 AND i.consumed_at IS NULL "
                        "AND i.revoked_at IS NULL AND i.expires_at > now() "
                        "FOR UPDATE OF i, o, m",
                        self.invitation_hash(token),
                    )
                    if row is None:
                        return None
                    if (
                        str(row["organization_status"] or "") != "active"
                        or str(row["upstream_status"] or "") != "active"
                        or str(row["member_status"] or "") != "invited"
                    ):
                        return None
                    existing_member = await conn.fetchrow(
                        "SELECT id, organization_id FROM customer_member "
                        "WHERE auth_user_id=$1 AND auth_user_id<>'' FOR UPDATE",
                        auth_user_id,
                    )
                    if existing_member is not None and str(existing_member["id"]) != str(
                        row["member_id"]
                    ):
                        raise OrganizationConflictError(
                            "auth account already belongs to another customer organization"
                        )
                    row = await conn.fetchrow(
                        "UPDATE customer_invitation SET consumed_at=now() "
                        "WHERE id=$1 AND consumed_at IS NULL RETURNING *",
                        row["id"],
                    )
                    if row is None:
                        return None
                    member = await conn.fetchrow(
                        "UPDATE customer_member SET auth_user_id=$3, updated_at=now() "
                        "WHERE id=$1 AND organization_id=$2 AND status='invited' RETURNING *",
                        row["member_id"], row["organization_id"], auth_user_id,
                    )
                    if member is None:
                        raise OrganizationConflictError("invitation member is no longer available")
                    await self._enqueue_projection_sync(
                        conn,
                        "organization.member.provision",
                        str(row["member_id"]),
                        {
                            "organizationId": str(row["organization_id"]),
                            "memberId": str(row["member_id"]),
                            "authUserId": str(auth_user_id),
                            "email": str(row["email"]),
                        },
                        version=f"accepted:{row['id']}",
                    )
                    return {
                        "id": row["id"],
                        "organizationId": row["organization_id"],
                        "memberId": row["member_id"],
                        "email": row["email"],
                        "consumedAt": _iso(row["consumed_at"]),
                    }
        except OrganizationConflictError:
            raise
        except Exception as exc:
            message = str(exc).lower()
            if "customer_member_auth_user_idx" in message or (
                "duplicate key" in message and "auth_user_id" in message
            ):
                raise OrganizationConflictError(
                    "auth account already belongs to another customer organization"
                ) from exc
            raise

    async def activate_member_upstream(
        self,
        organization_id: str,
        member_id: str,
        upstream_user_id: str,
    ) -> dict[str, Any]:
        """Mark a member active after upstream provisioning.

        Provisioning is driven by an outbox and can be retried after the
        upstream request succeeded but the local transaction was interrupted.
        Treat an already-active member with the same upstream identity as a
        successful retry, while refusing to overwrite a conflicting mapping.
        """

        upstream_user_id = str(upstream_user_id or "").strip()
        if not upstream_user_id:
            raise OrganizationConflictError("upstream user id is required")
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchrow(
                    "SELECT * FROM customer_member WHERE id=$1 AND organization_id=$2 FOR UPDATE",
                    member_id,
                    organization_id,
                )
                if current is None:
                    raise OrganizationNotFoundError("member was not found")
                current_status = str(current["status"] or "")
                current_upstream_id = str(current["upstream_user_id"] or "").strip()
                if current_status == "active":
                    if current_upstream_id and current_upstream_id != upstream_user_id:
                        raise OrganizationConflictError("member upstream mapping conflicts")
                    if current_upstream_id == upstream_user_id:
                        return await self._member_payload_with_department(
                            current, organization_id=organization_id
                        )
                elif current_status != "invited":
                    raise OrganizationConflictError("member is not awaiting activation")

                row = await conn.fetchrow(
                    "UPDATE customer_member SET status='active', upstream_user_id=$3, updated_at=now() "
                    "WHERE id=$1 AND organization_id=$2 RETURNING *",
                    member_id,
                    organization_id,
                    upstream_user_id,
                )
                if row is None:
                    raise OrganizationNotFoundError("member was not found")
        return await self._member_payload_with_department(row, organization_id=organization_id)

    async def outbox_for_member(self, member_id: str) -> dict[str, Any] | None:
        """Return the latest provisioning job for claim-status reconciliation."""

        row = await self._require_pool().fetchrow(
            "SELECT id,status,attempts,last_error,created_at,sent_at FROM customer_outbox "
            "WHERE kind='organization.member.provision' AND aggregate_id=$1 "
            "ORDER BY created_at DESC,id DESC LIMIT 1",
            member_id,
        )
        if row is None:
            return None
        return {
            "id": row["id"],
            "status": row["status"],
            "attempts": int(row["attempts"] or 0),
            "lastError": row["last_error"] or "",
            "createdAt": _iso(row["created_at"]),
            "completedAt": _iso(row["sent_at"]),
        }

    async def bind_member_account(
        self, organization_id: str, member_id: str, auth_user_id: str
    ) -> dict[str, Any]:
        row = await self._require_pool().fetchrow(
            "UPDATE customer_member SET auth_user_id=$3, updated_at=now() "
            "WHERE id=$1 AND organization_id=$2 AND status='invited' RETURNING *",
            member_id, organization_id, auth_user_id,
        )
        if row is None:
            raise OrganizationNotFoundError("invited member was not found")
        return await self._member_payload_with_department(row, organization_id=organization_id)

    async def set_upstream_team(
        self, organization_id: str, department_id: str, upstream_team_id: str
    ) -> dict[str, Any]:
        row = await self._require_pool().fetchrow(
            "UPDATE customer_department SET upstream_team_id=$3, updated_at=now() "
            "WHERE id=$1 AND organization_id=$2 RETURNING *",
            department_id, organization_id, upstream_team_id,
        )
        if row is None:
            raise OrganizationNotFoundError("department was not found")
        return self._dept_payload(row)

    async def revoke_invitation(self, invitation_id: str, *, organization_id: str) -> bool:
        result = await self._require_pool().execute("UPDATE customer_invitation SET revoked_at=now() WHERE id=$1 AND organization_id=$2 AND consumed_at IS NULL", invitation_id, organization_id)
        return result.endswith("1")

    async def revoke_member_invitation(self, organization_id: str, member_id: str) -> bool:
        """Revoke the current unconsumed invitation for one scoped member.

        The member id is resolved server-side, so callers do not need to expose
        invitation ids in the browser.  Updating only unconsumed rows makes a
        concurrent acceptance win cleanly instead of revoking an already-bound
        account.
        """

        result = await self._require_pool().execute(
            "UPDATE customer_invitation SET revoked_at=now() "
            "WHERE organization_id=$1 AND member_id=$2 "
            "AND consumed_at IS NULL AND revoked_at IS NULL",
            organization_id,
            member_id,
        )
        return result.endswith("1")

    async def mark_invitation_sent(self, invitation_id: str) -> bool:
        """Record successful delivery without exposing the invitation token."""

        result = await self._require_pool().execute(
            "UPDATE customer_invitation SET last_sent_at=now() "
            "WHERE id=$1 AND consumed_at IS NULL AND revoked_at IS NULL",
            invitation_id,
        )
        return result.endswith("1")

    async def enqueue_outbox(self, kind: str, aggregate_id: str, payload: dict[str, Any], *, idempotency_key: str | None = None) -> dict[str, Any]:
        key = idempotency_key or self._id(); outbox_id = hashlib.sha256(f"{kind}:{aggregate_id}:{key}".encode()).hexdigest()
        row = await self._require_pool().fetchrow("INSERT INTO customer_outbox(id,kind,aggregate_id,payload) VALUES($1,$2,$3,$4::jsonb) ON CONFLICT (id) DO UPDATE SET payload=EXCLUDED.payload RETURNING *", outbox_id, kind, aggregate_id, __import__('json').dumps(payload, ensure_ascii=False))
        return dict(row)

    async def enqueue_token_reconciliation(
        self,
        organization_id: str,
        token_id: str,
        *,
        upstream_organization_id: str,
        upstream_team_id: str = "",
        upstream_user_id: str = "",
        upstream_key_alias: str,
    ) -> dict[str, Any]:
        """Queue a secret-free retry for a key whose upstream create may have won.

        The alias and durable local id are enough to recover the upstream key;
        plaintext credentials are deliberately never copied into the outbox.
        """

        token_id = self._required_text(token_id, "token_id", 128)
        alias = self._required_text(upstream_key_alias, "upstream_key_alias", 256)
        payload = {
            "organizationId": self._required_text(organization_id, "organization_id", 128),
            "tokenId": token_id,
            "upstreamOrganizationId": self._required_text(
                upstream_organization_id, "upstream_organization_id", 128
            ),
            "upstreamTeamId": str(upstream_team_id or "").strip(),
            "upstreamUserId": str(upstream_user_id or "").strip(),
            "upstreamKeyAlias": alias,
        }
        return await self.enqueue_outbox(
            "organization.token.reconcile",
            token_id,
            payload,
            idempotency_key=f"reconcile:{token_id}",
        )

    async def claim_outbox(self, *, limit: int = 20) -> list[dict[str, Any]]:
        pool = self._require_pool()
        max_attempts = self._outbox_max_attempts()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # A process crash must not strand rows forever in ``processing``.
                await conn.execute(
                    "UPDATE customer_outbox SET status=CASE WHEN attempts >= $1 THEN 'failed' ELSE 'pending' END, locked_at=NULL "
                    "WHERE status='processing' AND locked_at < now()-interval '10 minutes'",
                    max_attempts,
                )
                await conn.execute(
                    "UPDATE customer_outbox SET status='failed', locked_at=NULL "
                    "WHERE status='pending' AND attempts >= $1",
                    max_attempts,
                )
                rows = await conn.fetch("SELECT * FROM customer_outbox WHERE status='pending' AND attempts < $1 AND available_at <= now() ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT $2", max_attempts, limit)
                if rows:
                    await conn.execute("UPDATE customer_outbox SET status='processing', locked_at=now(), attempts=attempts+1 WHERE id = ANY($1::text[])", [r["id"] for r in rows])
                return [dict(r) for r in rows]

    async def complete_outbox(self, outbox_id: str, *, error: str = "") -> bool:
        pool = self._require_pool()
        if error:
            attempts = await pool.fetchval(
                "SELECT attempts FROM customer_outbox WHERE id=$1", outbox_id
            )
            max_attempts = self._outbox_max_attempts()
            if attempts is not None and int(attempts) >= max_attempts:
                result = await pool.execute(
                    "UPDATE customer_outbox SET status='failed', locked_at=NULL, available_at=now(), last_error=$2 WHERE id=$1",
                    outbox_id,
                    error[:1000],
                )
            else:
                attempt_number = max(1, int(attempts or 1))
                delay_seconds = min(3600, 30 * (2 ** min(attempt_number - 1, 7)))
                result = await pool.execute(
                    "UPDATE customer_outbox SET status='pending', locked_at=NULL, available_at=now()+$2::interval, last_error=$3 WHERE id=$1",
                    outbox_id,
                    f"{delay_seconds} seconds",
                    error[:1000],
                )
        else:
            result = await pool.execute("UPDATE customer_outbox SET status='sent', locked_at=NULL, sent_at=now(), last_error='' WHERE id=$1", outbox_id)
        return result.endswith("1")

    async def outbox_health(self) -> dict[str, Any]:
        row = await self._require_pool().fetchrow(
            "SELECT count(*) FILTER (WHERE status IN ('pending','processing')) AS pending, "
            "count(*) FILTER (WHERE status='failed') AS failed, "
            "min(created_at) FILTER (WHERE status IN ('pending','processing')) AS oldest "
            "FROM customer_outbox"
        )
        return {
            "pendingCount": int(row["pending"] or 0),
            "failedCount": int(row["failed"] or 0),
            "oldestPendingAt": _iso(row["oldest"]),
        }

    async def settlement_health(self) -> dict[str, Any]:
        """Report durable usage-settlement progress for operations health."""

        row = await self._require_pool().fetchrow(
            "WITH attributed_usage AS ("
            "  SELECT o.id AS organization_id, u.usage_date, "
            "         ROUND(COALESCE(sum(u.spend), 0)::numeric, 6) AS amount "
            "  FROM usage_daily u JOIN customer_organization o "
            "    ON o.upstream_organization_id=u.organization_id "
            "  WHERE u.organization_id <> '' AND u.billing_eligible = TRUE "
            "    AND o.billing_effective_at IS NOT NULL "
            "    AND u.usage_date > (o.billing_effective_at AT TIME ZONE 'UTC')::date "
            "    AND u.usage_date < CURRENT_DATE "
            "  GROUP BY o.id, u.usage_date"
            "), compared AS ("
            "  SELECT COALESCE(u.organization_id,s.organization_id) AS organization_id, "
            "         COALESCE(u.usage_date,s.usage_date) AS usage_date, "
            "         COALESCE(u.amount,0) AS usage_amount, "
            "         COALESCE(s.settled_amount_usd,0) AS settled_amount "
            "  FROM attributed_usage u FULL OUTER JOIN customer_usage_settlement s "
            "    ON s.organization_id=u.organization_id AND s.usage_date=u.usage_date"
            ") SELECT "
            "  (SELECT count(*) FROM customer_usage_settlement) AS settled_count, "
            "  (SELECT max(usage_date) FROM customer_usage_settlement) AS latest_date, "
            "  (SELECT COALESCE(sum(settled_amount_usd),0) FROM customer_usage_settlement) AS settled_total, "
            "  count(*) FILTER (WHERE usage_amount <> settled_amount) AS mismatch_count, "
            "  COALESCE(sum(abs(usage_amount-settled_amount)) "
            "    FILTER (WHERE usage_amount <> settled_amount),0) AS mismatch_total "
            "FROM compared"
        )
        return {
            "settledDayCount": int(row["settled_count"] or 0),
            "latestSettledDate": row["latest_date"].isoformat() if row["latest_date"] else None,
            "settledTotalUsd": _settlement_money(row["settled_total"]),
            "reconciliationDifferenceCount": int(row["mismatch_count"] or 0),
            "reconciliationDifferenceUsd": _settlement_money(row["mismatch_total"]),
        }

    async def billing_effective_at_by_upstream_organization(
        self,
    ) -> dict[str, datetime]:
        """Return settlement cutoffs keyed by the persisted upstream org id."""

        rows = await self._require_pool().fetch(
            "SELECT upstream_organization_id, billing_effective_at "
            "FROM customer_organization "
            "WHERE upstream_organization_id <> '' "
            "AND billing_effective_at IS NOT NULL "
            "AND status NOT IN ('archived', 'suspended')"
        )
        return {
            str(row["upstream_organization_id"]): row["billing_effective_at"]
            for row in rows
            if row["billing_effective_at"] is not None
        }

    async def organization_has_report_only_assets(
        self, organization_id: str
    ) -> bool:
        """Return whether an upstream org contains assets we must not govern."""

        return bool(
            await self._require_pool().fetchval(
                "SELECT EXISTS(SELECT 1 FROM customer_usage_key_identity "
                "WHERE organization_id=$1 AND mode='report_only')",
                organization_id,
            )
        )

    @staticmethod
    async def _billing_projection_payload(
        conn: Any,
        organization_id: str,
        upstream_organization_id: str,
        balance: Decimal,
        billing_status: str,
    ) -> dict[str, Any]:
        preserve_report_only = bool(
            await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM customer_usage_key_identity "
                "WHERE organization_id=$1 AND mode='report_only')",
                organization_id,
            )
        )
        return {
            "organizationId": organization_id,
            "upstreamOrganizationId": upstream_organization_id,
            "balanceUsd": str(balance),
            "billingStatus": billing_status,
            "preserveReportOnlyAssets": preserve_report_only,
        }

    async def settle_usage_rows(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Map upstream organization ids and settle completed daily spend."""

        stats = {"processed": 0, "settled": 0, "idempotent": 0, "unmapped": 0}
        for item in rows:
            upstream_id = str(item.get("upstreamOrganizationId") or "").strip()
            usage_date = item.get("usageDate")
            if not upstream_id or not usage_date:
                stats["unmapped"] += 1
                continue
            organization = await self._require_pool().fetchrow(
                "SELECT id, billing_effective_at FROM customer_organization "
                "WHERE upstream_organization_id=$1",
                upstream_id,
            )
            if not organization:
                stats["unmapped"] += 1
                continue
            billing_effective_at = _row_value(
                organization, "billing_effective_at"
            )
            if billing_effective_at is None:
                # A customer is not billable until the first real grant sets
                # the effective timestamp. Historical reporting stays visible.
                stats["unmapped"] += 1
                continue
            day = self._settlement_date(usage_date)
            cutoff_day = billing_effective_at.astimezone(timezone.utc).date()
            if day < cutoff_day:
                continue
            if day == cutoff_day:
                stats["skipped"] = int(stats.get("skipped", 0)) + 1
                reasons = stats.setdefault("skipReasons", {})
                reasons["needs_event_time"] = int(
                    reasons.get("needs_event_time", 0)
                ) + 1
                continue
            result = await self.settle_usage(
                str(organization["id"]),
                usage_date,
                item.get("spendUsd", 0),
                upstream_organization_id=upstream_id,
            )
            stats["processed"] += 1
            if result.get("idempotent"):
                stats["idempotent"] += 1
            else:
                stats["settled"] += 1
        return stats

    async def record_audit(
        self,
        organization_id: str,
        action: str,
        *,
        actor: str = "",
        target_type: str = "",
        target_id: str = "",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a tenant-scoped audit event without storing credentials."""

        import json

        row = await self._require_pool().fetchrow(
            "INSERT INTO customer_audit_log("
            "id,organization_id,audit_action,actor,target_type,target_id,details"
            ") SELECT $1,id,$3,$4,$5,$6,$7::jsonb FROM customer_organization "
            "WHERE id=$2 RETURNING *",
            self._id(),
            organization_id,
            self._required_text(action, "action", 128),
            str(actor or "")[:254],
            str(target_type or "")[:64],
            str(target_id or "")[:128],
            json.dumps(details or {}, ensure_ascii=False),
        )
        if row is None:
            raise OrganizationNotFoundError("organization was not found")
        return {
            "id": row["id"],
            "organizationId": row["organization_id"],
            "action": row["audit_action"],
            "actor": row["actor"],
            "targetType": row["target_type"],
            "targetId": row["target_id"],
            "createdAt": _iso(row["created_at"]),
        }

    @staticmethod
    def _usage_key_identity_payload(row: Any) -> dict[str, Any]:
        key_hash = str(_row_value(row, "upstream_key_hash", "") or "")
        models = _row_value(row, "models_snapshot", []) or []
        if isinstance(models, str):
            import json

            try:
                models = json.loads(models)
            except ValueError:
                models = []
        return {
            "id": str(_row_value(row, "id", "") or ""),
            "organizationId": str(_row_value(row, "organization_id", "") or ""),
            "principalId": str(_row_value(row, "principal_id", "") or ""),
            "memberId": str(_row_value(row, "member_id", "") or ""),
            "departmentId": str(_row_value(row, "department_id", "") or ""),
            "backendId": str(_row_value(row, "backend_id", "") or ""),
            "upstreamKeyId": str(
                _row_value(row, "upstream_key_id", "") or ""
            ),
            "keyAlias": str(_row_value(row, "key_alias_snapshot", "") or ""),
            "mode": str(_row_value(row, "mode", "") or ""),
            "upstreamOrganizationIdSnapshot": str(
                _row_value(row, "upstream_organization_id_snapshot", "") or ""
            ),
            "upstreamTeamIdSnapshot": str(
                _row_value(row, "upstream_team_id_snapshot", "") or ""
            ),
            "upstreamUserIdSnapshot": str(
                _row_value(row, "upstream_user_id_snapshot", "") or ""
            ),
            "modelsSnapshot": [str(item) for item in models if str(item).strip()],
            "maxBudgetUsdSnapshot": (
                _money(_row_value(row, "max_budget_usd_snapshot"))
                if _row_value(row, "max_budget_usd_snapshot") is not None
                else None
            ),
            "spendUsdSnapshot": (
                _money(_row_value(row, "spend_usd_snapshot"))
                if _row_value(row, "spend_usd_snapshot") is not None
                else None
            ),
            "budgetDurationSnapshot": str(
                _row_value(row, "budget_duration_snapshot", "") or ""
            ),
            "expiresAtSnapshot": _iso(_row_value(row, "expires_at_snapshot")),
            "blockedSnapshot": bool(
                _row_value(row, "blocked_snapshot", False)
            ),
            "importBatchId": str(
                _row_value(row, "import_batch_id", "") or ""
            ),
            "reportingRequestedThrough": (
                _row_value(row, "reporting_requested_through").isoformat()
                if _row_value(row, "reporting_requested_through")
                else None
            ),
            "effectiveFrom": _iso(_row_value(row, "effective_from")),
            "effectiveThrough": _iso(_row_value(row, "effective_through")),
            "billingEligible": bool(_row_value(row, "billing_eligible", False)),
            "idempotencyKey": str(_row_value(row, "idempotency_key", "") or ""),
            "createdBy": str(_row_value(row, "created_by", "") or ""),
            "createdAt": _iso(_row_value(row, "created_at")),
            # Never return the complete upstream hash. The suffix is sufficient
            # for an operator to distinguish mappings in a read-only listing.
            "maskedKey": f"sha256:...{key_hash[-8:]}" if key_hash else "",
        }

    @staticmethod
    def _principal_payload(row: Any, *, upstream_user_ids: list[str] | None = None) -> dict[str, Any]:
        """Expose a principal without making an upstream identity a credential."""

        return {
            "id": str(_row_value(row, "id", "") or ""),
            "organizationId": str(_row_value(row, "organization_id", "") or ""),
            "name": str(_row_value(row, "name", "") or ""),
            "status": str(_row_value(row, "status", "pending") or "pending"),
            "memberId": str(_row_value(row, "member_id", "") or ""),
            "upstreamUserIds": list(dict.fromkeys(str(item) for item in (upstream_user_ids or []) if item)),
            "createdAt": _iso(_row_value(row, "created_at")),
            "updatedAt": _iso(_row_value(row, "updated_at")),
        }

    async def get_principal(
        self, organization_id: str, principal_id: str
    ) -> dict[str, Any] | None:
        """Load a principal only inside its owning organization."""

        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM customer_principal WHERE organization_id=$1 AND id=$2",
                organization_id,
                principal_id,
            )
            if row is None:
                return None
            identities = await conn.fetch(
                "SELECT upstream_user_id FROM customer_principal_upstream_identity "
                "WHERE organization_id=$1 AND principal_id=$2 ORDER BY created_at, id",
                organization_id,
                principal_id,
            )
        return self._principal_payload(
            row,
            upstream_user_ids=[str(_row_value(item, "upstream_user_id", "") or "") for item in identities],
        )

    async def ensure_principal(
        self,
        organization_id: str,
        name: str,
        *,
        principal_id: str = "",
        member_id: str = "",
        status: str = "pending",
    ) -> dict[str, Any]:
        """Get or create a tenant-scoped reporting principal idempotently."""

        organization_id = self._required_text(organization_id, "organization_id", 128)
        name = self._required_text(name, "name", 128)
        principal_id = str(principal_id or "").strip()
        member_id = str(member_id or "").strip()
        status = str(status or "pending").strip().lower()
        if status not in {"pending", "active", "suspended", "archived"}:
            raise OrganizationValidationError("invalid principal status")
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                organization = await conn.fetchrow(
                    "SELECT id FROM customer_organization WHERE id=$1 AND status <> 'archived' FOR UPDATE",
                    organization_id,
                )
                if organization is None:
                    raise OrganizationNotFoundError("organization was not found")
                if member_id:
                    member = await conn.fetchrow(
                        "SELECT id FROM customer_member WHERE organization_id=$1 AND id=$2",
                        organization_id,
                        member_id,
                    )
                    if member is None:
                        raise OrganizationNotFoundError("member was not found")
                existing = await conn.fetchrow(
                    "SELECT * FROM customer_principal WHERE organization_id=$1 AND "
                    "(id=$2 OR (lower(name)=lower($3) AND status <> 'archived')) "
                    "ORDER BY CASE WHEN id=$2 THEN 0 ELSE 1 END LIMIT 1 FOR UPDATE",
                    organization_id,
                    principal_id or "__missing__",
                    name,
                )
                if existing is not None:
                    if principal_id and str(existing["id"]) != principal_id:
                        raise OrganizationConflictError("principal id conflicts with existing name")
                    existing_member = str(_row_value(existing, "member_id", "") or "")
                    if member_id and existing_member and existing_member != member_id:
                        raise OrganizationConflictError("principal is already bound to another member")
                    if member_id and not existing_member:
                        existing = await conn.fetchrow(
                            "UPDATE customer_principal SET member_id=$3, updated_at=now() "
                            "WHERE organization_id=$1 AND id=$2 RETURNING *",
                            organization_id,
                            existing["id"],
                            member_id,
                        )
                    return self._principal_payload(existing)
                new_id = principal_id or self._id()
                try:
                    row = await conn.fetchrow(
                        "INSERT INTO customer_principal(id,organization_id,member_id,name,status) "
                        "VALUES($1,$2,$3,$4,$5) RETURNING *",
                        new_id,
                        organization_id,
                        member_id or None,
                        name,
                        status,
                    )
                except Exception as exc:
                    if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
                        retry = await conn.fetchrow(
                            "SELECT * FROM customer_principal WHERE organization_id=$1 AND lower(name)=lower($2)",
                            organization_id,
                            name,
                        )
                        if retry is not None:
                            return self._principal_payload(retry)
                    raise
        return self._principal_payload(row)

    async def attach_principal_upstream_identity(
        self,
        principal_id: str,
        *,
        backend_id: str,
        upstream_user_id: str,
        organization_id: str | None = None,
    ) -> dict[str, Any]:
        """Attach one immutable upstream user identity to a local principal."""

        principal_id = self._required_text(principal_id, "principal_id", 128)
        backend_id = self._required_text(backend_id, "backend_id", 128)
        upstream_user_id = self._required_text(upstream_user_id, "upstream_user_id", 256)
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                principal = await conn.fetchrow(
                    "SELECT * FROM customer_principal WHERE id=$1 "
                    + ("AND organization_id=$2 " if organization_id else "")
                    + "FOR UPDATE",
                    *( [principal_id, organization_id] if organization_id else [principal_id] ),
                )
                if principal is None:
                    raise OrganizationNotFoundError("principal was not found")
                owner = await conn.fetchrow(
                    "SELECT i.*, p.organization_id AS principal_organization_id "
                    "FROM customer_principal_upstream_identity i "
                    "JOIN customer_principal p ON p.id=i.principal_id "
                    "WHERE i.backend_id=$1 AND i.upstream_user_id=$2 FOR UPDATE",
                    backend_id,
                    upstream_user_id,
                )
                if owner is not None:
                    if str(owner["principal_id"]) != principal_id or str(owner["principal_organization_id"]) != str(principal["organization_id"]):
                        raise OrganizationConflictError("upstream user identity is already mapped to another organization")
                    identities = await conn.fetch(
                        "SELECT upstream_user_id FROM customer_principal_upstream_identity "
                        "WHERE organization_id=$1 AND principal_id=$2 ORDER BY created_at, id",
                        principal["organization_id"],
                        principal_id,
                    )
                    return self._principal_payload(
                        principal,
                        upstream_user_ids=[
                            str(_row_value(item, "upstream_user_id", "") or "")
                            for item in identities
                        ],
                    )
                try:
                    await conn.execute(
                        "INSERT INTO customer_principal_upstream_identity("
                        "id,organization_id,principal_id,backend_id,upstream_user_id) "
                        "VALUES($1,$2,$3,$4,$5)",
                        self._id(),
                        principal["organization_id"],
                        principal_id,
                        backend_id,
                        upstream_user_id,
                    )
                except Exception as exc:
                    if "duplicate" in str(exc).lower() or "unique" in str(exc).lower():
                        raise OrganizationConflictError("upstream user identity is already mapped to another organization") from exc
                    raise
        result = await self.get_principal(str(principal["organization_id"]), principal_id)
        if result is None:  # pragma: no cover - row was locked above
            raise OrganizationNotFoundError("principal was not found")
        return result

    async def link_principal_member(
        self, organization_id: str, principal_id: str, member_id: str
    ) -> dict[str, Any]:
        """Bind a principal to an activated member without changing history."""

        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                principal = await conn.fetchrow(
                    "SELECT * FROM customer_principal WHERE organization_id=$1 AND id=$2 FOR UPDATE",
                    organization_id,
                    principal_id,
                )
                if principal is None:
                    raise OrganizationNotFoundError("principal was not found")
                member = await conn.fetchrow(
                    "SELECT id, department_id FROM customer_member WHERE organization_id=$1 AND id=$2 FOR UPDATE",
                    organization_id,
                    member_id,
                )
                if member is None:
                    raise OrganizationNotFoundError("member was not found")
                existing = str(_row_value(principal, "member_id", "") or "")
                if existing and existing != member_id:
                    raise OrganizationConflictError("principal is already bound to another member")
                row = await conn.fetchrow(
                    "UPDATE customer_principal SET member_id=$3, status=CASE WHEN status='pending' THEN 'active' ELSE status END, updated_at=now() "
                    "WHERE organization_id=$1 AND id=$2 RETURNING *",
                    organization_id,
                    principal_id,
                    member_id,
                )
                await conn.execute(
                    "UPDATE customer_usage_key_identity SET member_id=$3 "
                    "WHERE organization_id=$1 AND principal_id=$2 AND member_id IS NULL",
                    organization_id,
                    principal_id,
                    member_id,
                )
        result = await self.get_principal(organization_id, principal_id)
        if result is None:  # pragma: no cover
            raise OrganizationNotFoundError("principal was not found")
        return result

    async def import_report_only_key_identity(
        self,
        organization_id: str,
        *,
        backend_id: str,
        upstream_key_hash: str,
        upstream_key_id: str = "",
        key_alias: str,
        member_id: str = "",
        principal_id: str = "",
        department_id: str,
        effective_from: datetime,
        effective_through: datetime | None,
        idempotency_key: str,
        upstream_organization_id_snapshot: str = "",
        upstream_team_id_snapshot: str = "",
        upstream_user_id_snapshot: str = "",
        models_snapshot: list[str] | None = None,
        max_budget_usd_snapshot: Any = None,
        spend_usd_snapshot: Any = None,
        budget_duration_snapshot: str = "",
        expires_at_snapshot: datetime | None = None,
        blocked_snapshot: bool = False,
        import_batch_id: str = "",
        reporting_requested_through: date | None = None,
        actor: str = "",
    ) -> dict[str, Any]:
        """Register an existing upstream key for a bounded reporting window.

        This is deliberately a local-only operation. It does not enqueue a key
        job and cannot update, revoke, or otherwise manage the upstream key.
        """

        import json
        import re

        organization_id = self._required_text(organization_id, "organization_id", 128)
        backend_id = self._required_text(backend_id, "backend_id", 128)
        key_hash = str(upstream_key_hash or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", key_hash):
            raise OrganizationValidationError("upstream key hash must be SHA-256")
        member_id = str(member_id or "").strip()
        upstream_key_id = str(upstream_key_id or "").strip()
        principal_id = self._required_text(principal_id, "principal_id", 128)
        department_id = self._required_text(department_id, "department_id", 128)
        idempotency_key = self._required_text(idempotency_key, "idempotency_key", 128)
        if effective_from.tzinfo is None or (
            effective_through is not None and effective_through.tzinfo is None
        ):
            raise OrganizationValidationError("reporting window must be timezone-aware")
        effective_from = effective_from.astimezone(timezone.utc)
        effective_through = (
            effective_through.astimezone(timezone.utc)
            if effective_through is not None
            else None
        )
        if effective_through is not None and effective_from > effective_through:
            raise OrganizationValidationError("reporting window is invalid")
        if expires_at_snapshot is not None:
            if expires_at_snapshot.tzinfo is None:
                raise OrganizationValidationError(
                    "upstream key expiry snapshot must be timezone-aware"
                )
            expires_at_snapshot = expires_at_snapshot.astimezone(timezone.utc)
        models_snapshot = list(
            dict.fromkeys(
                str(model).strip()
                for model in (models_snapshot or [])
                if str(model).strip()
            )
        )

        def optional_decimal(value: Any, field: str) -> Decimal | None:
            if value in (None, ""):
                return None
            try:
                parsed = Decimal(str(value))
            except Exception as exc:
                raise OrganizationValidationError(f"{field} must be numeric") from exc
            if not parsed.is_finite() or parsed < 0 or parsed.as_tuple().exponent < -6:
                raise OrganizationValidationError(
                    f"{field} must be a non-negative amount with at most six decimals"
                )
            return parsed.quantize(Decimal("0.000001"))

        max_budget_snapshot = optional_decimal(
            max_budget_usd_snapshot, "max_budget_usd_snapshot"
        )
        spend_snapshot = optional_decimal(spend_usd_snapshot, "spend_usd_snapshot")
        fingerprint_payload = {
            "organizationId": organization_id,
            "backendId": backend_id,
            "upstreamKeyHash": key_hash,
            "upstreamKeyId": upstream_key_id,
            "keyAlias": str(key_alias or "").strip(),
            "memberId": member_id,
            "principalId": principal_id,
            "departmentId": department_id,
            "upstreamOrganizationIdSnapshot": str(
                upstream_organization_id_snapshot or ""
            ).strip(),
            "upstreamTeamIdSnapshot": str(upstream_team_id_snapshot or "").strip(),
            "upstreamUserIdSnapshot": str(upstream_user_id_snapshot or "").strip(),
            "modelsSnapshot": models_snapshot,
            "maxBudgetUsdSnapshot": (
                str(max_budget_snapshot) if max_budget_snapshot is not None else None
            ),
            "spendUsdSnapshot": (
                str(spend_snapshot) if spend_snapshot is not None else None
            ),
            "budgetDurationSnapshot": str(budget_duration_snapshot or "").strip(),
            "expiresAtSnapshot": (
                expires_at_snapshot.isoformat() if expires_at_snapshot else None
            ),
            "blockedSnapshot": bool(blocked_snapshot),
            "importBatchId": str(import_batch_id or "").strip(),
            "reportingRequestedThrough": (
                reporting_requested_through.isoformat()
                if reporting_requested_through
                else None
            ),
            "effectiveFrom": effective_from.isoformat(),
            "effectiveThrough": (
                effective_through.isoformat() if effective_through is not None else None
            ),
        }
        request_fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow(
                    "SELECT * FROM customer_usage_key_identity "
                    "WHERE organization_id=$1 AND idempotency_key=$2 FOR UPDATE",
                    organization_id,
                    idempotency_key,
                )
                if existing is not None:
                    if str(existing["request_fingerprint"] or "") != request_fingerprint:
                        raise OrganizationConflictError(
                            "idempotency key was already used for a different import"
                        )
                    return self._usage_key_identity_payload(existing)
                organization = await conn.fetchrow(
                    "SELECT id FROM customer_organization WHERE id=$1 AND status <> 'archived'",
                    organization_id,
                )
                if organization is None:
                    raise OrganizationNotFoundError("organization was not found")
                principal = await conn.fetchrow(
                    "SELECT id, member_id FROM customer_principal "
                    "WHERE organization_id=$1 AND id=$2 AND status <> 'archived'",
                    organization_id,
                    principal_id,
                )
                if principal is None:
                    raise OrganizationNotFoundError("principal was not found")
                member = None
                if member_id:
                    member = await conn.fetchrow(
                        "SELECT id, department_id FROM customer_member "
                        "WHERE organization_id=$1 AND id=$2",
                        organization_id,
                        member_id,
                    )
                    if member is None:
                        raise OrganizationNotFoundError("member was not found")
                    bound_member_id = str(_row_value(principal, "member_id", "") or "")
                    if bound_member_id and bound_member_id != member_id:
                        raise OrganizationConflictError(
                            "principal is bound to a different member"
                        )
                department = await conn.fetchrow(
                    "SELECT id FROM customer_department "
                    "WHERE organization_id=$1 AND id=$2",
                    organization_id,
                    department_id,
                )
                if department is None:
                    raise OrganizationNotFoundError("department was not found")
                if member is not None and str(member["department_id"] or "") != department_id:
                    raise OrganizationConflictError(
                        "member does not belong to the selected department"
                    )
                try:
                    row = await conn.fetchrow(
                        "INSERT INTO customer_usage_key_identity("
                        "id,organization_id,principal_id,member_id,department_id,backend_id,"
                        "upstream_key_hash,upstream_key_id,key_alias_snapshot,mode,"
                        "upstream_organization_id_snapshot,upstream_team_id_snapshot,"
                        "upstream_user_id_snapshot,models_snapshot,max_budget_usd_snapshot,"
                        "spend_usd_snapshot,budget_duration_snapshot,expires_at_snapshot,"
                        "blocked_snapshot,import_batch_id,reporting_requested_through,"
                        "effective_from,effective_through,billing_eligible,"
                        "idempotency_key,request_fingerprint,created_by"
                        ") VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,'report_only',$10,$11,$12,$13::jsonb,$14,$15,$16,$17,$18,$19,$20,$21,$22,FALSE,$23,$24,$25) "
                        "RETURNING *",
                        self._id(),
                        organization_id,
                        principal_id,
                        member_id or None,
                        department_id,
                        backend_id,
                        key_hash,
                        upstream_key_id[:256],
                        str(key_alias or "").strip()[:256],
                        str(upstream_organization_id_snapshot or "").strip()[:128],
                        str(upstream_team_id_snapshot or "").strip()[:128],
                        str(upstream_user_id_snapshot or "").strip()[:256],
                        json.dumps(models_snapshot, ensure_ascii=False),
                        max_budget_snapshot,
                        spend_snapshot,
                        str(budget_duration_snapshot or "").strip()[:64],
                        expires_at_snapshot,
                        bool(blocked_snapshot),
                        str(import_batch_id or "").strip()[:128],
                        reporting_requested_through,
                        effective_from,
                        effective_through,
                        idempotency_key,
                        request_fingerprint,
                        str(actor or "")[:254],
                    )
                except Exception as exc:
                    constraint = str(getattr(exc, "constraint_name", "") or "")
                    message = str(exc)
                    if (
                        constraint
                        in {
                            "customer_usage_key_identity_backend_id_upstream_key_hash_key",
                            "customer_usage_key_identity_backend_key_id_idx",
                            "customer_usage_key_identity_organization_id_idempotency_key_key",
                        }
                        or "duplicate key" in message.lower()
                    ):
                        raise OrganizationConflictError(
                            "the upstream key is already claimed by an organization"
                        ) from exc
                    raise
                await conn.execute(
                    "INSERT INTO customer_audit_log("
                    "id,organization_id,audit_action,actor,target_type,target_id,details"
                    ") VALUES($1,$2,'organization.usage_key.imported',$3,'usage_key_identity',$4,$5::jsonb)",
                    self._id(),
                    organization_id,
                    str(actor or "")[:254],
                    str(row["id"]),
                    json.dumps(
                        {
                            "backendId": backend_id,
                            "keyAlias": str(key_alias or "").strip(),
                            "upstreamKeyId": upstream_key_id,
                            "memberId": member_id,
                            "principalId": principal_id,
                            "departmentId": department_id,
                            "upstreamUserIdSnapshot": str(
                                upstream_user_id_snapshot or ""
                            ).strip(),
                            "modelsSnapshot": models_snapshot,
                            "maxBudgetUsdSnapshot": (
                                str(max_budget_snapshot)
                                if max_budget_snapshot is not None
                                else None
                            ),
                            "spendUsdSnapshot": (
                                str(spend_snapshot)
                                if spend_snapshot is not None
                                else None
                            ),
                            "budgetDurationSnapshot": str(
                                budget_duration_snapshot or ""
                            ).strip(),
                            "expiresAtSnapshot": (
                                expires_at_snapshot.isoformat()
                                if expires_at_snapshot
                                else None
                            ),
                            "blockedSnapshot": bool(blocked_snapshot),
                            "importBatchId": str(import_batch_id or "").strip(),
                            "reportingRequestedThrough": (
                                reporting_requested_through.isoformat()
                                if reporting_requested_through
                                else None
                            ),
                            "effectiveFrom": effective_from.isoformat(),
                            "effectiveThrough": (
                                effective_through.isoformat()
                                if effective_through is not None
                                else None
                            ),
                            "billingEligible": False,
                        },
                        ensure_ascii=False,
                    ),
                )
        return self._usage_key_identity_payload(row)

    async def ensure_usage_backfill(
        self,
        organization_id: str,
        *,
        principal_id: str,
        usage_key_identity_id: str,
        backend_id: str,
        requested_from: date,
        requested_through: date,
        import_batch_id: str = "",
    ) -> dict[str, Any]:
        """Create an idempotent, three-day-window historical log backfill."""

        if requested_from > requested_through:
            raise OrganizationValidationError("backfill range is invalid")
        row = await self._require_pool().fetchrow(
            "INSERT INTO customer_usage_backfill("
            "id,organization_id,principal_id,usage_key_identity_id,backend_id,"
            "requested_from,requested_through,next_date,import_batch_id"
            ") SELECT $1,k.organization_id,k.principal_id,k.id,k.backend_id,$5,$6,$5,$7 "
            "FROM customer_usage_key_identity k "
            "WHERE k.id=$4 AND k.organization_id=$2 AND k.principal_id=$3 "
            "ON CONFLICT (usage_key_identity_id,requested_from,requested_through) "
            "DO UPDATE SET import_batch_id=EXCLUDED.import_batch_id RETURNING *",
            self._id(),
            organization_id,
            principal_id,
            usage_key_identity_id,
            requested_from,
            requested_through,
            str(import_batch_id or "")[:128],
        )
        if row is None:
            raise OrganizationNotFoundError("usage key identity was not found")
        return {
            "id": str(row["id"]),
            "organizationId": str(row["organization_id"]),
            "principalId": str(row["principal_id"]),
            "usageKeyIdentityId": str(row["usage_key_identity_id"]),
            "backendId": str(row["backend_id"]),
            "requestedFrom": row["requested_from"].isoformat(),
            "requestedThrough": row["requested_through"].isoformat(),
            "coveredFrom": (
                row["covered_from"].isoformat() if row["covered_from"] else None
            ),
            "coveredThrough": (
                row["covered_through"].isoformat()
                if row["covered_through"]
                else None
            ),
            "nextDate": row["next_date"].isoformat(),
            "status": str(row["status"]),
            "lastError": str(row["last_error"] or ""),
            "lastSyncedAt": _iso(row["last_synced_at"]),
        }

    async def adopt_organization_with_admin(
        self,
        name: str,
        admin_name: str,
        admin_email: str,
        *,
        department_name: str,
        upstream_organization_id: str,
        upstream_team_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Persist an exact upstream adoption without provisioning new objects."""

        import json

        name = self._required_text(name, "name", 128)
        admin_name = self._required_text(admin_name, "admin_name", 128)
        admin_email = self.normalize_email(admin_email)
        department_name = self._required_text(department_name, "department_name", 128)
        upstream_organization_id = self._required_text(
            upstream_organization_id, "upstream_organization_id", 128
        )
        upstream_team_id = self._required_text(upstream_team_id, "upstream_team_id", 128)
        idempotency_key = self._required_text(idempotency_key, "idempotency_key", 128)
        stable = hashlib.sha256(f"organization-adoption:{idempotency_key}".encode()).hexdigest()
        organization_id = f"org-{stable[:24]}"
        department_id = f"dept-{stable[24:48]}"
        member_id = f"member-{stable[8:32]}"
        pool = self._require_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    organization = await conn.fetchrow(
                        "SELECT * FROM customer_organization WHERE id=$1 FOR UPDATE",
                        organization_id,
                    )
                    if organization is None:
                        organization = await conn.fetchrow(
                            "INSERT INTO customer_organization("
                            "id,name,status,upstream_organization_id,upstream_status"
                            ") VALUES($1,$2,'active',$3,'active') RETURNING *",
                            organization_id,
                            name,
                            upstream_organization_id,
                        )
                        department = await conn.fetchrow(
                            "INSERT INTO customer_department("
                            "id,organization_id,name,upstream_team_id"
                            ") VALUES($1,$2,$3,$4) RETURNING *",
                            department_id,
                            organization_id,
                            department_name,
                            upstream_team_id,
                        )
                        member = await conn.fetchrow(
                            "INSERT INTO customer_member("
                            "id,organization_id,department_id,name,email,role,status,team_role"
                            ") VALUES($1,$2,$3,$4,$5,'admin','invited','leader') RETURNING *",
                            member_id,
                            organization_id,
                            department_id,
                            admin_name,
                            admin_email,
                        )
                        await conn.execute(
                            "INSERT INTO customer_audit_log("
                            "id,organization_id,audit_action,actor,target_type,target_id,details"
                            ") VALUES($1,$2,'organization.adopted','platform','organization',$2,$3::jsonb)",
                            self._id(),
                            organization_id,
                            json.dumps(
                                {
                                    "upstreamOrganizationId": upstream_organization_id,
                                    "upstreamTeamId": upstream_team_id,
                                    "idempotencyKey": idempotency_key,
                                },
                                ensure_ascii=False,
                            ),
                        )
                    else:
                        if (
                            str(organization["name"]) != name
                            or str(organization["upstream_organization_id"] or "")
                            != upstream_organization_id
                        ):
                            raise OrganizationConflictError(
                                "idempotency key was already used for another adoption"
                            )
                        department = await conn.fetchrow(
                            "SELECT * FROM customer_department WHERE id=$1 AND organization_id=$2",
                            department_id,
                            organization_id,
                        )
                        member = await conn.fetchrow(
                            "SELECT * FROM customer_member WHERE id=$1 AND organization_id=$2",
                            member_id,
                            organization_id,
                        )
                        if (
                            department is None
                            or member is None
                            or str(department["upstream_team_id"] or "") != upstream_team_id
                            or str(member["email"] or "").casefold() != admin_email.casefold()
                        ):
                            raise OrganizationConflictError(
                                "existing adoption state is incomplete or conflicting"
                            )
        except OrganizationConflictError:
            raise
        except Exception as exc:
            if "duplicate key" in str(exc).lower():
                raise OrganizationConflictError(
                    "an upstream object or administrator is already claimed"
                ) from exc
            raise
        await self.ensure_member_invitation(organization_id, member_id)
        return {
            "organization": self._org_payload(organization),
            "department": self._dept_payload(department),
            "admin": await self._member_payload_with_department(
                member, organization_id=organization_id
            ),
        }

    async def adoption_mapping_conflicts(
        self, upstream_organization_id: str, upstream_team_id: str
    ) -> list[dict[str, str]]:
        rows = await self._require_pool().fetch(
            "SELECT 'organization' AS kind,id FROM customer_organization "
            "WHERE upstream_organization_id=$1 UNION ALL "
            "SELECT 'department' AS kind,id FROM customer_department "
            "WHERE upstream_team_id=$2",
            str(upstream_organization_id or "").strip(),
            str(upstream_team_id or "").strip(),
        )
        return [{"kind": str(row["kind"]), "id": str(row["id"])} for row in rows]

    async def begin_adoption_operation(
        self,
        operation_key: str,
        *,
        request_fingerprint: str,
        preview_fingerprint: str,
        actor: str = "",
    ) -> dict[str, Any]:
        """Reserve an adoption idempotency key before making local writes."""

        import json

        operation_key = self._required_text(operation_key, "operation_key", 128)
        request_fingerprint = self._required_text(
            request_fingerprint, "request_fingerprint", 64
        )
        preview_fingerprint = self._required_text(
            preview_fingerprint, "preview_fingerprint", 64
        )
        row = await self._require_pool().fetchrow(
            "INSERT INTO customer_adoption_operation("
            "id,operation_key,request_fingerprint,preview_fingerprint,created_by"
            ") VALUES($1,$2,$3,$4,$5) ON CONFLICT (operation_key) DO UPDATE "
            "SET operation_key=EXCLUDED.operation_key RETURNING *",
            self._id(),
            operation_key,
            request_fingerprint,
            preview_fingerprint,
            str(actor or "")[:254],
        )
        if (
            str(row["request_fingerprint"] or "") != request_fingerprint
            or str(row["preview_fingerprint"] or "") != preview_fingerprint
        ):
            raise OrganizationConflictError(
                "idempotency key was already used for another adoption"
            )
        result = _row_value(row, "result", {}) or {}
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                result = {}
        return {
            "id": str(row["id"]),
            "status": str(row["status"]),
            "organizationId": str(_row_value(row, "organization_id", "") or ""),
            "result": result if isinstance(result, dict) else {},
        }

    async def get_adoption_operation(
        self, operation_key: str
    ) -> dict[str, Any] | None:
        import json

        row = await self._require_pool().fetchrow(
            "SELECT * FROM customer_adoption_operation WHERE operation_key=$1",
            str(operation_key or "").strip(),
        )
        if row is None:
            return None
        result = _row_value(row, "result", {}) or {}
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                result = {}
        return {
            "id": str(row["id"]),
            "status": str(row["status"]),
            "requestFingerprint": str(row["request_fingerprint"]),
            "previewFingerprint": str(row["preview_fingerprint"]),
            "organizationId": str(_row_value(row, "organization_id", "") or ""),
            "result": result if isinstance(result, dict) else {},
        }

    async def complete_adoption_operation(
        self,
        operation_id: str,
        organization_id: str,
        result: dict[str, Any],
    ) -> None:
        import json

        updated = await self._require_pool().execute(
            "UPDATE customer_adoption_operation SET organization_id=$2,status='applied',"
            "result=$3::jsonb,last_error='',updated_at=now() WHERE id=$1",
            operation_id,
            organization_id,
            json.dumps(result, ensure_ascii=False),
        )
        if not updated.endswith("1"):
            raise OrganizationNotFoundError("adoption operation was not found")

    async def fail_adoption_operation(self, operation_id: str, error: str) -> None:
        await self._require_pool().execute(
            "UPDATE customer_adoption_operation SET status='failed',last_error=$2,"
            "updated_at=now() WHERE id=$1 AND status<>'applied'",
            operation_id,
            str(error or "")[:1000],
        )

    async def create_token_record(self, organization_id: str, name: str, models: list[str], *, member_id: str = "", daily_budget_usd: Any = DEFAULT_TOKEN_DAILY_BUDGET_USD, duration: str = "never", expires_at: datetime | None = None, upstream_key_alias: str = "") -> dict[str, Any]:
        budget = self._token_daily_budget(daily_budget_usd)
        if not isinstance(models, list) or not all(isinstance(model, str) and model.strip() for model in models):
            raise OrganizationConflictError("models must be a non-empty list")
        models = list(dict.fromkeys(model.strip() for model in models))
        if not models:
            raise OrganizationConflictError("models must be a non-empty list")
        alias = str(upstream_key_alias or "").strip()
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Serialize with credit/status changes so a token cannot slip
                # through after the organization or member becomes ineligible.
                organization = await conn.fetchrow(
                    "SELECT id, status, billing_status, billing_balance_usd AS balance "
                    "FROM customer_organization WHERE id=$1 FOR UPDATE",
                    organization_id,
                )
                if organization is None or str(organization["status"] or "") != "active":
                    raise OrganizationNotFoundError("active organization was not found")
                billing_status = str(_row_value(organization, "billing_status", "past_due") or "past_due")
                balance = Decimal(str(_row_value(organization, "balance", 0) or 0))
                if billing_status != "active" or balance <= 0:
                    raise OrganizationConflictError("organization credit is insufficient")

                member = None
                if member_id:
                    member = await conn.fetchrow(
                        "SELECT id, department_id FROM customer_member "
                        "WHERE id=$1 AND organization_id=$2 AND status='active' FOR UPDATE",
                        member_id,
                        organization_id,
                    )
                    if member is None:
                        raise OrganizationNotFoundError("active member was not found")

                department_id = str(_row_value(member, "department_id", "") or "")
                upstream_team_id = ""
                if department_id:
                    department = await conn.fetchrow(
                        "SELECT upstream_team_id FROM customer_department "
                        "WHERE id=$1 AND organization_id=$2 AND status='active' FOR UPDATE",
                        department_id,
                        organization_id,
                    )
                    if department is None:
                        raise OrganizationNotFoundError("active department was not found")
                    upstream_team_id = str(department["upstream_team_id"] or "")

                try:
                    row = await conn.fetchrow(
                        "INSERT INTO customer_access_token(id,organization_id,member_id,department_id,name,models,daily_budget_usd,duration,expires_at,upstream_key_alias,upstream_team_id) "
                        "VALUES($1,$2,NULLIF($3,''),NULLIF($4,''),$5,$6::jsonb,$7,$8,$9,$10,$11) RETURNING *",
                        self._id(), organization_id, member_id, department_id,
                        self._required_text(name, "name", 128),
                        __import__('json').dumps(models), budget,
                        self._validate_token_duration(duration), expires_at,
                        alias, upstream_team_id,
                    )
                except Exception as exc:
                    constraint = str(getattr(exc, "constraint_name", "") or "")
                    message = str(exc)
                    if alias and (
                        constraint == "customer_token_alias_idx"
                        or "customer_token_alias_idx" in message
                    ):
                        # LiteLLM 1.92 does not consume Idempotency-Key.  The
                        # database alias constraint is therefore the
                        # cross-process guard that stops a concurrent request
                        # from creating a second upstream credential.
                        raise OrganizationConflictError(
                            "a token request with this alias already exists"
                        ) from exc
                    raise
        return self._token_payload(row, secret=None)

    @staticmethod
    def _token_payload(row: Any, *, secret: str | None) -> dict[str, Any]:
        models = _row_value(row, "models", [])
        if isinstance(models, str):
            import json
            models = json.loads(models)
        stored_hash = str(_row_value(row, "upstream_key_hash", "") or "")
        expires_at = _row_value(row, "expires_at")
        raw_status = str(_row_value(row, "status", "provisioning") or "provisioning")
        effective_status = raw_status
        if raw_status == "active" and expires_at is not None:
            try:
                expiry = expires_at if isinstance(expires_at, datetime) else datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry <= datetime.now(timezone.utc):
                    effective_status = "expired"
            except (TypeError, ValueError):
                pass
        # Only a hash survives persistence.  Derive a stable non-secret mask
        # for list payloads instead of exposing the hash itself as a token.
        masked = f"sk-...{secret[-4:]}" if secret else (f"sk-...{stored_hash[-4:]}" if stored_hash else "")
        member_id = str(_row_value(row, "member_id", "") or "")
        department_id = str(_row_value(row, "department_id", "") or "")
        result = {
            "id": _row_value(row, "id", ""),
            "organizationId": _row_value(row, "organization_id", ""),
            "memberId": member_id,
            "memberName": str(_row_value(row, "member_name", "") or ""),
            "memberEmail": str(_row_value(row, "member_email", "") or ""),
            "departmentId": department_id,
            "departmentName": str(_row_value(row, "department_name", "") or ""),
            "isShared": not bool(member_id),
            "upstreamTeamId": _row_value(row, "upstream_team_id") or None,
            "upstreamKeyAlias": _row_value(row, "upstream_key_alias", "") or "",
            "name": _row_value(row, "name", ""),
            "models": models or [],
            "status": effective_status,
            "dailyBudgetUsd": _money(_row_value(row, "daily_budget_usd", 0)),
            "duration": _row_value(row, "duration", "never"),
            "masked": masked,
            "upstreamKeyId": _row_value(row, "upstream_key_id") or None,
            "createdAt": _iso(_row_value(row, "created_at")),
            "updatedAt": _iso(_row_value(row, "updated_at")),
            "expiresAt": _iso(_row_value(row, "expires_at")),
            "revokedAt": _iso(_row_value(row, "revoked_at")),
        }
        if secret: result["token"] = secret
        return result

    async def finalize_token_record(self, token_id: str, *, upstream_key_id: str, upstream_key_hash: str = "", status: str = "active", plaintext_token: str | None = None) -> dict[str, Any]:
        """Activate a provisioned key only while its complete scope is eligible.

        The upstream create happens outside the database transaction, so credit,
        organization, member, or department state can change before this method
        runs. Re-check those rows under the same locks used by the mutations and
        make the transition compare-and-set from ``provisioning``.
        """

        pool = self._require_pool()
        rejection_reason = ""
        row = None
        async with pool.acquire() as conn:
            async with conn.transaction():
                token = await conn.fetchrow(
                    "SELECT * FROM customer_access_token WHERE id=$1 FOR UPDATE",
                    token_id,
                )
                if token is None:
                    raise OrganizationNotFoundError("access token was not found")
                if str(token["status"] or "") != "provisioning" or str(token["upstream_key_id"] or ""):
                    raise OrganizationConflictError("access token is no longer awaiting activation")

                organization_id = str(token["organization_id"] or "")
                organization = await conn.fetchrow(
                    "SELECT id, status, billing_status, billing_balance_usd AS balance "
                    "FROM customer_organization WHERE id=$1 FOR UPDATE",
                    organization_id,
                )
                balance = Decimal(str(_row_value(organization, "balance", 0) or 0))
                if (
                    organization is None
                    or str(organization["status"] or "") != "active"
                    or str(organization["billing_status"] or "") != "active"
                    or balance <= 0
                ):
                    row = await conn.fetchrow(
                        "UPDATE customer_access_token SET status='revoked', revoked_at=now(), updated_at=now() "
                        "WHERE id=$1 AND status='provisioning' RETURNING *",
                        token_id,
                    )
                    rejection_reason = "organization is no longer eligible for tokens"

                member_id = str(token["member_id"] or "")
                if not rejection_reason and member_id:
                    member = await conn.fetchrow(
                        "SELECT id, department_id, status, upstream_user_id "
                        "FROM customer_member WHERE id=$1 AND organization_id=$2 FOR UPDATE",
                        member_id,
                        organization_id,
                    )
                    if (
                        member is None
                        or str(member["status"] or "") != "active"
                        or not str(member["upstream_user_id"] or "")
                        or str(member["department_id"] or "") != str(token["department_id"] or "")
                    ):
                        row = await conn.fetchrow(
                            "UPDATE customer_access_token SET status='revoked', revoked_at=now(), updated_at=now() "
                            "WHERE id=$1 AND status='provisioning' RETURNING *",
                            token_id,
                        )
                        rejection_reason = "member is no longer eligible for tokens"

                department_id = str(token["department_id"] or "")
                if not rejection_reason and department_id:
                    department = await conn.fetchrow(
                        "SELECT id, status, upstream_team_id FROM customer_department "
                        "WHERE id=$1 AND organization_id=$2 FOR UPDATE",
                        department_id,
                        organization_id,
                    )
                    if (
                        department is None
                        or str(department["status"] or "") != "active"
                        or str(department["upstream_team_id"] or "")
                        != str(token["upstream_team_id"] or "")
                    ):
                        row = await conn.fetchrow(
                            "UPDATE customer_access_token SET status='revoked', revoked_at=now(), updated_at=now() "
                            "WHERE id=$1 AND status='provisioning' RETURNING *",
                            token_id,
                        )
                        rejection_reason = "department is no longer eligible for tokens"

                if not rejection_reason:
                    row = await conn.fetchrow(
                        "UPDATE customer_access_token SET upstream_key_id=$2, "
                        "upstream_key_hash=$3, status=$4, updated_at=now() "
                        "WHERE id=$1 AND status='provisioning' AND upstream_key_id='' RETURNING *",
                        token_id,
                        upstream_key_id,
                        upstream_key_hash,
                        status,
                    )
                    if row is None:
                        raise OrganizationConflictError("access token is no longer awaiting activation")
        if rejection_reason:
            raise OrganizationConflictError(rejection_reason)
        if row is None:
            raise OrganizationConflictError("access token is no longer awaiting activation")
        return self._token_payload(row, secret=plaintext_token)

    async def get_token_by_alias(self, upstream_key_alias: str) -> dict[str, Any] | None:
        row = await self._require_pool().fetchrow(
            "SELECT t.*, m.name AS member_name, m.email AS member_email, "
            "d.name AS department_name FROM customer_access_token t "
            "LEFT JOIN customer_member m ON m.id=t.member_id "
            "LEFT JOIN customer_department d ON d.id=t.department_id "
            "WHERE t.upstream_key_alias=$1 ORDER BY t.created_at DESC LIMIT 1",
            upstream_key_alias,
        )
        return self._token_payload(row, secret=None) if row else None

    async def usage_token_attribution_map(self) -> list[dict[str, Any]]:
        """Return secret-free token mappings used to attribute spend logs.

        Spend logs normally carry the upstream token hash, while older proxy
        versions may emit the stable key id or alias.  Keep all three lookup
        values so the synchronizer can apply the documented precedence without
        inferring a tenant from an email address.
        """

        rows = await self._require_pool().fetch(
            "SELECT t.upstream_key_id, t.upstream_key_hash, t.upstream_key_alias, "
            "       t.upstream_team_id, t.member_id, t.department_id, "
            "       o.upstream_organization_id, m.upstream_user_id "
            "FROM customer_access_token t "
            "JOIN customer_organization o ON o.id=t.organization_id "
            "LEFT JOIN customer_member m ON m.id=t.member_id "
            "WHERE o.upstream_organization_id <> ''"
        )
        managed = [
            {
                "backendId": "primary",
                "upstreamKeyId": str(_row_value(row, "upstream_key_id", "") or ""),
                "upstreamKeyHash": str(_row_value(row, "upstream_key_hash", "") or ""),
                "upstreamKeyAlias": str(_row_value(row, "upstream_key_alias", "") or ""),
                "organizationId": str(_row_value(row, "upstream_organization_id", "") or ""),
                "teamId": str(_row_value(row, "upstream_team_id", "") or ""),
                "userId": str(_row_value(row, "upstream_user_id", "") or ""),
                "memberId": str(_row_value(row, "member_id", "") or ""),
                "departmentId": str(_row_value(row, "department_id", "") or ""),
                "mode": "managed",
                "attributionSource": "managed_token",
                "billingEligible": True,
                "effectiveFrom": None,
                "effectiveThrough": None,
            }
            for row in rows
        ]
        report_rows = await self._require_pool().fetch(
            "SELECT k.*, o.upstream_organization_id AS current_upstream_organization_id, "
            "       COALESCE(NULLIF(k.upstream_user_id_snapshot, ''), m.upstream_user_id, '') AS member_upstream_user_id, "
            "       p.name AS principal_name, p.member_id AS principal_member_id "
            "FROM customer_usage_key_identity k "
            "JOIN customer_organization o ON o.id=k.organization_id "
            "JOIN customer_principal p ON p.organization_id=k.organization_id AND p.id=k.principal_id "
            "LEFT JOIN customer_member m ON m.id=k.member_id "
            "WHERE k.mode='report_only'"
        )
        report_only = [
            {
                "backendId": str(_row_value(row, "backend_id", "") or ""),
                "upstreamKeyId": str(
                    _row_value(row, "upstream_key_id", "") or ""
                ),
                "upstreamKeyHash": str(
                    _row_value(row, "upstream_key_hash", "") or ""
                ),
                # Alias is display/audit evidence only. The synchronizer never
                # indexes a report-only mapping by alias.
                "upstreamKeyAlias": str(
                    _row_value(row, "key_alias_snapshot", "") or ""
                ),
                "organizationId": str(
                    _row_value(row, "upstream_organization_id_snapshot", "")
                    or _row_value(row, "current_upstream_organization_id", "")
                    or ""
                ),
                "teamId": str(
                    _row_value(row, "upstream_team_id_snapshot", "") or ""
                ),
                "userId": str(_row_value(row, "member_upstream_user_id", "") or ""),
                "principalId": str(_row_value(row, "principal_id", "") or ""),
                "principalName": str(_row_value(row, "principal_name", "") or ""),
                "memberId": str(_row_value(row, "member_id", "") or ""),
                "departmentId": str(
                    _row_value(row, "department_id", "") or ""
                ),
                "mode": "report_only",
                "attributionSource": "legacy_report_only",
                "billingEligible": False,
                "effectiveFrom": _iso(_row_value(row, "effective_from")),
                "effectiveThrough": _iso(_row_value(row, "effective_through")),
                "modelsSnapshot": _row_value(row, "models_snapshot", []) or [],
                "maxBudgetUsdSnapshot": _money(
                    _row_value(row, "max_budget_usd_snapshot", 0)
                ) if _row_value(row, "max_budget_usd_snapshot") is not None else None,
                "spendUsdSnapshot": _money(
                    _row_value(row, "spend_usd_snapshot", 0)
                ) if _row_value(row, "spend_usd_snapshot") is not None else None,
                "budgetDurationSnapshot": str(
                    _row_value(row, "budget_duration_snapshot", "") or ""
                ),
                "expiresAtSnapshot": _iso(
                    _row_value(row, "expires_at_snapshot")
                ),
                "blockedSnapshot": bool(
                    _row_value(row, "blocked_snapshot", False)
                ),
            }
            for row in report_rows
        ]
        return managed + report_only

    async def list_tokens(self, organization_id: str, *, keyword: str = "", status: str = "", member_id: str = "", page: int = 1, page_size: int = 20, available_models: Any = None) -> dict[str, Any]:
        page, page_size = self._page_value(page, "page", 100000), self._page_value(page_size, "page_size", 100)
        if member_id:
            member_exists = await self._require_pool().fetchval(
                "SELECT true FROM customer_member WHERE id=$1 AND organization_id=$2",
                member_id,
                organization_id,
            )
            if not member_exists:
                raise OrganizationNotFoundError("member was not found")
        clauses, args = ["t.organization_id=$1"], [organization_id]
        if status:
            args.append(status)
            clauses.append(
                f"(CASE WHEN t.status='active' AND t.expires_at IS NOT NULL "
                f"AND t.expires_at <= now() THEN 'expired' ELSE t.status END)=${len(args)}"
            )
        if member_id: args.append(member_id); clauses.append(f"t.member_id=${len(args)}")
        if keyword:
            args.append(f"%{keyword.strip()}%")
            clauses.append(
                f"(t.name ILIKE ${len(args)} OR t.models::text ILIKE ${len(args)} "
                f"OR m.name ILIKE ${len(args)} OR m.email ILIKE ${len(args)} "
                f"OR d.name ILIKE ${len(args)})"
            )
        where = " AND ".join(clauses)
        pool = self._require_pool()
        total = await pool.fetchval(
            f"SELECT count(*) FROM customer_access_token t "
            f"LEFT JOIN customer_member m ON m.id=t.member_id "
            f"LEFT JOIN customer_department d ON d.id=t.department_id "
            f"WHERE {where}",
            *args,
        )
        args.extend([page_size, (page - 1) * page_size])
        rows = await pool.fetch(
            f"SELECT t.*, m.name AS member_name, m.email AS member_email, "
            f"d.name AS department_name "
            f"FROM customer_access_token t "
            f"LEFT JOIN customer_member m ON m.id=t.member_id "
            f"LEFT JOIN customer_department d ON d.id=t.department_id "
            f"WHERE {where} ORDER BY t.created_at DESC "
            f"LIMIT ${len(args)-1} OFFSET ${len(args)}",
            *args,
        )
        stats_row = await pool.fetchrow(
            "SELECT count(*) AS total, "
            "count(*) FILTER (WHERE status='active' AND "
            "    (expires_at IS NULL OR expires_at > now())) AS active_count, "
            "count(*) FILTER (WHERE status='revoked') AS revoked_count, "
            "count(*) FILTER (WHERE status='expired' OR "
            "    (status='active' AND expires_at IS NOT NULL AND expires_at <= now())) AS expired_count, "
            "count(*) FILTER (WHERE status='provisioning') AS provisioning_count, "
            "count(DISTINCT member_id) FILTER (WHERE status='active' AND "
            "    (expires_at IS NULL OR expires_at > now()) AND member_id IS NOT NULL) AS bound_member_count "
            "FROM customer_access_token WHERE organization_id=$1",
            organization_id,
        )
        bindable_rows = await pool.fetch(
            "SELECT m.id, m.name, m.email, m.department_id, d.name AS department_name "
            "FROM customer_member m "
            "JOIN customer_department d ON d.id=m.department_id "
            "WHERE m.organization_id=$1 AND m.status='active' "
            "AND m.upstream_user_id <> '' AND d.status='active' "
            "ORDER BY lower(m.name), m.id",
            organization_id,
        )
        imported_rows = await pool.fetch(
            "SELECT k.*, m.name AS member_name, m.email AS member_email, "
            "m.login_name AS member_login_name, d.name AS department_name, "
            "p.name AS principal_name, p.member_id AS principal_member_id "
            "FROM customer_usage_key_identity k "
            "JOIN customer_principal p ON p.organization_id=k.organization_id AND p.id=k.principal_id "
            "LEFT JOIN customer_member m ON m.id=k.member_id "
            "LEFT JOIN customer_department d ON d.id=k.department_id "
            "WHERE k.organization_id=$1 AND k.mode='report_only' "
            "ORDER BY k.created_at DESC",
            organization_id,
        )
        imported_items = []
        for row in imported_rows:
            if member_id and str(_row_value(row, "member_id", "") or "") != member_id:
                continue
            if status and status != "active":
                continue
            if keyword and keyword.strip().casefold() not in " ".join(
                str(_row_value(row, field, "") or "")
                for field in (
                    "key_alias_snapshot",
                    "member_name",
                    "member_email",
                    "member_login_name",
                    "department_name",
                )
            ).casefold():
                continue
            models_snapshot = _row_value(row, "models_snapshot", []) or []
            if isinstance(models_snapshot, str):
                import json

                try:
                    models_snapshot = json.loads(models_snapshot)
                except ValueError:
                    models_snapshot = []
            imported_items.append({
                "id": f"imported-{_row_value(row, 'id', '')}",
                "organizationId": organization_id,
                "principalId": str(_row_value(row, "principal_id", "") or ""),
                "memberId": str(_row_value(row, "member_id", "") or ""),
                "memberName": str(
                    _row_value(row, "member_name", "")
                    or _row_value(row, "principal_name", "")
                    or ""
                ),
                "memberEmail": str(
                    _row_value(row, "member_email", "")
                    or _row_value(row, "member_login_name", "")
                    or ""
                ),
                "departmentId": str(_row_value(row, "department_id", "") or ""),
                "departmentName": str(_row_value(row, "department_name", "") or ""),
                "isShared": not bool(_row_value(row, "member_id", "")),
                "name": str(_row_value(row, "key_alias_snapshot", "") or "历史资产"),
                "models": [str(item) for item in models_snapshot if str(item).strip()],
                "status": "active",
                "dailyBudgetUsd": _money(
                    _row_value(row, "max_budget_usd_snapshot", 0)
                ) if _row_value(row, "max_budget_usd_snapshot") is not None else 0.0,
                "duration": "never",
                "masked": self._usage_key_identity_payload(row).get("maskedKey", ""),
                "createdAt": _iso(_row_value(row, "created_at")),
                "updatedAt": _iso(_row_value(row, "created_at")),
                "expiresAt": _iso(_row_value(row, "expires_at_snapshot")),
                "spendUsdSnapshot": _money(
                    _row_value(row, "spend_usd_snapshot", 0)
                ) if _row_value(row, "spend_usd_snapshot") is not None else None,
                "budgetDurationSnapshot": str(
                    _row_value(row, "budget_duration_snapshot", "") or ""
                ),
                "reportingFrom": _iso(_row_value(row, "effective_from")),
                "reportingThrough": _iso(_row_value(row, "effective_through")),
                "source": "imported",
                "managementMode": "read_only",
                "billingMode": "report_only",
                "reportOnly": True,
                "billingEligible": False,
            })
        imported_total = len(imported_items)
        if page != 1:
            imported_items = []
        stats = {
            "total": int(_row_value(stats_row, "total", 0) or 0),
            "activeCount": int(_row_value(stats_row, "active_count", 0) or 0),
            "revokedCount": int(_row_value(stats_row, "revoked_count", 0) or 0),
            "expiredCount": int(_row_value(stats_row, "expired_count", 0) or 0),
            "provisioningCount": int(_row_value(stats_row, "provisioning_count", 0) or 0),
            "boundMemberCount": int(_row_value(stats_row, "bound_member_count", 0) or 0),
            "maxTokenCount": MAX_TOKENS_PER_ORGANIZATION,
        }
        stats["total"] += imported_total
        stats["reportOnlyCount"] = imported_total
        bindable = [
            {
                "id": _row_value(row, "id", ""),
                "name": _row_value(row, "name", ""),
                "email": _row_value(row, "email", ""),
                "departmentId": _row_value(row, "department_id", "") or "",
                "departmentName": _row_value(row, "department_name", "") or "",
            }
            for row in bindable_rows
        ]
        return {
            "items": imported_items + [self._token_payload(r, secret=None) for r in rows],
            "total": int(total or 0) + imported_total,
            "page": page,
            "pageSize": page_size,
            "stats": stats,
            "bindableMembers": bindable,
            "availableModels": list(available_models or []),
            "isDemo": False,
        }

    async def get_token(
        self, organization_id: str, token_id: str
    ) -> dict[str, Any] | None:
        row = await self._require_pool().fetchrow(
            "SELECT * FROM customer_access_token WHERE id=$1 AND organization_id=$2",
            token_id,
            organization_id,
        )
        return self._token_payload(row, secret=None) if row else None

    async def mark_token_revoked(self, organization_id: str, token_id: str) -> dict[str, Any]:
        row = await self._require_pool().fetchrow(
            "UPDATE customer_access_token SET status='revoked', revoked_at=now(), updated_at=now() "
            "WHERE id=$1 AND organization_id=$2 RETURNING *",
            token_id,
            organization_id,
        )
        if row is None:
            raise OrganizationNotFoundError("access token was not found")
        return self._token_payload(row, secret=None)

    async def revoke_token(self, organization_id: str, token_id: str) -> dict[str, Any]:
        """Compatibility alias for callers that do not coordinate upstream revocation."""

        return await self.mark_token_revoked(organization_id, token_id)

    async def billing_payload(self, organization_id: str, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        page, page_size = self._page_value(page, "page", 100000), self._page_value(page_size, "page_size", 100)
        pool = self._require_pool(); org = await self.get_organization(organization_id)
        if org is None: raise OrganizationNotFoundError("organization was not found")
        rows = await pool.fetch("SELECT * FROM customer_billing_ledger WHERE organization_id=$1 ORDER BY created_at DESC, id DESC LIMIT $2 OFFSET $3", organization_id, page_size, (page - 1) * page_size)
        total = await pool.fetchval("SELECT count(*) FROM customer_billing_ledger WHERE organization_id=$1", organization_id)
        account = await pool.fetchrow(
            "SELECT COALESCE(sum(amount_usd) FILTER (WHERE amount_usd > 0), 0) AS credits, "
            "COALESCE(sum(-amount_usd) FILTER (WHERE amount_usd < 0), 0) AS debits "
            "FROM customer_billing_ledger WHERE organization_id=$1",
            organization_id,
        )
        available = _money(org.get("billingBalanceUsd", 0))
        return {
            "organization": org,
            "account": {
                "initialBalanceUsd": 0.0,
                "totalCreditsUsd": _money(account["credits"]),
                "totalDebitsUsd": _money(account["debits"]),
                "availableBalanceUsd": available,
                "billingStatus": str(org.get("billingStatus") or "past_due"),
                "pastDue": str(org.get("billingStatus") or "past_due") == "past_due",
            },
            "records": {"items": [self._ledger_payload(r) for r in rows], "total": int(total or 0), "page": page, "pageSize": page_size},
            "isDemo": False,
        }

    async def adjust_billing(self, organization_id: str, *, operation: str, amount_usd: Any, reason: str = "", operator: str = "", operator_email: str = "", external_reference: str = "", idempotency_key: str) -> dict[str, Any]:
        # Ledger charges may be below the simulated top-up minimum: usage is
        # settled to cents, while operator grants still remain bounded by the
        # common validation limits.
        if isinstance(amount_usd, bool):
            raise OrganizationConflictError("amountUsd must be a number")
        try:
            amount = Decimal(str(amount_usd))
        except Exception as exc:
            raise OrganizationConflictError("amountUsd must be a number") from exc
        if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2 or amount > Decimal("100000.00"):
            raise OrganizationConflictError("amountUsd must be between 0.01 and 100000.00")
        amount = amount.quantize(Decimal("0.01")); operation = str(operation).strip().lower()
        if operation not in {"grant", "revoke", "charge", "credit", "refund"}:
            raise OrganizationConflictError("unsupported billing operation")
        idempotency_key = str(idempotency_key or "").strip()
        if not idempotency_key:
            raise OrganizationConflictError("idempotency key is required")
        external_reference = str(external_reference or "").strip()
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                organization = await conn.fetchrow(
                    "SELECT id, status, billing_status, billing_balance_usd, upstream_organization_id "
                    "FROM customer_organization WHERE id=$1 FOR UPDATE",
                    organization_id,
                )
                if organization is None:
                    raise OrganizationNotFoundError("organization was not found")
                existing = await conn.fetchrow(
                    "SELECT * FROM customer_billing_ledger WHERE organization_id=$1 "
                    "AND (idempotency_key=$2 OR ($3 <> '' AND external_reference=$3))",
                    organization_id, idempotency_key, external_reference,
                )
                if existing: return self._ledger_payload(existing)
                previous = Decimal(str(organization["billing_balance_usd"] or 0)); delta = amount if operation in {"grant", "credit", "refund"} else -amount; balance = previous + delta
                if balance < 0: raise OrganizationConflictError("organization credit is insufficient")
                row = await conn.fetchrow("INSERT INTO customer_billing_ledger(id,organization_id,operation,amount_usd,balance_after_usd,reason,operator,operator_email,external_reference,idempotency_key) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING *", self._id(), organization_id, operation, delta, balance, reason[:500], operator[:128], operator_email[:254], external_reference[:128], idempotency_key[:128])
                current_status = str(organization["billing_status"] or "past_due")
                next_billing_status = "active" if balance > 0 else "past_due"
                if current_status == "suspended":
                    next_billing_status = "suspended"
                await conn.execute(
                    "UPDATE customer_organization SET billing_status=$2, billing_balance_usd=$3, "
                    "billing_effective_at=CASE WHEN billing_effective_at IS NULL AND $4 THEN now() ELSE billing_effective_at END, "
                    "updated_at=now() WHERE id=$1",
                    organization_id,
                    next_billing_status,
                    balance,
                    operation == "grant",
                )
                upstream_org_id = str(_row_value(organization, "upstream_organization_id", "") or "")
                if upstream_org_id:
                    await self._enqueue_projection_sync(
                        conn,
                        "organization.billing.sync",
                        organization_id,
                        await self._billing_projection_payload(
                            conn,
                            organization_id,
                            upstream_org_id,
                            balance,
                            next_billing_status,
                        ),
                        version=str(row["id"]),
                    )
                if next_billing_status != "active":
                    await self._enqueue_token_revocations(
                        conn,
                        organization_id,
                        reason="organization_credit_insufficient",
                        version=str(row["id"]),
                    )
        return self._ledger_payload(row)

    @staticmethod
    def _settlement_date(value: Any) -> date:
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).date()
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value).strip()[:10])
        except (TypeError, ValueError) as exc:
            raise OrganizationValidationError("usage date must be YYYY-MM-DD") from exc

    async def settle_usage(
        self,
        organization_id: str,
        usage_date: Any,
        amount_usd: Any,
        *,
        upstream_organization_id: str = "",
        operator: str = "usage-settler",
    ) -> dict[str, Any]:
        """Idempotently charge one completed usage day to an organization.

        Settlements are allowed to take the balance below zero: the charge is
        an accounting fact, while ``past_due`` blocks new credentials and
        queues revocation of existing ones.
        """

        day = self._settlement_date(usage_date)
        try:
            amount = Decimal(str(amount_usd))
        except Exception as exc:
            raise OrganizationValidationError("usage amount must be a number") from exc
        if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -6:
            raise OrganizationValidationError("usage amount must be a non-negative amount with at most six decimals")
        amount = amount.quantize(Decimal("0.000001"))
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                organization = await conn.fetchrow(
                    "SELECT id, status, billing_status, billing_balance_usd, billing_effective_at, upstream_organization_id "
                    "FROM customer_organization WHERE id=$1 FOR UPDATE",
                    organization_id,
                )
                if organization is None:
                    raise OrganizationNotFoundError("organization was not found")
                billing_effective_at = _row_value(
                    organization, "billing_effective_at"
                )
                cutoff_day = (
                    billing_effective_at.astimezone(timezone.utc).date()
                    if billing_effective_at is not None
                    else None
                )
                if cutoff_day is None or day <= cutoff_day:
                    reason = (
                        "needs_event_time"
                        if cutoff_day is not None and day == cutoff_day
                        else "before_billing_effective_at"
                    )
                    return {
                        "organizationId": organization_id,
                        "usageDate": day.isoformat(),
                        "amountUsd": 0.0,
                        "balanceAfterUsd": _money(
                            organization["billing_balance_usd"]
                        ),
                        "idempotent": True,
                        "status": "skipped",
                        "reason": reason,
                        "beforeBillingEffectiveAt": True,
                        "billingStatus": str(
                            organization["billing_status"] or "past_due"
                        ),
                    }
                existing = await conn.fetchrow(
                    "SELECT * FROM customer_usage_settlement WHERE organization_id=$1 AND usage_date=$2",
                    organization_id,
                    day,
                )
                if existing is not None:
                    settled_amount = Decimal(str(existing["settled_amount_usd"] or 0))
                    delta = amount - settled_amount
                    if delta:
                        previous_balance = Decimal(str(organization["billing_balance_usd"] or 0))
                        balance = previous_balance - delta
                        operation = "charge" if delta > 0 else "credit"
                        external_reference = (
                            f"usage-adjustment:{organization_id}:{day.isoformat()}"
                        )
                        adjustment_version = self._id()
                        ledger_row = await conn.fetchrow(
                            "INSERT INTO customer_billing_ledger("
                            "id,organization_id,operation,amount_usd,balance_after_usd,reason,operator,operator_email,external_reference,idempotency_key"
                            ") VALUES($1,$2,$3,$4,$5,$6,$7,'',$8,$8) "
                            "ON CONFLICT (organization_id,idempotency_key) DO UPDATE SET "
                            "operation=CASE "
                            "WHEN customer_billing_ledger.amount_usd + EXCLUDED.amount_usd < 0 THEN 'charge' "
                            "ELSE 'credit' END, "
                            "amount_usd=customer_billing_ledger.amount_usd + EXCLUDED.amount_usd, "
                            "balance_after_usd=EXCLUDED.balance_after_usd, "
                            "reason=EXCLUDED.reason, operator=EXCLUDED.operator "
                            "RETURNING *",
                            adjustment_version,
                            organization_id,
                            operation,
                            -delta,
                            balance,
                            "daily usage settlement adjustment",
                            operator[:128],
                            external_reference,
                        )
                        await conn.execute(
                            "UPDATE customer_usage_settlement SET settled_amount_usd=$3, "
                            "upstream_organization_id=$4, updated_at=now() "
                            "WHERE organization_id=$1 AND usage_date=$2",
                            organization_id,
                            day,
                            amount,
                            str(upstream_organization_id or organization["upstream_organization_id"] or ""),
                        )
                        current_status = str(organization["billing_status"] or "past_due")
                        next_billing_status = "active" if balance > 0 else "past_due"
                        if current_status == "suspended" or str(organization["status"] or "") in {"suspended", "archived"}:
                            next_billing_status = "suspended"
                        await conn.execute(
                            "UPDATE customer_organization SET billing_status=$2, billing_balance_usd=$3, updated_at=now() WHERE id=$1",
                            organization_id,
                            next_billing_status,
                            balance,
                        )
                        upstream_org_id = str(
                            upstream_organization_id or organization["upstream_organization_id"] or ""
                        )
                        if upstream_org_id:
                            await self._enqueue_projection_sync(
                                conn,
                                "organization.billing.sync",
                                organization_id,
                                await self._billing_projection_payload(
                                    conn,
                                    organization_id,
                                    upstream_org_id,
                                    balance,
                                    next_billing_status,
                                ),
                                version=adjustment_version,
                            )
                        if next_billing_status != "active":
                            await self._enqueue_token_revocations(
                                conn,
                                organization_id,
                                reason="organization_credit_insufficient",
                                version=adjustment_version,
                            )
                        return {
                            "organizationId": organization_id,
                            "usageDate": day.isoformat(),
                            "amountUsd": _money(amount),
                            "adjustmentUsd": _money(delta),
                            "balanceAfterUsd": _money(balance),
                            "idempotent": False,
                            "adjusted": True,
                            "billingStatus": next_billing_status,
                            "record": self._ledger_payload(ledger_row),
                        }
                    return {
                        "organizationId": organization_id,
                        "usageDate": day.isoformat(),
                        "amountUsd": _money(settled_amount),
                        "balanceAfterUsd": _money(organization["billing_balance_usd"]),
                        "idempotent": True,
                        "billingStatus": str(organization["billing_status"] or "past_due"),
                    }
                previous_balance = Decimal(str(organization["billing_balance_usd"] or 0))
                balance = previous_balance - amount
                external_reference = f"usage:{organization_id}:{day.isoformat()}"
                ledger_row = await conn.fetchrow(
                    "INSERT INTO customer_billing_ledger("
                    "id,organization_id,operation,amount_usd,balance_after_usd,reason,operator,operator_email,external_reference,idempotency_key"
                    ") VALUES($1,$2,'charge',$3,$4,$5,$6,'',$7,$8) RETURNING *",
                    self._id(),
                    organization_id,
                    -amount,
                    balance,
                    "daily usage settlement",
                    operator[:128],
                    external_reference,
                    external_reference,
                )
                await conn.execute(
                    "INSERT INTO customer_usage_settlement(organization_id,usage_date,upstream_organization_id,settled_amount_usd) "
                    "VALUES($1,$2,$3,$4)",
                    organization_id,
                    day,
                    str(upstream_organization_id or organization["upstream_organization_id"] or ""),
                    amount,
                )
                current_status = str(organization["billing_status"] or "past_due")
                next_billing_status = "active" if balance > 0 else "past_due"
                if current_status == "suspended" or str(organization["status"] or "") in {"suspended", "archived"}:
                    next_billing_status = "suspended"
                await conn.execute(
                    "UPDATE customer_organization SET billing_status=$2, billing_balance_usd=$3, updated_at=now() WHERE id=$1",
                    organization_id,
                    next_billing_status,
                    balance,
                )
                upstream_org_id = str(
                    upstream_organization_id or organization["upstream_organization_id"] or ""
                )
                if upstream_org_id:
                    await self._enqueue_projection_sync(
                        conn,
                        "organization.billing.sync",
                        organization_id,
                        await self._billing_projection_payload(
                            conn,
                            organization_id,
                            upstream_org_id,
                            balance,
                            next_billing_status,
                        ),
                        version=external_reference,
                    )
                if next_billing_status != "active":
                    await self._enqueue_token_revocations(
                        conn,
                        organization_id,
                        reason="organization_credit_insufficient",
                        version=external_reference,
                    )
        return {
            "organizationId": organization_id,
            "usageDate": day.isoformat(),
            "amountUsd": _money(amount),
            "balanceAfterUsd": _money(balance),
            "idempotent": False,
            "billingStatus": next_billing_status,
            "record": self._ledger_payload(ledger_row),
        }

    @staticmethod
    def _ledger_payload(row: Any) -> dict[str, Any]:
        return {"id": row["id"], "timestamp": _iso(row["created_at"]), "type": row["operation"], "amountUsd": _money(row["amount_usd"]), "balanceAfterUsd": _money(row["balance_after_usd"]), "reason": row["reason"], "operator": row["operator"], "operatorEmail": row["operator_email"], "externalReference": row["external_reference"]}


# Short aliases used by application wiring and tests.
OrganizationRepository = PostgreSQLOrganizationRepository
PostgresOrganizationRepository = PostgreSQLOrganizationRepository
