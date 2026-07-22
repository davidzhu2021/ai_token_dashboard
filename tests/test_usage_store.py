from datetime import date, datetime, timezone
import asyncio

from backend import main
from backend.litellm_client import _date_text_in_usage_timezone, detect_source, detect_source_from_key
from backend.usage_store import UsageStore, empty_totals, summarize
from backend.usage_sync import UsageSynchronizer


def test_summarize_aggregates_daily_source_and_model_metrics() -> None:
    rows = [
        {
            "date": "2026-07-22",
            "source": "Codex",
            "model": "gpt-4o",
            "promptTokens": 10,
            "completionTokens": 5,
            "totalTokens": 15,
            "requestCount": 2,
            "successCount": 2,
            "failureCount": 0,
            "spend": 0.2,
        },
        {
            "date": "2026-07-22",
            "source": "Codex",
            "model": "gpt-4o",
            "promptTokens": 4,
            "completionTokens": 1,
            "totalTokens": 5,
            "requestCount": 1,
            "successCount": 0,
            "failureCount": 1,
            "spend": 0.1,
        },
    ]

    result = summarize(rows)

    assert result["rangeTotal"]["totalTokens"] == 20
    assert result["rangeTotal"]["requestCount"] == 3
    assert result["rangeTotal"]["failureCount"] == 1
    assert result["latestDay"]["date"] == "2026-07-22"
    assert result["sourceBreakdown"][0]["source"] == "Codex"
    assert result["modelBreakdown"][0]["model"] == "gpt-4o"


def test_usage_record_never_contains_request_details_or_api_key() -> None:
    record = UsageStore._usage_record(
        "primary",
        {
            "date": "2026-07-22",
            "_userId": "alice",
            "source": "Codex",
            "model": "gpt-4o",
            "promptTokens": 1,
            "completionTokens": 2,
            "totalTokens": 3,
            "requestCount": 1,
            "successCount": 1,
            "failureCount": 0,
            "spend": 0.01,
            "api_key": "sk-secret",
            "prompt": "private prompt",
        },
        datetime.now(timezone.utc),
    )

    assert "sk-secret" not in repr(record)
    assert "private prompt" not in repr(record)


def test_coalesce_usage_rows_prevents_duplicate_upsert_records() -> None:
    rows = [
        {"date": "2026-07-22", "_userId": "alice", "source": "其他", "model": "m", "totalTokens": 2},
        {"date": "2026-07-22", "_userId": "alice", "source": "其他", "model": "m", "totalTokens": 3},
    ]

    result = UsageStore._coalesce_usage_rows(rows)

    assert len(result) == 1
    assert result[0]["totalTokens"] == 5


def test_usage_record_normalizes_account_alias_models() -> None:
    record = UsageStore._usage_record(
        "primary",
        {
            "date": "2026-07-22",
            "_userId": "alice",
            "source": "Codex",
            "model": "chatgpt-acct-84-gpt-5.6-terra",
            "totalTokens": 3,
        },
        datetime.now(timezone.utc),
    )

    assert record[6] == "gpt-5.6-terra"


def test_usage_row_normalizes_account_alias_models_from_history() -> None:
    row = UsageStore._usage_row(
        {
            "usage_date": date(2026, 7, 22),
            "source": "Codex",
            "model": "chatgpt-acct-33-gpt-5.6-terra",
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
            "request_count": 1,
            "success_count": 1,
            "failure_count": 0,
            "spend": 0.01,
            "backend_id": "primary",
            "user_id": "alice",
            "employee_email": "alice@example.com",
            "employee_name": "Alice",
        }
    )

    assert row["model"] == "gpt-5.6-terra"


def test_merge_rows_by_sums_duplicate_normalized_models() -> None:
    rows = [
        {"_backendId": "primary", "date": "2026-07-22", "_userId": "alice", "source": "Codex", "model": "gpt-5.6-terra", "totalTokens": 2, "requestCount": 1, "spend": 0.1, "employeeName": "Alice"},
        {"_backendId": "primary", "date": "2026-07-22", "_userId": "alice", "source": "Codex", "model": "gpt-5.6-terra", "totalTokens": 3, "requestCount": 2, "spend": 0.2, "employeeName": "Alice"},
        {"_backendId": "primary", "date": "2026-07-22", "_userId": "alice", "source": "Codex", "model": "claude-opus-4-8", "totalTokens": 4, "requestCount": 1, "spend": 0.3, "employeeName": "Alice"},
    ]

    result = UsageStore._merge_rows_by(rows, ("_backendId", "date", "_userId", "source", "model"))

    by_model = {item["model"]: item for item in result}
    assert len(result) == 2
    assert by_model["gpt-5.6-terra"]["totalTokens"] == 5
    assert by_model["gpt-5.6-terra"]["requestCount"] == 3
    assert by_model["gpt-5.6-terra"]["spend"] == 0.1 + 0.2
    assert by_model["gpt-5.6-terra"]["employeeName"] == "Alice"
    assert by_model["claude-opus-4-8"]["totalTokens"] == 4


def test_usage_sync_date_range_uses_inclusive_days() -> None:
    start, end = UsageSynchronizer.date_range(3, date(2026, 7, 22))
    assert start == "2026-07-20"
    assert end == "2026-07-22"


def test_usage_store_environment_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("USAGE_SYNC_ENABLED", raising=False)
    monkeypatch.setenv("USAGE_DATABASE_URL", "postgresql://unused")
    assert UsageStore.from_environment() is None


def test_usage_store_environment_requires_both_enable_flag_and_dsn(monkeypatch) -> None:
    monkeypatch.setenv("USAGE_SYNC_ENABLED", "true")
    monkeypatch.delenv("USAGE_DATABASE_URL", raising=False)
    assert UsageStore.from_environment() is None


