"""Recovery behavior for the real organization capability probe."""

import asyncio
from datetime import datetime, timedelta, timezone

from backend import main


class _Store:
    async def connect(self):
        return None


class _Client:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = 0

    async def organization_capabilities(self):
        self.calls += 1
        result = next(self.results)
        if isinstance(result, BaseException):
            raise result
        return result


def _reset_status(monkeypatch):
    monkeypatch.setattr(
        main,
        "_organization_capability_status",
        {
            "mode": "real",
            "status": "starting",
            "available": False,
            "lastCheckedAt": None,
        },
    )
    monkeypatch.setattr(main, "_organization_store", _Store())
    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(main, "organization_mode", lambda: "real")
    monkeypatch.setattr(main, "organization_store", lambda: main._organization_store)
    monkeypatch.setenv("ORGANIZATION_INVITATION_SECRET", "test-secret")
    monkeypatch.setenv("ORGANIZATION_CAPABILITY_RECHECK_SECONDS", "60")


def test_capability_probe_failure_is_recorded_and_recovers(monkeypatch):
    _reset_status(monkeypatch)
    upstream = _Client(
        [
            RuntimeError("upstream unavailable"),
            {"available": True, "organizations": True, "teams": True, "keys": True},
        ]
    )
    started = []
    monkeypatch.setattr(main, "client", lambda: upstream)
    monkeypatch.setattr(main, "organization_store", lambda: main._organization_store)

    async def fake_start_worker():
        started.append(True)

    monkeypatch.setattr(main, "start_organization_outbox_worker", fake_start_worker)

    first = asyncio.run(main.refresh_organization_capabilities(force=True))
    assert first["available"] is False
    assert first["status"] == "error"
    assert upstream.calls == 1

    # The interval guard prevents health checks from creating a request storm.
    skipped = asyncio.run(main.refresh_organization_capabilities())
    assert skipped["available"] is False
    assert upstream.calls == 1

    monkeypatch.setitem(
        main._organization_capability_status,
        "lastCheckedAt",
        (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat(),
    )
    recovered = asyncio.run(main.refresh_organization_capabilities())
    assert recovered["available"] is True
    assert recovered["status"] == "ready"
    assert upstream.calls == 2
    assert started == [True]


def test_real_startup_is_non_blocking_and_worker_owns_first_probe(monkeypatch):
    _reset_status(monkeypatch)
    upstream = _Client([RuntimeError("must not run during startup")])
    started = []
    monkeypatch.setattr(main, "client", lambda: upstream)

    async def fake_start_worker():
        started.append(True)

    monkeypatch.setattr(main, "start_organization_outbox_worker", fake_start_worker)

    asyncio.run(main.start_organization_service())

    assert upstream.calls == 0
    assert started == [True]
    assert main._organization_capability_status["status"] == "starting"


def test_lifespan_registers_organization_service_before_usage_sync(monkeypatch):
    events = []

    monkeypatch.setattr(main, "validate_runtime_auth_config", lambda: events.append("validate"))

    async def fake_start_billing_store():
        events.append("billing")

    async def fake_start_organization_service():
        events.append("organization")

    async def fake_start_usage_sync():
        events.append("usage")

    async def fake_close():
        events.append("close")

    monkeypatch.setattr(main, "start_billing_store", fake_start_billing_store)
    monkeypatch.setattr(main, "start_organization_service", fake_start_organization_service)
    monkeypatch.setattr(main, "start_usage_sync", fake_start_usage_sync)
    monkeypatch.setattr(main, "close_litellm_client", fake_close)

    async def exercise_lifespan():
        async with main.app_lifespan(main.app):
            events.append("serving")

    asyncio.run(exercise_lifespan())

    assert events == ["validate", "billing", "organization", "usage", "serving", "close"]


def test_usage_sync_waits_for_repository_connect_and_probe(monkeypatch):
    events = []

    class UsageStore:
        async def connect(self):
            events.append("usage-connect")

    class OrganizationStore:
        async def connect(self):
            events.append("organization-connect")

    class Synchronizer:
        def __init__(self, _client, _usage_store, repository):
            assert repository is organization_store

        @staticmethod
        def date_range(_days):
            return "2026-07-30", "2026-07-31"

        async def sync(self, _start_date, _end_date):
            events.append("sync")
            return {"status": "ok", "rowCount": 0, "backendCount": 0, "errors": []}

    usage_store = UsageStore()
    organization_store = OrganizationStore()

    async def fake_probe():
        events.append("probe")
        return {"available": False, "status": "error"}

    monkeypatch.setattr(main, "usage_store", lambda: usage_store)
    monkeypatch.setattr(main, "organization_real_enabled", lambda: True)
    monkeypatch.setattr(main, "organization_store", lambda: organization_store)
    monkeypatch.setattr(main, "refresh_organization_capabilities", fake_probe)
    monkeypatch.setattr(main, "UsageSynchronizer", Synchronizer)
    monkeypatch.setattr(main, "client", lambda: object())
    monkeypatch.setattr(main, "_usage_sync_status", {"status": "starting", "lastRun": None})

    result = asyncio.run(main.run_usage_sync(2))

    assert result["status"] == "ok"
    assert events == ["usage-connect", "organization-connect", "probe", "sync"]


def test_health_triggers_due_probe(monkeypatch):
    _reset_status(monkeypatch)
    monkeypatch.setenv("ORGANIZATION_CAPABILITY_RECHECK_SECONDS", "0")
    upstream = _Client(
        [{"available": True, "organizations": True, "teams": True, "keys": True}]
    )
    monkeypatch.setattr(main, "client", lambda: upstream)

    async def fake_start_worker():
        return None

    monkeypatch.setattr(main, "start_organization_outbox_worker", fake_start_worker)

    payload = asyncio.run(main.health())

    assert upstream.calls == 1
    assert payload["organization"]["available"] is True
    assert payload["organization"]["status"] == "ready"
