"""Store rules and route boundaries for customer-scoped access tokens."""

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from backend import main
from backend.organization_store import (
    DEFAULT_TOKEN_DAILY_BUDGET_USD,
    MAX_TOKENS_PER_ORGANIZATION,
    ORGANIZATION_TOKEN_MODELS,
    InMemoryOrganizationStore,
    OrganizationConflictError,
    OrganizationNotFoundError,
    OrganizationValidationError,
)

from tests.test_organization_routes import (
    PLATFORM_EMAIL,
    csrf_headers,
    demo_client,
    error_code,
)


def test_seeded_tokens_are_isolated_per_customer_and_never_store_plaintext() -> None:
    store = InMemoryOrganizationStore()

    demo = store.list_tokens("org-demo")
    aurora = store.list_tokens("org-aurora")

    assert {item["id"] for item in demo["items"]}.isdisjoint({item["id"] for item in aurora["items"]})
    assert demo["stats"] == {
        "total": 3,
        "activeCount": 2,
        "revokedCount": 1,
        "expiredCount": 0,
        "boundMemberCount": 1,
        "maxTokenCount": MAX_TOKENS_PER_ORGANIZATION,
    }
    for item in demo["items"] + aurora["items"]:
        assert item["masked"].startswith("sk-...")
        assert len(item["masked"]) == len("sk-...") + 4
        assert "secret" not in item
    # One seeded token is shared by the whole customer, with no bound member.
    assert any(item["isShared"] and not item["memberId"] for item in demo["items"])
    assert all(item["memberName"] for item in demo["items"] if item["memberId"])


def test_create_token_returns_the_secret_once_and_keeps_only_a_masked_copy() -> None:
    store = InMemoryOrganizationStore()

    created = store.create_token(
        "org-demo",
        "构建流水线令牌",
        ["gpt-5.2", "claude-opus-5"],
        member_id="member-004",
        duration="30d",
        daily_budget_usd=Decimal("88.50"),
    )

    secret = created["secret"]
    token = created["token"]
    assert secret.startswith("sk-") and len(secret) > 20
    assert token["masked"] == f"sk-...{secret[-4:]}"
    # Catalog order keeps two identical selections serialising identically.
    assert token["models"] == ["claude-opus-5", "gpt-5.2"]
    assert token["memberId"] == "member-004"
    assert token["departmentName"] == "Engineering"
    assert token["status"] == "active"
    assert token["dailyBudgetUsd"] == 88.5
    assert token["expiresAt"] and token["expiresAt"] > token["createdAt"]

    listed = store.list_tokens("org-demo")
    stored = next(item for item in listed["items"] if item["id"] == token["id"])
    assert secret not in str(listed)
    assert stored["masked"] == token["masked"]


def test_create_token_defaults_to_a_shared_never_expiring_token() -> None:
    store = InMemoryOrganizationStore()

    token = store.create_token("org-demo", "共享评测", ["qwen3-coder-plus"])["token"]

    assert token["isShared"] is True
    assert token["memberId"] == ""
    assert token["duration"] == "never"
    assert token["expiresAt"] is None
    assert token["dailyBudgetUsd"] == float(DEFAULT_TOKEN_DAILY_BUDGET_USD)


def test_create_token_rejects_invalid_models_names_budgets_and_members() -> None:
    store = InMemoryOrganizationStore()

    with pytest.raises(OrganizationValidationError):
        store.create_token("org-demo", "无模型令牌", [])
    with pytest.raises(OrganizationValidationError):
        store.create_token("org-demo", "越权模型", ["gpt-4o-not-offered"])
    with pytest.raises(OrganizationValidationError):
        store.create_token("org-demo", "字符串模型", "gpt-5.2")
    with pytest.raises(OrganizationValidationError):
        store.create_token("org-demo", "   ", ["gpt-5.2"])
    with pytest.raises(OrganizationValidationError):
        store.create_token("org-demo", "额度过低", ["gpt-5.2"], daily_budget_usd="0.50")
    with pytest.raises(OrganizationValidationError):
        store.create_token("org-demo", "额度过高", ["gpt-5.2"], daily_budget_usd="5000.01")
    with pytest.raises(OrganizationValidationError):
        store.create_token("org-demo", "分币额度", ["gpt-5.2"], daily_budget_usd="10.001")
    with pytest.raises(OrganizationValidationError):
        store.create_token("org-demo", "错误有效期", ["gpt-5.2"], duration="7d")
    with pytest.raises(OrganizationNotFoundError):
        store.create_token("org-demo", "未知成员", ["gpt-5.2"], member_id="member-not-here")
    # A member from another customer must not become bindable through the id.
    with pytest.raises(OrganizationNotFoundError):
        store.create_token("org-demo", "跨企业成员", ["gpt-5.2"], member_id="member-aurora-001")
    with pytest.raises(OrganizationConflictError):
        store.create_token("org-demo", "停用成员", ["gpt-5.2"], member_id="member-009")


