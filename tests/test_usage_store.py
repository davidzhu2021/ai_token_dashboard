from datetime import date, datetime, timezone
import asyncio
from decimal import Decimal

import pytest

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


def test_usage_record_persists_non_secret_event_attribution() -> None:
    collected_at = datetime.now(timezone.utc)
    record = UsageStore._usage_record(
        "primary",
        {
            "date": "2026-07-22",
            "_userId": "alice",
            "organizationId": "org-upstream",
            "teamId": "team-at-request-time",
            "keyId": "hashed-key-id",
            "source": "Codex",
            "model": "gpt-5",
        },
        collected_at,
    )

    assert record[15:18] == ("org-upstream", "team-at-request-time", "hashed-key-id")
    assert record[18:] == ("", "explicit", True, "")
    assert record[6] == "gpt-5"


def test_usage_attribution_priority_is_explicit_then_token_then_user_mapping() -> None:
    collected_at = datetime.now(timezone.utc)
    explicit = UsageStore._usage_record(
        "primary",
        {
            "date": "2026-07-22",
            "organizationId": "org-explicit",
            "teamId": "team-explicit",
            "tokenOrganizationId": "org-token",
            "tokenTeamId": "team-token",
            "userOrganizationId": "org-user",
            "userTeamId": "team-user",
        },
        collected_at,
    )
    token = UsageStore._usage_record(
        "primary",
        {
            "date": "2026-07-22",
            "tokenOrganizationId": "org-token",
            "tokenTeamId": "team-token",
            "userOrganizationId": "org-user",
            "userTeamId": "team-user",
        },
        collected_at,
    )
    user = UsageStore._usage_record(
        "primary",
        {"date": "2026-07-22", "userOrganizationId": "org-user", "userTeamId": "team-user"},
        collected_at,
    )

    assert explicit[15:17] == ("org-explicit", "team-explicit")
    assert token[15:17] == ("org-token", "team-token")
    assert user[15:17] == ("org-user", "team-user")


def test_report_only_usage_is_visible_but_excluded_from_daily_settlement() -> None:
    import inspect

    record = UsageStore._usage_record(
        "primary",
        {
            "date": "2026-07-22",
            "organizationId": "org-upstream",
            "teamId": "team-upstream",
            "keyId": "a" * 64,
            "principalId": "principal-1",
            "attributionSource": "legacy_report_only",
            "billingEligible": False,
        },
        datetime.now(timezone.utc),
    )

    assert record[15:21] == (
        "org-upstream",
        "team-upstream",
        "a" * 64,
        "principal-1",
        "legacy_report_only",
        False,
    )
    source = inspect.getsource(UsageStore.organization_daily_spend)
    assert "billing_eligible = TRUE" in source


def test_event_record_persists_only_non_content_attribution_metadata() -> None:
    collected_at = datetime.now(timezone.utc)
    record = UsageStore._event_record(
        "primary",
        {
            "requestId": "request-1",
            "eventTime": "2026-07-22T08:30:00+00:00",
            "date": "2026-07-22",
            "_userId": "claude-code-lianghaiqiang",
            "organizationId": "org-upstream",
            "teamId": "team-upstream",
            "keyId": "a" * 64,
            "principalId": "principal-liang",
            "source": "Claude Code",
            "model": "claude-sonnet-4-5",
            "totalTokens": 42,
            "requestCount": 1,
            "successCount": 1,
            "spend": 0.012345,
            "attributionSource": "legacy_report_only",
            "billingEligible": False,
            "prompt": "must not be stored",
            "response": "must not be stored",
            "api_key": "sk-secret",
        },
        collected_at,
    )

    assert record is not None
    assert record[0:11] == (
        "primary",
        "request-1",
        datetime(2026, 7, 22, 8, 30, tzinfo=timezone.utc),
        date(2026, 7, 22),
        "claude-code-lianghaiqiang",
        "org-upstream",
        "team-upstream",
        "a" * 64,
        "principal-liang",
        "Claude Code",
        "claude-sonnet-4-5",
    )
    assert "must not be stored" not in repr(record)
    assert "sk-secret" not in repr(record)


def test_event_record_preserves_long_request_ids_without_truncation() -> None:
    request_id = "trace-" + ("x" * 500)
    record = UsageStore._event_record(
        "primary",
        {
            "requestId": request_id,
            "eventTime": "2026-07-22T08:30:00+00:00",
            "date": "2026-07-22",
            "_userId": "cursor-lianghaiqiang",
            "keyId": "a" * 64,
            "source": "Cursor",
            "model": "gpt-5.6-terra",
            "requestCount": 1,
            "successCount": 1,
            "spend": 0.1,
            "attributionSource": "legacy_report_only",
            "billingEligible": False,
        },
        datetime.now(timezone.utc),
    )

    assert record is not None
    assert record[1] == request_id


def test_usage_schema_has_request_level_attribution_without_content_columns() -> None:
    from backend.usage_store import USAGE_SCHEMA

    assert "CREATE TABLE IF NOT EXISTS usage_event_attribution" in USAGE_SCHEMA
    assert "event_time TIMESTAMPTZ NOT NULL" in USAGE_SCHEMA
    assert "PRIMARY KEY (backend_id, request_id)" in USAGE_SCHEMA
    event_schema = USAGE_SCHEMA.split(
        "CREATE TABLE IF NOT EXISTS usage_event_attribution", 1
    )[1]
    assert "prompt TEXT" not in event_schema
    assert "response TEXT" not in event_schema
    assert "api_key TEXT" not in event_schema


def test_snapshot_replacement_preserves_report_only_history() -> None:
    import inspect

    source = inspect.getsource(UsageStore.replace_backend_snapshot)

    assert source.count("attribution_source <> 'legacy_report_only'") == 2


