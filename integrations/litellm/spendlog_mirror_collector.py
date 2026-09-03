"""Read-only LiteLLM SpendLogs mirror collector for the 198 cluster."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import httpx

LOG = logging.getLogger("spendlog-mirror-collector")


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value or "")


def build_event(row: dict[str, Any], *, source: str, principal_salt: str, collected_at: datetime) -> dict[str, Any]:
    start = row.get("startTime") or row.get("start_time")
    end = row.get("endTime") or row.get("end_time") or start
    event: dict[str, Any] = {
        "sourceRequestId": str(row.get("request_id") or ""),
        "eventTime": _iso(start),
        "model": str(row.get("model_group") or row.get("model") or ""),
        "actualModel": str(row.get("model") or ""),
        "modelGroup": str(row.get("model_group") or ""),
        "provider": str(row.get("custom_llm_provider") or ""),
        "status": str(row.get("status") or "unknown"),
        "teamId": str(row.get("team_id") or ""),
        "organizationId": str(row.get("organization_id") or ""),
        "promptTokens": int(row.get("prompt_tokens") or 0),
        "completionTokens": int(row.get("completion_tokens") or 0),
        "totalTokens": int(row.get("total_tokens") or 0),
        "collectedAt": _iso(collected_at),
    }
    user = str(row.get("user") or "")
    if user:
        event["principalHash"] = hashlib.sha256(f"{principal_salt}:{user}".encode()).hexdigest()
    if isinstance(start, datetime) and isinstance(end, datetime):
        event["requestDurationMs"] = round((end - start).total_seconds() * 1000, 2)
    completion = row.get("completionStartTime") or row.get("completion_start_time")
    if isinstance(start, datetime) and isinstance(completion, datetime):
        event["ttftMs"] = round((completion - start).total_seconds() * 1000, 2)
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    error = metadata.get("error_information") if isinstance(metadata, dict) else {}
    if isinstance(error, dict) and error.get("error_code"):
        event["errorCode"] = str(error["error_code"])
    return {k: v for k, v in event.items() if v not in (None, "")}


def signed_headers(body: bytes, secret: str, *, timestamp: int | None = None) -> dict[str, str]:
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    digest = hashlib.sha256(body).hexdigest()
    signature = hmac.new(secret.encode(), f"{stamp}.{digest}".encode(), hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "x-observability-timestamp": stamp, "x-observability-signature": f"sha256={signature}"}


async def collect_once() -> int:
    dsn = os.environ["LITELLM_DATABASE_URL"]
    url = os.environ["OBSERVABILITY_INGEST_URL"]
    secret = os.environ["OBSERVABILITY_INGEST_HMAC_SECRET"]
    source = os.getenv("OBSERVABILITY_BACKEND_ID", "litellm-198")
    salt = os.environ["OBSERVABILITY_PRINCIPAL_SALT"]
    days = max(1, int(os.getenv("OBSERVABILITY_COLLECTOR_LOOKBACK_DAYS", "2")))
    collected = datetime.now(timezone.utc)
    since = collected - timedelta(days=days)
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch('SELECT request_id, "startTime", "endTime", "completionStartTime", model, model_group, custom_llm_provider, status, "user", team_id, organization_id, prompt_tokens, completion_tokens, total_tokens, metadata FROM "LiteLLM_SpendLogs" WHERE "startTime" >= $1 ORDER BY "startTime" ASC', since)
    finally:
        await conn.close()
    events = [build_event(dict(row), source=source, principal_salt=salt, collected_at=collected) for row in rows]
    events = [event for event in events if event.get("sourceRequestId")]
    if not events:
        return 0
    async with httpx.AsyncClient(timeout=float(os.getenv("OBSERVABILITY_INGEST_TIMEOUT_SECONDS", "10"))) as client:
        for offset in range(0, len(events), 100):
            body = json.dumps({"events": events[offset:offset + 100]}, separators=(",", ":")).encode()
            response = await client.post(url, content=body, headers=signed_headers(body, secret))
            response.raise_for_status()
    return len(events)


async def main() -> None:
    interval = max(30, int(os.getenv("OBSERVABILITY_COLLECTOR_INTERVAL_SECONDS", "300")))
    while True:
        try:
            LOG.info("mirrored events=%s", await collect_once())
        except Exception:
            LOG.exception("collector cycle failed")
        await asyncio.sleep(interval)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
