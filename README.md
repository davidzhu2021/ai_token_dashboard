# AI 用量中心

AI 用量中心是面向公司员工的 AI 工具用量查询系统。项目由 FastAPI 后端和静态前端组成：后端负责企业登录、会话、权限校验、上游用量聚合、访问密钥管理和模型列表查询；前端负责中文看板、图表、团队/部门视图和模型广场。

系统入口由后端统一提供，避免额外的前后端跨域配置：

```text
http://127.0.0.1:8000
```

生产访问地址：

```text
https://myai.carher.net
```

## 当前能力

- 飞书扫码登录：复用公司 Casdoor + 飞书 SSO 登录链路，并按企业邮箱域名限制访问。
- 我的用量：员工登录后只能查看自己的 Token、金额、请求次数、成功率、来源拆分、模型排行和明细。
- 个人访问密钥：展示本人访问密钥的名称、用途、掩码、状态、最近使用时间和用量，并支持本人密钥再生成。
- 团队负责人看板：团队负责人可查看自己负责团队的成员用量、趋势、来源占比、模型排行和成员排行。
- 管理员看板：管理员邮箱白名单用户可查看全员用量、员工排行、部门看板和指定员工/部门详情。
- 模型广场：展示当前账号可用模型，支持搜索、筛选和复制模型名称。
- 多数据源聚合：主数据源用于 AI 用量中心，可选 Her 数据源用于补充 Her 聊天机器人相关用量。
- 缓存加速：员工映射、个人用量、团队权限、团队用量、管理员用量、部门用量、密钥列表和模型列表均使用轻量 TTL 缓存。

LiteLLM 是本系统的内部后端集成。员工前端文案应使用 AI 用量中心、模型、来源、Token、Codex、Claude Code、Her、访问权限等产品术语，不暴露上游网关、管理员密钥或供应商实现细节。

## 目录结构

```text
D:\ai-token-dashboard
├── backend\
│   ├── auth.py              # 登录用户、管理员权限、企业邮箱和会话处理
│   ├── cache.py             # 轻量内存 TTL 缓存
│   ├── litellm_client.py    # 上游 API 封装、账号映射、用量聚合和密钥操作
│   └── main.py              # FastAPI 路由入口和静态文件挂载
├── assets\
│   └── app.js               # 前端状态、API 调用、图表渲染和页面交互
├── tests\                   # pytest 后端测试
├── index.html               # 单页 dashboard shell
├── requirements.txt         # Python 依赖
├── docker-compose.yml       # 本地/生产 Compose 服务
├── Dockerfile               # 容器镜像构建
├── .env.example             # 环境变量模板
└── README.md
```

## 本地启动

在 Windows PowerShell 中执行：

```powershell
cd D:\ai-token-dashboard
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

编辑 `D:\ai-token-dashboard\.env`，填入真实配置。不要把 `.env` 提交到 Git。

启动服务：

```powershell
cd D:\ai-token-dashboard
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

浏览器打开：

```text
http://127.0.0.1:8000
```

健康检查：

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/health'
```

## Docker 启动

Docker 镜像内部使用 `8000` 端口，本地建议映射到 `8000`，便于复用 Casdoor 本地回调地址 `http://127.0.0.1:8000/api/auth/callback`。

构建镜像：

```powershell
cd D:\ai-token-dashboard
docker build -t ai-token-dashboard .
```

单容器运行：

```powershell
docker run --rm --env-file .env -p 8000:8000 ai-token-dashboard
```

使用 Compose 运行：

```powershell
docker compose up -d --build
```

访问地址：

```text
http://127.0.0.1:8000
```

`.dockerignore` 已排除 `.env`、`.venv`、`.git`、缓存和日志，避免真实密钥或本地环境进入镜像构建上下文。

## 环境变量

`.env.example` 只放模板值，真实密钥只写入本地或部署环境的 `.env` / Secret。

