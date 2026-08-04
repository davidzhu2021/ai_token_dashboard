"""同步按北京时间日界归日的回归测试。

上游 /user/daily/activity 的 date 取自 startTime 的 UTC 日期，会把北京时间
00:00-08:00 的用量归到前一天。这里覆盖改用原始日志归日后的行为。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.litellm_client import LiteLLMBackend, LiteLLMClient
from backend.usage_sync import (
    UsageSynchronizer,
    run_pending_usage_backfills,
    run_usage_backfill_once,
)


def _report_only_mapping(*, through: str = "2026-07-28T02:00:00+00:00") -> dict:
    return {
        "backendId": "primary",
        "upstreamKeyHash": "a" * 64,
        "organizationId": "org-baic-upstream",
        "teamId": "team-baic-upstream",
        "principalId": "principal-lianghaiqiang",
        "memberId": "",
        "mode": "report_only",
        "attributionSource": "legacy_report_only",
        "billingEligible": False,
        "effectiveFrom": "2026-01-01T00:00:00+00:00",
        "effectiveThrough": through,
    }


PRIMARY = LiteLLMBackend(id="primary", label="通衢 API", base_url="http://x", admin_key="sk-admin")


def _log(user: str, start_time: str, tokens: int, spend: str, model: str = "gpt-5.6", status: str = "success") -> dict:
    return {
        "user": user,
        "startTime": start_time,
        "model": model,
        "spend": spend,
        "total_tokens": str(tokens),
        "prompt_tokens": str(tokens),
        "completion_tokens": "0",
        "status": status,
    }


class _LogClient(LiteLLMClient):
    """只桩掉 HTTP 层，保留真实的归日/聚合逻辑。"""

    def __init__(self, pages: list[list[dict]]) -> None:
        self._pages = pages
        self.backends = [PRIMARY]
        self._deployment_model_maps = {}
        self.requests: list[dict] = []

    async def _ensure_deployment_model_map(self, _backend):
        return {}

    async def request_backend(self, backend, method, path, **kwargs):  # type: ignore[override]
        params = kwargs.get("params") or {}
        self.requests.append(params)
        page = int(params.get("page") or 1)
        data = self._pages[page - 1] if 0 < page <= len(self._pages) else []
        return {"data": data, "total_pages": len(self._pages), "page": page}


def test_beijing_early_morning_usage_counts_toward_local_today(monkeypatch) -> None:
    """北京时间 07:00 的调用（UTC 前一天 23:00）应归到北京时间当天。"""
    monkeypatch.setenv("USAGE_TIMEZONE_OFFSET_MINUTES", "-480")

    client = _LogClient(
        [
            [
                # UTC 2026-07-27T23:00 == 北京 2026-07-28 07:00 -> 归到 07-28
                _log("claude-code-alice", "2026-07-27T23:00:00+00:00", 1000, "1.5"),
                # UTC 2026-07-28T06:00 == 北京 2026-07-28 14:00 -> 同样归到 07-28
                _log("claude-code-alice", "2026-07-28T06:00:00+00:00", 500, "0.5"),
            ]
        ]
    )

    rows, complete = asyncio.run(client.sync_rows_from_logs("2026-07-28", "2026-07-28", PRIMARY))

    assert complete is True
    alice = rows["claude-code-alice"]
    assert len(alice) == 1, "同一天同模型应聚合成一行"
    assert alice[0]["date"] == "2026-07-28"
    assert alice[0]["totalTokens"] == 1500
    assert alice[0]["spend"] == pytest.approx(2.0)
    assert alice[0]["requestCount"] == 2


def test_logs_outside_local_window_are_dropped(monkeypatch) -> None:
    """窗口外的记录（UTC 拉宽导致）必须按本地日界剔除，避免多算。"""
    monkeypatch.setenv("USAGE_TIMEZONE_OFFSET_MINUTES", "-480")

    client = _LogClient(
        [
            [
                # UTC 2026-07-27T15:00 == 北京 2026-07-27 23:00 -> 属于前一天，应剔除
                _log("claude-code-alice", "2026-07-27T15:00:00+00:00", 999, "9.9"),
                _log("claude-code-alice", "2026-07-28T02:00:00+00:00", 100, "0.1"),
            ]
        ]
    )

    rows, _ = asyncio.run(client.sync_rows_from_logs("2026-07-28", "2026-07-28", PRIMARY))

    assert rows["claude-code-alice"][0]["totalTokens"] == 100
    assert all(row["date"] == "2026-07-28" for row in rows["claude-code-alice"])


def test_scan_groups_all_users_in_one_pass(monkeypatch) -> None:
    """一次全局扫描要覆盖多个账号，避免按账号逐个查询。"""
    monkeypatch.setenv("USAGE_TIMEZONE_OFFSET_MINUTES", "-480")

    client = _LogClient(
        [
            [
                _log("claude-code-alice", "2026-07-28T01:00:00+00:00", 100, "1.0"),
                _log("cursor-bob", "2026-07-28T02:00:00+00:00", 200, "2.0"),
            ],
            [
                _log("cursor-bob", "2026-07-28T03:00:00+00:00", 300, "3.0"),
                _log("carher-9", "2026-07-28T04:00:00+00:00", 400, "4.0"),
            ],
        ]
    )

    rows, complete = asyncio.run(client.sync_rows_from_logs("2026-07-28", "2026-07-28", PRIMARY))

    assert complete is True
    assert set(rows) == {"claude-code-alice", "cursor-bob", "carher-9"}
    assert rows["cursor-bob"][0]["totalTokens"] == 500, "跨页同账号要累加"
    # 每页各请求一次，不按账号放大请求数
    assert [int(r["page"]) for r in client.requests] == [1, 2]


def test_log_sync_can_filter_by_stable_key_hash(monkeypatch) -> None:
    monkeypatch.setenv("USAGE_TIMEZONE_OFFSET_MINUTES", "-480")
    key_hash = "a" * 64
    client = _LogClient(
        [[_log("claude-code-alice", "2026-07-28T01:00:00+00:00", 100, "1.0")]]
    )

    asyncio.run(
        client.sync_rows_from_logs(
            "2026-07-28", "2026-07-28", PRIMARY, api_key=key_hash
        )
    )

    assert client.requests[0]["api_key"] == key_hash


def test_log_sync_retains_explicit_org_team_and_hashed_key_attribution(monkeypatch) -> None:
    monkeypatch.setenv("USAGE_TIMEZONE_OFFSET_MINUTES", "-480")
    first = _log("alice", "2026-07-28T01:00:00+00:00", 100, "1.0")
    first.update(
        {
            "organization_id": "org-explicit",
            "team_id": "team-explicit",
            "api_key": "hashed-key-1",
            "metadata": {
                "user_api_key_org_id": "org-metadata",
                "user_api_key_team_id": "team-metadata",
            },
        }
    )
    second = _log("alice", "2026-07-28T02:00:00+00:00", 50, "0.5")
    second["metadata"] = {
        "user_api_key_org_id": "org-metadata",
        "user_api_key_team_id": "team-metadata",
        "user_api_key": "sk-never-persist-this",
    }
    client = _LogClient([[first, second]])

    rows, complete = asyncio.run(client.sync_rows_from_logs("2026-07-28", "2026-07-28", PRIMARY))

    assert complete is True
    assert len(rows["alice"]) == 2
    by_org = {row["organizationId"]: row for row in rows["alice"]}
    assert by_org["org-explicit"]["teamId"] == "team-explicit"
    assert by_org["org-explicit"]["keyId"] == "hashed-key-1"
    assert by_org["org-metadata"]["teamId"] == "team-metadata"
    assert by_org["org-metadata"]["keyId"] != "sk-never-persist-this"
    assert len(by_org["org-metadata"]["keyId"]) == 64


def test_legacy_mapping_applies_only_within_immutable_reporting_window() -> None:
    mapping = _report_only_mapping()
    index = {("key_hash", "a" * 64): [mapping]}
    rows = [
        {
            "date": "2026-07-28",
            "eventTime": "2026-07-28T01:59:59+00:00",
            "keyId": "a" * 64,
        },
        {
            "date": "2026-07-28",
            "eventTime": "2026-07-28T02:00:01+00:00",
            "keyId": "a" * 64,
        },
    ]

    UsageSynchronizer._apply_token_attribution(rows, index)

    assert rows[0]["organizationId"] == "org-baic-upstream"
    assert rows[0]["principalId"] == "principal-lianghaiqiang"
    assert "memberId" not in rows[0]
    assert rows[0]["attributionSource"] == "legacy_report_only"
    assert rows[0]["billingEligible"] is False
    assert "organizationId" not in rows[1]


def test_report_only_mapping_adds_policy_when_log_scope_already_matches() -> None:
    key_hash = "a" * 64
    row = {
        "date": "2026-07-28",
        "eventTime": "2026-07-28T01:00:00+00:00",
        "keyId": key_hash,
        "organizationId": "org-baic-upstream",
        "teamId": "team-baic-upstream",
    }

    UsageSynchronizer._apply_token_attribution(
        [row], {("key_hash", key_hash): [_report_only_mapping()]}
    )

    assert row["organizationId"] == "org-baic-upstream"
    assert row["principalId"] == "principal-lianghaiqiang"
    assert row["attributionSource"] == "legacy_report_only"
    assert row["billingEligible"] is False


def test_explicit_scope_conflicting_with_stable_key_mapping_is_quarantined() -> None:
    key_hash = "a" * 64
    row = {
        "date": "2026-07-28",
        "eventTime": "2026-07-28T01:00:00+00:00",
        "keyId": key_hash,
        "organizationId": "org-other",
        "teamId": "team-other",
    }

    UsageSynchronizer._apply_token_attribution(
        [row], {("key_hash", key_hash): [_report_only_mapping()]}
    )

    assert row["organizationId"] == ""
    assert row["teamId"] == ""
    assert row["attributionSource"] == "tenant_mapping_conflict"
    assert row["billingEligible"] is False


def test_post_cutoff_usage_remains_unattributed_until_key_is_managed() -> None:
    key_hash = "a" * 64
    rows = [
        {
            "date": "2026-07-29",
            "eventTime": "2026-07-29T00:00:00+00:00",
            "keyId": key_hash,
        }
    ]
    index = {("key_hash", key_hash): [_report_only_mapping()]}

    UsageSynchronizer._apply_token_attribution(rows, index)

    assert rows[0].get("organizationId", "") == ""
    assert rows[0].get("billingEligible") is None


def test_principal_and_optional_member_are_separate_attribution_fields() -> None:
    key_hash = "a" * 64
    rows = [
        {
            "date": "2026-07-28",
            "eventTime": "2026-07-28T01:00:00+00:00",
            "keyId": key_hash,
        }
    ]
    index = {
        ("key_hash", key_hash): [
            {
                **_report_only_mapping(),
                "memberId": "member-lianghaiqiang",
            }
        ]
    }

    UsageSynchronizer._apply_token_attribution(rows, index)

    assert rows[0]["principalId"] == "principal-lianghaiqiang"
    assert rows[0]["memberId"] == "member-lianghaiqiang"


def test_report_only_mapping_is_backend_and_hash_scoped() -> None:
    class Repository:
        async def usage_token_attribution_map(self):
            return [
                _report_only_mapping(),
                {**_report_only_mapping(), "backendId": "her", "organizationId": "other"},
            ]

    synchronizer = UsageSynchronizer(object(), object(), Repository())
    primary = asyncio.run(synchronizer._token_attribution_map("primary"))
    her = asyncio.run(synchronizer._token_attribution_map("her"))

    assert primary[("key_hash", "a" * 64)][0]["organizationId"] == "org-baic-upstream"
    assert her[("key_hash", "a" * 64)][0]["organizationId"] == "other"


def test_organization_mapping_repository_failure_fails_closed() -> None:
    class Repository:
        async def usage_token_attribution_map(self):
            raise RuntimeError("database unavailable")

    synchronizer = UsageSynchronizer(object(), object(), Repository())

    with pytest.raises(
        RuntimeError,
        match="organization token attribution mappings are unavailable",
    ):
        asyncio.run(synchronizer._token_attribution_map("primary"))


def test_report_only_mapping_falls_back_to_upstream_user_id() -> None:
    row = {
        "_userId": "liang-upstream-user",
        "organizationId": "org-baic-upstream",
        "teamId": "team-baic-upstream",
        "eventTime": "2026-07-28T01:00:00+00:00",
    }
    mapping = {
        **_report_only_mapping(through=""),
        "userId": "liang-upstream-user",
    }

    UsageSynchronizer._apply_token_attribution(
        [row], {("user_id", "liang-upstream-user"): [mapping]}
    )

    assert row["principalId"] == "principal-lianghaiqiang"
    assert row["attributionSource"] == "legacy_report_only"
    assert row["billingEligible"] is False


def test_report_only_daily_fallback_stays_visible_but_not_billable() -> None:
    row = {
        "_userId": "liang-upstream-user",
        "organizationId": "org-baic-upstream",
        "teamId": "team-baic-upstream",
        "keyId": "a" * 64,
        "date": "2026-07-30",
    }

    UsageSynchronizer._apply_token_attribution(
        [row], {("key_hash", "a" * 64): [_report_only_mapping(through="")]}
    )

    assert row["principalId"] == "principal-lianghaiqiang"
    assert row["attributionSource"] == "legacy_report_only"
    assert row["billingEligible"] is False


def test_report_only_backfill_filters_by_hash_and_never_bills() -> None:
    key_hash = "a" * 64

    class Client:
        backends = [PRIMARY]

        async def sync_rows_from_logs(self, start, end, backend, *, api_key=None):
            assert (start, end, backend.id, api_key) == (
                "2026-07-29",
                "2026-07-31",
                "primary",
                key_hash,
            )
            row = _log("claude-code-lianghaiqiang", "2026-07-30T01:00:00Z", 10, "1")
            row.update({"date": "2026-07-30", "keyId": key_hash, "requestId": "req-1", "eventTime": "2026-07-30T01:00:00Z"})
            return {"claude-code-lianghaiqiang": [row], "__events__": [dict(row)]}, True

    class Repository:
        completed = None
        failed = None

        async def claim_usage_backfill_window(self, **_kwargs):
            if self.completed:
                return None
            return {
                "id": "backfill-1",
                "leaseToken": "lease-1",
                "backendId": "primary",
                "upstreamKeyHash": key_hash,
                "upstreamKeyId": key_hash,
                "upstreamUserId": "claude-code-lianghaiqiang",
                "upstreamOrganizationId": "org-baic",
                "upstreamTeamId": "team-baic",
                "principalId": "principal-liang",
                "effectiveFrom": "2026-07-29T00:00:00+00:00",
                "effectiveThrough": None,
                "windowFrom": "2026-07-29",
                "windowThrough": "2026-07-31",
            }

        async def complete_usage_backfill_window(self, backfill_id, **kwargs):
            self.completed = (backfill_id, kwargs)

        async def fail_usage_backfill_window(self, backfill_id, error, *, lease_token):
            self.failed = (backfill_id, error, lease_token)

    class Store:
        rows = None
        events = None

        async def upsert_attributed_usage(self, backend_id, rows, *, events):
            self.rows = rows
            self.events = events
            assert backend_id == "primary"
            return len(rows)

    repository = Repository()
    store = Store()
    result = asyncio.run(
        run_usage_backfill_once(Client(), store, repository, max_windows=1)
    )

    assert result == {"completedWindowCount": 1, "rowCount": 1}
    assert repository.failed is None
    assert store.rows[0]["principalId"] == "principal-liang"
    assert store.rows[0]["billingEligible"] is False
    assert store.events[0]["attributionSource"] == "legacy_report_only"


def test_report_only_backfill_rejects_unconfirmed_key_filter() -> None:
    key_hash = "a" * 64

    class Client:
        backends = [PRIMARY]

        async def sync_rows_from_logs(self, *_args, **_kwargs):
            row = _log(
                "claude-code-lianghaiqiang",
                "2026-07-30T01:00:00Z",
                10,
                "1",
            )
            row.update(
                {
                    "date": "2026-07-30",
                    "requestId": "req-1",
                    "eventTime": "2026-07-30T01:00:00Z",
                }
            )
            return {"claude-code-lianghaiqiang": [row], "__events__": [dict(row)]}, True

    class Repository:
        failed = None

        async def claim_usage_backfill_window(self, **_kwargs):
            if self.failed:
                return None
            return {
                "id": "backfill-1",
                "leaseToken": "lease-1",
                "backendId": "primary",
                "upstreamKeyHash": key_hash,
                "upstreamKeyId": key_hash,
                "upstreamUserId": "claude-code-lianghaiqiang",
                "upstreamOrganizationId": "org-baic",
                "upstreamTeamId": "team-baic",
                "principalId": "principal-liang",
                "effectiveFrom": "2026-07-29T00:00:00+00:00",
                "effectiveThrough": None,
                "windowFrom": "2026-07-29",
                "windowThrough": "2026-07-31",
            }

        async def fail_usage_backfill_window(self, backfill_id, error, *, lease_token):
            self.failed = (backfill_id, error, lease_token)

    class Store:
        async def upsert_attributed_usage(self, *_args, **_kwargs):
            raise AssertionError("unconfirmed logs must not be stored")

    repository = Repository()
    result = asyncio.run(
        run_usage_backfill_once(Client(), Store(), repository, max_windows=1)
    )

    assert result == {"completedWindowCount": 0, "rowCount": 0}
    assert "filter was not confirmed" in repository.failed[1]


def test_pending_backfill_runner_stops_when_queue_is_empty(monkeypatch) -> None:
    results = iter(
        [
            {"completedWindowCount": 1, "rowCount": 2},
            {"completedWindowCount": 1, "rowCount": 3},
            {"completedWindowCount": 0, "rowCount": 0},
        ]
    )

    async def once(*_args, **_kwargs):
        return next(results)

    monkeypatch.setattr("backend.usage_sync.run_usage_backfill_once", once)

    assert asyncio.run(run_pending_usage_backfills(object(), object(), object())) == {
        "completedWindowCount": 2,
        "rowCount": 5,
    }


def test_log_sync_prioritizes_claude_cli_tags_over_legacy_cursor_identity(monkeypatch) -> None:
    monkeypatch.setenv("USAGE_TIMEZONE_OFFSET_MINUTES", "-480")

    claude_cli_log = _log("cursor-zhuyida", "2026-07-28T01:00:00+00:00", 700, "7.0", model="claude-opus-5")
    claude_cli_log["metadata"] = {"user_api_key_user_id": "cursor-zhuyida"}
    claude_cli_log["request_tags"] = ["User-Agent: claude-cli", "User-Agent: claude-cli/2.1.220"]
    legacy_cursor_log = _log("cursor-zhuyida", "2026-07-28T02:00:00+00:00", 300, "3.0", model="claude-opus-5")
    legacy_cursor_log["metadata"] = {"user_api_key_user_id": "cursor-zhuyida"}
    legacy_cursor_log["request_tags"] = ["User-Agent: curl/8.0"]
    client = _LogClient([[claude_cli_log, legacy_cursor_log]])

    rows, complete = asyncio.run(client.sync_rows_from_logs("2026-07-28", "2026-07-28", PRIMARY))

    assert complete is True
    by_source = {row["source"]: row for row in rows["cursor-zhuyida"]}
    assert set(by_source) == {"Claude Code", "Cursor"}
    assert by_source["Claude Code"]["model"] == "claude-opus-5"
    assert by_source["Claude Code"]["totalTokens"] == 700
    assert by_source["Claude Code"]["requestCount"] == 1
    assert by_source["Cursor"]["totalTokens"] == 300


def test_truncated_scan_reports_incomplete(monkeypatch) -> None:
    """超过页数上限时必须报告不完整，让调用方退回旧路径而不是写入残缺数据。"""
    monkeypatch.setenv("USAGE_TIMEZONE_OFFSET_MINUTES", "-480")
    monkeypatch.setenv("USAGE_SYNC_LOG_MAX_PAGES", "1")

    client = _LogClient(
        [
            [_log("claude-code-alice", "2026-07-28T01:00:00+00:00", 100, "1.0")],
            [_log("claude-code-alice", "2026-07-28T02:00:00+00:00", 200, "2.0")],
        ]
    )

    _, complete = asyncio.run(client.sync_rows_from_logs("2026-07-28", "2026-07-28", PRIMARY))

    assert complete is False


def test_failure_status_counts_separately(monkeypatch) -> None:
    """额度拦截等失败请求要计入 failureCount，且不贡献 token。"""
    monkeypatch.setenv("USAGE_TIMEZONE_OFFSET_MINUTES", "-480")

    client = _LogClient(
        [
            [
                _log("claude-code-alice", "2026-07-28T01:00:00+00:00", 100, "1.0"),
                _log("claude-code-alice", "2026-07-28T02:00:00+00:00", 0, "0", status="failure"),
            ]
        ]
    )

    rows, _ = asyncio.run(client.sync_rows_from_logs("2026-07-28", "2026-07-28", PRIMARY))
    row = rows["claude-code-alice"][0]

    assert row["successCount"] == 1
    assert row["failureCount"] == 1
    assert row["requestCount"] == 2
    assert row["totalTokens"] == 100


def test_collect_backend_prefers_log_scan(monkeypatch) -> None:
    """同步器应采用日志扫描结果，且不再按账号调 usage_rows。"""
    monkeypatch.setenv("USAGE_TIMEZONE_OFFSET_MINUTES", "-480")

    class FakeClient:
        backends = [PRIMARY]

        def __init__(self) -> None:
            self.usage_rows_calls = 0

        def _admin_user_map(self, _users):
            return {"claude-code-alice": {"name": "Alice", "email": "alice@example.com"}}

        def _is_backend_usage_account(self, _backend, _user_id):
            return True

        def _encode_account_id(self, backend, user_id):
            return user_id

        async def users(self, _backend):
            return [{"user_id": "claude-code-alice", "user_email": "alice@example.com"}]

        async def teams(self, _backend):
            return []

        async def sync_rows_from_logs(self, _start, _end, _backend):
            return (
                {
                    "claude-code-alice": [
                        {
                            "date": "2026-07-28",
                            "source": "Claude Code",
                            "model": "gpt-5.6",
                            "totalTokens": 1500,
                            "spend": 2.0,
                        }
                    ]
                },
                True,
            )

        async def usage_rows(self, *_args, **_kwargs):
            self.usage_rows_calls += 1
            return []

    fake = FakeClient()
    snapshot = asyncio.run(
        UsageSynchronizer(fake, object()).collect_backend(PRIMARY, "2026-07-28", "2026-07-28")
    )

    assert fake.usage_rows_calls == 0, "日志扫描成功时不应再按账号查询"
    assert snapshot.rows[0]["totalTokens"] == 1500
    assert snapshot.rows[0]["_userId"] == "claude-code-alice"
    assert snapshot.rows[0]["employeeEmail"] == "alice@example.com"


def test_collect_backend_falls_back_when_scan_fails(monkeypatch) -> None:
    """日志扫描异常时必须退回原有逐账号聚合，不能让同步整体失败。"""
    monkeypatch.setenv("USAGE_TIMEZONE_OFFSET_MINUTES", "-480")

    class FakeClient:
        backends = [PRIMARY]

        def __init__(self) -> None:
            self.usage_rows_calls = 0

        def _admin_user_map(self, _users):
            return {"claude-code-alice": {"name": "Alice", "email": "alice@example.com"}}

        def _is_backend_usage_account(self, _backend, _user_id):
            return True

        def _encode_account_id(self, backend, user_id):
            return user_id

        async def users(self, _backend):
            return [{"user_id": "claude-code-alice", "user_email": "alice@example.com"}]

        async def teams(self, _backend):
            return []

        async def sync_rows_from_logs(self, _start, _end, _backend):
            raise RuntimeError("upstream down")

        async def usage_rows(self, _user_id, _start, _end, _source):
            self.usage_rows_calls += 1
            return [{"date": "2026-07-28", "source": "Claude Code", "model": "gpt-5.6", "totalTokens": 7}]

    fake = FakeClient()
    snapshot = asyncio.run(
        UsageSynchronizer(fake, object()).collect_backend(PRIMARY, "2026-07-28", "2026-07-28")
    )

    assert fake.usage_rows_calls == 1
    assert snapshot.rows[0]["totalTokens"] == 7


def test_long_window_skips_log_scan(monkeypatch) -> None:
    """初始回填这类长窗口不应触发日志扫描（单日约 3 分钟，90 天会跑数小时）。"""
    monkeypatch.setenv("USAGE_TIMEZONE_OFFSET_MINUTES", "-480")
    monkeypatch.setenv("USAGE_SYNC_LOG_MAX_WINDOW_DAYS", "3")

    class FakeClient:
        backends = [PRIMARY]

        def __init__(self) -> None:
            self.scan_calls = 0
            self.usage_rows_calls = 0

        def _admin_user_map(self, _users):
            return {"claude-code-alice": {"name": "Alice", "email": "alice@example.com"}}

        def _is_backend_usage_account(self, _backend, _user_id):
            return True

        def _encode_account_id(self, backend, user_id):
            return user_id

        async def users(self, _backend):
            return [{"user_id": "claude-code-alice", "user_email": "alice@example.com"}]

        async def teams(self, _backend):
            return []

        async def sync_rows_from_logs(self, _start, _end, _backend):
            self.scan_calls += 1
            return ({}, True)

        async def usage_rows(self, _user_id, _start, _end, _source):
            self.usage_rows_calls += 1
            return [{"date": "2026-07-28", "source": "Claude Code", "model": "gpt-5.6", "totalTokens": 5}]

    fake = FakeClient()
    # 30 天窗口，超过 3 天上限
    asyncio.run(UsageSynchronizer(fake, object()).collect_backend(PRIMARY, "2026-06-29", "2026-07-28"))

    assert fake.scan_calls == 0, "长窗口不应扫描日志"
    assert fake.usage_rows_calls == 1

    # 短窗口仍应扫描
    fake2 = FakeClient()
    asyncio.run(UsageSynchronizer(fake2, object()).collect_backend(PRIMARY, "2026-07-26", "2026-07-28"))
    assert fake2.scan_calls == 1, "3 天窗口应扫描日志"


def test_incomplete_scan_falls_back(monkeypatch) -> None:
    """扫描被截断时也要退回旧路径，避免写入残缺快照。"""
    monkeypatch.setenv("USAGE_TIMEZONE_OFFSET_MINUTES", "-480")

    class FakeClient:
        backends = [PRIMARY]

        def __init__(self) -> None:
            self.usage_rows_calls = 0

        def _admin_user_map(self, _users):
            return {"claude-code-alice": {"name": "Alice", "email": "alice@example.com"}}

        def _is_backend_usage_account(self, _backend, _user_id):
            return True

        def _encode_account_id(self, backend, user_id):
            return user_id

        async def users(self, _backend):
            return [{"user_id": "claude-code-alice", "user_email": "alice@example.com"}]

        async def teams(self, _backend):
            return []

        async def sync_rows_from_logs(self, _start, _end, _backend):
            return ({"claude-code-alice": [{"date": "2026-07-28", "totalTokens": 1}]}, False)

        async def usage_rows(self, _user_id, _start, _end, _source):
            self.usage_rows_calls += 1
            return [{"date": "2026-07-28", "source": "Claude Code", "model": "gpt-5.6", "totalTokens": 9}]

    fake = FakeClient()
    snapshot = asyncio.run(
        UsageSynchronizer(fake, object()).collect_backend(PRIMARY, "2026-07-28", "2026-07-28")
    )

    assert fake.usage_rows_calls == 1
    assert snapshot.rows[0]["totalTokens"] == 9
