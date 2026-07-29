# 通衢 API

通衢 API 是面向公司员工和获准注册用户的 AI 工具用量查询系统。项目由 FastAPI 后端和静态前端组成：后端负责企业登录、邮箱注册与登录、会话、权限校验、上游用量聚合、访问密钥管理和模型列表查询；前端负责中文看板、图表、团队/部门视图和模型广场。

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
- 邮箱注册与密码登录：可选为 `auto-link.com.cn`、`gmail.com`、`qq.com`、`163.com` 开放邮箱验证码注册、密码登录、找回/修改密码和服务端会话；默认安全关闭，不影响现有 SSO。
- 注册权限隔离：新注册账号默认没有模型访问权限，不自动创建访问密钥；启用充值中心后，到账充值可自动开通配置的模型访问范围。
- 我的用量：员工登录后只能查看自己的 Token、金额、请求次数、成功率、来源拆分、模型排行和明细。
- 个人访问密钥：展示本人访问密钥的名称、用途、掩码、状态、最近使用时间和用量，并支持本人密钥再生成。
- 团队负责人看板：团队负责人可查看自己负责团队的成员用量、趋势、来源占比、模型排行和成员排行。
- 管理员看板：管理员邮箱白名单用户可查看全员用量、员工排行、部门看板和指定员工/部门详情。
- 模型广场：展示当前账号可用模型，支持搜索、筛选和复制模型名称。
- 多数据源聚合：主数据源用于通衢 API，可选 Her 数据源用于补充 Her 聊天机器人相关用量。
- 缓存加速：员工映射、个人用量、团队权限、团队用量、管理员用量、部门用量、密钥列表和模型列表均使用轻量 TTL 缓存。
- 充值中心：支持兑换码、在线支付和个人微信/支付宝收款码转账；收款码转账必须由管理员核对到账后才发放额度。
- 企业组织（演示）：可在独立控制台维护部门和成员，默认关闭，仅展示进程内 Mock 数据。

LiteLLM 是本系统的内部后端集成。员工前端文案应使用通衢 API、模型、来源、Token、Codex、Claude Code、Her、访问权限等产品术语，不暴露上游网关、管理员密钥或供应商实现细节。

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

# Optional email/password authentication. Keep disabled until production
# dependencies and controls are configured.
AUTH_ENABLED=false
PASSWORD_LOGIN_ENABLED=false
PUBLIC_SIGNUP_ENABLED=false
EMAIL_VERIFICATION_REQUIRED=true
AUTH_ALLOWED_EMAIL_DOMAINS=auto-link.com.cn,gmail.com,qq.com,163.com
AUTH_DATABASE_PATH=.data/auth.sqlite3
AUTH_DEFAULT_UPSTREAM_ROLE=internal_user_viewer
AUTH_SESSION_TTL_SECONDS=1209600
AUTH_VERIFICATION_TTL_SECONDS=600
AUTH_PASSWORD_RESET_TTL_SECONDS=1800
AUTH_PROVISIONING_RETRY_SECONDS=30
AUTH_ENTITLEMENT_CACHE_TTL_SECONDS=30

SMTP_HOST=<transactional-smtp-host>
SMTP_PORT=587
SMTP_FROM=no-reply@notify.carher.net
SMTP_USERNAME=<smtp-username>
SMTP_PASSWORD=<smtp-password>
SMTP_SSL=false
SMTP_STARTTLS=true

TURNSTILE_ENABLED=false
TURNSTILE_SITE_KEY=
TURNSTILE_SECRET_KEY=

