from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

try:
    import asyncpg
except ImportError:  # pragma: no cover - optional for local development
    asyncpg = None  # type: ignore[assignment]

from .litellm_client import (
    department_key,
    normalize_model_display_name,
    normalize_team_text,
    team_identity_key,
)


logger = logging.getLogger("ai-token-dashboard.usage-store")


USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_daily (
    backend_id TEXT NOT NULL,
    usage_date DATE NOT NULL,
    user_id TEXT NOT NULL,
    organization_id TEXT NOT NULL DEFAULT '',
    team_id TEXT NOT NULL DEFAULT '',
    key_id TEXT NOT NULL DEFAULT '',
    principal_id TEXT NOT NULL DEFAULT '',
    attribution_source TEXT NOT NULL DEFAULT 'unattributed',
    billing_eligible BOOLEAN NOT NULL DEFAULT FALSE,
    employee_email TEXT NOT NULL DEFAULT '',
    employee_name TEXT NOT NULL DEFAULT '',
    email_source TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens BIGINT NOT NULL DEFAULT 0,
    completion_tokens BIGINT NOT NULL DEFAULT 0,
    total_tokens BIGINT NOT NULL DEFAULT 0,
    request_count BIGINT NOT NULL DEFAULT 0,
    success_count BIGINT NOT NULL DEFAULT 0,
    failure_count BIGINT NOT NULL DEFAULT 0,
    spend DOUBLE PRECISION NOT NULL DEFAULT 0,
    collected_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (
        backend_id, usage_date, user_id, source, model,
        organization_id, team_id, key_id, principal_id, attribution_source,
        billing_eligible
    )
);

-- Keep deployments created before organization mode compatible with the
-- richer attribution snapshot. Existing aggregate rows remain queryable with
-- empty attribution fields until the next sync fills them.
ALTER TABLE usage_daily ADD COLUMN IF NOT EXISTS organization_id TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_daily ADD COLUMN IF NOT EXISTS team_id TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_daily ADD COLUMN IF NOT EXISTS key_id TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_daily ADD COLUMN IF NOT EXISTS principal_id TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_daily ADD COLUMN IF NOT EXISTS attribution_source TEXT NOT NULL DEFAULT 'unattributed';
ALTER TABLE usage_daily ADD COLUMN IF NOT EXISTS billing_eligible BOOLEAN NOT NULL DEFAULT FALSE;
-- 邮箱来源：'' 未知/无邮箱、'upstream' 上游真实邮箱、'inferred_primary_directory' 按姓名推断。
ALTER TABLE usage_daily ADD COLUMN IF NOT EXISTS email_source TEXT NOT NULL DEFAULT '';

-- Create indexes only after compatibility columns exist. Older production
-- databases predate organization attribution and would otherwise fail startup
-- while planning an index on a column that has not been added yet.
CREATE INDEX IF NOT EXISTS usage_daily_employee_date_idx
    ON usage_daily (employee_email, usage_date);
CREATE INDEX IF NOT EXISTS usage_daily_date_idx
    ON usage_daily (usage_date);
CREATE INDEX IF NOT EXISTS usage_daily_date_backend_user_idx
    ON usage_daily (usage_date, backend_id, user_id);
CREATE INDEX IF NOT EXISTS usage_daily_org_date_idx
    ON usage_daily (organization_id, usage_date, backend_id);
CREATE INDEX IF NOT EXISTS usage_daily_team_date_idx
    ON usage_daily (team_id, usage_date, backend_id);
CREATE INDEX IF NOT EXISTS usage_daily_key_date_idx
    ON usage_daily (key_id, usage_date, backend_id);
CREATE INDEX IF NOT EXISTS usage_daily_date_source_model_idx
    ON usage_daily (usage_date, source, model);

-- Older deployments keyed aggregates only by user/source/model, which makes
-- two enterprise tokens overwrite one another. Rebuild the key so event-time
-- attribution remains stable across team moves and key rotation.
ALTER TABLE usage_daily DROP CONSTRAINT IF EXISTS usage_daily_pkey;
ALTER TABLE usage_daily ADD CONSTRAINT usage_daily_pkey PRIMARY KEY (
    backend_id, usage_date, user_id, source, model,
    organization_id, team_id, key_id, principal_id, attribution_source,
    billing_eligible
);

CREATE TABLE IF NOT EXISTS usage_sync_coverage (
    backend_id TEXT NOT NULL,
    usage_date DATE NOT NULL,
    synced_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (backend_id, usage_date)
);

CREATE TABLE IF NOT EXISTS usage_team_membership_daily (
    backend_id TEXT NOT NULL,
    snapshot_date DATE NOT NULL,
    team_id TEXT NOT NULL,
    team_name TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL,
    employee_email TEXT NOT NULL DEFAULT '',
    employee_name TEXT NOT NULL DEFAULT '',
    team_role TEXT NOT NULL DEFAULT 'user',
    PRIMARY KEY (backend_id, snapshot_date, team_id, user_id)
);

CREATE TABLE IF NOT EXISTS usage_department_directory (
    backend_id TEXT NOT NULL,
    department_id TEXT NOT NULL,
    department_name TEXT NOT NULL DEFAULT '',
    organization_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    synced_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (backend_id, department_id)
);

CREATE INDEX IF NOT EXISTS usage_department_directory_org_idx
    ON usage_department_directory (organization_id, status, department_name);

-- Stable identity facts are maintained independently from daily usage snapshots.
-- This lets realtime and historical rows resolve names even when a daily sync is
-- delayed or incomplete.
CREATE TABLE IF NOT EXISTS usage_identity_directory (
    backend_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    employee_email TEXT NOT NULL DEFAULT '',
    name_source TEXT NOT NULL DEFAULT 'user_id',
    confidence TEXT NOT NULL DEFAULT 'low',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (backend_id, user_id)
);

CREATE INDEX IF NOT EXISTS usage_identity_directory_email_idx
    ON usage_identity_directory (employee_email, backend_id);
CREATE INDEX IF NOT EXISTS usage_identity_directory_updated_idx
    ON usage_identity_directory (updated_at DESC);

CREATE INDEX IF NOT EXISTS usage_team_membership_lookup_idx
    ON usage_team_membership_daily (backend_id, team_id, snapshot_date);
CREATE INDEX IF NOT EXISTS usage_team_membership_employee_idx
    ON usage_team_membership_daily (employee_email, snapshot_date);
CREATE INDEX IF NOT EXISTS usage_team_membership_usage_join_idx
    ON usage_team_membership_daily (backend_id, snapshot_date, user_id);
CREATE INDEX IF NOT EXISTS usage_team_membership_team_filter_idx
    ON usage_team_membership_daily (backend_id, snapshot_date, team_id, user_id);
-- 权限解析对每个 (后端, 团队, 成员) 只取最新一天，没有这条索引要全表扫 17 万行排序。
CREATE INDEX IF NOT EXISTS usage_team_membership_latest_idx
    ON usage_team_membership_daily (backend_id, team_id, user_id, snapshot_date DESC);

CREATE TABLE IF NOT EXISTS usage_sync_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    status TEXT NOT NULL,
    backend_count INTEGER NOT NULL DEFAULT 0,
    row_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS usage_sync_runs_dates_idx
    ON usage_sync_runs (start_date, end_date, status, finished_at DESC);

CREATE TABLE IF NOT EXISTS usage_snapshot_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    revision TEXT NOT NULL DEFAULT '',
    published_at TIMESTAMPTZ,
    start_date DATE,
    end_date DATE,
    backend_ids TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[]
);

CREATE TABLE IF NOT EXISTS usage_sync_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    worker_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'starting',
    heartbeat_at TIMESTAMPTZ,
    last_started_at TIMESTAMPTZ,
    last_finished_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    last_error TEXT NOT NULL DEFAULT '',
    snapshot_revision TEXT NOT NULL DEFAULT ''
);

-- Reader instances enqueue a bounded refresh request for the worker instead of
-- waiting on an upstream query.  The request key makes repeated browser
-- refreshes idempotent; the worker may merge adjacent/overlapping rows before
-- running one synchronization pass.
CREATE TABLE IF NOT EXISTS usage_refresh_requests (
    request_key TEXT PRIMARY KEY,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    next_attempt_at TIMESTAMPTZ,
    last_duration_ms DOUBLE PRECISION
);
ALTER TABLE usage_refresh_requests ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
ALTER TABLE usage_refresh_requests ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE usage_refresh_requests ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_refresh_requests ADD COLUMN IF NOT EXISTS last_error TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_refresh_requests ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;
ALTER TABLE usage_refresh_requests ADD COLUMN IF NOT EXISTS last_duration_ms DOUBLE PRECISION;
CREATE INDEX IF NOT EXISTS usage_refresh_requests_pending_idx
    ON usage_refresh_requests (status, requested_at);

INSERT INTO usage_snapshot_state (singleton) VALUES (TRUE)
ON CONFLICT (singleton) DO NOTHING;
INSERT INTO usage_sync_state (singleton) VALUES (TRUE)
ON CONFLICT (singleton) DO NOTHING;

-- Non-content request metadata used for historical attribution and intraday
-- billing cutoffs. Prompts, responses, and plaintext credentials are omitted.
CREATE TABLE IF NOT EXISTS usage_event_attribution (
    backend_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    event_time TIMESTAMPTZ NOT NULL,
    usage_date DATE NOT NULL,
    raw_user_id TEXT NOT NULL DEFAULT '',
    employee_email TEXT NOT NULL DEFAULT '',
    employee_name TEXT NOT NULL DEFAULT '',
    name_source TEXT NOT NULL DEFAULT 'user_id',
    name_confidence TEXT NOT NULL DEFAULT 'low',
    organization_id TEXT NOT NULL DEFAULT '',
    team_id TEXT NOT NULL DEFAULT '',
    key_id TEXT NOT NULL DEFAULT '',
    principal_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
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
    provider TEXT,
    model_group TEXT,
    model_id TEXT,
    api_base TEXT,
    status TEXT,
    error_code TEXT,
    error_class TEXT,
    error_message TEXT,
    scenario TEXT,
    request_duration_ms DOUBLE PRECISION,
    ttft_ms DOUBLE PRECISION,
    attempted_retries INTEGER,
    max_retries INTEGER,
    trace_id TEXT,
    user_visible_failure BOOLEAN,
    final_failure_source TEXT,
    collected_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (backend_id, request_id)
);
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS employee_email TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS employee_name TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS name_source TEXT NOT NULL DEFAULT 'user_id';
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS name_confidence TEXT NOT NULL DEFAULT 'low';
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS provider TEXT;
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS model_group TEXT;
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS model_id TEXT;
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS api_base TEXT;
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS error_code TEXT;
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS error_class TEXT;
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS scenario TEXT;
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS request_duration_ms DOUBLE PRECISION;
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS ttft_ms DOUBLE PRECISION;
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS attempted_retries INTEGER;
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS max_retries INTEGER;
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS trace_id TEXT;
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS user_visible_failure BOOLEAN;
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS final_failure_source TEXT;
CREATE INDEX IF NOT EXISTS usage_event_attribution_org_time_idx
    ON usage_event_attribution (organization_id, event_time)
    WHERE organization_id <> '';
CREATE INDEX IF NOT EXISTS usage_event_attribution_event_time_backend_idx
    ON usage_event_attribution (event_time, backend_id, request_id);
CREATE INDEX IF NOT EXISTS usage_event_attribution_key_time_idx
    ON usage_event_attribution (key_id, event_time)
    WHERE key_id <> '';
CREATE INDEX IF NOT EXISTS usage_event_attribution_stability_idx
    ON usage_event_attribution (usage_date, model, scenario, error_code, event_time DESC);
CREATE INDEX IF NOT EXISTS usage_event_attribution_ttft_idx
    ON usage_event_attribution (usage_date, model, ttft_ms)
    WHERE ttft_ms IS NOT NULL;
CREATE INDEX IF NOT EXISTS usage_event_attribution_failure_idx
    ON usage_event_attribution (usage_date, model, final_failure_source, user_visible_failure);
CREATE INDEX IF NOT EXISTS usage_event_attribution_drilldown_idx
    ON usage_event_attribution (usage_date, scenario, error_code, event_time DESC);
CREATE INDEX IF NOT EXISTS usage_event_attribution_model_group_drilldown_idx
    ON usage_event_attribution (usage_date, model_group, scenario, error_code, event_time DESC);
CREATE INDEX IF NOT EXISTS usage_event_attribution_stability_samples_idx
    ON usage_event_attribution (usage_date, event_time DESC)
    WHERE user_visible_failure IS TRUE OR attempted_retries > 0 OR error_code <> '' OR error_class <> '' OR scenario <> 'unknown';

CREATE TABLE IF NOT EXISTS usage_realtime_daily (
    LIKE usage_daily INCLUDING DEFAULTS INCLUDING CONSTRAINTS
);
CREATE UNIQUE INDEX IF NOT EXISTS usage_realtime_daily_identity_idx ON usage_realtime_daily (
    backend_id, usage_date, user_id, source, model,
    organization_id, team_id, key_id, principal_id, attribution_source,
    billing_eligible
);
CREATE INDEX IF NOT EXISTS usage_realtime_daily_date_idx
    ON usage_realtime_daily (usage_date, backend_id);

CREATE TABLE IF NOT EXISTS usage_realtime_state (
    usage_date DATE PRIMARY KEY,
    ready BOOLEAN NOT NULL DEFAULT FALSE,
    revision BIGINT NOT NULL DEFAULT 0,
    latest_event_at TIMESTAMPTZ,
    last_archived_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL
);

