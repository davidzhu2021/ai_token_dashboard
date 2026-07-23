from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any

try:
    import asyncpg
except ImportError:  # pragma: no cover - optional for local development
    asyncpg = None  # type: ignore[assignment]

from .litellm_client import normalize_model_display_name


USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_daily (
    backend_id TEXT NOT NULL,
    usage_date DATE NOT NULL,
    user_id TEXT NOT NULL,
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
    PRIMARY KEY (backend_id, usage_date, user_id, source, model)
);

CREATE INDEX IF NOT EXISTS usage_daily_employee_date_idx
    ON usage_daily (employee_email, usage_date);
CREATE INDEX IF NOT EXISTS usage_daily_date_idx
    ON usage_daily (usage_date);
CREATE INDEX IF NOT EXISTS usage_daily_date_backend_user_idx
    ON usage_daily (usage_date, backend_id, user_id);
CREATE INDEX IF NOT EXISTS usage_daily_date_source_model_idx
    ON usage_daily (usage_date, source, model);

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

CREATE INDEX IF NOT EXISTS usage_team_membership_lookup_idx
    ON usage_team_membership_daily (backend_id, team_id, snapshot_date);
CREATE INDEX IF NOT EXISTS usage_team_membership_employee_idx
    ON usage_team_membership_daily (employee_email, snapshot_date);
CREATE INDEX IF NOT EXISTS usage_team_membership_usage_join_idx
    ON usage_team_membership_daily (backend_id, snapshot_date, user_id);