def test_coalesce_keeps_distinct_event_time_team_and_key_attribution() -> None:
    rows = [
        {"date": "2026-07-22", "_userId": "alice", "source": "Codex", "model": "gpt-5", "teamId": "team-old", "keyId": "key-1", "totalTokens": 2},
        {"date": "2026-07-22", "_userId": "alice", "source": "Codex", "model": "gpt-5", "teamId": "team-new", "keyId": "key-2", "totalTokens": 3},
    ]

    result = UsageStore._coalesce_usage_rows(rows)

    assert len(result) == 2
    assert {(row["teamId"], row["keyId"], row["totalTokens"]) for row in result} == {
        ("team-old", "key-1", 2),
        ("team-new", "key-2", 3),
    }


def test_organization_query_groups_by_event_time_team() -> None:
    import inspect

    source = inspect.getsource(UsageStore.organization_rows)

    assert "source, team_id," in source
    assert "GROUP BY backend_id, usage_date, user_id, principal_id, source, team_id" in source
    assert '"departmentId": team_id' in source
    assert '"departments": sorted(' in source


def test_organization_query_groups_multiple_upstream_users_by_principal() -> None:
    import inspect

    source = inspect.getsource(UsageStore.organization_rows)
    summaries = inspect.getsource(UsageStore._employee_summaries)

    assert "SELECT backend_id, usage_date, user_id, principal_id" in source
    assert '"employeeId": principal_id or record["user_id"]' in source
    assert "principal_id" in summaries


def test_identity_rows_preserve_original_upstream_user_ids() -> None:
    class Pool:
        async def fetch(self, query, *args):
            if "FROM usage_sync_coverage" in query:
                return [{"backend_id": "primary"}]
            # 一条 SQL 完成联合：同一行既可能命中 user_id 又可能命中 principal_id，
            # 分两次查再相加会重复计费。
            assert "user_id=ANY($2::text[]) OR principal_id=ANY($3::text[])" in query
            assert "SUM(total_tokens) AS total_tokens" in query
            assert args[0:3] == ("org-upstream", ["customer-member-1"], ["principal-1"])
            return [
                {
                    "usage_date": date(2026, 7, 30),
                    "source": "Codex",
                    "model": "gpt-5.6-sol",
                    "prompt_tokens": 1,
                    "completion_tokens": 2,
                    "total_tokens": 3,
                    "request_count": 2,
                    "success_count": 2,
                    "failure_count": 0,
                    "spend": 1.25,
                    "matched_user_ids": ["claude-code-lianghaiqiang", "cursor-lianghaiqiang"],
                },
                {
                    "usage_date": date(2026, 7, 30),
                    "source": "Codex",
                    "model": "openai/chatgpt-gpt-5.6-sol",
                    "prompt_tokens": 4,
                    "completion_tokens": 5,
                    "total_tokens": 9,
                    "request_count": 3,
                    "success_count": 2,
                    "failure_count": 1,
                    "spend": 2.75,
                    "matched_user_ids": ["cursor-lianghaiqiang", "codex-lianghaiqiang"],
                },
            ]

        async def fetchval(self, query, *args):
            if "MAX(synced_at)" in query:
                return datetime(2026, 7, 30, tzinfo=timezone.utc)
            raise AssertionError(query)

    store = UsageStore("postgresql://unused")
    store.pool = Pool()

    result = asyncio.run(
        store.organization_identity_rows(
            "org-upstream",
            ["customer-member-1"],
            ["principal-1"],
            "2026-07-30",
            "2026-07-30",
            "all",
            ["primary"],
        )
    )

    assert result["principalIds"] == ["principal-1"]
    assert result["upstreamUserIds"] == [
        "claude-code-lianghaiqiang",
        "codex-lianghaiqiang",
        "cursor-lianghaiqiang",
    ]
    assert len(result["rows"]) == 1
    assert result["rows"][0]["model"] == "gpt-5.6-sol"
    assert result["rows"][0]["totalTokens"] == 12
    assert result["rows"][0]["requestCount"] == 5
    assert result["rows"][0]["successCount"] == 4
    assert result["rows"][0]["failureCount"] == 1
    assert result["rows"][0]["spend"] == pytest.approx(4.0)


def test_identity_rows_query_once_so_dual_matches_are_not_doubled() -> None:
    """同一行同时命中两种身份时只能计一次。"""

    calls: list[str] = []

    class Pool:
        async def fetch(self, query, *args):
            if "FROM usage_sync_coverage" in query:
                return [{"backend_id": "primary"}]
            calls.append(query)
            return [
                {
                    "usage_date": date(2026, 8, 3),
                    "source": "Claude Code",
                    "model": "claude-opus-5",
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                    "request_count": 4,
                    "success_count": 4,
                    "failure_count": 0,
                    "spend": 12.5,
                    # 该行的 user_id 与 principal_id 都属于这位成员。
                    "matched_user_ids": ["customer-member-1"],
                }
            ]

        async def fetchval(self, query, *args):
            return None

    store = UsageStore("postgresql://unused")
    store.pool = Pool()

    result = asyncio.run(
        store.organization_identity_rows(
            "org-upstream",
            ["customer-member-1"],
            ["principal-1"],
            "2026-08-03",
            "2026-08-03",
            "all",
            ["primary"],
        )
    )

    assert len(calls) == 1
    assert len(result["rows"]) == 1
    assert result["rows"][0]["totalTokens"] == 30
    assert result["rows"][0]["spend"] == pytest.approx(12.5)
    assert result["upstreamUserIds"] == ["customer-member-1"]


def test_identity_rows_accept_a_member_without_any_principal() -> None:
    class Pool:
        async def fetch(self, query, *args):
            if "FROM usage_sync_coverage" in query:
                return [{"backend_id": "primary"}]
            assert args[1] == ["customer-member-1"]
            assert args[2] == []
            return []

        async def fetchval(self, query, *args):
            return None

    store = UsageStore("postgresql://unused")
    store.pool = Pool()

    result = asyncio.run(
        store.organization_identity_rows(
            "org-upstream",
            ["customer-member-1", " ", "customer-member-1"],
            [],
            "2026-08-03",
            "2026-08-03",
            "all",
            ["primary"],
        )
    )

    assert result["rows"] == []
    assert result["principalIds"] == []
    assert result["upstreamUserIds"] == []


