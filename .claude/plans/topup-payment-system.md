# 充值支付系统

参照 New API（蓝移API）形态，为注册用户补齐「余额 + 充值 + 订单流水」闭环。

## 调研结论与渠道决策

抓取 `Calcium-Ion/new-api` 源码（`controller/topup.go`、`model/topup.go`、`model/redemption.go`）与参考站 `/api/status` 实测配置得出：

- 参考站口径：`price=7.3`（7.3 元 = 1 美元额度）、`quota_per_unit=500000`、`quota_display_type=USD`。与你选的「¥ 充值 / $ 消耗」一致。
- New API 的落账核心是一个事务：CAS 更新订单状态 → 加余额 → 记流水。兑换码、易支付、Stripe、管理员补单全部汇入这一个事务。
- 兑换码防重用三层：事务 + `FOR UPDATE` 行锁 + `WHERE status=enabled` 的 CAS（靠 `RowsAffected==0` 判并发冲突）。
- 易支付：MD5 签名，`submit.php` 跳转下单，回调走 query 参数验签，必须原样返回纯文本 `success`，否则网关重推。

**决策：本期实现兑换码 + 易支付；Stripe 只留适配层不实现。**

- 兑换码零外部依赖、当天可上线，且是所有渠道的落账底座，先把它做对。
- 易支付是国内中转站事实标准（参考站即是），需要你提供 PID/KEY/网关地址；未配置时入口自动隐藏，配好即生效。
- Stripe 面向海外信用卡，你的场景用不上且需真实商户账号联调，本期不做。

## 已确认的设计决策

| 决策点 | 选择 |
| --- | --- |
| 额度模型 | 本地账本为真相源 + 同步写上游 `max_budget` |
| 新用户 | 保持 `no-default-models`，首次充值成功后自动授予模型权限 |
| 计价单位 | ¥ 充值、$ 余额与消耗（汇率可配） |
| 回调端点 | 免登录，靠 MD5 验签 + 订单号 CAS + 金额比对三道校验 |
| key 日限额 | 充值后同步抬高 key 日限额 |

> 注：「不要动 key 相关的」是上一个任务（用量归日修复）的约束。本期你明确选择同步抬高 key 日限额，按当前决定执行。

## 数据模型

新增 `backend/billing_store.py`，复用 `usage_store.py` 的 asyncpg 连接池模式，建三张表（Postgres，与 `usage_daily` 同库）：

```sql
CREATE TABLE billing_account (
    user_id TEXT PRIMARY KEY,             -- auth_users.id
    balance_usd NUMERIC(16,6) NOT NULL DEFAULT 0,   -- 已充值总额（美元额度）
    topup_total_usd NUMERIC(16,6) NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE billing_order (
    trade_no TEXT PRIMARY KEY,            -- 我方订单号
    user_id TEXT NOT NULL,
    channel TEXT NOT NULL,                -- redemption | epay | manual
    amount_usd NUMERIC(16,6) NOT NULL,    -- 到账美元额度
    money_cny NUMERIC(12,2) NOT NULL DEFAULT 0,  -- 应付人民币
    exchange_rate NUMERIC(10,4) NOT NULL, -- 下单时汇率快照
    status TEXT NOT NULL,                 -- pending | success | failed | expired
    payment_method TEXT NOT NULL DEFAULT '',   -- alipay | wxpay
    upstream_trade_no TEXT NOT NULL DEFAULT '',
    notify_payload JSONB,
    created_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ
);
CREATE INDEX billing_order_user_idx ON billing_order (user_id, created_at DESC);
CREATE INDEX billing_order_status_idx ON billing_order (status, created_at);

CREATE TABLE billing_redemption (
    id TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL UNIQUE,       -- 只存哈希，明文仅生成时返回一次
    code_hint TEXT NOT NULL DEFAULT '',   -- 尾 4 位，供管理员核对
    name TEXT NOT NULL DEFAULT '',
    amount_usd NUMERIC(16,6) NOT NULL,
    status TEXT NOT NULL DEFAULT 'enabled',  -- enabled | used | disabled
    created_by TEXT NOT NULL DEFAULT '',
    used_by TEXT,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ
);
```

`billing_ledger` 不单独建表——`billing_order` 本身即流水，兑换码兑换也写一条 `channel='redemption'` 的成功订单，前端「充值记录」直接查它，口径唯一。

兑换码只存哈希，与现有 `key_vault.py` / `auth_verification_codes` 的做法一致，避免管理员库泄露即等于资金泄露。

## 落账核心（唯一入口）

`billing_store.settle_order(trade_no, ...)` 在单个事务内：

1. `SELECT ... FOR UPDATE` 锁订单行
2. CAS：`UPDATE billing_order SET status='success' WHERE trade_no=$1 AND status='pending'`，`rowcount==0` 即判定重复回调，直接返回「已处理」而非报错
3. `INSERT ... ON CONFLICT DO UPDATE` 累加 `billing_account.balance_usd` 与 `topup_total_usd`
4. 返回新余额，供调用方同步上游

所有渠道（兑换码 / 易支付回调 / 管理员补单）都走这一个函数，幂等只实现一次。

## 上游同步

新增 `backend/litellm_client.py::set_user_budget(user_id, max_budget, backend=None)`，调 `/user/update` 写 `max_budget`（已核对 `internal_user_endpoints.py:1337 user_update`，字段确认存在）。

充值成功后按顺序：

