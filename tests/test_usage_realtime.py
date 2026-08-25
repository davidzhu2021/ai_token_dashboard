from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone

from backend.litellm_client import LiteLLMBackend, LiteLLMClient
from backend.usage_realtime import UsageRealtimeStore
from backend.usage_realtime_worker import UsageRealtimeWorker, new_worker_id
from backend.usage_store import UsageStore


BACKEND = LiteLLMBackend(
    id="primary", label="Tongqu API", base_url="http://upstream", admin_key="key"
)


def test_realtime_event_keeps_identity_source_when_enriched() -> None:
    worker = UsageRealtimeWorker.__new__(UsageRealtimeWorker)
    worker.directory = {"byUserId": {"u-1": {"name": "张三", "email": "zhangsan@example.com"}}}
    worker.token_maps = {}
    worker.team_by_user = {}
    worker.synchronizer = type("Sync", (), {
        "_reclassify_primary_her_usage": lambda *args: 0,
        "_apply_token_attribution": lambda *args: None,
    })()
    result = worker._enrich_event(BACKEND, {"_userId": "u-1", "totalTokens": 1})
    assert result["employeeName"] == "张三"
    assert result["employeeEmail"] == "zhangsan@example.com"
    assert result["nameSource"] == "identity_directory"


class IncrementalClient(LiteLLMClient):
    def __init__(self, pages):
        self.backends = [BACKEND]
        self._backend_map = {BACKEND.id: BACKEND}
        self._deployment_model_maps = {BACKEND.id: {}}
        self.pages = pages
        self.requests = []

    async def _ensure_deployment_model_map(self, _backend):
        return {}

    async def request_backend(self, backend, method, path, **kwargs):
        assert (backend.id, method, path) == ("primary", "GET", "/spend/logs/v2")
        params = kwargs["params"]
        self.requests.append(params)
        page = int(params["page"])
        return {
            "data": self.pages[page - 1],
            "total_pages": len(self.pages),
            "page": page,
        }


def test_incremental_spend_logs_use_small_sorted_window_and_return_events() -> None:
    client = IncrementalClient(
        [
            [
                {
                    "request_id": "req-1",
                    "user": "alice",
                    "startTime": "2026-08-13T02:00:01Z",
                    "model": "gpt-5",
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                    "spend": 0.1,
                    "status": "success",
                }
            ]
        ]
    )

    events, complete = asyncio.run(
        client.incremental_events_from_logs(
            datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 13, 2, 1, tzinfo=timezone.utc),
            BACKEND,
        )
    )

    assert complete is True
    assert events[0]["requestId"] == "req-1"
    assert events[0]["totalTokens"] == 5
    assert client.requests == [
        {
            "start_date": "2026-08-13 02:00:00",
            "end_date": "2026-08-13 02:01:00",
            "page": 1,
            "page_size": 100,
            "sort_by": "startTime",
            "sort_order": "asc",
        }
    ]


def test_incremental_spend_logs_resume_from_bounded_page_checkpoint() -> None:
    client = IncrementalClient([[], [], [], [], []])

    events, complete = asyncio.run(
        client.incremental_events_from_logs(
            datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 13, 2, 5, tzinfo=timezone.utc),
            BACKEND,
            start_page=2,
            max_pages=2,
        )
    )

    assert events == []
    assert complete is False
    assert [request["page"] for request in client.requests] == [2, 3]


def test_settled_spend_window_rejects_a_page_count_above_its_safety_limit() -> None:
    client = IncrementalClient([[], [], []])

    events, complete = asyncio.run(
        client.settled_events_from_logs(
            datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 13, 2, 1, tzinfo=timezone.utc),
            BACKEND,
            max_pages=2,
        )
    )

    assert events == []
    assert complete is False
    assert [request["page"] for request in client.requests] == [1]


