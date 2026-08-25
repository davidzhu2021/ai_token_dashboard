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
    assert 'item.upstream == null ? "无数据"' in source
    assert "本期确无异常记录" in source
    assert "异常趋势暂不可用" in source


def test_stability_dashboard_does_not_render_coverage_notice() -> None:
    """稳定性指标卡在覆盖不完整时仍保持展示，不额外占用提示区域。"""
    _, source = sources()

    start = source.index("function observabilityReasonCopy(payload, scope)")
    end = source.index("\nfunction renderObservabilityQuality", start)
    reason_copy = source[start:end]

    assert "if (scope === \"stability\" && (reasons.length || coverage.incomplete || coverage.partial)) {\n    return null;\n  }" in reason_copy


def test_stability_dashboard_keeps_observability_drilldown_without_governance_actions() -> None:
    markup, source = sources()
    for label in ("用户最终失败率", "兜底成功率", "TTFT P95", "Top 异常场景"):
        assert label in source
    for element_id in ("stabilityAttemptTimeline",):
        assert f'id="{element_id}"' in markup
    for removed_id in ("stabilityActions", "stabilityRequestActions", "stabilityRequestRegression"):
        assert f'id="{removed_id}"' not in markup
    assert "requestedModelGroup" in source
    assert "ttftCoverageRate" in source
    assert "低覆盖 TTFT 不参与稳定判定" in markup


def test_top_stability_scenario_metric_separates_name_and_count() -> None:
    markup, source = sources()

    assert "function stabilityScenarioMetricCard" in source
    assert 'class="observability-metric stability-scenario-metric-card' in source
    assert 'class="stability-scenario-metric-name"' in source
    assert 'class="stability-scenario-metric-count"' in source
    assert "stabilityScenarioMetricCard({ metric: topScenarioMetric, scenario: topScenario?.scenario" in source
    assert ".stability-scenario-metric-name" in markup
    assert ".stability-scenario-metric-count" in markup
    assert ".stability-scenario-metric-count { color:var(--ink,#14243a); font-size:clamp(16px,1.7vw,20px);" in markup
    assert ".stability-scenario-metric-name { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:20px;" in markup


def test_stability_model_ranking_uses_ranked_stability_cards() -> None:
    markup, source = sources()

    assert "<h3 class=\"panel-title\">模型排名</h3>" in markup
    assert "按综合稳定度排序；低覆盖 TTFT 不参与稳定判定。" in markup
    for css_class in (
        "observability-ranking-list",
        "observability-rank-card",
        "observability-rank-badge",
        "observability-rank-track",
        "observability-rank-status",
    ):
        assert css_class in markup
    for css_class in (
        "observability-ranking-list",
        "observability-rank-card",
        "observability-rank-track",
        "observability-rank-status",
    ):
        assert css_class in source
    for label in ("综合稳定度", "失败", "兜底", "TTFT"):
        assert f">{label}<" in source
    assert "fallbackRecoveryRate" in source
    assert "formatStabilityTtft" in source
    assert "Number(value) >= 1000" in source
    assert "is-${stabilityRankingStateClass(state)}" in source
    assert "rankingBadge(index)" in source
    assert "data-stability-model=" in source


def test_top_stability_scenarios_use_ranked_drilldown_cards() -> None:
    markup, source = sources()

    assert 'id="stabilityScenarioRanking"' in markup
    for css_class in (
        "stability-scenario-ranking",
        "stability-scenario-card",
        "stability-scenario-identity",
        "stability-scenario-metrics",
    ):
        assert css_class in markup
    for css_class in (
        "stability-scenario-card",
        "stability-scenario-identity",
        "stability-scenario-metrics",
    ):
        assert css_class in source
    for label in ("异常次数", "最终失败率", "模型组", "错误码"):
        assert f">{label}<" in source
    assert "rankingBadge(index)" in source
    assert "data-stability-scenario=" in source
    assert "data-stability-model=" in source
    assert "data-stability-error-code=" in source


def test_stability_model_ranking_hides_zero_and_full_failure_rates() -> None:
    source = Path("assets/app.js").read_text(encoding="utf-8")

    assert "const visibleStabilityRankings = stabilityRankings.filter" in source
    assert "numericFailureRate !== 0 && numericFailureRate !== 1" in source


def test_stability_model_ranking_distinguishes_fallback_statuses() -> None:
    source = Path("assets/app.js").read_text(encoding="utf-8")

    assert "const formatStabilityFallback = (item) =>" in source
    assert '"not_triggered"' in source
    assert '"未触发"' in source
    assert 'item.fallbackRecoveryStatus' in source


def test_cost_dashboard_separates_actual_forecast_and_auditable_metrics() -> None:
    markup, source = sources()
    for label in ("当前筛选区间花费及预算", "年度累计", "全年官方预测", "已核验累计节省"):
        assert label in source
    assert 'id="costContext"' in markup
    assert "active_approved_baseline_plan_missing" in source
    assert "as_of=" in source
    assert "recognition_status=" in source
    for removed in ('id="costTrend"', "实际与运行速率情景", "运行速率情景", "runRateForecast", 'id="costAnomalies"', "异常月份与对账提示", "暂无异常月份", 'id="costComposition"', 'id="costForecastComposition"', "实际组成", "官方预测组成", "observability-composition"):
        assert removed not in markup
        assert removed not in source


def test_governance_workbench_is_separate_and_permission_aware() -> None:
    markup, source = sources()
    assert 'data-view="governance-workbench"' in markup
    assert 'id="governanceWorkbenchView"' in markup
    for tab in ("actual-ledger", "plans", "savings"):
        assert f'data-governance-tab="{tab}"' in markup
        assert f'data-governance-panel="{tab}"' in markup
    for removed in ('data-governance-tab="stability-actions"', 'data-governance-tab="regressions"', 'canManageStability()', '"/api/admin/stability/actions"', '"/api/admin/stability/regressions"'):
        assert removed not in markup
        assert removed not in source
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
