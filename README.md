# AI 用量中心

这是一个中文 AI Token 用量查询系统，当前已接入独立 FastAPI 后端，并按公司现有 **Casdoor + 飞书 SSO** 方案提供扫码登录。前端只调用本系统的 `/api/*` 接口，管理员密钥、OIDC client secret 和认证 token 都只保存在后端。

## 本地启动

```powershell
cd D:\ai-token-dashboard
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

打开：

```text
http://127.0.0.1:8000
```

## 飞书扫码登录

在 Casdoor 的 `cltx` organization 下为 AI 用量中心新建独立 application：

```text
Application name: ai-token-dashboard
Organization: cltx
Provider: 现有飞书 Provider
Redirect URI: https://ai-usage.auto-link.com.cn/api/auth/callback
```

后端 `.env` 推荐配置：

```text
LITELLM_BASE_URL=https://cc.auto-link.com.cn/pro
LITELLM_ADMIN_KEY=<管理员密钥，仅后端保存>

APP_BASE_URL=https://ai-usage.auto-link.com.cn
SESSION_SECRET=<随机长字符串>

OIDC_ISSUER_URL=http://10.68.13.198:30882
OIDC_CLIENT_ID=<ai-token-dashboard client id>
OIDC_CLIENT_SECRET=<ai-token-dashboard client secret>
OAUTH_PROVIDER_NAME=飞书扫码登录
ALLOWED_EMAIL_DOMAIN=auto-link.com.cn

DEV_LOGIN_ENABLED=false
USAGE_LOG_MAX_PAGES=20
```

如果 Casdoor 已经有 HTTPS 反代地址，`OIDC_ISSUER_URL` 优先使用 HTTPS 地址。后端同时兼容 issuer base URL 和完整 discovery URL：

```text
https://casdoor.example.com
https://casdoor.example.com/.well-known/openid-configuration
```

本地开发验证真实数据时可临时启用 `DEV_LOGIN_ENABLED=true`；生产环境必须关闭。

## 已实现接口

```text
GET  /api/auth/config
GET  /api/auth/me
GET  /api/auth/sso/start
GET  /api/auth/callback
POST /api/auth/logout
POST /api/auth/dev-login
GET  /api/me/usage?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&source=all
GET  /api/me/usage/logs?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&page=1&page_size=50
GET  /api/me/keys
POST /api/me/keys/{key_id}/regenerate
GET  /api/models
```

## 数据说明

- 飞书扫码登录后，后端只保存最小 session 信息：邮箱、姓名、头像首字母。
- 只有 `ALLOWED_EMAIL_DOMAIN` 指定的公司邮箱允许登录。
- 员工身份通过邮箱匹配上游用户的 `user_email`、`sso_user_id` 或 `user_id`。
- 用量数据优先按个人访问密钥查询日聚合，再回退到明细日志和用户日聚合接口。
- 访问密钥来自 `/key/list?user_id=<当前员工>&return_full_object=true`，前端只展示脱敏值。
- 模型广场来自 `/models`，前端展示后端返回的当前账号可用模型。

## 安全约束

- 管理员密钥不得进入前端代码、浏览器存储或日志。
- OIDC `client_secret`、认证 token、id_token 不得进入前端代码、浏览器存储或日志。
- 前端不能传任意 `user_id` 查询数据，后端始终从当前会话识别员工。
- 更新访问密钥前，后端会校验该密钥属于当前员工。
- 完整新密钥只在更新后返回一次。
- 生产环境必须使用飞书扫码登录，并保持 `DEV_LOGIN_ENABLED=false`。
- `.env`、虚拟环境和审计日志已加入 `.gitignore`。