def test_identity_rows_return_none_when_a_backend_is_not_synced() -> None:
    class Pool:
        async def fetch(self, query, *args):
            if "FROM usage_sync_coverage" in query:
                return [{"backend_id": "primary"}]
            raise AssertionError("must not query usage before coverage is complete")

    store = UsageStore("postgresql://unused")
    store.pool = Pool()

    result = asyncio.run(
        store.organization_identity_rows(
            "org-upstream",
            ["customer-member-1"],
            ["principal-1"],
            "2026-08-03",
            "2026-08-03",
            "all",
            ["primary", "secondary"],
        )
    )

    assert result is None


def test_organization_rows_preserve_department_before_canonical_merge() -> None:
    class Pool:
        async def fetch(self, query, *args):
            if "FROM usage_sync_coverage" in query:
                return [{"backend_id": "primary"}]
            if "FROM usage_team_membership_daily" in query:
                return [
                    {"team_id": "dept-a", "team_name": "部门 A"},
                    {"team_id": "dept-b", "team_name": "部门 B"},
                ]
            if "FROM usage_query_daily" in query:
                base = {
                    "backend_id": "primary",
                    "usage_date": date(2026, 8, 3),
                    "user_id": "alice",
                    "principal_id": "principal-alice",
                    "employee_email": "alice@example.com",
                    "employee_name": "Alice",
                    "source": "Codex",
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                    "request_count": 1,
                    "success_count": 1,
                    "failure_count": 0,
                    "spend": 0.1,
                }
                return [
                    {**base, "team_id": "dept-a", "model_name": "gpt-5.6-sol"},
                    {**base, "team_id": "dept-b", "model_name": "openai/chatgpt-gpt-5.6-sol"},
                ]
            raise AssertionError(query)

        async def fetchval(self, query, *args):
            if "organization_id=''" in query:
                return 0
            if "MAX(synced_at)" in query:
                return datetime(2026, 8, 3, tzinfo=timezone.utc)
            raise AssertionError(query)

    store = UsageStore("postgresql://unused")
    store.pool = Pool()

    result = asyncio.run(
        store.organization_rows(
            "org-1", "2026-08-03", "2026-08-03", "all", ["primary"]
        )
    )

    assert len(result["rows"]) == 2
    assert {row["departmentId"] for row in result["rows"]} == {"dept-a", "dept-b"}
    assert {row["model"] for row in result["rows"]} == {"gpt-5.6-sol"}
    assert len(result["summaryRows"]) == 1
    assert result["summaryRows"][0]["totalTokens"] == 4


def test_coalesce_usage_rows_prevents_duplicate_upsert_records() -> None:
    rows = [
        {"date": "2026-07-22", "_userId": "alice", "source": "其他", "model": "m", "totalTokens": 2},
        {"date": "2026-07-22", "_userId": "alice", "source": "其他", "model": "m", "totalTokens": 3},
    ]

    result = UsageStore._coalesce_usage_rows(rows)

    assert len(result) == 1
    assert result[0]["totalTokens"] == 5


def test_coalesce_usage_rows_merges_normalized_route_models_by_source() -> None:
    rows = [
        {"date": "2026-07-22", "_userId": "alice", "source": "Codex", "model": "openrouter/anthropic/claude-opus-5", "totalTokens": 2, "requestCount": 1},
        {"date": "2026-07-22", "_userId": "alice", "source": "Codex", "model": "claude-opus-5", "totalTokens": 3, "requestCount": 2},
        {"date": "2026-07-22", "_userId": "alice", "source": "Claude Code", "model": "claude-opus-5", "totalTokens": 5, "requestCount": 1},
    ]

    result = UsageStore._coalesce_usage_rows(rows)
    by_source = {item["source"]: item for item in result}

    assert len(result) == 2
    assert by_source["Codex"]["model"] == "claude-opus-5"
    assert by_source["Codex"]["totalTokens"] == 5
    assert by_source["Codex"]["requestCount"] == 3
    assert by_source["Claude Code"]["totalTokens"] == 5


def test_team_member_rows_merge_normalized_models_without_mixing_sources() -> None:
    rows = [
        {
            "date": "2026-07-22",
            "source": "Codex",
            "model": "openrouter/anthropic/claude-opus-5",
            "promptTokens": 2,
            "completionTokens": 3,
            "totalTokens": 5,
            "requestCount": 1,
            "successCount": 1,
            "failureCount": 0,
            "spend": 0.1,
        },
        {
            "date": "2026-07-22",
            "source": "Codex",
            "model": "claude-opus-5",
            "promptTokens": 4,
            "completionTokens": 6,
            "totalTokens": 10,
            "requestCount": 2,
            "successCount": 1,
            "failureCount": 1,
            "spend": 0.2,
        },
        {
            "date": "2026-07-22",
            "source": "Claude Code",
            "model": "claude-opus-5",
            "promptTokens": 7,
            "completionTokens": 8,
            "totalTokens": 15,
            "requestCount": 1,
            "successCount": 1,
            "failureCount": 0,
            "spend": 0.3,
        },
    ]

    result = main.merge_team_member_usage_rows(rows)
    by_source = {item["source"]: item for item in result}

    assert len(result) == 2
    assert by_source["Codex"]["model"] == "claude-opus-5"
    assert by_source["Codex"]["promptTokens"] == 6
    assert by_source["Codex"]["completionTokens"] == 9
    assert by_source["Codex"]["totalTokens"] == 15
    assert by_source["Codex"]["requestCount"] == 3
    assert by_source["Codex"]["successCount"] == 2
    assert by_source["Codex"]["failureCount"] == 1
    assert by_source["Codex"]["spend"] == 0.1 + 0.2
    assert by_source["Claude Code"]["totalTokens"] == 15


