# AI 用量中心

AI 用量中心是一个面向公司员工的 AI 工具用量查询系统。项目由 FastAPI 后端和静态前端组成，后端负责登录、权限校验、真实用量聚合与模型列表查询，前端负责中文看板、图表和模型广场展示。

系统入口默认由后端统一提供，避免前后端跨域配置：

```powershell
http://127.0.0.1:8010
```

## 当前能力

- 飞书扫码登录：复用公司 Casdoor + 飞书 SSO 登录链路。
- 我的用量：员工登录后只查看自己的 Token、金额、请求次数、成功率、来源拆分和模型排行。
- 模型广场：展示当前员工可用模型，支持搜索、筛选和复制模型名称。
- 管理员全员看板：管理员邮箱白名单登录后可查看全员聚合用量和员工排行。
- 个人看板缓存：员工映射缓存 30 分钟，个人用量结果缓存 5 分钟，提升重复加载速度。

## 目录结构

```text
D:\ai-token-dashboard
├── backend\
│   ├── auth.py              # 登录用户、管理员权限和会话处理
│   ├── cache.py             # 轻量内存 TTL 缓存
│   ├── litellm_client.py    # 上游网关接口封装与数据聚合
│   └── main.py              # FastAPI 路由入口
├── assets\
│   └── app.js               # 前端交互、图表和页面渲染
├── index.html               # 静态前端页面
├── requirements.txt         # Python 依赖
├── .env.example             # 环境变量模板
└── README.md
```

## 本地启动

在 PowerShell 中执行：

```powershell
cd D:\ai-token-dashboard
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

编辑 `D:\ai-token-dashboard\.env`，填入后端真实配置。不要把 `.env` 提交到 Git。

启动服务：

```powershell
cd D:\ai-token-dashboard
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8010 --reload
```

浏览器打开：

```text
http://127.0.0.1:8010
```

## Docker 启动

Docker 镜像内部使用 `8000` 端口，建议本地映射到 `8010`，这样可以继续复用当前 Casdoor 本地回调地址 `http://127.0.0.1:8010/api/auth/callback`。

构建镜像：

```powershell
cd D:\ai-token-dashboard
docker build -t ai-token-dashboard .
```

单容器运行：

```powershell
docker run --rm --env-file .env -p 8010:8000 ai-token-dashboard
```

使用 Compose 运行：

```powershell
docker compose up -d --build
```

访问地址：

```text
http://127.0.0.1:8010
```

注意：`.dockerignore` 已排除 `.env`、`.venv`、`.git`、缓存和日志，避免真实密钥或本地环境进入镜像构建上下文。

## 环境变量

`.env.example` 只放模板值，真实密钥只写入本地或部署环境的 `.env` / Secret。

```env
LITELLM_BASE_URL=https://cc.auto-link.com.cn/pro
LITELLM_ADMIN_KEY=<backend-admin-key>

APP_BASE_URL=http://127.0.0.1:8010
SESSION_SECRET=<random-long-session-secret>

OIDC_ISSUER_URL=http://10.68.13.198:30882
OIDC_CLIENT_ID=<casdoor-application-client-id>
OIDC_CLIENT_SECRET=<casdoor-application-client-secret>
OIDC_CASDOOR_APPLICATION_ID=admin/ai-token-dashboard
OIDC_DIRECT_PROVIDER=lark-provider
OIDC_DIRECT_METHOD=signup
OIDC_SKIP_CASDOOR_PAGE=true
OIDC_PROVIDER_LOGIN_HOST=accounts.feishu.cn
OAUTH_PROVIDER_NAME=飞书扫码登录
ALLOWED_EMAIL_DOMAIN=auto-link.com.cn

ADMIN_EMAILS=admin1@auto-link.com.cn,admin2@auto-link.com.cn

DEV_LOGIN_ENABLED=false
DEBUG_MAPPING_ENABLED=false
DEBUG_OIDC_CLAIMS=false
USAGE_LOG_MAX_PAGES=20
USER_MAPPING_CACHE_TTL_SECONDS=1800
PERSONAL_USAGE_CACHE_TTL_SECONDS=300
ADMIN_USAGE_LOG_MAX_PAGES=30
ADMIN_USAGE_PAGE_SIZE=100
```

说明：

- `LITELLM_ADMIN_KEY` 只允许后端读取，前端永远不能保存或展示。
- `APP_BASE_URL` 本地开发可用 `http://127.0.0.1:8010`，生产环境改为正式域名。
- `SESSION_SECRET` 必须使用随机长字符串，生产环境不要使用示例值。
- `ADMIN_EMAILS` 是管理员白名单，普通员工不会看到全员看板入口。
- `ADMIN_USAGE_PAGE_SIZE` 必须小于等于 100；上游接口单页最大值就是 100。想扩大覆盖范围时增加 `ADMIN_USAGE_LOG_MAX_PAGES`，不要增大单页大小。

