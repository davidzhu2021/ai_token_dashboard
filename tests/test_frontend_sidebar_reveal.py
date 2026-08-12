"""侧边栏登录后立即可用、异步补齐权限入口的前端契约。"""

from pathlib import Path

from fastapi.testclient import TestClient

from backend import main


APP_JS = Path(__file__).parents[1] / "assets" / "app.js"
SIDEBAR_TABS = (
    "customersTab",
    "organizationTab",
    "adminTab",
    "departmentTab",
    "teamTab",
    "dashboardTab",
    "keysTab",
    "organizationTokensTab",
    "billingTab",
    "stabilityTab",
    "costControlTab",
)


def sidebar_markup() -> str:
    response = TestClient(main.app).get("/")
    assert response.status_code == 200
    start = response.text.index('<aside class="sidebar"')
    return response.text[start : response.text.index("</aside>", start)]


def test_every_sidebar_tab_starts_hidden_behind_a_skeleton() -> None:
    sidebar = sidebar_markup()

    # 骨架占位先于导航出现，权限探测期间它就是用户看到的内容。
    assert 'id="navSkeleton"' in sidebar
    assert sidebar.index('id="navSkeleton"') < sidebar.index('id="viewTabs"')
    assert 'class="view-tabs nav-pending"' in sidebar
    assert 'aria-busy="true"' in sidebar

    # 九项全部初始隐藏——包括过去静态可见的「我的用量」和「令牌管理」。
    for tab_id in SIDEBAR_TABS:
        marker = f'id="{tab_id}"'
        assert marker in sidebar, f"缺少侧边栏标签 {tab_id}"
        button_end = sidebar.index(">", sidebar.index(marker))
        button = sidebar[sidebar.index(marker) : button_end]
        assert "hidden" in button, f"{tab_id} 必须初始隐藏，否则会比其他项先出现"

    assert sidebar.count('class="view-tab hidden"') + sidebar.count(
        'class="view-tab active hidden"'
    ) == len(SIDEBAR_TABS)


def test_navigation_reveals_immediately_from_auth_identity() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    # 闸门只用于隔离上一身份；auth/me 落地后同一调用栈立即揭示基础导航。
    assert "let isNavigationRevealed = false;" in source
    assert "if (!isNavigationRevealed) return;" in source
    assert "function revealNavigation() {" in source
    assert "function resetNavigationToPending() {" in source
    assert 'el("teamTab").classList.toggle("hidden", !currentUser?.isTeamLeader);' in source
    assert 'el("dashboardTab")?.classList.remove("hidden");' in source
    show_app = source[
        source.index("async function showApp(user) {") : source.index("async function loadAuthScope() {")
    ]
    assert show_app.index("resetNavigationToPending();") < show_app.index("revealNavigation();")
    assert show_app.index("revealNavigation();") < show_app.index("const scopePromise = loadAuthScope();")
    assert "NAVIGATION_REVEAL_TIMEOUT_MS" not in source
    assert "scheduleNavigationRevealFallback" not in source


def test_billing_visibility_comes_from_the_scope_response() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    # 充值入口由 /api/auth/scope 的零成本字段异步补齐，不再等真实账本。
    assert "if (scope?.billingAvailable !== undefined) {" in source
    assert "billingAvailable = Boolean(scope.billingAvailable);" in source
    # 老后端没有该字段时退回按需探测，混合版本部署期间入口不会凭空消失。
    assert 'if (scope?.billingAvailable === undefined && !isOrganizationCustomerIdentity()) {' in source

    # 引导路径上不再串行等待个人充值账本。
    assert "const billingPromise = refreshBillingAvailability();" not in source


def test_login_reset_returns_navigation_to_pending() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    # 换账号时导航退回骨架态，上一个身份的可见项不会闪现给下一个身份。
    assert source.count("resetNavigationToPending();") >= 2
    assert 'el("customersTab").classList.add("hidden");' not in source
