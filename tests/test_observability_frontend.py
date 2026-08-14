from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_platform_admin_navigation_and_views_exist() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    for view in ("stability", "cost-control"):
        assert f'data-view="{view}"' in markup
    assert 'observabilityCapabilities' in source
    assert 'canViewStability()' in source
    assert 'canViewCosts()' in source
    assert 'el("stabilityTab")?.classList.toggle("hidden", !canViewStability())' in source
    assert 'el("costControlTab")?.classList.toggle("hidden", !canViewCosts())' in source


def test_partial_states_and_request_drawer_are_rendered() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    assert 'id="stabilityDrawer"' in markup
    assert "当前窗口覆盖不足" in source
    assert "当前月份覆盖不足" in source
    assert "提示词和响应正文" in markup


def test_cost_control_forms_and_write_handlers_exist() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    for element_id in (
        "costBudgetForm",
        "costItemForm",
        "savingsActionForm",
        "costModelSplit",
    ):
        assert f'id="{element_id}"' in markup
    assert 'method: id ? "PATCH" : "POST"' in source
    assert 'method: "PUT"' in source
    assert 'method: "DELETE"' in source


def test_observability_filters_use_expandable_toolbars() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    for element_id in (
        "stabilityFiltersButton",
        "stabilityResetFiltersButton",
        "stabilityFilterPanel",
        "stabilityFilterCount",
        "stabilityActiveFilters",
        "costFiltersButton",
        "costResetFiltersButton",
        "costFilterPanel",
        "costFilterCount",
        "costActiveFilters",
    ):
        assert f'id="{element_id}"' in markup
    assert 'aria-controls="stabilityFilterPanel"' in markup
    assert 'aria-controls="costFilterPanel"' in markup
    assert markup.count('aria-expanded="false"') >= 2
    assert 'data-observability-clear=' in source
    assert 'setObservabilityFilterPanel("stability"' in source
    assert 'setObservabilityFilterPanel("cost"' in source


def test_observability_filter_controls_and_reset_scope() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    for element_id in ("stabilityRange", "stabilityModel", "costMonth", "costCategory", "costModel", "costVendor"):
        assert f'id="{element_id}"' in markup
    reset_start = source.index("function resetObservabilityFilters")
    reset_end = source.index("function clearObservabilityFilter", reset_start)
    reset_source = source[reset_start:reset_end]
    assert 'id: "stabilityModel"' not in reset_source
    assert 'stabilityRange' not in reset_source
    assert 'costMonth' not in reset_source
    assert 'observabilityFilterConfig(scope).filters' in reset_source
    assert "currentStabilityWindow" in source
    assert "start_date=${startDate}&end_date=${endDate}&model=${encodeURIComponent(model)}" in source
    assert "cost_bucket=${encodeURIComponent(costBucket)}" in source


def test_observability_drawers_support_paginated_safe_drilldowns() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    assert 'role="dialog"' in markup
    assert 'aria-modal="true"' in markup
    assert 'data-stability-scenario=' in source
    assert '/api/admin/stability/scenarios?' in source
    assert 'data-stability-page=' in source
    assert 'data-cost-ledger-filter=' in source
    assert '/api/admin/costs/ledger?' in source
    assert 'data-cost-ledger-page=' in source
    assert 'messages' not in source[source.index('function openStabilityRequest'):source.index('function closeCostItemModal')]


def test_observability_state_is_latest_wins_and_cleared_on_login_change() -> None:
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    assert 'stabilityOverviewController?.abort()' in source
    assert 'costOverviewController?.abort()' in source
    assert 'stabilityOverviewRequestId' in source
    assert 'costOverviewRequestId' in source
    login_start = source.index('function showLogin()')
    login_source = source[login_start:login_start + 3000]
    assert 'stabilityOverview = null' in login_source
    assert 'costOverview = null' in login_source
    assert 'costDetailDrawer' in login_source


def test_cost_surface_exposes_annual_metrics_and_finance_metadata() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    for text in ('年度累计实际', '全年预测', '未来预计节省', '对账状态', '财务凭证号'):
        assert text in markup or text in source
    for element_id in ('costBucket', 'costProvider', 'costAccount', 'costReconciliation', 'costDetailDrawer'):
        assert f'id="{element_id}"' in markup


def test_frontend_copy_does_not_expose_backend_branding() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    start = markup.index('id="stabilityView"')
    end = markup.index('id="costControlView"')
    cost_end = markup.find("</section>", end) + len("</section>")
    dashboard_copy = markup[start:cost_end]
    assert "LiteLLM" not in dashboard_copy
    assert "Virtual Key" not in dashboard_copy
    assert "master key" not in dashboard_copy.lower()


def test_stability_middle_panels_have_equal_layout_and_scrollable_ranking() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    assert 'class="observability-grid observability-stability-grid"' in markup
    assert ".observability-stability-grid { grid-template-columns:repeat(2,minmax(0,1fr));" in markup
    assert ".observability-stability-grid > .panel { height:360px; min-height:360px;" in markup
    assert ".observability-stability-grid .observability-ranking { flex:1 1 auto; min-height:0;" in markup
    assert "overflow-y:auto" in markup
    assert "scrollbar-gutter:stable" in markup
    assert "overscroll-behavior:contain" in markup
    assert "@media (max-width: 900px)" in markup and ".observability-stability-grid { grid-template-columns:1fr; }" in markup
    assert "@media (max-width: 560px)" in markup and ".observability-stability-grid > .panel { height:300px; min-height:300px; }" in markup
