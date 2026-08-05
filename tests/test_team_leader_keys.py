"""团队负责人管理成员密钥的权限与两步处置流程。"""

import asyncio
import base64
import json
import os
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from backend import main
from backend.auth import SESSION_USER_KEY


LEADER_EMAIL = "leader@auto-link.com.cn"

TEAM = {
    "id": "team-a",
    "name": "Team A",
    "memberCount": 3,
    "backend": "primary",
    "teamScopes": [{"id": "team-a", "name": "Team A", "memberCount": 3, "backend": "primary"}],
}
OTHER_TEAM = {
    "id": "team-b",
    "name": "Team B",
    "memberCount": 2,
    "backend": "primary",
    "teamScopes": [{"id": "team-b", "name": "Team B", "memberCount": 2, "backend": "primary"}],
}

MEMBERS = [
    {
        "backendId": "primary",
        "userId": "leader-user",
        "accountId": "primary:leader-user",
        "employeeEmail": LEADER_EMAIL,
        "employeeName": "Leader",
        "teamRole": "admin",
    },
    {
        "backendId": "primary",
        "userId": "second-leader",
        "accountId": "primary:second-leader",
        "employeeEmail": "co-leader@auto-link.com.cn",
        "employeeName": "Co Leader",
        "teamRole": "admin",
    },
    {
        "backendId": "primary",
        "userId": "alice",
        "accountId": "primary:alice",
        "employeeEmail": "alice@auto-link.com.cn",
        "employeeName": "Alice",
        "teamRole": "user",
    },
    {
        "backendId": "primary",
        "userId": "bob",
        "accountId": "primary:bob",
        "employeeEmail": "bob@auto-link.com.cn",
        "employeeName": "Bob",
        "teamRole": "user",
    },
]


def upstream_key(key_id: str, user_id: str, status: str, name: str) -> dict[str, Any]:
    return {
        "_backendId": "primary",
        "_userId": user_id,
        "_rotation": {"key_alias": f"alias-{key_id}"},
        "id": key_id,
        "keyType": "Claude Code" if user_id == "alice" else "Codex",
        "name": name,
        "purpose": "",
        "masked": "sk-...ABCD",
        "models": ["gpt-5"],
        "createdAt": "2026-07-01 09:00",
        "lastUsed": "2026-07-20 10:00",
        "expiresAt": "永不过期",
        "monthTokens": 0,
        "spend": 0.0,
        "status": status,
    }


KEYS = {
    "primary:leader-user": [upstream_key("key-leader", "leader-user", "正常", "负责人密钥")],
    "primary:second-leader": [upstream_key("key-co-leader", "second-leader", "正常", "并列负责人密钥")],
    "primary:alice": [upstream_key("key-alice", "alice", "正常", "Alice 的密钥")],
    "primary:bob": [upstream_key("key-bob-blocked", "bob", "已禁用", "Bob 的旧密钥")],
}


class FakeStore:
    def __init__(self, leader_teams: list[dict[str, Any]]) -> None:
        self.leader_teams = leader_teams
        self.directory_calls: list[list[dict[str, Any]]] = []

    async def connect(self) -> None:
        return None

    async def snapshot_state(self) -> dict[str, Any]:
        return {"revision": "rev-1"}

    async def team_leader_scope(self, email: str, user_ids: list[str], backend_ids: list[str]) -> dict[str, Any]:
        if not self.leader_teams:
            return {"isTeamLeader": False, "teamBoardStatus": "none", "team": None, "leaderTeams": []}
        return {
            "isTeamLeader": True,
            "teamBoardStatus": "single" if len(self.leader_teams) == 1 else "multiple",
            "team": self.leader_teams[0] if len(self.leader_teams) == 1 else None,
            "leaderTeams": self.leader_teams,
        }

    async def team_member_directory(self, team_scopes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.directory_calls.append(team_scopes)
        if team_scopes and team_scopes[0].get("id") == "team-b":
            return []
        return [dict(item) for item in MEMBERS]


class FakeClient:
    def __init__(self) -> None:
        self.blocked: list[tuple[str, str, str]] = []
        self.deleted: list[tuple[str, str, str]] = []
        self.list_calls: list[tuple[list[str], bool]] = []

    async def keys_for_user_ids(self, user_ids: list[str], refresh: bool = False) -> list[dict[str, Any]]:
        self.list_calls.append((list(user_ids), refresh))
        keys: list[dict[str, Any]] = []
        for user_id in user_ids:
            keys.extend(dict(item) for item in KEYS.get(user_id, []))
        return keys

    async def block_key(self, key_id: str, user_id: str, changed_by: str) -> dict[str, str]:
        self.blocked.append((key_id, user_id, changed_by))
        return {"id": key_id}

    async def delete_key(self, key_id: str, user_id: str, changed_by: str) -> dict[str, str]:
        self.deleted.append((key_id, user_id, changed_by))
        return {"id": key_id}


class FakeVault:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str, str]] = []

    def delete(self, backend_id: str, user_id: str, key_id: str) -> None:
        self.deleted.append((backend_id, user_id, key_id))


def app_client() -> TestClient:
    client = TestClient(main.app)
    session = {
        SESSION_USER_KEY: {
            "email": LEADER_EMAIL,
            "name": "Leader",
            "avatar": "L",
            "department": "研发",
            "isAdmin": False,
        }
    }
    data = base64.b64encode(json.dumps(session).encode("utf-8"))
    signed = TimestampSigner(os.getenv("SESSION_SECRET", "dev-session-secret-change-me")).sign(data).decode("utf-8")
    client.cookies.set(main.SESSION_COOKIE_NAME, signed)
    return client