def test_team_member_ranking_merges_accounts_by_normalized_email() -> None:
    members = [
        {"user_id": "alice-1", "employee_email": " Alice@example.com ", "employee_name": "Alice", "team_role": "user"},
        {"user_id": "alice-2", "employee_email": "alice@EXAMPLE.com", "employee_name": "Alice", "team_role": "admin"},
        {"user_id": "bob-1", "employee_email": "bob@example.com", "employee_name": "Bob", "team_role": "user"},
        {"user_id": "shared-name-1", "employee_email": "", "employee_name": "Shared", "team_role": "user"},
        {"user_id": "shared-name-2", "employee_email": "", "employee_name": "Shared", "team_role": "user"},
    ]
    alice_summary = {
        "employeeId": "alice-1",
        "employeeName": "Alice",
        "employeeEmail": "alice@example.com",
        "bindStatus": "已绑定邮箱",
        "promptTokens": 70,
        "completionTokens": 30,
        "totalTokens": 100,
        "requestCount": 4,
        "successCount": 4,
        "failureCount": 0,
        "spend": 1.5,
        "primarySource": "Codex",
        "userIds": ["alice-1", "alice-2"],
        "teamRole": "user",
    }

    result = UsageStore._merge_team_members(
        members,
        {"alice-1": alice_summary, "alice-2": alice_summary},
    )

    assert len(result) == 4
    alice = next(item for item in result if item["employeeEmail"] == "alice@example.com")
    assert alice["userIds"] == ["alice-1", "alice-2"]
    assert alice["totalTokens"] == 100
    assert alice["requestCount"] == 4
    assert alice["teamRole"] == "admin"
    assert len([item for item in result if item["employeeName"] == "Shared"]) == 2


def test_team_member_ranking_matches_usage_summary_by_email_when_user_id_differs() -> None:
    members = [
        {"user_id": "claude-code-linsen", "employee_email": "lin@example.com", "employee_name": "Lin", "team_role": "user"},
    ]
    summary = {
        "employeeId": "cursor-lin",
        "employeeName": "Lin",
        "employeeEmail": "lin@example.com",
        "userIds": ["cursor-lin"],
        "totalTokens": 399,
        "requestCount": 4,
        "spend": 1.2,
        "teamRole": "user",
    }

    result = UsageStore._merge_team_members(members, {"email:lin@example.com": summary})

    assert result[0]["totalTokens"] == 399
    assert result[0]["userIds"] == ["cursor-lin"]


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


def test_canonical_usage_rows_merge_historical_aliases_and_all_metrics() -> None:
    rows = [
        {
            "date": "2026-08-03", "source": "Codex", "model": "gpt-5.6-sol",
            "promptTokens": 2, "completionTokens": 3, "totalTokens": 5,
            "requestCount": 1, "successCount": 1, "failureCount": 0, "spend": 0.1,
        },
        {
            "date": "2026-08-03", "source": "Codex", "model": "openai/chatgpt-gpt-5.6-sol",
            "promptTokens": 7, "completionTokens": 11, "totalTokens": 18,
            "requestCount": 2, "successCount": 1, "failureCount": 1, "spend": 0.3,
        },
    ]

    result = UsageStore._canonical_usage_rows(rows, ("date", "source", "model"))

    assert len(result) == 1
    assert result[0]["model"] == "gpt-5.6-sol"
    assert result[0]["promptTokens"] == 9
    assert result[0]["completionTokens"] == 14
    assert result[0]["totalTokens"] == 23
    assert result[0]["requestCount"] == 3
    assert result[0]["successCount"] == 2
    assert result[0]["failureCount"] == 1
    assert result[0]["spend"] == pytest.approx(0.4)


def test_usage_queries_group_raw_model_names_before_python_canonicalization() -> None:
    import inspect

    source = inspect.getsource(UsageStore._query_aggregated_rows)

    assert 'model_sql = "u.model"' in source
    assert "regexp_replace" not in source


def test_usage_store_environment_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("USAGE_SYNC_ENABLED", raising=False)
    monkeypatch.setenv("USAGE_DATABASE_URL", "postgresql://unused")
    assert UsageStore.from_environment() is None


def test_usage_store_environment_requires_both_enable_flag_and_dsn(monkeypatch) -> None:
    monkeypatch.setenv("USAGE_SYNC_ENABLED", "true")
    monkeypatch.delenv("USAGE_DATABASE_URL", raising=False)
    assert UsageStore.from_environment() is None


def test_usage_sync_cli_returns_nonzero_for_partial_result(monkeypatch, capsys) -> None:
    from backend import usage_sync

    class Store:
        async def connect(self):
            return None

        async def close(self):
            return None

    class Client:
        async def close(self):
            return None

    monkeypatch.setattr(usage_sync.UsageStore, "from_environment", lambda: Store())
    monkeypatch.setattr(usage_sync, "LiteLLMClient", Client)

    class Synchronizer:
        def __init__(self, _client, _store, _repository=None):
            pass

        @staticmethod
        def date_range(days):
            assert days == 90
            return "2026-05-06", "2026-08-03"

        async def sync(self, start_date, end_date):
            assert (start_date, end_date) == ("2026-05-06", "2026-08-03")
            return {"status": "partial", "rowCount": 3, "backendCount": 1, "errors": ["her: RuntimeError"]}

    monkeypatch.setattr(usage_sync, "UsageSynchronizer", Synchronizer)

    assert asyncio.run(usage_sync._run_cli(90)) == 1
    assert '"status": "partial"' in capsys.readouterr().out


