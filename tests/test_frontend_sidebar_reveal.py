"""侧边栏导航整栏一次性揭示的前端契约。

「我的用量」「令牌管理」曾是静态可见的标签，其余项各自等自己的权限探测回来才
揭示，管理员/团队 leader 登录后会看到导航项逐个蹦出来。这些断言锁定修复后的
契约：所有标签初始隐藏、导航先呈骨架态、权限落地后由一处代码统一揭示。
"""

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
    "billingTab",
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

    # 八项全部初始隐藏——包括过去静态可见的「我的用量」和「令牌管理」。
    for tab_id in SIDEBAR_TABS:
        marker = f'id="{tab_id}"'
        assert marker in sidebar, f"缺少侧边栏标签 {tab_id}"
        button_end = sidebar.index(">", sidebar.index(marker))
        button = sidebar[sidebar.index(marker) : button_end]
        assert "hidden" in button, f"{tab_id} 必须初始隐藏，否则会比其他项先出现"

    assert sidebar.count('class="view-tab hidden"') + sidebar.count(
        'class="view-tab active hidden"'
    ) == len(SIDEBAR_TABS)


def test_navigation_reveal_is_gated_until_permissions_land() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    # 闸门：权限没落地前 syncNavigationVisibility() 不动 DOM，否则先返回的探测
    # 会把自己那一项揭示到骨架旁边，又退化成逐项蹦出。
    assert "let isNavigationRevealed = false;" in source
    assert "if (!isNavigationRevealed) return;" in source

    assert "function revealNavigation() {" in source
    assert "function resetNavigationToPending() {" in source

    # 团队看板与我的用量都在统一揭示里决定，不再各自单独 toggle。
    assert 'el("teamTab").classList.toggle("hidden", !currentUser?.isTeamLeader);' in source
    assert 'el("dashboardTab")?.classList.remove("hidden");' in source

    # 揭示与失败兜底都要经过 revealNavigation()，否则用户会卡在骨架态。
    assert source.count("revealNavigation();") >= 2


def test_reveal_has_a_timeout_fallback_for_cold_upstream_lookups() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    # 上游查不到账号的邮箱（新入职、拼错地址）会让 team_scope_for_user 翻完整个
    # 用户列表，实测 24-32 秒。已开通员工是 10ms 量级，远快于这个时限，所以兜底
    # 对他们不生效——但没有它，那类用户会看到侧边栏空白半分钟。
    assert "const NAVIGATION_REVEAL_TIMEOUT_MS = 800;" in source
    assert "function scheduleNavigationRevealFallback() {" in source
    assert "if (!isNavigationRevealed) revealNavigation();" in source
    assert "scheduleNavigationRevealFallback();" in source

    # 兜底先触发时，随后到达的 scope 仍要把团队看板补上——revealNavigation()
    # 会重跑 syncNavigationVisibility()，所以计时器必须在那里被清掉。
    reveal = source[source.index("function revealNavigation() {") : source.index("function scheduleNavigationRevealFallback()")]
    assert "window.clearTimeout(navigationRevealTimer);" in reveal
    assert "syncNavigationVisibility();" in reveal

    # 退回骨架态时也要清掉计时器，避免上一个身份的兜底揭示下一个身份的导航。
    pending = source[source.index("function resetNavigationToPending() {") :]
    assert "window.clearTimeout(navigationRevealTimer);" in pending[: pending.index("}")]


def test_billing_visibility_comes_from_the_scope_response() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    # 充值入口的可见性改由 /api/auth/scope 的零成本字段给出，不再等
    # /api/me/billing 的上游往返。
    assert "if (scope?.billingAvailable !== undefined) {" in source
    assert "billingAvailable = Boolean(scope.billingAvailable);" in source
    # 老后端没有该字段时退回按需探测，混合版本部署期间入口不会凭空消失。
    assert 'if (scope?.billingAvailable === undefined && !isMockCustomerIdentity()) {' in source

    # 引导路径上不再串行等待个人充值账本。
    assert "const billingPromise = refreshBillingAvailability();" not in source


def test_login_reset_returns_navigation_to_pending() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    # 换账号时导航退回骨架态，上一个身份的可见项不会闪现给下一个身份。
    assert source.count("resetNavigationToPending();") >= 2
    assert 'el("customersTab").classList.add("hidden");' not in source