def test_settled_spend_window_rejects_inconsistent_page_shape() -> None:
    class Client(IncrementalClient):
        async def request_backend(self, backend, method, path, **kwargs):
            self.requests.append(kwargs["params"])
            page = int(kwargs["params"]["page"])
            if page == 1:
                return {"data": [{"request_id": "req-1"}], "total_pages": 2, "page": 1}
            # The upstream changed its snapshot while the scan was in flight.
            return {"data": [], "total_pages": 2, "page": 2}

    client = Client([])
    events, complete = asyncio.run(
        client.settled_events_from_logs(
            datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 13, 2, 1, tzinfo=timezone.utc),
            BACKEND,
            max_pages=2,
        )
    )

    assert events == []
    assert complete is False


def test_failed_settlement_records_segment_error_without_advancing_watermark() -> None:
    errors = []
    advances = []

    class Client:
        async def settled_events_from_logs(self, *_args, **_kwargs):
            return [], False

    class Store:
        async def record_realtime_settlement_segment(self, **kwargs):
            errors.append(kwargs)

        async def advance_realtime_settlement(self, *args, **kwargs):
            advances.append((args, kwargs))

    worker = UsageRealtimeWorker.__new__(UsageRealtimeWorker)
    worker.client = Client()
    worker.store = Store()
    worker.realtime = None
    worker.settlement_min_window_seconds = 60
    worker.settlement_max_pages = 2
    worker._enrich_event = lambda _backend, event: event

    complete = asyncio.run(
        worker.settle_and_advance(
            BACKEND,
            datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 13, 2, 1, tzinfo=timezone.utc),
        )
    )

    assert complete is False
    assert advances == []
    assert errors and errors[-1]["status"] == "incomplete"


def test_settlement_target_stops_at_the_last_closed_minute() -> None:
    worker = UsageRealtimeWorker.__new__(UsageRealtimeWorker)
    worker.settlement_delay_seconds = 180

    target = worker.settlement_target(
        datetime(2026, 8, 13, 2, 5, 42, tzinfo=timezone.utc)
    )

    assert target == datetime(2026, 8, 13, 2, 2, tzinfo=timezone.utc)


def test_dense_settlement_window_is_split_without_advancing_the_watermark() -> None:
    calls = []

    class Client:
        async def settled_events_from_logs(self, start, end, *_args, **_kwargs):
            calls.append((start, end))
            if (end - start).total_seconds() > 30:
                return [], False
            return [{"requestId": f"{start.timestamp()}"}], True

    class Store:
        async def archive_realtime_events(self, _events):
            return 1

    worker = UsageRealtimeWorker.__new__(UsageRealtimeWorker)
    worker.client = Client()
    worker.store = Store()
    worker.settlement_min_window_seconds = 15
    worker.settlement_max_pages = 2
    worker._enrich_event = lambda _backend, event: event

    complete = asyncio.run(
        worker.settle_window(
            BACKEND,
            datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 13, 2, 1, tzinfo=timezone.utc),
        )
    )

    assert complete is True
    assert calls[0] == (
        datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 13, 2, 1, tzinfo=timezone.utc),
    )
    assert len(calls) == 3


def test_closed_settlement_advances_only_after_a_complete_window() -> None:
    advances = []

    class Client:
        async def settled_events_from_logs(self, *_args, **_kwargs):
            return [{"requestId": "req-1"}], True

    class Store:
        async def archive_realtime_events(self, _events):
            return 1

        async def advance_realtime_settlement(self, backend_id, end_time, **_kwargs):
            advances.append((backend_id, end_time))

    class Realtime:
        async def ingest_event(self, *_args, **_kwargs):
            return True, 1

    worker = UsageRealtimeWorker.__new__(UsageRealtimeWorker)
    worker.client = Client()
    worker.store = Store()
    worker.realtime = Realtime()
    worker.settlement_min_window_seconds = 5
    worker.settlement_max_pages = 2
    worker._enrich_event = lambda _backend, event: event

    end = datetime(2026, 8, 13, 2, 1, tzinfo=timezone.utc)
    assert asyncio.run(worker.settle_and_advance(BACKEND, end - timedelta(minutes=1), end)) is True
    assert advances == [("primary", end)]


