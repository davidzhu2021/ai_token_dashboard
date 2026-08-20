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
    assert 'el("stabilityTab")?.classList.toggle("hidden", !canViewStability() || remoteDemoSnapshotOnly)' in source
    assert 'el("costControlTab")?.classList.toggle("hidden", !canViewCosts() || remoteDemoSnapshotOnly)' in source


def test_partial_states_and_request_drawer_are_rendered() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    assert 'id="stabilityDrawer"' in markup
    assert "当前窗口覆盖不足" in source
    assert "当前月份覆盖不足" in source
    assert "不展示内容字段" in markup


def test_cost_control_forms_and_write_handlers_exist() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    for element_id in (
        "costBudgetForm",
        "costItemForm",
        "savingsActionForm",
        "costModelShare",
    ):
        assert f'id="{element_id}"' in markup
    assert 'method: id ? "PATCH" : "POST"' in source
    assert 'method: "PUT"' in source
    assert 'method: "DELETE"' in source


def test_cost_observability_filters_use_expandable_toolbar() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    for element_id in (
        "costFiltersButton",
        "costResetFiltersButton",
        "costFilterPanel",
        "costFilterCount",
        "costActiveFilters",
    ):
        assert f'id="{element_id}"' in markup
    assert 'aria-controls="costFilterPanel"' in markup
    assert markup.count('aria-expanded="false"') >= 1
    assert 'data-observability-clear=' in source
    assert 'setObservabilityFilterPanel("cost"' in source


def test_observability_filter_controls_and_reset_scope() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    for element_id in ("stabilityRangeSelect", "stabilityModel", "costRangeSelect", "costCategory", "costModel", "costVendor"):
        assert f'id="{element_id}"' in markup
    reset_start = source.index("function resetObservabilityFilters")
    reset_end = source.index("function clearObservabilityFilter", reset_start)
    reset_source = source[reset_start:reset_end]
    assert 'id: "stabilityModel"' not in reset_source
    assert 'stabilityRangeSelect' not in reset_source
    assert 'costRangeSelect' not in reset_source
    assert 'observabilityFilterConfig(scope).filters' in reset_source
    assert "currentStabilityWindow" in source
    assert "start_date=${startDate}&end_date=${endDate}&model=${encodeURIComponent(model)}" in source
    assert "cost_bucket=${encodeURIComponent(costBucket)}" in source


def test_stability_and_cost_use_shared_date_range_controls() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    for control_id in ("stabilityRangeSelect", "costRangeSelect"):
        assert f'id="{control_id}"' in markup
    for value, label in (("1", "近 1 天"), ("7", "近 7 天"), ("14", "近 14 天"), ("30", "近 30 天"), ("custom", "自定义")):
        assert f'value="{value}"' in markup
        assert label in markup
    assert 'function currentCostWindow()' in source
    assert 'start_date=${startDate}&end_date=${endDate}' in source
    assert 'const { startDate, endDate } = currentCostWindow();' in source


def test_observability_drawers_support_paginated_safe_drilldowns() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    assert 'role="dialog"' in markup
    assert 'aria-modal="true"' in markup
    assert 'data-stability-scenario=' in source
    assert '/api/admin/stability/scenarios?' in source
    assert 'data-stability-page=' in source
    assert '/api/admin/costs/ledger?' in source
    assert 'data-cost-ledger-page=' in source
    assert 'data-cost-model-series-day=' in source
    assert 'renderCostModelShare' in source
    assert 'messages' not in source[source.index('function openStabilityRequest'):source.index('function closeCostItemModal')]


def test_model_cost_share_drilldown_uses_modal_overlay() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    assert 'id="costModelShareModal"' in markup
    assert 'role="dialog"' in markup[markup.index('id="costModelShareModal"'):]
    assert 'aria-modal="true"' in markup[markup.index('id="costModelShareModal"'):]
    assert 'aria-labelledby="costModelShareModalTitle"' in markup
    assert 'function openCostModelShareModal' in source
    assert 'function closeCostModelShareModal' in source
    assert 'data-close-cost-model-series' not in source
    render_start = source.index("function renderCostModelShare(items)")
    render_end = source.index("function openCostModelShareModal", render_start)
    assert "renderCostModelShareDetail" not in source[render_start:render_end]


