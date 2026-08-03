import asyncio
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from backend.litellm_client import LiteLLMBackend, LiteLLMClient


def make_client(monkeypatch: pytest.MonkeyPatch, handler: httpx.MockTransport) -> LiteLLMClient:
    monkeypatch.setenv("LITELLM_BASE_URL", "https://proxy.example")
    monkeypatch.setenv("LITELLM_ADMIN_KEY", "sk-admin")
    client = LiteLLMClient()
    asyncio.run(client.http_client.aclose())
    client.http_client = httpx.AsyncClient(transport=handler, timeout=client.timeout)
    return client


def json_request(request: httpx.Request) -> dict[str, Any]:
    import json

    return json.loads(request.content.decode("utf-8"))


def test_organization_and_team_management_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        responses = {
            ("POST", "/organization/new"): {"organization_id": "org-upstream"},
            ("PATCH", "/organization/update"): {"organization_id": "org-upstream", "blocked": True},
            ("GET", "/organization/list"): [{"organization_id": "org-upstream"}],
            ("GET", "/organization/info"): {"organization_id": "org-upstream", "members": [], "teams": []},
            ("POST", "/organization/member_add"): {"organization_id": "org-upstream"},
            ("PATCH", "/organization/member_update"): {"organization_id": "org-upstream", "user_id": "user-1"},
            ("DELETE", "/organization/member_delete"): {"organization_id": "org-upstream", "user_id": "user-1"},
            ("POST", "/team/new"): {"team_id": "team-upstream"},
            ("GET", "/v2/team/list"): {"teams": [{"team_id": "team-upstream"}]},
            ("POST", "/team/update"): {"team_id": "team-upstream", "blocked": True},
            ("POST", "/team/member_add"): {"team_id": "team-upstream"},
            ("POST", "/team/member_update"): {"team_id": "team-upstream", "user_id": "user-1"},
            ("POST", "/team/member_delete"): {"team_id": "team-upstream", "user_id": "user-1"},
        }
        return httpx.Response(200, json=responses[(request.method, request.url.path)])

    client = make_client(monkeypatch, httpx.MockTransport(handle))
    try:
        created = asyncio.run(
            client.create_organization(
                "Acme",
                organization_id="org-local",
                models=["gpt-5", "gpt-5"],
                max_budget=200,
                budget_duration="30d",
                metadata={"local_id": "org-local"},
                changed_by="operator@example.com",
            )
        )
        assert created["organization_id"] == "org-upstream"
        asyncio.run(client.update_organization("org-upstream", blocked=True, changed_by="operator@example.com"))
        assert asyncio.run(client.list_organizations(organization_id="org-upstream"))[0]["organization_id"] == "org-upstream"
        assert asyncio.run(client.organization_info("org-upstream"))["organization_id"] == "org-upstream"
        asyncio.run(client.add_organization_member("org-upstream", "enterprise_admin", user_id="user-1", max_budget=50))
        asyncio.run(client.update_organization_member("org-upstream", user_id="user-1", role="member"))
        asyncio.run(client.delete_organization_member("org-upstream", user_id="user-1"))
        asyncio.run(client.create_team("Engineering", "org-upstream", team_id="team-local", models=["gpt-5"]))
        teams = asyncio.run(
            client.list_teams(
                organization_id="org-upstream",
                team_id="team-upstream",
                team_alias="Engineering",
            )
        )
        assert teams == [{"team_id": "team-upstream"}]
        asyncio.run(client.update_team("team-upstream", blocked=True))
        asyncio.run(client.add_team_member("team-upstream", "member", user_email="USER@EXAMPLE.COM", max_budget=10))
        asyncio.run(client.update_team_member("team-upstream", user_id="user-1", role="admin"))
        asyncio.run(client.delete_team_member("team-upstream", user_id="user-1"))
    finally:
        asyncio.run(client.close())

    create_org = json_request(requests[0])
    assert create_org == {
        "organization_alias": "Acme",
        "models": ["gpt-5"],
        "organization_id": "org-local",
        "max_budget": 200,
        "budget_duration": "30d",
        "metadata": {"local_id": "org-local"},
    }
    assert requests[0].headers["litellm-changed-by"] == "operator@example.com"
    # LiteLLM 1.92 does not persist an organization-level ``blocked`` field;
    # sending it passes Pydantic's extra-field validation but fails in Prisma.
    assert "blocked" not in json_request(requests[1])
    assert requests[2].url.params["org_id"] == "org-upstream"
    assert requests[3].url.params["organization_id"] == "org-upstream"
    assert json_request(requests[4])["member"] == {"user_id": "user-1", "role": "org_admin"}
    assert json_request(requests[5])["role"] == "internal_user"
    assert requests[6].method == "DELETE"
    assert json_request(requests[7])["organization_id"] == "org-upstream"
    assert requests[8].url.params["organization_id"] == "org-upstream"
    assert requests[8].url.params["team_id"] == "team-upstream"
    assert requests[8].url.params["team_alias"] == "Engineering"
    assert json_request(requests[10])["member"] == {"user_email": "user@example.com", "role": "user"}