def test_realtime_status_exposes_unsettled_backend_state() -> None:
    values = {
        "usage:realtime:settlement-status": {
            "primary": '{"status":"verifying","error":"page drift"}',
        },
        "usage:realtime:settlements": {},
    }

    class Client:
        async def ping(self):
            return True

        async def get(self, _key):
            return None

        async def xpending(self, *_args):
            return {"pending": 0}

        async def hgetall(self, key):
            return values.get(key, {})

    store = UsageRealtimeStore.__new__(UsageRealtimeStore)
    store.client = Client()
    store.prefix = "usage:realtime"
    store.stream_key = "usage:realtime:archive"
    store.consumer_group = "workers"

    status = asyncio.run(store.status())
    assert status["settlementStatuses"]["primary"]["status"] == "verifying"


def test_publish_mirror_uses_the_durable_request_audit_rows() -> None:
    published = []

    class Store:
        async def realtime_event_rows(self, day):
            assert day == date(2026, 8, 13)
            return [{"backendId": "primary", "date": day.isoformat(), "spend": 9.5}]

        async def replace_realtime_aggregates(self, day, rows):
            published.append((day, rows))

        async def publish_realtime_state(self, *_args, **_kwargs):
            return None

    class Realtime:
        async def status(self):
            return {"revision": 1, "latestEventAt": None}

    worker = UsageRealtimeWorker.__new__(UsageRealtimeWorker)
    worker.store = Store()
    worker.realtime = Realtime()
    worker.current_day = date(2026, 8, 13)

    asyncio.run(worker.publish_mirror())

    assert published == [(date(2026, 8, 13), [{"backendId": "primary", "date": "2026-08-13", "spend": 9.5}])]


def test_recovery_discards_legacy_page_backfill_checkpoints() -> None:
    cleared = []

    class Realtime:
        async def set_ready(self, *_args): return None
        async def clear_day(self, *_args): return None
        async def seed_aggregate(self, *_args): return None
        async def seed_request_ids(self, *_args): return None
        async def clear_backfill_checkpoint(self, backend_id): cleared.append(backend_id)
        async def set_cursor(self, *_args): return None
        async def revision(self): return 1
        async def status(self): return {"revision": 1, "latestEventAt": None}
        async def set_verified_through(self, *_args): return None

    class Store:
        async def update_worker_state(self, **_kwargs): return None
        async def realtime_recovery_rows(self, *_args): return []
        async def realtime_request_ids(self, *_args): return []
        async def latest_archived_event_at(self, *_args): return None
        async def realtime_event_rows(self, *_args): return []
        async def replace_realtime_aggregates(self, *_args): return None
        async def publish_realtime_state(self, *_args, **_kwargs): return None
        async def publish_realtime_coverage(self, *_args): return None

    worker = UsageRealtimeWorker.__new__(UsageRealtimeWorker)
    worker.realtime = Realtime(); worker.store = Store(); worker.client = type("Client", (), {"backends": [BACKEND]})()
    worker.worker_id = "test"; worker.overlap_seconds = 60; worker.live_window_seconds = 60; worker.current_day = date(2026, 8, 13)
    worker.resettle_today = lambda *_args: asyncio.sleep(0)
    worker.settle_pending_windows = lambda *_args: asyncio.sleep(0)
    worker.flush_archive = lambda: asyncio.sleep(0)
    worker.publish_mirror = lambda **_kwargs: asyncio.sleep(0)

    asyncio.run(worker.recover())

    assert cleared == ["primary"]


