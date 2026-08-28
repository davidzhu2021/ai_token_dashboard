from pathlib import Path


APP_JS = Path(__file__).parents[1] / "assets" / "app.js"


def app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


def function_body(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    return source[start : source.index(end_marker, start)]


def test_team_overview_uses_one_complete_usage_request() -> None:
    source = app_js()
    body = function_body(source, "async function loadTeamData(", "\nfunction loadTeamMemberData(")

    assert 'include_member_rankings: "true"' in body
    assert 'include_member_rankings: "false"' not in body
    assert "loadTeamRankingData(" not in body
    assert "applyTeamUsagePayload(payload, cacheKey)" in body


def test_dashboard_request_is_shared_and_abortable() -> None:
    source = app_js()
    body = function_body(source, "function loadDashboardData(", "\nfunction loadAdminData(")

    assert "if (dashboardInFlight && dashboardRequestKey === queryKey) return dashboardInFlight;" in body
    assert "dashboardRequestController?.abort();" in body
    assert "{ signal: controller.signal }" in body
    assert "dashboardInFlight = request;" in body
    assert "return request;" in body


def test_usage_filters_abort_superseded_requests_without_clearing_old_data() -> None:
    source = app_js()
    admin = function_body(source, "function loadAdminData(", "\nfunction loadDepartmentData(")
    department = function_body(source, "function loadDepartmentData(", "\nasync function loadTeamRankingData(")
    member = function_body(source, "function loadTeamMemberData(", "\nfunction clearTeamMemberSelection(")

    assert "adminUsageRequestController?.abort();" in admin
    assert "{ signal: controller.signal }" in admin
    assert "adminUsageData = [];" not in admin
    assert "departmentUsageRequestController?.abort();" in department
    assert "{ signal: controller.signal }" in department
    assert "departmentUsageData = [];" not in department
    assert "teamMemberUsageRequestController?.abort();" in member
    assert "{ signal: controller.signal }" in member
    assert "teamMemberUsageData = [];" not in member


def test_api_has_timeout_and_chart_events_are_delegated_once() -> None:
    source = app_js()
    api_body = function_body(source, "async function api(", "\nasync function ensureCsrfToken(")
    chart_body = function_body(source, "function bindChartTooltipEvents(", "\nfunction renderEmptyChart(")
    line_chart = function_body(source, "function renderLineChart(", "\nfunction renderTrendTo(")

    assert "timeoutMs = 15_000" in api_body
    assert 'timeoutError.name = "TimeoutError";' in api_body
    assert 'timeoutError.code = "REQUEST_TIMEOUT";' in api_body
    assert "callerSignal?.addEventListener" in api_body
    assert 'cache: "no-store"' in api_body
    assert 'svg.dataset.chartTooltipBound === "true"' in chart_body
    assert 'svg.addEventListener("pointerleave", hideChartTooltip);' in chart_body
    assert 'node.addEventListener("pointerleave"' not in line_chart
    assert "bindChartTooltipEvents(svg);" in line_chart


def test_billing_loads_share_in_flight_requests_and_use_short_ttl() -> None:
    source = app_js()
    personal = function_body(source, "function loadBillingData(", "\nasync function refreshBillingAvailability(")
    organization = function_body(source, "function loadOrganizationBillingData(", "\nasync function changeOrganizationBillingPage(")

    assert "const BILLING_CACHE_TTL_MS = 10_000;" in source
    assert "if (billingRequest) return billingRequest;" in personal
    assert "Date.now() - billingLoadedAt < BILLING_CACHE_TTL_MS" in personal
    assert "billingRequest = request;" in personal
    assert "const ORGANIZATION_BILLING_CACHE_TTL_MS = 10_000;" in source
    assert "if (organizationBillingRequest && organizationBillingScopeKey === queryKey)" in organization
    assert "Date.now() - organizationBillingLoadedAt < ORGANIZATION_BILLING_CACHE_TTL_MS" in organization
    assert "organizationBillingRequest = request;" in organization


def test_freshness_copy_reflects_verification_state() -> None:
    source = app_js()
    body = function_body(source, "function freshnessText(", "\nfunction usageStatusState(")

    assert 'freshness.settlementState === "settled"' in body
    assert 'freshness.unsettledBackends' in body
    assert '"数据核验中，已核验截至"' in body


def test_usage_auto_refresh_is_five_seconds() -> None:
    source = app_js()
    body = function_body(source, "function scheduleUsageAutoRefresh()", "\nfunction observabilityPercent(")
    assert "setInterval(refreshVisibleUsageData, 5_000)" in body
