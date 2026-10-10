import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.usage_store import UsageStore
from backend.usage_sync import UsageSynchronizer


def snapshot(backend, complete=True):
    return SimpleNamespace(
        backend_id=backend, rows=[], memberships=[], events=None,
        departments=[], identities=[], quality={"complete": complete},
    )


@pytest.mark.parametrize("bad_source", ["primary", "her"])
@pytest.mark.parametrize("failure", ["sparse", "exception"])
def test_collection_failure_never_blocks_other_source(bad_source, failure):
    good_source = "her" if bad_source == "primary" else "primary"
    store = SimpleNamespace(
        begin_sync_run=AsyncMock(return_value=1),
        try_acquire_sync_lock=AsyncMock(return_value=object()),
        release_sync_lock=AsyncMock(), finish_sync_run=AsyncMock(),
        publish_snapshots=AsyncMock(return_value={"rowCount": 0, "snapshotRevision": "new"}),
    )
    sync = UsageSynchronizer(SimpleNamespace(backends=[
        SimpleNamespace(id="primary"), SimpleNamespace(id="her")]), store)
    sync._identity_directory = AsyncMock(return_value={})
    sync._refresh_historical_identity = AsyncMock()

    async def collect(backend, *args):
        if backend.id == bad_source and failure == "exception":
            raise TimeoutError()
        return snapshot(backend.id, backend.id != bad_source)

    sync.collect_backend = collect
    result = asyncio.run(sync.sync("2026-10-01", "2026-10-01"))
    assert result["status"] == "partial"
    assert result["publishedBackends"] == [good_source]
    assert [s.backend_id for s in store.publish_snapshots.call_args.args[2]] == [good_source]
    assert [s.backend_id for s in sync._refresh_historical_identity.call_args.args[0]] == [good_source]
    store.release_sync_lock.assert_awaited_once()


@pytest.mark.parametrize("healthy", ["primary", "her", None])
def test_real_publisher_only_deletes_and_covers_healthy_source(healthy):
    calls = []

    class Connection:
        @asynccontextmanager
        async def transaction(self):
            yield self

        async def execute(self, query, *args):
            calls.append((query, args))

        async def fetchval(self, *args):
            return "revision"

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    store = UsageStore("postgresql://unused")
    store.pool = Pool()
    result = asyncio.run(store.publish_snapshots("2026-10-01", "2026-10-01", [
        snapshot(source, source == healthy) for source in ["primary", "her"]]))
    if healthy is None:
        assert not calls
        assert result["snapshotRevision"] is None
    else:
        deletions = [(q, args) for q, args in calls if q.startswith("DELETE")]
        assert len(deletions) == 4
        assert all(args[0] == [healthy] for _, args in deletions)
        coverage = [args for q, args in calls if "INSERT INTO usage_sync_coverage" in q]
        assert coverage[0][0] == [healthy]
    assert result["status"] == "partial"


def test_all_sparse_does_not_publish_or_refresh_identity():
    store = SimpleNamespace(
        begin_sync_run=AsyncMock(return_value=1),
        try_acquire_sync_lock=AsyncMock(return_value=object()),
        release_sync_lock=AsyncMock(), finish_sync_run=AsyncMock(),
        publish_snapshots=AsyncMock(),
    )
    sync = UsageSynchronizer(SimpleNamespace(backends=[SimpleNamespace(id="her")]), store)
    sync._identity_directory = AsyncMock(return_value={})
    sync.collect_backend = AsyncMock(return_value=snapshot("her", False))
    sync._refresh_historical_identity = AsyncMock()
    result = asyncio.run(sync.sync("2026-10-01", "2026-10-01"))
    assert result["status"] == "failed"
    store.publish_snapshots.assert_not_awaited()
    sync._refresh_historical_identity.assert_awaited_once_with([])