def csrf_headers(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.get("/api/auth/csrf").json()["csrfToken"]}


@pytest.fixture()
def leader_env(monkeypatch) -> tuple[TestClient, FakeStore, FakeClient, FakeVault]:
    main.team_auth_cache.clear()
    store = FakeStore([TEAM])
    upstream = FakeClient()
    vault = FakeVault()
    monkeypatch.setattr(main, "usage_store", lambda: store)
    monkeypatch.setattr(main, "usage_backend_ids", lambda: ["primary"])
    monkeypatch.setattr(main, "client", lambda: upstream)
    monkeypatch.setattr(main, "key_vault", lambda: vault)
    return app_client(), store, upstream, vault


def test_team_keys_hide_leaders_and_never_expose_reveal(leader_env) -> None:
    client, _store, upstream, _vault = leader_env

    response = client.get("/api/team/keys")

    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload["keys"]] == ["key-alice", "key-bob-blocked"]
    assert payload["memberCount"] == 2
    assert payload["stats"] == {"total": 2, "active": 1, "disabled": 1, "expired": 0}
    assert all(item["revealable"] is False for item in payload["keys"])
    # 上游内部字段和账号句柄都不能进入响应。
    assert all(not any(name.startswith("_") for name in item) for item in payload["keys"])
    assert payload["keys"][0]["memberEmail"] == "alice@auto-link.com.cn"
    assert sorted(upstream.list_calls[0][0]) == ["primary:alice", "primary:bob"]


def test_team_keys_apply_search_and_status_filters(leader_env) -> None:
    client, _store, _upstream, _vault = leader_env

    searched = client.get("/api/team/keys", params={"search": "Alice"}).json()
    assert [item["id"] for item in searched["keys"]] == ["key-alice"]
    # 统计始终反映团队全量，不随筛选变化。
    assert searched["stats"]["total"] == 2

    searched_by_type = client.get("/api/team/keys", params={"search": "Claude Code"}).json()
    assert [item["id"] for item in searched_by_type["keys"]] == ["key-alice"]

    filtered = client.get("/api/team/keys", params={"status": "已禁用"}).json()
    assert [item["id"] for item in filtered["keys"]] == ["key-bob-blocked"]


def test_team_keys_reject_non_leader(monkeypatch) -> None:
    main.team_auth_cache.clear()
    monkeypatch.setattr(main, "usage_store", lambda: FakeStore([]))
    monkeypatch.setattr(main, "usage_backend_ids", lambda: ["primary"])
    monkeypatch.setattr(main, "client", lambda: FakeClient())

    response = app_client().get("/api/team/keys")

    assert response.status_code == 403
    assert response.json()["detail"] == "当前账号还没有团队负责人权限"


def test_team_keys_reject_unauthorized_team_ref(leader_env) -> None:
    client, _store, _upstream, _vault = leader_env

    response = client.get("/api/team/keys", params={"team_ref": main.team_ref(OTHER_TEAM)})

    assert response.status_code == 403
    assert response.json()["detail"] == "当前账号无权查看该团队看板"


def test_local_account_cannot_reach_team_member_keys() -> None:
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(main.team_member_accounts({"id": "local-1", "email": LEADER_EMAIL}, None))

    assert excinfo.value.status_code == 403


def test_revoke_blocks_upstream_key_and_keeps_it_listed(leader_env) -> None:
    client, _store, upstream, _vault = leader_env

    response = client.post("/api/team/keys/key-alice/revoke", json={"teamRef": ""}, headers=csrf_headers(client))

    assert response.status_code == 200
    assert upstream.blocked == [("key-alice", "primary:alice", LEADER_EMAIL)]
    assert upstream.deleted == []


def test_revoke_rejects_another_leaders_key(leader_env) -> None:
    client, _store, upstream, _vault = leader_env

    response = client.post("/api/team/keys/key-co-leader/revoke", json={}, headers=csrf_headers(client))

    assert response.status_code == 403
    assert response.json()["detail"] == "无权管理该密钥"
    assert upstream.blocked == []


def test_revoke_rejects_already_disabled_key(leader_env) -> None:
    client, _store, upstream, _vault = leader_env

    response = client.post("/api/team/keys/key-bob-blocked/revoke", json={}, headers=csrf_headers(client))

    assert response.status_code == 409
    assert response.json()["detail"] == "该密钥已经是停用状态"
    assert upstream.blocked == []


def test_delete_requires_revoke_first(leader_env) -> None:
    client, _store, upstream, _vault = leader_env

    response = client.post("/api/team/keys/key-alice/delete", json={}, headers=csrf_headers(client))

    assert response.status_code == 409
    assert response.json()["detail"] == "请先撤销该密钥再删除"
    assert upstream.deleted == []


def test_delete_removes_revoked_key_and_clears_vault(leader_env) -> None:
    client, _store, upstream, vault = leader_env

    response = client.post("/api/team/keys/key-bob-blocked/delete", json={}, headers=csrf_headers(client))

    assert response.status_code == 200
    assert response.json()["warning"] == ""
    assert upstream.deleted == [("key-bob-blocked", "primary:bob", LEADER_EMAIL)]
    assert vault.deleted == [("primary", "bob", "key-bob-blocked")]


def test_mutations_require_csrf(leader_env) -> None:
    client, _store, upstream, _vault = leader_env

    response = client.post("/api/team/keys/key-alice/revoke", json={})

    assert response.status_code == 403
    assert upstream.blocked == []