# Comma-separated direct reverse-proxy IPs/CIDRs; empty means XFF is ignored.
AUTH_TRUSTED_PROXY_IPS=

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
# Enterprise organization Mock V1. Keep false except for controlled demos.
ORGANIZATION_DEMO_ENABLED=false
USAGE_LOG_MAX_PAGES=20
USER_MAPPING_CACHE_TTL_SECONDS=1800
PERSONAL_USAGE_CACHE_TTL_SECONDS=300
ADMIN_USAGE_CACHE_TTL_SECONDS=300
DEPARTMENT_USAGE_CACHE_TTL_SECONDS=300
TEAM_AUTH_CACHE_TTL_SECONDS=300
TEAM_USAGE_CACHE_TTL_SECONDS=300
KEY_LIST_CACHE_TTL_SECONDS=300
MODEL_CACHE_TTL_SECONDS=1800
MODEL_USAGE_CACHE_TTL_SECONDS=300
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
- `AUTH_ENABLED`、`PASSWORD_LOGIN_ENABLED`、`PUBLIC_SIGNUP_ENABLED` 在模板中均默认关闭；三个开关应按后文的生产启用顺序逐步打开。
- `ORGANIZATION_DEMO_ENABLED=false` 默认不展示“企业组织”页。设为 `true` 时，页面只使用进程内确定性演示数据：不会创建真实账号、不会发送邀请邮件、不会调用上游服务；浏览器刷新会保留本次演示操作，服务重启或点击“重置演示数据”后恢复初始样例。生产环境应保持关闭，除非在受控演示中临时开启。
- `AUTH_ALLOWED_EMAIL_DOMAINS` 是逗号分隔的精确邮箱域名白名单；ToC 首期固定为 `auto-link.com.cn,gmail.com,qq.com,163.com`，不匹配子域名或后缀相似域名。留空代表不限制域名，生产公开注册不得留空。
- `AUTH_DATABASE_PATH` 当前指向本地 SQLite 文件。容器部署必须将其放在持久化卷中；当前实现按单应用实例设计，不要让多个副本同时写同一个 SQLite 文件。
- `AUTH_DEFAULT_UPSTREAM_ROLE=internal_user_viewer` 将新注册账号限制为只读角色；代码同时使用 `no-default-models` 禁止默认模型访问且不会自动创建访问密钥。不要将该变量设置为管理员角色。
- `SMTP_PASSWORD`、`TURNSTILE_SECRET_KEY` 与 `SESSION_SECRET` 属于部署 Secret，不得写入前端、日志或仓库。
- `AUTH_TRUSTED_PROXY_IPS` 只填写与应用直接连接的反向代理 IP 或 CIDR。为空时忽略 `X-Forwarded-For`；不得使用 `0.0.0.0/0` 或 `::/0` 这样的全网信任范围。
- `ADMIN_EMAILS` 是管理员白名单，普通员工不会看到全员或部门看板入口。
- `USAGE_TIMEZONE_OFFSET_MINUTES=-480` 表示按北京时间统计日期窗口；如果部署环境改用其他业务时区，需要同步调整。
- `ADMIN_USAGE_PAGE_SIZE` 必须小于等于 100；想扩大日志覆盖范围时增加 `ADMIN_USAGE_LOG_MAX_PAGES`，不要增大单页大小。
- `USAGE_SYNC_ENABLED=true` 时启用独立 PostgreSQL 聚合快照；数据库只保存按日期、账号、来源和模型聚合的 Token、请求、成功/失败及金额，不保存 API Key、提示词、响应正文或请求明细。
- `USAGE_DATABASE_URL` 应填写应用容器可访问的 PostgreSQL 地址，例如 `postgresql://ai_dashboard:<password>@usage-db:5432/ai_usage`；`USAGE_DB_PASSWORD` 只用于 Compose 初始化数据库用户密码。
- 首次启动会异步回填 `USAGE_INITIAL_BACKFILL_DAYS`（默认 90）天，之后按 `USAGE_SYNC_INTERVAL_SECONDS`（默认 1800 秒）刷新最近 `USAGE_SYNC_LOOKBACK_DAYS`（默认 3）天。历史范围优先查询快照库，当前日期超过 `USAGE_LIVE_REFRESH_MAX_AGE_SECONDS` 后后台刷新，手动点击刷新会强制刷新。

## 邮箱注册与密码登录配置

