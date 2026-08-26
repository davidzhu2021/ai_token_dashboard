import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from backend import main
from backend.usage_store import UsageStore
from backend.usage_worker import UsageSyncWorker, usage_worker_from_environment


def test_reader_role_does_not_start_usage_sync(monkeypatch) -> None:
    monkeypatch.setenv("USAGE_SYNC_ROLE", "reader")
    monkeypatch.setattr(main, "_usage_sync_task", None)

    class Store:
        async def connect(self):
            return None

    monkeypatch.setattr(main, "_usage_store", Store())

    asyncio.run(main.start_usage_sync())

    assert main._usage_sync_task is None


def test_health_exposes_missing_reader_configuration(monkeypatch) -> None:
    monkeypatch.setenv("USAGE_SYNC_ROLE", "reader")
    monkeypatch.setattr(main, "local_data_mode", lambda: "real")
    monkeypatch.delenv("USAGE_DATABASE_URL", raising=False)
    monkeypatch.delenv("USAGE_SYNC_ENABLED", raising=False)
    monkeypatch.delenv("USAGE_REALTIME_ENABLED", raising=False)
    monkeypatch.setattr(main, "_usage_store", None)
    monkeypatch.setattr(main, "organization_real_enabled", lambda: False)

    payload = asyncio.run(main.health())

    assert payload["status"] == "degraded"
    assert payload["usageSync"]["status"] == "misconfigured"
    assert "USAGE_DATABASE_URL" in payload["usageReaderConfig"]["missing"]


def test_worker_consumes_reader_refresh_queue(monkeypatch) -> None:
    monkeypatch.setattr("backend.usage_worker.usage_today", lambda: date(2026, 8, 14))
    class Store:
        async def claim_refresh_requests(self, **_kwargs):
            return [{"requestKey": "r1", "startDate": "2026-08-10", "endDate": "2026-08-14"}]

        async def finish_refresh_requests(self, keys, *, success, error=""):
            self.finished = (keys, success, error)

    now = datetime(2026, 8, 14, tzinfo=timezone.utc)
    client = type("Client", (), {"backends": [type("Backend", (), {"id": "primary"})()]})()
    store = Store()
    worker = UsageSyncWorker(client, store, now=lambda: now)
    calls = []

    async def fake_sync(days, **_kwargs):
        calls.append(days)
        return {"status": "ok"}

    worker._run_sync = fake_sync
    assert asyncio.run(worker.consume_refresh_requests()) is True
    assert calls == [5]
    assert store.finished == (["r1"], True, "")


def test_worker_merges_refresh_queue_to_earliest_start_and_latest_end(monkeypatch) -> None:
    monkeypatch.setattr("backend.usage_worker.usage_today", lambda: date(2026, 8, 18))
    class Store:
        async def claim_refresh_requests(self, **_kwargs):
            return [
                {"requestKey": "r1", "startDate": "2026-08-10", "endDate": "2026-08-14", "attempts": 1},
                {"requestKey": "r2", "startDate": "2026-08-12", "endDate": "2026-08-18", "attempts": 2},
            ]

        async def finish_refresh_requests(self, keys, *, success, error="", retry_after_seconds=0):
            self.finished = (keys, success, error, retry_after_seconds)

    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    client = type("Client", (), {"backends": [type("Backend", (), {"id": "primary"})()]})()
    store = Store()
    worker = UsageSyncWorker(client, store, now=lambda: now)
    calls = []

    async def fake_sync(days, **_kwargs):
        calls.append(days)
        return {"status": "ok"}

    worker._run_sync = fake_sync
    assert asyncio.run(worker.consume_refresh_requests()) is True
    assert calls == [9]
    assert store.finished == (["r1", "r2"], True, "", 0)


def test_worker_passes_merged_historical_end_date_to_sync(monkeypatch) -> None:
    class Store:
        async def claim_refresh_requests(self, **_kwargs):
            return [{
                "requestKey": "r1",
                "startDate": "2026-08-10",
                "endDate": "2026-08-14",
                "attempts": 1,
            }]

        async def finish_refresh_requests(self, *_args, **_kwargs):
            return None

        async def update_worker_state(self, **_kwargs):
            return None

    client = type("Client", (), {"backends": []})()
    worker = UsageSyncWorker(client, Store())
    calls = []

    async def fake_sync(_client, _store, days, _repository, _factory, *, end_date=None):
        calls.append((days, end_date))
        return {"status": "ok"}

    monkeypatch.setattr("backend.usage_worker.run_sync_with_recent_refresh", fake_sync)
    assert asyncio.run(worker.consume_refresh_requests()) is True
    assert calls == [(5, "2026-08-14")]