```env
LITELLM_BASE_URL=https://cc.auto-link.com.cn/pro
LITELLM_ADMIN_KEY=<backend-admin-key>

# Optional: Her chatbot usage source. Leave empty to disable.
HER_LITELLM_BASE_URL=https://litellm.carher.net
HER_LITELLM_ADMIN_KEY=<her-backend-admin-key>
HER_SOURCE_LABEL=Her
HER_ACCOUNT_INDEX_CACHE_TTL_SECONDS=1800
HER_KEY_LIST_MAX_PAGES=20

APP_BASE_URL=https://myai.carher.net
SESSION_SECRET=replace-with-a-random-long-string

OIDC_ISSUER_URL=http://10.68.13.198:30882
OIDC_CLIENT_ID=ai-token-dashboard-client-id
OIDC_CLIENT_SECRET=ai-token-dashboard-client-secret
OIDC_CASDOOR_APPLICATION_ID=admin/ai-token-dashboard
OIDC_APPLICATION_NAME=ai-token-dashboard
OIDC_DIRECT_PROVIDER=lark-provider
OIDC_DIRECT_METHOD=signup
OIDC_SKIP_CASDOOR_PAGE=true
OIDC_PROVIDER_LOGIN_HOST=accounts.feishu.cn
OAUTH_PROVIDER_NAME=飞书扫码登录
ALLOWED_EMAIL_DOMAIN=auto-link.com.cn
ADMIN_EMAILS=zhuyida@auto-link.com.cn,leader@auto-link.com.cn

FEISHU_DIRECT_LOGIN_ENABLED=false
FEISHU_APP_ID=cli-your-feishu-app-id
FEISHU_REDIRECT_URI=http://10.68.13.198:30882/callback

# Local development only. Keep false in production.
DEV_LOGIN_ENABLED=false
DEBUG_MAPPING_ENABLED=false
DEBUG_OIDC_CLAIMS=false
USAGE_LOG_MAX_PAGES=20
USER_MAPPING_CACHE_TTL_SECONDS=1800
PERSONAL_USAGE_CACHE_TTL_SECONDS=300
ADMIN_USAGE_CACHE_TTL_SECONDS=300
DEPARTMENT_USAGE_CACHE_TTL_SECONDS=300
TEAM_AUTH_CACHE_TTL_SECONDS=300
TEAM_USAGE_CACHE_TTL_SECONDS=300
KEY_LIST_CACHE_TTL_SECONDS=300
MODEL_CACHE_TTL_SECONDS=1800
LITELLM_MAX_CONCURRENCY=4
LITELLM_SLOW_REQUEST_MS=800
PERSONAL_USAGE_LOG_FALLBACK_ENABLED=false
ADMIN_USAGE_LOG_MAX_PAGES=30
ADMIN_USAGE_PAGE_SIZE=100
USAGE_TIMEZONE_OFFSET_MINUTES=-480
```

配置说明：

- `LITELLM_ADMIN_KEY` 和 `HER_LITELLM_ADMIN_KEY` 只允许后端读取，前端永远不能保存或展示。
- `HER_LITELLM_BASE_URL` 和 `HER_LITELLM_ADMIN_KEY` 同时存在时启用 Her 数据源；留空则不加载 Her。
- `APP_BASE_URL` 本地开发可改为 `http://127.0.0.1:8000`，生产环境使用正式域名。
- `SESSION_SECRET` 必须使用随机长字符串，生产环境不要使用示例值。
- `ADMIN_EMAILS` 是管理员白名单，普通员工不会看到全员或部门看板入口。
- `USAGE_TIMEZONE_OFFSET_MINUTES=-480` 表示按北京时间统计日期窗口；如果部署环境改用其他业务时区，需要同步调整。
- `ADMIN_USAGE_PAGE_SIZE` 必须小于等于 100；想扩大日志覆盖范围时增加 `ADMIN_USAGE_LOG_MAX_PAGES`，不要增大单页大小。

## 飞书扫码登录配置

系统使用 Casdoor 作为 OIDC 中枢，飞书作为登录 Provider。

Casdoor 侧建议配置：