本地认证与现有 Casdoor + 飞书 SSO 可以同时启用。`AUTH_ENABLED` 只控制本地邮箱认证总开关，不会关闭 SSO；OIDC 参数保持有效时，用户仍可继续使用飞书扫码登录。`DEV_LOGIN_ENABLED` 是另一条独立的开发模拟登录链路，生产环境必须始终保持 `false`。

开关含义：

- `AUTH_ENABLED=false`：关闭本地邮箱认证总入口；这是新部署和回滚时的安全状态。
- `PASSWORD_LOGIN_ENABLED=false`：关闭密码登录、找回密码等密码入口；只有同时设置 `AUTH_ENABLED=true` 才会生效。HTTPS 部署开启后必须同时启用并完整配置 Turnstile，否则应用会拒绝启动。
- `PUBLIC_SIGNUP_ENABLED=false`：关闭公开注册；建议最后启用，并可在紧急情况下单独关闭注册而保留已有账号登录。
- `EMAIL_VERIFICATION_REQUIRED=true`：注册必须验证邮箱。生产环境应保持 `true`，不要通过关闭验证来绕过 SMTP 配置。

持久化与有效期：

- `AUTH_DATABASE_PATH=.data/auth.sqlite3`：保存本地用户、密码哈希、服务端会话、验证码、重置令牌、限流和开户状态。Docker Compose 已持久化 `/app/.data`；非 Compose 部署需要提供等效持久化和备份。
- `AUTH_SESSION_TTL_SECONDS=1209600`：本地登录会话有效期，默认 14 天，代码最小值为 300 秒。
- `AUTH_VERIFICATION_TTL_SECONDS=600`：邮箱验证码有效期，默认 10 分钟，代码最小值为 60 秒。
- `AUTH_PASSWORD_RESET_TTL_SECONDS=1800`：密码重置链接有效期，默认 30 分钟，代码最小值为 300 秒。
- `AUTH_PROVISIONING_RETRY_SECONDS=30`：本地账号向用量系统开户失败后的最短重试间隔，默认 30 秒，代码最小值为 5 秒；它是重试节流时间，不是账号或任务的过期时间。
- `AUTH_ENTITLEMENT_CACHE_TTL_SECONDS=30`：登录会话读取模型权限状态的短缓存，默认 30 秒；用户点击“重新检查”时会主动刷新。

SMTP 配置：

- `SMTP_HOST`、`SMTP_PORT`、`SMTP_FROM` 指定邮件服务器、端口和发件人；生产使用专用事务邮件服务，默认发件人示例为 `no-reply@notify.carher.net`，不使用个人 QQ、163 或 Gmail 邮箱账号。注册验证和密码找回都依赖可用 SMTP。
- `SMTP_USERNAME`、`SMTP_PASSWORD` 用于 SMTP 鉴权。账号或密码为空只适用于明确允许匿名投递的内网邮件服务。
- `SMTP_SSL=true` 表示连接建立时直接使用 TLS，通常配合 465 端口；`SMTP_STARTTLS=true` 表示普通连接后升级 TLS，通常配合 587 端口。不要同时启用两种模式，也不要在生产环境关闭传输加密。
- 上线前必须按事务邮件服务商要求为 `notify.carher.net` 添加并验证 SPF/DKIM 记录；SMTP 账号和 DNS 配置属于部署外部依赖，仓库不能生成这些凭据。

Turnstile 配置：

- 面向互联网开放密码登录或注册时，必须配置 Cloudflare Turnstile，以保护登录、验证码、注册和找回密码入口。后端只有在邮箱验证、域名白名单、SMTP 和 Turnstile 全部就绪时才会真正开放注册接口。
- 在 Cloudflare 控制台为正式域名创建站点，分别填入 `TURNSTILE_SITE_KEY` 和 `TURNSTILE_SECRET_KEY`，确认前后端验证成功后再设置 `TURNSTILE_ENABLED=true`。
- `TURNSTILE_SECRET_KEY` 只能保存在后端 Secret 中。若 `TURNSTILE_ENABLED=true` 但任一 key 缺失，健康检查会标记为 degraded，相关认证请求会返回配置错误。
- Turnstile 站点必须限制为 `myai.carher.net`。Site Key 可以由配置接口提供给浏览器，Secret Key 只能保存在生产服务器；仓库不能代替 Cloudflare 创建有效 Key。

