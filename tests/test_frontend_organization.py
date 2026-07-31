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


def test_token_management_is_a_sidebar_destination_for_the_customer_admin() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    # 甲方管理员从侧边栏一级项进入令牌管理，企业详情页上方不再重复这一项。
    assert 'id="organizationTokensTab" class="view-tab hidden" type="button" data-view="organization-tokens"' in markup
    assert 'id="organizationTokensView" class="view-section organization-view hidden"' in markup
    assert 'el("organizationTokensTab")?.classList.toggle("hidden", !canManageCurrentOrganization);' in source

    # 令牌项自己高亮，不再借用 organization 那一项。
    assert "const sidebarView" not in source
    assert 'el("organizationTokensView")?.classList.toggle("hidden", view !== "organization-tokens");' in source
    assert 'if (view === "organization-tokens" && !canViewOrganizationTokens()) view = "dashboard";' in source
    assert 'if (currentView === "organization-tokens") return loadOrganizationTokens();' in source


def test_platform_drilldown_keeps_a_token_tab_inside_the_customer_workspace() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    # 乙方运营的侧边栏没有企业管理，只能从客户企业下钻，所以那条标签是他们唯一的
    # 只读入口，必须保留并且只对下钻场景可见。
    assert 'id="organizationTokensScopeTab" class="ghost-btn hidden" type="button" role="tab" data-organization-usage-view="tokens"' in markup
    assert 'if (tokensTab) tokensTab.classList.toggle("hidden", !isViewingCustomerOrganization());' in source
    assert '["info", "usage", "departments-usage", "billing", "tokens"].includes(view)' in source
    assert 'switchView("organization-tokens");' in source


def test_token_requests_stay_in_the_resolved_organization_scope() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    url = source[source.index("function organizationTokensUrl()") : source.index("function resetOrganizationTokenData()")]
    # 列表读取复用 organizationApiPath，甲方走 current、乙方走下钻路径，
    # 客户端始终不发送自己的企业 id。
    assert 'return `${organizationApiPath("/tokens")}?${params.toString()}`;' in url
    assert '"/api/platform/organizations' not in url

    # 写操作只有甲方自己的 current 路径，乙方没有对应入口。
    assert 'await api("/api/organization/current/tokens", {' in source
    assert "await api(`/api/organization/current/tokens/${encodeURIComponent(tokenId)}/revoke`, {" in source

    # 切换客户会作废在途响应，避免上一家企业的令牌串到当前列表里。
    assert "if (requestId !== organizationTokenRequestId || scopeKey !== organizationUsageScopeKey()) return;" in source
    assert "resetOrganizationTokenData();" in source


def test_platform_drilldown_sees_tokens_as_read_only() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    assert 'id="organizationTokenReadOnlyHint" class="organization-billing-readonly hidden"' in markup
    assert "function organizationTokenReadOnly() {\n  return isViewingCustomerOrganization();\n}" in source
    assert "return Boolean(!organizationTokenReadOnly() && organizationCanManage());" in source
    assert "if (!organizationTokenCanManage()) return;" in source
    assert "if (!organizationTokenCanManage() || isOrganizationTokenSaving) return;" in source
    assert "if (!organizationTokenCanManage() || !revokingOrganizationTokenId || isOrganizationTokenRevoking) return;" in source


def test_token_creation_lets_the_admin_pick_models_member_duration_and_budget() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    modal = markup[markup.index('id="organizationTokenModal"') : markup.index('id="organizationTokenSecretModal"')]
    assert 'id="organizationTokenModelChoices" class="model-choice-list"' in modal
    assert '<option value="">企业共享（不绑定成员）</option>' in modal
    for duration in ('value="never"', 'value="30d"', 'value="90d"'):
        assert duration in modal
    assert 'id="organizationTokenBudgetInput" class="input" type="number" min="1" max="5000"' in modal

    assert 'name="organizationTokenModel"' in source
    assert "body: JSON.stringify({ name, models, memberId, duration, dailyBudgetUsd: budget })," in source
    assert '"请至少选择一个可用模型。"' in source


def test_model_choices_show_sanitized_names_and_submit_raw_gateway_names() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    normalize = source[
        source.index("function normalizeOrganizationTokenModels(payload)") : source.index(
            "function renderOrganizationTokenModelChoices()"
        )
    ]
    # 新契约优先，旧字段保留：浏览器缓存着上一版 bundle 的用户不会看到空目录。
    assert "payload?.availableModelOptions" in normalize
    assert "payload?.availableModels" in normalize

    choices = source[
        source.index("function renderOrganizationTokenModelChoices()") : source.index(
            "function renderOrganizationTokenMemberOptions()"
        )
    ]
    # 可见文本是脱敏名，value 是目录下标——原始模型名不出现在 DOM 里。
    assert "${escapeHtml(model.displayName)}" in choices
    assert 'value="${index}"' in choices
    assert "model.names" not in choices

    selected = source[
        source.index("function selectedOrganizationTokenModels()") : source.index(
            "function closeOrganizationTokenModal(options = {})"
        )
    ]
    # 提交时展开成该选项覆盖的全部上游原始名：同一模型的多条线路一起授权。
    assert "organizationTokenModels[Number(input.value)]" in selected
    assert "option?.names" in selected


def test_new_token_secret_is_shown_once_and_never_re_rendered_from_the_list() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    secret_modal = markup[
        markup.index('id="organizationTokenSecretModal"') : markup.index('id="organizationTokenRevokeModal"')
    ]
    assert "完整令牌只展示这一次" in secret_modal
    assert 'id="organizationTokenSecretValue" class="new-key-value"' in secret_modal
    assert 'id="closeOrganizationTokenSecret" class="primary-btn"' in secret_modal

    # 关闭弹窗即丢弃明文；列表渲染只用后端返回的 masked，绝不拼接 secret。
    assert 'setText("organizationTokenSecretValue", "");' in source
    tokens_render = source[
        source.index("function renderOrganizationTokens()") : source.index("async function loadOrganizationTokens()")
    ]
    assert "secret" not in tokens_render
    assert 'organizationField(token, "masked", "masked") || "sk-...----"' in tokens_render


def test_token_table_escapes_every_rendered_value() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    tokens_render = source[
        source.index("function renderOrganizationTokens()") : source.index("async function loadOrganizationTokens()")
    ]
    # 令牌名称与绑定成员由管理员自由填写，任何未转义插值都会变成存储型 XSS。
    for expression in (
        "${escapeHtml(name)}",
        "${escapeHtml(masked)}",
        # 模型标签来自上游目录，同样不可信。
        "${escapeHtml(label)}",
        "${escapeHtml(memberName)}",
        "${escapeHtml(id)}",
    ):
        assert expression in tokens_render
    assert "${name}" not in tokens_render
    assert "${masked}" not in tokens_render
    assert "${label}" not in tokens_render


def test_token_copy_does_not_expose_backend_provider_terms() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")
    token_markup = markup[markup.index('id="organizationTokensView"') : markup.index('id="keysView"')]
    token_source = source[
        source.index("const ORGANIZATION_TOKEN_STATUS_LABELS")
        : source.index("function closeCustomerOrganizationModal(")
    ]

    for term in ("LiteLLM", "Proxy", "Virtual Key", "upstream", "admin key"):
        assert term not in token_markup
        assert term not in token_source