def test_enterprise_key_contract_and_one_time_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/key/generate":
            return httpx.Response(
                200,
                json={
                    "key": "sk-enterprise-secret",
                    "token_id": "hash-1",
                    "organization_id": "org-1",
                    "team_id": "team-1",
                    "user_id": "user-1",
                    "expires": "2026-12-31T00:00:00Z",
                },
            )
        if request.url.path == "/key/list":
            return httpx.Response(200, json={"keys": [{"token": "hash-1", "organization_id": "org-1"}]})
        if request.url.path == "/key/update":
            return httpx.Response(200, json={"token": "hash-1", "max_budget": 9})
        if request.url.path == "/key/delete":
            return httpx.Response(200, json={"deleted_keys": ["hash-1"]})
        raise AssertionError(request.url.path)

    client = make_client(monkeypatch, httpx.MockTransport(handle))
    try:
        created = asyncio.run(
            client.create_organization_key(
                "org-1",
                key_alias="enterprise:org-1:token-1",
                models=["gpt-5", "claude-opus"],
                daily_budget_usd=9,
                team_id="team-1",
                user_id="user-1",
                duration="30d",
                metadata={"local_token_id": "token-1"},
                changed_by="operator@example.com",
                idempotency_key="token-create-1",
            )
        )
        listed = asyncio.run(client.list_organization_keys("org-1", team_id="team-1", user_id="user-1"))
        updated = asyncio.run(client.update_organization_key_budget("hash-1", 12, changed_by="operator@example.com"))
        revoked = asyncio.run(client.revoke_organization_key("hash-1", changed_by="operator@example.com"))
    finally:
        asyncio.run(client.close())

    assert created["key"] == "sk-enterprise-secret"
    assert created["id"] == "hash-1"
    assert created["masked"] == "sk-...cret"
    assert listed == [{"token": "hash-1", "organization_id": "org-1"}]
    assert updated["max_budget"] == 9
    assert revoked == {"id": "hash-1", "deleted": True}

    # Key creation first performs an exact alias lookup because LiteLLM 1.92
    # does not consume Idempotency-Key on /key/generate. Assert contracts by
    # route instead of relying on the preflight request order.
    generate_request = next(request for request in requests if request.url.path == "/key/generate")
    list_requests = [request for request in requests if request.url.path == "/key/list"]
    update_request = next(request for request in requests if request.url.path == "/key/update")
    delete_request = next(request for request in requests if request.url.path == "/key/delete")

    assert list_requests[0].url.params["key_alias"] == "enterprise:org-1:token-1"
    assert list_requests[0].url.params["organization_id"] == "org-1"
    assert list_requests[0].url.params["return_full_object"] == "true"

    generated = json_request(generate_request)
    assert generated["organization_id"] == "org-1"
    assert generated["team_id"] == "team-1"
    assert generated["user_id"] == "user-1"
    assert generated["max_budget"] == 9
    assert generated["budget_duration"] == "1d"
    assert generated["key_type"] == "llm_api"
    assert generate_request.headers["idempotency-key"] == "token-create-1"
    assert list_requests[1].url.params["organization_id"] == "org-1"
    assert list_requests[1].url.params["return_full_object"] == "true"
    assert json_request(update_request) == {"key": "hash-1", "max_budget": 12, "budget_duration": "1d"}
    assert json_request(delete_request) == {"keys": ["hash-1"]}