def test_partial_refresh_stays_pending_for_retry(monkeypatch) -> None:
    class Store:
        async def claim_refresh_requests(self, **_kwargs):
            return [{"requestKey": "r1", "startDate": "2026-08-10", "endDate": "2026-08-14", "attempts": 1}]

        async def update_worker_state(self, **_kwargs):
            return None

        async def finish_refresh_requests(self, keys, **kwargs):
            self.finished = (keys, kwargs)

    worker = UsageSyncWorker(type("Client", (), {"backends": []})(), Store())

    async def partial(_days, **_kwargs):
        return {"status": "partial", "errors": ["primary: incomplete"]}

    worker._run_sync = partial
    assert asyncio.run(worker.consume_refresh_requests()) is True
    assert worker.store.finished[1]["success"] is False
    assert worker.store.finished[1]["retry_after_seconds"] > 0


def test_refresh_queue_does_not_truncate_ranges_longer_than_initial_backfill(monkeypatch) -> None:
    monkeypatch.setenv("USAGE_INITIAL_BACKFILL_DAYS", "90")

    class Store:
        async def claim_refresh_requests(self, **_kwargs):
            return [{"requestKey": "r1", "startDate": "2025-01-01", "endDate": "2026-08-14", "attempts": 1}]

        async def update_worker_state(self, **_kwargs):
            return None

        async def finish_refresh_requests(self, *_args, **_kwargs):
            return None

    worker = UsageSyncWorker(type("Client", (), {"backends": []})(), Store())
    calls = []

    async def sync(days, **kwargs):
        calls.append((days, kwargs["end_date"]))
        return {"status": "ok"}

    worker._run_sync = sync
    asyncio.run(worker.consume_refresh_requests())
    assert calls == [(591, "2026-08-14")]


def test_worker_factory_disables_realtime_for_snapshot_queue_worker(monkeypatch) -> None:
    monkeypatch.setenv("USAGE_REALTIME_ENABLED", "true")
    monkeypatch.setenv("USAGE_SYNC_ROLE", "worker")

    class Store:
        pass

    client = type("Client", (), {"backends": []})()
    worker = usage_worker_from_environment(client, Store(), realtime=None)

    assert isinstance(worker, UsageSyncWorker)


def test_snapshot_worker_retries_cancelled_refresh_with_backoff(monkeypatch) -> None:
    monkeypatch.setenv("USAGE_REFRESH_RETRY_BASE_SECONDS", "7")
    monkeypatch.setenv("USAGE_REFRESH_RETRY_MAX_SECONDS", "60")

    class Store:
        async def claim_refresh_requests(self, **kwargs):
            self.claim_kwargs = kwargs
            return [{"requestKey": "r1", "startDate": "2026-08-10", "endDate": "2026-08-14"}]

        async def finish_refresh_requests(self, keys, *, success, error="", retry_after_seconds=0):
            self.finished = (keys, success, error, retry_after_seconds)

    now = datetime(2026, 8, 14, tzinfo=timezone.utc)
    client = type("Client", (), {"backends": [type("Backend", (), {"id": "primary"})()]})()
    store = Store()
    worker = UsageSyncWorker(client, store, now=lambda: now)

    async def cancelled(_days, **_kwargs):
        raise asyncio.CancelledError()

    worker._run_sync = cancelled
    assert asyncio.run(worker.consume_refresh_requests()) is True
    assert store.finished == (["r1"], False, "CancelledError", 7)


def test_snapshot_revision_is_part_of_usage_cache_keys() -> None:
    first = main.personal_usage_cache_key(
        "user@example.com", "2026-08-01", "2026-08-05", "all", "revision-a"
    )
    second = main.personal_usage_cache_key(
        "user@example.com", "2026-08-01", "2026-08-05", "all", "revision-b"
    )

    assert first != second


def test_usage_singleflight_shares_one_cold_query() -> None:
    calls = 0

    async def run() -> tuple[dict, dict]:
        nonlocal calls

        async def factory() -> dict:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return {"rows": [1]}

        return tuple(
            await asyncio.gather(
                main.usage_singleflight("same-key", factory),
                main.usage_singleflight("same-key", factory),
            )
        )

    first, second = asyncio.run(run())

    assert calls == 1
    assert first == second == {"rows": [1]}