- Organization：`cltx`
- Application：`ai-token-dashboard`
- Provider：`lark-provider`
- Redirect URI：
  - 本地：`http://127.0.0.1:8000/api/auth/callback`
  - 生产：`https://myai.carher.net/api/auth/callback`

如需点击“飞书扫码登录”后直达飞书页面，`OIDC_APPLICATION_NAME` 需要与 Casdoor Application 名称一致，`OIDC_DIRECT_PROVIDER` 需要与 Casdoor 中的飞书 Provider 名称一致；后端会把它们随授权请求传给 Casdoor。如果 Casdoor 已经有 HTTPS 反代地址，`OIDC_ISSUER_URL` 优先使用 HTTPS 地址。后端同时兼容 issuer base URL 和完整 discovery URL。

如果 Casdoor 当前版本仍显示中间页，可启用 `FEISHU_DIRECT_LOGIN_ENABLED=true`。此模式仍由 Casdoor 完成 OIDC 回调校验，只是把用户第一跳直接送到飞书授权页；`FEISHU_REDIRECT_URI` 必须是 Casdoor 的 `/callback` 地址，并已加入飞书开放平台的重定向 URL 白名单。

后端登录入口：

```text
GET /api/auth/sso/start
```

如果 `OIDC_SKIP_CASDOOR_PAGE=true`，后端会尝试提取飞书真实登录地址并直接跳转到飞书扫码页。提取失败时会回退到标准 Casdoor 授权页，保证登录链路不中断。

## 接口概览

基础接口：

- `GET /api/health`：健康检查。
- `GET /`：返回单页 dashboard。

认证接口：

- `GET /api/auth/config`：返回登录按钮名称、开发登录开关、OIDC 配置状态和允许邮箱域名。
- `GET /api/auth/me`：返回当前登录员工信息、管理员身份和团队负责人权限。
- `POST /api/auth/dev-login`：开发环境模拟登录，仅 `DEV_LOGIN_ENABLED=true` 时可用。
- `GET /api/auth/sso/start`：发起飞书扫码登录。
- `GET /api/auth/callback`：OIDC 登录回调。
- `POST /api/auth/logout`：退出登录。

员工接口：

- `GET /api/me/usage`：返回我的用量汇总、趋势、来源拆分、模型排行和明细。
- `GET /api/me/usage/logs`：返回我的用量明细分页。
- `GET /api/me/keys`：返回本人访问密钥列表，密钥只展示掩码。
- `POST /api/me/keys/{key_id}/regenerate`：再生成本人访问密钥，新密钥只在本次响应中返回。
- `GET /api/models`：返回当前账号可用模型列表。

团队负责人接口：

- `GET /api/team/usage`：返回当前负责人授权团队的成员用量、趋势、来源、模型和成员排行；多团队负责人可通过 `team_ref` 切换团队。

管理员接口：

- `GET /api/admin/usage`：返回全员或指定员工聚合用量。
- `GET /api/admin/users`：返回员工用量排行。
- `GET /api/admin/departments/usage`：返回全部部门或指定部门的用量、趋势、来源、模型、部门排行和员工排行。

调试接口：

- `GET /api/debug/me-mapping`：开发环境查看当前邮箱匹配到的上游账号。
- `GET /api/debug/me-usage-compare`：开发环境对比不同上游口径的个人用量聚合。
- `GET /api/debug/admin-usage-compare`：开发环境对比管理员聚合数据质量和覆盖情况。

调试接口只有在 `DEBUG_MAPPING_ENABLED=true` 时可用，生产环境应保持关闭。

## 数据口径

我的用量以当前登录员工邮箱为主身份：

- 优先匹配上游用户列表中的 `user_email`。
- 兼容旧账号命名，例如 `cursor-邮箱前缀`、`claude-code-邮箱前缀`、`邮箱前缀`。
- Her 数据源会额外按邮箱、姓名、别名、key metadata 等信息建立账号索引。
- 员工不能通过前端传入任意 `user_id` 查询他人数据。

前端展示口径：

