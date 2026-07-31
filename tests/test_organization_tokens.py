"""Store rules and route boundaries for customer-scoped access tokens."""

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


def test_customer_admin_can_list_create_and_revoke_without_upstream_calls(monkeypatch) -> None:
    client = demo_client(monkeypatch, "owner@demo.example")

    def unexpected_client() -> Any:
        raise AssertionError("Mock token routes must not initialize an upstream client")

    monkeypatch.setattr(main, "client", unexpected_client)

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