def test_cost_model_share_daily_drilldown_uses_canonical_model_filter() -> None:
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    assert "canonicalModel: day.dataset.costModelSeriesName" in source
    assert "canonical_model: filters.canonicalModel" in source
    assert "model: day.dataset.costModelSeriesName" not in source


def test_stability_drawer_has_model_filter_toolbar() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    assert 'id="stabilityScenarioModel"' in markup
    assert 'id="stabilityScenarioResetButton"' in markup
    assert 'renderStabilityScenarioModelFilter' in source
    assert 'modelOptions: data.modelOptions' in source
    assert 'el("stabilityScenarioModel")?.addEventListener("change"' in source
    assert 'el("stabilityScenarioResetButton")?.addEventListener("click"' in source
    assert 'updateStabilityScenarioTitle' in source


def test_stability_drawer_switches_between_full_width_browsing_and_request_detail() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    assert 'id="stabilityDrawerBackToSamples"' in markup
    assert 'data-stability-drawer-mode="samples"' in markup
    assert '.observability-drawer[data-stability-drawer-mode="samples"] .observability-drawer-layout' in markup
    assert '.observability-sample-row strong { overflow-wrap:anywhere; word-break:break-word; }' in markup
    assert 'function setStabilityDrawerMode(mode)' in source
    assert 'setStabilityDrawerMode("samples")' in source
    assert 'setStabilityDrawerMode("detail")' in source
    assert 'el("stabilityDrawerBackToSamples")?.addEventListener("click", () => setStabilityDrawerMode("samples"))' in source


def test_cost_drawer_switches_between_full_width_browsing_and_detail() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    assert 'id="costDetailDrawerBackToLedger"' in markup
    assert 'data-cost-drawer-mode="ledger"' in markup
    assert '.observability-drawer[data-cost-drawer-mode="ledger"] .observability-drawer-layout' in markup
    assert '.observability-ledger-row strong { display:block; overflow-wrap:anywhere; word-break:break-word; }' in markup
    assert 'function setCostDrawerMode(mode)' in source
    assert 'setCostDrawerMode("ledger")' in source
    assert 'setCostDrawerMode("detail")' in source
    assert 'el("costDetailDrawerBackToLedger")?.addEventListener("click", () => setCostDrawerMode("ledger"))' in source


def test_cost_drawer_has_model_and_provider_filters_without_reconciliation_filter() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")
    assert 'id="costLedgerModelFilter"' in markup
    assert 'id="costLedgerProviderFilter"' in markup
    assert 'el("costLedgerModelFilter")?.addEventListener("change"' in source
    assert 'el("costLedgerProviderFilter")?.addEventListener("change"' in source


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


def test_stability_dashboard_no_longer_contains_governance_actions_panel() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    assert 'id="stabilityActions"' not in markup
    assert "排障动作" not in markup


def test_cost_dashboard_trend_breakdown_exposes_three_daily_series() -> None:
    """Prevent the cost trend panel from losing its shared daily comparison."""
    markup = (ROOT / "index.html").read_text(encoding="utf-8")
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")

    assert 'id="costTrendBreakdown"' in markup
    for label in ("实际支出", "预测支出", "累计高支出波动"):
        assert label in markup
    assert 'class="cost-dashboard-grid"' in markup
    assert 'function buildCostTrendBreakdownPoints' in source
    assert 'function renderMultiLineChart' in source
    assert 'renderCostTrendBreakdown(data)' in source
    assert 'cumulativeVolatility' in source
    cost_view = markup[markup.index('id="costControlView"'):markup.index('id="governanceWorkbenchView"')]
    for misleading_copy in ("可省", "可优化金额", "可优化空间"):
        assert misleading_copy not in cost_view