1. 写 `max_budget = topup_total_usd`（累计已充值，非当前余额——上游 `spend` 是累加的，两者相减才是可用余额，这与现有 spend 语义一致）
2. 抬高该用户所有 key 的日限额至 `min(topup_total_usd, KEY_DAILY_BUDGET_CAP)`，默认 cap 仍为 100，可配
3. 首次充值时若 `models` 仍为 `no-default-models`，授予 `TOPUP_DEFAULT_MODELS` 配置的模型集，`entitlementStatus` 随之变 active，并清 `local_entitlement_cache`

上游写入失败不回滚本地账本（钱已收到），改为记录告警 + 落一个待重试标记，由现有 `auth_provisioning_jobs` 同款的轻量重试路径补偿。这一点在健康检查里暴露计数。

## 后端接口

用户侧（均需 `enforce_csrf`，走 `current_local_auth_user`）：

- `GET /api/me/billing` — 余额、累计充值、汇率、最低充值额、可用渠道、订单分页
- `POST /api/me/billing/redeem` — 兑换码兑换
- `POST /api/me/billing/orders` — 易支付下单，返回 `{url, params}` 供前端 POST 表单跳转
- `GET /api/me/billing/orders/{trade_no}` — 轮询单个订单状态（返回后页面用它确认到账）

回调（免登录，白名单在 CSRF 与 auth 中间件之外）：

- `POST|GET /api/pay/epay/notify` — 三道校验：MD5 验签 → 订单存在且 `pending` → `money` 与下单快照一致。校验通过才结算，响应体固定纯文本 `success`。

管理侧（`isAdmin` 门禁）：

- `GET/POST /api/admin/billing/redemptions` — 列表 / 批量生成（明文仅此一次返回）
- `POST /api/admin/billing/redemptions/{id}/disable`
- `GET /api/admin/billing/orders` — 全站订单，支持按订单号搜索
- `POST /api/admin/billing/orders/{trade_no}/complete` — 手动补单，用于回调丢失

## 前端

`index.html` 侧边栏「用量导航」新增一项「充值中心」（`data-view="billing"`），沿用现有 `switchView` 机制；`assets/app.js` 加 `loadBillingData` / `renderBilling`。

充值页布局（对齐参考站）：

- 顶部三张统计卡：当前余额 / 累计充值 / 本月消耗（消耗复用现有 usage 数据）
- 兑换码卡片：单输入框 + 兑换按钮
- 在线充值卡片：金额档位快选（10/50/100/500 美元）+ 自定义输入，实时显示「应付 ¥X」，支付宝/微信二选一。未配置易支付时整卡隐藏
- 充值记录表格：时间、订单号、渠道、到账额度、应付金额、状态

管理员在「全员看板」下新增兑换码与订单两个面板。

文案严格遵守产品边界：不出现 LiteLLM / Proxy / Virtual Key / max_budget 等上游术语，统一用「额度」「余额」「令牌」。

`index.html` 末尾的 `app.js?v=` 版本号必须同步更新，否则用户拿到旧缓存。

## 配置项（`.env.example`）

```
BILLING_ENABLED=false
BILLING_EXCHANGE_RATE=7.3           # 人民币 : 1 美元额度
BILLING_MIN_TOPUP_USD=1
BILLING_KEY_DAILY_BUDGET_CAP=100    # 充值后 key 日限额上限
TOPUP_DEFAULT_MODELS=               # 首次充值自动授予的模型，逗号分隔
EPAY_ENABLED=false
EPAY_GATEWAY_URL=                   # 易支付站点根地址，不含 /submit.php
EPAY_PARTNER_ID=
EPAY_KEY=
EPAY_NOTIFY_BASE_URL=               # 公网可达的本站地址
```

`BILLING_ENABLED=false` 时所有新接口返回 404、前端导航项不出现，保证未配置环境行为不变。

## 测试

新增 `tests/test_billing_store.py`、`tests/test_billing_routes.py`、`tests/test_epay_notify.py`，全部 mock 上游：

- 兑换码：正常兑换、重复兑换、已禁用、已过期、并发兑换只成功一次
- 落账幂等：同一订单重复结算余额只加一次
- 易支付验签：正确签名放行、错误签名拒绝、金额被篡改拒绝、重复回调返回 success 但不重复加钱
- 汇率换算与最低充值校验
- 首次充值触发模型授权、非首次不重复授权
- 上游写入失败时本地账本不回滚且落告警
- `BILLING_ENABLED=false` 时接口 404

## 实施顺序

1. `billing_store.py` + schema + 落账事务 + 单测
2. `set_user_budget` 与充值后同步（含模型授权、key 日限额）+ 单测
3. 用户侧接口 + 兑换码全链路 + 单测
4. 易支付下单与回调 + 验签单测
5. 管理侧接口 + 单测
6. 前端充值中心页 + 管理面板 + 版本号
7. 全量 pytest、`127.0.0.1:8000` 手工验证（健康检查、注册→兑换→余额→模型可用）
8. review `git diff`（排除用户并发改动）、提交、推送、同步生产

## 风险与边界

- 易支付未配置时不可能联调真实支付，第 4 步只能做到验签与状态机的单测覆盖 + 本地伪造回调验证。真实通道需你提供商户信息后再验。
- 上游 `max_budget` 语义是「累计上限」，与本地「已充值总额」对齐；若历史上有人手工改过上游 `max_budget`，首次充值会被我们覆盖成 `topup_total_usd`。会在实施时先查一遍现网是否存在手工设过 `max_budget` 的本地注册账号，有则单独报告，不静默覆盖。
- 本期不做退款、不做分组倍率折扣、不做签到赠额，保持范围收敛。