- 最近一天：当前筛选结果中最新日期的整日汇总，不是最新一条明细。
- 所选日期范围：按当前日期范围和来源筛选累计。
- 金额：使用后端返回的 `spend` 汇总，展示为预估美元金额。
- 请求成功率：成功请求数除以请求总数。
- 来源拆分：前端展示 Codex、Claude Code、Her 与其他来源。
- 日期窗口：日志查询按 `USAGE_TIMEZONE_OFFSET_MINUTES` 换算成本地业务日期，默认北京时间。

缓存口径：

- 用户映射缓存默认 1800 秒。
- 个人用量缓存默认 300 秒。
- 管理员、部门和团队用量缓存默认 300 秒。
- 团队负责人权限缓存默认 300 秒。
- 模型列表缓存默认 1800 秒。
- 缓存 key 按用户、日期范围、来源、团队或部门隔离；点击刷新或缓存过期后会重新查询上游。

## 访问密钥

个人访问密钥入口只展示当前员工自己的密钥：

- 列表接口只返回掩码、名称、用途、状态、最近使用时间、Token 和金额，不返回完整密钥明文。
- 再生成前会校验目标密钥是否属于当前员工。
- 再生成成功后，新密钥只在本次接口响应中返回一次，并写入本地审计日志 `audit.log`。
- Her 来源密钥暂不支持在本系统更新。

## 团队负责人看板

团队负责人身份来自上游团队成员角色。后端会检查当前员工是否是团队 admin：

- 单团队负责人登录后默认进入团队看板。
- 多团队负责人可在团队选择器中切换负责团队。
- 非团队负责人不能访问 `/api/team/usage`。
- 团队成员排行包含团队内零用量成员；如果日志读取达到页数上限，页面会提示排行可能不完整。

团队看板可以查看：

- 团队 Token、金额、请求次数、成功率和活跃成员数。
- 团队每日 Token 趋势和每日金额消费趋势。
- 团队来源占比、模型排行、Prompt / Completion 拆分。
- 团队成员排行和团队角色。

## 管理员看板

管理员身份由后端 `.env` 的 `ADMIN_EMAILS` 决定。登录邮箱命中白名单后，`/api/auth/me` 会返回：

```json
{
  "isAdmin": true
}
```

管理员可以查看：

- 全员 Token、金额、请求次数、成功率。
- 活跃员工数。
- Codex / Claude Code / Her / 其他来源拆分。
- 每日 Token 趋势和每日金额消费趋势。
- 员工排行、员工搜索和员工详情。

管理员看板不展示访问密钥明文，不返回 prompt 或 response 内容。

## 管理员部门看板

部门看板仅管理员可见，接口为 `GET /api/admin/departments/usage`。

部门口径：

- 优先使用上游 Team，`team_id` 作为部门 ID，`team_alias` 作为部门名称。
- 如果日志中没有 Team 信息，则兜底读取 `metadata.department`、`metadata.department_name` 或组织字段。
- 仍无法识别时归入“未绑定部门”，方便后续补充上游数据。

部门看板可以查看：

- 每个部门的 Token、金额、请求次数、成功率和活跃员工数。
- 部门每日 Token 趋势和每日金额消费趋势。
- 部门来源占比、模型排行、Prompt / Completion 拆分。
- 部门用量排行；点击部门后查看该部门员工排行。

部门总览优先使用上游 `/team/daily/activity`，部门排行、员工排行和模型拆分来自 `/spend/logs/v2` 聚合。如果日志读取达到页数上限，页面会提示排行可能不完整。

## 上游接口参考

本项目的上游行为以本地官方项目 checkout `D:\litellm` 为准，相关端点包括：

- `/user/info`、`/user/list`
- `/team/list`、`/v2/team/list`、`/team/info`
- `/team/daily/activity`
- `/user/daily/activity/aggregated`、`/user/daily/activity`
- `/spend/logs/v2`
- `/key/list`、`/key/regenerate`
- `/model/info`

