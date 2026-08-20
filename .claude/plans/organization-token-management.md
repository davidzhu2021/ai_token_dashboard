# 甲方管理员：令牌管理

给客户企业（甲方）管理员加一个「令牌管理」子页：查看本企业令牌、新增令牌（创建时多选该令牌可用的模型）、撤销令牌。沿用现有客户企业 Mock V2 的边界——不碰上游 LiteLLM、不落库、进程内确定性数据。

## 为什么不能复用现有 `/api/me/keys`

- `keysView` 与 `/api/me/keys` 是乙方员工的个人密钥链路，走 `current_upstream_user()` → 上游 `/key/list`、`/key/generate`。
- 甲方演示身份被明确挡在这条链路外：[main.py:4874](backend/main.py#L4874) 的 `/api/models` 返回 `ORGANIZATION_MODELS_FORBIDDEN`，`/api/me/keys` 返回 `ORGANIZATION_UPSTREAM_FORBIDDEN`，[app.js:4373](assets/app.js#L4373) 还会对客户身份隐藏侧栏「令牌管理」。[test_organization_routes.py:698](tests/test_organization_routes.py#L698) 断言这三条路由不得初始化上游 client。
- 所以企业令牌必须自成一套：数据落在 `organization_store.py`，模型清单也由 store 提供（不能查上游模型目录）。

## 已确认的产品决策

- **归属**：企业级令牌，创建时可选绑定一名启用成员，默认「企业共享」。
- **字段**：名称、可用模型（多选，必选至少一个）、使用人（可选）、有效期（永不过期 / 30 天 / 90 天）、每日额度上限（美元）。
- **平台侧**：乙方管理员下钻客户企业时**只读**，与「企业额度」一致——能看列表，不能新增/撤销，也没有一次性明文。

## 数据层：`backend/organization_store.py`

模块常量（放在 `_USAGE_SOURCES` 附近）：

```python
ORGANIZATION_TOKEN_MODELS = ("claude-opus-5", "claude-sonnet-4-6", "gpt-5.2", "qwen3-coder-plus", "gemini-3-pro")
TOKEN_STATUSES = frozenset({"active", "revoked", "expired"})
TOKEN_DURATIONS = frozenset({"never", "30d", "90d"})
MAX_TOKENS_PER_ORGANIZATION = 20
MIN_TOKEN_DAILY_BUDGET_USD = Decimal("1.00")
MAX_TOKEN_DAILY_BUDGET_USD = Decimal("5000.00")
DEFAULT_TOKEN_DAILY_BUDGET_USD = Decimal("100.00")
```

新 dataclass `_AccessToken`：`identifier / name / models: list[str] / member_id / status / daily_budget_usd: Decimal / duration / masked / created_at / updated_at / expires_at / revoked_at`。

**不保存明文**：创建时用 `secrets.token_hex` 生成一次性 `sk-` 值，只回一次；store 里只留 `masked`（复用 `sk-...abcd` 形状）。种子令牌的 masked 由 `sha256(org_id + token_id)` 推导，保证确定性。这样既复用了个人密钥「关窗后不可再看」的心智，也不会让 Mock 存着看似真实的凭据。

`_OrganizationState` 加 `tokens: dict[str, _AccessToken]`。三家种子企业各灌 3 条：一条绑定成员的 active、一条企业共享 active、一条 revoked，时间戳全用 `_SEED_TIMESTAMP`。

新方法（含 `OrganizationScope` 门面转发，写进 `OrganizationStore` Protocol）：

| 方法 | 规则 |
| --- | --- |
| `available_token_models()` | 返回 `list(ORGANIZATION_TOKEN_MODELS)` |
| `list_tokens(*, keyword, status, member_id, page, page_size)` | 按名称/使用人邮箱模糊搜索，状态与成员过滤，复用 `_page_value` 分页；`memberName/memberEmail/departmentName` 由 member 反查填充 |
| `create_token(name, models, *, member_id="", duration="never", daily_budget_usd=DEFAULT)` | 名称 ≤80 且同企业 active 令牌内不重名；模型去重后必须全部属于目录，至少 1 个、至多 10 个；`member_id` 若给出必须存在且 `status == "active"`；企业非 active 拒绝；active 令牌数达上限拒绝；额度 1–5000 且 ≤2 位小数；`expires_at` 按 duration 从 `_now()` 推 |
| `revoke_token(token_id)` | 只能撤销 `active`；已撤销 → Conflict；返回撤销后的 payload |

令牌变更**不动** `_touch_usage_scope`：用量行不依赖令牌，没必要让看板缓存整体失效。

对应异常沿用现有类型（`OrganizationValidationError / NotFoundError / ConflictError`），路由层再翻成令牌语义的中文。

## 路由层：`backend/main.py`

请求模型（挨着 `OrganizationBillingTopupRequest`）：

```python
class OrganizationTokenCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    models: list[str] = Field(min_length=1, max_length=10)
    memberId: str = Field(default="", max_length=128)
    duration: Literal["never", "30d", "90d"] = "never"
    dailyBudgetUsd: Decimal = Decimal("100")
```

`dailyBudgetUsd` 照抄 `OrganizationBillingTopupRequest` 的两个校验器（拒绝字符串、拒绝非有限值、最多两位小数）；`models` 逐项 strip 并拒空。

甲方侧（全部 `require_organization_demo_manager`，即仅企业管理员）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/organization/current/tokens` | 列表 + `availableModels`，支持 `search` / `status` / `memberId` / 分页 |
| POST | `/api/organization/current/tokens` | 新增；`JSONResponse` 带 `Cache-Control: no-store`，返回 `{token, secret}` |
| POST | `/api/organization/current/tokens/{token_id}/revoke` | 撤销，返回 `{token}` |

平台侧只读一条：`GET /api/platform/organizations/{organization_id}/tokens`，走 `require_platform_organization`（跨客户 id 自然 404，不泄漏存在性）。**不加**平台的 POST/revoke。

新增 `organization_token_store_error(exc)`：把 NotFound → 404「未找到对应的令牌或成员」、Conflict → 409「当前令牌状态不允许此操作」、Validation → 400「请检查令牌名称、模型或额度后重试」，错误码用 `ORGANIZATION_TOKEN_*`。不改共享的 `organization_store_error`，免得影响部门/成员的既有契约。

## 前端

### `index.html`

1. `organizationUsageTabs` 加一项 `data-organization-usage-view="tokens"` → 「令牌管理」。
2. 新 `<section id="organizationTokensView" class="view-section hidden">`：
   - 平台面包屑 `organizationTokenBreadcrumb`（照 `customerBillingBreadcrumb` 写，含「返回企业详情」）
   - hero：企业名 + 「新增令牌」主按钮 + `organizationTokenReadOnlyHint`（平台只读时显示）
   - 概览卡：令牌总数 / 生效中 / 已撤销 / 绑定成员数
   - panel：搜索框 + 状态筛选 + 表格（名称 / 可用模型 / 使用人 / 每日额度 / 状态 / 创建时间 / 过期时间 / 操作）+ 上一页下一页
   - 空态与加载态文案照 `organization-empty` 现有写法
3. `organizationTokenModal`：名称、模型多选（复用 `.model-choice-list` / `.model-choice`）、使用人 select（首项「企业共享（不绑定成员）」）、有效期 select、每日额度 number。
4. `organizationTokenSecretModal`：一次性明文（复用 `.new-key-box` + `.warning-box`）+ 复制按钮 + 「我已保存」。
5. 版本号 `app.js?v=` 改成 `20260731-organization-token-management`。

CSS 尽量复用 `.organization-*`、`.model-choice*`、`.new-key-box`；只在 `.organization-token-*` 下补少量表格列宽/徽章样式。

### `assets/app.js`

- 状态：`organizationTokens / organizationTokenTotal / organizationTokenPage / organizationTokenPageSize=20 / organizationTokenFilters {search,status} / organizationTokenModels / isOrganizationTokenLoading / isOrganizationTokenSaving / organizationTokenRequestId`，切换客户时在 `openCustomerOrganization` / `closeCustomerOrganization` 里一并重置（复用现有 `resetOrganizationBillingData` 的模式，新增 `resetOrganizationTokenData()`）。
- `organizationTokensUrl()` 走 `organizationApiPath("/tokens")`，所以甲方自动落到 `current`、平台自动落到具名客户，无需前端传企业 id。
- `organizationTokenReadOnly()` = `isViewingCustomerOrganization()`；只读时隐藏「新增令牌」、禁用「撤销」、显示只读提示。
- `renderOrganizationTokens()` / `loadOrganizationTokens()` 带 requestId + scopeKey 双重防串（照 `loadOrganizationMembers` 的写法，客户切换时丢弃在途响应）。
- 创建成功后先弹明文弹窗再刷新列表；`escapeHtml` 全量套用（模型名、使用人、名称）。
- `showOrganizationUsage` 加 `tokens` 分支 → `switchView("organization-tokens")`；`renderOrganizationUsageTabs` 里该 tab 仅在 `isViewingCustomerOrganization() || organizationCanManage()` 时可见。
- `switchView`：加 `if (view === "organization-tokens" && !organizationCanView()) view = "dashboard";`、`organizationTokensView` 的 hidden 切换、`dashboardFilters` 隐藏名单、`isCustomerDetailView` 数组补该 view、进入时按需 `loadOrganizationTokens()`。
- `loadCurrentViewData` 与 `renderCustomerUsageBreadcrumbs` 各补一条分支。

## 测试

新增 `tests/test_organization_tokens.py`（store + 路由）：

- store：种子三家企业各 3 条且互不可见；跨企业 `token_id` 取不到；模型必须属目录；重名、超上限、非 active 企业、绑定非 active 成员、撤销已撤销全部抛对应异常；明文不出现在 `list_tokens` 任何字段里。
- 路由：普通成员 403（`ORGANIZATION_MANAGE_FORBIDDEN`）；待邀请/暂停成员 403；写操作缺 CSRF 403；平台管理员 GET 200 但 POST/revoke 404（路由不存在）；平台拿 A 企业 id 读 B 企业令牌 404；创建响应含 `secret` 且带 `Cache-Control: no-store`，列表响应不含 `secret`；全程 `monkeypatch` 掉 `main.client` 断言不初始化上游。

`tests/test_frontend_organization.py` 追加：tab 与 view 结构存在、平台只读断言（`organizationTokenReadOnly` 用于禁用写按钮）、文案不含 `LiteLLM/Virtual Key/Proxy`、模型多选存在。

`tests/test_frontend_billing.py:228` 与 `tests/test_static_cache.py:20` 的 `app.js?v=` 断言同步改新值。

## 文档与收尾

- `README.md` 的「客户企业 Mock V2 演示」章节补一段令牌管理：谁能用、字段含义、明文只显示一次、平台只读、演示数据重启即恢复。
- `.env.example` 无需改动（沿用 `ORGANIZATION_DEMO_ENABLED`）。
- 验证：`python -m pytest tests/`，再在 `127.0.0.1:8000` 用 dev-login 以 `owner@demo.example`（甲方管理员）和平台管理员各走一遍新增/撤销/只读，结束后停掉临时进程。
- 最后 `git diff` review → commit → push origin master → 按 AGENTS.md 同步 JSZX-AI-03 生产并做健康检查。

## 明确不做

- 不写上游 LiteLLM（不调 `/key/generate`）：当前客户企业整体是 Mock，真实发放要等 toB 上游打通阶段统一做。
- 不做令牌用量统计（列表不显示该令牌消耗了多少 Token）：Mock 用量按成员生成，没有令牌维度，硬造会和企业看板对不上账。
- 不做改名/改模型/改额度：先只支持新增与撤销，避免在 Mock 阶段引入轮换语义。
