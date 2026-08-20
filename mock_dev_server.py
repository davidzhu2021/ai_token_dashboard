"""本地 mock 服务：造一个「同时带多个团队」的负责人，用来肉眼验收令牌管理页。

只在本机跑，不连任何上游、不连数据库：usage_store / client / key_vault 全部换成
内存假实现。撤销与删除会真的改内存里的状态，所以可以完整走一遍两步流程。

    python mock_dev_server.py

然后打开 http://127.0.0.1:8000 ，用开发登录输入 leader@auto-link.com.cn 进入。
"""

from __future__ import annotations

import copy
import os
from typing import Any

# 必须在导入 backend.main 之前落好环境变量。
os.environ.update(
    {
        "DEV_LOGIN_ENABLED": "true",
        "APP_BASE_URL": "http://127.0.0.1:8000",
        "SESSION_SECRET": "mock-session-secret-for-local-preview",
        "COMPANY_EMAIL_DOMAINS": "auto-link.com.cn",
        "LITELLM_BASE_URL": "http://127.0.0.1:9/mock",
        "LITELLM_MASTER_KEY": "sk-mock-not-used",
        "USAGE_DB_ENABLED": "false",
        "USAGE_SYNC_ENABLED": "false",
        # 只当读端：mock 的 store 没有写入侧方法，后台同步循环不能起。
        "USAGE_SYNC_ROLE": "reader",
        "SNAPSHOT_READER_ENABLED": "false",
        "ORGANIZATION_DEMO_ENABLED": "false",
        "ORGANIZATION_REAL_ENABLED": "false",
        "AUTH_DB_ENABLED": "false",
        "BILLING_ENABLED": "false",
        "PASSWORD_LOGIN_ENABLED": "false",
    }
)

import uvicorn  # noqa: E402

from backend import main  # noqa: E402


LEADER_EMAIL = "leader@auto-link.com.cn"

# 三个团队，负责人同时带其中两个：用来看多团队下拉框；第三个团队用来验证越权。
TEAMS = [
    {
        "id": "team-model-platform",
        "name": "模型平台组",
        "memberCount": 4,
        "backend": "primary",
        "teamScopes": [{"id": "team-model-platform", "name": "模型平台组", "memberCount": 4, "backend": "primary"}],
    },
    {
        "id": "team-app-integration",
        "name": "应用集成组",
        "memberCount": 3,
        "backend": "primary",
        "teamScopes": [{"id": "team-app-integration", "name": "应用集成组", "memberCount": 3, "backend": "primary"}],
    },
]

MEMBERS: dict[str, list[dict[str, Any]]] = {
    "team-model-platform": [
        ("leader-user", LEADER_EMAIL, "周立成", "admin"),
        ("u-wangxin", "wang.xin@auto-link.com.cn", "王欣", "user"),
        ("u-lifang", "li.fang@auto-link.com.cn", "李芳", "user"),
        ("u-chenhao", "chen.hao@auto-link.com.cn", "陈皓", "user"),
    ],
    "team-app-integration": [
        ("leader-user", LEADER_EMAIL, "周立成", "admin"),
        ("u-zhaomin", "zhao.min@auto-link.com.cn", "赵敏", "admin"),
        ("u-sunqi", "sun.qi@auto-link.com.cn", "孙琦", "user"),
        ("u-zhoulei", "zhou.lei@auto-link.com.cn", "周磊", "user"),
    ],
}


def member_rows(team_id: str) -> list[dict[str, Any]]:
    return [
        {
            "backendId": "primary",
            "userId": user_id,
            "accountId": f"primary:{user_id}",
            "employeeEmail": email,
            "employeeName": name,
            "teamRole": role,
        }
        for user_id, email, name, role in MEMBERS[team_id]
    ]


def key(key_id: str, name: str, status: str, created: str, last_used: str) -> dict[str, Any]:
    return {
        "id": key_id,
        "name": name,
        "purpose": "",
        "masked": f"sk-...{key_id[-4:].upper()}",
        "models": ["claude-sonnet-4-5", "gpt-5.2"],
        "createdAt": created,
        "lastUsed": last_used,
        "expiresAt": "永不过期",
        "monthTokens": 128_000,
        "spend": 3.42,
        "status": status,
    }


# 按上游账号存放密钥，撤销/删除直接改这里，页面刷新后状态持久。
KEYS_BY_ACCOUNT: dict[str, list[dict[str, Any]]] = {
    "primary:leader-user": [
        key("leaderkey0001", "我的 Codex 密钥", "正常", "2026-05-12 09:20", "2026-08-05 08:11"),
        key("leaderkey0002", "我的 Claude Code 密钥", "正常", "2026-06-02 14:05", "2026-08-04 21:47"),
    ],
    "primary:u-wangxin": [
        key("wangxinkey001", "王欣 Codex", "正常", "2026-05-20 10:31", "2026-08-05 09:02"),
        key("wangxinkey002", "王欣 旧密钥", "已过期", "2026-01-08 16:44", "2026-03-19 11:23"),
    ],
    "primary:u-lifang": [
        key("lifangkey0001", "李芳 Claude Code", "正常", "2026-06-11 08:57", "2026-08-05 07:35"),
    ],
    "primary:u-chenhao": [
        key("chenhaokey001", "陈皓 集成测试", "已禁用", "2026-04-02 13:10", "2026-07-28 19:06"),
    ],
    "primary:u-zhaomin": [
        key("zhaominkey001", "赵敏 Codex", "正常", "2026-05-30 11:12", "2026-08-05 10:20"),
    ],
    "primary:u-sunqi": [
        key("sunqikey00001", "孙琦 Codex", "正常", "2026-07-01 09:44", "2026-08-05 09:58"),
    ],
    "primary:u-zhoulei": [
        key("zhouleikey001", "周磊 自动化脚本", "正常", "2026-03-17 15:26", "2026-08-01 12:03"),
    ],
}


