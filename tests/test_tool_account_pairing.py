"""同一个人的 cursor / claude-code 双账号之间互补身份。

工具账号按 ``cursor-<邮箱前缀>`` / ``claude-code-<邮箱前缀>`` 建号，上游只在其中
一个账号上填了姓名和邮箱，另一个是空白的。空白那个在员工榜和团队看板上只显示账号
编号、标成未绑定邮箱。这里覆盖按邮箱前缀把两个账号认成同一个人的行为。
"""

import asyncio
from typing import Any

from backend.litellm_client import LiteLLMBackend, tool_account_email_prefix
from backend.usage_sync import UsageSynchronizer


PRIMARY = LiteLLMBackend(id="primary", label="Primary", base_url="https://primary.test", admin_key="k")
HER = LiteLLMBackend(id="her", label="Her", base_url="https://her.test", admin_key="k", source="Her")


class FakeClient:
    def __init__(
        self,
        users: dict[str, list[dict[str, Any]]],
        profiles: dict[str, dict[str, Any]] | None = None,
        teams: list[dict[str, Any]] | None = None,
        log_rows: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.backends = [PRIMARY, HER]
        self._users = users
        self._profiles = profiles or {}
        self._teams = teams or []
        self._log_rows = log_rows

    async def users(self, backend: LiteLLMBackend | None = None) -> list[dict[str, Any]]:
        return self._users.get(backend.id if backend else "primary", [])

    async def her_account_index(self, backend: LiteLLMBackend) -> dict[str, Any]:
        return {"emails": {}, "names": {}, "profiles": self._profiles}

    async def usage_rows(self, user_id: str, start_date: str, end_date: str, source: str | None) -> list[dict[str, Any]]:
        return [
            {
                "date": start_date,
                "source": "Claude Code",
                "model": "claude-opus-5",
                "promptTokens": 10,
                "completionTokens": 5,
                "totalTokens": 15,
                "requestCount": 1,
                "successCount": 1,
                "failureCount": 0,
                "spend": 0.01,
            }
        ]

    async def teams(self, backend: LiteLLMBackend | None = None, include_details: bool = True) -> list[dict[str, Any]]:
        return self._teams if (backend is None or backend.id == "primary") else []

    def _admin_user_map(self, users: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {}

    def _is_backend_usage_account(self, backend: LiteLLMBackend, user_id: Any) -> bool:
        return bool(str(user_id or "").strip())

    def _encode_account_id(self, backend: LiteLLMBackend, user_id: str) -> str:
        return user_id

    async def sync_rows_from_logs(
        self, start_date: str, end_date: str, backend: LiteLLMBackend
    ) -> tuple[dict[str, list[dict[str, Any]]], bool]:
        if self._log_rows is None:
            raise RuntimeError("日志扫描未启用")
        return {user_id: list(rows) for user_id, rows in self._log_rows.items()}, True


def make_synchronizer(
    users: dict[str, list[dict[str, Any]]],
    profiles: dict[str, dict[str, Any]] | None = None,
    teams: list[dict[str, Any]] | None = None,
    log_rows: dict[str, list[dict[str, Any]]] | None = None,
) -> UsageSynchronizer:
    synchronizer = object.__new__(UsageSynchronizer)
    synchronizer.client = FakeClient(users, profiles, teams, log_rows)
    synchronizer.store = None
    synchronizer.organization_repository = None

    async def no_memberships(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def no_token_map(backend_id: str) -> dict[str, Any]:
        return {}

    synchronizer._token_attribution_map = no_token_map  # type: ignore[assignment]
    synchronizer._no_memberships = no_memberships  # type: ignore[attr-defined]
    return synchronizer


def collect_rows(synchronizer: UsageSynchronizer, backend: LiteLLMBackend) -> list[dict[str, Any]]:
    synchronizer.collect_memberships = synchronizer._no_memberships  # type: ignore[assignment]
    snapshot = asyncio.run(synchronizer.collect_backend(backend, "2026-08-01", "2026-08-01"))
    return snapshot.rows


def test_email_prefix_is_only_taken_from_tool_account_ids() -> None:
    assert tool_account_email_prefix("cursor-luoyun") == "luoyun"
    assert tool_account_email_prefix("claude-code-4165e5ef") == "4165e5ef"
    assert tool_account_email_prefix("carher-41") == ""
    assert tool_account_email_prefix("default_user_id") == ""
    assert tool_account_email_prefix(None) == ""


def test_blank_tool_account_borrows_identity_from_its_pair() -> None:
    synchronizer = make_synchronizer(
        users={
            "primary": [
                {"user_id": "cursor-4165e5ef", "user_alias": "陈光", "user_email": "4165e5ef@auto-link.com.cn"},
                {"user_id": "claude-code-4165e5ef", "user_alias": None, "user_email": None},
            ],
            "her": [],
        }
    )

    rows = {row["_userId"]: row for row in collect_rows(synchronizer, PRIMARY)}

    assert rows["claude-code-4165e5ef"]["employeeName"] == "陈光"
    assert rows["claude-code-4165e5ef"]["employeeEmail"] == "4165e5ef@auto-link.com.cn"
    assert rows["claude-code-4165e5ef"]["emailSource"] == "paired_tool_account"


def test_pinyin_prefix_pairs_are_merged_too() -> None:
    synchronizer = make_synchronizer(
        users={
            "primary": [
                {"user_id": "claude-code-luoyun", "user_alias": "骆赟", "user_email": "luoyun@auto-link.com.cn"},
                {"user_id": "cursor-luoyun", "user_alias": None, "user_email": None},
            ],
            "her": [],
        }
    )

    rows = {row["_userId"]: row for row in collect_rows(synchronizer, PRIMARY)}

    assert rows["cursor-luoyun"]["employeeName"] == "骆赟"
    assert rows["cursor-luoyun"]["employeeEmail"] == "luoyun@auto-link.com.cn"


def test_same_prefix_on_two_domains_is_not_merged() -> None:
    """同一个邮箱前缀落在两个域名上时无法确定是同一个人，不做归并。"""

    synchronizer = make_synchronizer(
        users={
            "primary": [
                {"user_id": "cursor-liwei", "user_alias": "李伟", "user_email": "liwei@auto-link.com.cn"},
                {"user_id": "claude-code-liwei2", "user_alias": "李威", "user_email": "liwei@carher.net"},
                {"user_id": "claude-code-liwei", "user_alias": None, "user_email": None},
            ],
            "her": [],
        }
    )

    rows = {row["_userId"]: row for row in collect_rows(synchronizer, PRIMARY)}

    assert rows["claude-code-liwei"]["employeeName"] == "claude-code-liwei"
    assert rows["claude-code-liwei"]["employeeEmail"] == ""


def test_upstream_identity_is_never_overwritten_by_its_pair() -> None:
    synchronizer = make_synchronizer(
        users={
            "primary": [
                {"user_id": "cursor-sunqian", "user_alias": "孙倩", "user_email": "sunqian@auto-link.com.cn"},
                {"user_id": "claude-code-sunqian", "user_alias": "孙倩本人", "user_email": "sq@auto-link.com.cn"},
            ],
            "her": [],
        }
    )

    rows = {row["_userId"]: row for row in collect_rows(synchronizer, PRIMARY)}

    assert rows["claude-code-sunqian"]["employeeName"] == "孙倩本人"
    assert rows["claude-code-sunqian"]["employeeEmail"] == "sq@auto-link.com.cn"
    assert rows["claude-code-sunqian"]["emailSource"] == "upstream"


def test_team_member_list_no_longer_shows_bare_account_ids() -> None:
    """团队看板读的是成员表，成员清单缺 alias 时也要补上姓名。"""

    users = [
        {"user_id": "cursor-8eaa3e26", "user_alias": "杜柃雲", "user_email": "8eaa3e26@auto-link.com.cn"},
        {"user_id": "claude-code-8eaa3e26", "user_alias": None, "user_email": None},
    ]
    teams = [
        {
            "team_id": "team-1",
            "team_alias": "上海车联",
            "members_with_roles": [
                {"user_id": "claude-code-8eaa3e26", "role": "user"},
                {"user_id": "cursor-8eaa3e26", "user_alias": "杜柃雲", "user_email": "8eaa3e26@auto-link.com.cn", "role": "user"},
            ],
        }
    ]
    synchronizer = make_synchronizer(users={"primary": users, "her": []}, teams=teams)
    directory = asyncio.run(synchronizer._identity_directory())

    memberships = asyncio.run(
        synchronizer.collect_memberships(PRIMARY, users, "2026-08-01", "2026-08-01", {}, directory)
    )

    by_user_id = {item["userId"]: item for item in memberships}
    assert by_user_id["claude-code-8eaa3e26"]["employeeName"] == "杜柃雲"
    assert by_user_id["claude-code-8eaa3e26"]["employeeEmail"] == "8eaa3e26@auto-link.com.cn"
    assert by_user_id["claude-code-8eaa3e26"]["teamName"] == "上海车联"


def test_unassigned_members_are_filled_as_well() -> None:
    users = [
        {"user_id": "cursor-zhouzhian", "user_alias": "周志安", "user_email": "zhouzhian@auto-link.com.cn"},
        {"user_id": "claude-code-zhouzhian", "user_alias": None, "user_email": None},
    ]
    synchronizer = make_synchronizer(users={"primary": users, "her": []}, teams=[])
    directory = asyncio.run(synchronizer._identity_directory())

    memberships = asyncio.run(
        synchronizer.collect_memberships(PRIMARY, users, "2026-08-01", "2026-08-01", {}, directory)
    )

    unassigned = {item["userId"]: item for item in memberships if item["teamId"] == "unassigned"}
    assert unassigned["claude-code-zhouzhian"]["employeeName"] == "周志安"
    assert unassigned["claude-code-zhouzhian"]["employeeEmail"] == "zhouzhian@auto-link.com.cn"


def test_suffix_pairs_are_matched_when_email_prefix_differs() -> None:
    """少数工具账号的邮箱前缀和编号后缀不一致，只能按编号后缀配对。"""

    synchronizer = make_synchronizer(
        users={
            "primary": [
                {"user_id": "claude-code-t1v", "user_alias": "白羽", "user_email": "baiyu@auto-link.com.cn"},
                {"user_id": "cursor-t1v", "user_alias": None, "user_email": None},
            ],
            "her": [],
        }
    )

    rows = {row["_userId"]: row for row in collect_rows(synchronizer, PRIMARY)}

    assert rows["cursor-t1v"]["employeeName"] == "白羽"
    assert rows["cursor-t1v"]["employeeEmail"] == "baiyu@auto-link.com.cn"
    assert rows["cursor-t1v"]["emailSource"] == "paired_tool_account"


def test_same_suffix_with_two_names_is_not_merged() -> None:
    synchronizer = make_synchronizer(
        users={
            "primary": [
                {"user_id": "claude-code-x9", "user_alias": "甲", "user_email": None},
                {"user_id": "cursor-x9", "user_alias": "乙", "user_email": None},
            ],
            "her": [],
        }
    )
    directory = asyncio.run(synchronizer._identity_directory())

    assert "x9" not in (directory.get("byToolSuffix") or {})


def test_accounts_only_present_in_request_logs_are_named_too() -> None:
    """全员看板的行来自日志扫描，账号清单里没有的编号同样要补齐身份。"""

    log_row = {
        "date": "2026-08-01",
        "source": "Codex",
        "model": "gpt-5",
        "promptTokens": 10,
        "completionTokens": 5,
        "totalTokens": 15,
        "requestCount": 1,
        "successCount": 1,
        "failureCount": 0,
        "spend": 0.01,
    }
    synchronizer = make_synchronizer(
        users={
            "primary": [
                {"user_id": "claude-code-luoyun", "user_alias": "骆赟", "user_email": "luoyun@auto-link.com.cn"},
            ],
            "her": [],
        },
        # `cursor-luoyun` 在上游账号清单里不存在，只在请求日志里出现。
        log_rows={"claude-code-luoyun": [log_row], "cursor-luoyun": [log_row], "unattributed": [log_row]},
    )
    synchronizer.collect_memberships = synchronizer._no_memberships  # type: ignore[assignment]
    snapshot = asyncio.run(synchronizer.collect_backend(PRIMARY, "2026-08-01", "2026-08-01"))

    rows = {row["_userId"]: row for row in snapshot.rows}
    assert rows["cursor-luoyun"]["employeeName"] == "骆赟"
    assert rows["cursor-luoyun"]["employeeEmail"] == "luoyun@auto-link.com.cn"
    assert rows["cursor-luoyun"]["emailSource"] == "paired_tool_account"
    assert rows["unattributed"]["employeeName"] == "未归属请求"
    assert rows["unattributed"]["employeeEmail"] == ""
