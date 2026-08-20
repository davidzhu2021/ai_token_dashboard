from __future__ import annotations

import asyncio

from backend.cache import AsyncJSONCache


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def set(self, key: str, value: str, **_kwargs):
        self.values[key] = value

    async def scan_iter(self, match: str):
        prefix = match.removesuffix("*")
        for key in list(self.values):
            if key.startswith(prefix):
                yield key

    async def delete(self, *keys: str):
        for key in keys:
            self.values.pop(key, None)


def test_async_json_cache_reuses_value_and_invalidates_prefix() -> None:
    async def run() -> None:
        cache = AsyncJSONCache(client=FakeRedis())
        calls = 0

        async def build():
            nonlocal calls
            calls += 1
            return {"items": [calls]}

        assert await cache.get_or_set("observability:stability:one", build) == {"items": [1]}
        assert await cache.get_or_set("observability:stability:one", build) == {"items": [1]}
        assert calls == 1
        await cache.invalidate_prefix("observability:stability:")
        assert await cache.get_or_set("observability:stability:one", build) == {"items": [2]}

    asyncio.run(run())


def test_async_json_cache_collapses_concurrent_misses() -> None:
    async def run() -> None:
        cache = AsyncJSONCache(client=FakeRedis())
        calls = 0
        release = asyncio.Event()
        started = asyncio.Event()

        async def build():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {"ok": True}

        first = asyncio.create_task(cache.get_or_set("observability:cost:one", build))
        second = asyncio.create_task(cache.get_or_set("observability:cost:one", build))
        await started.wait()
        assert calls == 1
        release.set()
        assert await asyncio.gather(first, second) == [{"ok": True}, {"ok": True}]

    asyncio.run(run())


def test_async_json_cache_falls_back_when_redis_fails() -> None:
    class BrokenRedis:
        async def get(self, _key: str):
            raise OSError("unavailable")

    async def run() -> None:
        cache = AsyncJSONCache(client=BrokenRedis())
        assert await cache.get_or_set("observability:cost:one", lambda: asyncio.sleep(0, result={"ok": True})) == {"ok": True}

    asyncio.run(run())


def test_clearing_local_cache_does_not_cancel_active_request_or_restore_stale_value() -> None:
    async def run() -> None:
        cache = AsyncJSONCache(client=FakeRedis())
        release = asyncio.Event()
        started = asyncio.Event()
        calls = 0

        async def build():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {"version": calls}

        active = asyncio.create_task(cache.get_or_set("observability:drilldown:stability:one", build))
        await started.wait()
        cache.clear_local()
        release.set()
        assert await active == {"version": 1}
        assert await cache.get_or_set("observability:drilldown:stability:one", build) == {"version": 2}
        assert calls == 2

    asyncio.run(run())