class MockStore:
    """只实现令牌管理页真正会用到的读取方法，其余一律返回空。"""

    async def connect(self) -> None:
        return None

    async def snapshot_state(self) -> dict[str, Any]:
        return {"revision": "mock-revision-1"}

    async def team_leader_scope(
        self, email: str, user_ids: list[str], backend_ids: list[str]
    ) -> dict[str, Any]:
        if email.strip().lower() != LEADER_EMAIL:
            return {"isTeamLeader": False, "teamBoardStatus": "none", "team": None, "leaderTeams": []}
        teams = copy.deepcopy(TEAMS)
        return {
            "isTeamLeader": True,
            "teamBoardStatus": "multiple",
            "team": None,
            "leaderTeams": teams,
        }

    async def team_member_directory(self, team_scopes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for scope in team_scopes:
            rows.extend(member_rows(str(scope.get("id"))))
        return rows

    async def covered_backend_ids(self, start_date: str, end_date: str, backend_ids: list[str]) -> list[str]:
        return list(backend_ids)

    async def has_complete_coverage(self, start_date: str, end_date: str, backend_ids: list[str]) -> bool:
        return True

    async def latest_sync_at(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def personal_rows(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"rows": [], "lastSyncedAt": None}

    async def team_rows(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def team_member_rows(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def model_usage_counts(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def health(self) -> dict[str, Any]:
        return {"status": "ok"}


class MockClient:
    def _account_keys(self, account_id: str) -> list[dict[str, Any]]:
        return KEYS_BY_ACCOUNT.setdefault(account_id, [])

    async def resolve_user(self, email: str, name: str | None = None) -> dict[str, Any]:
        user_id = "leader-user" if email.strip().lower() == LEADER_EMAIL else email.split("@", 1)[0]
        return {
            "user_id": user_id,
            "user_email": email,
            "user_alias": name or email,
            "matched_user_ids": [user_id],
            "matched_accounts": [{"backend": "primary", "user_id": user_id}],
        }

    async def keys_for_user_ids(self, user_ids: list[str], refresh: bool = False) -> list[dict[str, Any]]:
        keys: list[dict[str, Any]] = []
        for account_id in user_ids:
            normalized = account_id if ":" in account_id else f"primary:{account_id}"
            backend_id, raw_user_id = normalized.split(":", 1)
            for item in self._account_keys(normalized):
                keys.append({**copy.deepcopy(item), "_backendId": backend_id, "_userId": raw_user_id, "_rotation": {}})
        return keys

    async def available_key_models(self, user_id: str) -> tuple[list[str], bool]:
        return ["claude-sonnet-4-5", "gpt-5.2"], False

    async def usage_rows_for_user_ids(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def block_key(self, key_id: str, user_id: str, changed_by: str) -> dict[str, str]:
        for item in self._account_keys(user_id):
            if item["id"] == key_id:
                item["status"] = "已禁用"
                print(f"[mock] blocked key={key_id} account={user_id} by={changed_by}")
                return {"id": key_id}
        raise main.HTTPException(status_code=403, detail="不能停用不属于该成员的访问密钥")

    async def delete_key(self, key_id: str, user_id: str, changed_by: str) -> dict[str, str]:
        keys = self._account_keys(user_id)
        remaining = [item for item in keys if item["id"] != key_id]
        if len(remaining) == len(keys):
            raise main.HTTPException(status_code=403, detail="不能删除不属于该成员的访问密钥")
        KEYS_BY_ACCOUNT[user_id] = remaining
        print(f"[mock] deleted key={key_id} account={user_id} by={changed_by}")
        return {"id": key_id}


class MockVault:
    def has(self, backend_id: str, user_id: str, key_id: str) -> bool:
        return False

    def pending_rotations(self, backend_id: str, user_id: str) -> list[dict[str, Any]]:
        return []

    def delete(self, backend_id: str, user_id: str, key_id: str) -> None:
        print(f"[mock] vault delete backend={backend_id} user={user_id} key={key_id}")


_store = MockStore()
_client = MockClient()
_vault = MockVault()

main.usage_store = lambda: _store
main.client = lambda: _client
main.key_vault = lambda: _vault
main.usage_backend_ids = lambda: ["primary"]

if __name__ == "__main__":
    print("Mock 已就绪：http://127.0.0.1:8000")
    print(f"开发登录邮箱：{LEADER_EMAIL}（模型平台组 + 应用集成组 两个团队的负责人）")
    uvicorn.run(main.app, host="127.0.0.1", port=8000, log_level="warning")
