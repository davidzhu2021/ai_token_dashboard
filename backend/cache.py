import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

try:
    import redis.asyncio as redis
except ImportError:  # pragma: no cover - Redis is optional outside Compose
    redis = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    value: Any
    expires_at: float


class TTLCache:
    def __init__(self) -> None:
        self._items: dict[str, CacheEntry] = {}

    def get(self, key: str) -> tuple[bool, Any, int]:
        now = time.time()
        entry = self._items.get(key)
        if not entry:
            return False, None, 0
        if entry.expires_at <= now:
            self._items.pop(key, None)
            return False, None, 0
        return True, entry.value, max(0, int(entry.expires_at - now))

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        self._items[key] = CacheEntry(value=value, expires_at=time.time() + ttl_seconds)

    def delete(self, key: str) -> None:
        self._items.pop(key, None)

    def clear(self) -> None:
        self._items.clear()


class AsyncJSONCache:
    """Small Redis-backed JSON cache that remains optional at runtime."""

    def __init__(self, *, url: str | None = None, client: Any | None = None, ttl_seconds: int = 60) -> None:
        self._url = url if url is not None else os.getenv("USAGE_REDIS_URL", "").strip()
        self._client = client
        self._ttl_seconds = max(1, ttl_seconds)
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._local: dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def _redis(self) -> Any | None:
        if self._client is not None:
            return self._client
        if not self._url or redis is None:
            return None
        # A cache outage must not extend a dashboard request's latency budget.
        self._client = redis.from_url(
            self._url, decode_responses=True, socket_connect_timeout=0.2, socket_timeout=0.2
        )
        return self._client

    async def get(self, key: str) -> Any | None:
        local = self._local.get(key)
        if local and local.expires_at > time.time():
            return local.value
        if local:
            self._local.pop(key, None)
        try:
            client = await self._redis()
            if client is None:
                return None
            value = await client.get(key)
            parsed = json.loads(value) if value else None
            if parsed is not None:
                self._local[key] = CacheEntry(parsed, time.time() + self._ttl_seconds)
            return parsed
        except Exception:
            logger.warning("drilldown cache read unavailable")
            return None

    async def set(self, key: str, value: Any) -> None:
        self._local[key] = CacheEntry(value, time.time() + self._ttl_seconds)
        try:
            client = await self._redis()
            if client is not None:
                await client.set(key, json.dumps(value, ensure_ascii=True, separators=(",", ":"), default=str), ex=self._ttl_seconds)
        except Exception:
            logger.warning("drilldown cache write unavailable")

    async def get_or_set(self, key: str, factory: Any) -> Any:
        cached = await self.get(key)
        if cached is not None:
            return cached
        async with self._lock:
            task = self._tasks.get(key)
            if task is None or task.done():
                task = asyncio.create_task(factory())
                self._tasks[key] = task
                task.add_done_callback(lambda completed, cache_key=key: self._tasks.pop(cache_key, None) if self._tasks.get(cache_key) is completed else None)
        value = await asyncio.shield(task)
        await self.set(key, value)
        return value

    async def invalidate_prefix(self, prefix: str) -> None:
        for key in list(self._local):
            if key.startswith(prefix):
                self._local.pop(key, None)
        try:
            client = await self._redis()
            if client is None:
                return
            keys = [key async for key in client.scan_iter(match=f"{prefix}*")]
            if keys:
                await client.delete(*keys)
        except Exception:
            logger.warning("drilldown cache invalidation unavailable")

    def clear_local(self) -> None:
        """Forget in-flight work after a write so the next request rebuilds."""

        for key in list(self._tasks):
            if key.startswith("observability:drilldown:"):
                self._tasks.pop(key, None)
        self._local.clear()