def test_personal_payload_uses_singleflight(monkeypatch) -> None:
    calls = 0

    async def load(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return {"rows": []}

    monkeypatch.setattr(main, "_personal_usage_payload", load)
    main._usage_singleflight.clear()

    async def run():
        user = {"id": "user-1", "email": "user@example.com"}
        return await asyncio.gather(
            main.personal_usage_payload(user, "2026-08-01", "2026-08-05", "all"),
            main.personal_usage_payload(user, "2026-08-01", "2026-08-05", "all"),
        )

    first, second = asyncio.run(run())
    assert calls == 1
    assert first == second == {"rows": []}


def test_team_member_payload_uses_singleflight(monkeypatch) -> None:
    calls = 0

    async def load(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return {"rows": []}

    monkeypatch.setattr(main, "_team_member_usage_payload", load)
    main._usage_singleflight.clear()

    async def run():
        user = {"id": "leader-1", "email": "leader@example.com"}
        return await asyncio.gather(
            main.team_member_usage_payload(
                user, "2026-08-01", "2026-08-05", "all", "employee-1", team_ref_value="team-1"
            ),
            main.team_member_usage_payload(
                user, "2026-08-01", "2026-08-05", "all", "employee-1", team_ref_value="team-1"
            ),
        )

    first, second = asyncio.run(run())
    assert calls == 1
    assert first == second == {"rows": []}


def test_personal_rows_by_user_ids_uses_snapshot_identity() -> None:
    class Pool:
        async def fetch(self, query, *args):
            if "FROM usage_sync_coverage" in query:
                return [{"backend_id": "primary"}]
            assert "user_id=ANY($3::text[])" in query
            assert args[2] == ["upstream-user"]
            return [
                {
                    "usage_date": datetime(2026, 8, 5, tzinfo=timezone.utc).date(),
                    "source": "Codex",
                    "model_name": "gpt-test",
                    "prompt_tokens": 4,
                    "completion_tokens": 6,
                    "total_tokens": 10,
                    "request_count": 1,
                    "success_count": 1,
                    "failure_count": 0,
                    "spend": 0.1,
                }
            ]

        async def fetchval(self, *_args):
            return datetime(2026, 8, 5, tzinfo=timezone.utc)

    store = UsageStore("postgresql://unused")
    store.pool = Pool()
    payload = asyncio.run(
        store.personal_rows_by_user_ids(
            ["upstream-user"], "2026-08-05", "2026-08-05", "all", ["primary"]
        )
    )

    assert payload is not None
    assert payload["rows"][0]["totalTokens"] == 10


def test_team_leader_scope_merges_same_team_across_backends() -> None:
    class Pool:
        async def fetch(self, _query, *_args):
            return [
                {
                    "backend_id": "primary",
                    "team_id": "team-a",
                    "team_name": "Engineering",
                    "member_count": 4,
                    "is_leader": True,
                },
                {
                    "backend_id": "her",
                    "team_id": "team-a",
                    "team_name": "Engineering",
                    "member_count": 3,
                    "is_leader": False,
                },
            ]

    store = UsageStore("postgresql://unused")
    store.pool = Pool()
    scope = asyncio.run(
        store.team_leader_scope("leader@example.com", [], ["primary", "her"])
    )

    assert scope["teamBoardStatus"] == "single"
    assert [
        (item["backend"], item["id"]) for item in scope["team"]["teamScopes"]
    ] == [("primary", "team-a"), ("her", "team-a")]
    # 成员数是团队全员，不是命中负责人条件的行数。
    assert scope["team"]["memberCount"] == 4


def test_team_leader_scope_ignores_admin_only_on_secondary_backend() -> None:
    class Pool:
        async def fetch(self, _query, *_args):
            return [
                {
                    "backend_id": "primary",
                    "team_id": "team-a",
                    "team_name": "Engineering",
                    "member_count": 4,
                    "is_leader": False,
                },
                {
                    "backend_id": "her",
                    "team_id": "team-a",
                    "team_name": "Engineering",
                    "member_count": 3,
                    "is_leader": True,
                },
            ]

    store = UsageStore("postgresql://unused")
    store.pool = Pool()
    scope = asyncio.run(
        store.team_leader_scope("leader@example.com", [], ["primary", "her"])
    )

    assert scope["isTeamLeader"] is False
    assert scope["leaderTeams"] == []


def test_publish_snapshots_uses_copy_before_atomic_replace() -> None:
    calls: list[tuple[str, str]] = []

    class Transaction:
        async def __aenter__(self):
            calls.append(("transaction", "begin"))

        async def __aexit__(self, *_args):
            calls.append(("transaction", "commit"))

    class Connection:
        def transaction(self):
            return Transaction()

        async def execute(self, query, *_args):
            normalized = " ".join(query.split())
            calls.append(("execute", normalized))

        async def copy_records_to_table(self, table, *, records, columns):
            assert records
            assert columns
            calls.append(("copy", table))

        async def fetchval(self, *_args):
            return "2026-08-05 08:00:00+00"

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return None

    class Pool:
        def acquire(self):
            return Acquire()

    snapshot = type(
        "Snapshot",
        (),
        {
            "backend_id": "primary",
            "rows": [
                {
                    "date": "2026-08-05",
                    "userId": "user-1",
                    "source": "Codex",
                    "model": "gpt-test",
                    "totalTokens": 10,
                }
            ],
            "memberships": [
                {
                    "snapshotDate": "2026-08-05",
                    "teamId": "team-a",
                    "teamName": "Team A",
                    "userId": "user-1",
                }
            ],
            "events": None,
            "departments": [],
        },
    )()
    store = UsageStore("postgresql://unused")
    store.pool = Pool()

    result = asyncio.run(
        store.publish_snapshots("2026-08-05", "2026-08-05", [snapshot])
    )

    first_copy = next(index for index, item in enumerate(calls) if item[0] == "copy")
    first_delete = next(
        index
        for index, item in enumerate(calls)
        if item[0] == "execute" and item[1].startswith("DELETE FROM usage_daily")
    )
    assert first_copy < first_delete
    assert result["rowCount"] == 1
    assert result["snapshotRevision"]


def test_publish_snapshots_converts_date_window_arguments_before_sql() -> None:
    captured: list[tuple[object, ...]] = []

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Connection:
        def transaction(self):
            return Transaction()

        async def execute(self, query, *args):
            if "DELETE FROM usage_daily" in query:
                captured.append(args)

        async def copy_records_to_table(self, *_args, **_kwargs):
            return None

        async def fetchval(self, *_args):
            return "2026-08-05 08:00:00+00"

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return None

    class Pool:
        def acquire(self):
            return Acquire()

    snapshot = type("Snapshot", (), {
        "backend_id": "primary",
        "rows": [{"date": "2026-08-05", "userId": "user-1", "source": "Codex", "model": "gpt-test"}],
        "memberships": [],
        "events": None,
        "departments": [],
    })()
    store = UsageStore("postgresql://unused")
    store.pool = Pool()

    asyncio.run(store.publish_snapshots("2026-08-05", "2026-08-05", [snapshot]))

    assert captured
    assert isinstance(captured[0][1], date)
    assert isinstance(captured[0][2], date)


def test_publish_snapshots_merges_stage_rows_without_duplicate_key_failures() -> None:
    """历史 report-only 行与新的完整快照键重叠时，发布必须不会整体回滚。"""

    calls: list[str] = []

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Connection:
        def transaction(self):
            return Transaction()

        async def execute(self, query, *_args):
            calls.append(" ".join(query.split()))

        async def copy_records_to_table(self, *_args, **_kwargs):
            return None

        async def fetchval(self, *_args):
            return "2026-08-20 08:00:00+00"

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return None

    class Pool:
        def acquire(self):
            return Acquire()

    snapshot = type("Snapshot", (), {
        "backend_id": "primary",
        "rows": [{"date": "2026-08-20", "userId": "user-1", "source": "Codex", "model": "gpt-test"}],
        "memberships": [],
        "events": None,
        "departments": [],
    })()
    store = UsageStore("postgresql://unused")
    store.pool = Pool()

    asyncio.run(store.publish_snapshots("2026-08-20", "2026-08-20", [snapshot]))

    stage_inserts = [call for call in calls if call.startswith("INSERT INTO usage_") and "_stage_" in call]
    assert stage_inserts
    assert all("ON CONFLICT" in call and "DO UPDATE SET" in call for call in stage_inserts)
    usage_merge = next(call for call in stage_inserts if call.startswith("INSERT INTO usage_daily"))
    assert "spend=EXCLUDED.spend" in usage_merge
    event_merge = next(call for call in stage_inserts if call.startswith("INSERT INTO usage_event_attribution"))
    assert "status=EXCLUDED.status" in event_merge


def test_snapshot_state_falls_back_to_coverage_before_first_publish() -> None:
    """升级后发布状态表为空，但历史快照仍可用，权限读取不应整体失败。"""

    class Pool:
        async def fetchrow(self, query, *_args):
            if "FROM usage_snapshot_state" in query:
                return {
                    "revision": "",
                    "published_at": None,
                    "start_date": None,
                    "end_date": None,
                    "backend_ids": None,
                }
            assert "FROM usage_sync_coverage" in query
            return {
                "revision": "2026-08-05 05:00:00+00",
                "published_at": datetime(2026, 8, 5, 5, tzinfo=timezone.utc),
                "start_date": datetime(2026, 5, 7, tzinfo=timezone.utc).date(),
                "end_date": datetime(2026, 8, 5, tzinfo=timezone.utc).date(),
                "backend_ids": ["primary", "her"],
            }

    store = UsageStore("postgresql://unused")
    store.pool = Pool()
    state = asyncio.run(store.snapshot_state())

    assert state["revision"] == "2026-08-05 05:00:00+00"
    assert state["backendIds"] == ["primary", "her"]


def test_snapshot_state_stays_empty_without_any_coverage() -> None:
    class Pool:
        async def fetchrow(self, query, *_args):
            if "FROM usage_snapshot_state" in query:
                return None
            return {
                "revision": None,
                "published_at": None,
                "start_date": None,
                "end_date": None,
                "backend_ids": None,
            }

    store = UsageStore("postgresql://unused")
    store.pool = Pool()

    assert asyncio.run(store.snapshot_state())["revision"] == ""


def test_worker_startup_uses_recent_refresh_when_snapshot_is_old(monkeypatch) -> None:
    now = datetime(2026, 8, 5, 8, tzinfo=timezone.utc)

    class Store:
        async def has_complete_coverage(self, *_args):
            return True

        async def latest_success_at(self):
            return now - timedelta(minutes=31)

    client = type("Client", (), {"backends": [type("Backend", (), {"id": "primary"})()]})()
    monkeypatch.setenv("USAGE_SYNC_STARTUP_MAX_AGE_SECONDS", "1800")
    monkeypatch.setenv("USAGE_SYNC_RECENT_DAYS", "2")
    worker = UsageSyncWorker(client, Store(), now=lambda: now)

    assert asyncio.run(worker.startup_sync_days()) == 2


def test_health_degrades_on_stale_worker_heartbeat(monkeypatch) -> None:
    now = datetime.now(timezone.utc)

    class Store:
        async def health(self):
            return {"enabled": True, "connected": True, "status": "ok"}

        async def sync_state(self):
            return {
                "status": "idle",
                "workerId": "worker-1",
                "heartbeatAt": now - timedelta(seconds=121),
                "lastSuccessAt": now - timedelta(minutes=10),
                "lastStartedAt": None,
                "lastFinishedAt": None,
                "snapshotRevision": "rev",
                "lastError": "",
            }

    monkeypatch.setattr(main, "local_data_mode", lambda: "real")
    monkeypatch.setattr(main, "_usage_store", Store())
    monkeypatch.setattr(main, "organization_real_enabled", lambda: False)
    payload = asyncio.run(main.health())

    assert payload["status"] == "degraded"
    assert payload["usageSync"]["status"] == "degraded"


def test_health_uses_snapshot_published_at_not_realtime_heartbeat(monkeypatch) -> None:
    now = datetime.now(timezone.utc)

    class Store:
        async def health(self):
            return {"enabled": True, "connected": True, "status": "ok"}

        async def sync_state(self):
            return {
                "status": "idle",
                "workerId": "realtime-worker",
                "heartbeatAt": now,
                "lastSuccessAt": now,
                "publishedAt": now - timedelta(hours=2),
                "lastStartedAt": None,
                "lastFinishedAt": None,
                "snapshotRevision": "rev",
                "lastError": "",
            }

    monkeypatch.setenv("USAGE_SYNC_ROLE", "reader")
    monkeypatch.setattr(main, "local_data_mode", lambda: "real")
    monkeypatch.setattr(main, "_usage_store", Store())
    monkeypatch.setattr(main, "organization_real_enabled", lambda: False)

    payload = asyncio.run(main.health())

    assert payload["usageSync"]["snapshotLagSeconds"] >= 7199


def test_health_does_not_treat_last_success_as_snapshot_publish(monkeypatch) -> None:
    now = datetime.now(timezone.utc)

    class Store:
        async def health(self):
            return {"enabled": True, "connected": True, "status": "ok"}

        async def sync_state(self):
            return {
                "status": "idle",
                "workerId": "realtime-worker",
                "heartbeatAt": now,
                "lastSuccessAt": now,
                "publishedAt": None,
                "lastStartedAt": None,
                "lastFinishedAt": None,
                "snapshotRevision": "",
                "lastError": "",
            }

    monkeypatch.setenv("USAGE_SYNC_ROLE", "reader")
    monkeypatch.setattr(main, "local_data_mode", lambda: "real")
    monkeypatch.setattr(main, "_usage_store", Store())
    monkeypatch.setattr(main, "organization_real_enabled", lambda: False)

    payload = asyncio.run(main.health())

    assert payload["usageSync"]["publishedAt"] is None
    assert payload["usageSync"]["snapshotLagSeconds"] is None
    assert payload["usageSync"]["status"] == "degraded"


def test_health_refresh_queue_exposes_age_and_retry_metadata(monkeypatch) -> None:
    now = datetime.now(timezone.utc)

    class Store:
        async def health(self):
            return {"enabled": True, "connected": True, "status": "ok"}

        async def sync_state(self):
            return {"status": "idle", "heartbeatAt": now, "publishedAt": now}

        async def refresh_queue_status(self):
            return {
                "pendingCount": 10,
                "runningCount": 1,
                "oldestRequestedAt": now - timedelta(hours=2),
                "maxAttempts": 691,
                "lastAttemptedAt": now - timedelta(minutes=1),
                "lastError": "CancelledError",
            }

    monkeypatch.setenv("USAGE_SYNC_ROLE", "reader")
    monkeypatch.setattr(main, "local_data_mode", lambda: "real")
    monkeypatch.setattr(main, "_usage_store", Store())
    monkeypatch.setattr(main, "organization_real_enabled", lambda: False)

    payload = asyncio.run(main.health())

    queue = payload["usageRefreshQueue"]
    assert queue["pendingCount"] == 10
    assert queue["runningCount"] == 1
    assert queue["maxAttempts"] == 691
    assert queue["lastError"] == "CancelledError"
    assert queue["oldestAgeSeconds"] >= 7199


def test_health_marks_settled_status_with_stale_watermark_as_unsettled(monkeypatch) -> None:
    now = datetime.now(timezone.utc)

    class Store:
        async def health(self):
            return {"enabled": True, "connected": True, "status": "ok"}

        async def sync_state(self):
            return {"status": "idle", "heartbeatAt": now, "publishedAt": now}

    class Realtime:
        async def connect(self):
            return None

        async def status(self):
            return {
                "ready": True,
                "latestEventLagSeconds": 1,
                "latestEventAt": now,
                "verifiedThrough": {"primary": (now - timedelta(minutes=20)).isoformat()},
                "settlementStatuses": {"primary": {"status": "settled", "error": ""}},
            }

    monkeypatch.setenv("USAGE_SYNC_ROLE", "reader")
    monkeypatch.setenv("USAGE_REALTIME_ENABLED", "true")
    monkeypatch.setattr(main, "realtime_enabled", lambda: True)
    monkeypatch.setattr(main, "usage_backend_ids", lambda: ["primary"])
    monkeypatch.setattr(main, "local_data_mode", lambda: "real")
    monkeypatch.setattr(main, "_usage_store", Store())
    monkeypatch.setattr(main, "_usage_realtime_store", Realtime())
    monkeypatch.setattr(main, "organization_real_enabled", lambda: False)

    payload = asyncio.run(main.health())

    assert payload["settlement"]["unsettledBackends"] == ["primary"]


def test_health_marks_configured_backend_without_settlement_state_as_unsettled(monkeypatch) -> None:
    now = datetime.now(timezone.utc)

    class Store:
        async def health(self):
            return {"enabled": True, "connected": True, "status": "ok"}

        async def sync_state(self):
            return {"status": "idle", "heartbeatAt": now, "publishedAt": now}

    class Realtime:
        async def connect(self):
            return None

        async def status(self):
            return {"ready": True, "latestEventLagSeconds": 1, "latestEventAt": now}

    monkeypatch.setenv("USAGE_SYNC_ROLE", "reader")
    monkeypatch.setattr(main, "realtime_enabled", lambda: True)
    monkeypatch.setattr(main, "usage_backend_ids", lambda: ["primary", "her"])
    monkeypatch.setattr(main, "local_data_mode", lambda: "real")
    monkeypatch.setattr(main, "_usage_store", Store())
    monkeypatch.setattr(main, "_usage_realtime_store", Realtime())
    monkeypatch.setattr(main, "organization_real_enabled", lambda: False)

    payload = asyncio.run(main.health())

    assert payload["settlement"]["unsettledBackends"] == ["her", "primary"]
    assert payload["settlement"]["backends"]["primary"]["status"] == "verifying"
    assert payload["settlement"]["backends"]["her"]["status"] == "verifying"


def test_team_leader_scope_reports_multiple_teams() -> None:
    class Pool:
        async def fetch(self, _query, *_args):
            return [
                {
                    "backend_id": "primary",
                    "team_id": "team-a",
                    "team_name": "Engineering",
                    "member_count": 4,
                    "is_leader": True,
                },
                {
                    "backend_id": "primary",
                    "team_id": "team-b",
                    "team_name": "Algorithms",
                    "member_count": 2,
                    "is_leader": True,
                },
            ]

    store = UsageStore("postgresql://unused")
    store.pool = Pool()
    scope = asyncio.run(store.team_leader_scope("leader@example.com", [], ["primary"]))

    assert scope["teamBoardStatus"] == "multiple"
    assert scope["team"] is None
    assert [team["id"] for team in scope["leaderTeams"]] == ["team-b", "team-a"]


def test_admin_usage_fails_closed_when_snapshot_is_missing(monkeypatch) -> None:
    class Store:
        async def connect(self):
            return None

        async def snapshot_revision(self, *_args):
            return "2026-08-05 08:00:00+00"

        async def admin_rows(self, *_args):
            return None

    async def fail_upstream(*_args, **_kwargs):
        raise AssertionError("snapshot reader must not fall back to upstream")

    class Client:
        admin_usage_rows = staticmethod(fail_upstream)

    monkeypatch.setattr(main, "usage_store", lambda: Store())
    monkeypatch.setattr(main, "usage_backend_ids", lambda: ["primary"])
    monkeypatch.setattr(main, "client", lambda: Client())
    main.admin_usage_cache.clear()

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            main.admin_usage_payload(
                {"email": "admin@example.com"}, "2026-08-01", "2026-08-05", "all", None
            )
        )

    assert error.value.status_code == 503


def test_admin_usage_still_falls_back_to_upstream_without_snapshot_database(monkeypatch) -> None:
    calls: list[str] = []

    async def upstream_rows(start_date, end_date, source, employee):
        calls.append(start_date)
        return {"rows": [], "summary": {}}

    class Client:
        admin_usage_rows = staticmethod(upstream_rows)

    monkeypatch.setattr(main, "usage_store", lambda: None)
    monkeypatch.setattr(main, "client", lambda: Client())
    main.admin_usage_cache.clear()

    payload = asyncio.run(
        main.admin_usage_payload(
            {"email": "admin@example.com"}, "2026-08-01", "2026-08-05", "all", None
        )
    )

    assert calls == ["2026-08-01"]
    assert payload["cache"]["hit"] is False


def _scheduling_worker(now: datetime) -> UsageSyncWorker:
    client = type("Client", (), {"backends": [type("Backend", (), {"id": "primary"})()]})()
    return UsageSyncWorker(client, object(), now=lambda: now)


def test_refresh_account_identity_updates_history_rows() -> None:
    """身份回填的失败会被同步的 except 吞掉，这里直接跑通整条语句路径。"""

    executed: list[tuple[str, list]] = []

    class Connection:
        async def executemany(self, query, args):
            executed.append((query, list(args)))

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
    updated = asyncio.run(
        store.refresh_account_identity(
            "primary",
            [
                {"userId": "user-1", "name": "张三", "email": "zhangsan@example.com"},
                {"userId": "", "name": "无账号"},
            ],
        )
    )

    assert updated == 1
    assert "UPDATE usage_daily" in executed[0][0]
    assert executed[0][1] == [("primary", "张三", "zhangsan@example.com", "", "user-1")]


def test_worker_resumes_cycle_from_last_success_after_restart(monkeypatch) -> None:
    """重启后跳过启动同步时，周期从上次成功时刻起算，而不是从重启时刻。"""

    monkeypatch.setenv("USAGE_SYNC_INTERVAL_SECONDS", "1800")
    monkeypatch.setenv("USAGE_SYNC_CALIBRATION_INTERVAL_SECONDS", "21600")
    started_at = datetime(2026, 8, 5, 6, 4, tzinfo=timezone.utc)
    last_success = started_at - timedelta(minutes=17)
    synced: list[int] = []

    class Store:
        async def connect(self):
            return None

        async def has_complete_coverage(self, *_args):
            return True

        async def latest_success_at(self):
            return last_success

        async def heartbeat_worker(self, *_args, **_kwargs):
            return None

    client = type("Client", (), {"backends": [type("Backend", (), {"id": "primary"})()]})()
    clock = {"now": started_at}
    worker = UsageSyncWorker(client, Store(), now=lambda: clock["now"])

    async def fake_sync(days: int) -> dict:
        synced.append(days)
        worker.stop_event.set()
        return {"status": "ok"}

    async def fake_wait(awaitable, timeout):
        awaitable.close()
        clock["now"] = clock["now"] + timedelta(seconds=timeout)
        raise asyncio.TimeoutError

    monkeypatch.setattr(worker, "_run_sync", fake_sync)
    monkeypatch.setattr(worker, "_heartbeat_loop", lambda: asyncio.sleep(0))
    monkeypatch.setattr(asyncio, "wait_for", fake_wait)

    asyncio.run(worker.run())

    # 距上次成功已过 17 分钟，只需再等 13 分钟，而不是重新等满 30 分钟。
    assert synced == [2]
    assert clock["now"] == started_at + timedelta(minutes=13)


def test_worker_schedules_recent_refresh_every_interval(monkeypatch) -> None:
    monkeypatch.setenv("USAGE_SYNC_INTERVAL_SECONDS", "1800")
    monkeypatch.setenv("USAGE_SYNC_CALIBRATION_INTERVAL_SECONDS", "21600")
    monkeypatch.setenv("USAGE_SYNC_RECENT_DAYS", "2")
    monkeypatch.setenv("USAGE_SYNC_CALIBRATION_DAYS", "3")
    start = datetime(2026, 8, 5, tzinfo=timezone.utc)
    worker = _scheduling_worker(start)

    assert worker.due_sync(start + timedelta(minutes=29), start, start) is None
    assert worker.due_sync(start + timedelta(minutes=30), start, start) == ("refresh", 2)
    assert worker.seconds_until_next_sync(start + timedelta(minutes=10), start, start) == 1200.0


def test_worker_prefers_calibration_after_six_hours(monkeypatch) -> None:
    monkeypatch.setenv("USAGE_SYNC_INTERVAL_SECONDS", "1800")
    monkeypatch.setenv("USAGE_SYNC_CALIBRATION_INTERVAL_SECONDS", "21600")
    monkeypatch.setenv("USAGE_SYNC_RECENT_DAYS", "2")
    monkeypatch.setenv("USAGE_SYNC_CALIBRATION_DAYS", "3")
    start = datetime(2026, 8, 5, tzinfo=timezone.utc)
    worker = _scheduling_worker(start)
    later = start + timedelta(hours=6)

    assert worker.due_sync(later, later - timedelta(minutes=5), start) == ("calibration", 3)


class RecordingStore:
    def __init__(self) -> None:
        self.states: list[dict] = []
        self.heartbeats: list[tuple[str, str]] = []

    async def update_worker_state(self, **kwargs):
        self.states.append(kwargs)

    async def heartbeat_worker(self, worker_id, status="idle"):
        self.heartbeats.append((worker_id, status))


def test_worker_records_failure_without_marking_success(monkeypatch) -> None:
    now = datetime(2026, 8, 5, tzinfo=timezone.utc)
    store = RecordingStore()
    client = type("Client", (), {"backends": []})()
    worker = UsageSyncWorker(client, store, now=lambda: now, worker_id="worker-1")

    async def boom(*_args, **_kwargs):
        raise RuntimeError("upstream down")

    monkeypatch.setattr("backend.usage_worker.run_sync_with_recent_refresh", boom)

    result = asyncio.run(worker._run_sync(2))

    assert result["status"] == "failed"
    assert store.states[-1]["status"] == "failed"
    assert store.states[-1].get("last_success_at") is None
    assert worker._current_status == "failed"


def test_worker_heartbeat_writes_shared_state(monkeypatch) -> None:
    monkeypatch.setenv("USAGE_SYNC_HEARTBEAT_INTERVAL_SECONDS", "5")
    store = RecordingStore()
    client = type("Client", (), {"backends": []})()
    worker = UsageSyncWorker(client, store, worker_id="worker-1")
    worker._current_status = "idle"

    async def run() -> None:
        task = asyncio.create_task(worker._heartbeat_loop())
        await asyncio.sleep(0)
        worker.stop_event.set()
        await task

    asyncio.run(run())

    assert store.heartbeats[0] == ("worker-1", "idle")
