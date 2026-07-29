"""充值接口的端到端测试：鉴权门禁、兑换链路、支付回调、管理操作。

账本连真实 PostgreSQL（见 ``tests/test_billing_store.py`` 的启动说明），上游
LiteLLM 全部替换成假客户端，绝不触碰真实网关。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend import billing, main
from backend.auth import hash_password
from backend.auth_store import AuthStore
from backend.billing_store import BillingStore

TEST_DSN = os.getenv("BILLING_TEST_DATABASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="需要 BILLING_TEST_DATABASE_URL 指向可写的测试 PostgreSQL",
)


class FakeUpstream:
    """记录额度写入的假上游。"""

    def __init__(self) -> None:
        self.budgets: list[tuple[str, float]] = []
        self.model_grants: list[tuple[str, list[str]]] = []
        self.key_budgets: list[tuple[str, float]] = []
        self.existing_models: list[str] = ["no-default-models"]
        self.fail_budget = False

    async def set_user_budget(self, user_id: str, max_budget: float) -> None:
        if self.fail_budget:
            raise RuntimeError("上游 503")
        self.budgets.append((user_id, max_budget))

    async def grant_default_models(self, user_id: str, models: list[str]) -> list[str]:
        self.model_grants.append((user_id, list(models)))
        if [item for item in self.existing_models if item != "no-default-models"]:
            return []
        self.existing_models = sorted(models)
        return sorted(models)

    async def raise_key_daily_budgets(self, user_id: str, daily_budget: float) -> list[str]:
        self.key_budgets.append((user_id, daily_budget))
        return ["sk-test"]

    async def user_info(self, user_id: str) -> dict[str, Any]:
        # 登录时会用它判定权限状态。
        return {"user_id": user_id, "models": list(self.existing_models), "blocked": False}

    async def close(self) -> None:
        # 应用关闭时会调用。
        return None


PASSWORD = "billing-pass-123"


def _seed_user(store: AuthStore, email: str, user_id: str, *, provisioned: bool = True) -> str:
    user = store.create_user(email, "测试用户", hash_password(PASSWORD), email_verified=True)
    local_id = str(user["id"])
    if provisioned:
        store.set_provisioning_status(local_id, "provisioned", "primary", user_id, "")
    return local_id


def _login(client: TestClient, email: str) -> None:
    """走真实登录接口建立会话，避免手搓签名 cookie。"""
    client.cookies.clear()
    response = client.post(
        "/api/auth/login", json={"email": email, "password": PASSWORD}, headers=csrf(client)
    )
    assert response.status_code == 200, response.text


def _login_admin(client: TestClient, email: str = "boss@company.test") -> None:
    """以企业身份登录取得管理员权限。

    本地密码账号被刻意排除在管理员之外（见 ``auth_user_payload``：密码身份不继承
    SSO 的管理员权限），因此充值管理只能由企业统一认证的管理员访问。
    """
    client.cookies.clear()
    response = client.post("/api/auth/dev-login", json={"email": email}, headers=csrf(client))
    assert response.status_code == 200, response.text
    assert response.json()["isAdmin"] is True, "ADMIN_EMAILS 未覆盖该邮箱"


@pytest.fixture
def billing_env(tmp_path, monkeypatch):
    """提供已启动 lifespan 的 TestClient 与同循环内执行协程的入口。

    asyncpg 连接池绑定创建它的事件循环，因此必须在 TestClient 自己的循环里建池
    （由 lifespan 完成），断言用的协程也要送回同一个循环执行，否则连接会在操作
    中途被判定关闭。
    """
    opened: list[TestClient] = []

    def build(*, admin_email: str = "boss@company.test"):
        if opened:
            raise AssertionError("每个测试只应构造一个充值环境")
        client, auth, store, upstream = _configure(tmp_path, monkeypatch, admin_email)
        # 进入上下文触发 lifespan，连接池就建在 TestClient 自己的事件循环里。
        client.__enter__()
        opened.append(client)

        def call(coro_factory):
            return client.portal.call(coro_factory)

        call(lambda: store.pool.execute("TRUNCATE billing_order, billing_account, billing_redemption"))
        return client, auth, store, upstream, call

    yield build

    for client in opened:
        client.__exit__(None, None, None)


def _configure(tmp_path, monkeypatch, admin_email: str):
    auth = AuthStore(tmp_path / "auth.sqlite3")
    upstream = FakeUpstream()
    # 池大小压到 1：每个测试各建一个池，连跑时不能让新建连接的速率打满测试库。
    store = BillingStore(TEST_DSN, min_size=1, max_size=1)

    monkeypatch.setattr(main, "_auth_store", auth)
    monkeypatch.setattr(main, "_litellm_client", upstream)
    monkeypatch.setattr(main, "_billing_store", store)
    monkeypatch.setenv("BILLING_ENABLED", "true")
    monkeypatch.setenv("BILLING_EXCHANGE_RATE", "7.3")
    monkeypatch.setenv("BILLING_MIN_TOPUP_USD", "1")
    monkeypatch.setenv("BILLING_MAX_TOPUP_USD", "10000")
    monkeypatch.setenv("BILLING_KEY_DAILY_BUDGET_CAP", "100")
    monkeypatch.setenv("TOPUP_DEFAULT_MODELS", "gpt-4o")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("PASSWORD_LOGIN_ENABLED", "true")
    monkeypatch.setenv("APP_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("ADMIN_EMAILS", admin_email)
    monkeypatch.setenv("DEV_LOGIN_ENABLED", "true")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAIN", "company.test")
    main.local_entitlement_cache.clear()
    return TestClient(main.app), auth, store, upstream


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.get("/api/auth/csrf").json()["csrfToken"]}


def make_code(call, store: BillingStore, amount: float = 50.0, **kwargs: Any) -> str:
    created = call(lambda: store.create_redemptions(1, amount, **kwargs))
    return created[0]["code"]


# ---- 门禁 ----


def test_billing_requires_login(billing_env) -> None:
    client, _auth, _store, _upstream, call = billing_env()

    assert client.get("/api/me/billing").status_code == 401


def test_billing_returns_404_when_disabled(billing_env, monkeypatch) -> None:
    client, auth, _store, _upstream, call = billing_env()
    monkeypatch.setattr(main, "_billing_store", None)
    local_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")

    # 未启用充值的部署必须完全看不到这些能力。
    assert client.get("/api/me/billing").status_code == 404
    assert client.post("/api/me/billing/redeem", json={"code": "X"}, headers=csrf(client)).status_code == 404
    assert client.get("/api/admin/billing/orders").status_code == 404


def test_redeem_requires_csrf(billing_env) -> None:
    client, auth, store, _upstream, call = billing_env()
    local_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")
    code = make_code(call, store)

    response = client.post("/api/me/billing/redeem", json={"code": code})

    assert response.status_code == 403


def test_billing_rejects_account_still_provisioning(billing_env) -> None:
    client, auth, _store, _upstream, call = billing_env()
    local_id = _seed_user(auth, "u@example.com", "local-u", provisioned=False)
    _login(client, "u@example.com")

    response = client.get("/api/me/billing")

    assert response.status_code == 409


def test_billing_is_open_to_users_without_entitlement(billing_env) -> None:
    client, auth, _store, upstream, call = billing_env()
    upstream.existing_models = ["no-default-models"]
    local_id = _seed_user(auth, "new@example.com", "local-new")
    _login(client, "new@example.com")

    response = client.get("/api/me/billing")

    # 新用户尚未获得模型权限，但必须能进充值页，否则无法自助开通。
    assert response.status_code == 200
    assert response.json()["account"]["balanceUsd"] == pytest.approx(0.0)


# ---- 兑换码链路 ----


def test_redeem_credits_balance_and_syncs_upstream(billing_env) -> None:
    client, auth, store, upstream, call = billing_env()
    local_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")
    code = make_code(call, store, 50.0)

    response = client.post("/api/me/billing/redeem", json={"code": code}, headers=csrf(client))

    assert response.status_code == 200
    body = response.json()
    assert body["amountUsd"] == pytest.approx(50.0)
    assert body["account"]["balanceUsd"] == pytest.approx(50.0)
    assert body["entitlementSynced"] is True

    # 累计充值写成上游总额度，首次充值同时放开模型与密钥日限额。
    assert upstream.budgets == [("local-u", 50.0)]
    assert upstream.model_grants == [("local-u", ["gpt-4o"])]
    assert upstream.key_budgets == [("local-u", 50.0)]


def test_redeem_rejects_reused_code(billing_env) -> None:
    client, auth, store, _upstream, call = billing_env()
    first = _seed_user(auth, "a@example.com", "local-a")
    _login(client, "a@example.com")
    code = make_code(call, store, 10.0)
    assert client.post("/api/me/billing/redeem", json={"code": code}, headers=csrf(client)).status_code == 200

    second = _seed_user(auth, "b@example.com", "local-b")
    _login(client, "b@example.com")
    response = client.post("/api/me/billing/redeem", json={"code": code}, headers=csrf(client))

    assert response.status_code == 400
    assert "已被使用" in response.json()["detail"]


def test_redeem_rejects_unknown_code(billing_env) -> None:
    client, auth, _store, _upstream, call = billing_env()
    local_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")

    response = client.post(
        "/api/me/billing/redeem", json={"code": "ZZZZZZZZZZZZZZZZZZZZ"}, headers=csrf(client)
    )

    assert response.status_code == 400
    assert "无效" in response.json()["detail"]


def test_redeem_keeps_balance_when_upstream_write_fails(billing_env) -> None:
    client, auth, store, upstream, call = billing_env()
    upstream.fail_budget = True
    local_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")
    code = make_code(call, store, 30.0)

    response = client.post("/api/me/billing/redeem", json={"code": code}, headers=csrf(client))

    # 钱已到账，上游失败只降级为"待同步"，绝不回滚余额。
    assert response.status_code == 200
    assert response.json()["account"]["balanceUsd"] == pytest.approx(30.0)
    assert response.json()["entitlementSynced"] is False
    assert call(lambda: store.pending_sync_count()) == 1


def test_admin_can_retry_failed_upstream_sync(billing_env) -> None:
    client, auth, store, upstream, call = billing_env()
    upstream.fail_budget = True
    local_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")
    code = make_code(call, store, 30.0)
    client.post("/api/me/billing/redeem", json={"code": code}, headers=csrf(client))
    assert call(lambda: store.pending_sync_count()) == 1

    upstream.fail_budget = False
    _login_admin(client)
    response = client.post("/api/admin/billing/sync/retry", headers=csrf(client))

    assert response.status_code == 200
    assert response.json()["repaired"] == 1
    assert response.json()["pendingSyncCount"] == 0


def test_orders_list_shows_redemption_history(billing_env) -> None:
    client, auth, store, _upstream, call = billing_env()
    local_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")
    client.post("/api/me/billing/redeem", json={"code": make_code(call, store, 25.0)}, headers=csrf(client))

    body = client.get("/api/me/billing").json()

    assert body["orders"]["total"] == 1
    assert body["orders"]["items"][0]["channel"] == "redemption"
    assert body["account"]["topupTotalUsd"] == pytest.approx(25.0)


# ---- 在线支付下单 ----


def test_create_order_blocked_when_epay_unconfigured(billing_env, monkeypatch) -> None:
    client, auth, _store, _upstream, call = billing_env()
    monkeypatch.setenv("EPAY_ENABLED", "false")
    local_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")

    response = client.post(
        "/api/me/billing/orders", json={"amount": 10, "paymentMethod": "alipay"}, headers=csrf(client)
    )

    assert response.status_code == 503


def _enable_epay(monkeypatch) -> None:
    monkeypatch.setenv("EPAY_ENABLED", "true")
    monkeypatch.setenv("EPAY_GATEWAY_URL", "https://pay.example.com")
    monkeypatch.setenv("EPAY_PARTNER_ID", "1001")
    monkeypatch.setenv("EPAY_KEY", "merchant-key")
    monkeypatch.setenv("EPAY_NOTIFY_BASE_URL", "https://app.example.com")


def test_create_order_returns_signed_gateway_params(billing_env, monkeypatch) -> None:
    client, auth, store, _upstream, call = billing_env()
    _enable_epay(monkeypatch)
    local_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")

    response = client.post(
        "/api/me/billing/orders", json={"amount": 10, "paymentMethod": "alipay"}, headers=csrf(client)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["moneyCny"] == pytest.approx(73.0)
    assert body["submitUrl"] == "https://pay.example.com/submit.php"
    assert body["params"]["type"] == "zfb"
    assert billing.epay_verify(body["params"]) is True

    order = call(lambda: store.get_order(body["tradeNo"]))
    assert order["status"] == "pending"
    assert order["exchangeRate"] == pytest.approx(7.3)


def test_create_order_enforces_minimum(billing_env, monkeypatch) -> None:
    client, auth, _store, _upstream, call = billing_env()
    _enable_epay(monkeypatch)
    monkeypatch.setenv("BILLING_MIN_TOPUP_USD", "10")
    local_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")

    response = client.post(
        "/api/me/billing/orders", json={"amount": 5, "paymentMethod": "alipay"}, headers=csrf(client)
    )

    assert response.status_code == 400
    assert "不得低于" in response.json()["detail"]


def test_order_detail_is_scoped_to_owner(billing_env, monkeypatch) -> None:
    client, auth, _store, _upstream, call = billing_env()
    _enable_epay(monkeypatch)
    owner = _seed_user(auth, "owner@example.com", "local-owner")
    _login(client, "owner@example.com")
    trade_no = client.post(
        "/api/me/billing/orders", json={"amount": 10, "paymentMethod": "alipay"}, headers=csrf(client)
    ).json()["tradeNo"]

    other = _seed_user(auth, "other@example.com", "local-other")
    _login(client, "other@example.com")
    response = client.get(f"/api/me/billing/orders/{trade_no}")

    # 不能用订单号枚举他人充值记录。
    assert response.status_code == 404


# ---- 支付回调 ----


def _notify_params(trade_no: str, money: str = "73.00", status: str = "TRADE_SUCCESS") -> dict[str, str]:
    params = {
        "pid": "1001",
        "out_trade_no": trade_no,
        "trade_no": "GW-123",
        "trade_status": status,
        "money": money,
        "name": "通衢 API 额度充值",
    }
    params["sign"] = billing.epay_sign(params, "merchant-key")
    params["sign_type"] = "MD5"
    return params


def _place_order(client: TestClient, amount: float = 10.0) -> str:
    return client.post(
        "/api/me/billing/orders", json={"amount": amount, "paymentMethod": "alipay"}, headers=csrf(client)
    ).json()["tradeNo"]


def test_notify_is_reachable_without_login(billing_env, monkeypatch) -> None:
    client, auth, store, upstream, call = billing_env()
    _enable_epay(monkeypatch)
    local_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")
    trade_no = _place_order(client)

    # 支付网关不会带任何会话凭证。
    client.cookies.clear()
    response = client.post("/api/pay/epay/notify", data=_notify_params(trade_no))

    assert response.status_code == 200
    assert response.text == "success"
    assert call(lambda: store.get_account(local_id))["balanceUsd"] == pytest.approx(10.0)
    assert upstream.budgets == [("local-u", 10.0)]


def test_notify_rejects_bad_signature(billing_env, monkeypatch) -> None:
    client, auth, store, _upstream, call = billing_env()
    _enable_epay(monkeypatch)
    local_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")
    trade_no = _place_order(client)

    params = _notify_params(trade_no)
    params["sign"] = "0" * 32
    client.cookies.clear()
    response = client.post("/api/pay/epay/notify", data=params)

    assert response.text == "fail"
    assert call(lambda: store.get_account(local_id))["balanceUsd"] == pytest.approx(0.0)


def test_notify_rejects_tampered_amount(billing_env, monkeypatch) -> None:
    client, auth, store, _upstream, call = billing_env()
    _enable_epay(monkeypatch)
    local_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")
    trade_no = _place_order(client, 10.0)

    # 签名自洽但金额与下单快照不符：可能是别笔订单的重放。
    client.cookies.clear()
    response = client.post("/api/pay/epay/notify", data=_notify_params(trade_no, money="0.01"))

    assert response.text == "fail"
    assert call(lambda: store.get_account(local_id))["balanceUsd"] == pytest.approx(0.0)


def test_notify_is_idempotent(billing_env, monkeypatch) -> None:
    client, auth, store, upstream, call = billing_env()
    _enable_epay(monkeypatch)
    local_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")
    trade_no = _place_order(client)
    params = _notify_params(trade_no)

    client.cookies.clear()
    for _ in range(3):
        assert client.post("/api/pay/epay/notify", data=params).text == "success"

    # 网关重推不能重复加钱。
    assert call(lambda: store.get_account(local_id))["balanceUsd"] == pytest.approx(10.0)
    assert len(upstream.budgets) == 1


def test_notify_accepts_get_callback(billing_env, monkeypatch) -> None:
    client, auth, store, _upstream, call = billing_env()
    _enable_epay(monkeypatch)
    local_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")
    trade_no = _place_order(client)

    client.cookies.clear()
    response = client.get("/api/pay/epay/notify", params=_notify_params(trade_no))

    assert response.text == "success"
    assert call(lambda: store.get_account(local_id))["balanceUsd"] == pytest.approx(10.0)


def test_notify_ignores_non_success_status(billing_env, monkeypatch) -> None:
    client, auth, store, _upstream, call = billing_env()
    _enable_epay(monkeypatch)
    local_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")
    trade_no = _place_order(client)

    client.cookies.clear()
    response = client.post("/api/pay/epay/notify", data=_notify_params(trade_no, status="WAIT_BUYER_PAY"))

    # 回 success 停止重推，但不落账。
    assert response.text == "success"
    assert call(lambda: store.get_account(local_id))["balanceUsd"] == pytest.approx(0.0)


def test_notify_rejects_unknown_order(billing_env, monkeypatch) -> None:
    client, _auth, _store, _upstream, call = billing_env()
    _enable_epay(monkeypatch)

    response = client.post("/api/pay/epay/notify", data=_notify_params("TQ-NOPE"))

    assert response.text == "fail"


# ---- 管理端 ----


def test_redemption_management_requires_admin(billing_env) -> None:
    client, auth, _store, _upstream, call = billing_env()
    local_id = _seed_user(auth, "plain@example.com", "local-plain")
    _login(client, "plain@example.com")

    assert client.get("/api/admin/billing/redemptions").status_code == 403
    assert client.post(
        "/api/admin/billing/redemptions", json={"count": 1, "amount": 10}, headers=csrf(client)
    ).status_code == 403
    assert client.get("/api/admin/billing/orders").status_code == 403


def test_admin_creates_redemptions_and_plaintext_appears_once(billing_env) -> None:
    client, auth, _store, _upstream, call = billing_env()
    _login_admin(client)

    created = client.post(
        "/api/admin/billing/redemptions",
        json={"count": 3, "amount": 20, "name": "七月批次"},
        headers=csrf(client),
    )

    assert created.status_code == 200
    assert created.headers["Cache-Control"] == "no-store"
    codes = [item["code"] for item in created.json()["items"]]
    assert len(set(codes)) == 3

    listed = client.get("/api/admin/billing/redemptions").json()
    assert listed["total"] == 3
    # 列表接口只能给出尾 4 位提示，不能回显明文。
    serialized = repr(listed)
    assert all(code not in serialized for code in codes)


def test_admin_can_disable_redemption(billing_env) -> None:
    client, auth, store, _upstream, call = billing_env()
    _login_admin(client)
    created = client.post(
        "/api/admin/billing/redemptions", json={"count": 1, "amount": 20}, headers=csrf(client)
    ).json()["items"][0]

    response = client.post(
        f"/api/admin/billing/redemptions/{created['id']}/disable", headers=csrf(client)
    )
    assert response.status_code == 200

    user_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")
    redeemed = client.post(
        "/api/me/billing/redeem", json={"code": created["code"]}, headers=csrf(client)
    )
    assert redeemed.status_code == 400
    assert "已停用" in redeemed.json()["detail"]


def test_admin_manual_completion_settles_pending_order(billing_env, monkeypatch) -> None:
    client, auth, store, upstream, call = billing_env()
    _enable_epay(monkeypatch)
    user_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")
    trade_no = _place_order(client, 40.0)
    _login_admin(client)
    response = client.post(f"/api/admin/billing/orders/{trade_no}/complete", headers=csrf(client))

    assert response.status_code == 200
    assert response.json()["settled"] is True
    assert call(lambda: store.get_account(user_id))["balanceUsd"] == pytest.approx(40.0)
    assert upstream.budgets == [("local-u", 40.0)]

    # 补单后再补一次要被拒，避免重复加钱。
    again = client.post(f"/api/admin/billing/orders/{trade_no}/complete", headers=csrf(client))
    assert again.status_code == 400


def test_health_reports_pending_upstream_sync(billing_env, monkeypatch) -> None:
    client, auth, store, upstream, call = billing_env()
    healthy = client.get("/api/health").json()
    assert healthy["billing"] == {
        "enabled": True,
        "connected": True,
        "status": "ok",
        "pendingSyncCount": 0,
        "pendingReviewCount": 0,
    }

    upstream.fail_budget = True
    local_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")
    client.post(
        "/api/me/billing/redeem", json={"code": make_code(call, store, 10.0)}, headers=csrf(client)
    )

    degraded = client.get("/api/health").json()

    # 钱已收到但上游额度没写上，必须在健康检查里暴露出来。
    assert degraded["billing"]["pendingSyncCount"] == 1
    assert degraded["billing"]["status"] == "degraded"
    assert degraded["status"] == "degraded"


def test_health_reports_billing_disabled(billing_env, monkeypatch) -> None:
    client, _auth, _store, _upstream, _call = billing_env()
    monkeypatch.setattr(main, "_billing_store", None)

    body = client.get("/api/health").json()

    assert body["billing"] == {"enabled": False, "status": "disabled"}


def test_admin_order_search_and_pending_count(billing_env, monkeypatch) -> None:
    client, auth, store, upstream, call = billing_env()
    _enable_epay(monkeypatch)
    upstream.fail_budget = True
    user_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")
    trade_no = _place_order(client)
    client.cookies.clear()
    client.post("/api/pay/epay/notify", data=_notify_params(trade_no))
    _login_admin(client)
    body = client.get("/api/admin/billing/orders", params={"keyword": trade_no}).json()

    assert [item["tradeNo"] for item in body["items"]] == [trade_no]
    assert body["pendingSyncCount"] == 1


# ---- 收款码转账（人工确认到账） ----


def _enable_manual_qr(monkeypatch) -> None:
    monkeypatch.setenv("EPAY_ENABLED", "false")
    monkeypatch.setenv("MANUAL_PAY_ENABLED", "true")
    monkeypatch.setenv("MANUAL_PAY_ALIPAY_QR", "/assets/pay/alipay.png")
    monkeypatch.delenv("MANUAL_PAY_WXPAY_QR", raising=False)
    monkeypatch.setenv("MANUAL_PAY_REVIEW_MINUTES", "20")


def _place_manual_order(client: TestClient, amount: float = 10.0, method: str = "alipay") -> dict[str, Any]:
    response = client.post(
        "/api/me/billing/orders",
        json={"amount": amount, "paymentMethod": method, "channel": "manual_qr"},
        headers=csrf(client),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_manual_order_returns_qr_and_stays_unpaid(billing_env, monkeypatch) -> None:
    client, auth, store, upstream, call = billing_env()
    _enable_manual_qr(monkeypatch)
    user_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")

    body = _place_manual_order(client, 10.0)

    assert body["channel"] == "manual_qr"
    assert body["moneyCny"] == pytest.approx(73.0)
    assert body["qrUrl"] == "/assets/pay/alipay.png"
    assert body["reviewMinutes"] == 20
    # 收款码没有回调，下单本身绝不能加钱或写上游额度。
    assert call(lambda: store.get_account(user_id))["balanceUsd"] == pytest.approx(0.0)
    assert upstream.budgets == []


def test_manual_channel_falls_back_when_epay_unconfigured(billing_env, monkeypatch) -> None:
    client, auth, _store, _upstream, call = billing_env()
    _enable_manual_qr(monkeypatch)
    user_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")

    # 前端请求自动支付但未配商户号时退到收款码，而不是把用户堵在 503。
    response = client.post(
        "/api/me/billing/orders",
        json={"amount": 10, "paymentMethod": "alipay", "channel": "epay"},
        headers=csrf(client),
    )

    assert response.status_code == 200
    assert response.json()["channel"] == "manual_qr"


def test_manual_order_rejects_unconfigured_method(billing_env, monkeypatch) -> None:
    client, auth, _store, _upstream, call = billing_env()
    _enable_manual_qr(monkeypatch)
    user_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")

    # 只配了支付宝收款码，微信不能下单，否则用户拿不到可扫的码。
    response = client.post(
        "/api/me/billing/orders",
        json={"amount": 10, "paymentMethod": "wxpay", "channel": "manual_qr"},
        headers=csrf(client),
    )

    assert response.status_code == 400


def test_manual_channel_blocked_when_disabled(billing_env, monkeypatch) -> None:
    client, auth, _store, _upstream, call = billing_env()
    monkeypatch.setenv("EPAY_ENABLED", "false")
    monkeypatch.setenv("MANUAL_PAY_ENABLED", "false")
    user_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")

    response = client.post(
        "/api/me/billing/orders",
        json={"amount": 10, "paymentMethod": "alipay", "channel": "manual_qr"},
        headers=csrf(client),
    )

    assert response.status_code == 503


def test_submit_proof_requires_csrf(billing_env, monkeypatch) -> None:
    client, auth, _store, _upstream, call = billing_env()
    _enable_manual_qr(monkeypatch)
    user_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")
    trade_no = _place_manual_order(client)["tradeNo"]

    response = client.post(
        f"/api/me/billing/orders/{trade_no}/submit", json={"payerNote": "尾号 1234"}
    )

    assert response.status_code == 403


def test_submit_proof_does_not_credit_balance(billing_env, monkeypatch) -> None:
    client, auth, store, upstream, call = billing_env()
    _enable_manual_qr(monkeypatch)
    user_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")
    trade_no = _place_manual_order(client, 15.0)["tradeNo"]

    response = client.post(
        f"/api/me/billing/orders/{trade_no}/submit",
        json={"payerNote": "尾号 1234"},
        headers=csrf(client),
    )

    # 用户自称已付款不能成为入账依据，必须等管理员核对收款流水。
    assert response.status_code == 200
    assert response.json()["order"]["status"] == "pending"
    assert call(lambda: store.get_account(user_id))["balanceUsd"] == pytest.approx(0.0)
    assert upstream.budgets == []
    assert call(lambda: store.pending_review_count()) == 1


def test_submit_proof_rejects_other_users_order(billing_env, monkeypatch) -> None:
    client, auth, store, _upstream, call = billing_env()
    _enable_manual_qr(monkeypatch)
    _seed_user(auth, "owner@example.com", "local-owner")
    _login(client, "owner@example.com")
    trade_no = _place_manual_order(client)["tradeNo"]

    _seed_user(auth, "other@example.com", "local-other")
    _login(client, "other@example.com")
    response = client.post(
        f"/api/me/billing/orders/{trade_no}/submit",
        json={"payerNote": "我付过了"},
        headers=csrf(client),
    )

    assert response.status_code == 404
    assert call(lambda: store.get_order(trade_no))["payerNote"] == ""


def test_admin_confirms_manual_order_and_grants_entitlement(billing_env, monkeypatch) -> None:
    client, auth, store, upstream, call = billing_env()
    _enable_manual_qr(monkeypatch)
    user_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")
    trade_no = _place_manual_order(client, 30.0)["tradeNo"]
    client.post(
        f"/api/me/billing/orders/{trade_no}/submit",
        json={"payerNote": "尾号 4321"},
        headers=csrf(client),
    )

    _login_admin(client)
    queue = client.get("/api/admin/billing/orders").json()
    assert queue["pendingReviewCount"] == 1
    assert [item["tradeNo"] for item in queue["pendingReviews"]] == [trade_no]
    assert queue["pendingReviews"][0]["payerNote"] == "尾号 4321"

    response = client.post(
        f"/api/admin/billing/orders/{trade_no}/complete",
        json={"note": "已核对收款"},
        headers=csrf(client),
    )

    assert response.status_code == 200
    assert call(lambda: store.get_account(user_id))["balanceUsd"] == pytest.approx(30.0)
    assert upstream.budgets == [("local-u", 30.0)]
    order = call(lambda: store.get_order(trade_no))
    assert order["reviewedBy"] == "boss@company.test"
    assert order["reviewNote"] == "已核对收款"
    assert call(lambda: store.pending_review_count()) == 0


def test_admin_reject_leaves_balance_and_records_reason(billing_env, monkeypatch) -> None:
    client, auth, store, upstream, call = billing_env()
    _enable_manual_qr(monkeypatch)
    user_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")
    trade_no = _place_manual_order(client, 30.0)["tradeNo"]
    client.post(
        f"/api/me/billing/orders/{trade_no}/submit", json={"payerNote": "转错了"}, headers=csrf(client)
    )

    _login_admin(client)
    response = client.post(
        f"/api/admin/billing/orders/{trade_no}/reject",
        json={"note": "未查到该笔付款"},
        headers=csrf(client),
    )

    assert response.status_code == 200
    assert call(lambda: store.get_account(user_id))["balanceUsd"] == pytest.approx(0.0)
    assert upstream.budgets == []
    order = call(lambda: store.get_order(trade_no))
    assert order["status"] == "failed"
    assert order["reviewNote"] == "未查到该笔付款"

    # 驳回后不能再确认到账，否则同一笔订单会被两次处理。
    again = client.post(f"/api/admin/billing/orders/{trade_no}/complete", headers=csrf(client))
    assert again.status_code == 400


def test_manual_review_actions_require_admin(billing_env, monkeypatch) -> None:
    client, auth, store, _upstream, call = billing_env()
    _enable_manual_qr(monkeypatch)
    user_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")
    trade_no = _place_manual_order(client, 30.0)["tradeNo"]

    # 普通用户不能自己确认到账。
    assert client.post(
        f"/api/admin/billing/orders/{trade_no}/complete", headers=csrf(client)
    ).status_code == 403
    assert client.post(
        f"/api/admin/billing/orders/{trade_no}/reject", headers=csrf(client)
    ).status_code == 403
    assert call(lambda: store.get_account(user_id))["balanceUsd"] == pytest.approx(0.0)


def test_health_reports_pending_manual_reviews(billing_env, monkeypatch) -> None:
    client, auth, store, _upstream, call = billing_env()
    _enable_manual_qr(monkeypatch)
    user_id = _seed_user(auth, "u@example.com", "local-u")
    _login(client, "u@example.com")
    trade_no = _place_manual_order(client)["tradeNo"]
    client.post(
        f"/api/me/billing/orders/{trade_no}/submit", json={"payerNote": "尾号 1"}, headers=csrf(client)
    )

    body = client.get("/api/health").json()

    # 待人工确认只是等处理，不算故障，因此不降级整体状态。
    assert body["billing"]["pendingReviewCount"] == 1
    assert body["billing"]["status"] == "ok"
