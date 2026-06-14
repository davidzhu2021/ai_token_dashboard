import logging
import os
from base64 import urlsafe_b64encode
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

from authlib.integrations.base_client import OAuthError
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .auth import (
    SESSION_USER_KEY,
    allowed_email_domain,
    build_oauth,
    claim_value,
    env_bool,
    normalize_user,
    oidc_configured,
    require_user,
    validate_company_email,
)
from .litellm_client import LiteLLMClient, default_date_range, mask_key


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ai-token-dashboard")
logging.getLogger("httpx").setLevel(logging.WARNING)

app = FastAPI(title="AI 用量中心")
app.mount("/assets", StaticFiles(directory=ROOT_DIR / "assets"), name="assets")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "dev-session-secret-change-me"),
    same_site="lax",
    https_only=os.getenv("APP_BASE_URL", "").startswith("https://"),
)
oauth = build_oauth()


def auth_error_response(message: str, status_code: int = 400) -> HTMLResponse:
    html = f"""
    <!doctype html>
    <html lang="zh-CN">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>登录失败</title>
        <style>
          body {{
            margin: 0;
            min-height: 100vh;
            display: grid;
            place-items: center;
            background: #f6f8f5;
            color: #16231f;
            font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
          }}
          main {{
            width: min(520px, calc(100vw - 40px));
            padding: 32px;
            border: 1px solid #dfe8df;
            border-radius: 24px;
            background: rgba(255, 255, 255, .86);
            box-shadow: 0 24px 60px rgba(24, 44, 36, .12);
          }}
          h1 {{ margin: 0 0 12px; font-size: 24px; }}
          p {{ margin: 0 0 22px; color: #64716c; line-height: 1.7; }}
          a {{
            display: inline-flex;
            padding: 12px 18px;
            border-radius: 999px;
            background: #163f35;
            color: white;
            text-decoration: none;
            font-weight: 700;
          }}
        </style>
      </head>
      <body>
        <main>
          <h1>登录没有完成</h1>
          <p>{message}</p>
          <a href="/">返回首页重新扫码</a>
        </main>
      </body>
    </html>
    """
    return HTMLResponse(html, status_code=status_code)


def client() -> LiteLLMClient:
    try:
        return LiteLLMClient()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def feishu_direct_url(casdoor_authorize_url: str) -> str:
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    redirect_uri = os.getenv("FEISHU_REDIRECT_URI", "").strip()
    if not app_id or not redirect_uri:
        return casdoor_authorize_url

    parsed = urlparse(casdoor_authorize_url)
    query = parsed.query
    if query and os.getenv("OIDC_APPLICATION_NAME", "").strip() and "application=" not in query:
        query = query + "&" + urlencode({"application": os.getenv("OIDC_APPLICATION_NAME", "").strip()})

    state = urlsafe_b64encode(("?" + query).encode("utf-8")).decode("ascii")
    params = urlencode({"app_id": app_id, "redirect_uri": redirect_uri, "state": state})
    return urlunparse(("https", "accounts.feishu.cn", "/open-apis/authen/v1/index", "", params, ""))


async def current_upstream_user(request: Request) -> tuple[dict[str, Any], dict[str, Any]]:
    app_user = require_user(request)
    upstream = await client().resolve_user(app_user["email"])
    return app_user, upstream


def upstream_user_ids(upstream_user: dict[str, Any]) -> list[str]:
    ids = upstream_user.get("matched_user_ids")
    if isinstance(ids, list):
        cleaned = [str(item) for item in ids if item]
        if cleaned:
            return cleaned
    user_id = upstream_user.get("user_id")
    return [str(user_id)] if user_id else []


@app.get("/api/debug/me-mapping")
async def debug_me_mapping(request: Request) -> dict[str, Any]:
    if not env_bool("DEBUG_MAPPING_ENABLED", False):
        raise HTTPException(status_code=404, detail="接口不存在")
    app_user, upstream_user = await current_upstream_user(request)
    return {
        "email": app_user["email"],
        "userIds": upstream_user_ids(upstream_user),
        "matchedBy": upstream_user.get("matched_by"),
        "matchedSources": upstream_user.get("matched_sources", {}),
    }


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/auth/config")
async def auth_config() -> dict[str, Any]:
    return {
        "devLoginEnabled": env_bool("DEV_LOGIN_ENABLED", False),
        "oidcConfigured": oidc_configured(),
        "providerName": os.getenv("OAUTH_PROVIDER_NAME", "飞书扫码登录"),
        "allowedEmailDomain": allowed_email_domain(),
    }


@app.get("/api/auth/me")
async def auth_me(request: Request) -> dict[str, Any]:
    user = request.session.get(SESSION_USER_KEY)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


@app.post("/api/auth/dev-login")
async def dev_login(request: Request) -> dict[str, Any]:
    if not env_bool("DEV_LOGIN_ENABLED", False):
        raise HTTPException(status_code=403, detail="开发登录未启用，请使用企业统一认证")
    payload = await request.json()
    email = str(payload.get("email", "")).strip()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="请输入有效的企业邮箱")
    email = validate_company_email(email)
    # Validate that the employee exists upstream before creating a local session.
    await client().resolve_user(email)
    user = normalize_user(email)
    request.session[SESSION_USER_KEY] = user
    return user


