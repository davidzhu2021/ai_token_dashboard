"""充值业务规则：配置读取、金额换算、支付网关签名与到账后的额度同步。

路由层只做鉴权与参数校验，换算规则和上游写入编排都留在这里，便于单独测试。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

PAYMENT_METHODS = {"alipay": "zfb", "wxpay": "wx"}


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value


def billing_enabled() -> bool:
    return env_bool("BILLING_ENABLED", False)


def exchange_rate() -> float:
    """人民币与 1 美元额度的兑换比例。"""
    rate = env_float("BILLING_EXCHANGE_RATE", 7.3)
    return rate if rate > 0 else 7.3


def min_topup_usd() -> float:
    value = env_float("BILLING_MIN_TOPUP_USD", 1.0)
    return value if value > 0 else 1.0


def max_topup_usd() -> float:
    """单笔充值上限，防止手滑输入天文数字造成对账麻烦。"""
    value = env_float("BILLING_MAX_TOPUP_USD", 10000.0)
    return value if value > 0 else 10000.0


def key_daily_budget_cap() -> float:
    return max(0.0, env_float("BILLING_KEY_DAILY_BUDGET_CAP", 100.0))


def topup_default_models() -> list[str]:
    raw = os.getenv("TOPUP_DEFAULT_MODELS", "")
    return sorted({item.strip() for item in raw.split(",") if item.strip()})


def topup_amount_options() -> list[float]:
    raw = os.getenv("BILLING_TOPUP_OPTIONS", "10,50,100,500")
    options: list[float] = []
    for item in raw.split(","):
        text = item.strip()
        if not text:
            continue
        try:
            value = float(text)
        except ValueError:
            continue
        if value > 0:
            options.append(value)
    return options or [10.0, 50.0, 100.0, 500.0]


def epay_enabled() -> bool:
    return (
        env_bool("EPAY_ENABLED", False)
        and bool(os.getenv("EPAY_GATEWAY_URL", "").strip())
        and bool(os.getenv("EPAY_PARTNER_ID", "").strip())
        and bool(os.getenv("EPAY_KEY", "").strip())
    )


# ---- 收款码转账（人工确认） ----

MANUAL_METHOD_LABELS = {"alipay": "支付宝", "wxpay": "微信支付"}


def _manual_qr_image(method: str) -> str:
    """读取收款码图片地址。

    只接受 http(s) 或站内根路径，避免把 ``javascript:``/``data:`` 之类的值直接
    塞进前端 ``<img src>``。
    """
    raw = os.getenv(f"MANUAL_PAY_{method.upper()}_QR", "").strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://", "/")):
        return raw
    return ""


def manual_qr_methods() -> list[dict[str, str]]:
    """已配置收款码的支付方式，顺序固定便于前端默认选中第一个。"""
    methods: list[dict[str, str]] = []
    for method in ("alipay", "wxpay"):
        image = _manual_qr_image(method)
        if image:
            methods.append(
                {"method": method, "label": MANUAL_METHOD_LABELS[method], "qrUrl": image}
            )
    return methods


def manual_qr_enabled() -> bool:
    """收款码渠道是否可用：开关打开且至少配了一张收款码。"""
    return env_bool("MANUAL_PAY_ENABLED", False) and bool(manual_qr_methods())


def manual_qr_notice() -> str:
    return os.getenv(
        "MANUAL_PAY_NOTICE",
        "请按订单金额扫码付款，并在付款备注里填写订单号，便于快速核对。",
    ).strip()


def manual_qr_contact() -> str:
    return os.getenv("MANUAL_PAY_CONTACT", "").strip()


def manual_review_minutes() -> int:
    """对外承诺的人工确认时长，仅用于文案。"""
    try:
        value = int(os.getenv("MANUAL_PAY_REVIEW_MINUTES", "30"))
    except ValueError:
        return 30
    return value if value > 0 else 30


def manual_qr_config() -> dict[str, Any]:
    return {
        "enabled": manual_qr_enabled(),
        "methods": manual_qr_methods(),
        "notice": manual_qr_notice(),
        "contact": manual_qr_contact(),
        "reviewMinutes": manual_review_minutes(),
    }


def available_channels() -> list[str]:
    channels = ["redemption"]
    if epay_enabled():
        channels.append("epay")
    if manual_qr_enabled():
        channels.append("manual_qr")
    return channels


def money_for_amount(amount_usd: float) -> float:
    """按当前汇率算出应付人民币，分位四舍五入。"""
    value = (Decimal(str(amount_usd)) * Decimal(str(exchange_rate()))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(value)


def normalize_amount(amount_usd: Any) -> float:
    """校验并归一化充值额度，返回 6 位精度的美元额度。"""
    try:
        value = Decimal(str(amount_usd))
    except Exception as exc:  # noqa: BLE001 - 输入来自用户，任何解析失败都视为非法
        raise ValueError("请输入有效的充值额度") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("请输入有效的充值额度")
    quantized = value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    minimum = Decimal(str(min_topup_usd()))
    maximum = Decimal(str(max_topup_usd()))
    if quantized < minimum:
        raise ValueError(f"单笔充值额度不得低于 {minimum.normalize()}")
    if quantized > maximum:
        raise ValueError(f"单笔充值额度不得高于 {maximum.normalize()}")
    return float(quantized)


def generate_trade_no(user_id: str) -> str:
    """生成我方订单号。

    含时间戳便于人工排查，尾部随机串避免同一秒内碰撞。不含用户可控内容，
    确保可以安全地拼进支付网关参数。
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    digest = hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:6].upper()
    return f"TQ{stamp}{digest}{secrets.token_hex(3).upper()}"


