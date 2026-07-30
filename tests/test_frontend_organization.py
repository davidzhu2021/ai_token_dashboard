"""Static contracts for the enterprise organization demonstration UI."""

from pathlib import Path

from fastapi.testclient import TestClient

from backend import main


APP_JS = Path(__file__).parents[1] / "assets" / "app.js"
INDEX_HTML = Path(__file__).parents[1] / "index.html"


def test_customer_directory_is_the_only_organization_sidebar_entry() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    assert 'id="customersTab" class="view-tab hidden"' in markup
    assert 'id="organizationTab"' not in markup
    assert 'id="organizationView" class="view-section organization-view hidden"' in markup
    assert 'const isCustomerDetailView = isViewingCustomerOrganization()' in source
    assert 'button.dataset.view === "customers"' in source
    assert 'function organizationCanView()' in source
    assert 'function organizationCanManage()' in source


def test_organization_page_keeps_members_read_only_and_uses_current_scope_contract() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert 'if (!organizationCanView() || isOrganizationLoading) return;' in source
    assert 'if (!organizationCanView() || isOrganizationMemberLoading) return;' in source
    assert 'pageSize: String(organizationMemberPageSize)' in source
    assert 'params.set("departmentId", organizationMemberFilters.departmentId)' in source
    assert 'params.set("search", organizationMemberFilters.search)' in source
    assert 'data-organization-member-edit=' in source
    assert '${canManage ? "" : "disabled"}' in source


def test_organization_ui_has_demo_copy_filters_and_keyboard_close() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    for required in (
        "\u6f14\u793a\u6570\u636e",
        'id="organizationDepartmentFilter"',
        'id="organizationRoleFilter"',
        'id="organizationStatusFilter"',
        'id="organizationDepartmentModal"',
        'id="organizationMemberModal"',
        'id="organizationMemberTable"',
    ):
        assert required in markup
    assert 'event.key !== "Escape"' in source
    assert 'closeOrganizationMemberModal()' in source
    assert 'closeOrganizationDepartmentModal()' in source


def test_customer_usage_detail_has_working_return_controls_and_mock_safe_bootstrap() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    for required in (
        'id="customerUsageBreadcrumb"',
        'id="customerDepartmentBreadcrumb"',
        'id="backToCustomerOrganizationButton"',
        'id="backToCustomerOrganizationDepartmentButton"',
    ):
        assert required in markup
    assert "function renderCustomerUsageBreadcrumbs" in source
    assert 'renderCustomerUsageBreadcrumbs(view);' in source
    assert 'el("backToCustomerOrganizationButton").addEventListener("click", () => showOrganizationUsage("info"));' in source
    assert 'el("backToCustomerOrganizationDepartmentButton").addEventListener("click", () => showOrganizationUsage("info"));' in source
    assert "const isDemoCustomer = isMockCustomerIdentity();" in source
    assert "if (isDemoCustomer) {" in source


def test_customer_usage_detail_hides_platform_billing_management() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    # A seller admin viewing a customer's usage must not load or render the
    # platform-wide redemption and billing management panel in that scope.
    assert "function adminBillingVisible()" in source
    assert "currentUser?.isAdmin && billingAvailable && !isViewingCustomerOrganization()" in source


def test_inactive_mock_customer_identity_has_a_clear_limited_access_state() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert 'user?.isKnownDemoCustomerIdentity' in source
    assert 'organizationAccessStatus === "invited"' in source
    assert 'organizationAccessStatus === "suspended"' in source
    assert '"所属客户企业暂不可用"' in source


def test_archived_customer_controls_are_read_only_in_the_directory() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert 'data-customer-organization-edit="${escapeHtml(id)}" ${isArchived ? "disabled" : ""}' in source


def test_organization_copy_does_not_expose_backend_provider_terms() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")
    organization_markup = markup[markup.index('id="organizationView"') : markup.index('id="keysView"')]
    organization_source = source[source.index('const ORGANIZATION_ROLE_LABELS') : source.index('function switchView')]

    for term in ("LiteLLM", "Proxy", "Virtual Key", "upstream"):
        assert term not in organization_markup
        assert term not in organization_source


def test_index_includes_organization_view() -> None:
    response = TestClient(main.app).get("/")

    assert response.status_code == 200
    assert 'id="organizationView"' in response.text
