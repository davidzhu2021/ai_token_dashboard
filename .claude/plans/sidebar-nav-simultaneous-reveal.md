# 侧边栏导航项同时揭示

## 问题

总管理员 / 团队 leader 登录后，侧边栏里「我的用量」和「令牌管理」立刻出现，其余项延迟数百毫秒到 1 秒才逐个蹦出来。

## 根因

三层原因叠加：

1. **静态 vs 条件渲染不对称**。`index.html` 的 7 个 `.view-tab` 里，「我的用量」「令牌管理」没有 `hidden` 类，appView 一显示就在；其余 5 项初始 `hidden`，等权限探测回来才揭示。
2. **揭示时刻分散在 3 个回调里**，不是一次性的：
   - `showApp()` [app.js:5347-5349](../../assets/app.js#L5347-L5349) 先强制隐藏 `teamTab`/`billingTab`
   - `loadAuthScope()` [app.js:5385](../../assets/app.js#L5385) 揭示「团队看板」——等 `/api/auth/scope`
   - `refreshBillingAvailability()` [app.js:3567](../../assets/app.js#L3567) 揭示「充值中心」——等 `/api/me/billing`
3. **两次往返串行**。`/api/auth/scope` 只在 `/api/auth/me` 返回后才发起（[app.js:6645](../../assets/app.js#L6645) → [app.js:5373](../../assets/app.js#L5373)），延迟直接相加。而 `/api/auth/me` 是**故意**不解析团队范围的（[main.py:2887](../../backend/main.py#L2887) 返回 `teamBoardStatus: "loading"`，由 `test_auth_me_returns_base_identity_without_resolving_scope` 锁定），不能在那里补。

两个慢接口的成本不同：

- `/api/me/billing` [main.py:4372-4376](../../backend/main.py#L4372-L4376) 夹了一次上游 `client().user_info()`，但决定**标签是否可见**只需要 `BILLING_ENABLED` 环境开关 + 「账号有无本地 id」，两者都是零 I/O。**这个是纯粹的浪费，可彻底消除。**
- `/api/auth/scope` [main.py:3099](../../backend/main.py#L3099) → `team_scope_for_user` → 上游 `team_leader_scope`。`team_auth_cache` 命中时（TTL 300s）几乎免费；冷缓存时无法避免。

## 方案（已确认：整栏一起等，一次性揭示）

### 1. 新增零成本的充值可见性探针（后端）

`GET /api/auth/scope` 的返回值里增加 `billingAvailable` 字段，值取 `billing.billing_enabled() and require_billing_store 可用 and 该账号有本地 id`——全部本地判断，不碰上游。

这样「充值中心」的可见性与团队权限**在同一个响应里**返回，不再需要等 `/api/me/billing`。`/api/me/billing` 保留原样，仍由进入充值页时按需调用去取余额与订单。

注意：需保持 `isMockCustomerIdentity()` 分支语义不变——演示客户身份走企业额度合约，`billingAvailable` 对其恒为 false。

### 2. 侧边栏骨架占位 + 一次性揭示（前端）

- `index.html`：给 `<aside class="sidebar">` 的 `.view-tabs` 加初始态 `nav-pending` 类；7 个标签**全部**带 `hidden`（含现在静态可见的两项和默认可见的 `departmentTab`，消除现存的不一致）。
- 新增 CSS：`.view-tabs.nav-pending` 下渲染 3 条 `loading-line` 风格的骨架条，复用已有的 `loadingShimmer` 动画（[index.html:2207-2220](../../index.html#L2207-L2220)），不引入新动画。
- `syncNavigationVisibility()`：改为在**权限全部落地后**才调用一次，调用时移除 `nav-pending` 并按权限一次性 `toggle` 全部 7 项。
- `showApp()`：删掉现在那两行提前的 `classList.add("hidden")` 与提前的 `syncNavigationVisibility()`；改为 `await` 权限探测（`loadAuthScope()`）完成后再揭示导航，然后才做数据加载。

关键：`switchView("dashboard")` 与 `render()` 的调用时机不动——主区域内容照旧立即渲染，只有侧边栏进骨架态。用户不会觉得整页变慢，只是导航栏晚 300ms 一次性成形。

### 3. 收尾

- 同步 `index.html` 末尾 `app.js?v=` 版本号（现为 `20260730-organization-billing`），并更新 `tests/test_static_cache.py` 里的断言。项目约定：改 app.js 必须换版本号，否则线上命中旧缓存。

## 测试

在 `tests/` 下新增/扩展：

- `test_team_leader.py`：`/api/auth/scope` 返回 `billingAvailable`；确认 `test_auth_me_returns_base_identity_without_resolving_scope` 仍通过（不能把团队解析挪进 `/api/auth/me`）。
- 新增 `tests/test_frontend_sidebar_reveal.py`：断言 7 个 `.view-tab` 在初始 HTML 中全部带 `hidden`、`.view-tabs` 带 `nav-pending`、骨架标记存在。
- `test_static_cache.py`：更新版本号断言。
- 演示客户身份下 `billingAvailable` 为 false（`test_organization_billing.py` 或 `test_frontend_billing.py`）。

跑 `python -m pytest tests/ -q` 全量回归——本次改动碰到共享的登录引导路径，不能只跑新增文件。

## 手工验证

`127.0.0.1:8000` 起服务（端口占用时先查进程、复用本项目实例，不切换端口），用 `DEV_LOGIN_ENABLED=true` 分别以管理员、团队 leader、普通员工登录，确认：
7 项同时出现、无逐项蹦出、无布局跳动；冷缓存（重启服务清 `team_auth_cache`）与热缓存两种情况都看一遍。验证完停掉临时服务。

## 不做的事

- 不改 `/api/auth/me` 的团队解析行为（有测试锁定，且那是刻意的性能设计）。
- 不给 `team_auth_cache` 加预热或后台刷新——超出本次「同时出现」的诉求。
- 不动 `/api/me/billing` 本身的上游调用。