def test_publish_snapshot_date_parameters_are_date_objects() -> None:
    captured = []

    class Connection:
        async def execute(self, query, *args):
            if "DELETE FROM usage_daily" in query:
                captured.append(args[1:3])
            return "DELETE 0"

        async def executemany(self, *_args):
            return None

        def transaction(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return None

    class Pool:
        def acquire(self):
            return Acquire()

    store = UsageStore("postgresql://unused")
    store.pool = Pool()
    asyncio.run(
        store.replace_backend_snapshot(
            "primary", "2026-08-12", "2026-08-13", [], []
        )
    )

    assert captured == [(date(2026, 8, 12), date(2026, 8, 13))]


def test_usage_query_view_replaces_ready_day_instead_of_adding_it() -> None:
    from backend.usage_store import USAGE_SCHEMA

    assert "CREATE OR REPLACE VIEW usage_query_daily" in USAGE_SCHEMA
    assert "WHERE NOT EXISTS" in USAGE_SCHEMA
    assert "JOIN usage_realtime_state s ON s.usage_date=r.usage_date AND s.ready" in USAGE_SCHEMA


def test_realtime_configuration_enables_postgres_history_store(monkeypatch) -> None:
    monkeypatch.setenv("USAGE_DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("USAGE_SYNC_ENABLED", "false")
    monkeypatch.setenv("USAGE_REALTIME_ENABLED", "true")

    assert UsageStore.from_environment() is not None


def test_realtime_backfill_checkpoint_round_trips_through_redis_hash() -> None:
    values = {}

    class Client:
        async def hset(self, key, field, value):
            values[(key, field)] = value

        async def hget(self, key, field):
            return values.get((key, field))

        async def hdel(self, key, field):
            values.pop((key, field), None)

    store = UsageRealtimeStore.__new__(UsageRealtimeStore)
    store.client = Client()
    start_time = datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc)
    end_time = datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc)

    asyncio.run(
        store.set_backfill_checkpoint(
            "primary", start_time=start_time, end_time=end_time, next_page=11
        )
    )
    checkpoint = asyncio.run(store.backfill_checkpoint("primary"))

    assert checkpoint == {
        "startTime": start_time,
        "endTime": end_time,
        "nextPage": 11,
    }
    asyncio.run(store.clear_backfill_checkpoint("primary"))
    assert asyncio.run(store.backfill_checkpoint("primary")) is None


def test_archive_reader_claims_stale_pending_messages_before_new_ones() -> None:
    class Client:
        async def xautoclaim(self, *_args, **_kwargs):
            return (
                "0-0",
                [("1-0", {"event": json.dumps({"requestId": "req-1"})})],
                [],
            )

        async def xreadgroup(self, *_args, **_kwargs):
            raise AssertionError("new messages must wait until pending messages are replayed")

    store = UsageRealtimeStore.__new__(UsageRealtimeStore)
    store.client = Client()
    store.consumer_name = "test-consumer"

    messages = asyncio.run(store.read_archive_batch())

    assert messages == [("1-0", {"requestId": "req-1"})]


def test_incomplete_realtime_window_does_not_advance_cursor() -> None:
    class Realtime:
        cursor_value = datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc)

        async def cursor(self, _backend_id):
            return self.cursor_value

        async def set_cursor(self, *_args):
            raise AssertionError("an incomplete window must not advance the cursor")

    class Client:
        async def incremental_events_from_logs(self, *_args, **_kwargs):
            return [], False

    worker = UsageRealtimeWorker.__new__(UsageRealtimeWorker)
    worker.realtime = Realtime()
    worker.client = Client()
    worker.overlap_seconds = 60

    inserted = asyncio.run(
        worker.poll_backend(
            BACKEND, datetime(2026, 8, 13, 2, 1, tzinfo=timezone.utc)
        )
    )

    assert inserted == 0


def test_stale_cursor_uses_recent_live_window_and_preserves_history_start(monkeypatch) -> None:
    monkeypatch.setenv("USAGE_REALTIME_MAX_CURSOR_AGE_SECONDS", "900")
    monkeypatch.setenv("USAGE_REALTIME_LIVE_WINDOW_SECONDS", "900")
    worker = UsageRealtimeWorker.__new__(UsageRealtimeWorker)
    worker.overlap_seconds = 60
    worker.max_cursor_age_seconds = 900
    worker.live_window_seconds = 900

    now = datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc)
    old = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    start, history = worker.realtime_poll_window(old, now)

    assert start == datetime(2026, 8, 25, 7, 45, tzinfo=timezone.utc)
    assert history == old
    assert (now - start).total_seconds() <= 900