-- A watermark is advanced only after every request in its closed interval has
-- been durably recorded. It survives worker restarts and replaces page cursors.
CREATE TABLE IF NOT EXISTS usage_realtime_settlement (
    backend_id TEXT PRIMARY KEY,
    verified_through TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'pending',
    last_error TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_realtime_settlement_segments (
    backend_id TEXT NOT NULL,
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    request_count BIGINT NOT NULL DEFAULT 0,
    amount NUMERIC(16,6) NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT NOT NULL DEFAULT '',
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (backend_id, start_time, end_time)
);
CREATE INDEX IF NOT EXISTS usage_realtime_settlement_segments_status_idx
    ON usage_realtime_settlement_segments (backend_id, status, start_time);

CREATE OR REPLACE VIEW usage_query_daily AS
SELECT u.*
FROM usage_daily u
WHERE NOT EXISTS (
    SELECT 1 FROM usage_realtime_state s
    WHERE s.usage_date=u.usage_date AND s.ready
)
UNION ALL
SELECT r.*
FROM usage_realtime_daily r
JOIN usage_realtime_state s ON s.usage_date=r.usage_date AND s.ready;

-- Dashboard-facing API cost facts. Request-level attribution remains the
-- audit source; this table keeps overview queries bounded as history grows.
CREATE TABLE IF NOT EXISTS cost_api_daily (
    usage_date DATE NOT NULL,
    backend_id TEXT NOT NULL,
    account_id TEXT NOT NULL DEFAULT '',
    organization_id TEXT NOT NULL DEFAULT '',
    team_id TEXT NOT NULL DEFAULT '',
    key_id TEXT NOT NULL DEFAULT '',
    principal_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    model_group TEXT NOT NULL DEFAULT '',
    model_id TEXT NOT NULL DEFAULT '',
    api_base TEXT NOT NULL DEFAULT '',
    spend NUMERIC(18,6) NOT NULL DEFAULT 0,
    request_count BIGINT NOT NULL DEFAULT 0,
    refreshed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (
        usage_date, backend_id, account_id, organization_id, team_id,
        key_id, principal_id, source, model, provider, model_group,
        model_id, api_base
    )
);
CREATE INDEX IF NOT EXISTS cost_api_daily_date_idx
    ON cost_api_daily (usage_date, model, provider);
CREATE INDEX IF NOT EXISTS cost_api_daily_account_idx
    ON cost_api_daily (usage_date, account_id, key_id, principal_id);
CREATE INDEX IF NOT EXISTS cost_api_daily_ledger_idx
    ON cost_api_daily (usage_date DESC, provider, model, account_id);

CREATE TABLE IF NOT EXISTS observability_dashboard_snapshots (
    dashboard_type TEXT NOT NULL,
    snapshot_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    data_revision TEXT NOT NULL DEFAULT '',
    refreshing BOOLEAN NOT NULL DEFAULT FALSE,
    last_refresh_error TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (dashboard_type, snapshot_key)
);
CREATE INDEX IF NOT EXISTS observability_dashboard_snapshots_updated_idx
    ON observability_dashboard_snapshots (dashboard_type, updated_at DESC);

CREATE TABLE IF NOT EXISTS cost_items (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    vendor TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    business_scope TEXT NOT NULL DEFAULT '',
    amount NUMERIC(16,4) NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    exchange_rate NUMERIC(16,6) NOT NULL DEFAULT 1,
    amount_usd NUMERIC(16,4) NOT NULL,
    service_start_date DATE NOT NULL,
    service_end_date DATE NOT NULL,
    finance_bucket TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
ALTER TABLE cost_items ADD COLUMN IF NOT EXISTS cost_bucket TEXT NOT NULL DEFAULT '';
ALTER TABLE cost_items ADD COLUMN IF NOT EXISTS source_type TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE cost_items ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT '';
ALTER TABLE cost_items ADD COLUMN IF NOT EXISTS account_id TEXT NOT NULL DEFAULT '';
ALTER TABLE cost_items ADD COLUMN IF NOT EXISTS account_name TEXT NOT NULL DEFAULT '';
ALTER TABLE cost_items ADD COLUMN IF NOT EXISTS voucher_id TEXT NOT NULL DEFAULT '';
ALTER TABLE cost_items ADD COLUMN IF NOT EXISTS voucher_no TEXT NOT NULL DEFAULT '';
ALTER TABLE cost_items ADD COLUMN IF NOT EXISTS invoice_no TEXT NOT NULL DEFAULT '';
ALTER TABLE cost_items ADD COLUMN IF NOT EXISTS recognition_status TEXT NOT NULL DEFAULT 'actual';
ALTER TABLE cost_items ADD COLUMN IF NOT EXISTS reconciliation_status TEXT NOT NULL DEFAULT 'unreconciled';
ALTER TABLE cost_items ADD COLUMN IF NOT EXISTS plan_version_id TEXT;
ALTER TABLE cost_items ADD COLUMN IF NOT EXISTS scenario TEXT NOT NULL DEFAULT '';
ALTER TABLE cost_items ADD COLUMN IF NOT EXISTS source_evidence TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS cost_items_service_idx ON cost_items (service_start_date, service_end_date, enabled);
CREATE INDEX IF NOT EXISTS cost_items_ledger_idx
    ON cost_items (cost_bucket, provider, account_id, reconciliation_status, service_start_date);
CREATE INDEX IF NOT EXISTS cost_items_plan_version_idx
    ON cost_items (plan_version_id, recognition_status, service_start_date);

CREATE TABLE IF NOT EXISTS cost_budgets (
    month DATE PRIMARY KEY,
    budget_usd NUMERIC(16,4) NOT NULL,
    daily_target_usd NUMERIC(16,4) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS savings_actions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    baseline_daily_cost NUMERIC(16,4) NOT NULL,
    implemented_date DATE NOT NULL,
    verified_date DATE,
    verified_daily_cost NUMERIC(16,4),
    owner TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'planned',
    notes TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
ALTER TABLE savings_actions ADD COLUMN IF NOT EXISTS expected_daily_cost NUMERIC(16,4);
ALTER TABLE savings_actions ADD COLUMN IF NOT EXISTS expected_start_date DATE;
ALTER TABLE savings_actions ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT '';
ALTER TABLE savings_actions ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT '';
ALTER TABLE savings_actions ADD COLUMN IF NOT EXISTS cost_bucket TEXT NOT NULL DEFAULT '';
ALTER TABLE savings_actions ADD COLUMN IF NOT EXISTS evidence_url TEXT NOT NULL DEFAULT '';
ALTER TABLE savings_actions ADD COLUMN IF NOT EXISTS finance_reviewer TEXT NOT NULL DEFAULT '';

-- Legacy savings require evidence and finance review before they remain verified.
UPDATE savings_actions
SET status='pending_evidence', updated_at=NOW()
WHERE lower(status)='verified'
  AND (btrim(evidence_url)='' OR btrim(finance_reviewer)='');

CREATE TABLE IF NOT EXISTS stability_attempt_events (
    backend_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '',
    trace_id TEXT NOT NULL DEFAULT '',
    attempt_id TEXT NOT NULL DEFAULT '',
    attempt_index INTEGER NOT NULL DEFAULT 0,
    retry_index INTEGER NOT NULL DEFAULT 0,
    requested_model_group TEXT NOT NULL DEFAULT '',
    actual_model TEXT NOT NULL DEFAULT '',
    route_name TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unknown',
    error_code TEXT NOT NULL DEFAULT '',
    error_class TEXT NOT NULL DEFAULT '',
    error_category TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    scenario TEXT NOT NULL DEFAULT 'unknown',
    scenario_version TEXT NOT NULL DEFAULT '',
    event_time TIMESTAMPTZ NOT NULL,
    event_date DATE NOT NULL,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    ttft_ms DOUBLE PRECISION,
    duration_ms DOUBLE PRECISION,
    fallback_from TEXT NOT NULL DEFAULT '',
    fallback_to TEXT NOT NULL DEFAULT '',
    is_retry BOOLEAN NOT NULL DEFAULT FALSE,
    is_fallback BOOLEAN NOT NULL DEFAULT FALSE,
    collected_at TIMESTAMPTZ,
    received_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (backend_id, event_id)
);
CREATE INDEX IF NOT EXISTS stability_attempt_events_date_model_idx
    ON stability_attempt_events (event_date, requested_model_group, scenario, error_code, event_time DESC);
CREATE INDEX IF NOT EXISTS stability_attempt_events_request_idx
    ON stability_attempt_events (backend_id, request_id, attempt_index, event_time);
CREATE INDEX IF NOT EXISTS stability_attempt_events_trace_idx
    ON stability_attempt_events (trace_id, event_time) WHERE trace_id <> '';
ALTER TABLE stability_attempt_events ADD COLUMN IF NOT EXISTS retry_index INTEGER NOT NULL DEFAULT 0;
ALTER TABLE stability_attempt_events ADD COLUMN IF NOT EXISTS error_category TEXT NOT NULL DEFAULT '';
ALTER TABLE stability_attempt_events ADD COLUMN IF NOT EXISTS error_message TEXT NOT NULL DEFAULT '';
ALTER TABLE stability_attempt_events ADD COLUMN IF NOT EXISTS event_time TIMESTAMPTZ;
ALTER TABLE stability_attempt_events ADD COLUMN IF NOT EXISTS event_date DATE;
ALTER TABLE stability_attempt_events ADD COLUMN IF NOT EXISTS duration_ms DOUBLE PRECISION;
ALTER TABLE stability_attempt_events ADD COLUMN IF NOT EXISTS is_retry BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE stability_attempt_events ADD COLUMN IF NOT EXISTS is_fallback BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE stability_attempt_events ADD COLUMN IF NOT EXISTS collected_at TIMESTAMPTZ;
ALTER TABLE stability_attempt_events ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS stability_actions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'open',
    target_date DATE,
    fix_reference TEXT NOT NULL DEFAULT '',
    requested_model_group TEXT NOT NULL DEFAULT '',
    scenario TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    request_id TEXT NOT NULL DEFAULT '',
    baseline_start_date DATE,
    baseline_end_date DATE,
    created_by TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS stability_actions_risk_idx
    ON stability_actions (status, severity, target_date, requested_model_group, scenario);

CREATE TABLE IF NOT EXISTS stability_regressions (
    id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL REFERENCES stability_actions(id) ON DELETE CASCADE,
    scenario TEXT NOT NULL DEFAULT '',
    baseline_start_date DATE NOT NULL,
    baseline_end_date DATE NOT NULL,
    regression_start_date DATE NOT NULL,
    regression_end_date DATE NOT NULL,
    metric_name TEXT NOT NULL,
    baseline_value DOUBLE PRECISION,
    regression_value DOUBLE PRECISION,
    unit TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    conclusion TEXT NOT NULL DEFAULT '',
    evidence_url TEXT NOT NULL DEFAULT '',
    reviewer TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK (baseline_start_date <= baseline_end_date),
    CHECK (regression_start_date <= regression_end_date)
);
ALTER TABLE stability_actions ADD COLUMN IF NOT EXISTS notes TEXT NOT NULL DEFAULT '';
ALTER TABLE stability_actions ADD COLUMN IF NOT EXISTS request_id TEXT NOT NULL DEFAULT '';
ALTER TABLE stability_regressions ADD COLUMN IF NOT EXISTS notes TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS stability_regressions_action_idx
    ON stability_regressions (action_id, regression_end_date DESC);

CREATE TABLE IF NOT EXISTS cost_plan_versions (
    id TEXT PRIMARY KEY,
    year INTEGER NOT NULL CHECK (year BETWEEN 2000 AND 2200),
    version TEXT NOT NULL,
    scenario TEXT NOT NULL DEFAULT 'baseline',
    as_of DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    approved_by TEXT NOT NULL DEFAULT '',
    approved_at TIMESTAMPTZ,
    activated_by TEXT NOT NULL DEFAULT '',
    activated_at TIMESTAMPTZ,
    archived_by TEXT NOT NULL DEFAULT '',
    archived_at TIMESTAMPTZ,
    created_by TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT '',
    coverage_complete BOOLEAN NOT NULL DEFAULT FALSE,
    coverage_notes TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (year, version, scenario),
    CHECK (status IN ('draft', 'approved', 'archived')),
    CHECK (NOT is_active OR (status='approved' AND scenario='baseline'))
);
ALTER TABLE cost_plan_versions ADD COLUMN IF NOT EXISTS activated_by TEXT NOT NULL DEFAULT '';
ALTER TABLE cost_plan_versions ADD COLUMN IF NOT EXISTS activated_at TIMESTAMPTZ;
ALTER TABLE cost_plan_versions ADD COLUMN IF NOT EXISTS archived_by TEXT NOT NULL DEFAULT '';
ALTER TABLE cost_plan_versions ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
ALTER TABLE cost_plan_versions ADD COLUMN IF NOT EXISTS updated_by TEXT NOT NULL DEFAULT '';
ALTER TABLE cost_plan_versions ADD COLUMN IF NOT EXISTS coverage_complete BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE cost_plan_versions ADD COLUMN IF NOT EXISTS coverage_notes TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS cost_plan_versions_active_baseline_year_idx
    ON cost_plan_versions (year)
    WHERE is_active AND status='approved' AND scenario='baseline';
CREATE INDEX IF NOT EXISTS cost_plan_versions_lookup_idx
    ON cost_plan_versions (year, status, scenario, updated_at DESC);
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname='cost_items_plan_version_fk'
          AND conrelid='cost_items'::regclass
    ) THEN
        ALTER TABLE cost_items ADD CONSTRAINT cost_items_plan_version_fk
        FOREIGN KEY (plan_version_id) REFERENCES cost_plan_versions(id) ON DELETE RESTRICT;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS savings_measurements (
    id TEXT PRIMARY KEY,
    action_id TEXT REFERENCES savings_actions(id) ON DELETE SET NULL,
    scope TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    account_id TEXT NOT NULL DEFAULT '',
    cost_bucket TEXT NOT NULL DEFAULT '',
    baseline_start_date DATE NOT NULL,
    baseline_end_date DATE NOT NULL,
    measurement_start_date DATE NOT NULL,
    measurement_end_date DATE NOT NULL,
    baseline_amount_usd NUMERIC(16,4) NOT NULL,
    actual_amount_usd NUMERIC(16,4) NOT NULL,
    evidence_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending_evidence',
    finance_reviewer TEXT NOT NULL DEFAULT '',
    reviewed_at TIMESTAMPTZ,
    notes TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK (baseline_start_date <= baseline_end_date),
    CHECK (measurement_start_date <= measurement_end_date),
    CHECK (baseline_amount_usd >= 0),
    CHECK (actual_amount_usd >= 0),
    CHECK (
        status NOT IN ('reviewed', 'verified', 'approved')
        OR (btrim(evidence_url) <> '' AND btrim(finance_reviewer) <> '' AND reviewed_at IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS savings_measurements_period_idx
    ON savings_measurements (status, measurement_start_date, measurement_end_date);
CREATE INDEX IF NOT EXISTS savings_measurements_scope_idx
    ON savings_measurements (scope, status, measurement_start_date, measurement_end_date);

CREATE TABLE IF NOT EXISTS stability_sync_state (
    backend_id TEXT PRIMARY KEY,
    window_start DATE,
    window_end DATE,
    status TEXT NOT NULL DEFAULT 'unknown',
    partial BOOLEAN NOT NULL DEFAULT FALSE,
    event_count BIGINT NOT NULL DEFAULT 0,
    synced_at TIMESTAMPTZ,
    error_message TEXT NOT NULL DEFAULT ''
);
"""


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _record_value(record: Any, key: str, default: Any = None) -> Any:
    try:
        return record[key]
    except (KeyError, IndexError, TypeError):
        return default


# 按姓名从另一侧目录反填的邮箱只用于展示，必须和上游真实邮箱区分开。
INFERRED_EMAIL_SOURCE = "inferred_primary_directory"


def bind_status(email: Any, email_source: Any = "") -> str:
    """员工邮箱绑定状态：已绑定 / 推断 / 未绑定。"""

    if not _clean_text(email):
        return "未绑定邮箱"
    if _clean_text(email_source) == INFERRED_EMAIL_SOURCE:
        return "邮箱推断"
    return "已绑定邮箱"


def _record_bind_status(record: Any, email_key: str = "employee_email", source_key: str = "email_source") -> str:
    return bind_status(_record_value(record, email_key), _record_value(record, source_key, ""))


def _first_nonempty(row: dict[str, Any], *keys: str) -> str:
    """Return the first explicit attribution value present in a usage row."""

    for key in keys:
        value = _clean_text(row.get(key))
        if value:
            return value
    return ""


def _usage_attribution(row: dict[str, Any]) -> tuple[str, str, str, str, str, bool]:
    """Resolve org/team/key attribution without inferring from email.

    Rows produced from request logs carry the explicit fields first.  The
    token/user mapping fallbacks are accepted only when callers provide them
    explicitly (for example ``tokenOrganizationId`` or ``userOrganizationId``).
    """

    organization_id = _first_nonempty(
        row,
        "organizationId",
        "organization_id",
        "orgId",
        "org_id",
        "tokenOrganizationId",
        "token_organization_id",
        "userOrganizationId",
        "user_organization_id",
    )
    team_id = _first_nonempty(
        row,
        "teamId",
        "team_id",
        "tokenTeamId",
        "token_team_id",
        "userTeamId",
        "user_team_id",
    )
    key_id = _first_nonempty(
        row,
        "keyId",
        "key_id",
        "tokenKeyId",
        "token_key_id",
    )
    principal_id = _first_nonempty(row, "principalId", "principal_id")
    attribution_source = _first_nonempty(
        row, "attributionSource", "attribution_source"
    )
    if not attribution_source:
        attribution_source = "explicit" if organization_id else "unattributed"
    billing_eligible = row.get("billingEligible", row.get("billing_eligible"))
    if billing_eligible is None:
        # Existing explicit and managed-token attribution remains billable.
        billing_eligible = bool(organization_id) and attribution_source not in {
            "legacy_report_only",
            "tenant_mapping_conflict",
            "unattributed",
        }
    return (
        organization_id,
        team_id,
        key_id,
        principal_id,
        attribution_source,
        bool(billing_eligible),
    )


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(_clean_text(value)[:10])


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    else:
        text = _clean_text(value)
        if not text:
            raise ValueError("缺少时间字段")
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            result = datetime.fromtimestamp(float(text), tz=timezone.utc)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _optional_date(value: Any) -> date | None:
    return _as_date(value) if value not in (None, "") else None


def _optional_datetime(value: Any) -> datetime | None:
    return _as_datetime(value) if value not in (None, "") else None


def _input_value(data: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in data:
            return data[name]
    return default


def empty_totals() -> dict[str, Any]:
    return {
        "promptTokens": 0,
        "completionTokens": 0,
        "totalTokens": 0,
        "requestCount": 0,
        "successCount": 0,
        "failureCount": 0,
        "spend": 0.0,
    }


def add_totals(target: dict[str, Any], row: dict[str, Any]) -> None:
    for field in (
        "promptTokens",
        "completionTokens",
        "totalTokens",
        "requestCount",
        "successCount",
        "failureCount",
    ):
        target[field] += _as_int(row.get(field))
    target["spend"] += _as_float(row.get("spend"))


def _department_names_for(index: dict[str, list[str]], email: Any, user_ids: list[Any]) -> list[str]:
    """按邮箱优先、再按各 user_id 查部门名，合并跨后端账号的部门归属。"""

    names: list[str] = []
    keys = [_clean_text(email).lower()] + [_clean_text(item).lower() for item in user_ids]
    for key in keys:
        if not key:
            continue
        for name in index.get(key, []):
            if name not in names:
                names.append(name)
    return names


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_date: dict[str, dict[str, Any]] = {}
    by_source: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    total = empty_totals()
    for row in rows:
        add_totals(total, row)
        day = _clean_text(row.get("date"))
        if day:
            bucket = by_date.setdefault(day, {"date": day, **empty_totals()})
            add_totals(bucket, row)
        source = _clean_text(row.get("source")) or "其他"
        bucket = by_source.setdefault(source, {"source": source, **empty_totals()})
        add_totals(bucket, row)
        model = _clean_text(row.get("model")) or "未知模型"
        bucket = by_model.setdefault(model, {"model": model, **empty_totals()})
        add_totals(bucket, row)
    latest = by_date[sorted(by_date)[-1]] if by_date else None
    return {
        "latestDay": latest,
        "rangeTotal": total,
        "sourceBreakdown": sorted(by_source.values(), key=lambda item: item["totalTokens"], reverse=True),
        "modelBreakdown": sorted(by_model.values(), key=lambda item: item["totalTokens"], reverse=True),
    }


class UsageStore:
    """Small PostgreSQL adapter for aggregated usage snapshots only."""

    def __init__(
        self,
        dsn: str,
        min_size: int = 1,
        max_size: int = 5,
        read_only: bool = False,
    ) -> None:
        self.dsn = dsn
        self.min_size = min_size
        self.max_size = max_size
        self.read_only = read_only
        self.pool: Any = None
        self._connect_lock = asyncio.Lock()

    @classmethod
    def from_environment(cls) -> UsageStore | None:
        dsn = os.getenv("USAGE_DATABASE_URL", "").strip()
        # Realtime mode always needs PostgreSQL for history, archive and
        # database fallback. Keep the legacy flag for old local deployments,
        # but do not let it accidentally disable the new realtime pipeline.
        enabled = os.getenv("USAGE_SYNC_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
        realtime = os.getenv("USAGE_REALTIME_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
        enabled = enabled or realtime
        if not enabled or not dsn:
            return None
        try:
            min_size = max(1, int(os.getenv("USAGE_DB_POOL_MIN_SIZE", "1")))
        except ValueError:
            min_size = 1
        try:
            max_size = max(min_size, int(os.getenv("USAGE_DB_POOL_MAX_SIZE", "5")))
        except ValueError:
            max_size = max(5, min_size)
        read_only = os.getenv("REMOTE_DEMO_READ_ONLY", "false").strip().lower() in {"1", "true", "yes", "on"}
        return cls(dsn, min_size=min_size, max_size=max_size, read_only=read_only)

    async def connect(self) -> None:
        if self.pool is not None:
            return
        if asyncpg is None:
            raise RuntimeError("USAGE_SYNC_ENABLED=true 时需要安装 asyncpg")
        async with self._connect_lock:
            if self.pool is not None:
                return
            server_settings = {"default_transaction_read_only": "on"} if self.read_only else None
            pool = await asyncpg.create_pool(
                self.dsn,
                min_size=self.min_size,
                max_size=self.max_size,
                command_timeout=30,
                server_settings=server_settings,
            )
            if not self.read_only:
                try:
                    await pool.execute(USAGE_SCHEMA)
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
            raise RuntimeError("用量数据库尚未连接")
        return self.pool

    async def upsert_identity_directory(
        self,
        backend_id: str,
        identities: list[dict[str, Any]],
    ) -> int:
        """Persist the latest identity facts for one backend."""
        rows: list[tuple[str, str, str, str, str, str]] = []
        for identity in identities:
            user_id = str(identity.get("userId") or identity.get("user_id") or "").strip()
            if not user_id:
                continue
            display_name = str(
                identity.get("displayName")
                or identity.get("display_name")
                or identity.get("name")
                or ""
            ).strip()
            email = str(
                identity.get("employeeEmail")
                or identity.get("employee_email")
                or identity.get("email")
                or ""
            ).strip()
            source = str(identity.get("nameSource") or identity.get("name_source") or "user_id").strip()
            confidence = str(identity.get("confidence") or "low").strip()
            rows.append((backend_id, user_id, display_name, email, source, confidence))
        if not rows:
            return 0
        await self._require_pool().executemany(
            """
            INSERT INTO usage_identity_directory (
                backend_id, user_id, display_name, employee_email,
                name_source, confidence, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, NOW())
            ON CONFLICT (backend_id, user_id) DO UPDATE SET
                display_name = CASE
                    WHEN EXCLUDED.display_name <> '' THEN EXCLUDED.display_name
                    ELSE usage_identity_directory.display_name
                END,
                employee_email = CASE
                    WHEN EXCLUDED.employee_email <> '' THEN EXCLUDED.employee_email
                    ELSE usage_identity_directory.employee_email
                END,
                name_source = CASE
                    WHEN EXCLUDED.display_name <> '' THEN EXCLUDED.name_source
                    ELSE usage_identity_directory.name_source
                END,
                confidence = CASE
                    WHEN EXCLUDED.display_name <> '' THEN EXCLUDED.confidence
                    ELSE usage_identity_directory.confidence
                END,
                updated_at = NOW()
            """,
            rows,
        )
        return len(rows)

    async def identity_directory(
        self,
        backend_ids: list[str],
    ) -> dict[tuple[str, str], dict[str, str]]:
        """Load identity facts keyed by ``(backend_id, user_id)``."""
        if not backend_ids:
            return {}
        records = await self._require_pool().fetch(
            """
            SELECT backend_id, user_id, display_name, employee_email,
                   name_source, confidence
            FROM usage_identity_directory
            WHERE backend_id = ANY($1::text[])
            """,
            backend_ids,
        )
        return {
            (str(row["backend_id"]), str(row["user_id"])): {
                "displayName": str(row["display_name"] or ""),
                "employeeEmail": str(row["employee_email"] or ""),
                "nameSource": str(row["name_source"] or "user_id"),
                "confidence": str(row["confidence"] or "low"),
            }
            for row in records
        }

    async def refresh_usage_identity_columns(self, backend_ids: list[str]) -> int:
        """Backfill stale snapshot names without changing usage measures."""
        if not backend_ids:
            return 0
        query = """
            UPDATE {table} AS u
            SET employee_name = d.display_name,
                employee_email = CASE WHEN d.employee_email <> '' THEN d.employee_email ELSE u.employee_email END,
                email_source = CASE WHEN d.employee_email <> '' THEN 'identity_directory' ELSE u.email_source END
            FROM usage_identity_directory AS d
            WHERE u.backend_id = d.backend_id
              AND u.user_id = d.user_id
              AND d.display_name <> ''
              AND (u.employee_name = '' OR u.employee_name = u.user_id
                   OR u.employee_name IS NULL)
              AND u.backend_id = ANY($1::text[])
        """
        total = 0
        for table in ("usage_daily", "usage_realtime_daily"):
            result = await self._require_pool().execute(query.format(table=table), backend_ids)
            try:
                total += int(str(result).rsplit(" ", 1)[-1])
            except (ValueError, IndexError):
                continue
        return total

    async def identity_directory_health(self, backend_ids: list[str]) -> dict[str, Any]:
        if not backend_ids:
            return {"total": 0, "lowConfidence": 0, "updatedAt": None}
        row = await self._require_pool().fetchrow(
            """
            SELECT COUNT(*)::bigint AS total,
                   COUNT(*) FILTER (WHERE confidence='low')::bigint AS low_confidence,
                   MAX(updated_at) AS updated_at
            FROM usage_identity_directory
            WHERE backend_id = ANY($1::text[])
            """,
            backend_ids,
        )
        return {
            "total": _as_int(row["total"]) if row else 0,
            "lowConfidence": _as_int(row["low_confidence"]) if row else 0,
            "updatedAt": row["updated_at"] if row else None,
        }

    async def try_acquire_sync_lock(self) -> Any | None:
        pool = self._require_pool()
        connection = await pool.acquire()
        locked = await connection.fetchval("SELECT pg_try_advisory_lock(hashtext('ai-token-dashboard:usage-sync'))")
        if not locked:
            await pool.release(connection)
            return None
        return connection

    async def release_sync_lock(self, connection: Any) -> None:
        pool = self._require_pool()
        try:
            await connection.execute("SELECT pg_advisory_unlock(hashtext('ai-token-dashboard:usage-sync'))")
        finally:
            await pool.release(connection)

    async def begin_sync_run(self, start_date: str, end_date: str) -> int:
        return int(
            await self._require_pool().fetchval(
                """
                INSERT INTO usage_sync_runs (started_at, start_date, end_date, status)
                VALUES ($1, $2::date, $3::date, 'running')
                RETURNING id
                """,
                datetime.now(timezone.utc),
                _as_date(start_date),
                _as_date(end_date),
            )
        )

    async def finish_sync_run(
        self,
        run_id: int,
        status: str,
        backend_count: int,
        row_count: int,
        error_message: str = "",
    ) -> None:
        await self._require_pool().execute(
            """
            UPDATE usage_sync_runs
            SET finished_at = $1, status = $2, backend_count = $3,
                row_count = $4, error_message = $5
            WHERE id = $6
            """,
            datetime.now(timezone.utc),
            status,
            backend_count,
            row_count,
            error_message[:2000],
            run_id,
        )

    async def update_worker_state(
        self,
        *,
        worker_id: str,
        status: str,
        heartbeat_at: datetime | None = None,
        last_started_at: datetime | None = None,
        last_finished_at: datetime | None = None,
        last_success_at: datetime | None = None,
        last_error: str | None = None,
        snapshot_revision: str | None = None,
    ) -> None:
        await self._require_pool().execute(
            """
            INSERT INTO usage_sync_state (
                singleton, worker_id, status, heartbeat_at, last_started_at,
                last_finished_at, last_success_at, last_error, snapshot_revision
            ) VALUES (TRUE, $1, $2, $3, $4, $5, $6, COALESCE($7, ''), COALESCE($8, ''))
            ON CONFLICT (singleton) DO UPDATE SET
                worker_id=EXCLUDED.worker_id,
                status=EXCLUDED.status,
                heartbeat_at=COALESCE(EXCLUDED.heartbeat_at, usage_sync_state.heartbeat_at),
                last_started_at=COALESCE(EXCLUDED.last_started_at, usage_sync_state.last_started_at),
                last_finished_at=COALESCE(EXCLUDED.last_finished_at, usage_sync_state.last_finished_at),
                last_success_at=COALESCE(EXCLUDED.last_success_at, usage_sync_state.last_success_at),
                last_error=CASE WHEN $7 IS NULL THEN usage_sync_state.last_error ELSE EXCLUDED.last_error END,
                snapshot_revision=CASE WHEN $8 IS NULL THEN usage_sync_state.snapshot_revision ELSE EXCLUDED.snapshot_revision END
            """,
            worker_id,
            status,
            heartbeat_at,
            last_started_at,
            last_finished_at,
            last_success_at,
            last_error,
            snapshot_revision,
        )

    async def heartbeat_worker(self, worker_id: str, status: str = "idle") -> None:
        await self.update_worker_state(
            worker_id=worker_id,
            status=status,
            heartbeat_at=datetime.now(timezone.utc),
        )

    async def enqueue_refresh_request(self, start_date: str, end_date: str) -> bool:
        """Queue one idempotent reader refresh request for the sync worker."""

        request_key = hashlib.sha256(
            f"{start_date}:{end_date}".encode("ascii")
        ).hexdigest()
        result = await self._require_pool().execute(
            """
            INSERT INTO usage_refresh_requests (
                request_key, start_date, end_date, status, requested_at,
                claimed_at, completed_at, attempts, last_error, next_attempt_at
            ) VALUES ($1, $2::date, $3::date, 'pending', $4, NULL, NULL, 0, '', NULL)
            ON CONFLICT (request_key) DO UPDATE SET
                status=CASE
                    WHEN usage_refresh_requests.status='running'
                    THEN usage_refresh_requests.status
                    ELSE 'pending'
                END,
                requested_at=CASE
                    WHEN usage_refresh_requests.status='running'
                    THEN usage_refresh_requests.requested_at
                    ELSE EXCLUDED.requested_at
                END,
                claimed_at=CASE
                    WHEN usage_refresh_requests.status='running'
                    THEN usage_refresh_requests.claimed_at
                    ELSE NULL
                END,
                completed_at=CASE
                    WHEN usage_refresh_requests.status='running'
                    THEN usage_refresh_requests.completed_at
                    ELSE NULL
                END,
                last_error=CASE
                    WHEN usage_refresh_requests.status='running'
                    THEN usage_refresh_requests.last_error
                    ELSE ''
                END,
                next_attempt_at=CASE
                    WHEN usage_refresh_requests.status='running'
                    THEN usage_refresh_requests.next_attempt_at
                    ELSE NULL
                END
            """,
            request_key,
            _as_date(start_date),
            _as_date(end_date),
            datetime.now(timezone.utc),
        )
        return result.endswith(" 1")

    async def claim_refresh_requests(
        self,
        *,
        limit: int = 100,
        stale_after_seconds: int = 3600,
    ) -> list[dict[str, Any]]:
        """Claim pending refreshes, recovering requests stranded by a restart."""

        rows = await self._require_pool().fetch(
            """
            WITH candidates AS (
                SELECT request_key
                FROM usage_refresh_requests
                WHERE (status='pending' AND (next_attempt_at IS NULL OR next_attempt_at <= $1))
                   OR (status='running' AND claimed_at < $1::timestamptz - ($2::double precision * INTERVAL '1 second'))
                ORDER BY requested_at
                FOR UPDATE SKIP LOCKED
                LIMIT $3
            )
            UPDATE usage_refresh_requests AS request
            SET status='running', claimed_at=$1, attempts=request.attempts + 1,
                last_error='', next_attempt_at=NULL
            FROM candidates
            WHERE request.request_key=candidates.request_key
            RETURNING request.request_key, request.start_date, request.end_date,
                      request.requested_at, request.attempts
            """,
            datetime.now(timezone.utc),
            max(60, int(stale_after_seconds)),
            max(1, int(limit)),
        )
        return [
            {
                "requestKey": str(row["request_key"]),
                "startDate": row["start_date"].isoformat(),
                "endDate": row["end_date"].isoformat(),
                "requestedAt": row["requested_at"],
                "attempts": int(row["attempts"] or 0),
            }
            for row in rows
        ]

    async def finish_refresh_requests(
        self,
        request_keys: list[str],
        *,
        success: bool,
        error: str = "",
        retry_after_seconds: int = 0,
        duration_ms: float | None = None,
    ) -> None:
        if not request_keys:
            return
        await self._require_pool().execute(
            """
            UPDATE usage_refresh_requests
            SET status=$1, completed_at=$2, last_error=$3, next_attempt_at=$5,
                last_duration_ms=$6
            WHERE request_key=ANY($4::text[])
            """,
            "completed" if success else "pending",
            datetime.now(timezone.utc) if success else None,
            str(error or "")[:2000],
            request_keys,
            (
                datetime.now(timezone.utc) + timedelta(seconds=max(0, int(retry_after_seconds)))
                if not success and retry_after_seconds > 0
                else None
            ),
            float(duration_ms) if duration_ms is not None else None,
        )

    async def refresh_queue_status(self) -> dict[str, Any]:
        row = await self._require_pool().fetchrow(
            """
            SELECT COUNT(*) FILTER (WHERE status='pending') AS pending_count,
                   COUNT(*) FILTER (WHERE status='running') AS running_count,
                   MIN(requested_at) FILTER (WHERE status IN ('pending', 'running')) AS oldest_requested_at,
                   MAX(attempts) FILTER (WHERE status IN ('pending', 'running')) AS max_attempts,
                   MAX(claimed_at) FILTER (WHERE status IN ('pending', 'running')) AS last_attempted_at,
                   (array_agg(last_error ORDER BY claimed_at DESC NULLS LAST)
                       FILTER (WHERE status IN ('pending', 'running') AND last_error <> ''))[1] AS last_error,
                   (array_agg(last_duration_ms ORDER BY claimed_at DESC NULLS LAST)
                       FILTER (WHERE status IN ('pending', 'running')))[1] AS last_duration_ms
            FROM usage_refresh_requests
            """
        )
        return {
            "pendingCount": int((row or {}).get("pending_count") or 0),
            "runningCount": int((row or {}).get("running_count") or 0),
            "oldestRequestedAt": (row or {}).get("oldest_requested_at"),
            "maxAttempts": int((row or {}).get("max_attempts") or 0),
            "lastAttemptedAt": (row or {}).get("last_attempted_at"),
            "lastError": str((row or {}).get("last_error") or ""),
            "lastDurationMs": (
                float(row["last_duration_ms"])
                if (row or {}).get("last_duration_ms") is not None
                else None
            ),
        }

    async def snapshot_revision(
        self,
        start_date: str,
        end_date: str,
        backend_ids: list[str],
    ) -> str | None:
        if not backend_ids:
            return None
        coverage_revision = await self._require_pool().fetchval(
            """
            SELECT MIN(synced_at)::text
            FROM usage_sync_coverage
            WHERE usage_date=$1::date AND backend_id=ANY($2::text[])
            HAVING COUNT(DISTINCT backend_id)=cardinality($2::text[])
            """,
            _as_date(end_date),
            sorted(set(backend_ids)),
        )
        try:
            realtime = await self._require_pool().fetchrow(
                """
                SELECT revision, updated_at FROM usage_realtime_state
                WHERE usage_date=$1 AND ready
                """,
                _as_date(end_date),
            )
        except Exception:
            # Compatibility with lightweight test doubles and pre-migration DBs.
            realtime = None
        if realtime:
            return f"{coverage_revision or ''}:rt:{int(realtime['revision'] or 0)}:{realtime['updated_at']}"
        return coverage_revision

    async def sync_state(self) -> dict[str, Any]:
        row = await self._require_pool().fetchrow(
            """
            SELECT s.worker_id, s.status, s.heartbeat_at, s.last_started_at,
                   s.last_finished_at, s.last_success_at, s.last_error,
                   COALESCE(NULLIF(s.snapshot_revision, ''), p.revision) AS snapshot_revision,
                   p.published_at, p.start_date, p.end_date, p.backend_ids
            FROM usage_sync_state s
            CROSS JOIN usage_snapshot_state p
            WHERE s.singleton=TRUE AND p.singleton=TRUE
            """
        )
        if row is None:
            return {"status": "missing"}
        return {
            "workerId": str(row["worker_id"] or ""),
            "status": str(row["status"] or "unknown"),
            "heartbeatAt": row["heartbeat_at"],
            "lastStartedAt": row["last_started_at"],
            "lastFinishedAt": row["last_finished_at"],
            "lastSuccessAt": row["last_success_at"],
            "publishedAt": row["published_at"],
            "lastError": str(row["last_error"] or ""),
            "snapshotRevision": str(row["snapshot_revision"] or ""),
            "publishedAt": row["published_at"],
            "startDate": row["start_date"].isoformat() if row["start_date"] else None,
            "endDate": row["end_date"].isoformat() if row["end_date"] else None,
            "backendIds": list(row["backend_ids"] or []),
        }

    async def snapshot_state(self) -> dict[str, Any]:
        row = await self._require_pool().fetchrow(
            """
            SELECT revision, published_at, start_date, end_date, backend_ids
            FROM usage_snapshot_state WHERE singleton=TRUE
            """
        )
        if row is None or not row["revision"]:
            # 发布状态表是随本次改动新建的，首次发布前为空；已同步过的历史快照仍在
            # usage_sync_coverage 里，用它的最新同步时间兜底，避免升级后到首次发布
            # 之间把权限读取整体拒掉。
            return await self._snapshot_state_from_coverage()
        return {
            "revision": str(row["revision"] or ""),
            "publishedAt": row["published_at"],
            "startDate": row["start_date"].isoformat() if row["start_date"] else None,
            "endDate": row["end_date"].isoformat() if row["end_date"] else None,
            "backendIds": list(row["backend_ids"] or []),
        }

    async def _snapshot_state_from_coverage(self) -> dict[str, Any]:
        row = await self._require_pool().fetchrow(
            """
            SELECT MAX(synced_at)::text AS revision, MAX(synced_at) AS published_at,
                   MIN(usage_date) AS start_date, MAX(usage_date) AS end_date,
                   array_agg(DISTINCT backend_id) AS backend_ids
            FROM usage_sync_coverage
            """
        )
        if row is None or not row["revision"]:
            return {"revision": "", "publishedAt": None}
        return {
            "revision": str(row["revision"]),
            "publishedAt": row["published_at"],
            "startDate": row["start_date"].isoformat() if row["start_date"] else None,
            "endDate": row["end_date"].isoformat() if row["end_date"] else None,
            "backendIds": list(row["backend_ids"] or []),
        }

    async def latest_success_at(self) -> datetime | None:
        return await self._require_pool().fetchval(
            "SELECT last_success_at FROM usage_sync_state WHERE singleton=TRUE"
        )

    async def realtime_recovery_rows(self, usage_date: date) -> list[dict[str, Any]]:
        records = await self._require_pool().fetch(
            """
            SELECT backend_id, usage_date, user_id, employee_email, employee_name,
                   email_source, source, model, prompt_tokens, completion_tokens,
                   total_tokens, request_count, success_count, failure_count, spend,
                   organization_id, team_id, key_id, principal_id,
                   attribution_source, billing_eligible
            FROM usage_query_daily WHERE usage_date=$1
            ORDER BY backend_id, user_id, source, model
            """,
            usage_date,
        )
        return [
            {
                "backendId": str(row["backend_id"]),
                "date": row["usage_date"].isoformat(),
                "userId": str(row["user_id"]),
                "employeeEmail": str(row["employee_email"] or ""),
                "employeeName": str(row["employee_name"] or ""),
                "emailSource": str(row["email_source"] or ""),
                "source": str(row["source"]),
                "model": str(row["model"]),
                "promptTokens": int(row["prompt_tokens"] or 0),
                "completionTokens": int(row["completion_tokens"] or 0),
                "totalTokens": int(row["total_tokens"] or 0),
                "requestCount": int(row["request_count"] or 0),
                "successCount": int(row["success_count"] or 0),
                "failureCount": int(row["failure_count"] or 0),
                "spend": float(row["spend"] or 0),
                "organizationId": str(row["organization_id"] or ""),
                "teamId": str(row["team_id"] or ""),
                "keyId": str(row["key_id"] or ""),
                "principalId": str(row["principal_id"] or ""),
                "attributionSource": str(row["attribution_source"] or "unattributed"),
                "billingEligible": bool(row["billing_eligible"]),
            }
            for row in records
        ]

    async def realtime_identity_map(self, usage_date: date) -> dict[tuple[str, str], dict[str, str]]:
        records = await self._require_pool().fetch(
            """
            SELECT backend_id, user_id, employee_email, employee_name, email_source
            FROM usage_daily
            WHERE usage_date=$1 AND (employee_email<>'' OR employee_name<>'')
            """,
            usage_date,
        )
        return {
            (str(row["backend_id"]), str(row["user_id"])): {
                "email": str(row["employee_email"] or ""),
                "name": str(row["employee_name"] or ""),
                "emailSource": str(row["email_source"] or ""),
            }
            for row in records
        }

    async def realtime_request_ids(self, since: datetime) -> list[tuple[str, str]]:
        records = await self._require_pool().fetch(
            """
            SELECT backend_id, request_id FROM usage_event_attribution
            WHERE event_time >= $1 ORDER BY event_time
            """,
            since,
        )
        return [(str(row["backend_id"]), str(row["request_id"])) for row in records]

    async def realtime_event_rows(self, usage_date: date) -> list[dict[str, Any]]:
        """Aggregate only audited request facts for the published realtime day."""

        records = await self._require_pool().fetch(
            """
            SELECT e.backend_id, e.usage_date, e.raw_user_id, e.source, e.model,
                   organization_id, team_id, key_id, principal_id, attribution_source,
                   billing_eligible, MAX(event_time) AS event_time,
                   MAX(collected_at) AS collected_at,
                   SUM(prompt_tokens)::bigint AS prompt_tokens, SUM(completion_tokens)::bigint AS completion_tokens,
                   SUM(total_tokens)::bigint AS total_tokens, SUM(request_count)::bigint AS request_count,
                   SUM(success_count)::bigint AS success_count, SUM(failure_count)::bigint AS failure_count,
                   SUM(spend)::numeric AS spend,
                   COALESCE(MAX(d.employee_email), MAX(i.employee_email), '') AS employee_email,
                   COALESCE(MAX(d.employee_name), MAX(i.display_name), '') AS employee_name,
                   COALESCE(MAX(i.name_source), MAX(d.email_source), 'user_id') AS name_source,
                   COALESCE(MAX(i.confidence), 'low') AS name_confidence,
                   COALESCE(MAX(d.email_source), MAX(i.name_source), '') AS email_source
            FROM usage_event_attribution e
            LEFT JOIN LATERAL (
                SELECT employee_email, employee_name, email_source
                FROM usage_daily d
                WHERE d.backend_id=e.backend_id AND d.usage_date=e.usage_date AND d.user_id=e.raw_user_id
                ORDER BY employee_email DESC, employee_name DESC
                LIMIT 1
            ) d ON TRUE
            LEFT JOIN usage_identity_directory i
              ON i.backend_id=e.backend_id AND i.user_id=e.raw_user_id
            WHERE e.usage_date=$1
            GROUP BY e.backend_id, e.usage_date, e.raw_user_id, e.source, e.model,
                     organization_id, team_id, key_id, principal_id, attribution_source, billing_eligible
            ORDER BY e.backend_id, e.raw_user_id, e.source, e.model
            """,
            usage_date,
        )
        return [
            {
                "backendId": str(row["backend_id"]), "date": row["usage_date"].isoformat(),
                "userId": str(row["raw_user_id"]), "employeeEmail": str(row["employee_email"] or ""), "employeeName": str(row["employee_name"] or row["raw_user_id"]),
                "nameSource": str(row["name_source"] or "user_id"), "nameConfidence": str(row["name_confidence"] or "low"),
                "emailSource": str(row["email_source"] or ""), "source": str(row["source"]), "model": str(row["model"]),
                "promptTokens": int(row["prompt_tokens"] or 0), "completionTokens": int(row["completion_tokens"] or 0),
                "totalTokens": int(row["total_tokens"] or 0), "requestCount": int(row["request_count"] or 0),
                "successCount": int(row["success_count"] or 0), "failureCount": int(row["failure_count"] or 0),
                "spend": float(row["spend"] or 0), "organizationId": str(row["organization_id"] or ""),
                "teamId": str(row["team_id"] or ""), "keyId": str(row["key_id"] or ""),
                "principalId": str(row["principal_id"] or ""),
                "attributionSource": str(row["attribution_source"] or "unattributed"),
                "billingEligible": bool(row["billing_eligible"]),
            }
            for row in records
        ]

    async def latest_archived_event_at(self, backend_id: str) -> datetime | None:
        return await self._require_pool().fetchval(
            "SELECT MAX(event_time) FROM usage_event_attribution WHERE backend_id=$1",
            backend_id,
        )

    async def archive_realtime_events(self, events: list[dict[str, Any]]) -> int:
        if not events:
            return 0
        collected_at = datetime.now(timezone.utc)
        records = [
            record
            for event in events
            if (record := self._event_record(str(event.get("backendId") or ""), event, collected_at))
        ]
        if not records:
            return 0
        async with self._require_pool().acquire() as connection:
            async with connection.transaction():
                inserted = 0
                for record in records:
                    result = await connection.execute(
                        """
                        INSERT INTO usage_event_attribution (
                            backend_id, request_id, event_time, usage_date, raw_user_id,
                            organization_id, team_id, key_id, principal_id, source, model,
                            prompt_tokens, completion_tokens, total_tokens, request_count,
                            success_count, failure_count, spend, attribution_source,
                            billing_eligible, provider, model_group, model_id, api_base,
                            status, error_code, error_class, error_message, scenario,
                            request_duration_ms, ttft_ms, attempted_retries, max_retries,
                            trace_id, user_visible_failure, final_failure_source, collected_at
                        ) VALUES (
                            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                            $17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29,$30,
                            $31,$32,$33,$34,$35,$36,$37
                        ) ON CONFLICT (backend_id, request_id) DO NOTHING
                        """,
                        *record,
                    )
                    if result.endswith(" 1"):
                        inserted += 1
        return inserted

    async def publish_realtime_state(
        self,
        usage_date: date,
        *,
        ready: bool,
        revision: int,
        latest_event_at: datetime | None,
    ) -> None:
        now = datetime.now(timezone.utc)
        await self._require_pool().execute(
            """
            INSERT INTO usage_realtime_state (
                usage_date, ready, revision, latest_event_at, last_archived_at, updated_at
            ) VALUES ($1,$2,$3,$4,$5,$5)
            ON CONFLICT (usage_date) DO UPDATE SET
                ready=EXCLUDED.ready, revision=EXCLUDED.revision,
                latest_event_at=COALESCE(EXCLUDED.latest_event_at, usage_realtime_state.latest_event_at),
                last_archived_at=EXCLUDED.last_archived_at, updated_at=EXCLUDED.updated_at
            """,
            usage_date,
            ready,
            revision,
            latest_event_at,
            now,
        )

    async def replace_realtime_aggregates(
        self, usage_date: date, rows: list[dict[str, Any]]
    ) -> int:
        collected_at = datetime.now(timezone.utc)
        records = [
            self._usage_record(
                str(row.get("backendId") or ""),
                {**row, "_userId": row.get("userId")},
                collected_at,
            )
            for row in self._coalesce_usage_rows(rows)
            if row.get("backendId") and row.get("date")
        ]
        async with self._require_pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM usage_realtime_daily WHERE usage_date=$1", usage_date
                )
                if records:
                    await connection.executemany(
                        """
                        INSERT INTO usage_realtime_daily (
                            backend_id, usage_date, user_id, employee_email, employee_name,
                            source, model, prompt_tokens, completion_tokens, total_tokens,
                            request_count, success_count, failure_count, spend, collected_at,
                            organization_id, team_id, key_id, principal_id,
                            attribution_source, billing_eligible, email_source
                        ) VALUES (
                            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                            $16,$17,$18,$19,$20,$21,$22
                        )
                        """,
                        records,
                    )
        return len(records)

    async def publish_realtime_coverage(
        self, usage_date: date, backend_ids: list[str]
    ) -> None:
        if not backend_ids:
            return
        now = datetime.now(timezone.utc)
        await self._require_pool().executemany(
            """
            INSERT INTO usage_sync_coverage (backend_id, usage_date, synced_at)
            VALUES ($1,$2,$3)
            ON CONFLICT (backend_id, usage_date) DO UPDATE SET synced_at=EXCLUDED.synced_at
            """,
            [(backend_id, usage_date, now) for backend_id in backend_ids],
        )

    async def finalize_realtime_day(
        self,
        usage_date: date,
        identities: dict[tuple[str, str], dict[str, str]] | None = None,
    ) -> int:
        """Promote a completed realtime mirror into the historical base table."""

        async with self._require_pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM usage_realtime_daily WHERE usage_date=$1", usage_date
                )
                await connection.execute(
                    """
                    INSERT INTO usage_realtime_daily (
                        backend_id, usage_date, user_id, organization_id, team_id, key_id,
                        principal_id, attribution_source, billing_eligible, employee_email,
                        employee_name, email_source, source, model, prompt_tokens,
                        completion_tokens, total_tokens, request_count, success_count,
                        failure_count, spend, collected_at
                    )
                    SELECT e.backend_id, e.usage_date, e.raw_user_id, e.organization_id,
                           e.team_id, e.key_id, e.principal_id, e.attribution_source,
                           e.billing_eligible, MAX(u.employee_email), MAX(u.employee_name),
                           MAX(u.email_source), e.source, e.model, SUM(e.prompt_tokens),
                           SUM(e.completion_tokens), SUM(e.total_tokens), SUM(e.request_count),
                           SUM(e.success_count), SUM(e.failure_count),
                           SUM(e.spend)::double precision, $2
                    FROM usage_event_attribution e
                    LEFT JOIN usage_daily u ON u.backend_id=e.backend_id
                        AND u.usage_date=e.usage_date AND u.user_id=e.raw_user_id
                    WHERE e.usage_date=$1
                    GROUP BY e.backend_id, e.usage_date, e.raw_user_id, e.organization_id,
                             e.team_id, e.key_id, e.principal_id, e.attribution_source,
                             e.billing_eligible, e.source, e.model
                    """,
                    usage_date,
                    datetime.now(timezone.utc),
                )
                if identities:
                    await connection.executemany(
                        """
                        UPDATE usage_realtime_daily SET
                            employee_email=$3, employee_name=$4, email_source=$5
                        WHERE usage_date=$1 AND backend_id=$2 AND user_id=$6
                        """,
                        [
                            (
                                usage_date,
                                backend_id,
                                item.get("email", ""),
                                item.get("name", ""),
                                item.get("emailSource", ""),
                                user_id,
                            )
                            for (backend_id, user_id), item in identities.items()
                        ],
                    )
                await connection.execute(
                    "DELETE FROM usage_daily WHERE usage_date=$1", usage_date
                )
                result = await connection.execute(
                    """
                    INSERT INTO usage_daily
                    SELECT * FROM usage_realtime_daily WHERE usage_date=$1
                    """,
                    usage_date,
                )
                await connection.execute(
                    "UPDATE usage_realtime_state SET ready=FALSE, updated_at=$2 WHERE usage_date=$1",
                    usage_date,
                    datetime.now(timezone.utc),
                )
                await connection.execute(
                    "DELETE FROM usage_realtime_daily WHERE usage_date=$1", usage_date
                )
        return int(result.rsplit(" ", 1)[-1]) if result else 0

    async def realtime_state(self, usage_date: date) -> dict[str, Any]:
        row = await self._require_pool().fetchrow(
            """
            SELECT ready, revision, latest_event_at, last_archived_at, updated_at
            FROM usage_realtime_state WHERE usage_date=$1
            """,
            usage_date,
        )
        return dict(row) if row else {}

    async def team_rows(self, team_scopes: list[dict[str, Any]], start_date: str, end_date: str, source: str) -> dict[str, Any] | None:
        """Read one logical team from all covered backend/team pairs in one SQL query."""
        backend_ids = [str(item.get("backend")) for item in team_scopes if item.get("backend") and item.get("id")]
        team_ids = [str(item.get("id")) for item in team_scopes if item.get("backend") and item.get("id")]
        covered = sorted(set(backend_ids))
        if not backend_ids or not await self.has_complete_coverage(start_date, end_date, covered):
            return None
        model_sql = "u.model"
        records = await self._require_pool().fetch(
            f"""
            WITH scope(backend_id, team_id) AS (SELECT * FROM unnest($1::text[], $2::text[])),
            members AS (
                SELECT DISTINCT ON (m.backend_id, m.user_id) m.backend_id, m.team_id, m.team_name,
                       m.user_id, m.employee_email, m.employee_name, m.team_role
                FROM usage_team_membership_daily m JOIN scope s ON s.backend_id=m.backend_id AND s.team_id=m.team_id
                WHERE m.snapshot_date <= $4::date
                ORDER BY m.backend_id, m.user_id, m.snapshot_date DESC
            )
            SELECT 'member' AS kind, m.backend_id, m.team_id, m.team_name, m.user_id, m.employee_email, m.employee_name, ''::text AS email_source, m.team_role,
                   NULL::date AS usage_date, NULL::text AS source, NULL::text AS model_name,
                   0::bigint AS prompt_tokens, 0::bigint AS completion_tokens, 0::bigint AS total_tokens,
                   0::bigint AS request_count, 0::bigint AS success_count, 0::bigint AS failure_count, 0::double precision AS spend
            FROM members m
            UNION ALL
             SELECT 'usage', u.backend_id, u.team_id, NULL, u.user_id, MAX(u.employee_email), MAX(u.employee_name), MAX(u.email_source), NULL,
                    u.usage_date, u.source, {model_sql}, {self._aggregate_metrics_sql('u.')}
             FROM usage_query_daily u
             WHERE u.backend_id = ANY($1::text[])
               AND u.usage_date BETWEEN $3::date AND $4::date
               AND ($5 = 'all' OR u.source = $5)
               AND EXISTS (
                   SELECT 1 FROM scope s
                   WHERE s.backend_id=u.backend_id AND s.team_id=u.team_id
               )
             GROUP BY u.backend_id, u.usage_date, u.user_id, u.team_id, u.source, {model_sql}
            ORDER BY kind, usage_date NULLS FIRST, backend_id, user_id, source, model_name
            """,
            backend_ids, team_ids, _as_date(start_date), _as_date(end_date), source or "all",
        )
        member_records = [item for item in records if item["kind"] == "member"]
        usage_records = [item for item in records if item["kind"] == "usage"]
        rows = []
        for record in usage_records:
            row = self._aggregated_usage_row(record)
            row.update(
                {
                    "employeeId": record["user_id"],
                    "employeeName": record["employee_name"] or record["user_id"],
                    "employeeEmail": record["employee_email"] or "",
                    "bindStatus": _record_bind_status(record),
                }
            )
            rows.append(row)
        employees_by_identity: dict[str, dict[str, Any]] = {}
        source_totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for row, record in zip(rows, usage_records):
            email = str(row.get("employeeEmail") or "").strip().lower()
            identity = f"email:{email}" if email else f"id:{record['backend_id']}:{record['user_id']}"
            public_id = row["employeeId"] if email else f"{record['backend_id']}:{record['user_id']}"
            item = employees_by_identity.setdefault(identity, {"employeeId": public_id, "employeeName": row["employeeName"], "employeeEmail": email, "bindStatus": row["bindStatus"], **empty_totals(), "primarySource": "其他", "userIds": [], "teamRole": "user"})
            add_totals(item, row)
            source_totals[identity][str(row.get("source") or "其他")] += _as_int(row.get("totalTokens"))
            account_id = f"{record['backend_id']}:{record['user_id']}"
            if account_id not in item["userIds"]:
                item["userIds"].append(account_id)
        for identity, item in employees_by_identity.items():
            if source_totals[identity]:
                item["primarySource"] = max(source_totals[identity].items(), key=lambda pair: (pair[1], pair[0]))[0]
        latest_members = member_records
        employees = (
            self._merge_team_members(latest_members, employees_by_identity)
            if latest_members
            else list(employees_by_identity.values())
        )
        employees.sort(key=lambda item: (-item["totalTokens"], -item["spend"], str(item["employeeName"]).casefold()))
        summary_rows = self._group_rows(rows, ("date", "source", "model"))
        anchor = team_scopes[0]
        team_name = anchor.get("name") or (member_records[0]["team_name"] if member_records else "") or anchor["id"]
        return {"rows": self._public_rows(rows), "summaryRows": summary_rows, "employees": employees, "team": {"id": anchor["id"], "name": team_name, "memberCount": len(employees), "backend": anchor["backend"]}, "pageLimit": 0, "pageSize": 0, "pagesRead": 0, "totalPages": 0, "totalRecords": len(rows), "truncated": False, "dataQuality": {"summarySource": "database", "rankingSource": "database", "backends": covered, "scopeCount": len(team_scopes), "memberIdentityMatch": "normalized_email_or_backend_user_id", "teamAttribution": "usage_event_team_id", "memberDirectory": "latest_snapshot_on_or_before_end_date"}, "lastSyncedAt": await self.latest_sync_at(start_date, end_date, covered)}

    @staticmethod
    def _usage_record(backend_id: str, row: dict[str, Any], collected_at: datetime) -> tuple[Any, ...]:
        user_id = _clean_text(row.get("_userId") or row.get("userId")) or "unknown"
        (
            organization_id,
            team_id,
            key_id,
            principal_id,
            attribution_source,
            billing_eligible,
        ) = _usage_attribution(row)
        return (
            backend_id,
            _as_date(row.get("date")),
            user_id,
            _clean_text(row.get("employeeEmail") or row.get("employee_email")),
            _clean_text(row.get("employeeName") or row.get("employee_name")),
            _clean_text(row.get("source")) or "其他",
            normalize_model_display_name(row.get("model")) or "未知模型",
            _as_int(row.get("promptTokens")),
            _as_int(row.get("completionTokens")),
            _as_int(row.get("totalTokens")),
            _as_int(row.get("requestCount")),
            _as_int(row.get("successCount")),
            _as_int(row.get("failureCount")),
            _as_float(row.get("spend")),
            collected_at,
            organization_id,
            team_id,
            key_id,
            principal_id,
            attribution_source,
            billing_eligible,
            _clean_text(row.get("emailSource") or row.get("email_source")),
        )

    @staticmethod
    def _coalesce_usage_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, ...], dict[str, Any]] = {}
        for row in rows:
            model = normalize_model_display_name(row.get("model")) or "未知模型"
            (
                organization_id,
                team_id,
                key_id,
                principal_id,
                attribution_source,
                billing_eligible,
            ) = _usage_attribution(row)
            key = (
                _clean_text(row.get("date")),
                _clean_text(row.get("_userId") or row.get("userId")) or "unknown",
                _clean_text(row.get("source")) or "其他",
                model,
                organization_id,
                team_id,
                key_id,
                principal_id,
                attribution_source,
                str(billing_eligible),
            )
            current = grouped.get(key)
            if current is None:
                current = dict(row)
                current["model"] = model
                current.update(empty_totals())
                grouped[key] = current
            add_totals(current, row)
            if not current.get("employeeEmail") and row.get("employeeEmail"):
                current["employeeEmail"] = row["employeeEmail"]
            if not current.get("employeeName") and row.get("employeeName"):
                current["employeeName"] = row["employeeName"]
            if not current.get("emailSource") and row.get("emailSource"):
                current["emailSource"] = row["emailSource"]
            for field, value in (
                ("organizationId", organization_id),
                ("teamId", team_id),
                ("keyId", key_id),
                ("principalId", principal_id),
                ("attributionSource", attribution_source),
                ("billingEligible", billing_eligible),
            ):
                if value and not _clean_text(current.get(field)):
                    current[field] = value
        return list(grouped.values())

    @staticmethod
    def _membership_record(backend_id: str, row: dict[str, Any]) -> tuple[Any, ...]:
        return (
            backend_id,
            _as_date(row.get("snapshotDate")),
            _clean_text(row.get("teamId")),
            _clean_text(row.get("teamName")),
            _clean_text(row.get("userId")) or "unknown",
            _clean_text(row.get("employeeEmail")),
            _clean_text(row.get("employeeName")),
            _clean_text(row.get("teamRole")) or "user",
        )

    @staticmethod
    def _event_record(
        backend_id: str, row: dict[str, Any], collected_at: datetime
    ) -> tuple[Any, ...] | None:
        event_time_text = _clean_text(
            row.get("eventTime") or row.get("event_time")
        )
        if not event_time_text:
            return None
        try:
            event_time = datetime.fromisoformat(
                event_time_text.replace("Z", "+00:00")
            )
        except ValueError:
            return None
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=timezone.utc)
        event_time = event_time.astimezone(timezone.utc)
        (
            organization_id,
            team_id,
            key_id,
            principal_id,
            attribution_source,
            billing_eligible,
        ) = _usage_attribution(row)
        raw_user_id = _clean_text(row.get("_userId") or row.get("userId"))
        request_id = _clean_text(row.get("requestId") or row.get("request_id"))
        if not request_id:
            request_id = hashlib.sha256(
                "\x1f".join(
                    (
                        event_time.isoformat(),
                        raw_user_id,
                        key_id,
                        _clean_text(row.get("model")),
                        str(_as_float(row.get("spend"))),
                        str(_as_int(row.get("totalTokens"))),
                    )
                ).encode("utf-8")
            ).hexdigest()
        from .observability import normalize_event, _number
        observed = normalize_event(row)
        provider = _clean_text(row.get("provider") or row.get("custom_llm_provider") or row.get("llmProvider") or row.get("llm_provider"))
        model_group = _clean_text(row.get("modelGroup") or row.get("model_group"))
        model_id = _clean_text(row.get("modelId") or row.get("model_id"))
        api_base = _clean_text(row.get("apiBase") or row.get("api_base"))
        start = row.get("startTime") or row.get("start_time")
        end = row.get("endTime") or row.get("end_time") or row.get("completionEndTime")
        request_duration_ms = _number(row.get("requestDurationMs") or row.get("request_duration_ms")) or None
        try:
            if request_duration_ms is None and start is not None and end is not None:
                request_duration_ms = max(0.0, (datetime.fromisoformat(str(end).replace("Z", "+00:00")) - datetime.fromisoformat(str(start).replace("Z", "+00:00"))).total_seconds() * 1000)
        except (TypeError, ValueError):
            request_duration_ms = None
        return (
            backend_id,
            # Some upstream request ids embed nested trace material and can be
            # longer than 256 characters. Truncation makes distinct requests
            # collide in the event primary key, so preserve the full stable id.
            request_id,
            event_time,
            _as_date(row.get("date") or event_time.date()),
            raw_user_id[:256],
            organization_id[:256],
            team_id[:256],
            key_id[:256],
            principal_id[:256],
            _clean_text(row.get("source"))[:120],
            (normalize_model_display_name(row.get("model")) or "未知模型")[:256],
            _as_int(row.get("promptTokens")),
            _as_int(row.get("completionTokens")),
            _as_int(row.get("totalTokens")),
            _as_int(row.get("requestCount")),
            _as_int(row.get("successCount")),
            _as_int(row.get("failureCount")),
            str(_as_float(row.get("spend"))),
            attribution_source[:64],
            billing_eligible,
            provider[:160] or None,
            model_group[:256] or None,
            model_id[:256] or None,
            api_base[:512] or None,
            observed["status"],
            observed["errorCode"],
            observed["errorClass"],
            observed["errorMessage"],
            observed["scenario"],
            request_duration_ms,
            observed["ttftMs"],
            observed["attemptedRetries"],
            observed["maxRetries"],
            observed["traceId"],
            observed["userVisibleFailure"],
            observed["finalRequestFailureSource"],
            collected_at,
        )

    async def replace_backend_snapshot(
        self,
        backend_id: str,
        start_date: str,
        end_date: str,
        rows: list[dict[str, Any]],
        memberships: list[dict[str, Any]],
        *,
        events: list[dict[str, Any]] | None = None,
        departments: list[dict[str, Any]] | None = None,
    ) -> int:
        pool = self._require_pool()
        collected_at = datetime.now(timezone.utc)
        usage_records = [
            self._usage_record(backend_id, row, collected_at)
            for row in self._coalesce_usage_rows(rows)
            if row.get("date")
        ]
        membership_records = [self._membership_record(backend_id, row) for row in memberships if row.get("snapshotDate") and row.get("teamId")]
        event_records = [
            record
            for row in (events or [])
            if (record := self._event_record(backend_id, row, collected_at))
        ]
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM usage_daily WHERE backend_id = $1 "
                    "AND usage_date BETWEEN $2::date AND $3::date "
                    "AND attribution_source <> 'legacy_report_only'",
                    backend_id,
                    _as_date(start_date),
                    _as_date(end_date),
                )
                await connection.execute(
                    "DELETE FROM usage_team_membership_daily WHERE backend_id = $1 AND snapshot_date BETWEEN $2::date AND $3::date",
                    backend_id,
                    _as_date(start_date),
                    _as_date(end_date),
                )
                if departments is not None:
                    await connection.execute(
                        "DELETE FROM usage_department_directory WHERE backend_id=$1",
                        backend_id,
                    )
                    if departments:
                        await connection.executemany(
                            """
                            INSERT INTO usage_department_directory
                                (backend_id, department_id, department_name, organization_id, status, synced_at)
                            VALUES ($1,$2,$3,$4,$5,$6)
                            ON CONFLICT (backend_id, department_id) DO UPDATE SET
                                department_name=EXCLUDED.department_name,
                                organization_id=EXCLUDED.organization_id,
                                status=EXCLUDED.status,
                                synced_at=EXCLUDED.synced_at
                            """,
                            [
                                (backend_id, str(item.get("departmentId") or ""), str(item.get("departmentName") or ""),
                                 str(item.get("organizationId") or ""), str(item.get("status") or "active"), collected_at)
                                for item in departments if str(item.get("departmentId") or "")
                            ],
                        )
                await connection.execute(
                    "DELETE FROM usage_sync_coverage WHERE backend_id = $1 AND usage_date BETWEEN $2::date AND $3::date",
                    backend_id,
                    _as_date(start_date),
                    _as_date(end_date),
                )
                if events is not None:
                    await connection.execute(
                        "DELETE FROM usage_event_attribution WHERE backend_id=$1 "
                        "AND usage_date BETWEEN $2::date AND $3::date "
                        "AND attribution_source <> 'legacy_report_only'",
                        backend_id,
                        _as_date(start_date),
                        _as_date(end_date),
                    )
                if usage_records:
                    await connection.executemany(
                        """
                        INSERT INTO usage_daily (
                            backend_id, usage_date, user_id, employee_email, employee_name,
                            source, model, prompt_tokens, completion_tokens, total_tokens,
                            request_count, success_count, failure_count, spend, collected_at,
                            organization_id, team_id, key_id, principal_id,
                            attribution_source, billing_eligible, email_source
                        ) VALUES ($1, $2::date, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22)
                        ON CONFLICT (
                            backend_id, usage_date, user_id, source, model,
                            organization_id, team_id, key_id, principal_id,
                            attribution_source, billing_eligible
                        ) DO UPDATE SET
                            organization_id = EXCLUDED.organization_id,
                            team_id = EXCLUDED.team_id,
                            key_id = EXCLUDED.key_id,
                            principal_id = EXCLUDED.principal_id,
                            attribution_source = EXCLUDED.attribution_source,
                            billing_eligible = EXCLUDED.billing_eligible,
                            employee_email = EXCLUDED.employee_email,
                            employee_name = EXCLUDED.employee_name,
                            email_source = EXCLUDED.email_source,
                            prompt_tokens = EXCLUDED.prompt_tokens,
                            completion_tokens = EXCLUDED.completion_tokens,
                            total_tokens = EXCLUDED.total_tokens,
                            request_count = EXCLUDED.request_count,
                            success_count = EXCLUDED.success_count,
                            failure_count = EXCLUDED.failure_count,
                            spend = EXCLUDED.spend,
                            collected_at = EXCLUDED.collected_at
                        """,
                        usage_records,
                    )
                if membership_records:
                    await connection.executemany(
                        """
                        INSERT INTO usage_team_membership_daily (
                            backend_id, snapshot_date, team_id, team_name, user_id,
                            employee_email, employee_name, team_role
                        ) VALUES ($1, $2::date, $3, $4, $5, $6, $7, $8)
                        ON CONFLICT (backend_id, snapshot_date, team_id, user_id) DO UPDATE SET
                            team_name = EXCLUDED.team_name,
                            employee_email = EXCLUDED.employee_email,
                            employee_name = EXCLUDED.employee_name,
                            team_role = EXCLUDED.team_role
                        """,
                        membership_records,
                    )
                if event_records:
                    await connection.executemany(
                        """
                        INSERT INTO usage_event_attribution (
                            backend_id, request_id, event_time, usage_date,
                            raw_user_id, organization_id, team_id, key_id,
                            principal_id, source, model, prompt_tokens,
                            completion_tokens, total_tokens, request_count,
                            success_count, failure_count, spend,
                            attribution_source, billing_eligible, provider, model_group, model_id, api_base,
                            status, error_code, error_class, error_message, scenario, request_duration_ms,
                            ttft_ms, attempted_retries, max_retries, trace_id, user_visible_failure, final_failure_source, collected_at
                        ) VALUES (
                            $1,$2,$3,$4::date,$5,$6,$7,$8,$9,$10,$11,$12,$13,
                            $14,$15,$16,$17,$18::numeric,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29,$30,$31,$32,$33,$34,$35,$36,$37
                        )
                        ON CONFLICT (backend_id, request_id) DO UPDATE SET
                            event_time=EXCLUDED.event_time,
                            usage_date=EXCLUDED.usage_date,
                            raw_user_id=EXCLUDED.raw_user_id,
                            organization_id=EXCLUDED.organization_id,
                            team_id=EXCLUDED.team_id,
                            key_id=EXCLUDED.key_id,
                            principal_id=EXCLUDED.principal_id,
                            source=EXCLUDED.source,
                            model=EXCLUDED.model,
                            prompt_tokens=EXCLUDED.prompt_tokens,
                            completion_tokens=EXCLUDED.completion_tokens,
                            total_tokens=EXCLUDED.total_tokens,
                            request_count=EXCLUDED.request_count,
                            success_count=EXCLUDED.success_count,
                            failure_count=EXCLUDED.failure_count,
                            spend=EXCLUDED.spend,
                            attribution_source=EXCLUDED.attribution_source,
                            billing_eligible=EXCLUDED.billing_eligible,
                            provider=EXCLUDED.provider,
                            model_group=EXCLUDED.model_group,
                            model_id=EXCLUDED.model_id,
                            api_base=EXCLUDED.api_base,
                            status=EXCLUDED.status,
                            error_code=EXCLUDED.error_code,
                            error_class=EXCLUDED.error_class,
                            error_message=EXCLUDED.error_message,
                            scenario=EXCLUDED.scenario,
                            request_duration_ms=EXCLUDED.request_duration_ms,
                            ttft_ms=EXCLUDED.ttft_ms,
                            attempted_retries=EXCLUDED.attempted_retries,
                            max_retries=EXCLUDED.max_retries,
                            trace_id=EXCLUDED.trace_id,
                            user_visible_failure=EXCLUDED.user_visible_failure,
                            final_failure_source=EXCLUDED.final_failure_source,
                            collected_at=EXCLUDED.collected_at
                        """,
                        event_records,
                    )
                await connection.execute(
                    """
                    INSERT INTO usage_sync_coverage (backend_id, usage_date, synced_at)
                    SELECT $1, day::date, $4
                    FROM generate_series($2::date, $3::date, interval '1 day') AS day
                    ON CONFLICT (backend_id, usage_date) DO UPDATE SET synced_at = EXCLUDED.synced_at
                    """,
                    backend_id,
                    _as_date(start_date),
                    _as_date(end_date),
                    collected_at,
                )
        return len(usage_records)

    async def publish_stability_events(
        self,
        backend_id: str,
        replace_start_date: str,
        replace_end_date: str,
        events: list[dict[str, Any]],
        window_start: str,
        window_end: str,
        complete: bool,
    ) -> int:
        """Publish one bounded stability slice without waiting for usage aggregation."""

        pool = self._require_pool()
        collected_at = datetime.now(timezone.utc)
        records = [
            record
            for row in events
            if (record := self._event_record(backend_id, row, collected_at)) is not None
        ]
        # 同批 spend-log 事件也补写为 final_request 尝试事件：让「上游异常率」
        # 「重试恢复率」在外部 attempt 推送尚未接入时也能从原始日志推导，兜底
        # 恢复率仍只依赖推送方（spend log 不含 fallback 过程信息）。
        final_attempt_records = [
            record
            for row in events
            if (record := self._stability_final_request_record(backend_id, row, collected_at)) is not None
        ]
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM usage_event_attribution WHERE backend_id=$1 "
                    "AND usage_date BETWEEN $2::date AND $3::date "
                    "AND attribution_source <> 'legacy_report_only'",
                    backend_id,
                    _as_date(replace_start_date),
                    _as_date(replace_end_date),
                )
                if records:
                    await connection.executemany(
                        """
                        INSERT INTO usage_event_attribution (
                            backend_id, request_id, event_time, usage_date, raw_user_id,
                            organization_id, team_id, key_id, principal_id, source, model,
                            prompt_tokens, completion_tokens, total_tokens, request_count,
                            success_count, failure_count, spend, attribution_source,
                            billing_eligible, provider, model_group, model_id, api_base,
                            status, error_code, error_class, error_message, scenario,
                            request_duration_ms, ttft_ms, attempted_retries, max_retries,
                            trace_id, user_visible_failure, final_failure_source, collected_at
                        ) VALUES (
                            $1,$2,$3,$4::date,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                            $17,$18::numeric,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29,$30,
                            $31,$32,$33,$34,$35,$36,$37
                        )
                        ON CONFLICT (backend_id, request_id) DO UPDATE SET
                            event_time=EXCLUDED.event_time, usage_date=EXCLUDED.usage_date,
                            raw_user_id=EXCLUDED.raw_user_id, organization_id=EXCLUDED.organization_id,
                            team_id=EXCLUDED.team_id, key_id=EXCLUDED.key_id,
                            principal_id=EXCLUDED.principal_id, source=EXCLUDED.source,
                            model=EXCLUDED.model, prompt_tokens=EXCLUDED.prompt_tokens,
                            completion_tokens=EXCLUDED.completion_tokens,
                            total_tokens=EXCLUDED.total_tokens, request_count=EXCLUDED.request_count,
                            success_count=EXCLUDED.success_count, failure_count=EXCLUDED.failure_count,
                            spend=EXCLUDED.spend, attribution_source=EXCLUDED.attribution_source,
                            billing_eligible=EXCLUDED.billing_eligible, provider=EXCLUDED.provider,
                            model_group=EXCLUDED.model_group, model_id=EXCLUDED.model_id,
                            api_base=EXCLUDED.api_base, status=EXCLUDED.status,
                            error_code=EXCLUDED.error_code, error_class=EXCLUDED.error_class,
                            error_message=EXCLUDED.error_message, scenario=EXCLUDED.scenario,
                            request_duration_ms=EXCLUDED.request_duration_ms,
                            ttft_ms=EXCLUDED.ttft_ms, attempted_retries=EXCLUDED.attempted_retries,
                            max_retries=EXCLUDED.max_retries, trace_id=EXCLUDED.trace_id,
                            user_visible_failure=EXCLUDED.user_visible_failure,
                            final_failure_source=EXCLUDED.final_failure_source,
                            collected_at=EXCLUDED.collected_at
                        """,
                        records,
                    )
                # 窗口内由 spend log 生成的 final_request 尝试事件整体替换；
                # event_type='final_request' 过滤保证外部推送的 attempt 事件不被清掉。
                await connection.execute(
                    "DELETE FROM stability_attempt_events WHERE backend_id=$1 "
                    "AND event_date BETWEEN $2::date AND $3::date "
                    "AND event_type='final_request'",
                    backend_id,
                    _as_date(replace_start_date),
                    _as_date(replace_end_date),
                )
                if final_attempt_records:
                    await connection.executemany(
                        """
                        INSERT INTO stability_attempt_events (
                            backend_id, event_id, request_id, trace_id, attempt_id,
                            attempt_index, retry_index, requested_model_group,
                            actual_model, route_name, provider, event_type, status,
                            error_code, error_class, error_category, error_message, scenario,
                            scenario_version, event_time, event_date, started_at,
                            ended_at, ttft_ms, duration_ms, fallback_from, fallback_to,
                            is_retry, is_fallback, collected_at, received_at
                        ) VALUES (
                            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                            $16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29,$30,$31
                        ) ON CONFLICT (backend_id, event_id) DO NOTHING
                        """,
                        final_attempt_records,
                    )
                event_count = int(
                    await connection.fetchval(
                        "SELECT COUNT(*) FROM usage_event_attribution WHERE backend_id=$1 "
                        "AND usage_date BETWEEN $2::date AND $3::date",
                        backend_id,
                        _as_date(window_start),
                        _as_date(window_end),
                    )
                    or 0
                )
                await connection.execute(
                    """
                    INSERT INTO stability_sync_state (
                        backend_id, window_start, window_end, status, partial,
                        event_count, synced_at, error_message
                    ) VALUES ($1,$2::date,$3::date,$4,$5,$6,$7,$8)
                    ON CONFLICT (backend_id) DO UPDATE SET
                        window_start=EXCLUDED.window_start, window_end=EXCLUDED.window_end,
                        status=EXCLUDED.status, partial=EXCLUDED.partial,
                        event_count=EXCLUDED.event_count, synced_at=EXCLUDED.synced_at,
                        error_message=EXCLUDED.error_message
                    """,
                    backend_id,
                    _as_date(window_start),
                    _as_date(window_end),
                    "complete" if complete else "partial",
                    not complete,
                    event_count,
                    collected_at,
                    "" if complete else "page limit or upstream scan incomplete",
                )
        return len(records)

    async def replace_membership_snapshot(
        self, backend_id: str, start_date: str, end_date: str,
        memberships: list[dict[str, Any]],
    ) -> int:
        """Replace only recent team memberships without rewriting usage data."""
        records = [
            self._membership_record(backend_id, row)
            for row in memberships
            if row.get("snapshotDate") and row.get("teamId")
        ]
        async with self._require_pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM usage_team_membership_daily WHERE backend_id=$1 AND snapshot_date BETWEEN $2::date AND $3::date",
                    backend_id, _as_date(start_date), _as_date(end_date),
                )
                if records:
                    await connection.executemany(
                        """
                        INSERT INTO usage_team_membership_daily
                            (backend_id, snapshot_date, team_id, team_name, user_id, employee_email, employee_name, team_role)
                        VALUES ($1,$2::date,$3,$4,$5,$6,$7,$8)
                        ON CONFLICT (backend_id, snapshot_date, team_id, user_id) DO UPDATE SET
                            team_name=EXCLUDED.team_name, employee_email=EXCLUDED.employee_email,
                            employee_name=EXCLUDED.employee_name, team_role=EXCLUDED.team_role
                        """,
                        records,
                    )
        return len(records)

    async def publish_snapshots(
        self,
        start_date: str,
        end_date: str,
        snapshots: list[Any],
    ) -> dict[str, Any]:
        """COPY a complete multi-backend snapshot, then publish it atomically."""

        if not snapshots:
            return {"rowCount": 0, "snapshotRevision": None}
        # Normalize at the database boundary so DATE parameters are native date values.
        start_day = _as_date(start_date)
        end_day = _as_date(end_date)
        pool = self._require_pool()
        collected_at = datetime.now(timezone.utc)
        backend_ids = sorted({str(snapshot.backend_id) for snapshot in snapshots})
        usage_records = [
            self._usage_record(str(snapshot.backend_id), row, collected_at)
            for snapshot in snapshots
            for row in self._coalesce_usage_rows(list(snapshot.rows or []))
            if row.get("date")
        ]
        membership_records_by_key: dict[tuple[Any, ...], tuple[Any, ...]] = {}
        for snapshot in snapshots:
            for row in list(snapshot.memberships or []):
                if not row.get("snapshotDate") or not row.get("teamId"):
                    continue
                record = self._membership_record(str(snapshot.backend_id), row)
                membership_records_by_key[(record[0], record[1], record[2], record[4])] = record
        membership_records = list(membership_records_by_key.values())
        event_records_by_key: dict[tuple[str, str], tuple[Any, ...]] = {}
        for snapshot in snapshots:
            for row in list(getattr(snapshot, "events", None) or []):
                record = self._event_record(str(snapshot.backend_id), row, collected_at)
                if record is not None:
                    event_records_by_key[(str(record[0]), str(record[1]))] = record
        event_records = list(event_records_by_key.values())
        event_windows = {
            str(snapshot.backend_id): (
                str(getattr(snapshot, "event_replace_start_date", None) or getattr(snapshot, "event_start_date", None) or start_date),
                str(getattr(snapshot, "event_replace_end_date", None) or getattr(snapshot, "event_end_date", None) or end_date),
            )
            for snapshot in snapshots
            if getattr(snapshot, "events", None) is not None
        }
        department_records_by_key: dict[tuple[str, str], tuple[Any, ...]] = {}
        for snapshot in snapshots:
            for item in list(getattr(snapshot, "departments", None) or []):
                department_id = _clean_text(item.get("departmentId"))
                if not department_id:
                    continue
                record = (
                    str(snapshot.backend_id),
                    department_id,
                    _clean_text(item.get("departmentName")),
                    _clean_text(item.get("organizationId")),
                    _clean_text(item.get("status")) or "active",
                    collected_at,
                )
                department_records_by_key[(record[0], record[1])] = record
        department_records = list(department_records_by_key.values())
        event_backends = sorted(
            str(snapshot.backend_id)
            for snapshot in snapshots
            if getattr(snapshot, "events", None) is not None
        )
        department_backends = sorted(
            str(snapshot.backend_id)
            for snapshot in snapshots
            if getattr(snapshot, "departments", None) is not None
        )
        suffix = uuid.uuid4().hex
        usage_stage = f"usage_daily_stage_{suffix}"
        membership_stage = f"usage_membership_stage_{suffix}"
        event_stage = f"usage_event_stage_{suffix}"
        department_stage = f"usage_department_stage_{suffix}"
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    f"CREATE TEMP TABLE {usage_stage} (LIKE usage_daily INCLUDING DEFAULTS) ON COMMIT DROP"
                )
                await connection.execute(
                    f"CREATE TEMP TABLE {membership_stage} (LIKE usage_team_membership_daily INCLUDING DEFAULTS) ON COMMIT DROP"
                )
                await connection.execute(
                    f"CREATE TEMP TABLE {event_stage} (LIKE usage_event_attribution INCLUDING DEFAULTS) ON COMMIT DROP"
                )
                await connection.execute(
                    f"CREATE TEMP TABLE {department_stage} (LIKE usage_department_directory INCLUDING DEFAULTS) ON COMMIT DROP"
                )
                if usage_records:
                    await connection.copy_records_to_table(
                        usage_stage,
                        records=usage_records,
                        columns=(
                            "backend_id", "usage_date", "user_id", "employee_email",
                            "employee_name", "source", "model", "prompt_tokens",
                            "completion_tokens", "total_tokens", "request_count",
                            "success_count", "failure_count", "spend", "collected_at",
                            "organization_id", "team_id", "key_id", "principal_id",
                            "attribution_source", "billing_eligible", "email_source",
                        ),
                    )
                if membership_records:
                    await connection.copy_records_to_table(
                        membership_stage,
                        records=membership_records,
                        columns=(
                            "backend_id", "snapshot_date", "team_id", "team_name",
                            "user_id", "employee_email", "employee_name", "team_role",
                        ),
                    )
                if event_records:
                    await connection.copy_records_to_table(
                        event_stage,
                        records=event_records,
                        columns=(
                            "backend_id", "request_id", "event_time", "usage_date",
                            "raw_user_id", "organization_id", "team_id", "key_id",
                            "principal_id", "source", "model", "prompt_tokens",
                            "completion_tokens", "total_tokens", "request_count",
                            "success_count", "failure_count", "spend",
                            "attribution_source", "billing_eligible", "provider",
                            "model_group", "model_id", "api_base", "status",
                            "error_code", "error_class", "error_message", "scenario",
                            "request_duration_ms", "ttft_ms", "attempted_retries",
                            "max_retries", "trace_id", "user_visible_failure", "final_failure_source", "collected_at",
                        ),
                    )
                if department_records:
                    await connection.copy_records_to_table(
                        department_stage,
                        records=department_records,
                        columns=(
                            "backend_id", "department_id", "department_name",
                            "organization_id", "status", "synced_at",
                        ),
                    )
                await connection.execute(
                    "DELETE FROM usage_daily WHERE backend_id=ANY($1::text[]) "
                    "AND usage_date BETWEEN $2::date AND $3::date "
                    "AND attribution_source <> 'legacy_report_only'",
                    backend_ids,
                    start_day,
                    end_day,
                )
                await connection.execute(
                    "DELETE FROM usage_team_membership_daily WHERE backend_id=ANY($1::text[]) "
                    "AND snapshot_date BETWEEN $2::date AND $3::date",
                    backend_ids,
                    start_day,
                    end_day,
                )
                await connection.execute(
                    "DELETE FROM usage_sync_coverage WHERE backend_id=ANY($1::text[]) "
                    "AND usage_date BETWEEN $2::date AND $3::date",
                    backend_ids,
                    start_day,
                    end_day,
                )
                if event_backends:
                    for event_backend, (event_start, event_end) in event_windows.items():
                        await connection.execute(
                            "DELETE FROM usage_event_attribution WHERE backend_id=$1 "
                            "AND usage_date BETWEEN $2::date AND $3::date "
                            "AND attribution_source <> 'legacy_report_only'",
                            event_backend,
                            _as_date(event_start),
                            _as_date(event_end),
                        )
                if department_backends:
                    await connection.execute(
                        "DELETE FROM usage_department_directory WHERE backend_id=ANY($1::text[])",
                        department_backends,
                    )
                # Report-only imports are retained during the replacement. When
                # their key overlaps this complete snapshot, the newly collected
                # aggregate is authoritative and must replace the older row.
                stage_merges = (
                    (
                        "usage_daily",
                        f"""INSERT INTO usage_daily SELECT * FROM {usage_stage}
                        ON CONFLICT (backend_id, usage_date, user_id, source, model,
                            organization_id, team_id, key_id, principal_id,
                            attribution_source, billing_eligible) DO UPDATE SET
                            employee_email=EXCLUDED.employee_email,
                            employee_name=EXCLUDED.employee_name,
                            prompt_tokens=EXCLUDED.prompt_tokens,
                            completion_tokens=EXCLUDED.completion_tokens,
                            total_tokens=EXCLUDED.total_tokens,
                            request_count=EXCLUDED.request_count,
                            success_count=EXCLUDED.success_count,
                            failure_count=EXCLUDED.failure_count,
                            spend=EXCLUDED.spend,
                            collected_at=EXCLUDED.collected_at,
                            email_source=EXCLUDED.email_source""",
                    ),
                    (
                        "usage_team_membership_daily",
                        f"""INSERT INTO usage_team_membership_daily SELECT * FROM {membership_stage}
                        ON CONFLICT (backend_id, snapshot_date, team_id, user_id) DO UPDATE SET
                            team_name=EXCLUDED.team_name,
                            employee_email=EXCLUDED.employee_email,
                            employee_name=EXCLUDED.employee_name,
                            team_role=EXCLUDED.team_role""",
                    ),
                    (
                        "usage_event_attribution",
                        f"""INSERT INTO usage_event_attribution SELECT * FROM {event_stage}
                        ON CONFLICT (backend_id, request_id) DO UPDATE SET
                            event_time=EXCLUDED.event_time,
                            usage_date=EXCLUDED.usage_date,
                            raw_user_id=EXCLUDED.raw_user_id,
                            organization_id=EXCLUDED.organization_id,
                            team_id=EXCLUDED.team_id,
                            key_id=EXCLUDED.key_id,
                            principal_id=EXCLUDED.principal_id,
                            source=EXCLUDED.source,
                            model=EXCLUDED.model,
                            prompt_tokens=EXCLUDED.prompt_tokens,
                            completion_tokens=EXCLUDED.completion_tokens,
                            total_tokens=EXCLUDED.total_tokens,
                            request_count=EXCLUDED.request_count,
                            success_count=EXCLUDED.success_count,
                            failure_count=EXCLUDED.failure_count,
                            spend=EXCLUDED.spend,
                            attribution_source=EXCLUDED.attribution_source,
                            billing_eligible=EXCLUDED.billing_eligible,
                            provider=EXCLUDED.provider,
                            model_group=EXCLUDED.model_group,
                            model_id=EXCLUDED.model_id,
                            api_base=EXCLUDED.api_base,
                            status=EXCLUDED.status,
                            error_code=EXCLUDED.error_code,
                            error_class=EXCLUDED.error_class,
                            error_message=EXCLUDED.error_message,
                            scenario=EXCLUDED.scenario,
                            request_duration_ms=EXCLUDED.request_duration_ms,
                            ttft_ms=EXCLUDED.ttft_ms,
                            attempted_retries=EXCLUDED.attempted_retries,
                            max_retries=EXCLUDED.max_retries,
                            trace_id=EXCLUDED.trace_id,
                            user_visible_failure=EXCLUDED.user_visible_failure,
                            final_failure_source=EXCLUDED.final_failure_source,
                            collected_at=EXCLUDED.collected_at""",
                    ),
                    (
                        "usage_department_directory",
                        f"""INSERT INTO usage_department_directory SELECT * FROM {department_stage}
                        ON CONFLICT (backend_id, department_id) DO UPDATE SET
                            department_name=EXCLUDED.department_name,
                            organization_id=EXCLUDED.organization_id,
                            status=EXCLUDED.status,
                            synced_at=EXCLUDED.synced_at""",
                    ),
                )
                for target, query in stage_merges:
                    try:
                        await connection.execute(query)
                    except Exception:
                        logger.exception(
                            "usage snapshot publish stage merge failed target=%s start=%s end=%s",
                            target,
                            start_date,
                            end_date,
                        )
                        raise
                for snapshot in snapshots:
                    if getattr(snapshot, "events_complete", None) is None:
                        continue
                    state_complete = bool(
                        getattr(snapshot, "event_window_complete", None)
                        if getattr(snapshot, "event_window_complete", None) is not None
                        else getattr(snapshot, "events_complete", False)
                    )
                    state_start = getattr(snapshot, "event_start_date", None) or start_day
                    state_end = getattr(snapshot, "event_end_date", None) or end_day
                    event_count = int(
                        await connection.fetchval(
                            "SELECT COUNT(*) FROM usage_event_attribution "
                            "WHERE backend_id=$1 AND usage_date BETWEEN $2::date AND $3::date",
                            str(snapshot.backend_id),
                            _as_date(state_start),
                            _as_date(state_end),
                        )
                        or 0
                    )
                    await connection.execute(
                        """
                        INSERT INTO stability_sync_state (
                            backend_id, window_start, window_end, status, partial,
                            event_count, synced_at, error_message
                        ) VALUES ($1,$2::date,$3::date,$4,$5,$6,$7,$8)
                        ON CONFLICT (backend_id) DO UPDATE SET
                            window_start=EXCLUDED.window_start,
                            window_end=EXCLUDED.window_end,
                            status=EXCLUDED.status,
                            partial=EXCLUDED.partial,
                            event_count=EXCLUDED.event_count,
                            synced_at=EXCLUDED.synced_at,
                            error_message=EXCLUDED.error_message
                        """,
                        str(snapshot.backend_id),
                        getattr(snapshot, "event_start_date", None) or state_start,
                        getattr(snapshot, "event_end_date", None) or state_end,
                        "complete" if state_complete else "partial",
                        not state_complete,
                        event_count,
                        collected_at,
                        "" if state_complete else "page limit or upstream scan incomplete",
                    )
                await connection.execute(
                    """
                    INSERT INTO usage_sync_coverage (backend_id, usage_date, synced_at)
                    SELECT backend_id, day::date, $4
                    FROM unnest($1::text[]) AS backend_id
                    CROSS JOIN generate_series($2::date, $3::date, interval '1 day') AS day
                    """,
                    backend_ids,
                    start_day,
                    end_day,
                    collected_at,
                )
                revision = await connection.fetchval(
                    """
                    SELECT MIN(synced_at)::text
                    FROM usage_sync_coverage
                    WHERE usage_date=$1::date AND backend_id=ANY($2::text[])
                    HAVING COUNT(DISTINCT backend_id)=cardinality($2::text[])
                    """,
                    end_day,
                    backend_ids,
                )
                await connection.execute(
                    """
                    UPDATE usage_snapshot_state
                    SET revision=$1, published_at=$2, start_date=$3::date,
                        end_date=$4::date, backend_ids=$5::text[]
                    WHERE singleton=TRUE
                    """,
                    str(revision or ""),
                    collected_at,
                    start_day,
                    end_day,
                    backend_ids,
                )
        return {
            "rowCount": len(usage_records),
            "snapshotRevision": str(revision or ""),
            "publishedAt": collected_at,
        }

    async def upsert_attributed_usage(
        self,
        backend_id: str,
        rows: list[dict[str, Any]],
        *,
        events: list[dict[str, Any]],
    ) -> int:
        """Merge a key-scoped historical import without replacing other users."""

        pool = self._require_pool()
        collected_at = datetime.now(timezone.utc)
        usage_records = [
            self._usage_record(backend_id, row, collected_at)
            for row in self._coalesce_usage_rows(rows)
            if row.get("date")
        ]
        event_records = [
            record
            for row in events
            if (record := self._event_record(backend_id, row, collected_at))
        ]
        async with pool.acquire() as connection:
            async with connection.transaction():
                if usage_records:
                    await connection.executemany(
                        """
                        INSERT INTO usage_daily (
                            backend_id, usage_date, user_id, employee_email, employee_name,
                            source, model, prompt_tokens, completion_tokens, total_tokens,
                            request_count, success_count, failure_count, spend, collected_at,
                            organization_id, team_id, key_id, principal_id,
                            attribution_source, billing_eligible, email_source
                        ) VALUES ($1,$2::date,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22)
                        ON CONFLICT (
                            backend_id, usage_date, user_id, source, model,
                            organization_id, team_id, key_id, principal_id,
                            attribution_source, billing_eligible
                        ) DO UPDATE SET
                            employee_email=EXCLUDED.employee_email,
                            employee_name=EXCLUDED.employee_name,
                            email_source=EXCLUDED.email_source,
                            prompt_tokens=EXCLUDED.prompt_tokens,
                            completion_tokens=EXCLUDED.completion_tokens,
                            total_tokens=EXCLUDED.total_tokens,
                            request_count=EXCLUDED.request_count,
                            success_count=EXCLUDED.success_count,
                            failure_count=EXCLUDED.failure_count,
                            spend=EXCLUDED.spend,
                            collected_at=EXCLUDED.collected_at
                        """,
                        usage_records,
                    )
                if event_records:
                    await connection.executemany(
                        """
                        INSERT INTO usage_event_attribution (
                            backend_id, request_id, event_time, usage_date,
                            raw_user_id, organization_id, team_id, key_id,
                            principal_id, source, model, prompt_tokens,
                            completion_tokens, total_tokens, request_count,
                            success_count, failure_count, spend,
                            attribution_source, billing_eligible, provider, model_group, model_id, api_base,
                            status, error_code, error_class, error_message, scenario, request_duration_ms,
                            ttft_ms, attempted_retries, max_retries, trace_id, user_visible_failure, final_failure_source, collected_at
                        ) VALUES ($1,$2,$3,$4::date,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18::numeric,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29,$30,$31,$32,$33,$34,$35,$36,$37)
                        ON CONFLICT (backend_id, request_id) DO UPDATE SET
                            event_time=EXCLUDED.event_time,
                            usage_date=EXCLUDED.usage_date,
                            raw_user_id=EXCLUDED.raw_user_id,
                            organization_id=EXCLUDED.organization_id,
                            team_id=EXCLUDED.team_id,
                            key_id=EXCLUDED.key_id,
                            principal_id=EXCLUDED.principal_id,
                            source=EXCLUDED.source,
                            model=EXCLUDED.model,
                            prompt_tokens=EXCLUDED.prompt_tokens,
                            completion_tokens=EXCLUDED.completion_tokens,
                            total_tokens=EXCLUDED.total_tokens,
                            request_count=EXCLUDED.request_count,
                            success_count=EXCLUDED.success_count,
                            failure_count=EXCLUDED.failure_count,
                            spend=EXCLUDED.spend,
                            attribution_source=EXCLUDED.attribution_source,
                            billing_eligible=EXCLUDED.billing_eligible,
                            provider=EXCLUDED.provider,
                            model_group=EXCLUDED.model_group,
                            model_id=EXCLUDED.model_id,
                            api_base=EXCLUDED.api_base,
                            status=EXCLUDED.status,
                            error_code=EXCLUDED.error_code,
                            error_class=EXCLUDED.error_class,
                            error_message=EXCLUDED.error_message,
                            scenario=EXCLUDED.scenario,
                            request_duration_ms=EXCLUDED.request_duration_ms,
                            ttft_ms=EXCLUDED.ttft_ms,
                            attempted_retries=EXCLUDED.attempted_retries,
                            max_retries=EXCLUDED.max_retries,
                            trace_id=EXCLUDED.trace_id,
                            user_visible_failure=EXCLUDED.user_visible_failure,
                            final_failure_source=EXCLUDED.final_failure_source,
                            collected_at=EXCLUDED.collected_at
                        """,
                        event_records,
                    )
        return len(usage_records)

    async def refresh_account_identity(
        self, backend_id: str, identities: list[dict[str, Any]]
    ) -> int:
        """按账号回填历史行的姓名/邮箱，不改动任何用量数字。

        ``employee_name``/``employee_email`` 是写快照时固化在 ``usage_daily`` 里的，
        匹配规则升级后旧日期的行仍留着当时的空姓名。这里只做一次身份列的批量更新，
        避免为了改一个显示字段重新拉一遍上游用量。
        """

        records = [
            (
                _clean_text(item.get("userId") or item.get("user_id")),
                _clean_text(item.get("name") or item.get("employeeName")),
                _clean_text(item.get("email") or item.get("employeeEmail")),
                _clean_text(item.get("emailSource") or item.get("email_source")),
            )
            for item in identities
        ]
        records = [item for item in records if item[0] and (item[1] or item[2])]
        if not records:
            return 0
        pool = self.pool
        if pool is None:
            return 0
        async with pool.acquire() as connection:
            await connection.executemany(
                """
                UPDATE usage_daily
                SET employee_name = $2,
                    employee_email = $3,
                    email_source = $4
                WHERE backend_id = $1 AND user_id = $5
                  AND (employee_name IS DISTINCT FROM $2
                       OR employee_email IS DISTINCT FROM $3
                       OR email_source IS DISTINCT FROM $4)
                """,
                [
                    (backend_id, name, email, email_source, user_id)
                    for user_id, name, email, email_source in records
                ],
            )
        logger.info("usage identity refreshed backend=%s accounts=%s", backend_id, len(records))
        return len(records)

    async def latest_sync_at(self, start_date: str, end_date: str, backend_ids: list[str] | None = None) -> datetime | None:
        backend_filter = ""
        args: list[Any] = [_as_date(start_date), _as_date(end_date)]
        if backend_ids:
            backend_filter = " AND backend_id = ANY($3::text[])"
            args.append(backend_ids)
        row = await self._require_pool().fetchval(
            f"""
            SELECT MAX(synced_at)
            FROM usage_sync_coverage
            WHERE usage_date BETWEEN $1::date AND $2::date{backend_filter}
            """,
            *args,
        )
        return row

    async def latest_backend_sync_at(self, backend_id: str, start_date: str, end_date: str) -> datetime | None:
        return await self._require_pool().fetchval(
            """
            SELECT MAX(synced_at)
            FROM usage_sync_coverage
            WHERE backend_id = $1 AND usage_date BETWEEN $2::date AND $3::date
            """,
            backend_id,
            _as_date(start_date),
            _as_date(end_date),
        )

    async def organization_daily_spend(
        self,
        start_date: str,
        end_date: str,
        backend_ids: list[str] | None = None,
        *,
        billing_effective_at_by_organization: dict[str, datetime] | None = None,
    ) -> list[dict[str, Any]]:
        """Return authoritative organization spend grouped by event date.

        Only explicitly attributed rows participate. Unmapped requests remain
        visible through data-quality metrics and can never be charged to a
        customer by email or another inferred identity.
        """

        backend_filter = ""
        args: list[Any] = [_as_date(start_date), _as_date(end_date)]
        if backend_ids:
            backend_filter = " AND backend_id = ANY($3::text[])"
            args.append(backend_ids)
        records = await self._require_pool().fetch(
            f"""
            SELECT organization_id, usage_date,
                   ROUND(COALESCE(SUM(spend), 0)::numeric, 6) AS spend
            FROM usage_query_daily
            WHERE usage_date BETWEEN $1::date AND $2::date
              AND organization_id <> ''
              AND billing_eligible = TRUE{backend_filter}
            GROUP BY organization_id, usage_date
            ORDER BY usage_date, organization_id
            """,
            *args,
        )
        cutoffs = billing_effective_at_by_organization or {}
        result: list[dict[str, Any]] = []
        for record in records:
            organization_id = str(record["organization_id"])
            usage_day = record["usage_date"]
            cutoff = cutoffs.get(organization_id)
            if cutoff is not None:
                cutoff_day = cutoff.astimezone(timezone.utc).date()
                if usage_day < cutoff_day:
                    continue
                if usage_day == cutoff_day:
                    # Daily aggregates cannot prove which requests happened
                    # before an intraday credit cutoff. Fail closed rather than
                    # charging pre-credit usage; expose the reason so operations
                    # can distinguish this from a successful zero-dollar day.
                    result.append(
                        {
                            "upstreamOrganizationId": organization_id,
                            "usageDate": usage_day.isoformat(),
                            "spendUsd": str(record["spend"] or 0),
                            "settlementStatus": "skipped",
                            "settlementReason": "needs_event_time",
                        }
                    )
                    continue
            result.append(
                {
                    "upstreamOrganizationId": organization_id,
                    "usageDate": usage_day.isoformat(),
                    "spendUsd": str(record["spend"] or 0),
                }
            )
        return result

    async def has_coverage(self, start_date: str, end_date: str, backend_ids: list[str]) -> bool:
        return bool(await self.covered_backend_ids(start_date, end_date, backend_ids))

    async def covered_backend_ids(self, start_date: str, end_date: str, backend_ids: list[str]) -> list[str]:
        if not backend_ids:
            return []
        records = await self._require_pool().fetch(
            """
            SELECT backend_id
            FROM usage_sync_coverage
            WHERE usage_date BETWEEN $1::date AND $2::date AND backend_id = ANY($3::text[])
            GROUP BY backend_id
            HAVING COUNT(*) = (($2::date - $1::date) + 1)
            """,
            _as_date(start_date),
            _as_date(end_date),
            backend_ids,
        )
        return [str(record["backend_id"]) for record in records]

    async def has_complete_coverage(self, start_date: str, end_date: str, backend_ids: list[str]) -> bool:
        """Return whether every configured backend covers the complete date range."""
        return set(await self.covered_backend_ids(start_date, end_date, backend_ids)) == set(backend_ids)

    async def department_directory(self, backend_ids: list[str], organization_id: str | None = None) -> list[dict[str, Any]]:
        if not backend_ids:
            return []
        conditions = ["backend_id = ANY($1::text[])", "status = 'active'", "department_id <> ''"]
        args: list[Any] = [backend_ids]
        if organization_id:
            args.append(organization_id)
            conditions.append(f"organization_id = ${len(args)}")
        rows = await self._require_pool().fetch(
            "SELECT backend_id, department_id, department_name, organization_id, status "
            "FROM usage_department_directory WHERE " + " AND ".join(conditions) +
            " ORDER BY lower(department_name), department_id", *args
        )
        return [
            {
                "departmentKey": department_key(str(row["department_id"]), str(row["department_name"] or row["department_id"])),
                "departmentId": str(row["department_id"]),
                "departmentName": str(row["department_name"] or row["department_id"]),
                "organizationId": str(row["organization_id"] or ""),
                "status": str(row["status"] or "active"),
            }
            for row in rows
        ]

    async def replace_department_directory(self, backend_id: str, departments: list[dict[str, Any]]) -> int:
        pool = self._require_pool()
        synced_at = datetime.now(timezone.utc)
        records = [
            (
                backend_id,
                str(item.get("departmentId") or ""),
                str(item.get("departmentName") or ""),
                str(item.get("organizationId") or ""),
                str(item.get("status") or "active"),
                synced_at,
            )
            for item in departments
            if str(item.get("departmentId") or "")
        ]
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM usage_department_directory WHERE backend_id=$1", backend_id
                )
                if records:
                    await connection.executemany(
                        "INSERT INTO usage_department_directory "
                        "(backend_id,department_id,department_name,organization_id,status,synced_at) "
                        "VALUES ($1,$2,$3,$4,$5,$6)",
                        records,
                    )
        return len(records)

    async def model_usage_counts(
        self,
        start_date: str,
        end_date: str,
        backend_ids: list[str],
    ) -> dict[str, int] | None:
        """Return model request counts, or None when the snapshot is incomplete."""
        covered = await self.covered_backend_ids(start_date, end_date, backend_ids)
        if set(covered) != set(backend_ids):
            return None
        records = await self._require_pool().fetch(
            """
            SELECT model, SUM(request_count)::bigint AS request_count
            FROM usage_query_daily
            WHERE usage_date BETWEEN $1::date AND $2::date
              AND backend_id = ANY($3::text[])
            GROUP BY model
            """,
            _as_date(start_date),
            _as_date(end_date),
            backend_ids,
        )
        counts: dict[str, int] = defaultdict(int)
        for record in records:
            model = normalize_model_display_name(record["model"]) or "未知模型"
            counts[model.casefold()] += _as_int(record["request_count"])
        return dict(counts)

    async def organization_rows(
        self,
        organization_id: str,
        start_date: str,
        end_date: str,
        source: str,
        backend_ids: list[str],
        *,
        employee: str = "",
    ) -> dict[str, Any] | None:
        """Read a real organization exclusively from persisted attribution."""

        covered = await self.covered_backend_ids(start_date, end_date, backend_ids)
        if set(covered) != set(backend_ids):
            return None
        conditions = [
            "organization_id=$1",
            "usage_date BETWEEN $2::date AND $3::date",
            "backend_id=ANY($4::text[])",
            "($5='all' OR source=$5)",
        ]
        args: list[Any] = [organization_id, _as_date(start_date), _as_date(end_date), covered, source or "all"]
        normalized_employee = _clean_text(employee).lower()
        if normalized_employee:
            args.append(normalized_employee)
            conditions.append(
                f"(lower(principal_id)=${len(args)} OR lower(user_id)=${len(args)} "
                f"OR lower(employee_email)=${len(args)} OR lower(employee_name)=${len(args)})"
            )
        where = " AND ".join(conditions)
        records = await self._require_pool().fetch(
            f"""
            SELECT backend_id, usage_date, user_id, principal_id, MAX(employee_email) AS employee_email,
                   MAX(employee_name) AS employee_name, MAX(email_source) AS email_source, source, team_id,
                   model AS model_name,
                   {self._aggregate_metrics_sql()}
            FROM usage_query_daily
            WHERE {where}
            GROUP BY backend_id, usage_date, user_id, principal_id, source, team_id, model
            ORDER BY usage_date, MAX(employee_name), source, model_name
            """,
            *args,
        )
        rows = [self._aggregated_usage_row(record) for record in records]
        for row, record in zip(rows, records):
            principal_id = _clean_text(record["principal_id"])
            row.update(
                {
                    "employeeId": principal_id or record["user_id"],
                    "employeeEmail": record["employee_email"] or "",
                    "employeeName": record["employee_name"] or record["user_id"],
                    "bindStatus": _record_bind_status(record),
                    "principalId": principal_id,
                    "_userId": record["user_id"],
                }
            )
        department_names: dict[str, str] = {}
        try:
            department_records = await self._require_pool().fetch(
                """
                SELECT team_id, MAX(team_name) AS team_name
                FROM usage_team_membership_daily
                WHERE snapshot_date BETWEEN $1::date AND $2::date
                  AND backend_id=ANY($3::text[])
                  AND team_id <> ''
                GROUP BY team_id
                """,
                _as_date(start_date),
                _as_date(end_date),
                covered,
            )
            department_names = {
                _clean_text(record["team_id"]): _clean_text(record["team_name"])
                for record in department_records
                if _clean_text(record["team_id"])
            }
        except Exception:
            # Team snapshots are supporting display data. The persisted event
            # team id remains authoritative when a historical label is absent.
            department_names = {}
        for row, record in zip(rows, records):
            team_id = _clean_text(record["team_id"])
            if team_id:
                row.update(
                    {
                        "departmentId": team_id,
                        "departmentName": department_names.get(team_id) or team_id,
                    }
                )
        rows = self._canonical_usage_rows(
            rows,
            ("_backendId", "date", "_userId", "source", "model", "principalId", "departmentId"),
        )
        public_rows = self._public_rows(rows)
        unattributed_records = await self._require_pool().fetchval(
            """
            SELECT COALESCE(SUM(request_count), 0)
            FROM usage_query_daily
            WHERE usage_date BETWEEN $1::date AND $2::date
              AND backend_id=ANY($3::text[])
              AND organization_id=''
            """,
            _as_date(start_date),
            _as_date(end_date),
            covered,
        )
        last_synced = await self.latest_sync_at(start_date, end_date, covered)
        departments: dict[str, dict[str, Any]] = {}
        for row in public_rows:
            team_id = _clean_text(row.get("departmentId"))
            if not team_id:
                continue
            item = departments.setdefault(
                team_id,
                {
                    "departmentId": team_id,
                    "departmentName": row.get("departmentName") or team_id,
                    "organizationId": organization_id,
                    "activeEmployees": 0,
                    **empty_totals(),
                },
            )
            add_totals(item, row)
        return {
            "rows": public_rows,
            "summaryRows": self._group_rows(public_rows, ("date", "source", "model")),
            "employees": self._employee_summaries(rows),
            "departments": sorted(
                departments.values(),
                key=lambda item: (-item["totalTokens"], str(item["departmentName"]).lower()),
            ),
            "pageLimit": 0,
            "pageSize": 0,
            "pagesRead": 0,
            "totalPages": 0,
            "totalRecords": len(public_rows),
            "truncated": False,
            "lastSyncedAt": last_synced,
            "coverage": {"startDate": start_date, "endDate": end_date, "backends": covered, "complete": True},
            "dataQuality": {
                "summarySource": "database",
                "rankingSource": "database",
                "organizationScoped": True,
                "unattributedRequestCount": _as_int(unattributed_records),
                "attributionPriority": "request_log_then_token_then_upstream_user",
            },
        }

    async def _fetch_usage(self, start_date: str, end_date: str, backend_ids: list[str] | None = None) -> list[dict[str, Any]]:
        backend_filter = ""
        args: list[Any] = [_as_date(start_date), _as_date(end_date)]
        if backend_ids:
            backend_filter = " AND backend_id = ANY($3::text[])"
            args.append(backend_ids)
        records = await self._require_pool().fetch(
            f"""
            SELECT backend_id, usage_date, user_id, organization_id, team_id, key_id, principal_id,
                   employee_email, employee_name, email_source,
                   source, model, prompt_tokens, completion_tokens, total_tokens,
                   request_count, success_count, failure_count, spend, collected_at
            FROM usage_query_daily
            WHERE usage_date BETWEEN $1::date AND $2::date{backend_filter}
            ORDER BY usage_date, employee_name, source, model
            """,
            *args,
        )
        rows = [self._usage_row(record) for record in records]
        return self._canonical_usage_rows(
            rows,
            ("_backendId", "date", "_userId", "source", "model", "principalId"),
        )

    @staticmethod
    def _usage_row(record: Any) -> dict[str, Any]:
        return {
            "date": record["usage_date"].isoformat(),
            "source": record["source"],
            "model": normalize_model_display_name(record["model"]) or "未知模型",
            "promptTokens": _as_int(record["prompt_tokens"]),
            "completionTokens": _as_int(record["completion_tokens"]),
            "totalTokens": _as_int(record["total_tokens"]),
            "requestCount": _as_int(record["request_count"]),
            "successCount": _as_int(record["success_count"]),
            "failureCount": _as_int(record["failure_count"]),
            "spend": _as_float(record["spend"]),
            "_backendId": record["backend_id"],
            "_userId": record["user_id"],
            "organizationId": _record_value(record, "organization_id", "") or "",
            "teamId": _record_value(record, "team_id", "") or "",
            "keyId": _record_value(record, "key_id", "") or "",
            "principalId": _record_value(record, "principal_id", "") or "",
            "employeeEmail": record["employee_email"],
            "employeeName": record["employee_name"],
            "emailSource": _record_value(record, "email_source", "") or "",
            "bindStatus": _record_bind_status(record),
        }

    @staticmethod
    def _public_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        identity_fields = [
            field
            for field in ("_backendId", "_userId", "employeeId", "departmentId", "departmentKey")
            if any(field in row for row in rows)
        ]
        canonical = UsageStore._canonical_usage_rows(
            rows,
            tuple(identity_fields + ["date", "source", "model"]),
        )
        return [{key: value for key, value in row.items() if not key.startswith("_")} for row in canonical]

    @classmethod
    def _canonical_usage_rows(
        cls, rows: list[dict[str, Any]], key_fields: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["model"] = normalize_model_display_name(item.get("model")) or "未知模型"
            normalized.append(item)
        return cls._merge_rows_by(normalized, key_fields)

    @staticmethod
    def _merge_rows_by(rows: list[dict[str, Any]], key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
        """合并 key 完全相同的行（历史数据模型名归一化后可能重名），非计量字段保留首行值。"""
        grouped: dict[tuple[str, ...], dict[str, Any]] = {}
        for row in rows:
            key = tuple(str(row.get(field) or "") for field in key_fields)
            current = grouped.get(key)
            if current is None:
                current = dict(row)
                current.update(empty_totals())
                grouped[key] = current
            add_totals(current, row)
        return list(grouped.values())

    @staticmethod
    def _group_rows(rows: list[dict[str, Any]], key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
        rows = UsageStore._canonical_usage_rows(rows, key_fields)
        grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in rows:
            key = tuple(row.get(field, "") for field in key_fields)
            bucket = grouped.get(key)
            if bucket is None:
                bucket = {field: row.get(field, "") for field in key_fields}
                bucket.update(empty_totals())
                grouped[key] = bucket
            add_totals(bucket, row)
        return sorted(grouped.values(), key=lambda item: tuple(str(item.get(field, "")) for field in key_fields))

    async def personal_rows(self, email: str, start_date: str, end_date: str, source: str, backend_ids: list[str]) -> dict[str, Any] | None:
        covered = await self.covered_backend_ids(start_date, end_date, backend_ids)
        if set(covered) != set(backend_ids):
            return None
        records = await self._require_pool().fetch(
            """
            SELECT usage_date, source, model, prompt_tokens, completion_tokens, total_tokens,
                   request_count, success_count, failure_count, spend
            FROM usage_query_daily
            WHERE usage_date BETWEEN $1::date AND $2::date AND employee_email = $3
              AND backend_id = ANY($4::text[])
              AND ($5 = 'all' OR source = $5)
            """,
            _as_date(start_date),
            _as_date(end_date),
            email.strip().lower(),
            covered,
            source or "all",
        )
        rows = [
            {
                "date": record["usage_date"].isoformat(),
                "source": record["source"],
                "model": normalize_model_display_name(record["model"]) or "未知模型",
                "promptTokens": _as_int(record["prompt_tokens"]),
                "completionTokens": _as_int(record["completion_tokens"]),
                "totalTokens": _as_int(record["total_tokens"]),
                "requestCount": _as_int(record["request_count"]),
                "successCount": _as_int(record["success_count"]),
                "failureCount": _as_int(record["failure_count"]),
                "spend": _as_float(record["spend"]),
            }
            for record in records
        ]
        return {"rows": self._group_rows(rows, ("date", "source", "model")), "lastSyncedAt": await self.latest_sync_at(start_date, end_date, covered)}

    async def personal_rows_by_user_ids(
        self,
        user_ids: list[str],
        start_date: str,
        end_date: str,
        source: str,
        backend_ids: list[str],
    ) -> dict[str, Any] | None:
        covered = await self.covered_backend_ids(start_date, end_date, backend_ids)
        if set(covered) != set(backend_ids):
            return None
        normalized = sorted({str(item).strip() for item in user_ids if str(item).strip()})
        records = await self._require_pool().fetch(
            """
            SELECT usage_date, source, model,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(total_tokens) AS total_tokens,
                   SUM(request_count) AS request_count,
                   SUM(success_count) AS success_count,
                   SUM(failure_count) AS failure_count,
                   SUM(spend) AS spend
            FROM usage_query_daily
            WHERE usage_date BETWEEN $1::date AND $2::date
              AND user_id=ANY($3::text[])
              AND backend_id=ANY($4::text[])
              AND ($5='all' OR source=$5)
            GROUP BY usage_date, source, model
            ORDER BY usage_date, source, model
            """,
            _as_date(start_date),
            _as_date(end_date),
            normalized,
            covered,
            source or "all",
        )
        rows = [self._aggregated_usage_row(record, include_identity=False) for record in records]
        return {
            "rows": self._public_rows(rows),
            "lastSyncedAt": await self.latest_sync_at(start_date, end_date, covered),
        }

    async def team_leader_scope(
        self,
        email: str,
        user_ids: list[str],
        backend_ids: list[str],
    ) -> dict[str, Any]:
        """Resolve leader scope from the committed membership snapshot.

        Mirrors the upstream rule: leadership is anchored on the primary backend
        and other backends only contribute scopes for the same logical team.
        """

        empty = {"isTeamLeader": False, "teamBoardStatus": "none", "team": None, "leaderTeams": []}
        if not backend_ids:
            return empty
        normalized_email = email.strip().lower()
        normalized_ids = sorted({str(item).strip() for item in user_ids if str(item).strip()})
        records = await self._require_pool().fetch(
            """
            WITH latest AS (
                SELECT DISTINCT ON (backend_id, team_id, user_id)
                       backend_id, team_id, team_name, user_id,
                       employee_email, team_role, snapshot_date
                FROM usage_team_membership_daily
                WHERE backend_id=ANY($1::text[])
                ORDER BY backend_id, team_id, user_id, snapshot_date DESC
            )
            SELECT backend_id, team_id,
                   (array_agg(team_name ORDER BY snapshot_date DESC))[1] AS team_name,
                   COUNT(*) AS member_count,
                   bool_or(
                       lower(COALESCE(team_role, ''))='admin'
                       AND (
                           user_id=ANY($2::text[])
                           OR ($3<>'' AND lower(btrim(COALESCE(employee_email, '')))=$3)
                       )
                   ) AS is_leader
            FROM latest
            GROUP BY backend_id, team_id
            """,
            backend_ids,
            normalized_ids,
            normalized_email,
        )
        primary_backend = str(backend_ids[0])
        summaries_by_identity: dict[str, list[dict[str, Any]]] = defaultdict(list)
        anchors: list[dict[str, Any]] = []
        for row in records:
            team_id = str(row["team_id"] or "").strip()
            if not team_id:
                continue
            team_name = str(row["team_name"] or "").strip() or team_id
            summary = {
                "id": team_id,
                "name": team_name,
                "memberCount": int(row["member_count"] or 0),
                "backend": str(row["backend_id"]),
            }
            summaries_by_identity[team_identity_key(team_id, team_name)].append(summary)
            if summary["backend"] == primary_backend and row["is_leader"]:
                anchors.append(summary)
        leader_teams: list[dict[str, Any]] = []
        for anchor in sorted(anchors, key=lambda item: (normalize_team_text(item["name"]), item["id"])):
            identity = team_identity_key(anchor["id"], anchor["name"])
            scopes = [anchor]
            for backend_id in backend_ids[1:]:
                match = next(
                    (
                        item
                        for item in summaries_by_identity.get(identity, [])
                        if item["backend"] == str(backend_id)
                    ),
                    None,
                )
                if match is not None:
                    scopes.append(match)
            leader_teams.append({**anchor, "teamScopes": scopes})
        if not leader_teams:
            return empty
        return {
            "isTeamLeader": True,
            "teamBoardStatus": "single" if len(leader_teams) == 1 else "multiple",
            "team": leader_teams[0] if len(leader_teams) == 1 else None,
            "leaderTeams": leader_teams,
        }

    async def latest_membership_teams(
        self,
        backend_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return each member's most recent membership rows.

        一个用户在同一最新快照日期横跨多个团队时（部门改名后旧团队未清理），每行
        返回一次，由调用方按重命名映射折叠后判定唯一归属。供实时事件归因按团队
        目录回填 team_id。
        """

        args: list[Any] = []
        backend_filter = ""
        if backend_ids:
            backend_filter = "WHERE backend_id = ANY($1::text[])"
            args.append(backend_ids)
        records = await self._require_pool().fetch(
            f"""
            WITH ranked AS (
                SELECT backend_id, user_id, team_id, team_name, snapshot_date,
                       RANK() OVER (
                           PARTITION BY backend_id, user_id
                           ORDER BY snapshot_date DESC
                       ) AS rank
                FROM usage_team_membership_daily
                {backend_filter}
            )
            SELECT backend_id, user_id, team_id, team_name, snapshot_date
            FROM ranked
            WHERE rank = 1
            """,
            *args,
        )
        return [
            {
                "backendId": str(row["backend_id"]),
                "userId": str(row["user_id"]),
                "teamId": str(row["team_id"]),
                "teamName": str(row["team_name"] or ""),
                "snapshotDate": row["snapshot_date"].isoformat()
                if row["snapshot_date"]
                else "",
            }
            for row in records
        ]

    async def team_member_directory(self, team_scopes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """列出团队的最新成员名册，不受用量日期范围限制。

        团队看板的成员列表按快照日期过滤，成员在所选区间没有调用就会消失；
        权限判断必须覆盖全部在册成员，因此这里只取每个账号最新的一条成员记录。
        """

        pairs = [
            (str(scope.get("backend") or "").strip(), str(scope.get("id") or "").strip())
            for scope in team_scopes or []
        ]
        pairs = [pair for pair in pairs if pair[0] and pair[1]]
        if not pairs:
            return []
        conditions: list[str] = []
        args: list[Any] = []
        for backend_id, team_id in pairs:
            args.extend([backend_id, team_id])
            conditions.append(f"(backend_id = ${len(args) - 1} AND team_id = ${len(args)})")
        records = await self._require_pool().fetch(
            """
            SELECT DISTINCT ON (backend_id, team_id, user_id)
                   backend_id, team_id, user_id,
                   employee_email, employee_name, team_role
            FROM usage_team_membership_daily
            WHERE """
            + " OR ".join(conditions)
            + """
            ORDER BY backend_id, team_id, user_id, snapshot_date DESC
            """,
            *args,
        )
        members: dict[str, dict[str, Any]] = {}
        for row in records:
            backend_id = str(row["backend_id"] or "").strip()
            user_id = str(row["user_id"] or "").strip()
            if not backend_id or not user_id:
                continue
            email = str(row["employee_email"] or "").strip()
            role = str(row["team_role"] or "").strip().lower() or "user"
            account_id = f"{backend_id}:{user_id}"
            existing = members.get(account_id)
            if existing is None:
                members[account_id] = {
                    "backendId": backend_id,
                    "userId": user_id,
                    "accountId": account_id,
                    "employeeEmail": email,
                    "employeeName": str(row["employee_name"] or "").strip() or email or user_id,
                    "teamRole": role,
                }
                continue
            if role == "admin":
                existing["teamRole"] = "admin"
        # 同一个人可能在多个后端有账号，只要有一条是负责人就整体按负责人处理。
        admin_emails = {
            item["employeeEmail"].lower()
            for item in members.values()
            if item["teamRole"] == "admin" and item["employeeEmail"]
        }
        for item in members.values():
            if item["employeeEmail"] and item["employeeEmail"].lower() in admin_emails:
                item["teamRole"] = "admin"
        return sorted(members.values(), key=lambda item: (str(item["employeeName"]).lower(), item["accountId"]))

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
        """Aggregate one member across every identity that carries their spend.

        A member owns two kinds of identity: the stable upstream user id issued
        when their profile was created, and any local principal preserving the
        upstream ids they used before the tenant was adopted.  Historical
        report-only spend often splits across several upstream ids, so no single
        id can cover it.  The union runs as one query because a row can match on
        both columns at once — summing two separate reads would double-count it.
        """

        covered = await self.covered_backend_ids(start_date, end_date, backend_ids)
        if set(covered) != set(backend_ids):
            return None
        user_ids = sorted({str(item).strip() for item in upstream_user_ids if str(item).strip()})
        principals = sorted({str(item).strip() for item in principal_ids if str(item).strip()})
        records = await self._require_pool().fetch(
            """
            SELECT usage_date, source, model,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(total_tokens) AS total_tokens,
                   SUM(request_count) AS request_count,
                   SUM(success_count) AS success_count,
                   SUM(failure_count) AS failure_count,
                   SUM(spend) AS spend,
                   ARRAY_AGG(DISTINCT user_id) AS matched_user_ids
            FROM usage_query_daily
            WHERE organization_id=$1
              AND (user_id=ANY($2::text[]) OR principal_id=ANY($3::text[]))
              AND usage_date BETWEEN $4::date AND $5::date
              AND backend_id=ANY($6::text[])
              AND ($7='all' OR source=$7)
            GROUP BY usage_date, source, model
            ORDER BY usage_date, source, model
            """,
            organization_id,
            user_ids,
            principals,
            _as_date(start_date),
            _as_date(end_date),
            covered,
            source or "all",
        )
        rows: list[dict[str, Any]] = []
        matched_user_ids: set[str] = set()
        for record in records:
            matched_user_ids.update(
                str(item) for item in (record["matched_user_ids"] or []) if item
            )
            rows.append(
                {
                    "date": record["usage_date"].isoformat(),
                    "source": record["source"],
                    "model": normalize_model_display_name(record["model"]) or "未知模型",
                    "promptTokens": _as_int(record["prompt_tokens"]),
                    "completionTokens": _as_int(record["completion_tokens"]),
                    "totalTokens": _as_int(record["total_tokens"]),
                    "requestCount": _as_int(record["request_count"]),
                    "successCount": _as_int(record["success_count"]),
                    "failureCount": _as_int(record["failure_count"]),
                    "spend": _as_float(record["spend"]),
                }
            )
        return {
            "rows": self._group_rows(rows, ("date", "source", "model")),
            "principalIds": principals,
            "upstreamUserIds": sorted(matched_user_ids),
            "lastSyncedAt": await self.latest_sync_at(start_date, end_date, covered),
        }

    async def rows_by_employee_emails(
        self,
        emails: list[str],
        start_date: str,
        end_date: str,
        source: str,
        backend_ids: list[str],
    ) -> dict[str, dict[str, Any]] | None:
        normalized = sorted({str(email).strip().lower() for email in emails if str(email).strip()})
        if not normalized or set(await self.covered_backend_ids(start_date, end_date, backend_ids)) != set(backend_ids):
            return None
        records = await self._require_pool().fetch(
            """
            SELECT employee_email, usage_date, source, model, prompt_tokens, completion_tokens,
                   total_tokens, request_count, success_count, failure_count, spend,
                   ARRAY_AGG(DISTINCT user_id) AS user_ids
            FROM usage_query_daily
            WHERE usage_date BETWEEN $1::date AND $2::date
              AND employee_email = ANY($3::text[])
              AND backend_id = ANY($4::text[])
              AND ($5 = 'all' OR source = $5)
            GROUP BY employee_email, usage_date, source, model
            ORDER BY employee_email, usage_date, source, model
            """,
            _as_date(start_date), _as_date(end_date), normalized, backend_ids, source or "all",
        )
        result: dict[str, dict[str, Any]] = {email: {"rows": [], "userIds": [], "lastSyncedAt": None} for email in normalized}
        for record in records:
            email = str(record["employee_email"] or "").strip().lower()
            if email not in result:
                continue
            result[email]["rows"].append({
                "date": record["usage_date"].isoformat(),
                "source": record["source"],
                "model": normalize_model_display_name(record["model"]) or "未知模型",
                "promptTokens": _as_int(record["prompt_tokens"]),
                "completionTokens": _as_int(record["completion_tokens"]),
                "totalTokens": _as_int(record["total_tokens"]),
                "requestCount": _as_int(record["request_count"]),
                "successCount": _as_int(record["success_count"]),
                "failureCount": _as_int(record["failure_count"]),
                "spend": _as_float(record["spend"]),
            })
            result[email]["userIds"].extend(str(item) for item in (record["user_ids"] or []) if item)
        last_synced = await self.latest_sync_at(start_date, end_date, backend_ids)
        for item in result.values():
            item["rows"] = self._group_rows(item["rows"], ("date", "source", "model"))
            item["userIds"] = sorted(set(item["userIds"]))
            item["lastSyncedAt"] = last_synced
        return result

    @staticmethod
    def _aggregate_metrics_sql(prefix: str = "") -> str:
        return ", ".join(
            f"SUM({prefix}{field})::{('double precision' if field == 'spend' else 'bigint')} AS {field}"
            for field in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "request_count",
                "success_count",
                "failure_count",
                "spend",
            )
        )

    @staticmethod
    def _aggregated_usage_row(record: Any, include_identity: bool = True) -> dict[str, Any]:
        raw_model = _record_value(record, "model_name", None)
        if raw_model is None:
            raw_model = _record_value(record, "model", "")
        row = {
            "date": record["usage_date"].isoformat(),
            "source": record["source"],
            "model": normalize_model_display_name(raw_model) or "未知模型",
            "promptTokens": _as_int(record["prompt_tokens"]),
            "completionTokens": _as_int(record["completion_tokens"]),
            "totalTokens": _as_int(record["total_tokens"]),
            "requestCount": _as_int(record["request_count"]),
            "successCount": _as_int(record["success_count"]),
            "failureCount": _as_int(record["failure_count"]),
            "spend": _as_float(record["spend"]),
        }
        if include_identity:
            row.update(
                {
                    "_backendId": record["backend_id"],
                    "_userId": record["user_id"],
                    "employeeEmail": record["employee_email"] or "",
                    "employeeName": record["employee_name"] or record["user_id"] or "",
                }
            )
        return row

    async def _query_aggregated_rows(
        self,
        start_date: str,
        end_date: str,
        source: str,
        backend_ids: list[str],
        employee_ids: list[str] | None = None,
        team_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read already grouped daily rows for a scope without materializing raw records."""
        conditions = [
            "u.usage_date BETWEEN $1::date AND $2::date",
            "u.backend_id = ANY($3::text[])",
            "($4 = 'all' OR u.source = $4)",
        ]
        args: list[Any] = [_as_date(start_date), _as_date(end_date), backend_ids, source or "all"]
        if employee_ids:
            args.append(employee_ids)
            conditions.append(f"u.user_id = ANY(${len(args)}::text[])")
        if team_id:
            args.append(team_id)
            conditions.append(f"m.team_id = ${len(args)}")
        model_sql = "u.model"
        records = await self._require_pool().fetch(
            f"""
            SELECT u.backend_id, u.usage_date, u.user_id,
                   MAX(u.employee_email) AS employee_email,
                   MAX(u.employee_name) AS employee_name,
                   MAX(u.email_source) AS email_source,
                   u.source, {model_sql} AS model_name,
                   {self._aggregate_metrics_sql('u.')}
            FROM usage_query_daily u
            {"JOIN usage_team_membership_daily m ON m.backend_id = u.backend_id AND m.snapshot_date = u.usage_date AND m.user_id = u.user_id" if team_id else ""}
            WHERE {" AND ".join(conditions)}
            GROUP BY u.backend_id, u.usage_date, u.user_id, u.source, {model_sql}
            ORDER BY u.usage_date, MAX(u.employee_name), u.source, model_name
            """,
            *args,
        )
        return [self._aggregated_usage_row(record) for record in records]

    async def admin_rows(self, start_date: str, end_date: str, source: str, employee: str | None, backend_ids: list[str]) -> dict[str, Any] | None:
        covered = await self.covered_backend_ids(start_date, end_date, backend_ids)
        if not covered:
            return None
        employee_filter = (employee or "").strip().lower()
        conditions = [
            "usage_date BETWEEN $1::date AND $2::date",
            "backend_id = ANY($3::text[])",
            "($4 = 'all' OR source = $4)",
        ]
        args: list[Any] = [_as_date(start_date), _as_date(end_date), covered, source or "all"]
        if employee_filter:
            conditions.append("(position($5 IN lower(user_id)) > 0 OR position($5 IN lower(employee_email)) > 0 OR position($5 IN lower(employee_name)) > 0)")
            args.append(employee_filter)
        where_sql = " AND ".join(conditions)
        pool = self._require_pool()
        model_sql = "model"
        # 每员工×每天×每模型的明细只在筛选了某个员工时才有人看：未筛选时前端的
        # 趋势图走 summaryRows、排行榜走 employees，明细仅喂按 source/model 聚合的
        # 饼图与条形图，而那两张图从 summaryRows 能算出同样的结果。近 30 天这份明细
        # 有 14000 多行，SQL 本身只占 172ms，其余一秒多全花在传输与构造 dict 上，
        # 所以未筛选时直接不查。
        row_records = (
            await pool.fetch(
                f"""
            SELECT backend_id, usage_date, user_id, MAX(employee_email) AS employee_email,
                   MAX(employee_name) AS employee_name, MAX(email_source) AS email_source, source, {model_sql} AS model_name,
                   {self._aggregate_metrics_sql()}
            FROM usage_query_daily
            WHERE {where_sql}
            GROUP BY backend_id, usage_date, user_id, source, {model_sql}
            ORDER BY usage_date, MAX(employee_name), source, model_name
            """,
                *args,
            )
            if employee_filter
            else []
        )
        enriched = []
        for record in row_records:
            row = self._aggregated_usage_row(record)
            row.update(
                {
                    "employeeId": record["user_id"],
                    "employeeName": record["employee_name"] or record["user_id"],
                    "employeeEmail": record["employee_email"] or "",
                    "bindStatus": _record_bind_status(record),
                }
            )
            enriched.append(row)

        employee_records = await pool.fetch(
            f"""
            WITH filtered AS (
                SELECT *, COALESCE(NULLIF(employee_email, ''), user_id) AS employee_key,
                       {model_sql} AS model_name
                FROM usage_query_daily
                WHERE {where_sql}
            ), totals AS (
                SELECT employee_key, MIN(user_id) AS employee_id,
                       MAX(NULLIF(employee_email, '')) AS employee_email,
                       MAX(NULLIF(employee_name, '')) AS employee_name,
                       MAX(NULLIF(email_source, '')) AS email_source,
                       -- user_ids 在这一次分组里顺便聚出来。写成相关子查询
                       -- （ARRAY(SELECT ... WHERE employee_key = totals.employee_key)）
                       -- 会让 Postgres 对每个员工重扫一遍 filtered：近 30 天是
                       -- 513 loops × 15000 行 ≈ 1.3 秒，占该区间几乎全部耗时。
                       ARRAY_AGG(DISTINCT user_id ORDER BY user_id) AS user_ids,
                       {self._aggregate_metrics_sql('')}
                FROM filtered
                GROUP BY employee_key
            ), source_totals AS (
                SELECT employee_key, source, SUM(total_tokens)::bigint AS source_tokens
                FROM filtered
                GROUP BY employee_key, source
            ), primary_sources AS (
                SELECT DISTINCT ON (employee_key) employee_key, source AS primary_source
                FROM source_totals
                ORDER BY employee_key, source_tokens DESC, source
            )
            SELECT totals.*, primary_sources.primary_source
            FROM totals
            JOIN primary_sources USING (employee_key)
            ORDER BY totals.total_tokens DESC, totals.spend DESC, lower(COALESCE(totals.employee_name, totals.employee_id))
            """,
            *args,
        )
        # 员工排行有部门列，所以每一行都要部门归属，不能像逐员工明细那样只在
        # 筛选后才查。这张快照表按 (backend_id, snapshot_date, team_id, user_id)
        # 建主键，DISTINCT 后的结果集是成员数量级（不是用量行数量级），近 30 天
        # 全员也只有几百行。
        department_names, department_snapshot = await self._employee_department_names(
            start_date, end_date, covered, employee_filter
        )

        employees = [
            {
                "employeeId": record["employee_id"],
                "employeeName": record["employee_name"] or record["employee_id"],
                "employeeEmail": record["employee_email"] or "",
                "bindStatus": _record_bind_status(record),
                **{
                    "promptTokens": _as_int(record["prompt_tokens"]),
                    "completionTokens": _as_int(record["completion_tokens"]),
                    "totalTokens": _as_int(record["total_tokens"]),
                    "requestCount": _as_int(record["request_count"]),
                    "successCount": _as_int(record["success_count"]),
                    "failureCount": _as_int(record["failure_count"]),
                    "spend": _as_float(record["spend"]),
                },
                "primarySource": record["primary_source"] or "其他",
                "userIds": list(record["user_ids"] or []),
                "departmentNames": _department_names_for(
                    department_names,
                    record["employee_email"],
                    list(record["user_ids"] or []),
                ),
                "teamRole": "user",
            }
            for record in employee_records
        ]

        summary_records = await pool.fetch(
            f"""
            SELECT usage_date, source, {model_sql} AS model_name, {self._aggregate_metrics_sql()}
            FROM usage_query_daily
            WHERE {where_sql}
            GROUP BY usage_date, source, {model_sql}
            ORDER BY usage_date, source, model_name
            """,
            *args,
        )
        summary_rows = [self._aggregated_usage_row(record, include_identity=False) for record in summary_records]
        summary_rows = self._group_rows(summary_rows, ("date", "source", "model"))
        public_rows = self._public_rows(enriched)
        return {
            "rows": public_rows,
            "summaryRows": summary_rows,
            "employees": employees,
            "pageLimit": 0,
            "pageSize": 0,
            "pagesRead": 0,
            "totalPages": 0,
            # 未筛选员工时不查明细，用聚合行数表示本次统计规模，避免看板把范围显示成空。
            "totalRecords": len(enriched) if enriched else len(summary_rows),
            "truncated": False,
            "dataQuality": {
                "summarySource": "database",
                "rankingSource": "database",
                "departmentSnapshot": department_snapshot,
            },
            "lastSyncedAt": await self.latest_sync_at(start_date, end_date, covered),
        }

    async def realtime_settlement(self, backend_id: str) -> dict[str, Any] | None:
        row = await self._require_pool().fetchrow(
            "SELECT verified_through, status, last_error, updated_at FROM usage_realtime_settlement WHERE backend_id=$1",
            backend_id,
        )
        if not row:
            return None
        return {
            "verifiedThrough": row["verified_through"],
            "status": str(row["status"] or "pending"),
            "lastError": str(row["last_error"] or ""),
            "updatedAt": row["updated_at"],
        }

    async def realtime_settlements(self, backend_ids: list[str]) -> list[dict[str, Any]]:
        if not backend_ids:
            return []
        records = await self._require_pool().fetch(
            "SELECT backend_id, verified_through, status, last_error, updated_at FROM usage_realtime_settlement WHERE backend_id=ANY($1::text[])",
            backend_ids,
        )
        return [
            {
                "backendId": str(row["backend_id"]),
                "verifiedThrough": row["verified_through"],
                "status": str(row["status"] or "pending"),
                "lastError": str(row["last_error"] or ""),
                "updatedAt": row["updated_at"],
            }
            for row in records
        ]

    async def advance_realtime_settlement(
        self,
        backend_id: str,
        verified_through: datetime,
        *,
        status: str = "settled",
        error: str = "",
    ) -> None:
        await self._require_pool().execute(
            """
            INSERT INTO usage_realtime_settlement (backend_id, verified_through, status, last_error, updated_at)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT (backend_id) DO UPDATE SET
                verified_through=GREATEST(usage_realtime_settlement.verified_through, EXCLUDED.verified_through),
                status=EXCLUDED.status, last_error=EXCLUDED.last_error, updated_at=EXCLUDED.updated_at
            """,
            backend_id, verified_through, status, error, datetime.now(timezone.utc),
        )

    async def record_realtime_settlement_segment(
        self,
        *,
        backend_id: str,
        start_time: datetime,
        end_time: datetime,
        status: str,
        request_count: int = 0,
        amount: float | str = 0,
        retry_count: int = 0,
        error_summary: str = "",
        completed_at: datetime | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        await self._require_pool().execute(
            """
            INSERT INTO usage_realtime_settlement_segments (
                backend_id, start_time, end_time, status, request_count,
                amount, retry_count, error_summary, completed_at, updated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (backend_id, start_time, end_time) DO UPDATE SET
                status=EXCLUDED.status, request_count=EXCLUDED.request_count,
                amount=EXCLUDED.amount, retry_count=usage_realtime_settlement_segments.retry_count + EXCLUDED.retry_count,
                error_summary=EXCLUDED.error_summary, completed_at=EXCLUDED.completed_at,
                updated_at=EXCLUDED.updated_at
            """,
            backend_id, start_time, end_time, status, int(request_count), amount,
            int(retry_count), error_summary[:500], completed_at, now,
        )

    async def _employee_department_names(
        self,
        start_date: str,
        end_date: str,
        backend_ids: list[str],
        employee_filter: str = "",
    ) -> tuple[dict[str, list[str]], dict[str, Any]]:
        """返回 {user_id / employee_email: 部门名列表}，供成员下钻显示部门归属。

        同一员工可能在多个后端有账号、也可能同时属于多个部门，所以按 user_id 和
        邮箱两个维度都建索引：调用方先用邮箱查（跨后端账号合并后的身份），查不到
        再退回 user_id。
        """

        conditions = [
            "snapshot_date <= $2::date",
            "backend_id = ANY($3::text[])",
        ]
        args: list[Any] = [_as_date(start_date), _as_date(end_date), backend_ids]
        if employee_filter:
            conditions.append(
                "(position($4 IN lower(user_id)) > 0 OR position($4 IN lower(employee_email)) > 0 OR position($4 IN lower(employee_name)) > 0)"
            )
            args.append(employee_filter)
        records = await self._require_pool().fetch(
            """
            WITH ranked AS (
                SELECT user_id, employee_email, team_id, team_name, snapshot_date,
                       MAX(snapshot_date) FILTER (WHERE snapshot_date BETWEEN $1::date AND $2::date)
                           OVER (PARTITION BY backend_id, user_id) AS range_latest_date,
                       MAX(snapshot_date) OVER (PARTITION BY backend_id, user_id) AS fallback_latest_date
                FROM usage_team_membership_daily
                WHERE """ + " AND ".join(conditions) + """
            )
            SELECT DISTINCT user_id, employee_email, team_id, team_name, snapshot_date,
                   range_latest_date, fallback_latest_date
            FROM ranked
            WHERE snapshot_date = COALESCE(range_latest_date, fallback_latest_date)
            """,
            *args,
        )
        grouped: dict[str, list[str]] = {}
        latest_date = ""
        latest_fallback_date = ""
        has_selected_range = False
        has_fallback = False
        for record in records:
            snapshot_date = record.get("snapshot_date")
            snapshot_date_text = (
                snapshot_date.isoformat()
                if hasattr(snapshot_date, "isoformat")
                else str(snapshot_date or "")
            )
            if snapshot_date_text > latest_date:
                latest_date = snapshot_date_text
            if record.get("range_latest_date"):
                has_selected_range = True
            elif record.get("fallback_latest_date"):
                has_fallback = True
                if snapshot_date_text > latest_fallback_date:
                    latest_fallback_date = snapshot_date_text
            name = _clean_text(record["team_name"]) or _clean_text(record["team_id"])
            if not name or _clean_text(record["team_id"]).lower() == "unassigned":
                continue
            for key in (_clean_text(record["user_id"]).lower(), _clean_text(record["employee_email"]).lower()):
                if not key:
                    continue
                names = grouped.setdefault(key, [])
                if name not in names:
                    names.append(name)
        for names in grouped.values():
            names.sort()
        if has_selected_range and has_fallback:
            source = "mixed"
        elif has_selected_range:
            source = "selected_range"
        elif has_fallback:
            source = "latest_before_end_date"
        else:
            source = "none"
        return grouped, {
            "source": source,
            "latestDate": latest_date or None,
            "latestFallbackDate": latest_fallback_date or None,
        }

    @staticmethod
    def _employee_summaries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            principal_id = _clean_text(row.get("principalId"))
            key = (
                principal_id
                or _clean_text(row.get("employeeEmail"))
                or _clean_text(row.get("employeeId"))
            )
            item = grouped.setdefault(
                key,
                {
                    "employeeId": principal_id or row.get("employeeId"),
                    "principalId": principal_id,
                    "employeeName": row.get("employeeName") or row.get("employeeId"),
                    "employeeEmail": row.get("employeeEmail") or "",
                    "bindStatus": row.get("bindStatus") or "未绑定邮箱",
                    **empty_totals(),
                    "primarySource": "其他",
                    "userIds": [row.get("_userId")] if row.get("_userId") else [],
                    "teamRole": "user",
                },
            )
            add_totals(item, row)
            if row.get("_userId") and row["_userId"] not in item["userIds"]:
                item["userIds"].append(row["_userId"])
        return sorted(grouped.values(), key=lambda item: (-item["totalTokens"], -item["spend"], str(item["employeeName"]).lower()))

    async def _membership_rows(self, start_date: str, end_date: str, backend_id: str | None = None, team_id: str | None = None) -> list[Any]:
        conditions = ["snapshot_date BETWEEN $1::date AND $2::date"]
        args: list[Any] = [_as_date(start_date), _as_date(end_date)]
        if backend_id:
            args.append(backend_id)
            conditions.append(f"backend_id = ${len(args)}")
        if team_id:
            args.append(team_id)
            conditions.append(f"team_id = ${len(args)}")
        return await self._require_pool().fetch(
            "SELECT backend_id, snapshot_date, team_id, team_name, user_id, employee_email, employee_name, team_role FROM usage_team_membership_daily WHERE "
            + " AND ".join(conditions),
            *args,
        )

    async def department_rows(self, start_date: str, end_date: str, source: str, department: str | None, backend_ids: list[str]) -> dict[str, Any] | None:
        covered = await self.covered_backend_ids(start_date, end_date, backend_ids)
        if not covered:
            return None
        department_filter = normalize_team_text(department)
        args: list[Any] = [_as_date(start_date), _as_date(end_date), covered, source or "all", department_filter]
        # Keep the team recorded on the usage event authoritative. Resolve its
        # display name from the durable directory first because realtime mode
        # does not publish a membership snapshot for every current day.
        team_id_sql = "lower(btrim(u.team_id))"
        resolved_team_name_sql = "COALESCE(NULLIF(dd.department_name, ''), NULLIF(dn.team_name, ''), u.team_id)"
        team_name_sql = f"lower(regexp_replace(btrim({resolved_team_name_sql}), '\\s+', ' ', 'g'))"
        logical_key_sql = f"{team_id_sql} || '::' || {team_name_sql}"
        where_sql = f"""
            u.usage_date BETWEEN $1::date AND $2::date
            AND u.backend_id = ANY($3::text[])
            AND ($4 = 'all' OR u.source = $4)
            AND u.team_id <> ''
            AND ($5 = '' OR {logical_key_sql} = $5 OR {team_id_sql} = $5 OR {team_name_sql} = $5)
        """
        department_join_sql = """
            LEFT JOIN usage_department_directory dd
              ON dd.backend_id = u.backend_id AND dd.department_id = u.team_id
            LEFT JOIN (
                SELECT backend_id, team_id, MAX(NULLIF(team_name, '')) AS team_name
                FROM usage_team_membership_daily
                WHERE snapshot_date BETWEEN $1::date AND $2::date
                  AND backend_id = ANY($3::text[])
                GROUP BY backend_id, team_id
            ) dn ON dn.backend_id = u.backend_id AND dn.team_id = u.team_id
        """
        model_sql = "u.model"
        pool = self._require_pool()
        # 与 admin_rows 同理：部门看板只在选中某个部门后才渲染逐员工明细，未选中时
        # 画的是部门排行（departmentRankings）与聚合趋势，明细纯属白传。
        records = (
            await pool.fetch(
                f"""
            SELECT u.backend_id, u.usage_date, u.user_id,
                   MAX(u.employee_email) AS employee_email,
                   MAX(u.employee_name) AS employee_name,
                   MAX(u.email_source) AS email_source,
                   u.team_id, MAX({resolved_team_name_sql}) AS team_name, '' AS team_role,
                   u.source, {model_sql} AS model_name,
                   {self._aggregate_metrics_sql('u.')}
            FROM usage_query_daily u
            {department_join_sql}
            WHERE {where_sql}
            GROUP BY u.backend_id, u.usage_date, u.user_id, u.team_id, u.source, {model_sql}
            ORDER BY u.usage_date, MAX({resolved_team_name_sql}), MAX(u.employee_name), u.source, model_name
            """,
                *args,
            )
            if department_filter
            else []
        )
        rows = []
        for record in records:
            row = self._aggregated_usage_row(record)
            row.update(
                {
                    "departmentId": record["team_id"],
                    "departmentName": record["team_name"] or record["team_id"],
                    "departmentKey": department_key(record["team_id"], record["team_name"] or record["team_id"]),
                    "departmentBindStatus": "已绑定部门",
                    "employeeId": record["user_id"],
                    "employeeName": record["employee_name"] or record["user_id"],
                    "employeeEmail": record["employee_email"],
                    "bindStatus": _record_bind_status(record),
                }
            )
            rows.append(row)

        employee_records = await pool.fetch(
            f"""
            WITH filtered AS (
                SELECT u.*, {resolved_team_name_sql} AS team_name,
                       lower(COALESCE(NULLIF(u.employee_email, ''), u.user_id)) AS employee_key,
                       {model_sql} AS model_name
                FROM usage_query_daily u
                {department_join_sql}
                WHERE {where_sql}
            ), totals AS (
                SELECT employee_key, MIN(user_id) AS employee_id,
                       MAX(NULLIF(employee_email, '')) AS employee_email,
                       MAX(NULLIF(employee_name, '')) AS employee_name,
                       MAX(NULLIF(email_source, '')) AS email_source,
                       -- 与 admin_rows 同理：相关子查询会按员工数重扫 filtered，
                       -- 这里在同一次分组里聚出 user_ids。
                       ARRAY_AGG(DISTINCT user_id ORDER BY user_id) AS user_ids,
                       {self._aggregate_metrics_sql('')}
                FROM filtered
                GROUP BY employee_key
            ), source_totals AS (
                SELECT employee_key, source, SUM(total_tokens)::bigint AS source_tokens
                FROM filtered
                GROUP BY employee_key, source
            ), primary_sources AS (
                SELECT DISTINCT ON (employee_key) employee_key, source AS primary_source
                FROM source_totals
                ORDER BY employee_key, source_tokens DESC, source
            )
            SELECT totals.*, primary_sources.primary_source
            FROM totals JOIN primary_sources USING (employee_key)
            ORDER BY totals.total_tokens DESC, totals.spend DESC, lower(COALESCE(totals.employee_name, totals.employee_id))
            """,
            *args,
        )
        employees = [
            {
                "employeeId": record["employee_id"],
                "employeeName": record["employee_name"] or record["employee_id"],
                "employeeEmail": record["employee_email"] or "",
                "bindStatus": _record_bind_status(record),
                "promptTokens": _as_int(record["prompt_tokens"]),
                "completionTokens": _as_int(record["completion_tokens"]),
                "totalTokens": _as_int(record["total_tokens"]),
                "requestCount": _as_int(record["request_count"]),
                "successCount": _as_int(record["success_count"]),
                "failureCount": _as_int(record["failure_count"]),
                "spend": _as_float(record["spend"]),
                "primarySource": record["primary_source"] or "其他",
                "userIds": list(record["user_ids"] or []),
                "teamRole": "user",
            }
            for record in employee_records
        ]

        department_records = await pool.fetch(
            f"""
            WITH filtered AS (
                SELECT u.*, {resolved_team_name_sql} AS team_name, {logical_key_sql} AS department_key, {model_sql} AS model_name
                FROM usage_query_daily u
                {department_join_sql}
                WHERE {where_sql}
            ), source_totals AS (
                SELECT department_key, source, SUM(total_tokens)::bigint AS source_tokens
                FROM filtered GROUP BY department_key, source
            ), primary_sources AS (
                SELECT DISTINCT ON (department_key) department_key, source AS primary_source
                FROM source_totals ORDER BY department_key, source_tokens DESC, source
            )
            SELECT MIN(team_id) AS team_id, MIN(team_name) AS team_name, filtered.department_key,
                   {self._aggregate_metrics_sql('')}, COUNT(DISTINCT user_id)::bigint AS active_employees,
                   primary_sources.primary_source
            FROM filtered JOIN primary_sources USING (department_key)
            GROUP BY filtered.department_key, primary_sources.primary_source
            ORDER BY total_tokens DESC, spend DESC, lower(MIN(team_name))
            """,
            *args,
        )
        departments = [
            {
                "departmentKey": record["department_key"],
                "departmentId": record["team_id"],
                "departmentName": record["team_name"] or record["team_id"],
                "bindStatus": "已绑定部门",
                "promptTokens": _as_int(record["prompt_tokens"]),
                "completionTokens": _as_int(record["completion_tokens"]),
                "totalTokens": _as_int(record["total_tokens"]),
                "requestCount": _as_int(record["request_count"]),
                "successCount": _as_int(record["success_count"]),
                "failureCount": _as_int(record["failure_count"]),
                "spend": _as_float(record["spend"]),
                "primarySource": record["primary_source"] or "其他",
                "activeEmployees": _as_int(record["active_employees"]),
            }
            for record in department_records
        ]
        directory = await self.department_directory(covered)
        usage_by_id = {str(item.get("departmentId")): item for item in departments}
        for option in directory:
            current = usage_by_id.get(str(option["departmentId"]))
            if current:
                directory_name = str(option.get("departmentName") or "")
                option.update(current)
                option["departmentName"] = directory_name or str(current.get("departmentName") or option["departmentId"])
                option["departmentKey"] = department_key(str(option["departmentId"]), str(option["departmentName"]))
            else:
                option.update({
                    "promptTokens": 0, "completionTokens": 0, "totalTokens": 0,
                    "requestCount": 0, "successCount": 0, "failureCount": 0,
                    "spend": 0.0, "primarySource": "", "activeEmployees": 0,
                })
        summary_records = await pool.fetch(
            f"""
            SELECT u.usage_date, u.source, {model_sql} AS model_name, {self._aggregate_metrics_sql('u.')}
            FROM usage_query_daily u
            {department_join_sql}
            WHERE {where_sql}
            GROUP BY u.usage_date, u.source, {model_sql}
            ORDER BY u.usage_date, u.source, model_name
            """,
            *args,
        )
        summary_rows = [self._aggregated_usage_row(record, include_identity=False) for record in summary_records]
        summary_rows = self._group_rows(summary_rows, ("date", "source", "model"))
        public_rows = self._public_rows(rows)
        return {
            "rows": public_rows,
            "summaryRows": summary_rows,
            "departments": departments,
            "departmentOptions": directory,
            "employees": employees,
            "pageLimit": 0,
            "pageSize": 0,
            "pagesRead": 0,
            "totalPages": 0,
            "totalRecords": len(rows),
            "truncated": False,
            "dataQuality": {"summarySource": "database", "rankingSource": "database", "backends": covered, "departmentIdentityMatch": "normalized_team_id_and_name"},
            "lastSyncedAt": await self.latest_sync_at(start_date, end_date, covered),
        }

    async def _team_rows_legacy_unused(self, backend_id: str, team_id: str, start_date: str, end_date: str, source: str) -> dict[str, Any] | None:
        if not await self.has_coverage(start_date, end_date, [backend_id]):
            return None
        pool = self._require_pool()
        latest_members = await pool.fetch(
            """
            SELECT DISTINCT ON (user_id) snapshot_date, team_name, user_id,
                   employee_email, employee_name, team_role
            FROM usage_team_membership_daily
            WHERE backend_id = $1 AND team_id = $2
              AND snapshot_date BETWEEN $3::date AND $4::date
            ORDER BY user_id, snapshot_date DESC
            """,
            backend_id, team_id, _as_date(start_date), _as_date(end_date),
        )
        if not latest_members:
            return None
        args: list[Any] = [backend_id, team_id, _as_date(start_date), _as_date(end_date), source or "all"]
        model_sql = "u.model"
        records = await pool.fetch(
            f"""
            SELECT u.backend_id, u.usage_date, u.user_id,
                   MAX(u.employee_email) AS employee_email,
                   MAX(u.employee_name) AS employee_name,
                   MAX(u.email_source) AS email_source,
                   u.source, {model_sql} AS model_name,
                   {self._aggregate_metrics_sql('u.')}
            FROM usage_query_daily u
            JOIN LATERAL (
                SELECT m.team_role, m.employee_email, m.employee_name
                FROM usage_team_membership_daily m
                WHERE m.backend_id = u.backend_id AND m.snapshot_date = u.usage_date AND m.team_id = $2
                  AND (m.user_id = u.user_id OR (NULLIF(btrim(m.employee_email), '') IS NOT NULL AND lower(btrim(m.employee_email)) = lower(btrim(u.employee_email))))
                ORDER BY (m.user_id = u.user_id) DESC, m.user_id
                LIMIT 1
            ) m ON TRUE
            WHERE u.backend_id = $1
              AND u.usage_date BETWEEN $3::date AND $4::date
              AND ($5 = 'all' OR u.source = $5)
            GROUP BY u.backend_id, u.usage_date, u.user_id, u.source, {model_sql}
            ORDER BY u.usage_date, MAX(u.employee_name), u.source, model_name
            """,
            *args,
        )
        rows = []
        for record in records:
            row = self._aggregated_usage_row(record)
            row.update(
                {
                    "employeeId": record["user_id"],
                    "employeeName": record["employee_name"] or record["user_id"],
                    "employeeEmail": record["employee_email"] or "",
                    "bindStatus": _record_bind_status(record),
                }
            )
            rows.append(row)

        employee_records = await pool.fetch(
            f"""
            WITH filtered AS (
                SELECT u.*, m.team_role,
                       lower(COALESCE(NULLIF(btrim(m.employee_email), ''), NULLIF(btrim(u.employee_email), ''), btrim(u.user_id))) AS employee_key,
                       {model_sql} AS model_name
                FROM usage_query_daily u
                JOIN LATERAL (
                    SELECT m.team_role, m.employee_email
                    FROM usage_team_membership_daily m
                    WHERE m.backend_id = u.backend_id AND m.snapshot_date = u.usage_date AND m.team_id = $2
                      AND (m.user_id = u.user_id OR (NULLIF(btrim(m.employee_email), '') IS NOT NULL AND lower(btrim(m.employee_email)) = lower(btrim(u.employee_email))))
                    ORDER BY (m.user_id = u.user_id) DESC, m.user_id
                    LIMIT 1
                ) m ON TRUE
                WHERE u.backend_id = $1
                  AND u.usage_date BETWEEN $3::date AND $4::date
                  AND ($5 = 'all' OR u.source = $5)
            ), totals AS (
                SELECT employee_key, MIN(user_id) AS employee_id,
                       MAX(NULLIF(employee_email, '')) AS employee_email,
                       MAX(NULLIF(employee_name, '')) AS employee_name,
                       MAX(NULLIF(email_source, '')) AS email_source,
                       MAX(team_role) AS team_role,
                       -- 同 admin_rows：避免按成员数重扫 filtered 的相关子查询。
                       ARRAY_AGG(DISTINCT user_id ORDER BY user_id) AS user_ids,
                       {self._aggregate_metrics_sql('')}
            FROM filtered GROUP BY employee_key
            ), source_totals AS (
                SELECT employee_key, source, SUM(total_tokens)::bigint AS source_tokens
                FROM filtered GROUP BY employee_key, source
            ), primary_sources AS (
                SELECT DISTINCT ON (employee_key) employee_key, source AS primary_source
                FROM source_totals ORDER BY employee_key, source_tokens DESC, source
            )
            SELECT totals.*, primary_sources.primary_source
            FROM totals JOIN primary_sources USING (employee_key)
            ORDER BY totals.total_tokens DESC, totals.spend DESC, lower(COALESCE(totals.employee_name, totals.employee_id))
            """,
            *args,
        )
        employee_by_user_id: dict[str, dict[str, Any]] = {}
        for record in employee_records:
            item = {
                "employeeId": record["employee_id"],
                "employeeName": record["employee_name"] or record["employee_id"],
                "employeeEmail": record["employee_email"] or "",
                "bindStatus": _record_bind_status(record),
                "promptTokens": _as_int(record["prompt_tokens"]),
                "completionTokens": _as_int(record["completion_tokens"]),
                "totalTokens": _as_int(record["total_tokens"]),
                "requestCount": _as_int(record["request_count"]),
                "successCount": _as_int(record["success_count"]),
                "failureCount": _as_int(record["failure_count"]),
                "spend": _as_float(record["spend"]),
                "primarySource": record["primary_source"] or "其他",
                "userIds": list(record["user_ids"] or []),
                "teamRole": record["team_role"] or "user",
            }
            for user_id in item["userIds"]:
                employee_by_user_id[str(user_id)] = item
            if item["employeeEmail"]:
                employee_by_user_id[f"email:{item['employeeEmail'].strip().lower()}"] = item
        employees = self._merge_team_members(latest_members, employee_by_user_id)
        employees.sort(key=lambda item: (-item["totalTokens"], -item["spend"], str(item["employeeName"]).lower()))
        team_name = latest_members[0]["team_name"] or team_id
        summary_records = await pool.fetch(
            f"""
            SELECT u.usage_date, u.source, {model_sql} AS model_name, {self._aggregate_metrics_sql('u.')}
            FROM usage_query_daily u
            JOIN LATERAL (
                SELECT 1
                FROM usage_team_membership_daily m
                WHERE m.backend_id = u.backend_id AND m.snapshot_date = u.usage_date AND m.team_id = $2
                  AND (m.user_id = u.user_id OR (NULLIF(btrim(m.employee_email), '') IS NOT NULL AND lower(btrim(m.employee_email)) = lower(btrim(u.employee_email))))
                LIMIT 1
            ) m ON TRUE
            WHERE u.backend_id = $1
              AND u.usage_date BETWEEN $3::date AND $4::date
              AND ($5 = 'all' OR u.source = $5)
            GROUP BY u.usage_date, u.source, {model_sql}
            ORDER BY u.usage_date, u.source, model_name
            """,
            *args,
        )
        summary_rows = [self._aggregated_usage_row(record, include_identity=False) for record in summary_records]
        summary_rows = self._group_rows(summary_rows, ("date", "source", "model"))
        public_rows = self._public_rows(rows)
        return {
            "rows": public_rows,
            "summaryRows": summary_rows,
            "employees": employees,
            "team": {"id": team_id, "name": team_name or team_id, "memberCount": len(employees), "backend": backend_id},
            "pageLimit": 0,
            "pageSize": 0,
            "pagesRead": 0,
            "totalPages": 0,
            "totalRecords": len(rows),
            "truncated": False,
            "dataQuality": {"summarySource": "database", "rankingSource": "database"},
            "lastSyncedAt": await self.latest_sync_at(start_date, end_date, [backend_id]),
        }

    @staticmethod
    def _merge_team_members(latest_members: list[Any], employee_by_user_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        employees_by_identity: dict[str, dict[str, Any]] = {}
        for member in latest_members:
            member_email = str(member["employee_email"] or "").strip().lower()
            backend_id = str(member.get("backend_id") if hasattr(member, "get") else member["backend_id"] if "backend_id" in member else "")
            item = (employee_by_user_id.get(f"email:{member_email}") if member_email else None) or employee_by_user_id.get(f"id:{backend_id}:{member['user_id']}") or employee_by_user_id.get(str(member["user_id"]))
            if item is None:
                account_id = f"{backend_id}:{member['user_id']}"
                item = {"employeeId": member["user_id"] if member_email else account_id, "employeeName": member["employee_name"] or member["user_id"], "employeeEmail": member["employee_email"] or "", "bindStatus": _record_bind_status(member), **empty_totals(), "primarySource": "其他", "userIds": [account_id], "teamRole": member["team_role"] or "user"}
            else:
                item = dict(item)
                item["teamRole"] = member["team_role"] or item.get("teamRole") or "user"
            email = str(item.get("employeeEmail") or member["employee_email"] or "").strip().lower()
            identity = f"email:{email}" if email else f"id:{backend_id}:{str(member['user_id']).strip().lower()}"
            existing = employees_by_identity.get(identity)
            if existing is None:
                employees_by_identity[identity] = item
                continue
            for user_id in item.get("userIds") or []:
                if user_id not in existing["userIds"]:
                    existing["userIds"].append(user_id)
            if not existing.get("employeeEmail") and item.get("employeeEmail"):
                existing["employeeEmail"] = item["employeeEmail"]
            if not existing.get("employeeName") and item.get("employeeName"):
                existing["employeeName"] = item["employeeName"]
            if existing.get("teamRole") != "admin" and item.get("teamRole") == "admin":
                existing["teamRole"] = "admin"
        return list(employees_by_identity.values())

    async def _team_member_rows_legacy_unused(self, backend_id: str, team_id: str, employee: str, start_date: str, end_date: str, source: str) -> dict[str, Any] | None:
        if not await self.has_coverage(start_date, end_date, [backend_id]):
            return None
        pool = self._require_pool()
        normalized = employee.strip().lower()
        members = await pool.fetch(
            """
            SELECT DISTINCT ON (user_id) user_id, employee_email, employee_name, team_role, team_name
            FROM usage_team_membership_daily
            WHERE backend_id = $1 AND team_id = $2
              AND snapshot_date BETWEEN $3::date AND $4::date
              AND ($5 = lower(btrim(user_id)) OR $5 = lower(btrim(employee_email)) OR $5 = lower(btrim(employee_name)))
            ORDER BY user_id, snapshot_date DESC
            """,
            backend_id, team_id, _as_date(start_date), _as_date(end_date), normalized,
        )
        if not members:
            return None
        selected_user_ids = [str(member["user_id"]) for member in members]
        selected_emails = sorted({str(member["employee_email"]).strip().lower() for member in members if member["employee_email"]})
        args: list[Any] = [backend_id, team_id, _as_date(start_date), _as_date(end_date), source or "all", selected_user_ids, selected_emails]
        model_sql = "u.model"
        records = await pool.fetch(
            f"""
            SELECT u.backend_id, u.usage_date, u.user_id,
                   MAX(u.employee_email) AS employee_email,
                   MAX(u.employee_name) AS employee_name,
                   MAX(u.email_source) AS email_source,
                   u.source, {model_sql} AS model_name,
                   {self._aggregate_metrics_sql('u.')}
            FROM usage_query_daily u
            JOIN LATERAL (
                SELECT 1
                FROM usage_team_membership_daily m
                WHERE m.backend_id = u.backend_id AND m.snapshot_date = u.usage_date AND m.team_id = $2
                  AND (m.user_id = u.user_id OR (NULLIF(btrim(m.employee_email), '') IS NOT NULL AND lower(btrim(m.employee_email)) = lower(btrim(u.employee_email))))
                LIMIT 1
            ) m ON TRUE
            WHERE u.backend_id = $1
              AND u.usage_date BETWEEN $3::date AND $4::date
              AND ($5 = 'all' OR u.source = $5)
              AND (u.user_id = ANY($6::text[]) OR lower(NULLIF(btrim(u.employee_email), '')) = ANY($7::text[]))
            GROUP BY u.backend_id, u.usage_date, u.user_id, u.source, {model_sql}
            ORDER BY u.usage_date, u.source, model_name
            """,
            *args,
        )
        rows = []
        for record in records:
            row = self._aggregated_usage_row(record)
            row.update({"employeeId": record["user_id"], "employeeName": record["employee_name"] or record["user_id"], "employeeEmail": record["employee_email"] or "", "bindStatus": _record_bind_status(record)})
            rows.append({key: value for key, value in row.items() if not key.startswith("_")})
        selected_member = members[0]
        selected = {
            "employeeId": selected_user_ids[0],
            "employeeName": selected_member["employee_name"] or selected_user_ids[0],
            "employeeEmail": selected_member["employee_email"] or "",
            "bindStatus": _record_bind_status(selected_member),
            "userIds": selected_user_ids,
            "teamRole": selected_member["team_role"] or "user",
            **empty_totals(),
            "primarySource": "其他",
        }
        for member in members:
            selected["employeeName"] = selected["employeeName"] or member["employee_name"] or selected["employeeId"]
            selected["employeeEmail"] = selected["employeeEmail"] or member["employee_email"] or ""
            selected["teamRole"] = member["team_role"] or selected["teamRole"]
        selected.update(summarize(rows)["rangeTotal"])
        source_totals: dict[str, int] = defaultdict(int)
        for row in rows:
            source_totals[str(row.get("source") or "其他")] += _as_int(row.get("totalTokens"))
        if source_totals:
            selected["primarySource"] = max(source_totals.items(), key=lambda item: (item[1], item[0]))[0]
        team_name = selected_member["team_name"] or team_id
        return {
            "rows": rows,
            "summary": summarize(rows),
            "employee": selected,
            "team": {"id": team_id, "name": team_name, "memberCount": len(members), "backend": backend_id},
            "lastSyncedAt": await self.latest_sync_at(start_date, end_date, [backend_id]),
        }

    async def team_member_rows(self, team_scopes: list[dict[str, Any]], employee: str, start_date: str, end_date: str, source: str) -> dict[str, Any] | None:
        backend_ids = [str(item.get("backend")) for item in team_scopes if item.get("backend") and item.get("id")]
        team_ids = [str(item.get("id")) for item in team_scopes if item.get("backend") and item.get("id")]
        covered = sorted(set(backend_ids))
        if not backend_ids or not await self.has_complete_coverage(start_date, end_date, covered):
            return None
        normalized = employee.strip().casefold()
        selected_backend = ""
        selected_user = normalized
        if ":" in normalized:
            possible_backend, possible_user = normalized.split(":", 1)
            if possible_backend in covered:
                selected_backend, selected_user = possible_backend, possible_user
        model_sql = "u.model"
        records = await self._require_pool().fetch(
            f"""
            WITH scope(backend_id, team_id) AS (SELECT * FROM unnest($1::text[], $2::text[])),
            selected AS (
                SELECT DISTINCT ON (m.backend_id, m.user_id) m.backend_id, m.team_id, m.team_name, m.user_id,
                       m.employee_email, m.employee_name, m.team_role
                FROM usage_team_membership_daily m JOIN scope s ON s.backend_id=m.backend_id AND s.team_id=m.team_id
                WHERE m.snapshot_date <= $4::date
                  AND (($6<>'' AND m.backend_id=$6 AND $5=lower(btrim(m.user_id)))
                       OR ($6='' AND ($5=lower(btrim(m.user_id)) OR $5=lower(btrim(m.employee_email)) OR $5=lower(btrim(m.employee_name)))))
                ORDER BY m.backend_id, m.user_id, m.snapshot_date DESC
            )
            SELECT 'member' AS kind, s.backend_id, s.team_id, s.team_name, s.user_id, s.employee_email, s.employee_name, ''::text AS email_source, s.team_role,
                   NULL::date AS usage_date, NULL::text AS source, NULL::text AS model_name,
                   0::bigint AS prompt_tokens, 0::bigint AS completion_tokens, 0::bigint AS total_tokens,
                   0::bigint AS request_count, 0::bigint AS success_count, 0::bigint AS failure_count, 0::double precision AS spend
            FROM selected s
            UNION ALL
            SELECT 'usage', MIN(u.backend_id), NULL, NULL, MIN(u.user_id), MAX(u.employee_email), MAX(u.employee_name), MAX(u.email_source), NULL,
                   u.usage_date, u.source, {model_sql} AS model_name, {self._aggregate_metrics_sql('u.')}
            FROM usage_query_daily u
            WHERE u.backend_id=ANY($1::text[]) AND u.usage_date BETWEEN $3::date AND $4::date
              AND ($7='all' OR u.source=$7)
              AND EXISTS (SELECT 1 FROM selected s WHERE s.backend_id=u.backend_id AND (s.user_id=u.user_id OR (NULLIF(btrim(s.employee_email),'') IS NOT NULL AND lower(btrim(s.employee_email))=lower(btrim(u.employee_email)))))
             AND EXISTS (SELECT 1 FROM scope sc WHERE sc.backend_id=u.backend_id AND sc.team_id=u.team_id)
            GROUP BY u.usage_date, u.source, {model_sql}
            ORDER BY kind, usage_date NULLS FIRST, source, model_name
            """,
            backend_ids, team_ids, _as_date(start_date), _as_date(end_date), selected_user, selected_backend, source or "all",
        )
        members = [item for item in records if item["kind"] == "member"]
        if not members:
            anchor = team_scopes[0]
            return {
                "rows": [],
                "summary": summarize([]),
                "employee": None,
                "team": {"id": anchor["id"], "name": anchor.get("name") or anchor["id"], "memberCount": 0, "backend": anchor["backend"]},
                "lastSyncedAt": await self.latest_sync_at(start_date, end_date, covered),
                "dataQuality": {"backends": covered, "scopeCount": len(team_scopes), "memberIdentityMatch": "normalized_email_or_backend_user_id", "teamAttribution": "usage_event_team_id", "memberDirectory": "latest_snapshot_on_or_before_end_date"},
            }
        rows = []
        for record in records:
            if record["kind"] != "usage":
                continue
            row = self._aggregated_usage_row(record)
            row.update(
                {
                    "employeeId": record["user_id"],
                    "employeeName": record["employee_name"] or record["user_id"],
                    "employeeEmail": record["employee_email"] or "",
                    "bindStatus": _record_bind_status(record),
                }
            )
            rows.append(self._public_rows([row])[0])
        rows = self._group_rows(rows, ("date", "source", "model", "employeeId"))
        first = members[0]
        user_ids = [f"{item['backend_id']}:{item['user_id']}" for item in members]
        selected = {
            "employeeId": first["user_id"] if first["employee_email"] else f"{first['backend_id']}:{first['user_id']}",
            "employeeName": first["employee_name"] or first["user_id"],
            "employeeEmail": first["employee_email"] or "",
            "bindStatus": _record_bind_status(first),
            "userIds": user_ids,
            "teamRole": "admin" if any(item["team_role"] == "admin" for item in members) else first["team_role"] or "user",
            **empty_totals(),
            "primarySource": "其他",
        }
        selected.update(summarize(rows)["rangeTotal"])
        anchor = team_scopes[0]
        return {"rows": rows, "summary": summarize(rows), "employee": selected, "team": {"id": anchor["id"], "name": anchor.get("name") or first["team_name"] or anchor["id"], "memberCount": len(members), "backend": anchor["backend"]}, "lastSyncedAt": await self.latest_sync_at(start_date, end_date, covered), "dataQuality": {"backends": covered, "scopeCount": len(team_scopes), "memberIdentityMatch": "normalized_email_or_backend_user_id", "teamAttribution": "usage_event_team_id", "memberDirectory": "latest_snapshot_on_or_before_end_date"}}

    async def health(self) -> dict[str, Any]:
        if self.pool is None:
            return {"enabled": True, "connected": False, "status": "disconnected"}
        try:
            await self.pool.fetchval("SELECT 1")
        except Exception as exc:  # pragma: no cover - depends on database
            return {"enabled": True, "connected": False, "status": "error", "error": exc.__class__.__name__}
        return {"enabled": True, "connected": True, "status": "ok"}

    @staticmethod
    def _stability_final_request_record(event: dict[str, Any], received_at: datetime) -> tuple[Any, ...] | None:
        """把 spend-log 拉取的一条最终请求事件转成 final_request 尝试事件。

        上游 /spend/logs/v2 只有每条请求的最终状态（含 attempted_retries /
        status / error），没有 fallback 过程，因此 fallback 相关字段固定为空、
        is_fallback=False；重试信息从 attemptedRetries 还原，供「上游异常率」
        「重试恢复率」在外部 attempt 推送接入前先行计算。
        """
        request_id = _clean_text(_input_value(event, "request_id", "requestId"))
        event_time = _optional_datetime(_input_value(event, "event_time", "eventTime"))
        if not request_id or event_time is None:
            return None
        usage_date = _input_value(event, "usage_date", "usageDate")
        try:
            event_date = (
                event_time.date()
                if usage_date in (None, "")
                else _as_date(str(usage_date)[:10])
            )
        except (TypeError, ValueError):
            event_date = event_time.date()
        started_at = _optional_datetime(_input_value(event, "start_time", "startTime"))
        ended_at = _optional_datetime(_input_value(event, "end_time", "endTime"))
        raw_duration = _input_value(event, "request_duration_ms", "durationMs")
        duration_ms = None if raw_duration in (None, "") else _as_float(raw_duration)
        if duration_ms is None and started_at is not None and ended_at is not None:
            duration_ms = max(0.0, (ended_at - started_at).total_seconds() * 1000)
        raw_ttft = _input_value(event, "ttft_ms", "ttftMs")
        ttft_ms = None if raw_ttft in (None, "") else _as_float(raw_ttft)
        attempted_retries = max(0, _as_int(_input_value(event, "attempted_retries", "attemptedRetries")))
        model_group = _clean_text(_input_value(event, "model_group", "modelGroup")) or _clean_text(event.get("model"))
        return (
            _clean_text(event.get("backend_id")) or "",
            request_id[:256],
            request_id[:512],
            _clean_text(_input_value(event, "trace_id", "traceId"))[:512],
            request_id[:256],
            attempted_retries,
            attempted_retries,
            model_group[:256],
            model_group[:256],
            "",
            _clean_text(event.get("provider"))[:160],
            "final_request",
            (_clean_text(event.get("status")) or "unknown")[:64],
            _clean_text(_input_value(event, "error_code", "errorCode"))[:120],
            _clean_text(_input_value(event, "error_class", "errorClass"))[:160],
            "",
            _clean_text(_input_value(event, "error_message", "errorMessage"))[:1000],
            (_clean_text(event.get("scenario")) or "unknown")[:64],
            _clean_text(_input_value(event, "scenario_version", "scenarioVersion"))[:64],
            event_time,
            event_date,
            started_at,
            ended_at,
            ttft_ms,
            duration_ms,
            "",
            "",
            attempted_retries > 0,
            False,
            received_at,
            received_at,
        )

    @staticmethod
    def _stability_attempt_record(event: dict[str, Any], received_at: datetime) -> tuple[Any, ...]:
        backend_id = _clean_text(_input_value(event, "backend_id", "backendId"))
        event_id = _clean_text(_input_value(event, "event_id", "eventId"))
        if not backend_id or not event_id:
            raise ValueError("稳定性尝试事件缺少 backend_id 或 event_id")
        started_at = _optional_datetime(_input_value(event, "started_at", "startedAt"))
        ended_at = _optional_datetime(_input_value(event, "ended_at", "endedAt"))
        collected_at = _optional_datetime(_input_value(event, "collected_at", "collectedAt"))
        event_time = collected_at or ended_at or started_at
        if event_time is None:
            raise ValueError("稳定性尝试事件缺少 started_at、ended_at 或 collected_at")
        duration_ms = _input_value(event, "duration_ms", "durationMs")
        if duration_ms in (None, "") and started_at and ended_at:
            duration_ms = max(0.0, (ended_at - started_at).total_seconds() * 1000)
        attempt_index = max(0, _as_int(_input_value(event, "attempt_index", "attemptIndex")))
        retry_index = max(0, _as_int(_input_value(event, "retry_index", "retryIndex")))
        event_type = _clean_text(_input_value(event, "event_type", "eventType"))[:64]
        fallback_from = _clean_text(_input_value(event, "fallback_from", "fallbackFrom"))[:256]
        fallback_to = _clean_text(_input_value(event, "fallback_to", "fallbackTo"))[:256]
        is_retry = _as_bool(_input_value(event, "is_retry", "isRetry", default=False)) or retry_index > 0 or "retry" in event_type
        is_fallback = _as_bool(_input_value(event, "is_fallback", "isFallback", default=False)) or bool(fallback_from or fallback_to) or "fallback" in event_type
        return (
            backend_id[:120],
            event_id[:256],
            _clean_text(_input_value(event, "request_id", "requestId"))[:512],
            _clean_text(_input_value(event, "trace_id", "traceId"))[:512],
            _clean_text(_input_value(event, "attempt_id", "attemptId"))[:256],
            attempt_index,
            retry_index,
            _clean_text(_input_value(event, "requested_model_group", "requestedModelGroup"))[:256],
            _clean_text(_input_value(event, "actual_model", "actualModel"))[:256],
            _clean_text(_input_value(event, "route_name", "route"))[:256],
            _clean_text(event.get("provider"))[:160],
            event_type or "attempt",
            (_clean_text(event.get("status")) or "unknown")[:64],
            _clean_text(_input_value(event, "error_code", "errorCode"))[:120],
            _clean_text(_input_value(event, "error_class", "errorClass"))[:160],
            _clean_text(_input_value(event, "error_category", "errorCategory"))[:120],
            _clean_text(_input_value(event, "error_message", "errorMessage"))[:1000],
            (_clean_text(event.get("scenario")) or "unknown")[:64],
            _clean_text(_input_value(event, "scenario_version", "scenarioVersion"))[:64],
            event_time,
            event_time.date(),
            started_at,
            ended_at,
            _as_float(_input_value(event, "ttft_ms", "ttftMs")) if _input_value(event, "ttft_ms", "ttftMs") not in (None, "") else None,
            _as_float(duration_ms) if duration_ms not in (None, "") else None,
            fallback_from,
            fallback_to,
            is_retry,
            is_fallback,
            collected_at,
            received_at,
        )

    async def insert_stability_attempt_events(self, events: list[dict[str, Any]]) -> int:
        if not events:
            return 0
        received_at = datetime.now(timezone.utc)
        records = [self._stability_attempt_record(event, received_at) for event in events]
        inserted = 0
        async with self._require_pool().acquire() as connection:
            async with connection.transaction():
                for record in records:
                    result = await connection.execute(
                        """
                        INSERT INTO stability_attempt_events (
                            backend_id, event_id, request_id, trace_id, attempt_id,
                            attempt_index, retry_index, requested_model_group,
                            actual_model, route_name, provider, event_type, status,
                            error_code, error_class, error_category, error_message, scenario,
                            scenario_version, event_time, event_date, started_at,
                            ended_at, ttft_ms, duration_ms, fallback_from, fallback_to,
                            is_retry, is_fallback, collected_at, received_at
                        ) VALUES (
                            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                            $16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29,$30,$31
                        ) ON CONFLICT (backend_id, event_id) DO NOTHING
                        """,
                        *record,
                    )
                    inserted += int(result.endswith(" 1"))
        return inserted

    async def stability_attempt_events(
        self,
        start_date: str,
        end_date: str,
        model: str = "",
        trace_id: str = "",
        request_id: str = "",
        backend_id: str = "",
    ) -> list[dict[str, Any]]:
        records = await self._require_pool().fetch(
            """
            SELECT * FROM stability_attempt_events
            WHERE event_date BETWEEN $1::date AND $2::date
              AND ($3='' OR requested_model_group=$3 OR actual_model=$3)
              AND ($4='' OR trace_id=$4)
              AND ($5='' OR request_id=$5)
              AND ($6='' OR backend_id=$6)
            ORDER BY event_time, attempt_index, retry_index, event_id
            """,
            _as_date(start_date), _as_date(end_date), _clean_text(model),
            _clean_text(trace_id), _clean_text(request_id), _clean_text(backend_id),
        )
        return [dict(record) for record in records]

    async def stability_attempt_timeline(self, request_id: str, backend_id: str = "") -> list[dict[str, Any]]:
        records = await self._require_pool().fetch(
            """
            SELECT * FROM stability_attempt_events
            WHERE request_id=$1 AND ($2='' OR backend_id=$2)
            ORDER BY event_time, attempt_index, retry_index, event_id
            """,
            _clean_text(request_id), _clean_text(backend_id),
        )
        return [dict(record) for record in records]

    async def list_stability_actions(
        self,
        *,
        status: str = "",
        severity: str = "",
        owner: str = "",
        model: str = "",
        scenario: str = "",
        request_id: str = "",
        start_date: str = "",
        end_date: str = "",
    ) -> list[dict[str, Any]]:
        records = await self._require_pool().fetch(
            """
            SELECT * FROM stability_actions
            WHERE ($1='' OR status=$1) AND ($2='' OR severity=$2)
              AND ($3='' OR owner=$3) AND ($4='' OR requested_model_group=$4)
              AND ($5='' OR scenario=$5)
              AND ($6='' OR request_id=$6)
            ORDER BY
              CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                            WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END,
              target_date NULLS LAST, updated_at DESC
            """,
            _clean_text(status), _clean_text(severity), _clean_text(owner),
            _clean_text(model), _clean_text(scenario), _clean_text(request_id),
        )
        return [dict(record) for record in records]

    async def create_stability_action(self, action: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        record = await self._require_pool().fetchrow(
            """
            INSERT INTO stability_actions (
                id, title, description, notes, owner, severity, status, target_date,
                fix_reference, requested_model_group, scenario, error_code, request_id,
                baseline_start_date, baseline_end_date, created_by, updated_by,
                created_at, updated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::date,$9,$10,$11,$12,$13,$14::date,$15::date,$16,$16,$17,$17)
            RETURNING *
            """,
            _clean_text(action.get("id")) or str(uuid.uuid4()),
            _clean_text(_input_value(action, "title", "name")),
            _clean_text(action.get("description")), _clean_text(action.get("notes")),
            _clean_text(action.get("owner")),
            _clean_text(action.get("severity")) or "medium",
            _clean_text(action.get("status")) or "open",
            _optional_date(_input_value(action, "target_date", "targetDate")),
            _clean_text(_input_value(action, "fix_reference", "fixReference")),
            _clean_text(_input_value(action, "requested_model_group", "requestedModelGroup", "model")),
            _clean_text(action.get("scenario")),
            _clean_text(_input_value(action, "error_code", "errorCode")),
            _clean_text(_input_value(action, "request_id", "requestId")),
            _optional_date(_input_value(action, "baseline_start_date", "baselineStartDate")),
            _optional_date(_input_value(action, "baseline_end_date", "baselineEndDate")),
            _clean_text(_input_value(action, "created_by", "createdBy", "actor")), now,
        )
        return dict(record)

    async def update_stability_action(self, action_id: str, action: dict[str, Any]) -> dict[str, Any] | None:
        current = await self._require_pool().fetchrow("SELECT * FROM stability_actions WHERE id=$1", action_id)
        if current is None:
            return None
        merged = dict(current)
        aliases = {
            "title": ("title", "name"), "description": ("description",), "notes": ("notes",), "owner": ("owner",),
            "severity": ("severity",), "status": ("status",), "target_date": ("target_date", "targetDate"),
            "fix_reference": ("fix_reference", "fixReference"),
            "requested_model_group": ("requested_model_group", "requestedModelGroup", "model"),
            "scenario": ("scenario",), "error_code": ("error_code", "errorCode"),
            "request_id": ("request_id", "requestId"),
            "baseline_start_date": ("baseline_start_date", "baselineStartDate"),
            "baseline_end_date": ("baseline_end_date", "baselineEndDate"),
            "updated_by": ("updated_by", "updatedBy", "actor"),
        }
        for field, names in aliases.items():
            value = _input_value(action, *names, default=None)
            if any(name in action for name in names):
                merged[field] = value
        record = await self._require_pool().fetchrow(
            """
            UPDATE stability_actions SET title=$2, description=$3, notes=$4, owner=$5,
                severity=$6, status=$7, target_date=$8::date, fix_reference=$9,
                requested_model_group=$10, scenario=$11, error_code=$12, request_id=$13,
                baseline_start_date=$14::date, baseline_end_date=$15::date,
                updated_by=$16, updated_at=$17
            WHERE id=$1 RETURNING *
            """,
            action_id, _clean_text(merged["title"]), _clean_text(merged["description"]),
            _clean_text(merged["notes"]), _clean_text(merged["owner"]),
            _clean_text(merged["severity"]) or "medium",
            _clean_text(merged["status"]) or "open", _optional_date(merged["target_date"]),
            _clean_text(merged["fix_reference"]), _clean_text(merged["requested_model_group"]),
            _clean_text(merged["scenario"]), _clean_text(merged["error_code"]),
            _clean_text(merged["request_id"]),
            _optional_date(merged["baseline_start_date"]), _optional_date(merged["baseline_end_date"]),
            _clean_text(merged["updated_by"]), datetime.now(timezone.utc),
        )
        return dict(record) if record else None

    async def delete_stability_action(self, action_id: str) -> bool:
        result = await self._require_pool().execute("DELETE FROM stability_actions WHERE id=$1", action_id)
        return result.endswith(" 1")

    async def list_stability_regressions(self, action_id: str = "", scenario: str = "") -> list[dict[str, Any]]:
        records = await self._require_pool().fetch(
            """
            SELECT * FROM stability_regressions
            WHERE ($1='' OR action_id=$1) AND ($2='' OR scenario=$2)
            ORDER BY regression_end_date DESC, updated_at DESC
            """,
            _clean_text(action_id), _clean_text(scenario),
        )
        return [dict(record) for record in records]

    async def create_stability_regression(self, regression: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        record = await self._require_pool().fetchrow(
            """
            INSERT INTO stability_regressions (
                id, action_id, scenario, baseline_start_date, baseline_end_date,
                regression_start_date, regression_end_date, metric_name,
                baseline_value, regression_value, unit, status, conclusion,
                evidence_url, reviewer, notes, created_at, updated_at
            ) VALUES ($1,$2,$3,$4::date,$5::date,$6::date,$7::date,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$17)
            RETURNING *
            """,
            _clean_text(regression.get("id")) or str(uuid.uuid4()),
            _clean_text(_input_value(regression, "action_id", "actionId")),
            _clean_text(regression.get("scenario")),
            _as_date(_input_value(regression, "baseline_start_date", "baselineStartDate", "baselineStart")),
            _as_date(_input_value(regression, "baseline_end_date", "baselineEndDate", "baselineEnd")),
            _as_date(_input_value(regression, "regression_start_date", "regressionStartDate", "regressionStart")),
            _as_date(_input_value(regression, "regression_end_date", "regressionEndDate", "regressionEnd")),
            _clean_text(_input_value(regression, "metric_name", "metricName", "metric")),
            _input_value(regression, "baseline_value", "baselineValue"),
            _input_value(regression, "regression_value", "regressionValue"),
            _clean_text(regression.get("unit")), _clean_text(regression.get("status")) or "completed",
            _clean_text(regression.get("conclusion")),
            _clean_text(_input_value(regression, "evidence_url", "evidenceUrl")),
            _clean_text(regression.get("reviewer")), _clean_text(regression.get("notes")), now,
        )
        return dict(record)

    async def update_stability_regression(self, regression_id: str, regression: dict[str, Any]) -> dict[str, Any] | None:
        current = await self._require_pool().fetchrow("SELECT * FROM stability_regressions WHERE id=$1", regression_id)
        if current is None:
            return None
        merged = dict(current)
        aliases = {
            "action_id": ("action_id", "actionId"), "scenario": ("scenario",),
            "baseline_start_date": ("baseline_start_date", "baselineStartDate", "baselineStart"),
            "baseline_end_date": ("baseline_end_date", "baselineEndDate", "baselineEnd"),
            "regression_start_date": ("regression_start_date", "regressionStartDate", "regressionStart"),
            "regression_end_date": ("regression_end_date", "regressionEndDate", "regressionEnd"),
            "metric_name": ("metric_name", "metricName", "metric"), "baseline_value": ("baseline_value", "baselineValue"),
            "regression_value": ("regression_value", "regressionValue"), "unit": ("unit",),
            "status": ("status",), "conclusion": ("conclusion",),
            "evidence_url": ("evidence_url", "evidenceUrl"), "reviewer": ("reviewer",), "notes": ("notes",),
        }
        for field, names in aliases.items():
            if any(name in regression for name in names):
                merged[field] = _input_value(regression, *names)
        record = await self._require_pool().fetchrow(
            """
            UPDATE stability_regressions SET action_id=$2, scenario=$3,
                baseline_start_date=$4::date, baseline_end_date=$5::date,
                regression_start_date=$6::date, regression_end_date=$7::date,
                metric_name=$8, baseline_value=$9, regression_value=$10,
                unit=$11, status=$12, conclusion=$13, evidence_url=$14,
                reviewer=$15, notes=$16, updated_at=$17
            WHERE id=$1 RETURNING *
            """,
            regression_id, _clean_text(merged["action_id"]), _clean_text(merged["scenario"]),
            _as_date(merged["baseline_start_date"]), _as_date(merged["baseline_end_date"]),
            _as_date(merged["regression_start_date"]), _as_date(merged["regression_end_date"]),
            _clean_text(merged["metric_name"]), merged["baseline_value"], merged["regression_value"],
            _clean_text(merged["unit"]), _clean_text(merged["status"]) or "pending",
            _clean_text(merged["conclusion"]), _clean_text(merged["evidence_url"]),
            _clean_text(merged["reviewer"]), _clean_text(merged["notes"]), datetime.now(timezone.utc),
        )
        return dict(record) if record else None

    async def delete_stability_regression(self, regression_id: str) -> bool:
        result = await self._require_pool().execute("DELETE FROM stability_regressions WHERE id=$1", regression_id)
        return result.endswith(" 1")

    async def stability_events(
        self,
        start_date: str,
        end_date: str,
        model: str = "",
    ) -> list[dict[str, Any]]:
        records = await self._require_pool().fetch(
            """
            SELECT backend_id, request_id, event_time, usage_date, raw_user_id,
                   organization_id, team_id, key_id, principal_id, source, model,
                   prompt_tokens, completion_tokens, total_tokens, spend, provider,
                   model_group, model_id, api_base, status, error_code, error_class,
                   error_message, scenario, request_duration_ms, ttft_ms,
                   attempted_retries, max_retries, trace_id, user_visible_failure,
                   final_failure_source, collected_at
            FROM usage_event_attribution
            WHERE usage_date BETWEEN $1::date AND $2::date
              AND ($3='' OR model=$3 OR model_group=$3)
            ORDER BY event_time DESC
            """,
            _as_date(start_date),
            _as_date(end_date),
            _clean_text(model),
        )
        return [dict(record) for record in records]

    async def stability_overview_aggregates(
        self, start_date: str, end_date: str, model: str = ""
    ) -> dict[str, Any]:
        """Return compact SQL aggregates for the stability overview."""

        pool = self._require_pool()
        args = (_as_date(start_date), _as_date(end_date), _clean_text(model))
        base = """
            usage_date BETWEEN $1::date AND $2::date
              AND ($3='' OR model=$3 OR model_group=$3)
        """
        select_metrics = """
            COUNT(*)::bigint AS request_count,
            COUNT(*) FILTER (WHERE status IN ('success','failure'))::bigint AS status_count,
            COUNT(*) FILTER (WHERE final_failure_source='explicit')::bigint AS explicit_count,
            COUNT(*) FILTER (WHERE final_failure_source='explicit' AND user_visible_failure IS TRUE)::bigint AS explicit_failure_count,
            COUNT(*) FILTER (WHERE user_visible_failure IS NOT NULL)::bigint AS failure_known_count,
            COUNT(*) FILTER (WHERE user_visible_failure IS TRUE)::bigint AS failure_count,
            COUNT(*) FILTER (WHERE attempted_retries IS NOT NULL)::bigint AS retry_known_count,
            COUNT(*) FILTER (WHERE attempted_retries > 0)::bigint AS retry_count,
            COUNT(*) FILTER (WHERE attempted_retries > 0 AND status='success')::bigint AS retry_recovered_count,
            COUNT(ttft_ms)::bigint AS ttft_sample_count,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY ttft_ms) FILTER (WHERE ttft_ms IS NOT NULL)::double precision AS ttft_p95_ms,
            MAX(event_time) FILTER (WHERE ttft_ms IS NOT NULL) AS ttft_latest_at,
            MAX(collected_at) AS latest_collected_at
        """
        overall_query = pool.fetchrow(
            f"SELECT {select_metrics} FROM usage_event_attribution WHERE {base}", *args
        )
        daily_query = pool.fetch(
            f"SELECT usage_date AS dimension, {select_metrics} FROM usage_event_attribution WHERE {base} GROUP BY usage_date ORDER BY usage_date",
            *args,
        )
        models_query = pool.fetch(
            f"SELECT COALESCE(NULLIF(model_group,''), NULLIF(model,''), 'unknown') AS dimension, {select_metrics} FROM usage_event_attribution WHERE {base} GROUP BY 1",
            *args,
        )
        scenarios_query = pool.fetch(
            f"""
            SELECT COALESCE(NULLIF(model_group,''), NULLIF(model,''), 'unknown') AS requested_model_group,
                   COALESCE(NULLIF(scenario,''), 'unknown') AS scenario,
                   COALESCE(error_code,'') AS error_code,
                   COUNT(*)::bigint AS count,
                   (ARRAY_AGG(request_id ORDER BY event_time DESC))[1:5] AS sample_request_ids,
                   {select_metrics}
            FROM usage_event_attribution
            WHERE {base} AND (
                user_visible_failure IS TRUE OR attempted_retries > 0 OR
                COALESCE(error_code,'') <> '' OR COALESCE(error_class,'') <> '' OR
                COALESCE(scenario,'unknown') <> 'unknown'
            )
            GROUP BY 1,2,3 ORDER BY count DESC
            """,
            *args,
        )
        attempt_args = args
        attempt_base = """
            event_date BETWEEN $1::date AND $2::date
              AND ($3='' OR requested_model_group=$3 OR actual_model=$3)
        """
        terminal_attempts = f"""
            SELECT DISTINCT ON (
                backend_id,
                COALESCE(NULLIF(trace_id,''), NULLIF(request_id,''), event_id),
                COALESCE(NULLIF(attempt_id,''), attempt_index::text || ':' || actual_model || ':' || route_name)
            )
                backend_id, event_id, event_date, requested_model_group,
                actual_model, trace_id, request_id, attempt_id, attempt_index,
                route_name, status, event_type, fallback_from, fallback_to,
                is_fallback, is_retry, started_at, event_time, ended_at
            FROM stability_attempt_events WHERE {attempt_base}
            ORDER BY backend_id,
                     COALESCE(NULLIF(trace_id,''), NULLIF(request_id,''), event_id),
                     COALESCE(NULLIF(attempt_id,''), attempt_index::text || ':' || actual_model || ':' || route_name),
                     COALESCE(ended_at,event_time) DESC, event_id DESC
        """
        attempt_summary_query = pool.fetchrow(
            f"""
            WITH terminal AS MATERIALIZED ({terminal_attempts}),
            traces AS MATERIALIZED (
                SELECT COALESCE(NULLIF(requested_model_group,''), NULLIF(actual_model,''), 'unknown') AS dimension,
                       backend_id, COALESCE(NULLIF(trace_id,''), NULLIF(request_id,''), event_id) AS trace_key,
                       BOOL_OR(is_fallback OR event_type LIKE 'fallback_%' OR fallback_from<>'' OR fallback_to<>'') AS fallback_triggered,
                       BOOL_OR((is_fallback OR event_type LIKE 'fallback_%' OR fallback_from<>'' OR fallback_to<>'') AND status='success') AS fallback_recovered,
                       BOOL_OR(is_retry OR event_type LIKE 'retry_%' OR (attempt_index>0 AND NOT is_fallback)) AS retry_triggered,
                       BOOL_OR((is_retry OR event_type LIKE 'retry_%' OR (attempt_index>0 AND NOT is_fallback)) AND status='success') AS retry_recovered
                FROM terminal GROUP BY 1,2,3
            ), overall AS (
                SELECT (SELECT COUNT(*) FROM terminal)::bigint AS attempt_count,
                       (SELECT COUNT(*) FROM terminal WHERE status IN ('success','failure'))::bigint AS attempt_status_count,
                       (SELECT COUNT(*) FROM terminal WHERE status='failure')::bigint AS failed_attempt_count,
                       COUNT(*) FILTER (WHERE fallback_triggered)::bigint AS fallback_count,
                       COUNT(*) FILTER (WHERE fallback_recovered)::bigint AS fallback_recovered_count,
                       COUNT(*) FILTER (WHERE retry_triggered)::bigint AS retry_count,
                       COUNT(*) FILTER (WHERE retry_recovered)::bigint AS retry_recovered_count,
                       (SELECT MIN(COALESCE(started_at,event_time)) FROM terminal) AS available_from FROM traces
            ), terminal_model_totals AS (
                SELECT COALESCE(NULLIF(requested_model_group,''), NULLIF(actual_model,''), 'unknown') AS dimension,
                       COUNT(*)::bigint AS attempt_count,
                       COUNT(*) FILTER (WHERE status IN ('success','failure'))::bigint AS attempt_status_count
                FROM terminal GROUP BY 1
            ), trace_model_totals AS (
                SELECT dimension, COUNT(*) FILTER (WHERE fallback_triggered)::bigint AS fallback_count,
                       COUNT(*) FILTER (WHERE fallback_recovered)::bigint AS fallback_recovered_count
                FROM traces GROUP BY dimension
            ), by_model AS (
                SELECT terminal_model_totals.dimension, terminal_model_totals.attempt_count,
                       terminal_model_totals.attempt_status_count,
                       COALESCE(trace_model_totals.fallback_count, 0)::bigint AS fallback_count,
                       COALESCE(trace_model_totals.fallback_recovered_count, 0)::bigint AS fallback_recovered_count
                FROM terminal_model_totals LEFT JOIN trace_model_totals USING (dimension)
            ), by_day AS (
                SELECT event_date AS dimension, COUNT(*)::bigint AS attempt_count,
                       COUNT(*) FILTER (WHERE status IN ('success','failure'))::bigint AS attempt_status_count,
                       COUNT(*) FILTER (WHERE status='failure')::bigint AS failed_attempt_count
                FROM terminal GROUP BY event_date
            )
            SELECT row_to_json(overall) AS attempts,
                   COALESCE((SELECT json_agg(by_model) FROM by_model), '[]'::json) AS model_attempts,
                   COALESCE((SELECT json_agg(by_day ORDER BY dimension) FROM by_day), '[]'::json) AS daily_attempts
            FROM overall
            """, *attempt_args,
        )
        overall, daily, models, scenarios, attempt_summary = await asyncio.gather(
            overall_query, daily_query, models_query, scenarios_query, attempt_summary_query,
        )
        summary = dict(attempt_summary or {})

        def decode_json(value: Any, fallback: Any) -> Any:
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    return fallback
            return value if value is not None else fallback

        attempts = decode_json(summary.get("attempts"), {})
        model_attempts = decode_json(summary.get("model_attempts"), [])
        daily_attempts = decode_json(summary.get("daily_attempts"), [])
        return {
            "overall": dict(overall or {}),
            "daily": [dict(item) for item in daily],
            "models": [dict(item) for item in models],
            "scenarios": [dict(item) for item in scenarios],
            "attempts": dict(attempts or {}),
            "dailyAttempts": [dict(item) for item in daily_attempts],
            "modelAttempts": [dict(item) for item in model_attempts],
        }

    async def stability_scenario_samples(
        self,
        start_date: str,
        end_date: str,
        *,
        model: str = "",
        scenario: str = "",
        error_code: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        offset = (max(1, page) - 1) * page_size
        pool = self._require_pool()
        # model 筛选同时匹配 model 与 model_group，与 overview 排行口径一致，
        # 保证用模型组名也能筛出样本（model 列可能是占位符“未知模型”）。
        filters = """
            usage_date BETWEEN $1::date AND $2::date
              AND ($3='' OR model=$3 OR model_group=$3)
              AND ($4='' OR scenario=$4)
              AND ($5='' OR error_code=$5)
              AND (
                    user_visible_failure=TRUE
                 OR attempted_retries > 0
                 OR COALESCE(error_code, '') <> ''
                 OR COALESCE(error_class, '') <> ''
                 OR COALESCE(scenario, 'unknown') <> 'unknown'
              )
        """
        args = (
            _as_date(start_date),
            _as_date(end_date),
            _clean_text(model),
            _clean_text(scenario),
            _clean_text(error_code),
        )
        filtered_cte = """
            WITH filtered AS MATERIALIZED (
                SELECT request_id, backend_id, event_time, model, model_group, scenario, error_code,
                       status, user_visible_failure, attempted_retries, ttft_ms
                FROM usage_event_attribution WHERE """ + filters + """
            )
        """
        total_query = pool.fetchval(filtered_cte + "SELECT COUNT(*) FROM filtered", *args)
        records_query = pool.fetch(filtered_cte + """
            SELECT request_id, backend_id, event_time, model, scenario, error_code,
                   status, user_visible_failure, attempted_retries, ttft_ms
            FROM filtered ORDER BY event_time DESC LIMIT $6 OFFSET $7
            """, *args, page_size, offset)
        # 模型选项与样本同口径（窗口 + 场景 + 错误码 + 异常条件），但不受 model
        # 筛选约束；合并 model 与 model_group 的 distinct 值，按出现次数降序。
        option_filters = """
            usage_date BETWEEN $1::date AND $2::date
              AND ($3='' OR scenario=$3)
              AND ($4='' OR error_code=$4)
              AND (
                    user_visible_failure=TRUE
                 OR attempted_retries > 0
                 OR COALESCE(error_code, '') <> ''
                 OR COALESCE(error_class, '') <> ''
                 OR COALESCE(scenario, 'unknown') <> 'unknown'
              )
        """
        option_args = (
            _as_date(start_date),
            _as_date(end_date),
            _clean_text(scenario),
            _clean_text(error_code),
        )
        option_rows_query = pool.fetch("""
            WITH filtered AS MATERIALIZED (
                SELECT model, model_group FROM usage_event_attribution WHERE """ + option_filters + """
            )
            SELECT name, COUNT(*)::bigint AS n FROM (
                SELECT NULLIF(model, '') AS name FROM filtered
                UNION ALL SELECT NULLIF(model_group, '') AS name FROM filtered
            ) names WHERE name IS NOT NULL GROUP BY name ORDER BY n DESC, name LIMIT $5
            """, *option_args, 100)
        total, records, option_rows = await asyncio.gather(total_query, records_query, option_rows_query)
        return {
            "items": [dict(record) for record in records],
            "total": int(total or 0),
            "modelOptions": [{"name": str(row["name"]), "count": int(row["n"] or 0)} for row in option_rows],
        }

    async def stability_request(self, request_id: str, backend_id: str = "") -> dict[str, Any] | None:
        record = await self._require_pool().fetchrow(
            """
            SELECT backend_id, request_id, event_time, raw_user_id, organization_id,
                   team_id, key_id, principal_id, source, model, prompt_tokens,
                   completion_tokens, total_tokens, spend, provider, model_group,
                   model_id, api_base, status, error_code, error_class, error_message,
                   scenario, request_duration_ms, ttft_ms, attempted_retries,
                   max_retries, trace_id, user_visible_failure, final_failure_source, collected_at
            FROM usage_event_attribution
            WHERE request_id=$1 AND ($2='' OR backend_id=$2)
            ORDER BY event_time DESC LIMIT 1
            """,
            request_id,
            _clean_text(backend_id),
        )
        return dict(record) if record else None

    async def stability_sync_states(self) -> list[dict[str, Any]]:
        records = await self._require_pool().fetch(
            "SELECT * FROM stability_sync_state ORDER BY backend_id"
        )
        return [dict(record) for record in records]

    async def api_cost_rows(
        self,
        start_date: str,
        end_date: str,
        *,
        model: str = "",
        vendor: str = "",
        account_id: str = "",
    ) -> list[dict[str, Any]]:
        records = await self._require_pool().fetch(
            """
            SELECT usage_date, backend_id, user_id, organization_id, team_id,
                   key_id, principal_id, source, model,
                   SUM(spend)::double precision AS spend
            FROM usage_query_daily
            WHERE usage_date BETWEEN $1::date AND $2::date
              AND ($3='' OR model=$3)
              AND ($4='' OR source=$4)
              AND ($5='' OR user_id=$5 OR key_id=$5 OR principal_id=$5)
            GROUP BY usage_date, backend_id, user_id, organization_id, team_id,
                     key_id, principal_id, source, model
            ORDER BY usage_date, spend DESC
            """,
            _as_date(start_date),
            _as_date(end_date),
            _clean_text(model),
            _clean_text(vendor),
            _clean_text(account_id),
        )
        return [dict(record) for record in records]

    async def api_cost_ledger_rows(
        self,
        start_date: str,
        end_date: str,
        *,
        model: str = "",
        provider: str = "",
        account_id: str = "",
    ) -> list[dict[str, Any]]:
        records = await self._require_pool().fetch(
            """
            SELECT usage_date, backend_id, account_id AS user_id, organization_id,
                   team_id, key_id, principal_id, source, model, provider,
                   model_group, model_id, api_base,
                   spend::double precision AS spend, request_count
            FROM cost_api_daily
            WHERE usage_date BETWEEN $1::date AND $2::date
              AND ($3='' OR model=$3 OR model_group=$3)
              AND ($4='' OR provider=$4)
              AND ($5='' OR account_id=$5 OR key_id=$5 OR principal_id=$5)
            ORDER BY usage_date, spend DESC
            """,
            _as_date(start_date),
            _as_date(end_date),
            _clean_text(model),
            _clean_text(provider),
            _clean_text(account_id),
        )
        return [dict(record) for record in records]

    async def cost_ledger_page(
        self, start_date: str, end_date: str, *, page: int = 1, page_size: int = 100,
        cost_bucket: str = "", category: str = "", provider: str = "", vendor: str = "",
        model: str = "", canonical_model: str = "", account_id: str = "", reconciliation_status: str = "",
        recognition_status: str = "",
    ) -> dict[str, Any]:
        """Return a unified, already paginated ledger page from PostgreSQL."""

        page = max(1, int(page))
        page_size = max(1, min(500, int(page_size)))
        offset = (page - 1) * page_size
        rows = await self._require_pool().fetch(
            """
            WITH ledger AS (
                SELECT usage_date AS ledger_date, spend::double precision AS amount_usd,
                       'api_usage' AS source_type, 'api_usage' AS cost_bucket,
                       'API Token' AS category, model AS name, model, provider,
                       source AS vendor, account_id, organization_id, team_id, principal_id,
                       'actual' AS recognition_status, NULL::text AS reconciliation_status,
                       backend_id, key_id, NULL::text AS source_item_id, NULL::text AS request_id, source
                FROM cost_api_daily
                WHERE usage_date BETWEEN $1::date AND $2::date
                  AND NOT EXISTS (SELECT 1 FROM usage_event_attribution e WHERE e.usage_date=cost_api_daily.usage_date AND NULLIF(e.request_id,'') IS NOT NULL)
                  AND ($3='' OR model=$3 OR model_group=$3)
                  AND ($4='' OR COALESCE(NULLIF(model_group,''), NULLIF(model,''), '未知模型')=$4)
                  AND ($5='' OR provider=$5)
                  AND ($6='' OR account_id=$6 OR key_id=$6 OR principal_id=$6)
                  AND ($7='' OR $7='API Token') AND ($8='' OR $8='api_usage')
                  AND ($9='' OR $9='actual')
                  AND ($10='' OR source=$10)
                UNION ALL
                SELECT usage_date, spend::double precision, 'api_usage', 'api_usage', 'API Token',
                       COALESCE(NULLIF(model_group,''), model), model, provider, source, raw_user_id,
                       organization_id, team_id, principal_id, 'actual', NULL,
                       backend_id, key_id, NULL, request_id, source
                FROM usage_event_attribution
                WHERE usage_date BETWEEN $1::date AND $2::date
                  AND NULLIF(request_id,'') IS NOT NULL
                  AND ($3='' OR model=$3 OR model_group=$3)
                  AND ($4='' OR COALESCE(NULLIF(model_group,''), NULLIF(model,''), '未知模型')=$4)
                  AND ($5='' OR provider=$5) AND ($6='' OR raw_user_id=$6 OR key_id=$6 OR principal_id=$6)
                  AND ($7='' OR $7='API Token') AND ($8='' OR $8='api_usage') AND ($9='' OR $9='actual') AND ($10='' OR source=$10)
                UNION ALL
                SELECT day::date, (amount_usd / GREATEST(1, service_end_date-service_start_date+1))::double precision,
                       COALESCE(source_type,'manual'), CASE COALESCE(NULLIF(cost_bucket,''), category)
                           WHEN 'subscription' THEN 'account_procurement' WHEN 'account_purchase' THEN 'account_procurement'
                           WHEN 'fallback' THEN 'fallback_channel' WHEN 'backup_api' THEN 'fallback_channel'
                           WHEN 'feishu' THEN 'feishu_surrounding' WHEN 'surrounding' THEN 'feishu_surrounding'
                           WHEN 'infra' THEN 'infrastructure' WHEN 'labor' THEN 'other' WHEN 'support' THEN 'other'
                           ELSE COALESCE(NULLIF(cost_bucket,''), category) END, category, name, model,
                       COALESCE(NULLIF(provider,''), vendor), vendor, account_id, '' AS organization_id, '' AS team_id, '' AS principal_id,
                       COALESCE(recognition_status,'actual'), COALESCE(reconciliation_status,'unreconciled'),
                       NULL, NULL, id, NULL, '' AS source
                FROM cost_items, generate_series(GREATEST(service_start_date,$1::date), LEAST(service_end_date,$2::date), interval '1 day') day
                WHERE enabled AND service_start_date <= $2::date AND service_end_date >= $1::date
                  AND ($3='' OR model=$3) AND ($5='' OR COALESCE(NULLIF(provider,''),vendor)=$5)
                  AND ($6='' OR account_id=$6) AND ($7='' AND $8='' OR ($7<>'API Token' AND ($7='' OR category=$7)) AND ($8='' OR CASE COALESCE(NULLIF(cost_bucket,''), category) WHEN 'subscription' THEN 'account_procurement' WHEN 'account_purchase' THEN 'account_procurement' WHEN 'fallback' THEN 'fallback_channel' WHEN 'backup_api' THEN 'fallback_channel' WHEN 'feishu' THEN 'feishu_surrounding' WHEN 'surrounding' THEN 'feishu_surrounding' WHEN 'infra' THEN 'infrastructure' WHEN 'labor' THEN 'other' WHEN 'support' THEN 'other' ELSE COALESCE(NULLIF(cost_bucket,''),category) END=$8))
                  AND ($9='' OR COALESCE(recognition_status,'actual')=$9) AND ($10='' OR vendor=$10)
            ),
            paged AS (
                SELECT ledger_date, amount_usd, source_type, cost_bucket, category, name, model, provider, vendor,
                       account_id, organization_id, team_id, principal_id, recognition_status, reconciliation_status,
                       backend_id, key_id, source_item_id, request_id, source
                FROM ledger ORDER BY ledger_date DESC, amount_usd DESC, name LIMIT $11 OFFSET $12
            )
            SELECT (SELECT COUNT(*) FROM ledger) AS total_count, paged.* FROM paged
            UNION ALL SELECT (SELECT COUNT(*) FROM ledger), NULL::date, NULL::double precision, NULL::text, NULL::text,
                NULL::text, NULL::text, NULL::text, NULL::text, NULL::text, NULL::text, NULL::text, NULL::text,
                NULL::text, NULL::text, NULL::text, NULL::text, NULL::text, NULL::text, NULL::text, NULL::text
            WHERE NOT EXISTS (SELECT 1 FROM paged)
            """,
            _as_date(start_date), _as_date(end_date), _clean_text(model), _clean_text(canonical_model), _clean_text(provider), _clean_text(account_id),
            _clean_text(category), _clean_text(cost_bucket), _clean_text(recognition_status or "actual"), _clean_text(vendor), page_size, offset,
        )
        total = int(rows[0]["total_count"] or 0) if rows else 0
        items = [dict(row) for row in rows if row.get("ledger_date", row.get("usage_date")) is not None]
        return {"items": items, "total": total, "page": page, "pageSize": page_size, "totalPages": (total + page_size - 1) // page_size if total else 0}

    async def rebuild_cost_api_daily(self, start_date: str | date, end_date: str | date) -> int:
        """Replace only the affected daily cost aggregates."""

        start = _as_date(start_date)
        end = _as_date(end_date)
        async with self._require_pool().acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM cost_api_daily WHERE usage_date BETWEEN $1::date AND $2::date",
                    start,
                    end,
                )
                result = await connection.execute(
                    """
                    INSERT INTO cost_api_daily (
                        usage_date, backend_id, account_id, organization_id, team_id,
                        key_id, principal_id, source, model, provider, model_group,
                        model_id, api_base, spend, request_count, refreshed_at
                    )
                    SELECT usage_date, backend_id, raw_user_id, organization_id, team_id,
                           key_id, principal_id, source, model, COALESCE(provider, ''),
                           COALESCE(model_group, ''), COALESCE(model_id, ''),
                           COALESCE(api_base, ''), SUM(spend), COUNT(*), NOW()
                    FROM usage_event_attribution
                    WHERE usage_date BETWEEN $1::date AND $2::date
                    GROUP BY usage_date, backend_id, raw_user_id, organization_id,
                             team_id, key_id, principal_id, source, model,
                             COALESCE(provider, ''), COALESCE(model_group, ''),
                             COALESCE(model_id, ''), COALESCE(api_base, '')
                    """,
                    start,
                    end,
                )
        return int(result.rsplit(" ", 1)[-1])

    async def cost_api_daily_bounds(self) -> dict[str, Any]:
        record = await self._require_pool().fetchrow(
            "SELECT MIN(usage_date) AS start_date, MAX(usage_date) AS end_date, COUNT(*) AS row_count FROM cost_api_daily"
        )
        return dict(record) if record else {"start_date": None, "end_date": None, "row_count": 0}

    async def next_cost_api_backfill_range(self, batch_days: int = 7) -> dict[str, Any] | None:
        record = await self._require_pool().fetchrow(
            """
            WITH event_days AS (
                SELECT DISTINCT usage_date FROM usage_event_attribution
            ), missing AS (
                SELECT e.usage_date FROM event_days e
                WHERE NOT EXISTS (
                    SELECT 1 FROM cost_api_daily c WHERE c.usage_date=e.usage_date
                )
            )
            SELECT MIN(usage_date) AS start_date,
                   LEAST(MAX(usage_date), MIN(usage_date) + ($1::int - 1)) AS end_date
            FROM missing
            """,
            max(1, int(batch_days)),
        )
        if not record or record["start_date"] is None:
            return None
        return dict(record)

    async def get_observability_snapshot(self, dashboard_type: str, snapshot_key: str) -> dict[str, Any] | None:
        record = await self._require_pool().fetchrow(
            "SELECT * FROM observability_dashboard_snapshots WHERE dashboard_type=$1 AND snapshot_key=$2",
            _clean_text(dashboard_type),
            _clean_text(snapshot_key),
        )
        return dict(record) if record else None

    async def save_observability_snapshot(
        self,
        dashboard_type: str,
        snapshot_key: str,
        payload: dict[str, Any],
        *,
        data_revision: str = "",
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        record = await self._require_pool().fetchrow(
            """
            INSERT INTO observability_dashboard_snapshots (
                dashboard_type, snapshot_key, payload, generated_at, data_revision,
                refreshing, last_refresh_error, updated_at
            ) VALUES ($1,$2,$3::jsonb,$4,$5,FALSE,'',$4)
            ON CONFLICT (dashboard_type, snapshot_key) DO UPDATE SET
                payload=EXCLUDED.payload, generated_at=EXCLUDED.generated_at,
                data_revision=EXCLUDED.data_revision, refreshing=FALSE,
                last_refresh_error='', updated_at=EXCLUDED.updated_at
            RETURNING *
            """,
            _clean_text(dashboard_type), _clean_text(snapshot_key),
            json.dumps(payload, ensure_ascii=False), now, _clean_text(data_revision),
        )
        return dict(record)

    async def mark_observability_snapshot_refresh(
        self, dashboard_type: str, snapshot_key: str, *, refreshing: bool, error: str = ""
    ) -> None:
        await self._require_pool().execute(
            """
            UPDATE observability_dashboard_snapshots
            SET refreshing=$3, last_refresh_error=$4, updated_at=NOW()
            WHERE dashboard_type=$1 AND snapshot_key=$2
            """,
            _clean_text(dashboard_type), _clean_text(snapshot_key), bool(refreshing), _clean_text(error)[:500],
        )

    async def delete_observability_snapshots(self, dashboard_type: str) -> int:
        result = await self._require_pool().execute(
            "DELETE FROM observability_dashboard_snapshots WHERE dashboard_type=$1",
            _clean_text(dashboard_type),
        )
        return int(result.rsplit(" ", 1)[-1])

    async def list_cost_items(
        self,
        *,
        as_of: str | date | None = None,
        model: str = "",
        provider: str = "",
        account_id: str = "",
        cost_bucket: str = "",
        recognition_status: str = "",
        plan_version_id: str = "",
        actual_only: bool = False,
    ) -> list[dict[str, Any]]:
        status = "actual" if actual_only else _clean_text(recognition_status)
        pool = self._require_pool()
        try:
            records = await pool.fetch(
                """
                SELECT * FROM cost_items
                WHERE ($1::date IS NULL OR service_start_date <= $1::date)
                  AND ($2='' OR model=$2) AND ($3='' OR provider=$3)
                  AND ($4='' OR account_id=$4) AND ($5='' OR cost_bucket=$5)
                  AND ($6='' OR recognition_status=$6)
                  AND ($7='' OR plan_version_id=$7)
                ORDER BY service_start_date DESC, name
                """,
                _optional_date(as_of), _clean_text(model), _clean_text(provider),
                _clean_text(account_id), _clean_text(cost_bucket), status,
                _clean_text(plan_version_id),
            )
        except TypeError:
            # Some lightweight adapters only implement the legacy no-argument query.
            if any((as_of, model, provider, account_id, cost_bucket, status, plan_version_id)):
                raise
            records = await pool.fetch("SELECT * FROM cost_items ORDER BY service_start_date DESC, name")
        return [dict(record) for record in records]

    async def list_actual_cost_items(
        self,
        as_of: str | date,
        *,
        model: str = "",
        provider: str = "",
        account_id: str = "",
        cost_bucket: str = "",
    ) -> list[dict[str, Any]]:
        return await self.list_cost_items(
            as_of=as_of, model=model, provider=provider, account_id=account_id,
            cost_bucket=cost_bucket, actual_only=True,
        )

    async def create_cost_item(self, item: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        record = await self._require_pool().fetchrow(
            """
            INSERT INTO cost_items (id, category, name, vendor, model, business_scope,
                amount, currency, exchange_rate, amount_usd, service_start_date,
                service_end_date, finance_bucket, notes, enabled, created_at, updated_at,
                cost_bucket, source_type, provider, account_id, account_name, voucher_id,
                voucher_no, invoice_no, recognition_status, reconciliation_status,
                plan_version_id, scenario, source_evidence)
            VALUES ($1,$2,$3,$4,$5,$6,$7::numeric,$8,$9::numeric,$10::numeric,$11::date,$12::date,$13,$14,$15,$16,$16,
                $17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29)
            RETURNING *
            """,
            item["id"], item["category"], item["name"], item.get("vendor", ""),
            item.get("model", ""), item.get("businessScope", ""), str(item["amount"]),
            item["currency"], str(item["exchangeRate"]), str(item["amountUsd"]),
            _as_date(item["serviceStartDate"]), _as_date(item["serviceEndDate"]),
            item.get("financeBucket", ""), item.get("notes", ""), bool(item.get("enabled", True)), now,
            item.get("costBucket", ""), item.get("sourceType", "manual"),
            item.get("provider", ""), item.get("accountId", ""), item.get("accountName", ""),
            item.get("voucherId", ""), item.get("voucherNo", ""), item.get("invoiceNo", ""),
            item.get("recognitionStatus", "actual"), item.get("reconciliationStatus", "unreconciled"),
            _input_value(item, "planVersionId", "plan_version_id"),
            _clean_text(item.get("scenario")),
            _clean_text(_input_value(item, "sourceEvidence", "source_evidence")),
        )
        return dict(record)

    async def update_cost_item(self, item_id: str, item: dict[str, Any]) -> dict[str, Any] | None:
        record = await self._require_pool().fetchrow(
            """
            UPDATE cost_items SET category=$2, name=$3, vendor=$4, model=$5,
                business_scope=$6, amount=$7::numeric, currency=$8,
                exchange_rate=$9::numeric, amount_usd=$10::numeric,
                service_start_date=$11::date, service_end_date=$12::date,
                finance_bucket=$13, notes=$14, enabled=$15, updated_at=$16,
                cost_bucket=$17, source_type=$18, provider=$19, account_id=$20,
                account_name=$21, voucher_id=$22, voucher_no=$23, invoice_no=$24,
                recognition_status=$25, reconciliation_status=$26,
                plan_version_id=$27, scenario=$28, source_evidence=$29
            WHERE id=$1 RETURNING *
            """,
            item_id, item["category"], item["name"], item.get("vendor", ""),
            item.get("model", ""), item.get("businessScope", ""), str(item["amount"]),
            item["currency"], str(item["exchangeRate"]), str(item["amountUsd"]),
            _as_date(item["serviceStartDate"]), _as_date(item["serviceEndDate"]),
            item.get("financeBucket", ""), item.get("notes", ""), bool(item.get("enabled", True)),
            datetime.now(timezone.utc),
            item.get("costBucket", ""), item.get("sourceType", "manual"),
            item.get("provider", ""), item.get("accountId", ""), item.get("accountName", ""),
            item.get("voucherId", ""), item.get("voucherNo", ""), item.get("invoiceNo", ""),
            item.get("recognitionStatus", "actual"), item.get("reconciliationStatus", "unreconciled"),
            _input_value(item, "planVersionId", "plan_version_id"),
            _clean_text(item.get("scenario")),
            _clean_text(_input_value(item, "sourceEvidence", "source_evidence")),
        )
        return dict(record) if record else None

    async def delete_cost_item(self, item_id: str) -> bool:
        result = await self._require_pool().execute("DELETE FROM cost_items WHERE id=$1", item_id)
        return result.endswith("1")

    async def list_cost_plan_versions(
        self,
        year: int | None = None,
        status: str = "",
        scenario: str = "",
    ) -> list[dict[str, Any]]:
        records = await self._require_pool().fetch(
            """
            SELECT * FROM cost_plan_versions
            WHERE ($1::integer IS NULL OR year=$1)
              AND ($2='' OR status=$2) AND ($3='' OR scenario=$3)
            ORDER BY year DESC, is_active DESC, updated_at DESC
            """,
            int(year) if year is not None else None,
            _clean_text(status), _clean_text(scenario),
        )
        return [dict(record) for record in records]

    async def create_cost_plan_version(self, plan: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        actor = _clean_text(_input_value(plan, "created_by", "createdBy", "actor"))
        record = await self._require_pool().fetchrow(
            """
            INSERT INTO cost_plan_versions (
                id, year, version, scenario, as_of, status, is_active,
                created_by, updated_by, coverage_complete, coverage_notes, notes,
                created_at, updated_at
            ) VALUES ($1,$2,$3,$4,$5::date,'draft',FALSE,$6,$6,$7,$8,$9,$10,$10)
            RETURNING *
            """,
            _clean_text(plan.get("id")) or str(uuid.uuid4()),
            int(_input_value(plan, "year", "plan_year", "planYear")),
            _clean_text(plan.get("version")),
            _clean_text(plan.get("scenario")) or "baseline",
            _as_date(_input_value(plan, "as_of", "as_of_date", "asOfDate", "asOf")),
            actor,
            _as_bool(_input_value(plan, "coverage_complete", "coverageComplete", default=False)),
            _clean_text(_input_value(plan, "coverage_notes", "coverageNotes")),
            _clean_text(plan.get("notes")), now,
        )
        return dict(record)

    async def update_cost_plan_version(self, plan_id: str, plan: dict[str, Any]) -> dict[str, Any] | None:
        current = await self._require_pool().fetchrow("SELECT * FROM cost_plan_versions WHERE id=$1", plan_id)
        if current is None:
            return None
        if current["status"] != "draft":
            raise ValueError("仅草稿计划可以编辑")
        merged = dict(current)
        aliases = {
            "year": ("year", "plan_year", "planYear"), "version": ("version",),
            "scenario": ("scenario",), "as_of": ("as_of", "as_of_date", "asOfDate", "asOf"),
            "coverage_complete": ("coverage_complete", "coverageComplete"),
            "coverage_notes": ("coverage_notes", "coverageNotes"), "notes": ("notes",),
            "updated_by": ("updated_by", "updatedBy", "actor"),
        }
        for field, names in aliases.items():
            if any(name in plan for name in names):
                merged[field] = _input_value(plan, *names)
        record = await self._require_pool().fetchrow(
            """
            UPDATE cost_plan_versions SET year=$2, version=$3, scenario=$4,
                as_of=$5::date, coverage_complete=$6, coverage_notes=$7,
                notes=$8, updated_by=$9, updated_at=$10
            WHERE id=$1 RETURNING *
            """,
            plan_id, int(merged["year"]), _clean_text(merged["version"]),
            _clean_text(merged["scenario"]) or "baseline", _as_date(merged["as_of"]),
            _as_bool(merged["coverage_complete"]),
            _clean_text(merged["coverage_notes"]), _clean_text(merged["notes"]),
            _clean_text(merged["updated_by"]), datetime.now(timezone.utc),
        )
        return dict(record) if record else None

    async def approve_cost_plan_version(self, plan_id: str, actor: str) -> dict[str, Any] | None:
        record = await self._require_pool().fetchrow(
            """
            UPDATE cost_plan_versions SET status='approved', is_active=FALSE,
                approved_by=$2, approved_at=$3, updated_by=$2, updated_at=$3
            WHERE id=$1 AND status='draft' RETURNING *
            """,
            plan_id, _clean_text(actor), datetime.now(timezone.utc),
        )
        return dict(record) if record else None

    async def activate_cost_plan_version(self, plan_id: str, actor: str) -> dict[str, Any] | None:
        pool = self._require_pool()
        now = datetime.now(timezone.utc)
        async with pool.acquire() as connection:
            async with connection.transaction():
                plan = await connection.fetchrow(
                    "SELECT * FROM cost_plan_versions WHERE id=$1 FOR UPDATE", plan_id
                )
                if plan is None:
                    return None
                if plan["status"] != "approved" or plan["scenario"] != "baseline":
                    raise ValueError("只有已批准的 baseline 计划可以激活")
                if not bool(plan["coverage_complete"]):
                    raise ValueError("计划覆盖不完整，不能作为官方预测基准")
                await connection.execute(
                    """
                    UPDATE cost_plan_versions SET is_active=FALSE, updated_by=$2, updated_at=$3
                    WHERE year=$1 AND is_active AND id<>$4
                    """,
                    plan["year"], _clean_text(actor), now, plan_id,
                )
                record = await connection.fetchrow(
                    """
                    UPDATE cost_plan_versions SET is_active=TRUE, activated_by=$2,
                        activated_at=$3, updated_by=$2, updated_at=$3
                    WHERE id=$1 RETURNING *
                    """,
                    plan_id, _clean_text(actor), now,
                )
        return dict(record) if record else None

    async def archive_cost_plan_version(self, plan_id: str, actor: str) -> dict[str, Any] | None:
        record = await self._require_pool().fetchrow(
            """
            UPDATE cost_plan_versions SET status='archived', is_active=FALSE,
                archived_by=$2, archived_at=$3, updated_by=$2, updated_at=$3
            WHERE id=$1 AND status<>'archived' RETURNING *
            """,
            plan_id, _clean_text(actor), datetime.now(timezone.utc),
        )
        return dict(record) if record else None

    async def delete_cost_plan_version(self, plan_id: str) -> bool:
        result = await self._require_pool().execute(
            "DELETE FROM cost_plan_versions WHERE id=$1 AND status='draft' AND NOT is_active",
            plan_id,
        )
        return result.endswith(" 1")

    async def list_cost_budgets(self) -> list[dict[str, Any]]:
        records = await self._require_pool().fetch("SELECT * FROM cost_budgets ORDER BY month DESC")
        return [dict(record) for record in records]

    async def upsert_cost_budget(self, month: str, budget_usd: float, daily_target_usd: float) -> dict[str, Any]:
        record = await self._require_pool().fetchrow(
            """
            INSERT INTO cost_budgets (month, budget_usd, daily_target_usd, updated_at)
            VALUES ($1::date,$2::numeric,$3::numeric,$4)
            ON CONFLICT (month) DO UPDATE SET budget_usd=EXCLUDED.budget_usd,
                daily_target_usd=EXCLUDED.daily_target_usd, updated_at=EXCLUDED.updated_at
            RETURNING *
            """,
            f"{month}-01", str(budget_usd), str(daily_target_usd), datetime.now(timezone.utc),
        )
        return dict(record)

    async def list_savings_actions(self) -> list[dict[str, Any]]:
        records = await self._require_pool().fetch("SELECT * FROM savings_actions ORDER BY implemented_date DESC, name")
        return [dict(record) for record in records]

    async def create_savings_action(self, action: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        record = await self._require_pool().fetchrow(
            """
            INSERT INTO savings_actions (id, name, baseline_daily_cost, implemented_date,
                verified_date, verified_daily_cost, owner, status, notes, created_at, updated_at,
                expected_daily_cost, expected_start_date, provider, model, cost_bucket,
                evidence_url, finance_reviewer)
            VALUES ($1,$2,$3::numeric,$4::date,$5::date,$6::numeric,$7,$8,$9,$10,$10,
                $11::numeric,$12::date,$13,$14,$15,$16,$17) RETURNING *
            """,
            action["id"], action["name"], str(action["baselineDailyCost"]),
            _as_date(action["implementedDate"]), _as_date(action["verifiedDate"]) if action.get("verifiedDate") else None,
            str(action["verifiedDailyCost"]) if action.get("verifiedDailyCost") is not None else None,
            action.get("owner", ""), action.get("status", "planned"), action.get("notes", ""), now,
            str(action["expectedDailyCost"]) if action.get("expectedDailyCost") is not None else None,
            _as_date(action["expectedStartDate"]) if action.get("expectedStartDate") else None,
            action.get("provider", ""), action.get("model", ""), action.get("costBucket", ""),
            action.get("evidenceUrl", ""), action.get("financeReviewer", ""),
        )
        return dict(record)

    async def update_savings_action(self, action_id: str, action: dict[str, Any]) -> dict[str, Any] | None:
        record = await self._require_pool().fetchrow(
            """
            UPDATE savings_actions SET name=$2, baseline_daily_cost=$3::numeric,
                implemented_date=$4::date, verified_date=$5::date,
                verified_daily_cost=$6::numeric, owner=$7, status=$8, notes=$9, updated_at=$10,
                expected_daily_cost=$11::numeric, expected_start_date=$12::date,
                provider=$13, model=$14, cost_bucket=$15, evidence_url=$16,
                finance_reviewer=$17
            WHERE id=$1 RETURNING *
            """,
            action_id, action["name"], str(action["baselineDailyCost"]),
            _as_date(action["implementedDate"]), _as_date(action["verifiedDate"]) if action.get("verifiedDate") else None,
            str(action["verifiedDailyCost"]) if action.get("verifiedDailyCost") is not None else None,
            action.get("owner", ""), action.get("status", "planned"), action.get("notes", ""),
            datetime.now(timezone.utc),
            str(action["expectedDailyCost"]) if action.get("expectedDailyCost") is not None else None,
            _as_date(action["expectedStartDate"]) if action.get("expectedStartDate") else None,
            action.get("provider", ""), action.get("model", ""), action.get("costBucket", ""),
            action.get("evidenceUrl", ""), action.get("financeReviewer", ""),
        )
        return dict(record) if record else None

    async def list_savings_measurements(
        self,
        *,
        as_of: str | date | None = None,
        year: int | None = None,
        status: str = "",
        action_id: str = "",
        provider: str = "",
        model: str = "",
        account_id: str = "",
        cost_bucket: str = "",
    ) -> list[dict[str, Any]]:
        records = await self._require_pool().fetch(
            """
            SELECT * FROM savings_measurements
            WHERE ($1::date IS NULL OR measurement_end_date <= $1::date)
              AND ($2::integer IS NULL OR EXTRACT(YEAR FROM measurement_end_date)::integer=$2)
              AND ($3='' OR status=$3) AND ($4='' OR action_id=$4)
              AND ($5='' OR provider=$5) AND ($6='' OR model=$6)
              AND ($7='' OR account_id=$7) AND ($8='' OR cost_bucket=$8)
            ORDER BY measurement_end_date DESC, updated_at DESC
            """,
            _optional_date(as_of), int(year) if year is not None else None,
            _clean_text(status), _clean_text(action_id), _clean_text(provider),
            _clean_text(model), _clean_text(account_id), _clean_text(cost_bucket),
        )
        return [dict(record) for record in records]

    @staticmethod
    def _savings_measurement_values(measurement: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, Any]:
        merged = dict(current or {})
        aliases = {
            "action_id": ("action_id", "actionId"), "scope": ("scope", "scope_key", "scopeKey"),
            "provider": ("provider",), "model": ("model",), "account_id": ("account_id", "accountId"),
            "cost_bucket": ("cost_bucket", "costBucket"),
            "baseline_start_date": ("baseline_start_date", "baselineStartDate", "baselineStart"),
            "baseline_end_date": ("baseline_end_date", "baselineEndDate", "baselineEnd"),
            "measurement_start_date": ("measurement_start_date", "measurementStartDate", "measurementStart"),
            "measurement_end_date": ("measurement_end_date", "measurementEndDate", "measurementEnd"),
            "baseline_amount_usd": ("baseline_amount_usd", "baselineAmountUsd"),
            "actual_amount_usd": ("actual_amount_usd", "actualAmountUsd"),
            "evidence_url": ("evidence_url", "evidenceUrl"), "status": ("status",),
            "finance_reviewer": ("finance_reviewer", "financeReviewer"),
            "reviewed_at": ("reviewed_at", "reviewedAt"), "notes": ("notes",),
        }
        for field, names in aliases.items():
            if current is None or any(name in measurement for name in names):
                merged[field] = _input_value(measurement, *names, default=merged.get(field))
        merged["scope"] = _clean_text(merged.get("scope")) or "|".join(
            _clean_text(merged.get(field)) for field in ("provider", "model", "account_id", "cost_bucket")
        )
        merged["status"] = _clean_text(merged.get("status")) or "pending_evidence"
        if merged["status"] in {"reviewed", "verified", "approved"}:
            if not _clean_text(merged.get("evidence_url")) or not _clean_text(merged.get("finance_reviewer")):
                raise ValueError("已核验节省必须提供证据链接和财务复核人")
            merged["reviewed_at"] = _optional_datetime(merged.get("reviewed_at")) or datetime.now(timezone.utc)
        else:
            merged["reviewed_at"] = _optional_datetime(merged.get("reviewed_at"))
        return merged

    @staticmethod
    async def _assert_savings_measurement_not_overlapping(connection: Any, values: dict[str, Any], measurement_id: str) -> None:
        if values["status"] not in {"reviewed", "verified", "approved"}:
            return
        overlap = await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM savings_measurements
                WHERE id<>$1 AND status IN ('reviewed', 'verified', 'approved') AND scope=$2
                  AND measurement_start_date <= $4::date
                  AND measurement_end_date >= $3::date
            )
            """,
            measurement_id, values["scope"], _as_date(values["measurement_start_date"]),
            _as_date(values["measurement_end_date"]),
        )
        if overlap:
            raise ValueError("同一范围存在重叠的已核验节省测量")

    async def create_savings_measurement(self, measurement: dict[str, Any]) -> dict[str, Any]:
        values = self._savings_measurement_values(measurement)
        measurement_id = _clean_text(measurement.get("id")) or str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        async with self._require_pool().acquire() as connection:
            async with connection.transaction():
                await self._assert_savings_measurement_not_overlapping(connection, values, measurement_id)
                record = await connection.fetchrow(
                    """
                    INSERT INTO savings_measurements (
                        id, action_id, scope, provider, model, account_id,
                        cost_bucket, baseline_start_date, baseline_end_date,
                        measurement_start_date, measurement_end_date,
                        baseline_amount_usd, actual_amount_usd, evidence_url,
                        status, finance_reviewer, reviewed_at, notes, created_at, updated_at
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::date,$9::date,$10::date,$11::date,
                        $12::numeric,$13::numeric,$14,$15,$16,$17,$18,$19,$19)
                    RETURNING *
                    """,
                    measurement_id, _clean_text(values.get("action_id")) or None, values["scope"],
                    _clean_text(values.get("provider")), _clean_text(values.get("model")),
                    _clean_text(values.get("account_id")), _clean_text(values.get("cost_bucket")),
                    _as_date(values["baseline_start_date"]), _as_date(values["baseline_end_date"]),
                    _as_date(values["measurement_start_date"]), _as_date(values["measurement_end_date"]),
                    str(values["baseline_amount_usd"]), str(values["actual_amount_usd"]),
                    _clean_text(values.get("evidence_url")), values["status"],
                    _clean_text(values.get("finance_reviewer")), values["reviewed_at"],
                    _clean_text(values.get("notes")), now,
                )
        return dict(record)

    async def update_savings_measurement(self, measurement_id: str, measurement: dict[str, Any]) -> dict[str, Any] | None:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                current = await connection.fetchrow(
                    "SELECT * FROM savings_measurements WHERE id=$1 FOR UPDATE", measurement_id
                )
                if current is None:
                    return None
                values = self._savings_measurement_values(measurement, dict(current))
                await self._assert_savings_measurement_not_overlapping(connection, values, measurement_id)
                record = await connection.fetchrow(
                    """
                    UPDATE savings_measurements SET action_id=$2, scope=$3,
                        provider=$4, model=$5, account_id=$6, cost_bucket=$7,
                        baseline_start_date=$8::date, baseline_end_date=$9::date,
                        measurement_start_date=$10::date, measurement_end_date=$11::date,
                        baseline_amount_usd=$12::numeric, actual_amount_usd=$13::numeric,
                        evidence_url=$14, status=$15, finance_reviewer=$16,
                        reviewed_at=$17, notes=$18, updated_at=$19
                    WHERE id=$1 RETURNING *
                    """,
                    measurement_id, _clean_text(values.get("action_id")) or None, values["scope"],
                    _clean_text(values.get("provider")), _clean_text(values.get("model")),
                    _clean_text(values.get("account_id")), _clean_text(values.get("cost_bucket")),
                    _as_date(values["baseline_start_date"]), _as_date(values["baseline_end_date"]),
                    _as_date(values["measurement_start_date"]), _as_date(values["measurement_end_date"]),
                    str(values["baseline_amount_usd"]), str(values["actual_amount_usd"]),
                    _clean_text(values.get("evidence_url")), values["status"],
                    _clean_text(values.get("finance_reviewer")), values["reviewed_at"],
                    _clean_text(values.get("notes")), datetime.now(timezone.utc),
                )
        return dict(record) if record else None

    async def delete_savings_measurement(self, measurement_id: str) -> bool:
        result = await self._require_pool().execute("DELETE FROM savings_measurements WHERE id=$1", measurement_id)
        return result.endswith(" 1")
