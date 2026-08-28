from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore[assignment]


class LiteLLMStabilityReader:
    """Read-only adapter for the upstream spend log database."""

    def __init__(self, dsn: str, *, command_timeout: float = 20):
        self.dsn = dsn
        self.command_timeout = command_timeout
        self.pool = None
        self.available_columns: set[str] = set()

    async def start(self) -> None:
        if asyncpg is None:
            raise RuntimeError("直连稳定性数据源需要安装 asyncpg")
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=3, command_timeout=self.command_timeout, server_settings={"default_transaction_read_only": "on"})
        rows = await self.pool.fetch("SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'LiteLLM_SpendLogs'")
        self.available_columns = {str(row["column_name"]) for row in rows}
        if not self.available_columns:
            await self.pool.close()
            self.pool = None
            raise RuntimeError("稳定性数据源缺少 LiteLLM_SpendLogs 表")

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    async def fetch_rows(self, start_date: str, end_date: str, model: str = "") -> list[dict[str, Any]]:
        if self.pool is None:
            await self.start()
        start = datetime.fromisoformat(start_date).replace(tzinfo=timezone(timedelta(hours=8)))
        end = datetime.fromisoformat(end_date).replace(tzinfo=timezone(timedelta(hours=8))) + timedelta(days=1)
        columns = ["request_id", "api_key", "model", "model_group", "custom_llm_provider", "startTime", "endTime", "status", "metadata", "request_tags", "api_base", "user", "team_id", "organization_id"]
        select = ", ".join(f'"{column}"' if column in self.available_columns else f'NULL AS "{column}"' for column in columns)
        query = f"""
            SELECT {select}
            FROM "LiteLLM_SpendLogs"
            WHERE "startTime" >= $1 AND "startTime" < $2
              AND ($3 = '' OR "model" = $3 OR COALESCE("model_group", '') = $3)
            ORDER BY "startTime" DESC
        """
        try:
            records = await self.pool.fetch(query, start, end, model.strip())
        except Exception as exc:
            raise RuntimeError("稳定性数据源查询失败") from exc
        rows = []
        for record in records:
            item = dict(record)
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            info = metadata.get("error_information") or metadata.get("errorInformation") or {}
            if not isinstance(info, dict):
                info = {}
            item.update({
                "event_date": item.get("startTime"),
                "event_time": item.get("startTime"),
                "provider": item.get("custom_llm_provider") or item.get("api_base") or "",
                "status_code": str(info.get("status_code") or metadata.get("status_code") or item.get("status") or ""),
                "error_code": str(info.get("error_code") or metadata.get("error_code") or ""),
                "error_class": str(info.get("error_class") or metadata.get("error_class") or ""),
                "error_message": str(info.get("error_message") or metadata.get("error_message") or ""),
            })
            rows.append(item)
        return rows


def configured_litellm_reader() -> LiteLLMStabilityReader | None:
    dsn = os.getenv("LITELLM_DATABASE_URL", "").strip()
    return LiteLLMStabilityReader(dsn) if dsn else None
