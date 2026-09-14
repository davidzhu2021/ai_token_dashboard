import asyncio
from datetime import datetime, timezone
from typing import Any

from backend.usage_store import UsageStore


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _StateConnection:
    """Small asyncpg-shaped fake that applies publish statements to state."""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.usage_event_attribution: dict[tuple[str, str], tuple[Any, ...]] = {}
        self.stability_attempt_events: dict[tuple[str, str], tuple[Any, ...]] = {}
        self.sync_state: tuple[Any, ...] | None = None

    def transaction(self):
        return _Transaction()

    async def execute(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        if normalized.startswith("DELETE FROM usage_event_attribution"):
            backend_id, start_date, end_date = args[:3]
            self.usage_event_attribution = {
                key: record
                for key, record in self.usage_event_attribution.items()
                if not (
                    record[0] == backend_id
                    and start_date <= record[3] <= end_date
                    and record[18] != "legacy_report_only"
                )
            }
        elif normalized.startswith("DELETE FROM stability_attempt_events"):
            backend_id, start_date, end_date = args[:3]
            self.stability_attempt_events = {
                key: record
                for key, record in self.stability_attempt_events.items()
                if not (
                    record[0] == backend_id
                    and start_date <= record[20] <= end_date
                    and record[11] == "final_request"
                )
            }
        elif normalized.startswith("INSERT INTO stability_sync_state"):
            self.sync_state = args

    async def executemany(self, query: str, records: list[tuple[Any, ...]]) -> None:
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        if normalized.startswith("INSERT INTO usage_event_attribution"):
            for record in records:
                self.usage_event_attribution[(record[0], record[1])] = record
        elif normalized.startswith("INSERT INTO stability_attempt_events"):
            for record in records:
                self.stability_attempt_events.setdefault((record[0], record[1]), record)

    async def fetchval(self, query: str, *args: Any) -> int:
        if query.lstrip().startswith("SELECT COUNT(*) FROM usage_event_attribution"):
            backend_id, start_date, end_date = args[:3]
            return sum(
                record[0] == backend_id and start_date <= record[3] <= end_date
                for record in self.usage_event_attribution.values()
            )
        return 0


class _Acquire:
    def __init__(self, connection: _StateConnection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def __init__(self) -> None:
        self.connection = _StateConnection()

    def acquire(self):
        return _Acquire(self.connection)


def _event(request_id: str, event_date: str = "2026-08-02") -> dict[str, Any]:
    return {
        "backend_id": "primary",
        "requestId": request_id,
        "eventTime": f"{event_date}T10:00:00+00:00",
        "date": event_date,
        "userId": "user-1",
        "model": "gpt-4o",
        "status": "success",
        "requestCount": 1,
        "successCount": 1,
        "totalTokens": 4,
    }


def _seed(store: UsageStore, pool: _Pool) -> None:
    collected_at = datetime.now(timezone.utc)
    old_event = _event("old-request")
    old_usage = store._event_record("primary", old_event, collected_at)
    assert old_usage is not None
    old_attempt = store._stability_final_request_record(old_event, collected_at)
    assert old_attempt is not None
    pool.connection.usage_event_attribution[(old_usage[0], old_usage[1])] = old_usage
    pool.connection.stability_attempt_events[(old_attempt[0], old_attempt[1])] = old_attempt


def _publish(*, complete: bool) -> _Pool:
    store = UsageStore("postgresql://unused")
    pool = _Pool()
    store.pool = pool
    _seed(store, pool)
    asyncio.run(
        store.publish_stability_events(
            "primary",
            "2026-08-01",
            "2026-08-07",
            [_event("new-request")],
            "2026-08-01",
            "2026-08-07",
            complete,
        )
    )
    return pool


def test_partial_stability_publish_preserves_existing_window_records() -> None:
    pool = _publish(complete=False)
    usage_ids = {key[1] for key in pool.connection.usage_event_attribution}
    attempt_ids = {key[1] for key in pool.connection.stability_attempt_events}

    assert usage_ids == {"old-request", "new-request"}
    assert attempt_ids == {"old-request", "new-request"}
    assert pool.connection.sync_state is not None
    assert pool.connection.sync_state[3:5] == ("partial", True)


def test_complete_stability_publish_replaces_existing_window_records() -> None:
    pool = _publish(complete=True)
    usage_ids = {key[1] for key in pool.connection.usage_event_attribution}
    attempt_ids = {key[1] for key in pool.connection.stability_attempt_events}

    assert usage_ids == {"new-request"}
    assert attempt_ids == {"new-request"}
    assert pool.connection.sync_state is not None
    assert pool.connection.sync_state[3:5] == ("complete", False)


def test_partial_multi_backend_snapshot_skips_event_window_delete() -> None:
    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Connection:
        def __init__(self):
            self.queries: list[str] = []

        def transaction(self):
            return Transaction()

        async def execute(self, query, *_args):
            self.queries.append(" ".join(query.split()))

        async def copy_records_to_table(self, *_args, **_kwargs):
            return None

        async def fetchval(self, *_args):
            query = str(_args[0]) if _args else ""
            return 0 if "COUNT(*)" in query else "2026-08-07 00:00:00+00"

    class Acquire:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, *_args):
            return None

    class Pool:
        def __init__(self):
            self.connection = Connection()

        def acquire(self):
            return Acquire(self.connection)

    snapshot = type(
        "Snapshot",
        (),
        {
            "backend_id": "primary",
            "rows": [{"date": "2026-08-07", "userId": "user-1", "source": "Codex", "model": "gpt-4o"}],
            "memberships": [],
            "events": [_event("partial-request")],
            "departments": [],
            "event_start_date": "2026-08-01",
            "event_end_date": "2026-08-07",
            "event_replace_start_date": "2026-08-07",
            "event_replace_end_date": "2026-08-07",
            "events_complete": False,
            "event_window_complete": False,
        },
    )()
    store = UsageStore("postgresql://unused")
    pool = Pool()
    store.pool = pool

    asyncio.run(store.publish_snapshots("2026-08-07", "2026-08-07", [snapshot]))

    assert not any(
        query.startswith("DELETE FROM usage_event_attribution")
        for query in pool.connection.queries
    )
