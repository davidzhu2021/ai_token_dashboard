"""Static contracts for real organization and usage operational states."""

from pathlib import Path


APP_JS = Path(__file__).parents[1] / "assets" / "app.js"
INDEX_HTML = Path(__file__).parents[1] / "index.html"


def test_usage_payload_quality_and_coverage_reach_visible_status_regions() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    markup = INDEX_HTML.read_text(encoding="utf-8")

    for element_id in (
        "personalUsageStatus",
        "adminUsageStatus",
        "teamUsageStatus",
        "departmentUsageStatus",
    ):
        assert f'id="{element_id}" class="operational-status hidden"' in markup

    assert "payload.dataQuality || null" in source
    assert "payload.coverage || null" in source
    assert "function usageStatusState(" in source
    assert "quality.snapshotUnavailable" in source
    assert "range.complete === false" in source
    assert 'title: "同步快照暂不可用"' in source
    assert 'title: "数据覆盖不完整"' in source
    usage_state = source[
        source.index("function usageStatusState(")
        : source.index("function renderUsageStatus(")
    ]
    assert "freshness?.stale" not in usage_state
    assert 'title: "同步延迟"' not in usage_state
    assert 'renderUsageStatus("personalUsageStatus"' in source
    assert 'renderUsageStatus("adminUsageStatus"' in source
    assert 'renderUsageStatus("teamUsageStatus"' in source
    assert 'renderUsageStatus("departmentUsageStatus"' in source


def test_organization_and_billing_statuses_are_server_field_driven() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    markup = INDEX_HTML.read_text(encoding="utf-8")

    assert "provisioningStatus" in source
    assert "billingStatus" in source
    assert "pastDue" in source
    assert "function organizationOperationalState(" in source
    operational_state = source[
        source.index("function organizationOperationalState(")
        : source.index("function renderOrganizationOperationalStatus(")
    ]
    assert "organizationProvisioningStatus(organization)" in operational_state
    assert "upstream" not in operational_state.lower()
    assert 'title: "企业账号开通中"' in source
    assert 'title: "企业账号同步失败"' in source
    assert 'title: "企业余额不足"' in source
    assert 'id="organizationOperationalStatus"' in markup
    assert 'id="organizationBillingOperationalStatus"' in markup
    assert "renderOrganizationOperationalStatus(\"organizationOperationalStatus\"" in source
    assert "renderOrganizationOperationalStatus(\n    \"organizationBillingOperationalStatus\"" in source


def test_customer_directory_exposes_provisioning_and_billing_states() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert "function organizationStatusChip(" in source
    assert 'label: "开通中"' in source
    assert 'label: "同步失败"' in source
    assert 'label: "余额不足"' in source
    assert 'class=\"customer-organization-status ${isArchived ? \"archived\" : \"\"} ${statusChip.tone}\"' in source


def test_real_organization_load_failures_are_not_rendered_as_empty_data() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert 'let customerOrganizationsLoadError = "";' in source
    assert 'let organizationLoadError = "";' in source
    assert 'let organizationMemberLoadError = "";' in source
    assert 'customerOrganizationsLoadError = error.message || "客户企业列表加载失败，请稍后重试。";' in source
    assert 'organizationMemberLoadError = error.message || "成员列表加载失败，请稍后重试。";' in source
    assert 'organizationLoadError = error.message || "企业组织加载失败，请稍后重试。";' in source
    assert 'grid.innerHTML = `<div class="customer-directory-empty">${escapeHtml(customerOrganizationsLoadError)}</div>`;' in source
    assert 'container.innerHTML = `<div class="organization-empty">${escapeHtml(organizationLoadError)}</div>`;' in source
    assert 'const memberLoadError = organizationMemberLoadError || organizationLoadError;' in source
    assert 'table.innerHTML = `<tr><td colspan="6"><div class="organization-empty">${escapeHtml(memberLoadError)}</div></td></tr>`;' in source


def test_enterprise_billing_failure_does_not_look_like_a_zero_balance() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    render = source[
        source.index("function renderOrganizationBilling()")
        : source.index("function organizationBillingUrl(")
    ]

    assert 'const hasLoadError = Boolean(organizationBillingLoadError && !organizationBillingData);' in render
    assert 'const unavailableValue = hasLoadError ? "暂不可用" : null;' in render
    assert 'setText("organizationBillingBalance", unavailableValue ||' in render
    assert 'setText("organizationBillingTotalCredits", unavailableValue ||' in render
    assert 'setText("organizationBillingUsageEstimate", unavailableValue ||' in render
    assert '!canTopup || hasLoadError' in render
    assert '!context.canAdjust || hasLoadError' in render


def test_token_creation_is_blocked_while_organization_is_unready_or_past_due() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    helper = source[
        source.index("function organizationTokenCreationBlockedByOrganization()")
        : source.index("function organizationTokensUrl()")
    ]
    assert '"企业账号同步失败，暂时不能创建企业 Token"' in helper
    assert '"企业账号仍在开通中，暂时不能创建企业 Token"' in helper
    assert '"企业余额不足或额度尚未生效，暂时不能创建企业 Token"' in helper

    renderer = source[
        source.index("function renderOrganizationTokens()")
        : source.index("async function loadOrganizationTokens()")
    ]
    assert "const organizationBlockedReason = organizationTokenCreationBlockedByOrganization();" in renderer
    assert "|| organizationBlockedReason" in renderer
    assert "历史资产会明确标注为只读且不计企业额度" in renderer
