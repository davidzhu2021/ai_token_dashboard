import os
import asyncio
from typing import Any

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from backend import main
from backend.auth import SESSION_USER_KEY
from backend.litellm_client import LiteLLMBackend, LiteLLMClient


class FakeLiteLLMClient:
    def __init__(self, scope: dict[str, Any], payload: dict[str, Any] | None = None) -> None:
        self.scope = scope
        self.payload = payload or {}
        self.calls: list[tuple[str, str, str, str, str | None]] = []

    async def team_leader_scope(self, upstream_user: dict[str, Any]) -> dict[str, Any]:
        return self.scope

    async def team_usage_rows(self, backend: str, team_id: str, start_date: str, end_date: str, source: str | None, refresh: bool = False) -> dict[str, Any]:
        self.calls.append((backend, team_id, start_date, end_date, source))
        return self.payload


def reset_caches() -> None:
    main.user_mapping_cache.clear()
    main.team_auth_cache.clear()
    main.team_usage_cache.clear()


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
    client.cookies.set("session", signed)
    return client

def patch_user(monkeypatch, user_id: str = "leader-user") -> None:
    async def fake_cached_resolve_user(email: str, name: str | None = None, refresh: bool = False):
        return {
            "matched_user_ids": [user_id],
            "matched_accounts": [{"backend": "primary", "user_id": user_id, "account_id": user_id}],
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
    assert calls == [("GET", "/v2/team/list"), ("GET", "/team/info")]
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


def test_auth_me_marks_single_team_admin(monkeypatch) -> None:
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
    assert payload["isTeamLeader"] is True
    assert payload["teamBoardStatus"] == "single"
    assert payload["team"]["id"] == "team-a"
    assert payload["team"]["teamRef"]
    assert "backend" not in payload["team"]
    assert payload["leaderTeams"][0]["teamRef"] == payload["team"]["teamRef"]


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

    me = app_client().get("/api/auth/me")
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

    me_payload = app_client().get("/api/auth/me").json()
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
