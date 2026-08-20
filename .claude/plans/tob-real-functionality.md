# toB 侧 mock 转真实功能

把客户企业模块从进程内确定性 Mock 改成真实功能：组织目录与成员身份落库、成员可真实登录、企业/部门/成员看板读真实用量、企业令牌在网关真实签发。企业额度与充值本轮不动（继续沿用现有模拟充值）。

## 现状与关键接缝

`backend/organization_store.py`（2637 行）是全部 toB 数据的进程内 `InMemoryOrganizationStore`：种子三家客户、`sha256` 生成的确定性假用量、`sk-` 令牌只存掩码且调不通任何模型、`$5000` 种子余额。所有路由都经 `organization_store_call` / `organization_scoped_store_call` 以 `getattr` + `asyncio.to_thread` 调用，且已定义 `OrganizationStore` Protocol（backend/organization_store.py:92）。

**这意味着换存储不必改路由和前端**：只要新实现满足同一 Protocol，`backend/main.py` 里 20 多个 toB 路由与 `assets/app.js`、`index.html` 全部保持不变。这是本方案的主轴，也符合"以主分支界面为主"。

可复用的既有真实设施：

- `backend/auth_store.py`：`auth_users` 表、`create_user`、`get_user_by_email`、密码哈希、邀请/验证码链路，以及 `auth_upstream_accounts`（本地账号 ↔ 网关 user_id 映射）。
- `backend/usage_store.py`：`rows_by_employee_emails(emails, start, end, source, backend_ids)`（backend/usage_store.py:713）按成员邮箱集合返回真实用量行，形状与 Mock 行几乎一致。
- `backend/litellm_client.py`：`create_key`（backend/litellm_client.py:1571）、`delete_key`、`available_key_models`、`organization_token_models`。
- `worktree-tob-multi-tenant` 分支的 `tenant_store.py`（1142 行）/`tenant.py`：真实 SQLite 租户表、成员账号+邀请、上游 team 同步与 `upstream_status` 补偿状态机。**复用其 schema 设计、约束与上游同步策略**，但不采用其 `/api/org/*` 路由与前端（那会替换掉 master 的界面）。

`_summary_rows` / `_employee_summaries` / `_department_summaries`（backend/organization_store.py:2021-2117）是纯函数，只依赖行数据与目录状态，可直接服务真实行。

## 实施步骤

### 1. 新增 `backend/organization_repository.py`：真实持久化

SQLite，与 `auth_store` 指向同一库文件（复用其短连接 + WAL + `BEGIN IMMEDIATE` + `PRAGMA foreign_keys=ON` 模式），使 `organization_member.user_id` 能对 `auth_users(id)` 建真实外键。表：

- `organization`(id, name, status, created_at, updated_at, archived_at)
- `organization_domain`(organization_id, domain) — 邮箱域名归属，用于登录时解析企业
- `organization_department`(id, organization_id, name, status, upstream_team_id, upstream_status, upstream_error, ...)
- `organization_member`(id, organization_id, user_id→auth_users, name, email, department_id, role, status, team_role, ...)
- `organization_invitation`(id, organization_id, email, token_hash, role, department_id, expires_at, ...)
- `organization_token`(id, organization_id, name, models JSON, member_id, status, daily_budget_usd, duration, masked, upstream_key_id, expires_at, revoked_at, ...)

移植分支 `tenant_store.py` 已验证的约束：唯一 slug/域名、席位上限、最后一名管理员不可停用/降级、跨企业邮箱不可重复归属。保留 master `InMemoryOrganizationStore` 现有的邮箱规范化与字段校验（`normalize_email`、`_required_text` 等）——照搬，避免校验语义回退。

实现 `OrganizationStore` Protocol 的全部目录方法，返回 payload 形状与 Mock 逐字段一致（`isDemo` 改为 `false`）。

### 2. 成员身份与登录

