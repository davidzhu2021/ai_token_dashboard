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
    personal_billing_markup = billing_markup[
        billing_markup.index('id="personalBillingWorkspace"') :
    ]
    organization_billing_markup = billing_markup[
        billing_markup.index('id="organizationBillingWorkspace"') : billing_markup.index('id="personalBillingWorkspace"')
    ]

    # 兑换码只给卖方平台运营面板发放；个人充值与企业 Mock 额度页都
    # 不能提供兑换入口。企业页可以明确说明“不包含兑换码”，因此不能
    # 再把说明文字本身误判成一个入口。
    assert 'id="redeemForm"' not in personal_billing_markup
    assert 'id="redeemCode"' not in personal_billing_markup
    assert 'id="redeemForm"' not in organization_billing_markup
    assert 'id="redeemCode"' not in organization_billing_markup
    assert "本页不包含支付方式、收款码、兑换码或真实订单。" in organization_billing_markup
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

    # showApp 在权限受限时会提前 return；充值可见性必须在那之前由轻量
    # /api/auth/scope 解析，不能等待真实账本余额或订单请求。
    assert "const scopePromise = loadAuthScope();" in source
    assert "if (accountAccessCopy(currentUser)) {\n    // 权限受限的新用户照样要看到充值入口——他们正是靠充值开通。\n    await scopePromise;\n    return;\n  }" in source
    assert "if (scope?.billingAvailable !== undefined) {\n      billingAvailable = Boolean(scope.billingAvailable);\n    }" in source
    assert 'el("accountAccessTopupButton").addEventListener("click", () => switchView("billing"));' in source


def test_topup_entry_hidden_when_billing_unavailable() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    # 个人充值未开放，或当前身份不具备企业额度能力时，直接切换到
    # 充值页也必须回退到我的用量。
    assert 'el("accountAccessTopupButton").classList.toggle("hidden", !(state.topup && billingAvailable));' in source
    assert 'if (view === "billing" && !canAccessBillingView()) view = "dashboard";' in source
    access_guard = source[source.index("function canAccessBillingView()") : source.index("function selectedCustomerOrganizationId()")]
    assert "isOrganizationBillingView()" in access_guard
    assert "billingAvailable && !currentUser?.organizationDemoEnabled" in access_guard


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

    # Leaving either billing workspace stops legacy payment polling and closes
    # the Mock top-up modal, so no billing UI survives a view transition.
    leaving_billing = source[source.index('if (currentView === "billing" && view !== "billing")') : source.index("currentView = view;")]
    assert "hideManualPayPanel();" in leaving_billing
    assert "closeOrganizationTopupModal({ force: true });" in leaving_billing
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


def test_static_asset_version_bumped_for_organization_token_real_models_release() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")

    # 不更新版本号线上用户会命中旧缓存：旧 app.js 把勾选框 value 当成模型名直接提交，
    # 而新契约的 value 是目录下标，会被后端判成「不存在的模型」。
    assert 'src="/assets/app.js?v=20260731-organization-token-real-models"' in markup


def test_billing_copy_avoids_upstream_provider_terms() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    billing_markup = markup[markup.index('id="billingView"') : markup.index('id="modelsView"')]
    source = APP_JS.read_text(encoding="utf-8")
    billing_source = source[source.index("// ---- 充值中心 ----") : source.index("async function copyText")]

    for term in ("LiteLLM", "Virtual Key", "max_budget", "Proxy", "proxy"):
        assert term not in billing_markup, f"充值页面文案不应暴露 {term}"
        assert term not in billing_source, f"充值前端逻辑不应暴露 {term}"


def test_organization_billing_has_a_separate_mock_contract_and_payment_free_workspace() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    for required in (
        'id="organizationBillingWorkspace"',
        'id="organizationTopupModal"',
        'id="organizationBillingRecordBody"',
        'id="openOrganizationTopupModalButton"',
        'data-organization-usage-view="billing"',
    ):
        assert required in markup
    assert 'path: "/api/organization/current/billing"' in source
    assert 'path: `${customerOrganizationPath(organizationId)}/billing`' in source
    assert 'api("/api/organization/current/billing/topups"' in source
    assert "body: JSON.stringify({ amountUsd: amount })" in source
    assert "function organizationBillingContext()" in source
    assert "function renderOrganizationBilling()" in source
    assert "function submitOrganizationTopup(event)" in source


def test_organization_billing_workspace_hides_personal_payment_ui() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    render_billing = source[source.index("function renderOrganizationBilling()") : source.index("function organizationBillingUrl(")]
    assert 'workspace.classList.toggle("hidden", !context);' in render_billing
    assert 'personalWorkspace.classList.toggle("hidden", Boolean(context));' in render_billing
    assert "if (renderOrganizationBilling()) return;" in source


def test_organization_billing_records_can_page_beyond_the_initial_response() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    assert 'id="organizationBillingPreviousPageButton"' in markup
    assert 'id="organizationBillingNextPageButton"' in markup
    assert 'id="organizationBillingPageInfo"' in markup
    assert "function changeOrganizationBillingPage(direction)" in source
    assert 'changeOrganizationBillingPage(-1)' in source
    assert 'changeOrganizationBillingPage(1)' in source


def test_mock_billing_sidebar_requires_customer_capability_not_platform_admin() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    navigation = source[source.index("function syncNavigationVisibility()") : source.index("function renderCustomerUsageBreadcrumbs")]
    assert "const canUseBillingSidebar = isCustomer" in navigation
    assert "? canViewOrganizationBilling()" in navigation
    assert ": Boolean(billingAvailable && !currentUser?.organizationDemoEnabled);" in navigation
    assert 'el("billingTab")?.classList.toggle("hidden", !canUseBillingSidebar);' in navigation


def test_organization_topup_modal_closes_only_via_existing_safe_modal_contract() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    backdrop = source[source.index('document.querySelectorAll(".modal-backdrop")') : source.index('document.addEventListener("keydown"')]
    assert "if (event.target !== backdrop) return;" in backdrop
    assert 'backdrop.id === "organizationTopupModal"' in backdrop
    keyboard = source[source.index('document.addEventListener("keydown"') : source.index('document.addEventListener("visibilitychange"')]
    assert 'if (event.key !== "Escape") return;' in keyboard
    assert '!el("organizationTopupModal").classList.contains("hidden")' in keyboard


def test_mock_customer_billing_never_falls_back_to_personal_billing() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    billing_refresh = source[source.index("async function refreshBillingAvailability()") : source.index("function updateBillingNav()")]
    assert "if (isMockCustomerIdentity())" in billing_refresh
    assert "billingAvailable = false;" in billing_refresh
    switch_view = source[source.index("function switchView(view)") : source.index("async function loadCurrentViewData")]
    assert "if (isOrganizationBillingView())" in switch_view
    assert "loadOrganizationBillingData()" in switch_view
    assert "else if (!isBillingLoading) loadBillingData();" in switch_view


def test_organization_topup_modal_uses_existing_keyboard_and_backdrop_contract() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert 'backdrop.id === "organizationTopupModal"' in source
    assert '!el("organizationTopupModal").classList.contains("hidden")' in source
    assert "closeOrganizationTopupModal()" in source
