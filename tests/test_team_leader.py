import os
import asyncio
from typing import Any

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from backend import main
from backend.auth import SESSION_USER_KEY
from backend.litellm_client import LiteLLMBackend, LiteLLMClient


class FakeLiteLLMClient:
    def __init__(
        self,
        scope: dict[str, Any],
        payload: dict[str, Any] | None = None,
        usage_rows: list[dict[str, Any]] | dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.scope = scope
        self.payload = payload or {}
        self.calls: list[tuple[str, str, str, str, str | None]] = []
        self.usage_calls: list[tuple[list[str], str, str, str | None]] = []
        self.usage_rows = usage_rows or []

    async def team_leader_scope(self, upstream_user: dict[str, Any]) -> dict[str, Any]:
        return self.scope

    async def team_usage_rows(self, backend: str, team_id: str, start_date: str, end_date: str, source: str | None) -> dict[str, Any]:
        self.calls.append((backend, team_id, start_date, end_date, source))
        return self.payload

    async def usage_rows_for_user_ids(self, user_ids: list[str], start_date: str, end_date: str, source: str | None) -> list[dict[str, Any]]:
        self.usage_calls.append((user_ids, start_date, end_date, source))
        if isinstance(self.usage_rows, dict):
            rows: list[dict[str, Any]] = []
            for user_id in user_ids:
                rows.extend(self.usage_rows.get(user_id, []))
            return rows
        return self.usage_rows


def reset_caches() -> None:
    main.user_mapping_cache.clear()
    main.personal_usage_cache.clear()
    main.team_auth_cache.clear()
    main.team_usage_cache.clear()
    main.team_member_usage_cache.clear()


def app_client(email: str = "leader@auto-link.com.cn") -> TestClient:
    import base64
    import json

    client = TestClient(main.app)
    session = {
        SESSION_USER_KEY: {
            "email": email,
            "name": "Leader",
            "avatar": "L",
            "department": "????",
            "isAdmin": False,
        }
    }
    data = base64.b64encode(json.dumps(session).encode("utf-8"))
    signed = TimestampSigner(os.getenv("SESSION_SECRET", "dev-session-secret-change-me")).sign(data).decode("utf-8")
    client.cookies.set(main.SESSION_COOKIE_NAME, signed)
    return client

def patch_user(monkeypatch, user_id: str = "leader-user") -> None:
    async def fake_cached_resolve_user(email: str, name: str | None = None, refresh: bool = False):
        resolved_user_id = "alice-user" if email == "alice@auto-link.com.cn" else user_id
        return {
            "matched_user_ids": [resolved_user_id],
            "matched_accounts": [{"backend": "primary", "user_id": resolved_user_id, "account_id": resolved_user_id}],
        }, {"hit": False, "ttlSeconds": 0}

    monkeypatch.setattr(main, "cached_resolve_user", fake_cached_resolve_user)


def test_team_leader_scope_matches_admin_role_by_raw_user_id(monkeypatch) -> None:
    client = object.__new__(LiteLLMClient)
    backend = LiteLLMBackend(
        id="primary",
        label="Primary",
        base_url="https://example.test",
        admin_key="test-key",
    )
    client.backends = [backend]
    client._backend_map = {"primary": backend}

    async def fake_teams(backend_arg: LiteLLMBackend | None = None) -> list[dict[str, Any]]:
        return [
            {
                "team_id": "team-a",
                "team_alias": "Team A",
                "members_with_roles": [
                    {"user_id": "leader-user", "role": "admin"},
                    {"user_id": "regular-user", "role": "user"},
                ],
            }
        ]

    monkeypatch.setattr(client, "teams", fake_teams)

    scope = asyncio.run(client.team_leader_scope({"matched_accounts": [{"backend": "primary", "user_id": "leader-user"}]}))
    regular_scope = asyncio.run(client.team_leader_scope({"matched_accounts": [{"backend": "primary", "user_id": "regular-user"}]}))

    assert scope["teamBoardStatus"] == "single"
    assert scope["team"]["id"] == "team-a"
    assert regular_scope["teamBoardStatus"] == "none"


def test_team_leader_scope_accepts_admin_role_and_email_match(monkeypatch) -> None:
    client = object.__new__(LiteLLMClient)
    backend = LiteLLMBackend(
        id="primary",
        label="Primary",
        base_url="https://example.test",
        admin_key="test-key",
    )
    client.backends = [backend]
    client._backend_map = {"primary": backend}

    async def fake_teams(backend_arg: LiteLLMBackend | None = None) -> list[dict[str, Any]]:
        return [
            {
                "team_id": "team-email",
                "team_alias": "Email Team",
                "members_with_roles": [
                    {"user_id": "different-user", "user_email": "leader@auto-link.com.cn", "role": "admin"},
                    {"user_id": "regular-user", "user_email": "member@auto-link.com.cn", "role": "user"},
                ],
            }
        ]

    monkeypatch.setattr(client, "teams", fake_teams)

    scope = asyncio.run(
        client.team_leader_scope(
            {
                "user_email": "leader@auto-link.com.cn",
                "matched_accounts": [{"backend": "primary", "user_id": "leader-user", "user_email": "leader@auto-link.com.cn"}],
            }
        )
    )

    assert scope["teamBoardStatus"] == "single"
    assert scope["team"]["id"] == "team-email"


def test_team_leader_scope_rejects_noncanonical_team_admin_role(monkeypatch) -> None:
    client = object.__new__(LiteLLMClient)
    backend = LiteLLMBackend(
        id="primary",
        label="Primary",
        base_url="https://example.test",
        admin_key="test-key",
    )
    client.backends = [backend]
    client._backend_map = {"primary": backend}

    async def fake_teams(backend_arg: LiteLLMBackend | None = None) -> list[dict[str, Any]]:
        return [
            {
                "team_id": "team-a",
                "team_alias": "Team A",
                "members_with_roles": [
                    {"user_id": "leader-user", "role": "team_admin"},
                ],
            }
        ]

    monkeypatch.setattr(client, "teams", fake_teams)

    scope = asyncio.run(client.team_leader_scope({"matched_accounts": [{"backend": "primary", "user_id": "leader-user"}]}))

    assert scope["isTeamLeader"] is False
    assert scope["teamBoardStatus"] == "none"


def test_teams_hydrates_v2_list_with_team_info(monkeypatch) -> None:
    client = object.__new__(LiteLLMClient)
    backend = LiteLLMBackend(
        id="primary",
        label="Primary",
        base_url="https://example.test",
        admin_key="test-key",
    )
    client.backends = [backend]
    client._backend_map = {"primary": backend}
    calls: list[tuple[str, str]] = []

    async def fake_request_backend(backend_arg: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((method, path))
        if path == "/v2/team/list":
            return {
                "teams": [
                    {"team_id": "team-a", "team_alias": "Team A", "members_with_roles": []},
                ],
                "total_pages": 1,
            }
        if path == "/team/info":
            return {
                "team_id": "team-a",
                "team_info": {
                    "team_id": "team-a",
                    "team_alias": "Team A",
                    "members_with_roles": [
                        {"user_id": "leader-user", "role": "admin"},
                    ],
                },
            }
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    teams = asyncio.run(client.teams(backend))
    scope = asyncio.run(client.team_leader_scope({"matched_accounts": [{"backend": "primary", "user_id": "leader-user"}]}))

    assert teams[0]["members_with_roles"][0]["user_id"] == "leader-user"
    assert calls == [("GET", "/v2/team/list"), ("GET", "/team/info"), ("GET", "/v2/team/list")]
    assert scope["isTeamLeader"] is True
    assert scope["team"]["id"] == "team-a"


def test_teams_falls_back_to_team_list_when_v2_unavailable(monkeypatch) -> None:
    client = object.__new__(LiteLLMClient)
    backend = LiteLLMBackend(
        id="primary",
        label="Primary",
        base_url="https://example.test",
        admin_key="test-key",
    )
    client.backends = [backend]
    client._backend_map = {"primary": backend}

    async def fake_request_backend(backend_arg: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if path == "/v2/team/list":
            raise main.HTTPException(status_code=404, detail="not found")
        if path == "/team/list":
            return [
                {
                    "team_id": "team-a",
                    "team_alias": "Team A",
                    "members_with_roles": [{"user_id": "leader-user", "role": "admin"}],
                }
            ]
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    scope = asyncio.run(client.team_leader_scope({"matched_accounts": [{"backend": "primary", "user_id": "leader-user"}]}))

    assert scope["isTeamLeader"] is True
    assert scope["team"]["id"] == "team-a"


def test_auth_me_returns_base_identity_without_resolving_scope(monkeypatch) -> None:
    reset_caches()
    patch_user(monkeypatch)
    fake = FakeLiteLLMClient(
        {
            "isTeamLeader": True,
            "teamBoardStatus": "single",
            "team": {"id": "team-a", "name": "Team A", "memberCount": 2, "backend": "primary"},
            "leaderTeams": [{"id": "team-a", "name": "Team A", "memberCount": 2, "backend": "primary"}],
        }
    )
    monkeypatch.setattr(main, "client", lambda: fake)

    response = app_client().get("/api/auth/me")

    assert response.status_code == 200
    payload = response.json()
    assert payload["email"] == "leader@auto-link.com.cn"
    assert payload["isTeamLeader"] is False
    assert payload["teamBoardStatus"] == "loading"
    assert fake.scope


def test_auth_scope_returns_team_permissions(monkeypatch) -> None:
    reset_caches()
    patch_user(monkeypatch)
    fake = FakeLiteLLMClient(
        {
            "isTeamLeader": True,
            "teamBoardStatus": "single",
            "team": {"id": "team-a", "name": "Team A", "memberCount": 2, "backend": "primary"},
            "leaderTeams": [{"id": "team-a", "name": "Team A", "memberCount": 2, "backend": "primary"}],
        }
    )
    monkeypatch.setattr(main, "client", lambda: fake)

    response = app_client().get("/api/auth/scope")

    assert response.status_code == 200
    payload = response.json()
    assert payload["isTeamLeader"] is True
    assert payload["team"]["teamRef"]
    assert "backend" not in payload["team"]


def test_auth_scope_requires_login() -> None:
    response = TestClient(main.app).get("/api/auth/scope")
    assert response.status_code == 401


def test_non_team_admin_cannot_access_team_usage(monkeypatch) -> None:
    reset_caches()
    patch_user(monkeypatch)
    fake = FakeLiteLLMClient({"isTeamLeader": False, "teamBoardStatus": "none", "team": None, "leaderTeams": []})
    monkeypatch.setattr(main, "client", lambda: fake)

    response = app_client("member@auto-link.com.cn").get("/api/team/usage")

    assert response.status_code == 403


def test_multiple_team_admin_gets_selectable_teams(monkeypatch) -> None:
    reset_caches()
    patch_user(monkeypatch)
    fake = FakeLiteLLMClient(
        {
            "isTeamLeader": True,
            "teamBoardStatus": "multiple",
            "team": None,
            "leaderTeams": [
                {"id": "team-a", "name": "Team A", "memberCount": 2, "backend": "primary"},
                {"id": "team-b", "name": "Team B", "memberCount": 3, "backend": "primary"},
            ],
        }
    )
    monkeypatch.setattr(main, "client", lambda: fake)

    me = app_client().get("/api/auth/scope")
    assert me.json()["teamBoardStatus"] == "multiple"
    assert len(me.json()["leaderTeams"]) == 2
    assert all("teamRef" in team for team in me.json()["leaderTeams"])


def test_multiple_team_admin_can_request_authorized_team_ref(monkeypatch) -> None:
    reset_caches()
    patch_user(monkeypatch)
    scope = {
        "isTeamLeader": True,
        "teamBoardStatus": "multiple",
        "team": None,
        "leaderTeams": [
            {"id": "team-a", "name": "Team A", "memberCount": 2, "backend": "primary"},
            {"id": "team-b", "name": "Team B", "memberCount": 3, "backend": "primary"},
        ],
    }
    fake = FakeLiteLLMClient(scope, {"rows": [], "summaryRows": [], "employees": [], "team": {"id": "team-b", "name": "Team B", "memberCount": 3}})
    monkeypatch.setattr(main, "client", lambda: fake)

    me_payload = app_client().get("/api/auth/scope").json()
    team_b_ref = next(team["teamRef"] for team in me_payload["leaderTeams"] if team["id"] == "team-b")
    response = app_client().get(f"/api/team/usage?team_ref={team_b_ref}")

    assert response.status_code == 200
    assert fake.calls[0][1] == "team-b"
    assert response.json()["team"]["teamRef"] == team_b_ref


def test_invalid_team_ref_is_forbidden(monkeypatch) -> None:
    reset_caches()
    patch_user(monkeypatch)
    fake = FakeLiteLLMClient(
        {
            "isTeamLeader": True,
            "teamBoardStatus": "multiple",
            "team": None,
            "leaderTeams": [{"id": "team-a", "name": "Team A", "memberCount": 2, "backend": "primary"}],
        }
    )
    monkeypatch.setattr(main, "client", lambda: fake)

    response = app_client().get("/api/team/usage?team_ref=not-authorized")

    assert response.status_code == 403
    assert fake.calls == []


def test_team_usage_includes_zero_usage_members(monkeypatch) -> None:
    reset_caches()
    patch_user(monkeypatch)
    fake = FakeLiteLLMClient(
        {
            "isTeamLeader": True,
            "teamBoardStatus": "single",
            "team": {"id": "team-a", "name": "Team A", "memberCount": 2, "backend": "primary"},
            "leaderTeams": [{"id": "team-a", "name": "Team A", "memberCount": 2, "backend": "primary"}],
        },
        {
            "rows": [],
            "summaryRows": [],
            "employees": [
                {"employeeId": "active", "employeeName": "Active", "totalTokens": 10},
                {"employeeId": "quiet", "employeeName": "Quiet", "totalTokens": 0},
            ],
            "team": {"id": "team-a", "name": "Team A", "memberCount": 2, "backend": "primary"},
        },
    )
    monkeypatch.setattr(main, "client", lambda: fake)

    response = app_client().get("/api/team/usage")

    assert response.status_code == 200
    employees = response.json()["employees"]
    assert {employee["employeeId"] for employee in employees} == {"active", "quiet"}


def test_team_usage_ranking_uses_member_account_usage(monkeypatch) -> None:
    reset_caches()
    patch_user(monkeypatch)
    fake = FakeLiteLLMClient(
        {
            "isTeamLeader": True,
            "teamBoardStatus": "single",
            "team": {"id": "team-a", "name": "Team A", "memberCount": 2, "backend": "primary"},
            "leaderTeams": [{"id": "team-a", "name": "Team A", "memberCount": 2, "backend": "primary"}],
        },
        {
            "rows": [
                {
                    "date": "2026-07-22",
                    "source": "Claude Code",
                    "model": "team-model",
                    "employeeId": "alice@auto-link.com.cn",
                    "employeeName": "Alice",
                    "employeeEmail": "alice@auto-link.com.cn",
                    "promptTokens": 600,
                    "completionTokens": 399,
                    "totalTokens": 999,
                    "requestCount": 9,
                    "successCount": 9,
                    "failureCount": 0,
                    "spend": 9.99,
                }
            ],
            "summaryRows": [],
            "employees": [
                {
                    "employeeId": "alice@auto-link.com.cn",
                    "employeeName": "Alice",
                    "employeeEmail": "alice@auto-link.com.cn",
                    "userIds": ["alice-user"],
                    "totalTokens": 10,
                    "teamRole": "user",
                    "bindStatus": "已绑定邮箱",
                },
                {
                    "employeeId": "quiet",
                    "employeeName": "Quiet",
                    "employeeEmail": "",
                    "userIds": ["quiet-user"],
                    "totalTokens": 0,
                    "teamRole": "user",
                    "bindStatus": "未绑定邮箱",
                },
            ],
            "team": {"id": "team-a", "name": "Team A", "memberCount": 2, "backend": "primary"},
        },
        {
            "alice-user": [
                {
                    "date": "2026-07-22",
                    "source": "Cursor",
                    "model": "gpt-5",
                    "promptTokens": 20,
                    "completionTokens": 10,
                    "totalTokens": 30,
                    "requestCount": 2,
                    "successCount": 2,
                    "failureCount": 0,
                    "spend": 1.23,
                }
            ],
            "quiet-user": [],
        },
    )
    monkeypatch.setattr(main, "client", lambda: fake)

    response = app_client().get("/api/team/usage")

    assert response.status_code == 200
    payload = response.json()
    employees = {item["employeeId"]: item for item in payload["employees"]}
    assert payload["rows"][0]["totalTokens"] == 999
    assert employees["alice@auto-link.com.cn"]["totalTokens"] == 30
    assert employees["alice@auto-link.com.cn"]["requestCount"] == 2
    assert employees["quiet"]["totalTokens"] == 0
    assert fake.usage_calls[0][0] == ["alice-user"]
    assert fake.usage_calls[1][0] == ["quiet-user"]


def test_team_usage_ranking_matches_member_detail_total(monkeypatch) -> None:
    reset_caches()
    patch_user(monkeypatch)
    usage_rows = {
        "alice-user": [
            {
                "date": "2026-07-22",
                "source": "Claude Code",
                "model": "anthropic.claude-opus-4-8",
                "promptTokens": 400,
                "completionTokens": 100,
                "totalTokens": 500,
                "requestCount": 4,
                "successCount": 4,
                "failureCount": 0,
                "spend": 2.5,
            }
        ],
        "bob-user": [],
    }
    fake = FakeLiteLLMClient(team_member_scope(), team_member_payload(), usage_rows)
    monkeypatch.setattr(main, "client", lambda: fake)

    ranking_response = app_client().get("/api/team/usage")
    member_response = app_client().get("/api/team/member/usage?employee=alice@auto-link.com.cn")

    assert ranking_response.status_code == 200
    assert member_response.status_code == 200
    alice = next(item for item in ranking_response.json()["employees"] if item["employeeId"] == "alice@auto-link.com.cn")
    assert alice["totalTokens"] == member_response.json()["summary"]["rangeTotal"]["totalTokens"]
    assert alice["requestCount"] == member_response.json()["summary"]["rangeTotal"]["requestCount"]


def test_team_usage_ranking_resolves_member_email_without_user_ids(monkeypatch) -> None:
    reset_caches()

    async def fake_cached_resolve_user(email: str, name: str | None = None, refresh: bool = False):
        if email == "alice@auto-link.com.cn":
            return {
                "matched_user_ids": ["alice-resolved-user"],
                "matched_accounts": [{"backend": "primary", "user_id": "alice-resolved-user", "account_id": "alice-resolved-user"}],
            }, {"hit": False, "ttlSeconds": 0}
        return {
            "matched_user_ids": ["leader-user"],
            "matched_accounts": [{"backend": "primary", "user_id": "leader-user", "account_id": "leader-user"}],
        }, {"hit": False, "ttlSeconds": 0}

    monkeypatch.setattr(main, "cached_resolve_user", fake_cached_resolve_user)
    fake = FakeLiteLLMClient(
        team_member_scope(),
        {
            "rows": [],
            "summaryRows": [],
            "employees": [
                {
                    "employeeId": "alice@auto-link.com.cn",
                    "employeeName": "Alice",
                    "employeeEmail": "alice@auto-link.com.cn",
                    "userIds": [],
                    "teamRole": "user",
                    "bindStatus": "已绑定邮箱",
                },
            ],
            "team": {"id": "team-a", "name": "Team A", "memberCount": 1, "backend": "primary"},
        },
        {
            "alice-resolved-user": [
                {
                    "date": "2026-07-22",
                    "source": "Cursor",
                    "model": "gpt-5",
                    "promptTokens": 5,
                    "completionTokens": 5,
                    "totalTokens": 10,
                    "requestCount": 1,
                    "successCount": 1,
                    "failureCount": 0,
                    "spend": 0.1,
                }
            ],
        },
    )
    monkeypatch.setattr(main, "client", lambda: fake)

    response = app_client().get("/api/team/usage")

    assert response.status_code == 200
    alice = response.json()["employees"][0]
    assert alice["totalTokens"] == 10
    assert alice["userIds"] == ["alice-resolved-user"]


def test_team_usage_ranking_aggregates_all_accounts_for_member_email(monkeypatch) -> None:
    reset_caches()

    async def fake_cached_resolve_user(email: str, name: str | None = None, refresh: bool = False):
        return {
            "matched_user_ids": ["alice-claude", "alice-cursor", "alice-claude"],
            "matched_accounts": [{"user_id": "alice-claude"}, {"user_id": "alice-cursor"}],
        }, {"hit": False, "ttlSeconds": 0}

    monkeypatch.setattr(main, "cached_resolve_user", fake_cached_resolve_user)
    fake = FakeLiteLLMClient(
        team_member_scope(),
        {"rows": [], "summaryRows": [], "employees": [{
            "employeeId": "alice@example.com", "employeeName": "Alice", "employeeEmail": "alice@example.com",
            "userIds": ["alice-claude"], "teamRole": "user", "bindStatus": "已绑定邮箱",
        }], "team": {"id": "team-a", "name": "Team A", "memberCount": 1}},
        {
            "alice-claude": [{"source": "Claude Code", "totalTokens": 100, "requestCount": 1}],
            "alice-cursor": [{"source": "Cursor", "totalTokens": 250, "requestCount": 2}],
        },
    )
    monkeypatch.setattr(main, "client", lambda: fake)

    response = app_client().get("/api/team/usage")

    assert response.status_code == 200
    employee = response.json()["employees"][0]
    assert employee["totalTokens"] == 350
    assert employee["requestCount"] == 3
    assert employee["userIds"] == ["alice-claude", "alice-cursor"]


def test_team_usage_uses_one_cross_backend_sql_batch_without_upstream(monkeypatch) -> None:
    reset_caches()
    patch_user(monkeypatch)

    class FakeStore:
        calls = 0

        async def connect(self):
            return None

        async def team_rows(self, *_args):
            return {"rows": [], "summaryRows": [], "employees": [
                {"employeeId": "alice", "employeeName": "Alice", "employeeEmail": "alice@example.com", "userIds": ["alice-primary"], "teamRole": "user"},
                {"employeeId": "bob", "employeeName": "Bob", "employeeEmail": "bob@example.com", "userIds": ["bob-primary"], "teamRole": "user"},
            ], "team": {"id": "team-a", "name": "Team A", "memberCount": 2}, "lastSyncedAt": None}

        async def rows_by_employee_emails(self, emails, *_args):
            self.calls += 1
            return {
                "alice@example.com": {"rows": [{"source": "Her", "totalTokens": 40}], "userIds": ["alice-her"], "lastSyncedAt": None},
                "bob@example.com": {"rows": [{"source": "Cursor", "totalTokens": 60}], "userIds": ["bob-primary"], "lastSyncedAt": None},
            }

    fake_store = FakeStore()
    fake = FakeLiteLLMClient(team_member_scope())
    async def fail_upstream(*_args, **_kwargs):
        raise AssertionError("database hit must not resolve members upstream")
    fake.usage_rows_for_user_ids = fail_upstream
    monkeypatch.setattr(main, "usage_store", lambda: fake_store)
    monkeypatch.setattr(main, "usage_backend_ids", lambda: ["primary", "her"])
    monkeypatch.setattr(main, "client", lambda: fake)

    response = app_client().get("/api/team/usage")

    assert response.status_code == 200
    assert fake_store.calls == 1
    assert [item["totalTokens"] for item in response.json()["employees"]] == [60, 40]


def test_team_usage_refresh_still_uses_cross_backend_sql_batch(monkeypatch) -> None:
    reset_caches()
    patch_user(monkeypatch)

    class FakeStore:
        calls = 0

        async def connect(self):
            return None

        async def team_rows(self, *_args):
            return {"rows": [], "summaryRows": [], "employees": [
                {"employeeId": "alice", "employeeName": "Alice", "employeeEmail": "alice@example.com", "userIds": ["alice-primary"], "teamRole": "user"},
            ], "team": {"id": "team-a", "name": "Team A", "memberCount": 1}, "lastSyncedAt": None}

        async def rows_by_employee_emails(self, emails, *_args):
            self.calls += 1
            return {"alice@example.com": {"rows": [{"source": "Cursor", "totalTokens": 80}], "userIds": ["alice-primary"], "lastSyncedAt": None}}

    fake_store = FakeStore()
    fake = FakeLiteLLMClient(team_member_scope())

    async def fail_upstream(*_args, **_kwargs):
        raise AssertionError("refresh must keep using the database batch")

    fake.usage_rows_for_user_ids = fail_upstream
    monkeypatch.setattr(main, "usage_store", lambda: fake_store)
    monkeypatch.setattr(main, "usage_backend_ids", lambda: ["primary"])
    monkeypatch.setattr(main, "client", lambda: fake)

    response = app_client().get("/api/team/usage?refresh=1")

    assert response.status_code == 200
    assert fake_store.calls == 1
    assert response.json()["employees"][0]["totalTokens"] == 80


def test_team_usage_ignores_client_team_override(monkeypatch) -> None:
    reset_caches()
    patch_user(monkeypatch)
    fake = FakeLiteLLMClient(
        {
            "isTeamLeader": True,
            "teamBoardStatus": "single",
            "team": {"id": "authorized-team", "name": "Authorized", "memberCount": 1, "backend": "primary"},
            "leaderTeams": [{"id": "authorized-team", "name": "Authorized", "memberCount": 1, "backend": "primary"}],
        },
        {"rows": [], "summaryRows": [], "employees": [], "team": {"id": "authorized-team", "name": "Authorized", "memberCount": 1, "backend": "primary"}},
    )
    monkeypatch.setattr(main, "client", lambda: fake)

    response = app_client().get("/api/team/usage?team=other-team")

    assert response.status_code == 200
    assert fake.calls[0][1] == "authorized-team"


def team_member_scope() -> dict[str, Any]:
    return {
        "isTeamLeader": True,
        "teamBoardStatus": "single",
        "team": {"id": "team-a", "name": "Team A", "memberCount": 2, "backend": "primary"},
        "leaderTeams": [{"id": "team-a", "name": "Team A", "memberCount": 2, "backend": "primary"}],
    }


def team_member_payload() -> dict[str, Any]:
    return {
        "rows": [],
        "summaryRows": [],
        "employees": [
            {
                "employeeId": "alice@auto-link.com.cn",
                "employeeName": "Alice",
                "employeeEmail": "alice@auto-link.com.cn",
                "userIds": ["alice-user"],
                "totalTokens": 10,
                "teamRole": "user",
                "bindStatus": "已绑定邮箱",
            },
            {
                "employeeId": "bob-id",
                "employeeName": "Bob",
                "employeeEmail": "",
                "userIds": ["bob-user"],
                "totalTokens": 0,
                "teamRole": "user",
                "bindStatus": "未绑定邮箱",
            },
        ],
        "team": {"id": "team-a", "name": "Team A", "memberCount": 2, "backend": "primary"},
    }


def test_non_team_admin_cannot_access_team_member_usage(monkeypatch) -> None:
    reset_caches()
    patch_user(monkeypatch)
    fake = FakeLiteLLMClient({"isTeamLeader": False, "teamBoardStatus": "none", "team": None, "leaderTeams": []})
    monkeypatch.setattr(main, "client", lambda: fake)

    response = app_client("member@auto-link.com.cn").get("/api/team/member/usage?employee=alice@auto-link.com.cn")

    assert response.status_code == 403
    assert fake.usage_calls == []


def test_team_member_usage_rejects_invalid_team_ref(monkeypatch) -> None:
    reset_caches()
    patch_user(monkeypatch)
    fake = FakeLiteLLMClient(team_member_scope(), team_member_payload())
    monkeypatch.setattr(main, "client", lambda: fake)

    response = app_client().get("/api/team/member/usage?team_ref=not-authorized&employee=alice@auto-link.com.cn")

    assert response.status_code == 403
    assert fake.usage_calls == []


def test_team_member_usage_matches_member_email_and_returns_empty_summary(monkeypatch) -> None:
    reset_caches()
    patch_user(monkeypatch)
    fake = FakeLiteLLMClient(team_member_scope(), team_member_payload())
    monkeypatch.setattr(main, "client", lambda: fake)

    response = app_client().get("/api/team/member/usage?employee=alice@auto-link.com.cn&start_date=2026-07-01&end_date=2026-07-22")

    assert response.status_code == 200
    payload = response.json()
    assert payload["rows"] == []
    assert payload["summary"]["rangeTotal"]["totalTokens"] == 0
    assert payload["user"]["email"] == "alice@auto-link.com.cn"
    assert fake.usage_calls == [(["alice-user"], "2026-07-01", "2026-07-22", "all")]


def test_team_member_usage_matches_personal_dashboard_for_same_email(monkeypatch) -> None:
    reset_caches()

    async def fake_cached_resolve_user(email: str, name: str | None = None, refresh: bool = False):
        ids = ["alice-claude", "alice-cursor"] if email == "alice@auto-link.com.cn" else ["leader-user"]
        return {"matched_user_ids": ids, "matched_accounts": [{"user_id": item} for item in ids]}, {"hit": False, "ttlSeconds": 0}

    usage_rows = {
        "alice-claude": [{"date": "2026-07-22", "source": "Claude Code", "model": "claude", "totalTokens": 100, "requestCount": 1}],
        "alice-cursor": [{"date": "2026-07-22", "source": "Cursor", "model": "gpt-5", "totalTokens": 200, "requestCount": 2}],
    }
    monkeypatch.setattr(main, "cached_resolve_user", fake_cached_resolve_user)
    fake = FakeLiteLLMClient(team_member_scope(), team_member_payload(), usage_rows)
    monkeypatch.setattr(main, "client", lambda: fake)

    member = app_client().get("/api/team/member/usage?employee=alice@auto-link.com.cn&start_date=2026-07-22&end_date=2026-07-22")
    personal = asyncio.run(main.personal_usage_payload(
        {"email": "alice@auto-link.com.cn", "name": "Alice"}, "2026-07-22", "2026-07-22", "all",
    ))

    assert member.status_code == 200
    assert member.json()["rows"] == personal["rows"]
    assert member.json()["summary"] == personal["summary"]


def test_team_member_usage_matches_employee_id_and_user_ids(monkeypatch) -> None:
    reset_caches()
    patch_user(monkeypatch)
    fake = FakeLiteLLMClient(
        team_member_scope(),
        team_member_payload(),
        [
            {
                "date": "2026-07-22",
                "source": "Cursor",
                "model": "gpt-5",
                "promptTokens": 7,
                "completionTokens": 3,
                "totalTokens": 10,
                "requestCount": 1,
                "successCount": 1,
                "failureCount": 0,
                "spend": 0.01,
            }
        ],
    )
    monkeypatch.setattr(main, "client", lambda: fake)

    response = app_client().get("/api/team/member/usage?employee=bob-id")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["rangeTotal"]["totalTokens"] == 10
    assert payload["user"]["employeeId"] == "bob-id"
    assert fake.usage_calls[0][0] == ["bob-user"]


def test_team_member_usage_rejects_non_team_member(monkeypatch) -> None:
    reset_caches()
    patch_user(monkeypatch)
    fake = FakeLiteLLMClient(team_member_scope(), team_member_payload())
    monkeypatch.setattr(main, "client", lambda: fake)

    response = app_client().get("/api/team/member/usage?employee=mallory@auto-link.com.cn")

    assert response.status_code == 404
    assert fake.usage_calls == []
