from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
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
    collected_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (backend_id, request_id)
);
ALTER TABLE usage_event_attribution ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS usage_event_attribution_org_time_idx
    ON usage_event_attribution (organization_id, event_time)
    WHERE organization_id <> '';
CREATE INDEX IF NOT EXISTS usage_event_attribution_key_time_idx
    ON usage_event_attribution (key_id, event_time)
    WHERE key_id <> '';
"""


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _record_value(record: Any, key: str, default: Any = None) -> Any:
    try:
        return record[key]
    except (KeyError, IndexError, TypeError):
        return default


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


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(_clean_text(value)[:10])


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

    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 5) -> None:
        self.dsn = dsn
        self.min_size = min_size
        self.max_size = max_size
        self.pool: Any = None
        self._connect_lock = asyncio.Lock()

    @classmethod
    def from_environment(cls) -> UsageStore | None:
        dsn = os.getenv("USAGE_DATABASE_URL", "").strip()
        enabled = os.getenv("USAGE_SYNC_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
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
        return cls(dsn, min_size=min_size, max_size=max_size)

    async def connect(self) -> None:
        if self.pool is not None:
            return
        if asyncpg is None:
            raise RuntimeError("USAGE_SYNC_ENABLED=true 时需要安装 asyncpg")
        async with self._connect_lock:
            if self.pool is not None:
                return
            pool = await asyncpg.create_pool(self.dsn, min_size=self.min_size, max_size=self.max_size, command_timeout=30)
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

    async def snapshot_revision(
        self,
        start_date: str,
        end_date: str,
        backend_ids: list[str],
    ) -> str | None:
        if not backend_ids:
            return None
        return await self._require_pool().fetchval(
            """
            SELECT MIN(synced_at)::text
            FROM usage_sync_coverage
            WHERE usage_date=$1::date AND backend_id=ANY($2::text[])
            HAVING COUNT(DISTINCT backend_id)=cardinality($2::text[])
            """,
            _as_date(end_date),
            sorted(set(backend_ids)),
        )

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
                WHERE m.snapshot_date BETWEEN $3::date AND $4::date
                ORDER BY m.backend_id, m.user_id, m.snapshot_date DESC
            )
            SELECT 'member' AS kind, m.backend_id, m.team_id, m.team_name, m.user_id, m.employee_email, m.employee_name, m.team_role,
                   NULL::date AS usage_date, NULL::text AS source, NULL::text AS model_name,
                   0::bigint AS prompt_tokens, 0::bigint AS completion_tokens, 0::bigint AS total_tokens,
                   0::bigint AS request_count, 0::bigint AS success_count, 0::bigint AS failure_count, 0::double precision AS spend
            FROM members m
            UNION ALL
            SELECT 'usage', u.backend_id, NULL, NULL, u.user_id, MAX(u.employee_email), MAX(u.employee_name), NULL,
                   u.usage_date, u.source, {model_sql}, {self._aggregate_metrics_sql('u.')}
            FROM usage_daily u
            WHERE u.backend_id = ANY($1::text[])
              AND u.usage_date BETWEEN $3::date AND $4::date
              AND ($5 = 'all' OR u.source = $5)
              AND EXISTS (
                  SELECT 1 FROM usage_team_membership_daily m JOIN scope s ON s.backend_id=m.backend_id AND s.team_id=m.team_id
                  WHERE m.backend_id=u.backend_id AND m.snapshot_date=u.usage_date
                    AND (m.user_id=u.user_id OR (NULLIF(btrim(m.employee_email),'') IS NOT NULL AND lower(btrim(m.employee_email))=lower(btrim(u.employee_email))))
              )
            GROUP BY u.backend_id, u.usage_date, u.user_id, u.source, {model_sql}
            ORDER BY kind, usage_date NULLS FIRST, backend_id, user_id, source, model_name
            """,
            backend_ids, team_ids, _as_date(start_date), _as_date(end_date), source or "all",
        )
        member_records = [item for item in records if item["kind"] == "member"]
        if not member_records:
            anchor = team_scopes[0]
            return {
                "rows": [],
                "summaryRows": [],
                "employees": [],
                "team": {"id": anchor["id"], "name": anchor.get("name") or anchor["id"], "memberCount": 0, "backend": anchor["backend"]},
                "pageLimit": 0,
                "pageSize": 0,
                "pagesRead": 0,
                "totalPages": 0,
                "totalRecords": 0,
                "truncated": False,
                "dataQuality": {"summarySource": "database", "rankingSource": "database", "backends": covered, "scopeCount": len(team_scopes), "memberIdentityMatch": "normalized_email_or_backend_user_id"},
                "lastSyncedAt": await self.latest_sync_at(start_date, end_date, covered),
            }
        usage_records = [item for item in records if item["kind"] == "usage"]
        rows = []
        for record in usage_records:
            row = self._aggregated_usage_row(record)
            row.update(
                {
                    "employeeId": record["user_id"],
                    "employeeName": record["employee_name"] or record["user_id"],
                    "employeeEmail": record["employee_email"] or "",
                    "bindStatus": "已绑定邮箱" if record["employee_email"] else "未绑定邮箱",
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
        employees = self._merge_team_members(latest_members, employees_by_identity)
        employees.sort(key=lambda item: (-item["totalTokens"], -item["spend"], str(item["employeeName"]).casefold()))
        summary_rows = self._group_rows(rows, ("date", "source", "model"))
        anchor = team_scopes[0]
        return {"rows": self._public_rows(rows), "summaryRows": summary_rows, "employees": employees, "team": {"id": anchor["id"], "name": anchor.get("name") or member_records[0]["team_name"] or anchor["id"], "memberCount": len(employees), "backend": anchor["backend"]}, "pageLimit": 0, "pageSize": 0, "pagesRead": 0, "totalPages": 0, "totalRecords": len(rows), "truncated": False, "dataQuality": {"summarySource": "database", "rankingSource": "database", "backends": covered, "scopeCount": len(team_scopes), "memberIdentityMatch": "normalized_email_or_backend_user_id"}, "lastSyncedAt": await self.latest_sync_at(start_date, end_date, covered)}

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
                            attribution_source, billing_eligible
                        ) VALUES ($1, $2::date, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21)
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
                            attribution_source, billing_eligible, collected_at
                        ) VALUES (
                            $1,$2,$3,$4::date,$5,$6,$7,$8,$9,$10,$11,$12,$13,
                            $14,$15,$16,$17,$18::numeric,$19,$20,$21
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

    async def publish_snapshots(
        self,
        start_date: str,
        end_date: str,
        snapshots: list[Any],
    ) -> dict[str, Any]:
        """COPY a complete multi-backend snapshot, then publish it atomically."""

        if not snapshots:
            return {"rowCount": 0, "snapshotRevision": None}
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
                            "attribution_source", "billing_eligible",
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
                            "attribution_source", "billing_eligible", "collected_at",
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
                    _as_date(start_date),
                    _as_date(end_date),
                )
                await connection.execute(
                    "DELETE FROM usage_team_membership_daily WHERE backend_id=ANY($1::text[]) "
                    "AND snapshot_date BETWEEN $2::date AND $3::date",
                    backend_ids,
                    _as_date(start_date),
                    _as_date(end_date),
                )
                await connection.execute(
                    "DELETE FROM usage_sync_coverage WHERE backend_id=ANY($1::text[]) "
                    "AND usage_date BETWEEN $2::date AND $3::date",
                    backend_ids,
                    _as_date(start_date),
                    _as_date(end_date),
                )
                if event_backends:
                    await connection.execute(
                        "DELETE FROM usage_event_attribution WHERE backend_id=ANY($1::text[]) "
                        "AND usage_date BETWEEN $2::date AND $3::date "
                        "AND attribution_source <> 'legacy_report_only'",
                        event_backends,
                        _as_date(start_date),
                        _as_date(end_date),
                    )
                if department_backends:
                    await connection.execute(
                        "DELETE FROM usage_department_directory WHERE backend_id=ANY($1::text[])",
                        department_backends,
                    )
                await connection.execute(f"INSERT INTO usage_daily SELECT * FROM {usage_stage}")
                await connection.execute(
                    f"INSERT INTO usage_team_membership_daily SELECT * FROM {membership_stage}"
                )
                await connection.execute(
                    f"INSERT INTO usage_event_attribution SELECT * FROM {event_stage}"
                )
                await connection.execute(
                    f"INSERT INTO usage_department_directory SELECT * FROM {department_stage}"
                )
                await connection.execute(
                    """
                    INSERT INTO usage_sync_coverage (backend_id, usage_date, synced_at)
                    SELECT backend_id, day::date, $4
                    FROM unnest($1::text[]) AS backend_id
                    CROSS JOIN generate_series($2::date, $3::date, interval '1 day') AS day
                    """,
                    backend_ids,
                    _as_date(start_date),
                    _as_date(end_date),
                    collected_at,
                )
                revision = await connection.fetchval(
                    """
                    SELECT MIN(synced_at)::text
                    FROM usage_sync_coverage
                    WHERE usage_date=$1::date AND backend_id=ANY($2::text[])
                    HAVING COUNT(DISTINCT backend_id)=cardinality($2::text[])
                    """,
                    _as_date(end_date),
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
                    _as_date(start_date),
                    _as_date(end_date),
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
                            attribution_source, billing_eligible
                        ) VALUES ($1,$2::date,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21)
                        ON CONFLICT (
                            backend_id, usage_date, user_id, source, model,
                            organization_id, team_id, key_id, principal_id,
                            attribution_source, billing_eligible
                        ) DO UPDATE SET
                            employee_email=EXCLUDED.employee_email,
                            employee_name=EXCLUDED.employee_name,
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
                            attribution_source, billing_eligible, collected_at
                        ) VALUES ($1,$2,$3,$4::date,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18::numeric,$19,$20,$21)
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
                            collected_at=EXCLUDED.collected_at
                        """,
                        event_records,
                    )
        return len(usage_records)

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
            FROM usage_daily
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
            FROM usage_daily
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
                   MAX(employee_name) AS employee_name, source, team_id,
                   model AS model_name,
                   {self._aggregate_metrics_sql()}
            FROM usage_daily
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
                    "bindStatus": "已绑定邮箱" if record["employee_email"] else "未绑定邮箱",
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
            FROM usage_daily
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
                   employee_email, employee_name,
                   source, model, prompt_tokens, completion_tokens, total_tokens,
                   request_count, success_count, failure_count, spend, collected_at
            FROM usage_daily
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
            FROM usage_daily
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
            FROM usage_daily
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
            FROM usage_daily
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
            FROM usage_daily
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
                   u.source, {model_sql} AS model_name,
                   {self._aggregate_metrics_sql('u.')}
            FROM usage_daily u
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
                   MAX(employee_name) AS employee_name, source, {model_sql} AS model_name,
                   {self._aggregate_metrics_sql()}
            FROM usage_daily
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
                    "bindStatus": "已绑定邮箱" if record["employee_email"] else "未绑定邮箱",
                }
            )
            enriched.append(row)

        employee_records = await pool.fetch(
            f"""
            WITH filtered AS (
                SELECT *, COALESCE(NULLIF(employee_email, ''), user_id) AS employee_key,
                       {model_sql} AS model_name
                FROM usage_daily
                WHERE {where_sql}
            ), totals AS (
                SELECT employee_key, MIN(user_id) AS employee_id,
                       MAX(NULLIF(employee_email, '')) AS employee_email,
                       MAX(NULLIF(employee_name, '')) AS employee_name,
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
        department_names = await self._employee_department_names(start_date, end_date, covered, employee_filter)
        employees = [
            {
                "employeeId": record["employee_id"],
                "employeeName": record["employee_name"] or record["employee_id"],
                "employeeEmail": record["employee_email"] or "",
                "bindStatus": "已绑定邮箱" if record["employee_email"] else "未绑定邮箱",
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
            FROM usage_daily
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
            "dataQuality": {"summarySource": "database", "rankingSource": "database"},
            "lastSyncedAt": await self.latest_sync_at(start_date, end_date, covered),
        }

    async def _employee_department_names(
        self,
        start_date: str,
        end_date: str,
        backend_ids: list[str],
        employee_filter: str = "",
    ) -> dict[str, list[str]]:
        """返回 {user_id / employee_email: 部门名列表}，供成员下钻显示部门归属。

        同一员工可能在多个后端有账号、也可能同时属于多个部门，所以按 user_id 和
        邮箱两个维度都建索引：调用方先用邮箱查（跨后端账号合并后的身份），查不到
        再退回 user_id。
        """

        conditions = [
            "snapshot_date BETWEEN $1::date AND $2::date",
            "backend_id = ANY($3::text[])",
        ]
        args: list[Any] = [_as_date(start_date), _as_date(end_date), backend_ids]
        if employee_filter:
            conditions.append(
                "(position($4 IN lower(user_id)) > 0 OR position($4 IN lower(employee_email)) > 0 OR position($4 IN lower(employee_name)) > 0)"
            )
            args.append(employee_filter)
        records = await self._require_pool().fetch(
            "SELECT DISTINCT user_id, employee_email, team_id, team_name FROM usage_team_membership_daily WHERE "
            + " AND ".join(conditions),
            *args,
        )
        grouped: dict[str, list[str]] = {}
        for record in records:
            name = _clean_text(record["team_name"]) or _clean_text(record["team_id"])
            if not name:
                continue
            for key in (_clean_text(record["user_id"]).lower(), _clean_text(record["employee_email"]).lower()):
                if not key:
                    continue
                names = grouped.setdefault(key, [])
                if name not in names:
                    names.append(name)
        for names in grouped.values():
            names.sort()
        return grouped

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
        # Keep the team recorded on the usage event authoritative. Membership
        # snapshots are only a display-name fallback and may be late or contain
        # multiple rows for one account.
        team_id_sql = "lower(btrim(u.team_id))"
        team_name_sql = "lower(regexp_replace(btrim(COALESCE(dn.team_name, u.team_id)), '\\s+', ' ', 'g'))"
        logical_key_sql = f"{team_id_sql} || '::' || {team_name_sql}"
        where_sql = f"""
            u.usage_date BETWEEN $1::date AND $2::date
            AND u.backend_id = ANY($3::text[])
            AND ($4 = 'all' OR u.source = $4)
            AND u.team_id <> ''
            AND ($5 = '' OR {logical_key_sql} = $5 OR {team_id_sql} = $5 OR {team_name_sql} = $5)
        """
        department_join_sql = """
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
                   u.team_id, MAX(dn.team_name) AS team_name, '' AS team_role,
                   u.source, {model_sql} AS model_name,
                   {self._aggregate_metrics_sql('u.')}
            FROM usage_daily u
            {department_join_sql}
            WHERE {where_sql}
            GROUP BY u.backend_id, u.usage_date, u.user_id, u.team_id, u.source, {model_sql}
            ORDER BY u.usage_date, MAX(dn.team_name), MAX(u.employee_name), u.source, model_name
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
                    "bindStatus": "已绑定邮箱" if record["employee_email"] else "未绑定邮箱",
                }
            )
            rows.append(row)

        employee_records = await pool.fetch(
            f"""
            WITH filtered AS (
                SELECT u.*, dn.team_name,
                       lower(COALESCE(NULLIF(u.employee_email, ''), u.user_id)) AS employee_key,
                       {model_sql} AS model_name
                FROM usage_daily u
                {department_join_sql}
                WHERE {where_sql}
            ), totals AS (
                SELECT employee_key, MIN(user_id) AS employee_id,
                       MAX(NULLIF(employee_email, '')) AS employee_email,
                       MAX(NULLIF(employee_name, '')) AS employee_name,
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
                "bindStatus": "已绑定邮箱" if record["employee_email"] else "未绑定邮箱",
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
                SELECT u.*, dn.team_name, {logical_key_sql} AS department_key, {model_sql} AS model_name
                FROM usage_daily u
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
                option.update(current)
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
            FROM usage_daily u
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
                   u.source, {model_sql} AS model_name,
                   {self._aggregate_metrics_sql('u.')}
            FROM usage_daily u
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
                    "bindStatus": "已绑定邮箱" if record["employee_email"] else "未绑定邮箱",
                }
            )
            rows.append(row)

        employee_records = await pool.fetch(
            f"""
            WITH filtered AS (
                SELECT u.*, m.team_role,
                       lower(COALESCE(NULLIF(btrim(m.employee_email), ''), NULLIF(btrim(u.employee_email), ''), btrim(u.user_id))) AS employee_key,
                       {model_sql} AS model_name
                FROM usage_daily u
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
                "bindStatus": "已绑定邮箱" if record["employee_email"] else "未绑定邮箱",
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
            FROM usage_daily u
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
                item = {"employeeId": member["user_id"] if member_email else account_id, "employeeName": member["employee_name"] or member["user_id"], "employeeEmail": member["employee_email"] or "", "bindStatus": "已绑定邮箱" if member["employee_email"] else "未绑定邮箱", **empty_totals(), "primarySource": "其他", "userIds": [account_id], "teamRole": member["team_role"] or "user"}
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
                   u.source, {model_sql} AS model_name,
                   {self._aggregate_metrics_sql('u.')}
            FROM usage_daily u
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
            row.update({"employeeId": record["user_id"], "employeeName": record["employee_name"] or record["user_id"], "employeeEmail": record["employee_email"] or "", "bindStatus": "已绑定邮箱" if record["employee_email"] else "未绑定邮箱"})
            rows.append({key: value for key, value in row.items() if not key.startswith("_")})
        selected_member = members[0]
        selected = {
            "employeeId": selected_user_ids[0],
            "employeeName": selected_member["employee_name"] or selected_user_ids[0],
            "employeeEmail": selected_member["employee_email"] or "",
            "bindStatus": "已绑定邮箱" if selected_member["employee_email"] else "未绑定邮箱",
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
                WHERE m.snapshot_date BETWEEN $3::date AND $4::date
                  AND (($6<>'' AND m.backend_id=$6 AND $5=lower(btrim(m.user_id)))
                       OR ($6='' AND ($5=lower(btrim(m.user_id)) OR $5=lower(btrim(m.employee_email)) OR $5=lower(btrim(m.employee_name)))))
                ORDER BY m.backend_id, m.user_id, m.snapshot_date DESC
            )
            SELECT 'member' AS kind, s.backend_id, s.team_id, s.team_name, s.user_id, s.employee_email, s.employee_name, s.team_role,
                   NULL::date AS usage_date, NULL::text AS source, NULL::text AS model_name,
                   0::bigint AS prompt_tokens, 0::bigint AS completion_tokens, 0::bigint AS total_tokens,
                   0::bigint AS request_count, 0::bigint AS success_count, 0::bigint AS failure_count, 0::double precision AS spend
            FROM selected s
            UNION ALL
            SELECT 'usage', MIN(u.backend_id), NULL, NULL, MIN(u.user_id), MAX(u.employee_email), MAX(u.employee_name), NULL,
                   u.usage_date, u.source, {model_sql} AS model_name, {self._aggregate_metrics_sql('u.')}
            FROM usage_daily u
            WHERE u.backend_id=ANY($1::text[]) AND u.usage_date BETWEEN $3::date AND $4::date
              AND ($7='all' OR u.source=$7)
              AND EXISTS (SELECT 1 FROM selected s WHERE s.backend_id=u.backend_id AND (s.user_id=u.user_id OR (NULLIF(btrim(s.employee_email),'') IS NOT NULL AND lower(btrim(s.employee_email))=lower(btrim(u.employee_email)))))
              AND EXISTS (SELECT 1 FROM usage_team_membership_daily m JOIN scope sc ON sc.backend_id=m.backend_id AND sc.team_id=m.team_id WHERE m.backend_id=u.backend_id AND m.snapshot_date=u.usage_date AND (m.user_id=u.user_id OR (NULLIF(btrim(m.employee_email),'') IS NOT NULL AND lower(btrim(m.employee_email))=lower(btrim(u.employee_email)))))
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
                "dataQuality": {"backends": covered, "scopeCount": len(team_scopes), "memberIdentityMatch": "normalized_email_or_backend_user_id"},
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
                    "bindStatus": "已绑定邮箱" if record["employee_email"] else "未绑定邮箱",
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
            "bindStatus": "已绑定邮箱" if first["employee_email"] else "未绑定邮箱",
            "userIds": user_ids,
            "teamRole": "admin" if any(item["team_role"] == "admin" for item in members) else first["team_role"] or "user",
            **empty_totals(),
            "primarySource": "其他",
        }
        selected.update(summarize(rows)["rangeTotal"])
        anchor = team_scopes[0]
        return {"rows": rows, "summary": summarize(rows), "employee": selected, "team": {"id": anchor["id"], "name": anchor.get("name") or first["team_name"] or anchor["id"], "memberCount": len(members), "backend": anchor["backend"]}, "lastSyncedAt": await self.latest_sync_at(start_date, end_date, covered), "dataQuality": {"backends": covered, "scopeCount": len(team_scopes), "memberIdentityMatch": "normalized_email_or_backend_user_id"}}

    async def health(self) -> dict[str, Any]:
        if self.pool is None:
            return {"enabled": True, "connected": False, "status": "disconnected"}
        try:
            await self.pool.fetchval("SELECT 1")
        except Exception as exc:  # pragma: no cover - depends on database
            return {"enabled": True, "connected": False, "status": "error", "error": exc.__class__.__name__}
        return {"enabled": True, "connected": True, "status": "ok"}