# ---- 易支付签名 ----


def epay_sign(params: dict[str, Any], key: str) -> str:
    """按易支付规则计算 MD5 签名。

    规则：剔除 ``sign``/``sign_type`` 与空值，其余参数按键名 ASCII 升序拼成
    ``a=1&b=2`` 后直接追加商户密钥再取 MD5。这与彩虹易支付的通用实现一致。
    """
    items = sorted(
        (str(name), str(value))
        for name, value in params.items()
        if name not in {"sign", "sign_type"} and str(value or "") != ""
    )
    raw = "&".join(f"{name}={value}" for name, value in items) + str(key)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def epay_verify(params: dict[str, Any]) -> bool:
    """校验回调签名。

    未配置密钥时一律拒绝——宁可漏单也不能在未配置状态下凭空放行加钱请求。
    """
    key = os.getenv("EPAY_KEY", "").strip()
    if not key:
        return False
    provided = str(params.get("sign") or "").strip().lower()
    if not provided:
        return False
    expected = epay_sign(params, key)
    return hmac.compare_digest(provided, expected)


def epay_purchase_params(
    trade_no: str,
    money_cny: float,
    payment_method: str,
    subject: str,
    notify_url: str,
    return_url: str,
) -> dict[str, str]:
    gateway_type = PAYMENT_METHODS.get(payment_method)
    if not gateway_type:
        raise ValueError("不支持的支付方式")
    params = {
        "pid": os.getenv("EPAY_PARTNER_ID", "").strip(),
        "type": gateway_type,
        "out_trade_no": trade_no,
        "notify_url": notify_url,
        "return_url": return_url,
        "name": subject,
        "money": f"{money_cny:.2f}",
    }
    params["sign"] = epay_sign(params, os.getenv("EPAY_KEY", "").strip())
    params["sign_type"] = "MD5"
    return params


def epay_submit_url() -> str:
    base = os.getenv("EPAY_GATEWAY_URL", "").strip().rstrip("/")
    if not base:
        raise ValueError("尚未配置支付网关地址")
    return f"{base}/submit.php"


def epay_notify_url() -> str:
    base = os.getenv("EPAY_NOTIFY_BASE_URL", "").strip().rstrip("/")
    if not base:
        base = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
    if not base:
        raise ValueError("尚未配置支付回调地址")
    return f"{base}/api/pay/epay/notify"


def epay_return_url() -> str:
    base = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
    return f"{base}/?topup=done" if base else "/?topup=done"


def epay_redirect_url(params: dict[str, str]) -> str:
    """生成 GET 跳转地址，供不便提交表单的客户端直接打开。"""
    return f"{epay_submit_url()}?{urlencode(params)}"


# ---- 到账后的上游同步 ----


async def sync_upstream_entitlement(
    client: Any,
    upstream_user_id: str,
    topup_total_usd: float,
) -> dict[str, Any]:
    """把充值结果写到上游：总额度、模型权限、密钥日限额。

    三个动作按重要性排序，任一失败都会抛出，由调用方记录待重试标记。本地账本
    绝不因此回滚——钱已经收到了。
    """
    result: dict[str, Any] = {"budget": 0.0, "models": [], "keys": []}
    await client.set_user_budget(upstream_user_id, topup_total_usd)
    result["budget"] = topup_total_usd

    models = topup_default_models()
    if models:
        result["models"] = await client.grant_default_models(upstream_user_id, models)

    cap = key_daily_budget_cap()
    if cap > 0:
        # 日限额是风控上限，不该超过账户累计充值额度本身。
        target = min(topup_total_usd, cap)
        result["keys"] = await client.raise_key_daily_budgets(upstream_user_id, target)
    return result


def public_config() -> dict[str, Any]:
    """给前端的充值配置，不含任何商户密钥。"""
    return {
        "enabled": billing_enabled(),
        "exchangeRate": exchange_rate(),
        "minTopupUsd": min_topup_usd(),
        "maxTopupUsd": max_topup_usd(),
        "amountOptions": topup_amount_options(),
        "channels": available_channels(),
        "currencySymbol": "¥",
        "manualPay": manual_qr_config(),
    }
