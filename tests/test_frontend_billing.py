"""充值中心前端结构与行为约定测试。"""

from pathlib import Path

from fastapi.testclient import TestClient

from backend import main

APP_JS = Path(__file__).parents[1] / "assets" / "app.js"
INDEX_HTML = Path(__file__).parents[1] / "index.html"


def test_billing_view_is_present_with_navigation_entry() -> None:
    response = TestClient(main.app).get("/")

    assert response.status_code == 200
    assert 'id="billingTab"' in response.text
    assert 'data-view="billing"' in response.text
    assert 'id="billingView" class="view-section hidden"' in response.text
    assert 'id="topupForm"' in response.text
    assert 'id="billingOrderBody"' in response.text


def test_billing_navigation_starts_hidden_until_backend_confirms() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")

    # 后端未开放充值时导航项不能出现，因此默认带 hidden。
    assert 'id="billingTab" class="view-tab hidden"' in markup
    assert 'id="billingOnlinePanel" class="panel hidden"' in markup
    assert 'id="billingPayPanel" class="panel hidden"' in markup
    assert 'id="adminBillingSection" class="hidden"' in markup


def test_billing_page_shows_balance_topup_total_and_spent() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")

    assert 'id="billingBalance"' in markup
    assert 'id="billingTopupTotal"' in markup
    assert 'id="billingSpent"' in markup
    assert 'id="billingRateChip"' in markup


def test_user_page_has_no_redemption_entry() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")
    billing_markup = markup[markup.index('id="billingView"') : markup.index('id="modelsView"')]

    # 兑换码只给管理员发放，用户页面不显示兑换入口。
    assert 'id="redeemForm"' not in billing_markup
    assert 'id="redeemCode"' not in billing_markup
    assert "兑换" not in billing_markup
    assert "submitRedeem" not in source


def test_admin_keeps_redemption_management() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    admin_markup = markup[markup.index('id="adminBillingSection"') : markup.index('id="teamView"')]

    assert 'id="adminRedemptionForm"' in admin_markup
    assert 'id="adminRedemptionBody"' in admin_markup


def test_payment_methods_are_rendered_from_backend_config() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    # 支付方式取决于后端配了哪几张收款码，不能在 HTML 里写死。
    assert 'id="topupMethodRow"' in markup
    assert 'name="paymentMethod" value="alipay" checked' not in markup
    assert "function renderTopupMethods()" in source
    assert 'name="paymentMethod" value="${escapeHtml(' in source


def test_frontend_calls_billing_endpoints() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert 'api("/api/me/billing")' in source
    assert 'api("/api/me/billing/orders"' in source
    assert "/submit`" in source
    assert 'api(`/api/me/billing/orders/${encodeURIComponent(pendingTopupTradeNo)}`)' in source
    assert 'api("/api/admin/billing/redemptions?limit=50")' in source
    assert 'api("/api/admin/billing/redemptions"' in source
    assert "/reject`" in source
    assert 'api("/api/admin/billing/sync/retry"' in source


def test_billing_view_is_reachable_without_entitlement() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    # showApp 在权限受限时会提前 return；充值必须在那之前初始化，
    # 否则新用户永远看不到唯一的自助开通入口。
    assert "const billingPromise = refreshBillingAvailability();" in source
    assert "if (accountAccessCopy(currentUser)) {\n    await billingPromise;\n    return;\n  }" in source
    assert 'el("accountAccessTopupButton").addEventListener("click", () => switchView("billing"));' in source


def test_topup_entry_hidden_when_billing_unavailable() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    # 充值未开放时不能给出走不通的入口。
    assert 'el("accountAccessTopupButton").classList.toggle("hidden", !(state.topup && billingAvailable));' in source
    assert 'if (view === "billing" && !billingAvailable) view = "dashboard";' in source


def test_online_panel_hidden_until_a_channel_is_configured() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert 'onlinePanel.classList.toggle("hidden", !channel)' in source
    # 渠道优先自动支付，其次收款码转账；都没配就不给入口。
    assert 'if (channels.includes("epay")) return "epay";' in source
    assert 'if (channels.includes("manual_qr")) return "manual_qr";' in source
    assert '设置了 channel = ""' not in source


