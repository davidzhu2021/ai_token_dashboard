# toB 多租户改造

把当前单公司（`auto-link.com.cn`）内部系统改造成可售卖给多家公司的 toB 形态：卖方保留平台超管，每家客户公司有自己的管理员，能在系统里新增部门、新增成员、分配预算、看本公司用量。本期先用 mock 数据打通全链路，上游 LiteLLM 写入放在开关后面。

## 现状盘点（决定复用边界）

| 能力 | 现状 | 改造判断 |
| --- | --- | --- |
| 管理员身份 | `.env` 的 `ADMIN_EMAILS` 白名单（[auth.py:143](backend/auth.py#L143)） | 复用，语义收窄为**平台超管**（卖方），不再是客户管理员 |
| 部门负责人 | 反推上游 team 的 `admin` 成员角色（[litellm_client.py:2236](backend/litellm_client.py#L2236)） | 保留给存量 SSO 员工；租户成员改用本地 `tenant_member.role` |
| 部门看板 | `/api/admin/departments/usage` 已按 team 聚合出 Token/金额/成功率/成员排行 | **重度复用**，加一层 tenant 过滤即可变成"公司内部门看板" |
| 本地账号 | `auth_store.py` SQLite：users/sessions/identities/upstream_accounts/provisioning_jobs | 复用，新增 tenant 系表同库（外键能生效） |
| 邮箱注册 | `AUTH_ALLOWED_EMAIL_DOMAINS` 全局白名单（[main.py:793](backend/main.py#L793)） | 改为按 `tenant_domain` 表判定归属公司 |
| 钱包账本 | `billing_store.py` Postgres：account/order/redemption，落账只在 `settle_order` 一处 CAS | 复用那处 CAS 不动，靠 `owner_type` 列区分个人/企业钱包 |
| 成员开通 | `provision_local_user` → 上游 `create_internal_user(local-<uid>)` + `no-default-models` | 复用，追加"加入部门 team"和"授予公司默认模型" |
| 邀请链路 | `auth_password_reset_tokens` + `send_auth_email` 已有完整发信与令牌消费 | 复用做成员邀请，管理员不接触密码 |

结论：**部门看板、账本落账、账号开通、发信这四条最贵的链路都能直接复用**，新增的是租户/部门/成员的组织关系层和两个管理页面。

## 角色分层

```
platform_admin   卖方（ADMIN_EMAILS）      → 管所有租户，看跨租户汇总
  └─ tenant owner  客户公司主账号          → 管本公司部门/成员/预算/钱包，不可被移除
      └─ tenant admin  客户公司管理员      → 同上，但不能改 owner、不能删公司
          └─ manager   部门负责人          → 只看本部门用量与成员
              └─ member 普通员工           → 只看自己（现有 /api/me/* 不变）
```

一个 `auth_users` 账号只归属一个租户（`tenant_member` 上加 `user_id` 唯一索引）。存量 SSO 员工不属于任何租户，行为完全不变。

## 数据模型：新增 `backend/tenant_store.py`

指向**同一个 SQLite 文件**（`AUTH_DATABASE_PATH`），与 `auth_store` 分文件但同库，这样 `tenant_member.user_id` 能对 `auth_users(id)` 建真实外键。复用 `auth_store.py` 的 `_connection()` / `_lock` / `_timestamp()` 模式，包括 WAL、`busy_timeout`、`BEGIN IMMEDIATE`。

```sql
CREATE TABLE tenant (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,                          -- 公司名
    slug TEXT NOT NULL UNIQUE,                   -- 上游 team 命名前缀
    status TEXT NOT NULL DEFAULT 'active',       -- active | suspended
    plan TEXT NOT NULL DEFAULT 'trial',          -- trial | standard | enterprise
    contact_name TEXT NOT NULL DEFAULT '',
    contact_email TEXT NOT NULL DEFAULT '',
    signup_mode TEXT NOT NULL DEFAULT 'invite',  -- invite（仅邀请）| domain（域名自助）
    default_models TEXT NOT NULL DEFAULT '[]',   -- JSON，新成员默认模型范围
    member_budget_usd REAL NOT NULL DEFAULT 0,   -- 新成员默认额度
    seat_limit INTEGER NOT NULL DEFAULT 0,       -- 0 = 不限
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE tenant_domain (
    domain TEXT PRIMARY KEY,                     -- 全局唯一 → 自助注册据此归属公司
    tenant_id TEXT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    verified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE tenant_department (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    upstream_team_id TEXT NOT NULL DEFAULT '',   -- 上游 team_id；mock 期为空
    upstream_status TEXT NOT NULL DEFAULT 'pending',  -- pending | synced | failed
    budget_usd REAL NOT NULL DEFAULT 0,          -- 部门预算上限
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, slug)
);

CREATE TABLE tenant_member (
    tenant_id TEXT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
    department_id TEXT REFERENCES tenant_department(id) ON DELETE SET NULL,
    role TEXT NOT NULL DEFAULT 'member',         -- owner | admin | manager | member
    status TEXT NOT NULL DEFAULT 'active',       -- invited | active | suspended
    member_budget_usd REAL NOT NULL DEFAULT 0,
    invited_by TEXT NOT NULL DEFAULT '',
    joined_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, user_id)
);
CREATE UNIQUE INDEX tenant_member_user_idx ON tenant_member(user_id);
CREATE INDEX tenant_member_dept_idx ON tenant_member(tenant_id, department_id);

CREATE TABLE tenant_invitation (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    department_id TEXT,
    role TEXT NOT NULL DEFAULT 'member',
    token_hash TEXT NOT NULL UNIQUE,             -- 只存 sha256，复用 hash_auth_token
    expires_at TEXT NOT NULL,
    accepted_at TEXT, revoked_at TEXT,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX tenant_invitation_lookup_idx ON tenant_invitation(tenant_id, email, created_at);
```

`tenant_store.py` 方法（约 30 个，全部同步 + `asyncio.to_thread` 包装，与 `auth_store_call` 一致）：
`create_tenant / get_tenant / get_tenant_by_slug / list_tenants / update_tenant / add_domain / remove_domain / tenant_for_domain / create_department / list_departments / update_department / archive_department / add_member / get_member / get_member_by_user / list_members / update_member / remove_member / count_members / create_invitation / get_invitation_by_token / accept_invitation / revoke_invitation / list_invitations / seat_usage`。

### 钱包改造（最小侵入）

`billing_account` 主键 `user_id` **不动**。企业钱包写成 `user_id = 'tenant:<tenant_id>'`，另加显式标记列供查询和对账：

```sql
ALTER TABLE billing_account ADD COLUMN IF NOT EXISTS owner_type TEXT NOT NULL DEFAULT 'user';
ALTER TABLE billing_order  ADD COLUMN IF NOT EXISTS tenant_id  TEXT NOT NULL DEFAULT '';
ALTER TABLE billing_order  ADD COLUMN IF NOT EXISTS owner_type TEXT NOT NULL DEFAULT 'user';
```

`settle_order` 那处 pending→success 的 CAS 事务和幂等逻辑**一行不改**，这是最关键的复用点。企业管理员充值 = 用 `tenant:<id>` 作为 owner 走同一条 `create_order` → `settle_order` 路径。成员额度分配是本地 `tenant_member.member_budget_usd` 的记账，同步到上游时写成员的 `max_budget`。

## 新增 `backend/tenant.py`

纯函数与守卫，不碰 IO：

- `slugify(name)` — 公司/部门名 → ASCII slug（中文名走拼音兜底为 `dept-<短 hash>`）
- `upstream_team_alias(tenant, department)` — `t-<tenant slug>-<dept slug>`，上游 team 命名唯一化
- `TENANT_ROLES` / `role_rank()` / `can_manage_role(actor, target)` — owner > admin > manager > member
- `require_tenant_context(request)` — 取当前用户的 `tenant_member`，无归属抛 403
- `require_tenant_admin(request)` — role ∈ {owner, admin}
- `require_department_scope(request, department_id)` — admin 全公司，manager 仅本部门
- `require_platform_admin(request)` — 复用 `auth.is_admin_email`
- `public_tenant()` / `public_department()` / `public_member()` — 出参裁剪，不泄漏 `upstream_team_id`、`token_hash`、上游 user_id

前端产品边界照旧：这些出参一律不带 `team_id`、`LiteLLM`、`Virtual Key` 等上游痕迹（AGENTS.md 的 Frontend Product Boundary）。

## 新增 `backend/demo_data.py`（mock 数据）

本地开发大概率没起 Postgres（`USAGE_SYNC_ENABLED` 默认 false），所以 mock 用量不能依赖 `usage_daily`。做一个**确定性**生成器：种子取 `sha256(user_id + date + model)`，同样输入永远同样输出，这样刷新页面数字不跳、截图可复现。

- `seed_demo_tenants(tenant_store, auth_store)` — 灌 2 家示范公司：
  - 「星海智能」slug `xinghai`，4 部门（研发中心 / 产品设计 / 市场运营 / 客户成功），14 名成员，1 owner + 1 admin + 4 manager
  - 「云枢科技」slug `yunshu`，3 部门，9 名成员
  - 成员账号用 `demo-<n>@<domain>` + 统一演示密码，`email_verified=1`，`tenant_member.status='active'`
- `demo_usage_rows(members, start_date, end_date, source)` — 生成 30 天用量行，字段与 `usage_store.personal_rows` 返回的 row 结构**逐字段对齐**（`date/source/model/promptTokens/completionTokens/totalTokens/requestCount/successCount/failureCount/spend`），这样下游 `usage_summary()`、`_department_summaries()` 这些聚合函数不用改。
- 来源分布贴合真实：Codex 45% / Claude Code 35% / Her 12% / 其他 8%；模型用现有 `model_display_name` 认得的名字。
- 入口：`POST /api/platform/demo/seed`（仅平台超管 + 仅 `DEMO_MODE=true`）和 `python -m backend.demo_data` 命令行两种。

`DEMO_MODE=true` 时，`/api/org/usage` 与 `/api/org/members` 在 `usage_store` 不可用时回落到生成器；`DEMO_MODE=false` 时这条回落路径整个关闭，绝不可能在生产返回假数字。

## 路由（`backend/main.py`）

### 平台侧（`require_platform_admin`）
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/platform/tenants` | 租户列表：成员数/席位/状态/套餐/本期用量 |
| POST | `/api/platform/tenants` | 开户：公司名、域名、套餐、席位、默认模型、owner 邮箱 |
| GET | `/api/platform/tenants/{id}` | 租户详情 + 部门 + 成员概况 |
| PATCH | `/api/platform/tenants/{id}` | 改套餐/席位/状态/默认模型 |
| POST | `/api/platform/tenants/{id}/domains` | 加域名 |
| DELETE | `/api/platform/tenants/{id}/domains/{domain}` | 删域名 |
| POST | `/api/platform/demo/seed` | 灌 mock（仅 `DEMO_MODE`） |

### 企业侧（`require_tenant_admin`，manager 只读本部门）
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/org/profile` | 公司信息、席位占用、注册方式、钱包余额 |
| GET | `/api/org/overview` | 概览：Token/金额/请求数/成功率/活跃成员/部门排行 |
| GET/POST | `/api/org/departments` | 部门列表 / **新增部门** |
| PATCH/DELETE | `/api/org/departments/{id}` | 改名、改预算 / 归档（有成员时拒绝并提示先转移） |
| GET/POST | `/api/org/members` | 成员列表 / **新增成员**（管理员直接建号） |
| PATCH | `/api/org/members/{user_id}` | 改部门、改角色、改额度、停用/恢复 |
| DELETE | `/api/org/members/{user_id}` | 移出公司（owner 不可移除） |
| POST | `/api/org/members/invite` | 发邀请邮件 |
| GET | `/api/org/invitations` | 邀请列表（pending/已接受/已过期） |
| DELETE | `/api/org/invitations/{id}` | 撤销邀请 |
| GET | `/api/org/usage` | 公司/部门用量，走 tenant 过滤后的 `department_usage_payload` |
| GET | `/api/org/members/{user_id}/usage` | 单个成员用量（管理员或其部门 manager） |
| GET | `/api/org/billing` | 企业钱包余额、充值流水、部门/成员额度分配 |
| POST | `/api/org/billing/orders` | 企业充值下单（复用现有 `create_topup_order` 主体） |
| POST | `/api/org/billing/allocate` | 给部门/成员分配额度 |

### 公开
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/auth/invitation/{token}` | 校验邀请，返回公司名/部门/邮箱（不返回令牌本身） |
| POST | `/api/auth/invitation/{token}/accept` | 设密码 → 建号 → 入职 → provisioning |

### 现有路由的改造点
- [main.py:793](backend/main.py#L793) `validate_public_signup_email`：先查 `tenant_for_domain(domain)`；命中且 `signup_mode='domain'` 且未超席位 → 放行并记住 tenant；命中但 `invite` → 报"该公司仅支持邀请注册"；未命中 → 回落现有 `AUTH_ALLOWED_EMAIL_DOMAINS`（保住存量 toC）。
- [main.py:2255](backend/main.py#L2255) `register`：注册成功后自动 `add_member(tenant, user, role='member')`，部门留空由管理员分配。
- [main.py:936](backend/main.py#L936) `auth_user_payload`：追加 `tenant: {id, name, plan}`、`tenantRole`、`department`、`isTenantAdmin`、`isPlatformAdmin`。
- [main.py:2406](backend/main.py#L2406) `auth_scope`：现在对本地账号直接返回空 scope；改为返回租户 scope（`isTenantAdmin` / `isDepartmentManager` / 可见部门列表），前端据此显示导航。
- [main.py:1013](backend/main.py#L1013) `provision_local_user`：成功后若有租户归属，追加"授予公司 `default_models`"和"加入部门 team"（后者受 `TENANT_UPSTREAM_SYNC` 开关控制）。
- `require_admin`（[auth.py:213](backend/auth.py#L213)）：语义改为平台超管，`/api/admin/*` 全员看板保持只给卖方。

## 上游同步（`TENANT_UPSTREAM_SYNC`，默认 false）

按 AGENTS.md 的 LiteLLM Reference Requirement，端点已对 `D:\litellm` 核实：

- 建部门 → `POST /team/new`（`team_alias` = `upstream_team_alias()`、`models` = 公司默认模型、`max_budget` = 部门预算），回写 `upstream_team_id`（[team_endpoints.py:896](file:///D:/litellm/litellm/proxy/management_endpoints/team_endpoints.py#L896)）
- 成员入部门 → `POST /team/member_add`（`{team_id, member: {role, user_id}}`，manager 映射 `admin`，member 映射 `user`）（[team_endpoints.py:2423](file:///D:/litellm/litellm/proxy/management_endpoints/team_endpoints.py#L2423)）
- 调部门预算 → `POST /team/update`；调成员额度 → 现有 `set_user_budget`
- 移出成员 → `POST /team/member_delete`

失败**不阻塞本地写入**，写 `upstream_status='failed'` 并复用现有 `auth_provisioning_jobs` 队列补偿（`enqueue_provisioning` 已在 [auth_store.py:978](backend/auth_store.py#L978)），健康检查暴露积压数。这与现在充值同步失败的处理策略一致——本地是真相源，上游是执行副本。

不用 `/organization/*`：org 是上游企业版特性，靠 team 命名前缀做隔离对上游版本兼容性更好。

## 前端

`index.html` 新增两个 view（照 `adminBillingSection` 的 panel 结构写），`assets/app.js` 新增对应 state/render/loader。**必须同步改 [index.html:6315](index.html#L6315) 的 `app.js?v=` 版本号**，否则浏览器缓存旧脚本。

### `orgView`（企业管理，nav `data-view="org"`，仅 tenant admin 可见）
子 tab 五个：
- **概览** — 公司 Token/金额/请求/成功率卡片 + 部门用量排行表 + 席位占用
- **部门** — 部门表（名称/成员数/本期 Token/金额/预算/状态）+「新增部门」弹窗（名称、预算、负责人）+ 行内改名改预算
- **成员** — 成员表（姓名/邮箱/部门/角色/状态/本期用量/额度）+「新增成员」弹窗（邮箱、姓名、部门、角色、额度、开通方式二选一：发邀请 / 设初始密码）+ 行内改部门改角色 + 停用/移除
- **邀请** — 待接受邀请列表 + 撤销 + 重发
- **企业钱包** — 余额/累计充值/本期消耗 + 充值入口（复用现有充值 UI 组件）+ 部门额度分配表

### `platformView`（平台管理，nav `data-view="platform"`，仅 ADMIN_EMAILS 可见）
- 租户表（公司/套餐/席位/成员数/本期用量/状态）+「开通新公司」弹窗 + 域名管理 + 停用/恢复
- `DEMO_MODE` 下多一个「灌入示范数据」按钮

### 现有前端改动
- [app.js:3733](assets/app.js#L3733) `switchView`：加 `org` / `platform` 两个分支和权限兜底（照现有 `view === "admin" && !currentUser?.isAdmin` 的写法）
- [app.js:4108](assets/app.js#L4108) `loadAuthScope`：按 `isTenantAdmin` / `isPlatformAdmin` 切换两个 nav 项的 hidden
- 用户 pill 下加公司名，让员工知道自己在哪家公司的空间里
- 登录页/邀请页：`?invite_token=` 走接受邀请流程（照 `takeResetPasswordTokenFromUrl` 的写法，用完立刻从 URL 抹掉）

## 测试（`tests/`，pytest，mock 上游）

- `test_tenant_store.py` — CRUD、`user_id` 唯一索引拦重复归属、部门 slug 唯一、席位计数、邀请令牌只存哈希
- `test_tenant_roles.py` — `can_manage_role` 矩阵、manager 越权访问他部门 403、member 访问 `/api/org/*` 403、owner 不可被移除
- `test_org_routes.py` — 建部门/建成员/改成员/移除的完整流；跨租户读取隔离（A 公司管理员拿 B 公司 department_id 必须 404 而非 403，不泄漏存在性）
- `test_platform_routes.py` — 开户、域名唯一、非 `ADMIN_EMAILS` 一律 403
- `test_tenant_signup.py` — 域名归属注册、`invite` 模式拒绝自助、席位满拒绝、未命中域名回落存量白名单
- `test_tenant_invitation.py` — 邀请接受建号入职、过期/已用/已撤销令牌拒绝、令牌不出现在响应里
- `test_demo_data.py` — 生成器确定性（同输入同输出）、`DEMO_MODE=false` 时假数据路径完全关闭
- `test_frontend_org.py` — 照 `test_frontend_billing.py` 断言 `orgView`/`platformView` 结构、导航默认 hidden、`app.js?v=` 已更新

## 环境变量（`.env.example`）

```bash
# toB 多租户
TENANT_ENABLED=false          # 关闭时所有 /api/org/* 与 /api/platform/* 返回 404，存量行为零变化
TENANT_UPSTREAM_SYNC=false    # 打开后建部门/加成员写上游
DEMO_MODE=false               # 仅本地演示；true 才允许灌 mock 与假用量回落
TENANT_DEFAULT_SEAT_LIMIT=50
TENANT_INVITATION_TTL_HOURS=168
```

`TENANT_ENABLED=false` 是硬开关：新表照建但路由不挂、注册链路不改判定。这样这次改造合并后生产行为与今天完全一致，等你验收完再开。

## 实施顺序

1. **P0 骨架**（可验收：能建公司、建部门、建成员、看到 mock 用量）
   `tenant_store.py` + `tenant.py` + `demo_data.py` + 平台/企业路由 + `orgView`/`platformView` 前端 + `test_tenant_store.py` / `test_tenant_roles.py` / `test_org_routes.py` / `test_demo_data.py`
2. **P1 开通与归属**：邀请链路、域名自助注册、`auth_user_payload`/`auth_scope` 扩展 + 对应测试
3. **P2 钱包与预算**：企业钱包 `owner_type`、企业充值、部门/成员额度分配 + 测试
4. **P3 上游打通**：`TENANT_UPSTREAM_SYNC` 下的 `/team/new`、`/team/member_add`、预算写入与失败补偿 + 测试
5. **收尾**：`README.md` 补 toB 章节（角色矩阵、开户流程、接口概览、演示数据说明）、`.env.example` 补开关、`git diff` review → commit → push → 生产同步

每个 P 阶段结束我会跑一遍 `pytest` 和 `127.0.0.1:8000` 上的手工验证再进下一阶段，你可以在任意阶段之间叫停或调方向。

## 明确不做

- 不做租户独立子域/独立数据库（单库 + 行级隔离足够，客户量上百再拆）
- 不做客户自助注册开公司（开户走你的平台后台，避免垃圾租户）
- 不做发票/合同/审批流（超出用量系统边界，客户通常自有财务系统）
- 不动 `/api/me/*` 和现有 SSO 员工链路的任何行为