def test_duplicate_active_token_names_are_rejected_but_revoked_names_free_up() -> None:
    store = InMemoryOrganizationStore()

    first = store.create_token("org-demo", "重名令牌", ["gpt-5.2"])["token"]
    with pytest.raises(OrganizationConflictError):
        store.create_token("org-demo", "重名令牌", ["gpt-5.2"])

    store.revoke_token("org-demo", first["id"])
    reused = store.create_token("org-demo", "重名令牌", ["gpt-5.2"])["token"]

    assert reused["id"] != first["id"]


def test_token_count_is_capped_per_customer() -> None:
    store = InMemoryOrganizationStore()
    existing = store.list_tokens("org-demo")["total"]

    for index in range(MAX_TOKENS_PER_ORGANIZATION - existing):
        store.create_token("org-demo", f"批量令牌-{index}", ["gpt-5.2"])

    with pytest.raises(OrganizationConflictError):
        store.create_token("org-demo", "超额令牌", ["gpt-5.2"])
    # The cap counts tokens, not just active ones, so another customer is
    # unaffected by a neighbour reaching its limit.
    assert store.create_token("org-aurora", "邻居令牌", ["gpt-5.2"])["token"]["id"]


def test_revoke_token_is_idempotent_only_once_and_is_customer_scoped() -> None:
    store = InMemoryOrganizationStore()

    revoked = store.revoke_token("org-demo", "token-demo-001")

    assert revoked["status"] == "revoked"
    assert revoked["revokedAt"]
    with pytest.raises(OrganizationConflictError):
        store.revoke_token("org-demo", "token-demo-001")
    with pytest.raises(OrganizationNotFoundError):
        store.revoke_token("org-demo", "token-aurora-001")
    assert store.list_tokens("org-aurora")["stats"]["activeCount"] == 2


def test_token_list_filters_by_keyword_status_and_member() -> None:
    store = InMemoryOrganizationStore()

    by_keyword = store.list_tokens("org-demo", keyword="共享")
    by_model = store.list_tokens("org-demo", keyword="qwen3")
    by_status = store.list_tokens("org-demo", status="revoked")
    by_member = store.list_tokens("org-demo", member_id="member-001")

    assert [item["id"] for item in by_keyword["items"]] == ["token-demo-002"]
    assert [item["id"] for item in by_model["items"]] == ["token-demo-003", "token-demo-002"]
    assert [item["id"] for item in by_status["items"]] == ["token-demo-003"]
    assert [item["id"] for item in by_member["items"]] == ["token-demo-001"]
    with pytest.raises(OrganizationValidationError):
        store.list_tokens("org-demo", status="disabled")
    with pytest.raises(OrganizationNotFoundError):
        store.list_tokens("org-demo", member_id="member-aurora-001")


def test_token_list_exposes_the_demo_model_catalog_and_bindable_members() -> None:
    store = InMemoryOrganizationStore()

    payload = store.list_tokens("org-demo")

    assert payload["availableModels"] == list(ORGANIZATION_TOKEN_MODELS)
    assert store.available_token_models() == list(ORGANIZATION_TOKEN_MODELS)
    bindable = payload["bindableMembers"]
    # Suspended members cannot receive a token, so they are not offered.
    assert "member-009" not in {item["id"] for item in bindable}
    assert all(item["name"] and "departmentName" in item for item in bindable)


def test_reset_restores_seeded_tokens_for_one_customer_only() -> None:
    store = InMemoryOrganizationStore()
    store.create_token("org-demo", "重置前令牌", ["gpt-5.2"])
    store.create_token("org-aurora", "邻居保留令牌", ["gpt-5.2"])

    store.reset("org-demo")

    assert store.list_tokens("org-demo")["total"] == 3
    assert store.list_tokens("org-aurora")["total"] == 4


