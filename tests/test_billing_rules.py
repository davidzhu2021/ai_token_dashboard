"""充值业务规则测试：金额换算、支付签名、到账后的上游同步编排。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend import billing


def run(coro):
    return asyncio.run(coro)


# ---- 金额换算 ----


def test_money_uses_configured_exchange_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BILLING_EXCHANGE_RATE", "7.3")

    assert billing.money_for_amount(10) == pytest.approx(73.00)
    assert billing.money_for_amount(1) == pytest.approx(7.30)
    # 分位必须四舍五入到两位，否则支付网关会拒收金额。
    assert billing.money_for_amount(0.137) == pytest.approx(1.00)


def test_invalid_exchange_rate_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BILLING_EXCHANGE_RATE", "0")
    assert billing.exchange_rate() == pytest.approx(7.3)

    monkeypatch.setenv("BILLING_EXCHANGE_RATE", "-5")
    assert billing.exchange_rate() == pytest.approx(7.3)

    monkeypatch.setenv("BILLING_EXCHANGE_RATE", "not-a-number")
    assert billing.exchange_rate() == pytest.approx(7.3)


def test_normalize_amount_enforces_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BILLING_MIN_TOPUP_USD", "5")

    assert billing.normalize_amount("5") == pytest.approx(5.0)
    with pytest.raises(ValueError, match="不得低于"):
        billing.normalize_amount("4.99")


def test_normalize_amount_enforces_maximum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BILLING_MIN_TOPUP_USD", "1")
    monkeypatch.setenv("BILLING_MAX_TOPUP_USD", "1000")

    assert billing.normalize_amount("1000") == pytest.approx(1000.0)
    with pytest.raises(ValueError, match="不得高于"):
        billing.normalize_amount("1000.01")


@pytest.mark.parametrize("value", ["0", "-1", "abc", "", "nan", "inf", None])
def test_normalize_amount_rejects_garbage(value: Any) -> None:
    with pytest.raises(ValueError):
        billing.normalize_amount(value)


def test_amount_options_parse_and_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BILLING_TOPUP_OPTIONS", "20, 60 ,x, -5, 200")
    assert billing.topup_amount_options() == [20.0, 60.0, 200.0]

    monkeypatch.setenv("BILLING_TOPUP_OPTIONS", "junk")
    assert billing.topup_amount_options() == [10.0, 50.0, 100.0, 500.0]


def test_trade_no_is_unique_and_opaque() -> None:
    numbers = {billing.generate_trade_no("user-1") for _ in range(200)}

    assert len(numbers) == 200
    # 订单号会被拼进支付网关参数，不能回显用户标识。
    assert all(number.startswith("TQ") for number in numbers)
    assert all("user-1" not in number for number in numbers)


# ---- 渠道开关 ----


def test_channels_hide_epay_until_fully_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPAY_ENABLED", "true")
    monkeypatch.setenv("EPAY_GATEWAY_URL", "https://pay.example.com")
    monkeypatch.setenv("EPAY_PARTNER_ID", "1001")
    monkeypatch.delenv("EPAY_KEY", raising=False)

    # 缺少任一必填项都不能把入口暴露给用户。
    assert billing.epay_enabled() is False
    assert billing.available_channels() == ["redemption"]

    monkeypatch.setenv("EPAY_KEY", "secret")
    assert billing.epay_enabled() is True
    assert billing.available_channels() == ["redemption", "epay"]

    monkeypatch.setenv("EPAY_ENABLED", "false")
    assert billing.available_channels() == ["redemption"]


def test_public_config_never_leaks_merchant_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BILLING_ENABLED", "true")
    monkeypatch.setenv("EPAY_ENABLED", "true")
    monkeypatch.setenv("EPAY_GATEWAY_URL", "https://pay.example.com")
    monkeypatch.setenv("EPAY_PARTNER_ID", "1001")
    monkeypatch.setenv("EPAY_KEY", "super-secret-key")

    config = billing.public_config()
    serialized = repr(config)

    assert "super-secret-key" not in serialized
    assert "1001" not in serialized
    assert config["enabled"] is True
    assert config["channels"] == ["redemption", "epay"]


# ---- 收款码转账（人工确认） ----


def test_manual_qr_requires_switch_and_at_least_one_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MANUAL_PAY_ALIPAY_QR", raising=False)
    monkeypatch.delenv("MANUAL_PAY_WXPAY_QR", raising=False)
    monkeypatch.setenv("MANUAL_PAY_ENABLED", "true")

    # 开关打开但没有收款码时不能暴露入口，否则用户点进去无码可扫。
    assert billing.manual_qr_enabled() is False
    assert "manual_qr" not in billing.available_channels()

    monkeypatch.setenv("MANUAL_PAY_ALIPAY_QR", "/assets/pay/alipay.png")
    assert billing.manual_qr_enabled() is True
    assert billing.available_channels() == ["redemption", "manual_qr"]

    monkeypatch.setenv("MANUAL_PAY_ENABLED", "false")
    assert billing.manual_qr_enabled() is False


def test_manual_qr_methods_follow_configured_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANUAL_PAY_ENABLED", "true")
    monkeypatch.delenv("MANUAL_PAY_ALIPAY_QR", raising=False)
    monkeypatch.setenv("MANUAL_PAY_WXPAY_QR", "https://cdn.example.com/wx.png")

    methods = billing.manual_qr_methods()

    assert [item["method"] for item in methods] == ["wxpay"]
    assert methods[0]["label"] == "微信支付"
    assert methods[0]["qrUrl"] == "https://cdn.example.com/wx.png"


@pytest.mark.parametrize(
    "value",
    ["javascript:alert(1)", "data:image/png;base64,AAAA", "file:///c:/qr.png", "assets/pay/qr.png"],
)
def test_manual_qr_rejects_unsafe_image_sources(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("MANUAL_PAY_ENABLED", "true")
    monkeypatch.setenv("MANUAL_PAY_ALIPAY_QR", value)
    monkeypatch.delenv("MANUAL_PAY_WXPAY_QR", raising=False)

    # 这个值会直接进 <img src>，只放行 http(s) 与站内根路径。
    assert billing.manual_qr_methods() == []
    assert billing.manual_qr_enabled() is False


def test_manual_review_minutes_falls_back_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANUAL_PAY_REVIEW_MINUTES", "not-a-number")
    assert billing.manual_review_minutes() == 30

    monkeypatch.setenv("MANUAL_PAY_REVIEW_MINUTES", "0")
    assert billing.manual_review_minutes() == 30

    monkeypatch.setenv("MANUAL_PAY_REVIEW_MINUTES", "15")
    assert billing.manual_review_minutes() == 15


def test_public_config_exposes_manual_channel_details(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BILLING_ENABLED", "true")
    monkeypatch.setenv("EPAY_ENABLED", "false")
    monkeypatch.setenv("MANUAL_PAY_ENABLED", "true")
    monkeypatch.setenv("MANUAL_PAY_ALIPAY_QR", "/assets/pay/alipay.png")
    monkeypatch.setenv("MANUAL_PAY_CONTACT", "it@company.test")

    config = billing.public_config()

    assert config["channels"] == ["redemption", "manual_qr"]
    assert config["manualPay"]["enabled"] is True
    assert config["manualPay"]["contact"] == "it@company.test"
    assert config["manualPay"]["methods"][0]["qrUrl"] == "/assets/pay/alipay.png"


# ---- 易支付签名 ----


def _signed(params: dict[str, Any], key: str = "test-key") -> dict[str, Any]:
    signed = dict(params)
    signed["sign"] = billing.epay_sign(params, key)
    signed["sign_type"] = "MD5"
    return signed


def test_sign_excludes_sign_fields_and_blank_values() -> None:
    base = {"pid": "1001", "out_trade_no": "TQ1", "money": "73.00"}

    with_noise = {**base, "sign": "whatever", "sign_type": "MD5", "extra": ""}

    # sign/sign_type 与空值参与签名会导致与网关算法不一致。
    assert billing.epay_sign(with_noise, "k") == billing.epay_sign(base, "k")


def test_sign_is_order_independent() -> None:
    forward = {"a": "1", "b": "2", "c": "3"}
    shuffled = {"c": "3", "a": "1", "b": "2"}

    assert billing.epay_sign(forward, "k") == billing.epay_sign(shuffled, "k")


def test_verify_accepts_valid_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPAY_KEY", "test-key")
    params = _signed({"out_trade_no": "TQ1", "money": "73.00", "trade_status": "TRADE_SUCCESS"})

    assert billing.epay_verify(params) is True


def test_verify_rejects_tampered_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPAY_KEY", "test-key")
    params = _signed({"out_trade_no": "TQ1", "money": "73.00", "trade_status": "TRADE_SUCCESS"})

    params["money"] = "7300.00"

    assert billing.epay_verify(params) is False


def test_verify_rejects_wrong_key(monkeypatch: pytest.MonkeyPatch) -> None:
    params = _signed({"out_trade_no": "TQ1", "money": "73.00"}, key="attacker-key")
    monkeypatch.setenv("EPAY_KEY", "test-key")

    assert billing.epay_verify(params) is False


def test_verify_rejects_missing_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPAY_KEY", "test-key")

    assert billing.epay_verify({"out_trade_no": "TQ1"}) is False
    assert billing.epay_verify({"out_trade_no": "TQ1", "sign": ""}) is False


def test_verify_rejects_everything_when_key_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EPAY_KEY", raising=False)
    params = _signed({"out_trade_no": "TQ1", "money": "73.00"}, key="")

    # 未配置密钥时必须一律拒绝，否则任何人都能伪造到账通知。
    assert billing.epay_verify(params) is False


def test_purchase_params_are_signed_and_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPAY_PARTNER_ID", "1001")
    monkeypatch.setenv("EPAY_KEY", "test-key")

    params = billing.epay_purchase_params(
        "TQ1", 73.0, "alipay", "额度充值", "https://x/notify", "https://x/return"
    )

    assert params["type"] == "zfb"
    assert params["money"] == "73.00"
    assert params["out_trade_no"] == "TQ1"
    assert billing.epay_verify(params) is True

    wx = billing.epay_purchase_params("TQ2", 10.0, "wxpay", "额度充值", "https://x/n", "https://x/r")
    assert wx["type"] == "wx"


def test_purchase_params_reject_unknown_method() -> None:
    with pytest.raises(ValueError, match="不支持的支付方式"):
        billing.epay_purchase_params("TQ1", 10.0, "paypal", "额度充值", "https://x/n", "https://x/r")


def test_submit_url_requires_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPAY_GATEWAY_URL", "https://pay.example.com/")
    assert billing.epay_submit_url() == "https://pay.example.com/submit.php"

    monkeypatch.setenv("EPAY_GATEWAY_URL", "")
    with pytest.raises(ValueError, match="支付网关地址"):
        billing.epay_submit_url()


def test_notify_url_prefers_dedicated_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_BASE_URL", "https://app.example.com")
    monkeypatch.setenv("EPAY_NOTIFY_BASE_URL", "https://callback.example.com")
    assert billing.epay_notify_url() == "https://callback.example.com/api/pay/epay/notify"

    monkeypatch.setenv("EPAY_NOTIFY_BASE_URL", "")
    assert billing.epay_notify_url() == "https://app.example.com/api/pay/epay/notify"

    monkeypatch.setenv("APP_BASE_URL", "")
    with pytest.raises(ValueError, match="支付回调地址"):
        billing.epay_notify_url()


# ---- 到账后的上游同步 ----


class _RecordingClient:
    """记录上游写入调用的替身，避免测试触碰真实网关。"""

    def __init__(self, existing_models: list[str] | None = None, keys: list[dict[str, Any]] | None = None) -> None:
        self.existing_models = existing_models if existing_models is not None else ["no-default-models"]
        self.keys = keys or []
        self.budget_calls: list[tuple[str, float]] = []
        self.model_calls: list[tuple[str, list[str]]] = []
        self.key_calls: list[tuple[str, float]] = []

    async def set_user_budget(self, user_id: str, max_budget: float) -> None:
        self.budget_calls.append((user_id, max_budget))

    async def grant_default_models(self, user_id: str, models: list[str]) -> list[str]:
        self.model_calls.append((user_id, list(models)))
        real = [item for item in self.existing_models if item != "no-default-models"]
        if real:
            return []
        return sorted(models)

    async def raise_key_daily_budgets(self, user_id: str, daily_budget: float) -> list[str]:
        self.key_calls.append((user_id, daily_budget))
        return [str(item.get("id")) for item in self.keys]


def test_sync_writes_budget_models_and_key_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOPUP_DEFAULT_MODELS", "gpt-4o, claude-sonnet-4-6")
    monkeypatch.setenv("BILLING_KEY_DAILY_BUDGET_CAP", "100")
    client = _RecordingClient(keys=[{"id": "sk-1"}])

    result = run(billing.sync_upstream_entitlement(client, "local-1", 50.0))

    assert client.budget_calls == [("local-1", 50.0)]
    assert result["models"] == ["claude-sonnet-4-6", "gpt-4o"]
    # 日限额不该超过账户累计充值额度本身。
    assert client.key_calls == [("local-1", 50.0)]
    assert result["keys"] == ["sk-1"]


def test_sync_caps_key_daily_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOPUP_DEFAULT_MODELS", raising=False)
    monkeypatch.setenv("BILLING_KEY_DAILY_BUDGET_CAP", "100")
    client = _RecordingClient(keys=[{"id": "sk-1"}])

    run(billing.sync_upstream_entitlement(client, "local-2", 5000.0))

    # 大额充值也不能突破单日风控上限。
    assert client.key_calls == [("local-2", 100.0)]


def test_sync_skips_model_grant_when_already_entitled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOPUP_DEFAULT_MODELS", "gpt-4o")
    client = _RecordingClient(existing_models=["gpt-5-pro"])

    result = run(billing.sync_upstream_entitlement(client, "local-3", 20.0))

    # 管理员已单独开通更宽权限时，充值不能把它收窄回默认集。
    assert result["models"] == []


def test_sync_skips_models_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOPUP_DEFAULT_MODELS", raising=False)
    client = _RecordingClient()

    result = run(billing.sync_upstream_entitlement(client, "local-4", 20.0))

    assert client.model_calls == []
    assert result["models"] == []


def test_sync_skips_key_update_when_cap_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BILLING_KEY_DAILY_BUDGET_CAP", "0")
    client = _RecordingClient(keys=[{"id": "sk-1"}])

    result = run(billing.sync_upstream_entitlement(client, "local-5", 20.0))

    assert client.key_calls == []
    assert result["keys"] == []


def test_sync_propagates_budget_failure() -> None:
    class _Failing(_RecordingClient):
        async def set_user_budget(self, user_id: str, max_budget: float) -> None:
            raise RuntimeError("上游 503")

    # 失败必须冒泡，由调用方落待重试标记，而不是静默丢掉。
    with pytest.raises(RuntimeError, match="上游 503"):
        run(billing.sync_upstream_entitlement(_Failing(), "local-6", 20.0))
