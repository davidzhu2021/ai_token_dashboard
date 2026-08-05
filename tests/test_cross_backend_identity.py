"""两套上游共用一份账号编号时的身份补齐。

主站上的 ``carher-*`` 账号既没有姓名也没有邮箱，同一批编号在另一套上游却有完整的
姓名和部门；反过来，另一套上游多数账号没有邮箱，而主站目录里有"中文姓名 -> 邮箱"
的对应关系。这里覆盖同步时把两边合成一份目录、并把按姓名推断出的邮箱标注出来的行为。
"""

import asyncio
from typing import Any

from backend.litellm_client import LiteLLMBackend
from backend.usage_store import bind_status
from backend.usage_sync import UsageSynchronizer


PRIMARY = LiteLLMBackend(id="primary", label="Primary", base_url="https://primary.test", admin_key="k")
HER = LiteLLMBackend(id="her", label="Her", base_url="https://her.test", admin_key="k", source="Her")


class FakeClient:
    def __init__(
        self,
        users: dict[str, list[dict[str, Any]]],
        profiles: dict[str, dict[str, Any]],
        teams: list[dict[str, Any]] | None = None,
    ) -> None:
        self.backends = [PRIMARY, HER]
        self._users = users
        self._profiles = profiles
        self._teams = teams or []

    async def users(self, backend: LiteLLMBackend | None = None) -> list[dict[str, Any]]:
        return self._users.get(backend.id if backend else "primary", [])

    async def her_account_index(self, backend: LiteLLMBackend) -> dict[str, Any]:
        return {"emails": {}, "names": {}, "profiles": self._profiles}

    async def usage_rows(self, user_id: str, start_date: str, end_date: str, source: str | None) -> list[dict[str, Any]]:
        return [
            {
                "date": start_date,
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
        ]

    async def teams(self, backend: LiteLLMBackend | None = None, include_details: bool = True) -> list[dict[str, Any]]:
        return self._teams if (backend is None or backend.id == "her") else []

    def _admin_user_map(self, users: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {}

    def _is_backend_usage_account(self, backend: LiteLLMBackend, user_id: Any) -> bool:
        return bool(str(user_id or "").strip())

    def _encode_account_id(self, backend: LiteLLMBackend, user_id: str) -> str:
        return user_id


def make_synchronizer(
    users: dict[str, list[dict[str, Any]]],
    profiles: dict[str, dict[str, Any]],
    teams: list[dict[str, Any]] | None = None,
) -> UsageSynchronizer:
    synchronizer = object.__new__(UsageSynchronizer)
    synchronizer.client = FakeClient(users, profiles, teams)
    synchronizer.store = None
    synchronizer.organization_repository = None

    async def no_memberships(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def no_token_map(backend_id: str) -> dict[str, Any]:
        return {}

    synchronizer.collect_memberships = no_memberships  # type: ignore[assignment]
    synchronizer._token_attribution_map = no_token_map  # type: ignore[assignment]
    return synchronizer


def collect(synchronizer: UsageSynchronizer, backend: LiteLLMBackend) -> list[dict[str, Any]]:
    snapshot = asyncio.run(synchronizer.collect_backend(backend, "2026-08-01", "2026-08-01"))
    return snapshot.rows


def test_primary_account_borrows_name_and_department_from_the_other_directory() -> None:
    synchronizer = make_synchronizer(
        users={
            "primary": [{"user_id": "carher-14", "user_alias": None, "user_email": None}],
            "her": [],
        },
        profiles={
            "carher-14": {
                "email": "wangfang@carher.net",
                "name": "王芳",
                "department": "研发中心",
                "emailSource": "upstream",
            }
        },
    )

    rows = collect(synchronizer, PRIMARY)

    assert rows
    assert rows[0]["employeeName"] == "王芳"
    assert rows[0]["employeeEmail"] == "wangfang@carher.net"
    assert rows[0]["emailSource"] == "upstream"


def test_missing_email_is_inferred_from_the_primary_directory_and_flagged() -> None:
    synchronizer = make_synchronizer(
        users={
            "primary": [
                {"user_id": "u-1", "user_alias": "李雷", "user_email": "lilei@auto-link.com.cn"},
            ],
            "her": [{"user_id": "carher-20", "user_alias": "李雷", "user_email": None}],
        },
        profiles={"carher-20": {"email": "", "name": "李雷", "department": "销售部", "emailSource": ""}},
    )

    rows = collect(synchronizer, HER)

    assert rows
    assert rows[0]["employeeEmail"] == "lilei@auto-link.com.cn"
    assert rows[0]["emailSource"] == "inferred_primary_directory"
    assert bind_status(rows[0]["employeeEmail"], rows[0]["emailSource"]) == "邮箱推断"


def test_ambiguous_name_is_not_inferred() -> None:
    """同一个姓名在主站目录里对应两个邮箱时不做推断。"""

    synchronizer = make_synchronizer(
        users={
            "primary": [
                {"user_id": "u-1", "user_alias": "张伟", "user_email": "zhangwei@auto-link.com.cn"},
                {"user_id": "u-2", "user_alias": "张伟", "user_email": "zhangwei2@auto-link.com.cn"},
            ],
            "her": [{"user_id": "carher-30", "user_alias": "张伟", "user_email": None}],
        },
        profiles={"carher-30": {"email": "", "name": "张伟", "department": "", "emailSource": ""}},
    )

    rows = collect(synchronizer, HER)

    assert rows
    assert rows[0]["employeeEmail"] == ""
    assert rows[0]["emailSource"] == ""
    assert bind_status(rows[0]["employeeEmail"], rows[0]["emailSource"]) == "未绑定邮箱"


def test_upstream_email_is_never_overwritten_by_inference() -> None:
    synchronizer = make_synchronizer(
        users={
            "primary": [{"user_id": "u-1", "user_alias": "孙倩", "user_email": "other@auto-link.com.cn"}],
            "her": [{"user_id": "carher-40", "user_alias": "孙倩", "user_email": "sunqian@carher.net"}],
        },
        profiles={"carher-40": {"email": "sunqian@carher.net", "name": "孙倩", "department": "", "emailSource": "upstream"}},
    )

    rows = collect(synchronizer, HER)

    assert rows[0]["employeeEmail"] == "sunqian@carher.net"
    assert rows[0]["emailSource"] == "upstream"
    assert bind_status(rows[0]["employeeEmail"], rows[0]["emailSource"]) == "已绑定邮箱"


def test_snapshot_carries_identities_for_historical_refresh() -> None:
    synchronizer = make_synchronizer(
        users={
            "primary": [{"user_id": "carher-14"}],
            "her": [],
        },
        profiles={"carher-14": {"email": "wangfang@carher.net", "name": "王芳", "department": "", "emailSource": "upstream"}},
    )

    snapshot = asyncio.run(synchronizer.collect_backend(PRIMARY, "2026-08-01", "2026-08-01"))

    assert snapshot.identities == [
        {
            "userId": "carher-14",
            "name": "王芳",
            "email": "wangfang@carher.net",
            "emailSource": "upstream",
        }
    ]


def test_team_membership_fills_names_from_the_shared_directory() -> None:
    """成员清单里只有账号编号时，团队看板也要用档案补出姓名。

    用量表在 ``collect_backend`` 里另外合并过一次账号档案，成员表没有这一步，
    补齐逻辑必须对两套后端都生效，否则团队看板上会继续只剩编号。
    """

    profiles = {
        "carher-224": {"email": "", "name": "潘晓", "department": "AI技术院", "emailSource": ""},
        "carher-236": {
            "email": "liuguoxian@auto-link.com.cn",
            "name": "刘国现",
            "department": "AI Infra部",
            "emailSource": "enterprise_email",
        },
    }
    teams = [
        {
            "team_id": "team-ai",
            "team_alias": "AI技术院",
            "members_with_roles": [
                {"user_id": "carher-224", "role": "user"},
                {"user_id": "carher-236", "role": "user"},
            ],
        }
    ]
    synchronizer = object.__new__(UsageSynchronizer)
    synchronizer.client = FakeClient({"primary": [], "her": []}, profiles, teams)
    directory = asyncio.run(synchronizer._identity_directory())

    memberships = asyncio.run(
        synchronizer.collect_memberships(HER, [], "2026-08-01", "2026-08-01", {}, directory)
    )

    by_user_id = {item["userId"]: item for item in memberships}
    assert by_user_id["carher-224"]["employeeName"] == "潘晓"
    assert by_user_id["carher-224"]["employeeEmail"] == ""
    assert by_user_id["carher-236"]["employeeName"] == "刘国现"
    assert by_user_id["carher-236"]["employeeEmail"] == "liuguoxian@auto-link.com.cn"