def test_usage_sync_cli_refreshes_recent_log_window_after_long_backfill(monkeypatch, capsys) -> None:
    from backend import usage_sync

    class Store:
        async def connect(self):
            return None

        async def close(self):
            return None

    class Client:
        async def close(self):
            return None

    calls: list[tuple[str, str]] = []

    class Synchronizer:
        def __init__(self, _client, _store, _repository=None):
            pass

        @staticmethod
        def date_range(days):
            return {
                90: ("2026-05-06", "2026-08-03"),
                3: ("2026-08-01", "2026-08-03"),
            }[days]

        async def sync(self, start_date, end_date):
            calls.append((start_date, end_date))
            return {
                "status": "ok",
                "rowCount": 100 if start_date == "2026-05-06" else 12,
                "backendCount": 2,
                "errors": [],
            }

    monkeypatch.setenv("USAGE_SYNC_LOG_TIMEZONE_ENABLED", "true")
    monkeypatch.setenv("USAGE_SYNC_LOG_MAX_WINDOW_DAYS", "3")
    monkeypatch.setattr(usage_sync.UsageStore, "from_environment", lambda: Store())
    monkeypatch.setattr(usage_sync, "LiteLLMClient", Client)
    monkeypatch.setattr(usage_sync, "UsageSynchronizer", Synchronizer)

    assert asyncio.run(usage_sync._run_cli(90)) == 0
    assert calls == [
        ("2026-05-06", "2026-08-03"),
        ("2026-08-01", "2026-08-03"),
    ]
    output = capsys.readouterr().out
    assert '"status": "ok"' in output
    assert '"recentRefresh": {"backendCount": 2, "days": 3' in output


def test_usage_sync_cli_fails_when_recent_refresh_is_partial(monkeypatch) -> None:
    from backend import usage_sync

    results = [
        {"status": "ok", "rowCount": 100, "backendCount": 2, "errors": []},
        {
            "status": "partial",
            "rowCount": 10,
            "backendCount": 1,
            "errors": ["primary: RuntimeError"],
        },
    ]

    async def fake_run_sync_once(_client, _store, _days):
        return results.pop(0)

    monkeypatch.setenv("USAGE_SYNC_LOG_TIMEZONE_ENABLED", "true")
    monkeypatch.setenv("USAGE_SYNC_LOG_MAX_WINDOW_DAYS", "3")
    monkeypatch.setattr(usage_sync, "run_sync_once", fake_run_sync_once)

    result = asyncio.run(
        usage_sync.run_sync_with_recent_refresh(object(), object(), 90)
    )

    assert result["status"] == "partial"
    assert result["errors"] == ["primary: RuntimeError"]
    assert result["recentRefresh"]["days"] == 3


def test_usage_store_date_values_are_asyncpg_compatible() -> None:
    usage_record = UsageStore._usage_record(
        "primary",
        {"date": "2026-07-22", "_userId": "alice", "model": "gpt-4o"},
        datetime.now(timezone.utc),
    )
    membership_record = UsageStore._membership_record(
        "primary",
        {"snapshotDate": "2026-07-22", "teamId": "team-1", "userId": "alice"},
    )

    assert usage_record[1] == date(2026, 7, 22)
    assert isinstance(usage_record[1], date)
    assert membership_record[1] == date(2026, 7, 22)
    assert isinstance(membership_record[1], date)


def test_usage_schema_is_idempotent_and_uses_aggregate_only_columns() -> None:
    from backend.usage_store import USAGE_SCHEMA

    assert USAGE_SCHEMA.count("CREATE TABLE IF NOT EXISTS usage_daily") == 1
    assert "organization_id, team_id, key_id" in USAGE_SCHEMA
    assert "api_key" not in USAGE_SCHEMA.lower()
    assert "prompt TEXT" not in USAGE_SCHEMA
    assert "response TEXT" not in USAGE_SCHEMA


def test_usage_schema_contains_query_indexes() -> None:
    from backend.usage_store import USAGE_SCHEMA

    assert "usage_daily_date_backend_user_idx" in USAGE_SCHEMA
    assert "usage_daily_date_source_model_idx" in USAGE_SCHEMA
    assert "usage_team_membership_usage_join_idx" in USAGE_SCHEMA
    assert "usage_team_membership_team_filter_idx" in USAGE_SCHEMA


def test_usage_schema_adds_organization_columns_before_dependent_indexes() -> None:
    from backend.usage_store import USAGE_SCHEMA

    add_column = USAGE_SCHEMA.index(
        "ALTER TABLE usage_daily ADD COLUMN IF NOT EXISTS organization_id"
    )
    create_index = USAGE_SCHEMA.index(
        "CREATE INDEX IF NOT EXISTS usage_daily_org_date_idx"
    )

    assert add_column < create_index


def test_usage_schema_contains_identity_directory_table_and_indexes() -> None:
    from backend.usage_store import USAGE_SCHEMA

    assert "CREATE TABLE IF NOT EXISTS usage_identity_directory" in USAGE_SCHEMA
    assert "PRIMARY KEY (backend_id, user_id)" in USAGE_SCHEMA
    assert "usage_identity_directory_email_idx" in USAGE_SCHEMA
    assert "usage_identity_directory_updated_idx" in USAGE_SCHEMA


def test_upsert_identity_directory_writes_nonempty_identity_fields() -> None:
    class FakePool:
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[tuple[object, ...]]]] = []

        async def executemany(self, query, args):
            self.calls.append((query, list(args)))

    store = UsageStore("postgresql://unused")
    pool = FakePool()
    store.pool = pool

    count = asyncio.run(
        store.upsert_identity_directory(
            "primary",
            [{"userId": "u-1", "displayName": "Alice", "employeeEmail": "alice@example.com", "nameSource": "litellm_user_alias", "confidence": "high"}],
        )
    )

    assert count == 1
    query, args = pool.calls[0]
    assert "INSERT INTO usage_identity_directory" in query
    assert "ON CONFLICT (backend_id, user_id)" in query
    assert args == [("primary", "u-1", "Alice", "alice@example.com", "litellm_user_alias", "high")]


