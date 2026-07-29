"""同步按北京时间日界归日的回归测试。

上游 /user/daily/activity 的 date 取自 startTime 的 UTC 日期，会把北京时间
00:00-08:00 的用量归到前一天。这里覆盖改用原始日志归日后的行为。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.litellm_client import LiteLLMBackend, LiteLLMClient
from backend.usage_sync import UsageSynchronizer


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
        self.requests: list[dict] = []

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