def test_history_backfill_window_is_bounded() -> None:
    worker = UsageRealtimeWorker.__new__(UsageRealtimeWorker)
    worker.history_window_seconds = 3600
    start = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc)

    bounded = worker.history_backfill_window(start, end)

    assert bounded == (start, start + timedelta(hours=1))


def test_background_queue_runs_when_settlement_times_out() -> None:
    calls = []

    async def slow_settlement(_now):
        await asyncio.sleep(0.05)

    async def consume_queue():
        calls.append("queue")
        return True

    worker = UsageRealtimeWorker.__new__(UsageRealtimeWorker)
    worker.background_budget_seconds = 0.01
    worker.settle_pending_windows = slow_settlement
    worker.consume_refresh_requests = consume_queue
    worker.backfill_cost_aggregates = lambda: asyncio.sleep(0)
    worker.backfill_once = lambda: asyncio.sleep(0)

    asyncio.run(worker.run_background_once(datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc)))

    assert calls == ["queue"]


def test_realtime_backfill_persists_next_page_after_each_bounded_batch() -> None:
    calls = []

    class Realtime:
        async def backfill_checkpoint(self, _backend_id):
            return {
                "startTime": datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc),
                "endTime": datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc),
                "nextPage": 11,
            }

        async def ingest_event(self, *_args):
            return True, 1

        async def set_backfill_checkpoint(self, backend_id, **checkpoint):
            calls.append((backend_id, checkpoint))

        async def clear_backfill_checkpoint(self, *_args):
            raise AssertionError("an incomplete batch must retain its checkpoint")

    class Client:
        async def incremental_events_from_logs(self, *args, **kwargs):
            calls.append((args, kwargs))
            return [{"requestId": "req-1"}], False

    worker = UsageRealtimeWorker.__new__(UsageRealtimeWorker)
    worker.realtime = Realtime()
    worker.client = Client()
    worker.backfill_pages_per_cycle = 10
    worker.live_window_seconds = 60
    worker._enrich_event = lambda _backend, event: event

    inserted = asyncio.run(worker.backfill_backend(BACKEND))

    assert inserted == 1
    request_kwargs = calls[0][1]
    assert request_kwargs["start_page"] == 11
    assert request_kwargs["max_pages"] == 10
    assert calls[1][1]["next_page"] == 21


def test_realtime_backfill_clears_checkpoint_after_final_page() -> None:
    cleared = []

    class Realtime:
        async def backfill_checkpoint(self, _backend_id):
            return {
                "startTime": datetime(2026, 8, 13, 1, 0, tzinfo=timezone.utc),
                "endTime": datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc),
                "nextPage": 21,
            }

        async def ingest_event(self, *_args):
            return False, 1

        async def cursor(self, _backend_id):
            return datetime(2026, 8, 13, 2, 0, 30, tzinfo=timezone.utc)

        async def clear_backfill_checkpoint(self, backend_id):
            cleared.append(backend_id)

    class Client:
        async def incremental_events_from_logs(self, *_args, **_kwargs):
            return [], True

    worker = UsageRealtimeWorker.__new__(UsageRealtimeWorker)
    worker.realtime = Realtime()
    worker.client = Client()
    worker.backfill_pages_per_cycle = 10
    worker.live_window_seconds = 60
    worker._enrich_event = lambda _backend, event: event

    assert asyncio.run(worker.backfill_backend(BACKEND)) == 0
    assert cleared == ["primary"]


def test_realtime_directory_refresh_updates_department_directory() -> None:
    calls = []

    class Synchronizer:
        organization_repository = None

        async def _identity_directory(self):
            return {"byUserId": {}}

        async def _token_attribution_map(self, backend_id):
            return {("backend", backend_id): []}

        async def sync_department_directories(self):
            calls.append("departments")

    worker = UsageRealtimeWorker.__new__(UsageRealtimeWorker)
    worker.client = type("Client", (), {"backends": [BACKEND]})()
    worker.synchronizer = Synchronizer()
    worker.directory = {}
    worker.token_maps = {}

    asyncio.run(worker._refresh_directories())

    assert calls == ["departments"]


