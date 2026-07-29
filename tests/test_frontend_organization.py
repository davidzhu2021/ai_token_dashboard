"""Static contracts for the enterprise organization demonstration UI."""

from pathlib import Path

from fastapi.testclient import TestClient

from backend import main


APP_JS = Path(__file__).parents[1] / "assets" / "app.js"
INDEX_HTML = Path(__file__).parents[1] / "index.html"


def test_organization_view_is_hidden_until_the_server_grants_demo_access() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    assert 'id="organizationTab" class="view-tab hidden"' in markup
    assert 'id="organizationView" class="view-section organization-view hidden"' in markup
    assert 'organizationDemoEnabled' in source
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