class _CatalogOnlyClient:
    """只提供模型目录的假上游：任何写操作都视为契约破坏。

    企业令牌本身仍是演示数据——上游只用来回答「有哪些模型」，绝不能被用来开户、
    发 key 或删 key。
    """

    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.catalog_calls = 0

    async def organization_token_models(self) -> list[str]:
        self.catalog_calls += 1
        return list(self.names)

    async def request_backend(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("token routes must not send any upstream write request")

    def __getattr__(self, item: str) -> Any:
        raise AssertionError(f"token routes must not call upstream method {item!r}")


def test_customer_admin_can_list_create_and_revoke_without_upstream_writes(monkeypatch) -> None:
    client = demo_client(monkeypatch, "owner@demo.example")
    fake = _CatalogOnlyClient(list(ORGANIZATION_TOKEN_MODELS))
    monkeypatch.setattr(main, "client", lambda: fake)

    listed = client.get("/api/organization/current/tokens")
    created = client.post(
        "/api/organization/current/tokens",
        json={
            "name": "接口自动化令牌",
            "models": ["gpt-5.2", "claude-sonnet-4-6"],
            "memberId": "member-002",
            "duration": "90d",
            "dailyBudgetUsd": 120,
        },
        headers=csrf_headers(),
    )

    assert listed.status_code == 200
    assert listed.json()["availableModels"] == list(ORGANIZATION_TOKEN_MODELS)
    assert all("secret" not in item for item in listed.json()["items"])
    # 列表与创建各查一次目录，且上游只被用于查目录。
    assert fake.catalog_calls == 2
    assert created.status_code == 200
    body = created.json()
    assert body["ok"] is True
    assert body["secret"].startswith("sk-")
    assert created.headers["cache-control"] == "no-store"

    token_id = body["token"]["id"]
    after_create = client.get("/api/organization/current/tokens")
    assert body["secret"] not in after_create.text

    revoked = client.post(
        f"/api/organization/current/tokens/{token_id}/revoke", headers=csrf_headers()
    )
    assert revoked.status_code == 200
    assert revoked.json()["token"]["status"] == "revoked"

    again = client.post(
        f"/api/organization/current/tokens/{token_id}/revoke", headers=csrf_headers()
    )
    assert again.status_code == 409
    assert error_code(again) == "ORGANIZATION_TOKEN_CONFLICT"


def test_token_writes_require_csrf(monkeypatch) -> None:
    client = demo_client(monkeypatch, "owner@demo.example")

    created = client.post(
        "/api/organization/current/tokens",
        json={"name": "缺少 CSRF", "models": ["gpt-5.2"]},
    )
    revoked = client.post("/api/organization/current/tokens/token-demo-001/revoke")

    assert created.status_code == 403
    assert revoked.status_code == 403


def test_regular_member_cannot_read_or_create_customer_tokens(monkeypatch) -> None:
    client = demo_client(monkeypatch, "avery.chen@demo.example")

    listed = client.get("/api/organization/current/tokens")
    created = client.post(
        "/api/organization/current/tokens",
        json={"name": "成员越权令牌", "models": ["gpt-5.2"]},
        headers=csrf_headers(),
    )

    for response in (listed, created):
        assert response.status_code == 403
        assert error_code(response) == "ORGANIZATION_MANAGE_FORBIDDEN"


def test_customer_admin_cannot_bind_or_revoke_another_customers_records(monkeypatch) -> None:
    client = demo_client(monkeypatch, "owner@demo.example")

    created = client.post(
        "/api/organization/current/tokens",
        json={"name": "跨企业绑定", "models": ["gpt-5.2"], "memberId": "member-aurora-001"},
        headers=csrf_headers(),
    )
    revoked = client.post(
        "/api/organization/current/tokens/token-aurora-001/revoke", headers=csrf_headers()
    )

    for response in (created, revoked):
        assert response.status_code == 404
        assert error_code(response) == "ORGANIZATION_TOKEN_NOT_FOUND"


def test_invalid_token_payloads_are_rejected_before_the_store(monkeypatch) -> None:
    client = demo_client(monkeypatch, "owner@demo.example")

    cases = [
        {"name": "", "models": ["gpt-5.2"]},
        {"name": "无模型", "models": []},
        {"name": "字符串额度", "models": ["gpt-5.2"], "dailyBudgetUsd": "120"},
        {"name": "布尔额度", "models": ["gpt-5.2"], "dailyBudgetUsd": True},
        {"name": "超额额度", "models": ["gpt-5.2"], "dailyBudgetUsd": 5000.01},
        {"name": "错误有效期", "models": ["gpt-5.2"], "duration": "7d"},
        {"name": "多余字段", "models": ["gpt-5.2"], "organizationId": "org-aurora"},
    ]

    for payload in cases:
        response = client.post(
            "/api/organization/current/tokens", json=payload, headers=csrf_headers()
        )
        assert response.status_code == 422, payload

    unavailable_model = client.post(
        "/api/organization/current/tokens",
        json={"name": "未开放模型", "models": ["gpt-4o-not-offered"]},
        headers=csrf_headers(),
    )
    assert unavailable_model.status_code == 400
    assert error_code(unavailable_model) == "ORGANIZATION_TOKEN_INVALID_INPUT"


def test_platform_admin_token_access_is_read_only_and_customer_scoped(monkeypatch) -> None:
    client = demo_client(monkeypatch, PLATFORM_EMAIL, platform_admin=True)

    listed = client.get("/api/platform/organizations/org-aurora/tokens")
    unknown = client.get("/api/platform/organizations/org-not-here/tokens")
    created = client.post(
        "/api/platform/organizations/org-aurora/tokens",
        json={"name": "乙方代建令牌", "models": ["gpt-5.2"]},
        headers=csrf_headers(),
    )
    revoked = client.post(
        "/api/platform/organizations/org-aurora/tokens/token-aurora-001/revoke",
        headers=csrf_headers(),
    )

    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [
        "token-aurora-003",
        "token-aurora-002",
        "token-aurora-001",
    ]
    assert all("secret" not in item for item in listed.json()["items"])
    assert unknown.status_code == 404
    assert error_code(unknown) == "ORGANIZATION_NOT_FOUND"
    # No seller-side write route exists, so both attempts must miss routing
    # instead of reaching a store mutation.
    assert created.status_code == 405
    assert revoked.status_code == 405
    assert client.get("/api/platform/organizations/org-aurora/tokens").json()["stats"][
        "activeCount"
    ] == 2


def test_platform_token_reads_require_a_platform_admin(monkeypatch) -> None:
    client = demo_client(monkeypatch, "owner@demo.example")

    response = client.get("/api/platform/organizations/org-demo/tokens")

    assert response.status_code == 403


def test_available_models_come_from_the_real_gateway_catalog(monkeypatch) -> None:
    client = demo_client(monkeypatch, "owner@demo.example")
    # 上游目录里既有裸模型名，也有带内部线路代号的部署别名。
    fake = _CatalogOnlyClient(
        ["claude-opus-5", "wangsu-claude-opus-5", "gemini-3-pro", "openrouter/gpt-5"]
    )
    monkeypatch.setattr(main, "client", lambda: fake)

    payload = client.get("/api/organization/current/tokens").json()

    # 原始名照原样给出：它才是调用时可用的模型名。
    assert payload["availableModels"] == [
        "claude-opus-5",
        "wangsu-claude-opus-5",
        "gemini-3-pro",
        "openrouter/gpt-5",
    ]
    # 展示层按脱敏名归组：同一模型的多条线路合成一个选项，代号不出现在展示名里。
    options = {option["displayName"]: option["names"] for option in payload["availableModelOptions"]}
    assert options["claude-opus-5"] == ["claude-opus-5", "wangsu-claude-opus-5"]
    assert options["gpt-5"] == ["openrouter/gpt-5"]
    assert options["gemini-3-pro"] == ["gemini-3-pro"]
    for display_name in options:
        assert "wangsu" not in display_name
        assert "openrouter" not in display_name


def test_a_real_catalog_model_can_be_granted_and_an_unlisted_one_cannot(monkeypatch) -> None:
    client = demo_client(monkeypatch, "owner@demo.example")
    monkeypatch.setattr(main, "client", lambda: _CatalogOnlyClient(["wangsu-claude-opus-5"]))

    granted = client.post(
        "/api/organization/current/tokens",
        json={"name": "真实目录令牌", "models": ["wangsu-claude-opus-5"], "duration": "never"},
        headers=csrf_headers(),
    )
    # 回落清单里的模型不在真实目录里，必须被拒。
    rejected = client.post(
        "/api/organization/current/tokens",
        json={"name": "目录外令牌", "models": ["gpt-5.2"], "duration": "never"},
        headers=csrf_headers(),
    )

    assert granted.status_code == 200
    # 令牌存的是上游原始名，展示标签才脱敏。
    assert granted.json()["token"]["models"] == ["wangsu-claude-opus-5"]
    assert rejected.status_code == 400
    assert error_code(rejected) == "ORGANIZATION_TOKEN_INVALID_INPUT"


def test_token_list_keeps_models_that_left_the_catalog(monkeypatch) -> None:
    client = demo_client(monkeypatch, "owner@demo.example")
    # 种子令牌引用的模型全部不在这份目录里。
    monkeypatch.setattr(main, "client", lambda: _CatalogOnlyClient(["brand-new-model"]))

    payload = client.get("/api/organization/current/tokens").json()

    # 已签发令牌是历史事实：既不被过滤掉，也不被改写。
    assert payload["total"] == 3
    assert payload["availableModels"] == ["brand-new-model"]
    assert any(item["models"] for item in payload["items"])
    assert all(item["modelLabels"] for item in payload["items"])


def test_catalog_falls_back_when_the_gateway_is_unavailable(monkeypatch) -> None:
    client = demo_client(monkeypatch, "owner@demo.example")

    def unconfigured_upstream() -> Any:
        # client() 在未配置 LITELLM_BASE_URL 时就是这样失败的。
        raise main.HTTPException(status_code=500, detail="请先在 .env 中配置 LITELLM_BASE_URL")

    monkeypatch.setattr(main, "client", unconfigured_upstream)

    listed = client.get("/api/organization/current/tokens")
    created = client.post(
        "/api/organization/current/tokens",
        json={"name": "降级令牌", "models": ["gpt-5.2"], "duration": "never"},
        headers=csrf_headers(),
    )

    # 上游拿不到目录不能让整页不可用：回落到内置清单，令牌管理照常工作。
    assert listed.status_code == 200
    assert listed.json()["availableModels"] == list(ORGANIZATION_TOKEN_MODELS)
    assert created.status_code == 200


def test_catalog_falls_back_when_the_gateway_returns_nothing(monkeypatch) -> None:
    client = demo_client(monkeypatch, "owner@demo.example")
    monkeypatch.setattr(main, "client", lambda: _CatalogOnlyClient([]))

    payload = client.get("/api/organization/current/tokens").json()

    # 空目录会让创建弹窗变成一个勾不了任何模型的死胡同，所以同样回落。
    assert payload["availableModels"] == list(ORGANIZATION_TOKEN_MODELS)


def test_platform_drilldown_sees_the_same_catalog(monkeypatch) -> None:
    client = demo_client(monkeypatch, PLATFORM_EMAIL)
    monkeypatch.setattr(main, "client", lambda: _CatalogOnlyClient(["wangsu-claude-opus-5"]))

    payload = client.get("/api/platform/organizations/org-aurora/tokens").json()

    assert payload["availableModels"] == ["wangsu-claude-opus-5"]
    assert payload["availableModelOptions"] == [
        {"displayName": "claude-opus-5", "names": ["wangsu-claude-opus-5"]}
    ]


def test_gateway_catalog_merges_backends_and_tolerates_one_failure() -> None:
    """真实客户端取目录：多网关取并集去重，单个网关失败不影响其余。"""

    from backend.cache import TTLCache
    from backend.litellm_client import LiteLLMBackend, LiteLLMClient

    upstream = object.__new__(LiteLLMClient)
    primary = LiteLLMBackend(id="primary", label="Primary", base_url="https://a.test", admin_key="k")
    secondary = LiteLLMBackend(id="her", label="Her", base_url="https://b.test", admin_key="k", source="Her")
    upstream.backends = [primary, secondary]
    upstream._model_cache = TTLCache()
    broken = LiteLLMBackend(id="broken", label="Broken", base_url="https://c.test", admin_key="k")
    upstream.backends.append(broken)

    async def fake_request_backend(backend: Any, _method: str, path: str, **_kwargs: Any) -> Any:
        assert path == "/models"
        if backend.id == "broken":
            raise main.HTTPException(status_code=502, detail="gateway down")
        if backend.id == "primary":
            return {"data": [{"id": "claude-opus-5"}, {"id": "gemini-3-pro"}]}
        return {"data": [{"id": "gemini-3-pro"}, {"id": "qwen3-coder-plus"}]}

    upstream.request_backend = fake_request_backend

    names = asyncio.run(upstream.organization_token_models())

    assert names == ["claude-opus-5", "gemini-3-pro", "qwen3-coder-plus"]
