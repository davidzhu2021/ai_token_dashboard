from backend.cache import AsyncStaleJSONCache


def test_stale_json_cache_keeps_value_until_stale_expiry(monkeypatch):
    cache = AsyncStaleJSONCache(url="", ttl_seconds=1, stale_seconds=3)
    import asyncio

    asyncio.run(cache.set("key", {"complete": True, "totals": {"spend": 1.25}}))

    fresh = asyncio.run(cache.get("key"))
    assert fresh["value"]["totals"]["spend"] == 1.25
    assert fresh["ageSeconds"] >= 0

    monkeypatch.setattr("backend.cache.time.time", lambda: fresh["cachedAt"] + 2)
    stale = asyncio.run(cache.get("key"))
    assert stale["value"]["complete"] is True
    assert abs(stale["ageSeconds"] - 2) < 0.01


def test_stale_json_cache_expires_after_stale_window(monkeypatch):
    cache = AsyncStaleJSONCache(url="", ttl_seconds=1, stale_seconds=3)
    import asyncio

    asyncio.run(cache.set("key", {"complete": True}))
    entry = asyncio.run(cache.get("key"))
    monkeypatch.setattr("backend.cache.time.time", lambda: entry["cachedAt"] + 4)
    assert asyncio.run(cache.get("key")) is None