def test_realtime_directory_refresh_persists_identity_directory() -> None:
    calls = []

    class Store:
        async def upsert_identity_directory(self, backend_id, identities):
            calls.append(("upsert", backend_id, identities))

        async def refresh_usage_identity_columns(self, backend_ids):
            calls.append(("refresh", backend_ids))

    class Synchronizer:
        organization_repository = None

        async def _identity_directory(self):
            return {"byUserId": {}}

        async def _token_attribution_map(self, backend_id):
            return {}

        async def sync_department_directories(self):
            pass

    class Client:
        backends = [BACKEND]

        async def users(self, backend):
            return [{"user_id": "cursor-zhangsan", "user_alias": "张三", "user_email": "zhangsan@example.com"}]

    worker = UsageRealtimeWorker.__new__(UsageRealtimeWorker)
    worker.client = Client()
    worker.store = Store()
    worker.synchronizer = Synchronizer()
    worker.directory = {}
    worker.token_maps = {}

    asyncio.run(worker._refresh_directories())

    assert calls[0][0:2] == ("upsert", "primary")
    assert calls[0][2][0]["name"] == "张三"
    assert calls[0][2][0]["email"] == "zhangsan@example.com"
    assert calls[1] == ("refresh", ["primary"])


def test_worker_lock_scripts_are_atomic_and_owner_scoped() -> None:
    calls = []

    class Script:
        def __init__(self, name):
            self.name = name

        async def __call__(self, *, keys, args):
            calls.append((self.name, keys, args))
            return 1

    class Client:
        def register_script(self, source):
            name = "renew" if "EXPIRE" in source else "release"
            return Script(name)

    store = UsageRealtimeStore.__new__(UsageRealtimeStore)
    store.client = Client()
    store.lock_ttl_seconds = 300
    store._lock_renew_script = None
    store._lock_release_script = None

    assert asyncio.run(store.renew_worker_lock("worker-a", 90)) is True
    asyncio.run(store.release_worker_lock("worker-a"))

    assert calls == [
        ("renew", ["usage:realtime:worker-lock"], ["worker-a", "90"]),
        ("release", ["usage:realtime:worker-lock"], ["worker-a"]),
    ]


def test_worker_ids_are_unique_across_restarts() -> None:
    first = new_worker_id()
    second = new_worker_id()

    assert first != second
    assert first.count(":") == 2
    assert len(first.rsplit(":", 1)[-1]) == 12


def test_worker_lock_acquisition_uses_nx_and_ttl() -> None:
    calls = []

    class Client:
        async def set(self, *args, **kwargs):
            calls.append((args, kwargs))
            return True

    store = UsageRealtimeStore.__new__(UsageRealtimeStore)
    store.client = Client()
    store.lock_ttl_seconds = 300

    assert asyncio.run(store.acquire_worker_lock("worker-new", 90)) is True
    assert calls == [(("usage:realtime:worker-lock", "worker-new"), {"nx": True, "ex": 90})]


def test_live_cycle_publishes_even_when_background_work_fails() -> None:
    calls = []

    class Store:
        async def publish_realtime_coverage(self, *_args):
            calls.append("coverage")

    worker = UsageRealtimeWorker.__new__(UsageRealtimeWorker)
    worker.store = Store()
    worker.client = type("Client", (), {"backends": []})()
    worker.poll_once = lambda *_args: asyncio.sleep(0)
    worker.flush_archive = lambda: asyncio.sleep(0)
    worker.publish_mirror = lambda: asyncio.sleep(0)
    worker.current_day = date(2026, 8, 13)

    asyncio.run(worker.run_live_once(datetime(2026, 8, 13, tzinfo=timezone.utc)))

    assert calls == ["coverage"]


