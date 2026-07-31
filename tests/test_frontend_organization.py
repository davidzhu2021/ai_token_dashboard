"""Static contracts for the enterprise organization demonstration UI."""

from pathlib import Path

from fastapi.testclient import TestClient

from backend import main


APP_JS = Path(__file__).parents[1] / "assets" / "app.js"
INDEX_HTML = Path(__file__).parents[1] / "index.html"


def test_both_parties_reach_the_same_organization_workspace_from_their_own_entry() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    # 乙方从「客户企业」目录下钻，甲方管理员有自己的「企业管理」入口，两者复用
    # 同一个 organizationView，只是 API 基址不同。
    assert 'id="customersTab" class="view-tab hidden"' in markup
    assert 'id="organizationTab" class="view-tab hidden"' in markup
    assert 'id="organizationView" class="view-section organization-view hidden"' in markup
    assert 'el("organizationTab")?.classList.toggle("hidden", !canManageCurrentOrganization);' in source
    assert 'const canManageCurrentOrganization = Boolean(currentUser?.canManageOrganization);' in source
    assert 'const isCustomerDetailView = isViewingCustomerOrganization()' in source
    assert 'button.dataset.view === "customers"' in source
    assert 'function organizationCanView()' in source
    assert 'function organizationCanManage()' in source


def test_organization_api_base_switches_between_current_tenant_and_named_customer() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    base = source[source.index("function organizationApiBasePath()") : source.index("function organizationApiPath(")]
    # 甲方永远走服务端从会话解析出的 current 范围，不接受客户端指定企业 id。
    assert "return customerOrganizationPath(customerOrganizationId);" in base
    assert 'return "/api/organization/current";' in base
    assert "if (customerOrganizationsAvailable() && customerOrganizationId)" in base


def test_enterprise_admin_workspace_hides_customer_lifecycle_controls() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    # 企业名称修改、归档、返回客户目录、重置演示数据都是乙方生命周期操作，
    # 甲方管理员进入同一个工作区时不得出现。
    assert 'el("backToCustomersButton")?.classList.toggle("hidden", !isPlatformCustomer);' in source
    assert "async function resetCustomerOrganizationsDemo() {\n  if (!customerOrganizationsAvailable()) return;" in source
    assert 'const isPlatformCustomer = customerOrganizationsAvailable() && Boolean(selectedCustomerOrganizationId());' in source


def test_member_roles_are_only_enterprise_admin_and_member() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    labels = source[source.index("const ORGANIZATION_ROLE_LABELS") : source.index("const ORGANIZATION_STATUS_LABELS")]
    assert labels.count("admin: \"企业管理员\"") == 1
    assert labels.count("member: \"成员\"") == 1
    assert "owner" not in labels

    # 「企业主」文案、owner 选项和筛选项都不应留在前端。
    assert "企业主" not in markup
    assert "企业主" not in source
    assert '<option value="owner"' not in markup

    role_filter = markup[markup.index('id="organizationRoleFilter"') :]
    role_filter = role_filter[: role_filter.index("</select>")]
    assert '<option value="admin">企业管理员</option>' in role_filter
    assert '<option value="member">成员</option>' in role_filter

    role_input = markup[markup.index('id="organizationMemberRoleInput"') :]
    role_input = role_input[: role_input.index("</select>")]
    assert '<option value="admin">企业管理员</option>' in role_input
    assert '<option value="member">成员</option>' in role_input


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
    # The V2 capability deliberately does not reuse ``isAdmin`` as an
    # enterprise role, because a customer detail is a tenant-scoped view.
    assert "function adminBillingVisible()" in source
    assert "isPlatformAdmin() && billingAvailable && !isViewingCustomerOrganization()" in source
    assert "if (adminBillingVisible() && !adminRedemptions.length && !adminBillingOrders.length) loadAdminBillingData();" in source


def test_customer_billing_detail_is_read_only_and_stays_in_customer_scope() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    context = source[source.index("function organizationBillingContext()") : source.index("function organizationBillingContextKey()")]
    assert 'kind: "platformCustomer"' in context
    assert "readOnly: true" in context
    assert 'path: `${customerOrganizationPath(organizationId)}/billing`' in context
    assert 'path: "/api/organization/current/billing"' in context

    # The customer-detail tab cannot surface the seller's payment workflow.
    assert "if (!context || context.readOnly || !canSimulateOrganizationTopup()) return;" in source
    assert "if (!context || context.readOnly || !canSimulateOrganizationTopup() || isOrganizationTopupSaving) return;" in source


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


def test_customer_identity_labels_the_current_company_and_scopes_team_cache() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert 'const organizationName = String(currentUser.organizationName || currentUser.organization?.name || "").trim();' in source
    assert '? `${currentUser.email} · ${organizationName}`' in source
    assert source.count('`${organizationUsageScopeKey()}|${selectedTeamRef}|${startDate}|${endDate}|${source}`') == 2


def test_member_avatars_are_round_and_tone_varied_per_member() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")
    avatar_style = markup[
        markup.index(".organization-member-avatar {") : markup.index(".organization-member-identity strong,")
    ]

    # 整列同一个浅蓝方块看起来很呆板；圆形 + 按邮箱确定性取色让成员行更容易区分。
    assert "border-radius: 50%;" in avatar_style
    assert "linear-gradient" not in avatar_style
    for tone in range(1, 6):
        assert f".organization-member-avatar.tone-{tone} {{" in avatar_style
    assert 'class="organization-member-avatar ${avatarTone(member.email || name)}"' in source
    assert "const AVATAR_TONE_COUNT = 5;" in source
    assert "return `tone-${(total % AVATAR_TONE_COUNT) + 1}`;" in source


def test_member_identity_styles_do_not_deform_the_avatar_letter() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    # 头像本身就是 .organization-member-name 的直接 span 子元素，之前笼统匹配
    # `.organization-member-name span` 的规则权重更高，把 display: grid 覆盖成
    # block，字母因此跌到左上角。姓名/邮箱的排版必须限定在内层容器上。
    assert '<div class="organization-member-identity">' in source
    assert ".organization-member-name span {" not in markup
    assert ".organization-member-name strong," not in markup