@app.get("/api/auth/sso/start")
async def sso_start(request: Request):
    if not oidc_configured():
        raise HTTPException(status_code=501, detail="企业统一认证参数尚未配置")
    redirect_uri = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/") + "/api/auth/callback"
    authorize_params: dict[str, str] = {}
    direct_provider = os.getenv("OIDC_DIRECT_PROVIDER", "").strip()
    direct_method = os.getenv("OIDC_DIRECT_METHOD", "").strip()
    direct_application = os.getenv("OIDC_APPLICATION_NAME", "").strip()
    if direct_application:
        authorize_params["application"] = direct_application
    if direct_provider:
        authorize_params["provider_hint"] = direct_provider
        authorize_params["provider"] = direct_provider
    if direct_method:
        authorize_params["method"] = direct_method
    response = await oauth.company.authorize_redirect(request, redirect_uri, **authorize_params)
    if env_bool("FEISHU_DIRECT_LOGIN_ENABLED", False):
        location = response.headers.get("location")
        if location:
            return RedirectResponse(feishu_direct_url(location))
    return response


@app.get("/api/auth/callback")
async def sso_callback(request: Request):
    if not oidc_configured():
        raise HTTPException(status_code=501, detail="企业统一认证参数尚未配置")
    try:
        token = await oauth.company.authorize_access_token(request)
        raw_userinfo = token.get("userinfo") or await oauth.company.userinfo(token=token)
        userinfo = dict(raw_userinfo or {})
        if env_bool("DEBUG_OIDC_CLAIMS", False):
            logger.info("oidc callback claims: %s", sorted(userinfo.keys()))
        email = claim_value(userinfo, "email", "preferred_username", "username")
        if not email:
            logger.warning("oidc callback missing email claim; claims=%s", sorted(userinfo.keys()))
            return auth_error_response("企业认证没有返回邮箱，请联系管理员检查登录应用的授权范围。", 400)
        email = validate_company_email(email)
        name = claim_value(userinfo, "displayName", "display_name", "nickname", "name")
        user = normalize_user(email, name, userinfo)
        request.session[SESSION_USER_KEY] = user
        return RedirectResponse("/")
    except OAuthError as exc:
        logger.warning("oidc callback oauth error: %s", exc.__class__.__name__)
        request.session.clear()
        return auth_error_response("登录状态已失效或扫码链接已过期，请从首页重新发起飞书扫码登录。", 400)
    except HTTPException:
        raise
    except Exception:
        logger.exception("oidc callback failed")
        request.session.clear()
        return auth_error_response("登录回调处理失败，请重新扫码；如果持续失败，请联系管理员查看后端日志。", 500)


@app.post("/api/auth/logout")
async def logout(request: Request) -> dict[str, bool]:
    request.session.clear()
    return {"ok": True}


@app.get("/api/me/usage")
async def my_usage(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    source: str = Query("all"),
) -> dict[str, Any]:
    app_user, upstream_user = await current_upstream_user(request)
    user_ids = upstream_user_ids(upstream_user)
    if not user_ids:
        raise HTTPException(status_code=502, detail="上游员工记录缺少 user_id")
    if not start_date or not end_date:
        start_date, end_date = default_date_range()
    rows = await client().usage_rows_for_user_ids(user_ids, start_date, end_date, source)
    return {
        "user": app_user,
        "startDate": start_date,
        "endDate": end_date,
        "source": source,
        "rows": rows,
    }


@app.get("/api/me/usage/logs")
async def my_usage_logs(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    source: str = Query("all"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
) -> dict[str, Any]:
    app_user, upstream_user = await current_upstream_user(request)
    user_ids = upstream_user_ids(upstream_user)
    if not start_date or not end_date:
        start_date, end_date = default_date_range()
    rows = await client().usage_rows_for_user_ids(user_ids, start_date, end_date, source)
    start = (page - 1) * page_size
    end = start + page_size
    return {"user": app_user, "rows": rows[start:end], "total": len(rows), "page": page, "pageSize": page_size}


@app.get("/api/me/keys")
async def my_keys(request: Request) -> dict[str, Any]:
    _, upstream_user = await current_upstream_user(request)
    user_ids = upstream_user_ids(upstream_user)
    if not user_ids:
        raise HTTPException(status_code=502, detail="上游员工记录缺少 user_id")
    return {"keys": await client().keys_for_user_ids(user_ids)}


@app.post("/api/me/keys/{key_id:path}/regenerate")
async def regenerate_my_key(key_id: str, request: Request) -> dict[str, str]:
    app_user, upstream_user = await current_upstream_user(request)
    user_ids = upstream_user_ids(upstream_user)
    if not user_ids:
        raise HTTPException(status_code=502, detail="上游员工记录缺少 user_id")
    new_key = None
    last_error: HTTPException | None = None
    for user_id in user_ids:
        try:
            new_key = await client().regenerate_key(key_id, user_id, app_user["email"])
            break
        except HTTPException as exc:
            last_error = exc
    if new_key is None:
        raise last_error or HTTPException(status_code=403, detail="不能更新不属于自己的访问密钥")
    audit_line = f'{app_user["email"]}\t{key_id}\t{request.client.host if request.client else "-"}\tsuccess\n'
    try:
        with (ROOT_DIR / "audit.log").open("a", encoding="utf-8") as audit:
            audit.write(audit_line)
    except OSError:
        logger.exception("failed to write audit log")
    return {"key": new_key, "masked": mask_key(new_key)}


@app.get("/api/models")
async def models(request: Request) -> dict[str, Any]:
    require_user(request)
    return {"models": await client().models()}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT_DIR / "index.html")


@app.get("/{path:path}")
async def spa_fallback(path: str) -> FileResponse:
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="接口不存在")
    return FileResponse(ROOT_DIR / "index.html")