def test_identity_directory_returns_keyed_records() -> None:
    class FakePool:
        async def fetch(self, query, *args):
            assert "FROM usage_identity_directory" in query
            assert args == (["primary"],)
            return [{"backend_id": "primary", "user_id": "u-1", "display_name": "Alice", "employee_email": "alice@example.com", "name_source": "litellm_user_alias", "confidence": "high"}]

    store = UsageStore("postgresql://unused")
    store.pool = FakePool()
    result = asyncio.run(store.identity_directory(["primary"]))

    assert result[("primary", "u-1")] == {
        "displayName": "Alice",
        "employeeEmail": "alice@example.com",
        "nameSource": "litellm_user_alias",
        "confidence": "high",
    }


def test_refresh_usage_identity_columns_only_replaces_empty_or_user_id_names() -> None:
    class FakePool:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []

        async def execute(self, query, *args):
            self.calls.append((query, args))
            return "UPDATE 3"

    store = UsageStore("postgresql://unused")
    pool = FakePool()
    store.pool = pool
    updated = asyncio.run(store.refresh_usage_identity_columns(["primary"]))

    assert updated == 6
    assert len(pool.calls) == 2
    for query, args in pool.calls:
        assert "UPDATE usage_" in query
        assert "FROM usage_identity_directory" in query
        assert "u.employee_name = '' OR u.employee_name = u.user_id" in query
        assert args == (["primary"],)


def test_model_usage_counts_uses_complete_database_coverage_and_normalizes_models() -> None:
    class FakePool:
        async def fetch(self, query, *_args):
            if "FROM usage_sync_coverage" in query:
                return [{"backend_id": "primary"}, {"backend_id": "secondary"}]
            return [
                {"model": "chatgpt-acct-1-gpt-4o", "request_count": 2},
                {"model": "gpt-4o", "request_count": 3},
            ]

    store = UsageStore("postgresql://unused")
    store.pool = FakePool()

    result = asyncio.run(store.model_usage_counts("2026-07-01", "2026-07-03", ["primary", "secondary"]))

    assert result == {"gpt-4o": 5}