注册后的权限策略：

- 注册成功会创建本地账号并尝试创建受限的用量系统账号；开户请求不自动创建访问密钥，模型范围使用 `no-default-models`，默认角色为 `internal_user_viewer`。
- 即使开户成功，新用户的 `entitlementStatus` 仍为 `inactive`，在管理员开通权限前不能查询个人用量、创建访问密钥或调用模型。
- 启用充值中心后，已结算订单会把累计充值额同步为用户级消费上限；当前可用额度按累计充值额减去实际累计消耗计算。首次充值可通过 `TOPUP_DEFAULT_MODELS` 自动授予模型范围。
- 个人收款码没有可信的支付回调。扫码转账后，用户提交付款说明，管理员必须在收款流水中核对后再确认到账；不要把“已提交”当作“已到账”。
- 飞书 SSO 与本地密码账号并存，但不会因为邮箱相同而自动合并身份。

## 充值中心配置

充值账本使用 `USAGE_DATABASE_URL` 指向的 PostgreSQL，与用量快照共用数据库但使用独立表。默认关闭；只有数据库已连接且 `BILLING_ENABLED=true` 时，注册用户才能看到“充值中心”。

```dotenv
BILLING_ENABLED=true
BILLING_EXCHANGE_RATE=7.3
BILLING_MIN_TOPUP_USD=1
BILLING_MAX_TOPUP_USD=10000
BILLING_TOPUP_OPTIONS=10,50,100,500
BILLING_KEY_DAILY_BUDGET_CAP=100
BILLING_REDEMPTION_SECRET=<随机长字符串>
TOPUP_DEFAULT_MODELS=<首次充值后开放的模型，逗号分隔>

# 个人收款码试运行：仅人工核对到账，不会自动入账。
MANUAL_PAY_ENABLED=true
MANUAL_PAY_ALIPAY_QR=/assets/pay/alipay.png
MANUAL_PAY_WXPAY_QR=/assets/pay/wechat.png
MANUAL_PAY_NOTICE=请按订单金额扫码付款，并在付款备注里填写订单号，便于快速核对。
MANUAL_PAY_REVIEW_MINUTES=30
MANUAL_PAY_CONTACT=<收款咨询联系方式>
```

收款码文件不要提交到 Git：将支付宝、微信收款码分别上传到部署机的 `assets/pay/alipay.png`、`assets/pay/wechat.png`，再重建服务。管理员使用企业 SSO 管理员账号进入“全员看板”，在“待确认到账”中核对收款流水并确认或驳回。订单确认是不可逆的发放动作，必须核对订单号、应付金额、付款方式和付款说明。

如需自动到账，可配置兼容易支付协议的商户网关：`EPAY_ENABLED=true`、`EPAY_GATEWAY_URL`、`EPAY_PARTNER_ID`、`EPAY_KEY` 与公网可访问的 `EPAY_NOTIFY_BASE_URL`。异步回调会校验签名、订单状态和金额；不要将个人收款码伪装成自动支付渠道。

反向代理配置：

- `AUTH_TRUSTED_PROXY_IPS` 接受逗号分隔的单个 IP 或 CIDR，例如 `127.0.0.1,172.20.0.0/16`。仅当请求的直接来源命中此列表时，后端才会使用 `X-Forwarded-For` 计算真实客户端 IP。
- 只信任实际反向代理所在地址，并确保代理覆盖而不是透传外部传入的客户端 IP 头。直连部署保持空值即可。
- `APP_BASE_URL` 必须填写用户实际访问的 HTTPS 地址；它同时参与安全 Cookie 和请求来源校验，域名或协议不一致会导致登录请求被拒绝。

生产启用顺序：

