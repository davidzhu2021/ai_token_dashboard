from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sources() -> tuple[str, str]:
    return (
        (ROOT / "index.html").read_text(encoding="utf-8"),
        (ROOT / "assets" / "app.js").read_text(encoding="utf-8"),
    )


def test_trustworthy_observability_statuses_and_loading_lifecycle() -> None:
    markup, source = sources()
    for element_id in ("stabilityQuality", "stabilityMetrics", "costQuality", "costMetrics"):
        marker = f'id="{element_id}"'
        start = markup.index(marker)
        fragment = markup[start : start + 220]
        assert 'aria-live="polite"' in fragment
        assert 'aria-atomic="true"' in fragment
    assert 'target.setAttribute("role", isError ? "alert" : "status")' in source
    assert 'isStabilityLoading = false;\n      renderStabilityOverview();' in source
    assert 'isCostOverviewLoading = false;\n      renderCostOverview();' in source
    assert 'upstream == null ? ""' in source
    assert 'actual == null ? ""' in source
    assert '#costTrend { padding-bottom:20px; overflow:clip; }' in markup
    assert '#costTrend .observability-bar { min-width:0; }' in markup
    assert "本期确无异常记录" in source
    assert "异常趋势暂不可用" in source
    assert "本期确无费用记录" in source
    assert "费用趋势暂不可用" in source


def test_stability_dashboard_has_four_primary_metrics_and_governance_drilldown() -> None:
    markup, source = sources()
    for label in ("用户最终失败率", "兜底成功率", "TTFT P95", "Top 异常场景"):
        assert label in source
    for element_id in (
        "stabilityActions",
        "stabilityScenarioMatrix",
        "stabilityAttemptTimeline",
        "stabilityRequestActions",
        "stabilityRequestRegression",
    ):
        assert f'id="{element_id}"' in markup
    assert "requestedModelGroup" in source
    assert "ttftCoverageRate" in source
    assert "低覆盖 TTFT 不参与稳定判定" in markup


def test_stability_model_ranking_uses_compact_metric_columns() -> None:
    markup, source = sources()

    assert "<h3 class=\"panel-title\">模型排名</h3>" in markup
    assert "按综合稳定度排序；低覆盖 TTFT 不参与稳定判定。" in markup
    assert "observability-ranking-head" in markup
    for label in ("模型", "失败", "兜底", "TTFT", "状态"):
        assert f">{label}<" in source
    assert "fallbackRecoveryRate" in source
    assert "formatStabilityTtft" in source
    assert "Number(value) >= 1000" in source
    assert "is-${stabilityRankingStateClass(state)}" in source
    assert "data-stability-model=" in source


def test_stability_model_ranking_hides_zero_and_full_failure_rates() -> None:
    source = Path("assets/app.js").read_text(encoding="utf-8")

    assert "const visibleStabilityRankings = stabilityRankings.filter" in source
    assert "numericFailureRate !== 0 && numericFailureRate !== 1" in source


def test_cost_dashboard_separates_actual_forecast_and_auditable_metrics() -> None:
    markup, source = sources()
    for label in ("年度累计实际", "全年官方预测", "已核验累计节省", "月度预算"):
        assert label in source
    assert 'id="costComposition"' in markup
    assert 'id="costForecastComposition"' in markup
    assert 'id="costContext"' in markup
    assert "active_approved_baseline_plan_missing" in source
    assert "运行速率情景" in markup
    assert "as_of=" in source
    assert "recognition_status=" in source


def test_governance_workbench_is_separate_and_permission_aware() -> None:
    markup, source = sources()
    assert 'data-view="governance-workbench"' in markup
    assert 'id="governanceWorkbenchView"' in markup
    for tab in ("stability-actions", "regressions", "actual-ledger", "plans", "savings"):
        assert f'data-governance-tab="{tab}"' in markup
        assert f'data-governance-panel="{tab}"' in markup
    assert 'canManageStability() || canManageCosts() || canReconcileCosts()' in source
    assert '"/api/admin/stability/actions"' in source
    assert '"/api/admin/stability/regressions"' in source
    assert '"/api/admin/costs/savings-measurements"' in source
    assert "/api/admin/costs/plan-versions?year=" in source


def test_mobile_navigation_and_kpis_remain_discoverable() -> None:
    markup, source = sources()
    assert 'id="mobileViewSelect"' in markup
    assert ".observability-metric-grid { grid-template-columns:repeat(2,minmax(0,1fr));" in markup
    assert ".sidebar .nav-zone > #viewTabs:not(.nav-pending) { display:none; }" in markup
    assert "syncMobileViewPicker()" in source


def test_observability_frontend_hides_internal_provider_branding() -> None:
    markup, _ = sources()
    start = markup.index('id="stabilityView"')
    end = markup.index('id="modelsView"', start)
    copy = markup[start:end]
    for term in ("LiteLLM", "Virtual Key", "master key", "Proxy"):
        assert term not in copy
