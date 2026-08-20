# 企业令牌：可选模型改为上游真实目录

## 目标与范围

把「新增令牌」里可勾选的模型从硬编码常量换成上游真实模型目录。按你确认的范围：

- **只接目录，不建上游账号**：不调 `/user/new`、不调 `/team/new`、不发真实 key。
- **令牌本身仍是 Mock**：`secrets.token_hex` 生成的明文、掩码存储、每家 3 条种子数据全部保留。
- **先本地验证**：不改生产 `.env`，本地起服务确认后再决定是否推生产。

### 明确不做（以及为什么）

「按 team 权限过滤」现在没有可过滤的依据：不建上游主体，每家企业在上游就没有对应
team，`key_model_scope()` 需要一个真实 `user_id`/`team_id` 才能解析
`all-proxy-models` / `no-default-models`。所以本次取**网关全量目录**，并把过滤点收敛成
一个独立函数 `organization_token_model_catalog()`，将来接了 team 只改这一个地方。这一
点与你选的「按 team 权限过滤」有出入，是因为它依赖你同时否掉的上游主体，先按可实现
的部分交付。

## 关键取舍

### 1. 目录从哪取：`_proxy_model_names()`

复用 `LiteLLMClient._proxy_model_names(backend)`（[litellm_client.py:1342](backend/litellm_client.py#L1342)）——
读 `GET /models`，返回排序后的纯模型名列表。比 `models()` 轻：不需要定价、上下文窗口、
用量热度，令牌勾选框只要名字。

多 backend（primary + Her）取并集去重。`source` 非空的 backend（Her）在
`create_key` 里被显式拒绝，但这里只是列目录、不发 key，仍并入以保持目录完整。

### 2. 原始名存储、脱敏名展示

`is_internal_model_alias()` 的注释写明：内部别名是员工**唯一能调用**的模型名，只从展示
名剥离，不得据此丢弃模型（[litellm_client.py:382-384](backend/litellm_client.py#L382-L384)）。
所以：

- 令牌里存 **原始上游名**（将来真发 key 时正是要传给上游的值）。
- 前端勾选框显示 `model_display_name()` 脱敏后的名字，满足产品边界（不得出现
  `wangsu` / `zerokey` 这类网关代号）。
- `availableModels` 从字符串数组升级为 `[{name, displayName}]`；同时保留纯字符串数组
  兼容旧前端 bundle（浏览器可能缓存着旧 `app.js`）。

### 3. Store 不碰上游，目录由路由层注入

`organization_store.py` 的契约是纯内存、无 HTTP、无鉴权，17 个后端测试锁着这条。所以
不在 store 里发请求，而是：

- `list_tokens()` / `create_token()` 新增可选参数 `available_models: tuple[str, ...] | None`；
  为 `None` 时回落到现有常量 `ORGANIZATION_TOKEN_MODELS`（保持 store 单测可离线跑）。
- `_validate_token_models()` 改为接收目录参数，而不是读全局常量——否则前端能勾选、
  后端会拒。
- 路由层取目录后传进去。

### 4. 上游不可用必须降级，不能让页面打不开

`client()` 在未配置 `LITELLM_BASE_URL` 时抛 500（[main.py:345](backend/main.py#L345)），而企业
演示经常跑在没有上游凭据的环境（现有 607 个测试就是）。所以新增
`organization_token_model_catalog()`：

- 成功 → 真实目录。
- `client()` 抛错 / 上游超时 / 目录为空 → 回落到 `ORGANIZATION_TOKEN_MODELS`，记一条
  `logger.warning`，页面照常可用。
- 目录结果按 `MODEL_CACHE_TTL_SECONDS` 复用 `_model_cache`（`_proxy_model_names` 目前
  不缓存，这里加一层，避免每次开弹窗都打上游）。

### 5. 已存在令牌引用了目录外的模型

真实目录会变（上游下线某模型），种子数据里的 `gpt-5.2` 等也可能不在真实目录里。已签发
令牌的 `models` 是历史事实，**不做校验、不改写、照原样展示**；只有**创建**时校验必须落
在当前目录内。列表接口因此不能拿目录去过滤 `items`。

## 改动清单

**backend/litellm_client.py**
- `_proxy_model_names()` 加 TTL 缓存（键 `proxy_model_names:{backend.id}`）。
- 新增 `organization_token_models()`：遍历所有 backend 取并集、去空、去重、排序，
  单个 backend 失败不影响其余（沿用 `models()` 里 `except HTTPException: continue` 的写法）。

**backend/main.py**
- 新增 `async def organization_token_model_catalog() -> tuple[str, ...]`：调上游、失败降级到
  常量、返回原始名元组。
- `GET /api/organization/current/tokens`、`POST /api/organization/current/tokens`、
  `GET /api/platform/organizations/{id}/tokens` 三处传入目录。
- `OrganizationTokenCreateRequest.models` 的 `max_length` 现在绑死
  `MAX_MODELS_PER_TOKEN = len(ORGANIZATION_TOKEN_MODELS)`（5），真实目录可能更多 →
  改为一个独立上限常量 `MAX_MODELS_PER_TOKEN = 50`，避免真实目录被 Pydantic 提前截断。

**backend/organization_store.py**
- `_validate_token_models(value, catalog)` 接收目录。
- `list_tokens()` / `create_token()` 接收 `available_models`，`availableModels` 按目录输出。
- `available_token_models()` 保留（离线默认值来源）。

**assets/app.js**
- `organizationTokenModels` 支持 `{name, displayName}` 与纯字符串两种载荷。
- `renderOrganizationTokenModelChoices()`：`value` 用原始名，可见文本用 `displayName`。
- 表格里已签发令牌的模型标签同样显示 `displayName`，`title` 属性给原始名便于排查。
- 目录为空时给出明确文案，而不是空白弹窗。

**index.html**
- `app.js?v=` 升到 `20260731-organization-token-real-models`。

**tests/**
- 重写 `test_customer_admin_can_list_create_and_revoke_without_upstream_calls`：上游只用于
  取目录，写操作仍不落上游——断言 `/key/generate`、`/user/new`、`/team/new`、`/key/delete`
  一次都没被调用（用一个记录 `request_backend` 调用的假客户端）。
- 新增：目录来自上游真实名（stub `_proxy_model_names` 返回含内部别名的列表，断言
  `availableModels` 用原始名、`displayName` 已脱敏）。
- 新增：上游不可用时降级到内置常量且接口仍 200。
- 新增：创建时勾选目录外的模型被拒 400。
- 新增：已签发令牌引用目录外模型时列表照常返回、不被过滤。
- 新增前端契约：勾选框 `value` 是原始名、显示是脱敏名。
- 同步两处 `app.js?v=` 断言（`test_frontend_billing.py`、`test_static_cache.py`）。

**README.md**
- 「固定演示目录 `claude-opus-5`…」改为：可选模型来自网关真实目录，上游不可用时回落内置
  列表；并写明令牌本身仍是演示数据、不会真的调用网关。

## 验证

1. `python -m pytest tests/` 全绿。
2. 重启本地 `127.0.0.1:8000`（`ORGANIZATION_DEMO_ENABLED=true DEV_LOGIN_ENABLED=true`），
   用 `owner@demo.example` 打开令牌管理，确认「新增令牌」里的模型来自真实网关、名字已脱敏。
3. 临时清掉 `LITELLM_BASE_URL` 起一次，确认降级路径下页面仍可用（不改 `.env`，用环境变量覆盖）。
4. `git diff` review → commit → push；生产同步等你确认本地效果后再决定。