产品/API 意图参考官方文档：[LiteLLM Proxy UI](https://docs.litellm.ai/docs/proxy/ui)。

## 安全规则

- 管理员 key、Her 管理员 key、OIDC client secret、session secret 只保存在后端环境变量或部署 Secret 中。
- 前端不保存管理员 key、OIDC token 或访问密钥明文。
- 普通员工只能访问 `/api/me/*` 下自己的数据。
- 团队负责人只能访问自己负责团队的 `/api/team/usage` 数据。
- 管理员全员和部门数据只能通过 `/api/admin/*` 获取，且必须命中 `ADMIN_EMAILS`。
- 访问密钥再生成必须先校验当前员工对目标密钥的归属权。
- 不在日志中打印管理员 key、OIDC token、访问密钥明文、prompt 或 response 内容。
- `.env` 已加入忽略规则，不应提交到远端仓库。
- 生产环境必须使用 SSO，并保持 `DEV_LOGIN_ENABLED=false`、`DEBUG_MAPPING_ENABLED=false`、`DEBUG_OIDC_CLAIMS=false`。

## 测试与验证

当前仓库使用 `pytest` 编写后端测试。新增后端行为时，测试文件放在 `tests/test_*.py`，并 mock 上游 API 和 OIDC。

常用验证：

```powershell
.\.venv\Scripts\python.exe -m pytest
Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/health'
```

README-only 修改通常不需要跑完整后端测试，但提交前仍应检查 UTF-8 中文、接口路径、环境变量名和 git diff。

## 生产同步

只在 `git push origin master` 成功后同步生产服务器。标准同步命令：

```powershell
wsl bash -lc "cd /home/zhuyida/codes/carher-admin/scripts && ./jms ssh JSZX-AI-03 'cd /home/cltx/apps/ai-token-dashboard/current && git pull origin master && docker compose up -d --build && sleep 5 && curl -fsS http://127.0.0.1:8000/api/health'"
```

本地公开健康检查：

```powershell
Invoke-RestMethod -Uri 'https://myai.carher.net/api/health' -TimeoutSec 12
```

可选服务器状态检查：

```powershell
wsl bash -lc "cd /home/zhuyida/codes/carher-admin/scripts && ./jms ssh JSZX-AI-03 'cd /home/cltx/apps/ai-token-dashboard/current && docker compose ps && git log --oneline -1'"
```

不要在服务器上热修改代码；所有变更都应本地修改、提交、推送，再由服务器拉取 `master` 构建发布。

## 常见问题

### 登录按钮显示乱码

检查 `.env` 和 `.env.example`：

```env
OAUTH_PROVIDER_NAME=飞书扫码登录
```

如果 `.env` 曾被错误编码保存，建议用支持 UTF-8 的编辑器重新保存，然后重启后端。

### 点击飞书扫码登录后仍短暂看到 Casdoor 页面

确认以下配置：

```env
OIDC_DIRECT_PROVIDER=lark-provider
OIDC_DIRECT_METHOD=signup
OIDC_SKIP_CASDOOR_PAGE=true
OIDC_PROVIDER_LOGIN_HOST=accounts.feishu.cn
```

如果 Casdoor 页面结构变化，后端提取飞书链接可能失败，会自动回退到标准登录流程。

### 全员、部门或团队排行提示日志读取不完整

上游日志接口使用分页读取。先确认：

```env
ADMIN_USAGE_PAGE_SIZE=100
ADMIN_USAGE_LOG_MAX_PAGES=30
```

`ADMIN_USAGE_PAGE_SIZE` 不要超过 100；需要扩大覆盖范围时增加 `ADMIN_USAGE_LOG_MAX_PAGES`，修改后重启后端。

### 我的用量加载慢

首次加载需要查询上游并聚合数据，后续同一日期范围和来源会命中 5 分钟个人用量缓存。可以检查 `/api/me/usage` 响应中的 `cache.hit` 判断是否命中缓存。

### 我的用量和公司原系统不一致

先确认日期范围、来源筛选、时区和员工账号映射是否一致。开发环境可临时开启：

```env
DEBUG_MAPPING_ENABLED=true
```

然后访问：

```text
/api/debug/me-mapping
/api/debug/me-usage-compare
```

排查完成后应关闭调试开关并重启服务。