## 飞书扫码登录配置

系统使用 Casdoor 作为 OIDC 中枢，飞书作为登录 Provider。

Casdoor 侧建议配置：

- Organization：`cltx`
- Application：`ai-token-dashboard`
- Provider：`lark-provider`
- Redirect URI：
  - 本地：`http://127.0.0.1:8010/api/auth/callback`
  - 生产：`https://ai-usage.auto-link.com.cn/api/auth/callback`

后端登录入口：

```text
GET /api/auth/sso/start
```

如果 `OIDC_SKIP_CASDOOR_PAGE=true`，后端会尝试提取飞书真实登录地址并直接跳转到飞书扫码页。提取失败时会回退到标准 Casdoor 授权页，保证登录链路不中断。

## 接口概览

认证接口：

- `GET /api/auth/config`：返回登录按钮名称和开发登录开关。
- `GET /api/auth/me`：返回当前登录员工信息和 `isAdmin`。
- `GET /api/auth/sso/start`：发起飞书扫码登录。
- `GET /api/auth/callback`：OIDC 登录回调。
- `POST /api/auth/logout`：退出登录。

员工接口：

- `GET /api/me/usage`：返回我的用量汇总、趋势、来源拆分、模型排行和明细。
- `GET /api/me/usage/logs`：返回我的用量明细分页。
- `GET /api/models`：返回当前员工可用模型列表。

管理员接口：

- `GET /api/admin/usage`：返回全员或指定员工聚合用量。
- `GET /api/admin/users`：返回员工用量排行。

调试接口：

- `GET /api/debug/me-mapping`：开发环境查看当前邮箱匹配到的上游账号。
- `GET /api/debug/me-usage-compare`：开发环境对比不同上游口径的个人用量聚合。

调试接口只有在 `DEBUG_MAPPING_ENABLED=true` 时可用，生产环境应保持关闭。

## 数据口径

我的用量以当前登录员工邮箱为主身份：

- 优先匹配上游用户列表中的 `user_email`。
- 兼容旧账号命名，例如 `cursor-邮箱前缀`、`claude-code-邮箱前缀`、`邮箱前缀`。
- 员工不能通过前端传入任意 `user_id` 查询他人数据。

前端展示口径：

- 最近一天：当前筛选结果中最新日期的整日汇总，不是最新一条明细。
- 所选日期范围：按当前日期范围和来源筛选累计。
- 金额：使用后端返回的 `spend` 汇总，展示为预估美元金额。
- 请求成功率：成功请求数除以请求总数。
- 来源拆分：当前区分 Cursor 和 Claude Code。

缓存口径：

- 用户映射缓存默认 1800 秒。
- 个人用量缓存默认 300 秒。
- 缓存 key 按员工邮箱、开始日期、结束日期和来源隔离。
- 数据允许最多 5 分钟延迟；点击刷新或缓存过期后会重新查询上游。

## 管理员全员看板

管理员身份由后端 `.env` 的 `ADMIN_EMAILS` 决定。登录邮箱命中白名单后，`/api/auth/me` 会返回：

```json
{
  "isAdmin": true
}
```

管理员可以查看：

- 全员 Token、金额、请求次数、成功率。
- 活跃员工数。
- Cursor / Claude Code 来源拆分。
- 每日 Token 趋势和每日金额消费趋势。
- 员工排行和员工详情。

管理员看板不展示访问密钥明文，不返回 prompt 或 response 内容。

## 安全规则

- 管理员 key、OIDC client secret、session secret 只保存在后端环境变量或部署 Secret 中。
- 前端不保存管理员 key、OIDC token 或访问密钥明文。
- 普通员工只能访问 `/api/me/*` 下自己的数据。
- 管理员全员数据只能通过 `/api/admin/*` 获取，且必须命中 `ADMIN_EMAILS`。
- 不在日志中打印管理员 key、OIDC token、访问密钥明文、prompt 或 response 内容。
- `.env` 已加入忽略规则，不应提交到远端仓库。

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

### 全员看板报 page_size 超过限制

上游接口单页最大值为 100。确认：

```env
ADMIN_USAGE_PAGE_SIZE=100
```

修改后需要重启后端。

### 我的用量加载慢

首次加载需要查询上游并聚合数据，后续同一日期范围和来源会命中 5 分钟个人用量缓存。可以检查 `/api/me/usage` 响应中的 `cache.hit` 判断是否命中缓存。

### 我的用量和公司原系统不一致

先确认日期范围、来源筛选和员工账号映射是否一致。开发环境可临时开启：

```env
DEBUG_MAPPING_ENABLED=true
```

然后访问：

```text
/api/debug/me-mapping
/api/debug/me-usage-compare
```

排查完成后应关闭调试开关并重启服务。