def test_manual_pay_panel_collects_proof_and_explains_review() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    assert 'id="billingPayQr"' in markup
    assert 'id="manualPayForm"' in markup
    assert 'id="manualPayNote"' in markup
    # 收款码没有回调，必须明确告知需要人工确认，不能让用户以为立即到账。
    assert "管理员" in markup
    assert "function submitManualPayment(" in source
    assert "showManualPayPanel(payload)" in source


def test_rendered_billing_fields_are_escaped() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    # 订单与兑换码列表走 innerHTML，动态字段必须转义。
    assert "escapeHtml(order.tradeNo || \"-\")" in source
    assert "escapeHtml(order.userId || \"-\")" in source
    assert "escapeHtml(item.name || \"-\")" in source
    assert "escapeHtml(item.code)" in source
    assert "escapeHtml(item.codeHint || \"\")" in source
    # 用户回填的付款说明和管理员备注都会渲染，必须转义。
    assert "escapeHtml(order.payerNote || \"-\")" in source
    assert "escapeHtml(note)" in source

    # 拼进 data-* 属性的标识同样要转义，否则可被用来注入属性。
    billing_admin = source[
        source.index("function renderAdminRedemptions") : source.index("function renderAdminBilling")
    ]
    assert "data-disable-redemption=" in billing_admin
    assert billing_admin.count("escapeHtml(") >= 6
    for raw in (
        "data-disable-redemption=\"${item.id}",
        "data-complete-order=\"${order.tradeNo}",
        "data-reject-order=\"${order.tradeNo}",
    ):
        assert raw not in source, "data-* 属性里的标识必须经过 escapeHtml"


def test_manual_qr_image_source_comes_from_backend_payload() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    # 二维码走 img.src 赋值而不是 innerHTML 拼接，避免属性注入。
    assert 'qr.src = String(payload.qrUrl || "");' in source


def test_generated_codes_warn_about_one_time_display() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert "以下兑换码仅显示这一次" in source


def test_admin_review_queue_supports_confirm_and_reject() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    assert 'id="adminBillingReviewBody"' in markup
    assert 'id="adminBillingPendingReview"' in markup
    assert "function renderAdminBillingReviews()" in source
    assert "async function rejectBillingOrder(" in source
    # 确认即放款，必须有二次确认。
    assert "window.confirm(" in source[source.index("async function completeBillingOrder(") :][:600]


def test_polling_stops_when_leaving_billing_view() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert 'if (currentView === "billing" && view !== "billing") hideManualPayPanel();' in source
    assert "stopTopupPolling();" in source[source.index("function hideManualPayPanel()") :][:200]
    # 人工确认比自动回调慢，轮询窗口相应放宽。
    assert "attempts > 100" in source


def test_logout_clears_billing_state() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    # 换账号后不能残留上一个账号的余额与订单。
    for cleared in (
        "billingAccount = null;",
        "billingOrders = [];",
        "billingAvailable = false;",
        "adminBillingOrders = [];",
        "adminBillingReviews = [];",
        "pendingTopupTradeNo = \"\";",
    ):
        assert cleared in source


def test_static_asset_version_bumped_for_billing_release() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")

    # 不更新版本号线上用户会命中旧缓存，看不到充值中心。
    assert 'src="/assets/app.js?v=20260729-topup-manual-qr"' in markup


def test_billing_copy_avoids_upstream_provider_terms() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    billing_markup = markup[markup.index('id="billingView"') : markup.index('id="modelsView"')]
    source = APP_JS.read_text(encoding="utf-8")
    billing_source = source[source.index("// ---- 充值中心 ----") : source.index("async function copyText")]

    for term in ("LiteLLM", "Virtual Key", "max_budget", "Proxy", "proxy"):
        assert term not in billing_markup, f"充值页面文案不应暴露 {term}"
        assert term not in billing_source, f"充值前端逻辑不应暴露 {term}"