def test_realtime_worker_renews_lock_independently_of_work_loop(monkeypatch) -> None:
    calls = []

    class Realtime:
        lock_ttl_seconds = 60

        async def renew_worker_lock(self, worker_id, ttl_seconds):
            calls.append((worker_id, ttl_seconds))
            return len(calls) < 2

    worker = UsageRealtimeWorker.__new__(UsageRealtimeWorker)
    worker.realtime = Realtime()
    worker.worker_id = "worker-a"
    worker.stop_event = asyncio.Event()
    worker._lock_lost = asyncio.Event()
    worker.lock_ttl_seconds = 60
    worker.lock_renew_seconds = 0.01

    async def run_loop():
        task = asyncio.create_task(worker._renew_worker_lock_loop())
        await asyncio.sleep(0.04)
        await task

    asyncio.run(run_loop())

    assert calls[0] == ("worker-a", 60)
    assert worker._lock_lost.is_set()
    assert worker.stop_event.is_set()


def test_realtime_worker_consumes_reader_refresh_queue() -> None:
    class Store:
        async def claim_refresh_requests(self, **_kwargs):
            return [{"requestKey": "r1", "startDate": "2026-08-10", "endDate": "2026-08-14"}]

        async def finish_refresh_requests(self, keys, *, success, error=""):
            self.finished = (keys, success, error)

    class Synchronizer:
        async def sync(self, start_date, end_date):
            self.range = (start_date, end_date)
            return {"status": "ok"}

    worker = UsageRealtimeWorker.__new__(UsageRealtimeWorker)
    worker.store = Store()
    worker.synchronizer = Synchronizer()

    assert asyncio.run(worker.consume_refresh_requests()) is True
    assert worker.synchronizer.range == ("2026-08-10", "2026-08-14")
    assert worker.store.finished == (["r1"], True, "")


def test_realtime_worker_releases_claim_when_refresh_is_cancelled() -> None:
    class Store:
        async def claim_refresh_requests(self, **_kwargs):
            return [{"requestKey": "r1", "startDate": "2026-08-10", "endDate": "2026-08-14"}]

        async def finish_refresh_requests(self, keys, *, success, error=""):
            self.finished = (keys, success, error)

    class Synchronizer:
        async def sync(self, _start_date, _end_date):
            raise asyncio.CancelledError()

    worker = UsageRealtimeWorker.__new__(UsageRealtimeWorker)
    worker.store = Store()
    worker.synchronizer = Synchronizer()

    async def run():
        try:
            await worker.consume_refresh_requests()
        except asyncio.CancelledError:
            return
        raise AssertionError("cancelled refresh should propagate")

    asyncio.run(run())
    assert worker.store.finished == (["r1"], False, "CancelledError")


def test_realtime_worker_waits_for_existing_lock(monkeypatch) -> None:
    attempts = 0

    class Store:
        async def connect(self):
            return None

    class Realtime:
        async def connect(self):
            return None

        async def acquire_worker_lock(self, _worker_id):
            nonlocal attempts
            attempts += 1
            return attempts >= 2

        async def release_worker_lock(self, _worker_id):
            return None

    worker = UsageRealtimeWorker.__new__(UsageRealtimeWorker)
    worker.store = Store()
    worker.realtime = Realtime()
    worker.synchronizer = type("Sync", (), {"organization_repository": None})()
    worker.worker_id = "worker-new"
    worker.poll_seconds = 2
    worker.stop_event = asyncio.Event()
    worker._lock_lost = asyncio.Event()
    worker._lock_renew_task = None

    async def no_repository():
        return None

    async def renew_loop():
        await worker.stop_event.wait()

    async def recover():
        worker.stop_event.set()

    monkeypatch.setattr(worker, "_connect_repository", no_repository)
    monkeypatch.setattr(worker, "_renew_worker_lock_loop", renew_loop)
    monkeypatch.setattr(worker, "recover", recover)
    monkeypatch.setenv("USAGE_REALTIME_LOCK_RETRY_SECONDS", "0")

    asyncio.run(worker.run())

    assert attempts == 2