def test_usage_schema_is_idempotent_and_uses_aggregate_only_columns() -> None:
    from backend.usage_store import USAGE_SCHEMA

    assert USAGE_SCHEMA.count("CREATE TABLE IF NOT EXISTS usage_daily") == 1
    assert "PRIMARY KEY (backend_id, usage_date, user_id, source, model)" in USAGE_SCHEMA
    assert "api_key" not in USAGE_SCHEMA.lower()
    assert "prompt TEXT" not in USAGE_SCHEMA
    assert "response TEXT" not in USAGE_SCHEMA


def test_source_detection_falls_back_to_other_without_request_details() -> None:
    assert detect_source({"user": "cursor-alice", "metadata": {}}) == "Cursor"
    assert detect_source({"key_alias": "claude-code-alice"}) == "Claude Code"
    assert detect_source({"user": "ordinary-account"}) == "其他"
    assert detect_source_from_key({"name": "personal-cursor-key"}) == "Cursor"
    assert detect_source_from_key({"name": "unclassified"}) == "其他"


def test_usage_timezone_converts_utc_boundary_to_business_date(monkeypatch) -> None:
    monkeypatch.setenv("USAGE_TIMEZONE_OFFSET_MINUTES", "-480")
    assert _date_text_in_usage_timezone("2026-07-21T15:59:59Z") == "2026-07-21"
    assert _date_text_in_usage_timezone("2026-07-21T16:00:00Z") == "2026-07-22"


def test_usage_sync_isolates_backend_failures() -> None:
    class FakeStore:
        def __init__(self) -> None:
            self.finished = None

        async def begin_sync_run(self, *_args):
            return 1

        async def try_acquire_sync_lock(self):
            return object()

        async def release_sync_lock(self, _lock):
            return None

        async def replace_backend_snapshot(self, backend_id, *_args):
            return 2 if backend_id == "primary" else 0

        async def finish_sync_run(self, *args):
            self.finished = args

    class FakeClient:
        backends = [
            type("Backend", (), {"id": "primary"})(),
            type("Backend", (), {"id": "her"})(),
        ]

    synchronizer = UsageSynchronizer(FakeClient(), FakeStore())

    async def fake_collect(backend, *_args):
        if backend.id == "her":
            raise RuntimeError("unavailable")
        return type("Snapshot", (), {"backend_id": backend.id, "rows": [], "memberships": []})()

    synchronizer.collect_backend = fake_collect
    result = asyncio.run(synchronizer.sync("2026-07-20", "2026-07-22"))
    assert result["status"] == "partial"
    assert result["backendCount"] == 1
    assert result["errors"] == ["her: RuntimeError"]


def test_usage_sync_passes_backend_account_index_to_membership_snapshot() -> None:
    class FakeClient:
        async def users(self, _backend):
            return [{"user_id": "carher-001", "user_email": "alice@example.com", "user_alias": "Alice"}]

        def _admin_user_map(self, _users):
            return {"carher-001": {"id": "alice@example.com", "name": "Alice", "email": "alice@example.com", "userIds": ["carher-001"]}}

        def _is_backend_usage_account(self, _backend, _user_id):
            return True

        async def her_account_index(self, _backend):
            return {"profiles": {"carher-001": {"email": "alice@example.com", "name": "Alice"}}}

        async def usage_rows(self, *_args):
            return []

        async def teams(self, _backend):
            return []

    class Backend:
        id = "her"
        source = "Her"

    client = FakeClient()
    synchronizer = UsageSynchronizer(client, object())
    captured = {}

    async def capture(_backend, _users, _start, _end, account_index=None):
        captured["account_index"] = account_index
        return []

    synchronizer.collect_memberships = capture
    asyncio.run(synchronizer.collect_backend(Backend(), "2026-07-20", "2026-07-22"))
    assert captured["account_index"]["profiles"]["carher-001"]["email"] == "alice@example.com"


def test_usage_sync_lock_failure_is_recorded_and_not_released() -> None:
    class FakeStore:
        released = False
        finished = None

        async def begin_sync_run(self, *_args):
            return 9

        async def try_acquire_sync_lock(self):
            raise ConnectionError("database unavailable")

        async def release_sync_lock(self, _lock):
            self.released = True

        async def finish_sync_run(self, *args):
            self.finished = args

    synchronizer = UsageSynchronizer(type("Client", (), {"backends": []})(), FakeStore())
    try:
        asyncio.run(synchronizer.sync("2026-07-20", "2026-07-22"))
    except ConnectionError:
        pass
    else:
        raise AssertionError("expected the database lock failure to propagate")
    assert synchronizer.store.finished == (9, "failed", 0, 0, "ConnectionError")
    assert synchronizer.store.released is False


def test_health_reports_degraded_when_usage_database_is_unavailable(monkeypatch) -> None:
    class FakeStore:
        async def health(self):
            return {"enabled": True, "connected": False, "status": "error", "error": "ConnectionError"}

    monkeypatch.setattr(main, "_usage_store", FakeStore())
    monkeypatch.setattr(main, "_usage_sync_status", {"status": "error", "lastRun": "2026-07-22T00:00:00+00:00"})
    payload = asyncio.run(main.health())
    assert payload["status"] == "degraded"
    assert payload["usageDatabase"]["connected"] is False


def test_health_reports_degraded_when_one_backend_sync_fails(monkeypatch) -> None:
    class FakeStore:
        async def health(self):
            return {"enabled": True, "connected": True, "status": "ok"}

    monkeypatch.setattr(main, "_usage_store", FakeStore())
    monkeypatch.setattr(main, "_usage_sync_status", {"status": "partial", "lastRun": "2026-07-22T00:00:00+00:00"})
    payload = asyncio.run(main.health())
    assert payload["status"] == "degraded"