def test_enterprise_key_list_reads_every_upstream_page(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = int(request.url.params.get("page", "1"))
        payloads = {
            1: {
                "keys": [{"token": "hash-1", "organization_id": "org-1"}],
                "current_page": 1,
                "total_pages": 2,
                "total_count": 2,
            },
            2: {
                "keys": [{"token": "hash-2", "organization_id": "org-1"}],
                "current_page": 2,
                "total_pages": 2,
                "total_count": 2,
            },
        }
        return httpx.Response(200, json=payloads[page])

    client = make_client(monkeypatch, httpx.MockTransport(handle))
    try:
        records = asyncio.run(client.list_organization_keys("org-1"))
    finally:
        asyncio.run(client.close())

    assert [record["token"] for record in records] == ["hash-1", "hash-2"]
    assert [request.url.params["page"] for request in requests] == ["1", "2"]


def test_key_alias_recovery_requires_an_exact_upstream_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    alias = "enterprise:org-1:token-1"

    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "keys": [
                    {
                        "token": "hash-missing-org",
                        "key_alias": alias,
                        "team_id": "team-1",
                        "user_id": "user-1",
                    },
                    {
                        "token": "hash-wrong-team",
                        "key_alias": alias,
                        "org_id": "org-1",
                        "team_id": "team-2",
                        "user_id": "user-1",
                    },
                    {
                        "token": "hash-wrong-user",
                        "key_alias": alias,
                        "org_id": "org-1",
                        "team_id": "team-1",
                        "user_id": "user-2",
                    },
                    {
                        "token": "hash-exact",
                        "key_alias": alias,
                        "org_id": "org-1",
                        "team_id": "team-1",
                        "user_id": "user-1",
                    },
                ]
            },
        )

    client = make_client(monkeypatch, httpx.MockTransport(handle))
    try:
        recovered = asyncio.run(
            client.find_organization_key_by_alias(
                "org-1",
                alias,
                team_id="team-1",
                user_id="user-1",
            )
        )
        shared = asyncio.run(client.find_organization_key_by_alias("org-1", alias))
    finally:
        asyncio.run(client.close())

    assert recovered is not None
    assert recovered["token"] == "hash-exact"
    assert shared is None


def test_capability_probe_and_daily_activity_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v2/team/list":
            return httpx.Response(500, json={"detail": {"error": "No db connected"}})
        if request.url.path.endswith("/daily/activity"):
            return httpx.Response(
                200,
                json={
                    "results": [{"date": "2026-07-30", "metrics": {"spend": 1.5}}],
                    "metadata": {"has_more": False},
                },
            )
        return httpx.Response(200, json={"items": []})

    client = make_client(monkeypatch, httpx.MockTransport(handle))
    try:
        capabilities = asyncio.run(client.organization_capabilities())
        organization_usage = asyncio.run(client.organization_daily_usage("org-1", "2026-07-01", "2026-07-31"))
        team_usage = asyncio.run(client.team_daily_usage("team-1", "2026-07-01", "2026-07-31", model="gpt-5"))
    finally:
        asyncio.run(client.close())

    assert capabilities["available"] is False
    assert capabilities["organizations"] is True
    assert capabilities["teams"] is False
    assert capabilities["keys"] is True
    assert capabilities["errors"]["teams"]["statusCode"] == 500
    assert client.daily_usage_rows(organization_usage) == [{"date": "2026-07-30", "metrics": {"spend": 1.5}}]
    assert client.daily_usage_rows(team_usage) == [{"date": "2026-07-30", "metrics": {"spend": 1.5}}]
    assert requests[3].url.params["organization_ids"] == "org-1"
    assert requests[4].url.params["team_ids"] == "team-1"
    assert requests[4].url.params["model"] == "gpt-5"


def test_member_identity_and_key_delete_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch, httpx.MockTransport(lambda _: httpx.Response(200, json={"deleted_keys": []})))
    try:
        with pytest.raises(HTTPException, match="用户编号或邮箱"):
            asyncio.run(client.add_organization_member("org-1", "member"))
        with pytest.raises(HTTPException, match="未确认"):
            asyncio.run(client.revoke_organization_key("hash-1"))
    finally:
        asyncio.run(client.close())


def test_durable_key_delete_is_idempotent_on_upstream_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404, json={"detail": {"error": "No keys found"}})

    client = make_client(monkeypatch, httpx.MockTransport(handle))
    try:
        repeated = asyncio.run(
            client.revoke_organization_key(
                "hash-1",
                changed_by="worker@example.com",
                idempotency_key="outbox-token-revoke",
            )
        )
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(client.revoke_organization_key("hash-1"))
    finally:
        asyncio.run(client.close())

    assert repeated == {"id": "hash-1", "deleted": True, "alreadyAbsent": True}
    assert requests[0].headers["idempotency-key"] == "outbox-token-revoke"
    assert requests[0].headers["litellm-changed-by"] == "worker@example.com"
    assert exc_info.value.status_code == 404


def test_management_methods_accept_explicit_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"organization_id": "org-1"})

    client = make_client(monkeypatch, httpx.MockTransport(handle))
    backend = LiteLLMBackend("secondary", "Secondary", "https://proxy.example", "sk-secondary")
    try:
        asyncio.run(client.organization_info("org-1", backend=backend))
    finally:
        asyncio.run(client.close())
    assert seen == ["Bearer sk-secondary"]