def test_rows_by_employee_emails_requires_all_backends_and_merges_her() -> None:
    class FakePool:
        async def fetch(self, query, *_args):
            if "FROM usage_sync_coverage" in query:
                return [{"backend_id": "primary"}, {"backend_id": "her"}]
            return [
                {"employee_email": "alice@example.com", "usage_date": date(2026, 7, 22), "source": "Cursor", "model": "gpt-5", "prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10, "request_count": 1, "success_count": 1, "failure_count": 0, "spend": 0.1, "user_ids": ["alice-primary"]},
                {"employee_email": "alice@example.com", "usage_date": date(2026, 7, 22), "source": "Her", "model": "gpt-5", "prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20, "request_count": 2, "success_count": 2, "failure_count": 0, "spend": 0.2, "user_ids": ["alice-her"]},
            ]

        async def fetchval(self, *_args):
            return datetime(2026, 7, 22, tzinfo=timezone.utc)

    store = UsageStore("postgresql://unused")
    store.pool = FakePool()
    result = asyncio.run(store.rows_by_employee_emails(["alice@example.com"], "2026-07-22", "2026-07-22", "all", ["primary", "her"]))

    assert result is not None
    alice = result["alice@example.com"]
    assert sum(row["totalTokens"] for row in alice["rows"]) == 30
    assert alice["userIds"] == ["alice-her", "alice-primary"]


def test_team_rows_uses_one_cross_backend_query_and_merges_email_identity() -> None:
    class Record(dict):
        pass

    class FakePool:
        calls = 0

        async def fetch(self, query, *args):
            if "FROM usage_sync_coverage" in query:
                return [{"backend_id": "primary"}, {"backend_id": "her"}]
            self.calls += 1
            assert "u.team_id" in query
            assert "m.snapshot_date=u.usage_date" not in query
            assert "m.snapshot_date <= $4::date" in query
            assert args[0] == ["primary", "her"]
            assert args[1] == ["team-a", "team-a"]
            return [
                Record(kind="member", backend_id="primary", team_id="team-a", team_name="Team A", user_id="alice-primary", employee_email="alice@example.com", employee_name="Alice", team_role="user", usage_date=None, source=None, model_name=None, prompt_tokens=0, completion_tokens=0, total_tokens=0, request_count=0, success_count=0, failure_count=0, spend=0),
                Record(kind="member", backend_id="her", team_id="team-a", team_name="Team A", user_id="alice-her", employee_email="alice@example.com", employee_name="Alice", team_role="user", usage_date=None, source=None, model_name=None, prompt_tokens=0, completion_tokens=0, total_tokens=0, request_count=0, success_count=0, failure_count=0, spend=0),
                Record(kind="usage", backend_id="primary", team_id=None, team_name=None, user_id="alice-primary", employee_email="alice@example.com", employee_name="Alice", team_role=None, usage_date=date(2026, 7, 22), source="Codex", model_name="gpt-5", prompt_tokens=4, completion_tokens=6, total_tokens=10, request_count=1, success_count=1, failure_count=0, spend=0.1),
                Record(kind="usage", backend_id="her", team_id=None, team_name=None, user_id="alice-her", employee_email="alice@example.com", employee_name="Alice", team_role=None, usage_date=date(2026, 7, 22), source="Her", model_name="gpt-5", prompt_tokens=8, completion_tokens=12, total_tokens=20, request_count=2, success_count=2, failure_count=0, spend=0.2),
            ]

        async def fetchval(self, *_args):
            return datetime(2026, 7, 22, tzinfo=timezone.utc)

    store = UsageStore("postgresql://unused")
    store.pool = FakePool()
    result = asyncio.run(store.team_rows([{"backend": "primary", "id": "team-a", "name": "Team A"}, {"backend": "her", "id": "team-a", "name": "Team A"}], "2026-07-22", "2026-07-22", "all"))

    assert store.pool.calls == 1
    assert len(result["employees"]) == 1
    assert result["employees"][0]["totalTokens"] == 30
    assert result["dataQuality"]["backends"] == ["her", "primary"]
    assert result["dataQuality"]["teamAttribution"] == "usage_event_team_id"
    assert result["dataQuality"]["memberDirectory"] == "latest_snapshot_on_or_before_end_date"


def test_team_member_rows_uses_event_team_scope_and_latest_member_snapshot() -> None:
    class Record(dict):
        pass

    class FakePool:
        async def fetch(self, query, *args):
            if "FROM usage_sync_coverage" in query:
                return [{"backend_id": "primary"}]
            assert "m.snapshot_date <= $4::date" in query
            assert "m.snapshot_date=u.usage_date" not in query
            assert "sc.team_id=u.team_id" in query
            assert "FROM selected s" in query
            return [
                Record(kind="member", backend_id="primary", team_id="team-a", team_name="Team A", user_id="alice", employee_email="alice@example.com", employee_name="Alice", team_role="admin", usage_date=None, source=None, model_name=None, prompt_tokens=0, completion_tokens=0, total_tokens=0, request_count=0, success_count=0, failure_count=0, spend=0),
                Record(kind="usage", backend_id="primary", team_id=None, team_name=None, user_id="alice", employee_email="alice@example.com", employee_name="Alice", team_role=None, usage_date=date(2026, 7, 22), source="Codex", model_name="gpt-5", prompt_tokens=4, completion_tokens=6, total_tokens=10, request_count=1, success_count=1, failure_count=0, spend=0.1),
            ]

        async def fetchval(self, *_args):
            return datetime(2026, 7, 22, tzinfo=timezone.utc)

    store = UsageStore("postgresql://unused")
    store.pool = FakePool()
    result = asyncio.run(store.team_member_rows([{"backend": "primary", "id": "team-a", "name": "Team A"}], "alice", "2026-07-22", "2026-07-22", "all"))

    assert result is not None
    assert result["summary"]["rangeTotal"]["totalTokens"] == 10
    assert result["dataQuality"]["teamAttribution"] == "usage_event_team_id"
    assert result["dataQuality"]["memberDirectory"] == "latest_snapshot_on_or_before_end_date"


def test_model_usage_counts_returns_none_when_any_backend_lacks_coverage() -> None:
    class FakePool:
        async def fetch(self, query, *_args):
            assert "FROM usage_sync_coverage" in query
            return [{"backend_id": "primary"}]

    store = UsageStore("postgresql://unused")
    store.pool = FakePool()

    result = asyncio.run(store.model_usage_counts("2026-07-01", "2026-07-03", ["primary", "secondary"]))

    assert result is None


def test_source_detection_falls_back_to_other_without_request_details() -> None:
    assert detect_source({"user": "cursor-alice", "metadata": {}}) == "Cursor"
    assert detect_source({"key_alias": "claude-code-alice"}) == "Claude Code"
    assert detect_source({"user": "ordinary-account"}) == "其他"
    assert detect_source_from_key({"name": "personal-cursor-key"}) == "Cursor"
    assert detect_source_from_key({"name": "unclassified"}) == "其他"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_tags", ["User-Agent: claude-cli", "User-Agent: claude-cli/2.1.220"]),
        ("request_tags", {"user_agent": "claude-cli/2.1.220"}),
        ("request_tags", '["User-Agent: claude-cli/2.1.220"]'),
        ("request_tags", "User-Agent: claude-cli/2.1.220"),
        ("tags", {"user_agent": "claude-cli/2.1.220"}),
        ("tags", ["User-Agent: claude-cli/2.1.220"]),
        ("tags", '["User-Agent: claude-cli/2.1.220"]'),
        ("tags", "User-Agent: claude-cli/2.1.220"),
    ],
)
def test_source_detection_prioritizes_claude_cli_tags_over_legacy_cursor_identity(field, value) -> None:
    record = {
        "user": "cursor-zhuyida",
        "metadata": {"user_api_key_user_id": "cursor-zhuyida"},
        field: value,
    }

    assert detect_source(record) == "Claude Code"


@pytest.mark.parametrize(
    "metadata",
    [
        {"client": "claude-code"},
        ["client: claude-code"],
        '{"client": "claude-code"}',
        "claude-code",
    ],
)
def test_source_detection_prioritizes_explicit_metadata_claude_code_over_legacy_cursor_identity(metadata) -> None:
    assert detect_source({"user": "cursor-zhuyida", "metadata": metadata}) == "Claude Code"


def test_source_detection_falls_back_to_legacy_identity_without_claude_cli_tags() -> None:
    record = {
        "user": "cursor-zhuyida",
        "metadata": {"user_api_key_user_id": "cursor-zhuyida"},
    }

    assert detect_source({**record, "request_tags": []}) == "Cursor"
    assert detect_source({**record, "request_tags": ["User-Agent: curl/8.0"]}) == "Cursor"
    assert detect_source({**record, "request_tags": None}) == "Cursor"
    assert detect_source({**record, "request_tags": "User-Agent: notclaude-cli/2.1.220"}) == "Cursor"
    assert detect_source({**record, "request_tags": "[malformed"}) == "Cursor"
    assert detect_source({**record, "tags": "[malformed"}) == "Cursor"
    assert detect_source({**record, "metadata": '{"client":'}) == "Cursor"


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


def test_organization_daily_spend_groups_only_explicit_attribution() -> None:
    class Pool:
        async def fetch(self, query, *args):
            assert "organization_id <> ''" in query
            assert "billing_eligible = TRUE" in query
            assert "GROUP BY organization_id, usage_date" in query
            assert "ROUND(COALESCE(SUM(spend), 0)::numeric, 6)" in query
            assert args[2] == ["primary"]
            return [
                {
                    "organization_id": "org-upstream",
                    "usage_date": date(2026, 7, 30),
                    "spend": Decimal("12.340001"),
                }
            ]

    store = UsageStore("postgresql://unused")
    store.pool = Pool()

    rows = asyncio.run(
        store.organization_daily_spend(
            "2026-07-30", "2026-07-30", ["primary"]
        )
    )

    assert rows == [
        {
            "upstreamOrganizationId": "org-upstream",
            "usageDate": "2026-07-30",
            "spendUsd": "12.340001",
        }
    ]