- `create_member` 在同一事务内建 `auth_users` 账号并建立 `organization_member.user_id` 外键。两种开通方式沿用分支做法：`invite`（建 pending 账号 + 发邀请邮件，成员自设密码，复用 `send_auth_email_sync` 与 SMTP 配置）与 `password`（管理员设初始密码，不依赖 SMTP）。
- 登录时按邮箱域名经 `organization_domain` 解析企业成员身份，替换 `known_demo_member_email` 的 dev-login 特例与 `@demo.example` 种子身份。`backend/main.py` 中 `is_demo_customer_user`、`organization_memberships_for_user`、`require_non_inactive_demo_identity` 改为查真实成员表；`invited`/`suspended`/`archived` 的失败语义保持不变。
- 平台管理员仍绝不由客户角色推导 `isPlatformAdmin`（保持现有不变式）。

### 3. 用量真实归属

- 在 store 里新增可注入的用量数据源。因 `usage_store` 是 async 而 Protocol 是同步，用量取数放在 `backend/main.py` 侧：先同步取企业成员邮箱与部门映射，再 `await usage_store().rows_by_employee_emails(...)`，最后把真实行喂给现有纯聚合函数生成同形状 payload。
- 企业隔离由"只传本企业成员邮箱"保证，与现有 `require_platform_organization` / 会话解析的企业作用域叠加。
- `dataQuality.summarySource` 由 `deterministic_mock` 改为真实来源标记；Postgres 未覆盖该日期区间时 `rows_by_employee_emails` 返回 `None`，此时按现有 503 语义返回"用量数据同步中"，**不回落假数据**。
- 移除 `_usage_rows_for_state` 与 `_USAGE_SOURCES` 假用量生成、`has_mock_usage` 字段。

### 4. 令牌真实签发

- `create_token` 改为经 `litellm_client` 在网关 `/key/generate` 真实签发：`models` 取管理员勾选的展示名所覆盖的全部上游原始名（沿用 master 已实现的 `organization_token_model_options` 归组语义），`max_budget`/`budget_duration` 落每日额度，`duration` 落有效期，`metadata` 带企业与成员标识。
- 绑定成员的令牌挂到该成员的网关账号（经 `auth_upstream_accounts`），企业共享令牌挂到企业级账号。
- 本地只存 `masked` 与 `upstream_key_id`；明文仍只在创建响应返回一次并保持 `Cache-Control: no-store`。
- `revoke_token` 调 `/key/delete` 真实失效，本地保留审计行。上游失败按分支的补偿策略标 `upstream_status=failed` 并给出明确中文错误，不静默成功。
- 保留每企业 20 个上限、同名生效令牌不可重复、撤销后原名可复用。

### 5. 配置、迁移与清理

- 用 `ORGANIZATION_ENABLED` 取代 `ORGANIZATION_DEMO_ENABLED` 语义（真实功能不再是"演示"），`.env.example` 与 README 同步更新；生产可开启。
- 移除 `/api/platform/organizations/demo/reset` 重置种子路由与前端入口（真实数据不能被一键重置）。
- 保留 `InMemoryOrganizationStore` 仅供测试用作 Protocol 双测，或按现有 `demo_data.py` 思路把种子改为"往真实库灌示范企业"的一次性脚本。
- 前端仅改必要文案：去掉"演示/模拟"字样与 `isDemo` 相关提示；界面结构、导航与交互不动。按既有约定给 `app.js` 版本号加缓存破坏。

### 6. 测试

新增 `tests/test_organization_repository.py`（目录 CRUD、外键、席位与最后管理员约束、跨企业隔离）、`tests/test_organization_usage_real.py`（真实行聚合、企业隔离、未覆盖日期不回落假数据）、`tests/test_organization_token_upstream.py`（签发/撤销 mock 上游、失败标状态、明文只返回一次）。改造现有 `test_organization_*.py` / `test_customer_organization_store_v2.py` 使其针对真实实现；上游与 SMTP 全部 mock。

## 验证

`python -m pytest tests/ -q`，随后在 `127.0.0.1:8000` 手工验证：`GET /api/health`、企业管理员登录、部门与成员增改、成员真实登录、三类看板读到真实用量、令牌签发后实际调用网关成功、撤销后调用失败。

## 范围说明

企业额度与充值保持现状（模拟充值、`$5000` 种子余额）——按你的选择本轮不真实化。这会留下一个可见的不一致：目录、用量、令牌都是真实的，而企业余额仍是模拟值，令牌消费不会扣减企业额度。若需要一致，可在本轮完成后接第二阶段（企业钱包落 `billing_store`、真实渠道充值、上游额度同步）。
