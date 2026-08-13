from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

from backend.litellm_client import LiteLLMBackend, LiteLLMClient
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