1. 首次部署保持三个认证开关均为 `false`，确认 `/api/health` 正常且原有飞书 SSO 登录不受影响。
2. 设置稳定随机的 `SESSION_SECRET`，确认 `/app/.data/auth.sqlite3` 位于持久化卷，保持单应用实例并完成定期备份与恢复演练；同时准确填写 `APP_BASE_URL` 和 `AUTH_TRUSTED_PROXY_IPS`。
3. 准备专用事务邮件账号，完成 `notify.carher.net` 的 SPF/DKIM 配置并实测验证码和重置邮件；保持 `EMAIL_VERIFICATION_REQUIRED=true`。
4. 配置 Turnstile 正式域名、Site Key 和 Secret Key，先验证成功，再设置 `TURNSTILE_ENABLED=true`。
5. 先设置 `AUTH_ENABLED=true`、`PASSWORD_LOGIN_ENABLED=true`，同时保持 `PUBLIC_SIGNUP_ENABLED=false`，验证页面、CSRF、限流和已有测试账号登录流程。
6. 设置 `AUTH_ALLOWED_EMAIL_DOMAINS=auto-link.com.cn,gmail.com,qq.com,163.com` 和 `AUTH_DEFAULT_UPSTREAM_ROLE=internal_user_viewer`，用指定测试邮箱确认开户成功且权限为 `inactive`。
7. 最后开启 `PUBLIC_SIGNUP_ENABLED=true`，完整验证注册、邮箱验证码、登录、退出、找回密码及账号开户状态。

若本地认证出现故障，优先将 `PUBLIC_SIGNUP_ENABLED=false` 停止新注册；需要完全关闭密码入口时，再将 `PASSWORD_LOGIN_ENABLED=false` 或 `AUTH_ENABLED=false`。这些操作不会关闭已配置的 SSO。当前不会自动把同邮箱的本地账号与 SSO 身份合并，上线前应避免为同一员工重复建立两套账号。

生产开放注册的外部阻塞项是有效的事务邮件 SMTP 账号、`notify.carher.net` 的 SPF/DKIM、绑定 `myai.carher.net` 的 Turnstile Site/Secret Key，以及可恢复的 SQLite 备份方案。任一项未完成时都应保持 `PUBLIC_SIGNUP_ENABLED=false`；不要通过强制显示表单或关闭安全校验来假开启注册。

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

- `GET /api/auth/config`：返回 SSO、本地密码登录、公开注册、邮箱验证和 Turnstile 的前端配置状态。
- `GET /api/auth/csrf`：获取认证写操作所需的 CSRF Token。
- `GET /api/auth/me`：返回当前登录员工信息、管理员身份和团队负责人权限。
- `POST /api/auth/verification/request`：发送注册邮箱验证码。
- `POST /api/auth/register`：使用邮箱验证码创建本地账号。
- `POST /api/auth/login`：使用邮箱和密码登录。
- `POST /api/auth/password/forgot`：请求密码重置邮件；无论账号是否存在均返回通用结果。
- `POST /api/auth/password/reset`：使用一次性重置令牌设置新密码。
- `POST /api/auth/password/change`：本地登录用户修改密码并轮换会话。
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
- 模型广场全公司近 30 天使用频率缓存默认 300 秒。
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
- 生产环境必须保持 `DEV_LOGIN_ENABLED=false`、`DEBUG_MAPPING_ENABLED=false`、`DEBUG_OIDC_CLAIMS=false`；SSO 可以与本地认证并存，并应保留为企业用户的稳定登录与应急入口。
- 生产公开注册必须同时启用邮箱验证、严格域名白名单、可用 SMTP 和 Turnstile；任一项未准备好时保持 `PUBLIC_SIGNUP_ENABLED=false`。
- 本地认证数据库和部署 Secret 必须纳入权限控制与备份；SQLite 部署保持单实例，并定期备份 `/app/.data/auth.sqlite3`。不得把 SQLite 文件、SMTP 密码、Turnstile Secret 或 session secret 放入镜像或 Git。

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
