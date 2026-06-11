import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
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
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "dev-session-secret-change-me"),
    same_site="lax",
    https_only=os.getenv("APP_BASE_URL", "").startswith("https://"),
)
oauth = build_oauth()


def client() -> LiteLLMClient:
    try:
        return LiteLLMClient()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


async def current_upstream_user(request: Request) -> tuple[dict[str, Any], dict[str, Any]]:
    app_user = require_user(request)
    upstream = await client().resolve_user(app_user["email"])
    return app_user, upstream


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
    return await oauth.company.authorize_redirect(request, redirect_uri)


@app.get("/api/auth/callback")
async def sso_callback(request: Request):
    if not oidc_configured():
        raise HTTPException(status_code=501, detail="企业统一认证参数尚未配置")
    token = await oauth.company.authorize_access_token(request)
    userinfo = dict(token.get("userinfo") or await oauth.company.userinfo(token=token))
    email = claim_value(userinfo, "email", "preferred_username", "username")
    if not email:
        raise HTTPException(status_code=400, detail="企业认证未返回邮箱")
    email = validate_company_email(email)
    name = claim_value(userinfo, "name", "displayName", "display_name", "nickname")
    user = normalize_user(email, name, userinfo)
    request.session[SESSION_USER_KEY] = user
    return RedirectResponse("/")


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
    user_id = upstream_user.get("user_id")
    if not user_id:
        raise HTTPException(status_code=502, detail="上游员工记录缺少 user_id")
    if not start_date or not end_date:
        start_date, end_date = default_date_range()
    rows = await client().usage_rows(str(user_id), start_date, end_date, source)
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
    user_id = upstream_user.get("user_id")
    if not start_date or not end_date:
        start_date, end_date = default_date_range()
    rows = await client().usage_rows(str(user_id), start_date, end_date, source)
    start = (page - 1) * page_size
    end = start + page_size
    return {"user": app_user, "rows": rows[start:end], "total": len(rows), "page": page, "pageSize": page_size}


@app.get("/api/me/keys")
async def my_keys(request: Request) -> dict[str, Any]:
    _, upstream_user = await current_upstream_user(request)
    user_id = upstream_user.get("user_id")
    if not user_id:
        raise HTTPException(status_code=502, detail="上游员工记录缺少 user_id")
    return {"keys": await client().keys_for_user(str(user_id))}


@app.post("/api/me/keys/{key_id:path}/regenerate")
async def regenerate_my_key(key_id: str, request: Request) -> dict[str, str]:
    app_user, upstream_user = await current_upstream_user(request)
    user_id = upstream_user.get("user_id")
    if not user_id:
        raise HTTPException(status_code=502, detail="上游员工记录缺少 user_id")
    new_key = await client().regenerate_key(key_id, str(user_id), app_user["email"])
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
