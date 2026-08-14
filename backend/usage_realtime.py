from __future__ import annotations

import json
import os
import socket
from datetime import date, datetime, timezone
from typing import Any

try:
    import redis.asyncio as redis
except ImportError:  # pragma: no cover - optional when realtime usage is disabled
    redis = None  # type: ignore[assignment]


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def realtime_enabled() -> bool:
    return _env_bool("USAGE_REALTIME_ENABLED", True) and bool(
        os.getenv("USAGE_REDIS_URL", "").strip()
    )


def _text(value: Any) -> str:
    return str(value or "").strip()


class UsageRealtimeStore:
    prefix = "usage:realtime"
    stream_key = f"{prefix}:archive"
    consumer_group = "usage-archive"

    _LOCK_RENEW_LUA = """
local key = KEYS[1]
if redis.call('GET', key) ~= ARGV[1] then
  return 0
end
return redis.call('EXPIRE', key, ARGV[2])
"""

    _LOCK_RELEASE_LUA = """
local key = KEYS[1]
if redis.call('GET', key) ~= ARGV[1] then
  return 0
end
return redis.call('DEL', key)
"""

    _INGEST_LUA = """
local dedup_key = KEYS[1]
local aggregate_key = KEYS[2]
local aggregate_index = KEYS[3]
local revision_key = KEYS[4]
local stream_key = KEYS[5]
local latest_event_key = KEYS[6]
local event_key = KEYS[7]

if redis.call('SET', dedup_key, '1', 'NX', 'EX', ARGV[1]) == false then
  return {0, redis.call('GET', revision_key) or '0'}
end

redis.call('HSET', aggregate_key,
  'backendId', ARGV[2], 'date', ARGV[3], 'userId', ARGV[4],
  'employeeEmail', ARGV[5], 'employeeName', ARGV[6], 'emailSource', ARGV[7],
  'source', ARGV[8], 'model', ARGV[9], 'organizationId', ARGV[10],
  'teamId', ARGV[11], 'keyId', ARGV[12], 'principalId', ARGV[13],
  'attributionSource', ARGV[14], 'billingEligible', ARGV[15])
redis.call('HINCRBY', aggregate_key, 'promptTokens', ARGV[16])
redis.call('HINCRBY', aggregate_key, 'completionTokens', ARGV[17])
redis.call('HINCRBY', aggregate_key, 'totalTokens', ARGV[18])
redis.call('HINCRBY', aggregate_key, 'requestCount', ARGV[19])
redis.call('HINCRBY', aggregate_key, 'successCount', ARGV[20])
redis.call('HINCRBY', aggregate_key, 'failureCount', ARGV[21])
redis.call('HINCRBYFLOAT', aggregate_key, 'spend', ARGV[22])
redis.call('EXPIRE', aggregate_key, ARGV[1])
redis.call('SADD', aggregate_index, aggregate_key)
redis.call('EXPIRE', aggregate_index, ARGV[1])
redis.call('SET', event_key, ARGV[23], 'EX', ARGV[1])
local revision = redis.call('INCR', revision_key)
redis.call('SET', latest_event_key, ARGV[24])
if ARGV[25] == '1' then
  redis.call('XADD', stream_key, '*', 'event', ARGV[23])
end
return {1, revision}
"""

    def __init__(self, url: str) -> None:
        if redis is None:
            raise RuntimeError("USAGE_REALTIME_ENABLED=true requires redis")
        self.url = url
        self.client = redis.from_url(url, decode_responses=True)
        self.ttl_seconds = max(
            3600, _env_int("USAGE_REALTIME_EVENT_TTL_SECONDS", 259200)
        )
        self.lock_ttl_seconds = max(
            60, _env_int("USAGE_REALTIME_LOCK_TTL_SECONDS", 300)
        )
        self.consumer_name = f"{socket.gethostname()}-{os.getpid()}"
        self._ingest_script: Any | None = None
        self._lock_renew_script: Any | None = None
        self._lock_release_script: Any | None = None
        self._connected = False

    @classmethod
    def from_environment(cls) -> UsageRealtimeStore | None:
        if not realtime_enabled():
            return None
        return cls(os.getenv("USAGE_REDIS_URL", "").strip())

    async def connect(self) -> None:
        if self._connected:
            return
        await self.client.ping()
        self._ingest_script = self.client.register_script(self._INGEST_LUA)
        self._lock_renew_script = self.client.register_script(self._LOCK_RENEW_LUA)
        self._lock_release_script = self.client.register_script(self._LOCK_RELEASE_LUA)
        try:
            await self.client.xgroup_create(
                self.stream_key, self.consumer_group, id="0", mkstream=True
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._connected = True

    async def close(self) -> None:
        await self.client.aclose()
        self._connected = False

    @staticmethod
    def _aggregate_fingerprint(event: dict[str, Any]) -> str:
        fields = (
            "backendId",
            "date",
            "_userId",
            "source",
            "model",
            "organizationId",
            "teamId",
            "keyId",
            "principalId",
            "attributionSource",
            "billingEligible",
        )
        import hashlib

        encoded = json.dumps(
            [_text(event.get(field)) for field in fields],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    async def ingest_event(
        self, backend_id: str, event: dict[str, Any], *, archive: bool = True
    ) -> tuple[bool, int]:
        if self._ingest_script is None:
            await self.connect()
        request_id = _text(event.get("requestId"))
        usage_date = _text(event.get("date"))
        if not request_id or not usage_date:
            return False, await self.revision()
        aggregate_id = self._aggregate_fingerprint(
            {**event, "backendId": backend_id}
        )
        event_payload = json.dumps(
            {**event, "backendId": backend_id},
            ensure_ascii=True,
            separators=(",", ":"),
            default=str,
        )
        event_time = _text(event.get("eventTime")) or datetime.now(
            timezone.utc
        ).isoformat()
        result = await self._ingest_script(
            keys=[
                f"{self.prefix}:dedup:{backend_id}:{request_id}",
                f"{self.prefix}:aggregate:{usage_date}:{aggregate_id}",
                f"{self.prefix}:aggregate-index:{usage_date}",
                f"{self.prefix}:revision",
                self.stream_key,
                f"{self.prefix}:latest-event",
                f"{self.prefix}:event:{backend_id}:{request_id}",
            ],
            args=[
                self.ttl_seconds,
                backend_id,
                usage_date,
                _text(event.get("_userId") or event.get("userId")) or "unknown",
                _text(event.get("employeeEmail")),
                _text(event.get("employeeName")),
                _text(event.get("emailSource")),
                _text(event.get("source")) or "其他",
                _text(event.get("model")) or "未知模型",
                _text(event.get("organizationId")),
                _text(event.get("teamId")),
                _text(event.get("keyId")),
                _text(event.get("principalId")),
                _text(event.get("attributionSource")) or "unattributed",
                "1" if event.get("billingEligible") else "0",
                int(event.get("promptTokens") or 0),
                int(event.get("completionTokens") or 0),
                int(event.get("totalTokens") or 0),
                int(event.get("requestCount") or 0),
                int(event.get("successCount") or 0),
                int(event.get("failureCount") or 0),
                float(event.get("spend") or 0),
                event_payload,
                event_time,
                "1" if archive else "0",
            ],
        )
        return bool(int(result[0])), int(result[1])

    async def seed_aggregate(self, backend_id: str, row: dict[str, Any]) -> None:
        aggregate_id = self._aggregate_fingerprint(
            {**row, "backendId": backend_id, "_userId": row.get("userId")}
        )
        usage_date = _text(row.get("date"))
        key = f"{self.prefix}:aggregate:{usage_date}:{aggregate_id}"
        values = {
            "backendId": backend_id,
            "date": usage_date,
            "userId": _text(row.get("userId")),
            "employeeEmail": _text(row.get("employeeEmail")),
            "employeeName": _text(row.get("employeeName")),
            "emailSource": _text(row.get("emailSource")),
            "source": _text(row.get("source")),
            "model": _text(row.get("model")),
            "organizationId": _text(row.get("organizationId")),
            "teamId": _text(row.get("teamId")),
            "keyId": _text(row.get("keyId")),
            "principalId": _text(row.get("principalId")),
            "attributionSource": _text(row.get("attributionSource")),
            "billingEligible": "1" if row.get("billingEligible") else "0",
            "promptTokens": int(row.get("promptTokens") or 0),
            "completionTokens": int(row.get("completionTokens") or 0),
            "totalTokens": int(row.get("totalTokens") or 0),
            "requestCount": int(row.get("requestCount") or 0),
            "successCount": int(row.get("successCount") or 0),
            "failureCount": int(row.get("failureCount") or 0),
            "spend": float(row.get("spend") or 0),
        }
        pipe = self.client.pipeline()
        pipe.hset(key, mapping=values)
        pipe.expire(key, self.ttl_seconds)
        pipe.sadd(f"{self.prefix}:aggregate-index:{usage_date}", key)
        pipe.expire(f"{self.prefix}:aggregate-index:{usage_date}", self.ttl_seconds)
        await pipe.execute()

    async def seed_request_ids(self, records: list[tuple[str, str]]) -> None:
        if not records:
            return
        pipe = self.client.pipeline()
        for backend_id, request_id in records:
            pipe.set(
                f"{self.prefix}:dedup:{backend_id}:{request_id}",
                "1",
                ex=self.ttl_seconds,
            )
        await pipe.execute()

    async def clear_day(self, usage_date: date | str) -> None:
        day = usage_date.isoformat() if isinstance(usage_date, date) else usage_date
        index_key = f"{self.prefix}:aggregate-index:{day}"
        aggregate_keys = list(await self.client.smembers(index_key))
        if aggregate_keys:
            await self.client.delete(*aggregate_keys)
        await self.client.delete(index_key)

    async def aggregate_rows(self, usage_date: date | str) -> list[dict[str, Any]]:
        day = usage_date.isoformat() if isinstance(usage_date, date) else usage_date
        keys = sorted(await self.client.smembers(f"{self.prefix}:aggregate-index:{day}"))
        if not keys:
            return []
        pipe = self.client.pipeline()
        for key in keys:
            pipe.hgetall(key)
        records = await pipe.execute()
        integer_fields = {
            "promptTokens",
            "completionTokens",
            "totalTokens",
            "requestCount",
            "successCount",
            "failureCount",
        }
        rows: list[dict[str, Any]] = []
        for record in records:
            if not record:
                continue
            row = dict(record)
            for field in integer_fields:
                row[field] = int(float(row.get(field) or 0))
            row["spend"] = float(row.get("spend") or 0)
            row["billingEligible"] = row.get("billingEligible") == "1"
            rows.append(row)
        return rows

    async def revision(self) -> int:
        return int(await self.client.get(f"{self.prefix}:revision") or 0)

    async def set_cursor(self, backend_id: str, value: datetime) -> None:
        await self.client.hset(
            f"{self.prefix}:cursors", backend_id, value.astimezone(timezone.utc).isoformat()
        )

    async def cursor(self, backend_id: str) -> datetime | None:
        value = await self.client.hget(f"{self.prefix}:cursors", backend_id)
        if not value:
            return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    async def set_ready(self, ready: bool) -> None:
        await self.client.set(f"{self.prefix}:ready", "1" if ready else "0")

    async def ready(self) -> bool:
        return await self.client.get(f"{self.prefix}:ready") == "1"

    async def acquire_worker_lock(self, worker_id: str, ttl_seconds: int | None = None) -> bool:
        ttl_seconds = max(1, int(ttl_seconds or self.lock_ttl_seconds))
        return bool(
            await self.client.set(
                f"{self.prefix}:worker-lock", worker_id, nx=True, ex=ttl_seconds
            )
        )

    async def renew_worker_lock(self, worker_id: str, ttl_seconds: int | None = None) -> bool:
        key = f"{self.prefix}:worker-lock"
        ttl_seconds = max(1, int(ttl_seconds or self.lock_ttl_seconds))
        script = self._lock_renew_script
        if script is None:
            script = self.client.register_script(self._LOCK_RENEW_LUA)
            self._lock_renew_script = script
        return bool(
            await script(keys=[key], args=[worker_id, str(ttl_seconds)])
        )

    async def release_worker_lock(self, worker_id: str) -> None:
        key = f"{self.prefix}:worker-lock"
        script = self._lock_release_script
        if script is None:
            script = self.client.register_script(self._LOCK_RELEASE_LUA)
            self._lock_release_script = script
        await script(keys=[key], args=[worker_id])

    async def read_archive_batch(
        self, count: int = 200, block_ms: int = 100
    ) -> list[tuple[str, dict[str, Any]]]:
        # Claim stale messages first so a worker restart cannot strand events
        # in another consumer's pending list forever.
        records = []
        try:
            claimed = await self.client.xautoclaim(
                self.stream_key,
                self.consumer_group,
                self.consumer_name,
                min_idle_time=30_000,
                start_id="0-0",
                count=count,
            )
            records = [(self.stream_key, claimed[1])] if claimed and claimed[1] else []
        except (AttributeError, TypeError, NotImplementedError):
            # Older redis clients/test doubles may not expose XAUTOCLAIM.
            records = []
        if not records:
            records = await self.client.xreadgroup(
                self.consumer_group,
                self.consumer_name,
                {self.stream_key: "0"},
                count=count,
                block=0,
            )
        if not records:
            records = await self.client.xreadgroup(
                self.consumer_group,
                self.consumer_name,
                {self.stream_key: ">"},
                count=count,
                block=block_ms,
            )
        output: list[tuple[str, dict[str, Any]]] = []
        for _stream, messages in records or []:
            for message_id, fields in messages:
                try:
                    output.append((message_id, json.loads(fields.get("event") or "{}")))
                except json.JSONDecodeError:
                    output.append((message_id, {}))
        return output

    async def acknowledge(self, message_ids: list[str]) -> None:
        if message_ids:
            await self.client.xack(self.stream_key, self.consumer_group, *message_ids)

    async def status(self) -> dict[str, Any]:
        await self.client.ping()
        latest = await self.client.get(f"{self.prefix}:latest-event")
        latest_dt: datetime | None = None
        if latest:
            try:
                latest_dt = datetime.fromisoformat(latest.replace("Z", "+00:00"))
                if latest_dt.tzinfo is None:
                    latest_dt = latest_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                latest_dt = None
        pending = await self.client.xpending(self.stream_key, self.consumer_group)
        cursors = await self.client.hgetall(f"{self.prefix}:cursors")
        return {
            "connected": True,
            "ready": await self.ready(),
            "revision": await self.revision(),
            "latestEventAt": latest_dt,
            "latestEventLagSeconds": (
                max(0, int((datetime.now(timezone.utc) - latest_dt).total_seconds()))
                if latest_dt
                else None
            ),
            "pendingArchiveCount": int((pending or {}).get("pending") or 0),
            "cursors": cursors,
        }