CREATE INDEX IF NOT EXISTS usage_team_membership_team_filter_idx
    ON usage_team_membership_daily (backend_id, snapshot_date, team_id, user_id);

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
"""


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


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
        return cls(dsn)

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

    @staticmethod
    def _usage_record(backend_id: str, row: dict[str, Any], collected_at: datetime) -> tuple[Any, ...]:
        user_id = _clean_text(row.get("_userId") or row.get("userId")) or "unknown"
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
        )

    @staticmethod
    def _coalesce_usage_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for row in rows:
            key = (
                _clean_text(row.get("date")),
                _clean_text(row.get("_userId") or row.get("userId")) or "unknown",
                _clean_text(row.get("source")) or "其他",
                _clean_text(row.get("model")) or "未知模型",
            )
            current = grouped.get(key)
            if current is None:
                current = dict(row)
                current.update(empty_totals())
                grouped[key] = current
            add_totals(current, row)
            if not current.get("employeeEmail") and row.get("employeeEmail"):
                current["employeeEmail"] = row["employeeEmail"]
            if not current.get("employeeName") and row.get("employeeName"):
                current["employeeName"] = row["employeeName"]
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

    async def replace_backend_snapshot(
        self,
        backend_id: str,
        start_date: str,
        end_date: str,
        rows: list[dict[str, Any]],
        memberships: list[dict[str, Any]],
    ) -> int:
        pool = self._require_pool()
        collected_at = datetime.now(timezone.utc)
        usage_records = [
            self._usage_record(backend_id, row, collected_at)
            for row in self._coalesce_usage_rows(rows)
            if row.get("date")
        ]
        membership_records = [self._membership_record(backend_id, row) for row in memberships if row.get("snapshotDate") and row.get("teamId")]
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM usage_daily WHERE backend_id = $1 AND usage_date BETWEEN $2::date AND $3::date",
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
                await connection.execute(
                    "DELETE FROM usage_sync_coverage WHERE backend_id = $1 AND usage_date BETWEEN $2::date AND $3::date",
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
                            request_count, success_count, failure_count, spend, collected_at
                        ) VALUES ($1, $2::date, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                        ON CONFLICT (backend_id, usage_date, user_id, source, model) DO UPDATE SET
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

    async def _fetch_usage(self, start_date: str, end_date: str, backend_ids: list[str] | None = None) -> list[dict[str, Any]]:
        backend_filter = ""
        args: list[Any] = [_as_date(start_date), _as_date(end_date)]
        if backend_ids:
            backend_filter = " AND backend_id = ANY($3::text[])"
            args.append(backend_ids)
        records = await self._require_pool().fetch(
            f"""
            SELECT backend_id, usage_date, user_id, employee_email, employee_name,
                   source, model, prompt_tokens, completion_tokens, total_tokens,
                   request_count, success_count, failure_count, spend, collected_at
            FROM usage_daily
            WHERE usage_date BETWEEN $1::date AND $2::date{backend_filter}
            ORDER BY usage_date, employee_name, source, model
            """,
            *args,
        )
        rows = [self._usage_row(record) for record in records]
        return self._merge_rows_by(rows, ("_backendId", "date", "_userId", "source", "model"))

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
            "employeeEmail": record["employee_email"],
            "employeeName": record["employee_name"],
        }

    @staticmethod
    def _public_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{key: value for key, value in row.items() if not key.startswith("_")} for row in rows]

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
        row = {
            "date": record["usage_date"].isoformat(),
            "source": record["source"],
            "model": normalize_model_display_name(record["model_name"]) or "未知模型",
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
        model_sql = "regexp_replace(u.model, '^[A-Za-z][A-Za-z0-9]*-acct-[0-9]+-', '', 'i')"
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
        model_sql = "regexp_replace(model, '^[A-Za-z][A-Za-z0-9]*-acct-[0-9]+-', '', 'i')"
        row_records = await pool.fetch(
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
            SELECT totals.*, primary_sources.primary_source,
                   ARRAY(SELECT DISTINCT user_id FROM filtered f WHERE f.employee_key = totals.employee_key ORDER BY user_id) AS user_ids
            FROM totals
            JOIN primary_sources USING (employee_key)
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
        public_rows = self._public_rows(enriched)
        return {
            "rows": public_rows,
            "summaryRows": summary_rows,
            "employees": employees,
            "pageLimit": 0,
            "pageSize": 0,
            "pagesRead": 0,
            "totalPages": 0,
            "totalRecords": len(enriched),
            "truncated": False,
            "dataQuality": {"summarySource": "database", "rankingSource": "database"},
            "lastSyncedAt": await self.latest_sync_at(start_date, end_date, covered),
        }

    @staticmethod
    def _employee_summaries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = _clean_text(row.get("employeeEmail")) or _clean_text(row.get("employeeId"))
            item = grouped.setdefault(
                key,
                {
                    "employeeId": row.get("employeeId"),
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
        department_filter = (department or "").strip().lower()
        args: list[Any] = [_as_date(start_date), _as_date(end_date), covered, source or "all", department_filter]
        where_sql = """
            u.usage_date BETWEEN $1::date AND $2::date
            AND u.backend_id = ANY($3::text[])
            AND ($4 = 'all' OR u.source = $4)
            AND ($5 = '' OR lower(m.team_id) = $5 OR lower(m.team_name) = $5)
        """
        model_sql = "regexp_replace(u.model, '^[A-Za-z][A-Za-z0-9]*-acct-[0-9]+-', '', 'i')"
        pool = self._require_pool()
        records = await pool.fetch(
            f"""
            SELECT u.backend_id, u.usage_date, u.user_id,
                   MAX(u.employee_email) AS employee_email,
                   MAX(u.employee_name) AS employee_name,
                   m.team_id, MAX(m.team_name) AS team_name, MAX(m.team_role) AS team_role,
                   u.source, {model_sql} AS model_name,
                   {self._aggregate_metrics_sql('u.')} 
            FROM usage_daily u
            JOIN usage_team_membership_daily m
              ON m.backend_id = u.backend_id AND m.snapshot_date = u.usage_date AND m.user_id = u.user_id
            WHERE {where_sql}
            GROUP BY u.backend_id, u.usage_date, u.user_id, m.team_id, u.source, {model_sql}
            ORDER BY u.usage_date, MAX(m.team_name), MAX(u.employee_name), u.source, model_name
            """,
            *args,
        )
        rows = []
        for record in records:
            row = self._aggregated_usage_row(record)
            row.update(
                {
                    "departmentId": record["team_id"],
                    "departmentName": record["team_name"] or record["team_id"],
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
                SELECT u.*, m.team_id, m.team_name,
                       lower(COALESCE(NULLIF(u.employee_email, ''), u.user_id)) AS employee_key,
                       {model_sql} AS model_name
                FROM usage_daily u
                JOIN usage_team_membership_daily m
                  ON m.backend_id = u.backend_id AND m.snapshot_date = u.usage_date AND m.user_id = u.user_id
                WHERE {where_sql}
            ), totals AS (
                SELECT employee_key, MIN(user_id) AS employee_id,
                       MAX(NULLIF(employee_email, '')) AS employee_email,
                       MAX(NULLIF(employee_name, '')) AS employee_name,
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
            SELECT totals.*, primary_sources.primary_source,
                   ARRAY(SELECT DISTINCT user_id FROM filtered f WHERE f.employee_key = totals.employee_key ORDER BY user_id) AS user_ids
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
                SELECT u.*, m.team_id, m.team_name, {model_sql} AS model_name
                FROM usage_daily u
                JOIN usage_team_membership_daily m
                  ON m.backend_id = u.backend_id AND m.snapshot_date = u.usage_date AND m.user_id = u.user_id
                WHERE {where_sql}
            ), source_totals AS (
                SELECT team_id, source, SUM(total_tokens)::bigint AS source_tokens
                FROM filtered GROUP BY team_id, source
            ), primary_sources AS (
                SELECT DISTINCT ON (team_id) team_id, source AS primary_source
                FROM source_totals ORDER BY team_id, source_tokens DESC, source
            )
            SELECT team_id, MAX(team_name) AS team_name,
                   {self._aggregate_metrics_sql('')}, COUNT(DISTINCT user_id)::bigint AS active_employees,
                   primary_sources.primary_source
            FROM filtered JOIN primary_sources USING (team_id)
            GROUP BY team_id, primary_sources.primary_source
            ORDER BY total_tokens DESC, spend DESC, lower(MAX(team_name))
            """,
            *args,
        )
        departments = [
            {
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
        summary_records = await pool.fetch(
            f"""
            SELECT u.usage_date, u.source, {model_sql} AS model_name, {self._aggregate_metrics_sql('u.')}
            FROM usage_daily u
            JOIN usage_team_membership_daily m
              ON m.backend_id = u.backend_id AND m.snapshot_date = u.usage_date AND m.user_id = u.user_id
            WHERE {where_sql}
            GROUP BY u.usage_date, u.source, {model_sql}
            ORDER BY u.usage_date, u.source, model_name
            """,
            *args,
        )
        summary_rows = [self._aggregated_usage_row(record, include_identity=False) for record in summary_records]
        public_rows = self._public_rows(rows)
        return {
            "rows": public_rows,
            "summaryRows": summary_rows,
            "departments": departments,
            "employees": employees,
            "pageLimit": 0,
            "pageSize": 0,
            "pagesRead": 0,
            "totalPages": 0,
            "totalRecords": len(rows),
            "truncated": False,
            "dataQuality": {"summarySource": "database", "rankingSource": "database"},
            "lastSyncedAt": await self.latest_sync_at(start_date, end_date, covered),
        }

    async def team_rows(self, backend_id: str, team_id: str, start_date: str, end_date: str, source: str) -> dict[str, Any] | None:
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
        model_sql = "regexp_replace(u.model, '^[A-Za-z][A-Za-z0-9]*-acct-[0-9]+-', '', 'i')"
        records = await pool.fetch(
            f"""
            SELECT u.backend_id, u.usage_date, u.user_id,
                   MAX(u.employee_email) AS employee_email,
                   MAX(u.employee_name) AS employee_name,
                   u.source, {model_sql} AS model_name,
                   {self._aggregate_metrics_sql('u.')}
            FROM usage_daily u
            JOIN usage_team_membership_daily m
              ON m.backend_id = u.backend_id AND m.snapshot_date = u.usage_date AND m.user_id = u.user_id
             AND m.team_id = $2
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
                       lower(COALESCE(NULLIF(btrim(u.employee_email), ''), btrim(u.user_id))) AS employee_key,
                       {model_sql} AS model_name
                FROM usage_daily u
                JOIN usage_team_membership_daily m
                  ON m.backend_id = u.backend_id AND m.snapshot_date = u.usage_date AND m.user_id = u.user_id
                 AND m.team_id = $2
                WHERE u.backend_id = $1
                  AND u.usage_date BETWEEN $3::date AND $4::date
                  AND ($5 = 'all' OR u.source = $5)
            ), totals AS (
                SELECT employee_key, MIN(user_id) AS employee_id,
                       MAX(NULLIF(employee_email, '')) AS employee_email,
                       MAX(NULLIF(employee_name, '')) AS employee_name,
                       MAX(team_role) AS team_role,
                       {self._aggregate_metrics_sql('')}
                FROM filtered GROUP BY employee_key
            ), source_totals AS (
                SELECT employee_key, source, SUM(total_tokens)::bigint AS source_tokens
                FROM filtered GROUP BY employee_key, source
            ), primary_sources AS (
                SELECT DISTINCT ON (employee_key) employee_key, source AS primary_source
                FROM source_totals ORDER BY employee_key, source_tokens DESC, source
            )
            SELECT totals.*, primary_sources.primary_source,
                   ARRAY(SELECT DISTINCT user_id FROM filtered f WHERE f.employee_key = totals.employee_key ORDER BY user_id) AS user_ids
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
        employees = self._merge_team_members(latest_members, employee_by_user_id)
        employees.sort(key=lambda item: (-item["totalTokens"], -item["spend"], str(item["employeeName"]).lower()))
        team_name = latest_members[0]["team_name"] or team_id
        summary_records = await pool.fetch(
            f"""
            SELECT u.usage_date, u.source, {model_sql} AS model_name, {self._aggregate_metrics_sql('u.')}
            FROM usage_daily u
            JOIN usage_team_membership_daily m
              ON m.backend_id = u.backend_id AND m.snapshot_date = u.usage_date AND m.user_id = u.user_id
             AND m.team_id = $2
            WHERE u.backend_id = $1
              AND u.usage_date BETWEEN $3::date AND $4::date
              AND ($5 = 'all' OR u.source = $5)
            GROUP BY u.usage_date, u.source, {model_sql}
            ORDER BY u.usage_date, u.source, model_name
            """,
            *args,
        )
        summary_rows = [self._aggregated_usage_row(record, include_identity=False) for record in summary_records]
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
            item = employee_by_user_id.get(str(member["user_id"]))
            if item is None:
                item = {"employeeId": member["user_id"], "employeeName": member["employee_name"] or member["user_id"], "employeeEmail": member["employee_email"] or "", "bindStatus": "已绑定邮箱" if member["employee_email"] else "未绑定邮箱", **empty_totals(), "primarySource": "其他", "userIds": [member["user_id"]], "teamRole": member["team_role"] or "user"}
            else:
                item = dict(item)
                item["teamRole"] = member["team_role"] or item.get("teamRole") or "user"
            email = str(item.get("employeeEmail") or member["employee_email"] or "").strip().lower()
            identity = email or str(item.get("employeeId") or member["user_id"]).strip().lower()
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

    async def team_member_rows(self, backend_id: str, team_id: str, employee: str, start_date: str, end_date: str, source: str) -> dict[str, Any] | None:
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
              AND ($5 = lower(user_id) OR $5 = lower(employee_email) OR $5 = lower(employee_name))
            ORDER BY user_id, snapshot_date DESC
            """,
            backend_id, team_id, _as_date(start_date), _as_date(end_date), normalized,
        )
        if not members:
            return None
        selected_user_ids = [str(member["user_id"]) for member in members]
        args: list[Any] = [backend_id, team_id, _as_date(start_date), _as_date(end_date), source or "all", selected_user_ids]
        model_sql = "regexp_replace(u.model, '^[A-Za-z][A-Za-z0-9]*-acct-[0-9]+-', '', 'i')"
        records = await pool.fetch(
            f"""
            SELECT u.backend_id, u.usage_date, u.user_id,
                   MAX(u.employee_email) AS employee_email,
                   MAX(u.employee_name) AS employee_name,
                   u.source, {model_sql} AS model_name,
                   {self._aggregate_metrics_sql('u.')}
            FROM usage_daily u
            JOIN usage_team_membership_daily m
              ON m.backend_id = u.backend_id AND m.snapshot_date = u.usage_date AND m.user_id = u.user_id
             AND m.team_id = $2
            WHERE u.backend_id = $1
              AND u.usage_date BETWEEN $3::date AND $4::date
              AND ($5 = 'all' OR u.source = $5)
              AND u.user_id = ANY($6::text[])
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

    async def health(self) -> dict[str, Any]:
        if self.pool is None:
            return {"enabled": True, "connected": False, "status": "disconnected"}
        try:
            await self.pool.fetchval("SELECT 1")
        except Exception as exc:  # pragma: no cover - depends on database
            return {"enabled": True, "connected": False, "status": "error", "error": exc.__class__.__name__}
        return {"enabled": True, "connected": True, "status": "ok"}
