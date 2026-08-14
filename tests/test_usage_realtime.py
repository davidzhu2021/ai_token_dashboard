from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone

from backend.litellm_client import LiteLLMBackend, LiteLLMClient
from backend.usage_realtime import UsageRealtimeStore
from backend.usage_realtime_worker import UsageRealtimeWorker
from backend.usage_store import UsageStore


BACKEND = LiteLLMBackend(
    id="primary", label="Tongqu API", base_url="http://upstream", admin_key="key"
)


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
