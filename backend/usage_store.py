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
        if not covered:
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

    async def admin_rows(self, start_date: str, end_date: str, source: str, employee: str | None, backend_ids: list[str]) -> dict[str, Any] | None:
        covered = await self.covered_backend_ids(start_date, end_date, backend_ids)
        if not covered:
            return None
        rows = await self._fetch_usage(start_date, end_date, covered)
        if source and source != "all":
            rows = [row for row in rows if row["source"] == source]
        employee_filter = (employee or "").strip().lower()
        if employee_filter:
            rows = [
                row
                for row in rows
                if employee_filter in " ".join(
                    str(row.get(key) or "").lower() for key in ("_userId", "employeeEmail", "employeeName")
                )
            ]
        enriched = []
        for row in rows:
            item = dict(row)
            item.update(
                {
                    "employeeId": row["_userId"],
                    "employeeName": row["employeeName"] or row["_userId"],
                    "employeeEmail": row["employeeEmail"],
                    "bindStatus": "已绑定邮箱" if row["employeeEmail"] else "未绑定邮箱",
                }
            )
            enriched.append(item)
        employees = self._employee_summaries(enriched)
        public_rows = self._public_rows(enriched)
        return {
            "rows": public_rows,
            "summaryRows": public_rows,
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
        records = await self._require_pool().fetch(
            """
            SELECT u.*, m.team_id, m.team_name, m.team_role
            FROM usage_daily u
            JOIN usage_team_membership_daily m
              ON m.backend_id = u.backend_id AND m.snapshot_date = u.usage_date AND m.user_id = u.user_id
            WHERE u.usage_date BETWEEN $1::date AND $2::date
              AND u.backend_id = ANY($3::text[])
              AND ($4 = 'all' OR u.source = $4)
            """,
            _as_date(start_date),
            _as_date(end_date),
            covered,
            source or "all",
        )
        department_filter = (department or "").strip().lower()
        rows = []
        for record in records:
            if department_filter and department_filter not in {str(record["team_id"]).lower(), str(record["team_name"]).lower()}:
                continue
            row = self._usage_row(record)
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
        rows = self._merge_rows_by(rows, ("_backendId", "date", "_userId", "departmentId", "source", "model"))
        departments = self._group_rows(rows, ("departmentId", "departmentName"))
        department_sources: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        department_employees: dict[str, set[str]] = defaultdict(set)
        department_status: dict[str, str] = {}
        for row in rows:
            department_id = str(row.get("departmentId") or "")
            department_sources[department_id][str(row.get("source") or "其他")] += _as_int(row.get("totalTokens"))
            department_employees[department_id].add(str(row.get("employeeId") or ""))
            department_status[department_id] = str(row.get("departmentBindStatus") or "已绑定部门")
        for item in departments:
            department_id = str(item.get("departmentId") or "")
            source_values = department_sources.get(department_id, {})
            item["primarySource"] = max(source_values, key=source_values.get) if source_values else "其他"
            item["activeEmployees"] = len({value for value in department_employees.get(department_id, set()) if value})
            item["bindStatus"] = department_status.get(department_id, "已绑定部门")
        employees = self._employee_summaries(rows)
        public_rows = self._public_rows(rows)
        return {
            "rows": public_rows,
            "summaryRows": public_rows,
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
        memberships = await self._membership_rows(start_date, end_date, backend_id, team_id)
        if not memberships:
            return None
        records = await self._require_pool().fetch(
            """
            SELECT u.*, m.team_id, m.team_name, m.team_role
            FROM usage_daily u
            JOIN usage_team_membership_daily m
              ON m.backend_id = u.backend_id AND m.snapshot_date = u.usage_date AND m.user_id = u.user_id
            WHERE u.backend_id = $1 AND m.team_id = $2
              AND u.usage_date BETWEEN $3::date AND $4::date
              AND ($5 = 'all' OR u.source = $5)
            """,
            backend_id,
            team_id,
            _as_date(start_date),
            _as_date(end_date),
            source or "all",
        )
        rows = []
        for record in records:
            row = self._usage_row(record)
            row.update(
                {
                    "employeeId": record["user_id"],
                    "employeeName": record["employee_name"] or record["user_id"],
                    "employeeEmail": record["employee_email"],
                    "bindStatus": "已绑定邮箱" if record["employee_email"] else "未绑定邮箱",
                }
            )
            rows.append(row)
        rows = self._merge_rows_by(rows, ("_backendId", "date", "_userId", "source", "model"))
        latest_members: dict[str, Any] = {}
        for member in memberships:
            key = str(member["user_id"])
            current = latest_members.get(key)
            if current is None or member["snapshot_date"] > current["snapshot_date"]:
                latest_members[key] = member
        employees: list[dict[str, Any]] = []
        employee_summaries = self._employee_summaries(rows)
        by_user_id = {
            str(user_id): item
            for item in employee_summaries
            for user_id in item.get("userIds") or []
        }
        for member in latest_members.values():
            item = by_user_id.get(str(member["user_id"]))
            if item is None:
                item = {"employeeId": member["user_id"], "employeeName": member["employee_name"] or member["user_id"], "employeeEmail": member["employee_email"], "bindStatus": "已绑定邮箱" if member["employee_email"] else "未绑定邮箱", **empty_totals(), "primarySource": "其他", "userIds": [member["user_id"]], "teamRole": member["team_role"]}
            else:
                item = dict(item)
                item["teamRole"] = member["team_role"]
            employees.append(item)
        employees.sort(key=lambda item: (-item["totalTokens"], -item["spend"], str(item["employeeName"]).lower()))
        team_name = next(iter(latest_members.values()))["team_name"] if latest_members else team_id
        public_rows = self._public_rows(rows)
        return {
            "rows": public_rows,
            "summaryRows": public_rows,
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

    async def team_member_rows(self, backend_id: str, team_id: str, employee: str, start_date: str, end_date: str, source: str) -> dict[str, Any] | None:
        team = await self.team_rows(backend_id, team_id, start_date, end_date, source)
        if team is None:
            return None
        normalized = employee.strip().lower()
        selected = next(
            (
                item
                for item in team["employees"]
                if normalized in {str(item.get("employeeId") or "").lower(), str(item.get("employeeEmail") or "").lower(), str(item.get("employeeName") or "").lower(), *[str(value).lower() for value in item.get("userIds") or []]}
            ),
            None,
        )
        if selected is None:
            return None
        selected_user_ids = {str(value) for value in selected.get("userIds") or []}
        if not selected_user_ids:
            selected_user_ids.add(str(selected.get("employeeId")))
        rows = [row for row in team["rows"] if str(row.get("employeeId")) in selected_user_ids]
        return {
            "rows": rows,
            "summary": summarize(rows),
            "employee": selected,
            "team": team["team"],
            "lastSyncedAt": team.get("lastSyncedAt"),
        }

    async def health(self) -> dict[str, Any]:
        if self.pool is None:
            return {"enabled": True, "connected": False, "status": "disconnected"}
        try:
            await self.pool.fetchval("SELECT 1")
        except Exception as exc:  # pragma: no cover - depends on database
            return {"enabled": True, "connected": False, "status": "error", "error": exc.__class__.__name__}
        return {"enabled": True, "connected": True, "status": "ok"}