def test_organization_daily_spend_excludes_pre_credit_and_credit_day() -> None:
    class Pool:
        async def fetch(self, _query, *_args):
            return [
                {
                    "organization_id": "org-upstream",
                    "usage_date": date(2026, 7, 28),
                    "spend": Decimal("9.05"),
                },
                {
                    "organization_id": "org-upstream",
                    "usage_date": date(2026, 7, 29),
                    "spend": Decimal("1.00"),
                },
                {
                    "organization_id": "org-upstream",
                    "usage_date": date(2026, 7, 30),
                    "spend": Decimal("0.25"),
                },
            ]

    store = UsageStore("postgresql://unused")
    store.pool = Pool()
    rows = asyncio.run(
        store.organization_daily_spend(
            "2026-07-28",
            "2026-07-30",
            ["primary"],
            billing_effective_at_by_organization={
                "org-upstream": datetime(
                    2026, 7, 29, 12, tzinfo=timezone.utc
                )
            },
        )
    )

    assert rows == [
        {
            "upstreamOrganizationId": "org-upstream",
            "usageDate": "2026-07-29",
            "spendUsd": "1.00",
            "settlementStatus": "skipped",
            "settlementReason": "needs_event_time",
        },
        {
            "upstreamOrganizationId": "org-upstream",
            "usageDate": "2026-07-30",
            "spendUsd": "0.25",
        }
    ]


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

    async def capture(_backend, _users, _start, _end, account_index=None, directory=None):
        captured["account_index"] = account_index
        return []

    synchronizer.collect_memberships = capture
    asyncio.run(synchronizer.collect_backend(Backend(), "2026-07-20", "2026-07-22"))
    assert captured["account_index"]["profiles"]["carher-001"]["email"] == "alice@example.com"


def test_usage_sync_expands_team_membership_to_all_email_accounts() -> None:
    class FakeClient:
        def _admin_user_map(self, _users):
            return {"team-user": {"name": "Alice", "email": "alice@example.com", "userIds": ["team-user"]}}

        async def teams(self, _backend):
            return [{"team_id": "team-a", "members_with_roles": [{"user_id": "team-user", "user_email": "alice@example.com", "role": "user"}]}]

        async def resolve_user(self, _email, _name):
            return {"matched_accounts": [
                {"backend": "primary", "user_id": "team-user"},
                {"backend": "primary", "user_id": "cursor-user"},
                {"backend": "secondary", "user_id": "other-user"},
            ]}

        def _is_backend_usage_account(self, _backend, _user_id):
            return True

    backend = type("Backend", (), {"id": "primary", "source": None})()
    synchronizer = UsageSynchronizer(FakeClient(), object())

    rows = asyncio.run(synchronizer.collect_memberships(
        backend,
        [{"user_id": "team-user", "user_email": "alice@example.com"}, {"user_id": "cursor-user", "user_email": "alice@example.com"}],
        "2026-07-22",
        "2026-07-22",
    ))

    team_rows = [row for row in rows if row["teamId"] == "team-a"]
    assert {row["userId"] for row in team_rows} == {"team-user", "cursor-user"}
    assert len(team_rows) == 2


def test_usage_sync_expands_team_membership_when_upstream_member_omits_email() -> None:
    class FakeClient:
        def _admin_user_map(self, _users):
            return {
                "claude-code-tankaiwen": {
                    "name": "谭凯文",
                    "email": "tankaiwen@auto-link.com.cn",
                    "userIds": ["claude-code-tankaiwen", "cursor-tankaiwen"],
                },
                "cursor-tankaiwen": {
                    "name": "谭凯文",
                    "email": "tankaiwen@auto-link.com.cn",
                    "userIds": ["claude-code-tankaiwen", "cursor-tankaiwen"],
                },
            }

        async def teams(self, _backend):
            return [{
                "team_id": "team-ai-infra",
                "team_alias": "AI Infra部",
                "members_with_roles": [{"user_id": "claude-code-tankaiwen", "role": "user"}],
            }]

        def _is_backend_usage_account(self, _backend, _user_id):
            return True

    backend = type("Backend", (), {"id": "primary", "source": None})()
    synchronizer = UsageSynchronizer(
        FakeClient(),
        object(),
    )

    rows = asyncio.run(synchronizer.collect_memberships(
        backend,
        [
            {"user_id": "claude-code-tankaiwen", "user_email": "tankaiwen@auto-link.com.cn"},
            {"user_id": "cursor-tankaiwen", "user_email": "tankaiwen@auto-link.com.cn"},
        ],
        "2026-08-25",
        "2026-08-25",
    ))

    team_rows = [row for row in rows if row["teamId"] == "team-ai-infra"]
    assert {row["userId"] for row in team_rows} == {
        "claude-code-tankaiwen",
        "cursor-tankaiwen",
    }


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


def test_department_directory_sync_does_not_collect_usage_or_team_details() -> None:
    class Client:
        backends = [type("Backend", (), {"id": "primary"})()]

        async def teams(self, _backend, include_details=True):
            assert include_details is False
            return [{
                "team_id": "team-baic",
                "team_alias": "北汽集团",
                "organization_id": "org-baic",
                "members_with_roles": [],
            }]

    class Store:
        async def replace_department_directory(self, backend_id, departments):
            assert backend_id == "primary"
            assert departments[0]["departmentId"] == "team-baic"
            assert departments[0]["departmentName"] == "北汽集团"
            return len(departments)

    result = asyncio.run(UsageSynchronizer(Client(), Store()).sync_department_directories())

    assert result == {
        "status": "ok",
        "backends": {"primary": 1},
        "departmentCount": 1,
    }
