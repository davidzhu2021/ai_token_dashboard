import base64
import logging
import os
import re
from base64 import urlsafe_b64encode
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, unquote, urljoin, urlparse, urlunparse

import httpx
from authlib.integrations.base_client import OAuthError
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .cache import TTLCache
from .auth import (
    SESSION_USER_KEY,
    allowed_email_domain,
    build_oauth,
    claim_value,
    env_bool,
    normalize_user,
    oidc_configured,
    require_admin,
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
user_mapping_cache = TTLCache()
personal_usage_cache = TTLCache()
admin_usage_cache = TTLCache()
department_usage_cache = TTLCache()
_litellm_client: LiteLLMClient | None = None


def allowed_provider_login_url(url: str) -> str | None:
    parsed = urlparse(url)
    allowed_host = os.getenv("OIDC_PROVIDER_LOGIN_HOST", "accounts.feishu.cn").strip().lower()
    if parsed.scheme != "https" or parsed.hostname != allowed_host:
        return None
    return url


def find_provider_login_url(text: str, base_url: str) -> str | None:
    allowed_host = os.getenv("OIDC_PROVIDER_LOGIN_HOST", "accounts.feishu.cn").strip().lower()
    pattern = rf"https://{re.escape(allowed_host)}[^\s\"'<>]+"
    for match in re.findall(pattern, text):
        candidate = unquote(unescape(match)).rstrip(").,;")
        if allowed := allowed_provider_login_url(candidate):
            return allowed
    for match in re.findall(r"""(?:href|src)=["']([^"']+)["']""", text, flags=re.IGNORECASE):
        candidate = unquote(unescape(urljoin(base_url, match)))
        if allowed := allowed_provider_login_url(candidate):
            return allowed
    return None


async def resolve_provider_login_url(authorize_url: str) -> str | None:
    if provider_url := await build_lark_provider_login_url(authorize_url):
        return provider_url

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0), follow_redirects=False) as http_client:
            response = await http_client.get(authorize_url, headers={"Accept": "text/html,application/xhtml+xml"})
    except httpx.HTTPError as exc:
        logger.warning("provider shortcut fetch failed: %s", exc.__class__.__name__)
        return None

    location = response.headers.get("location")
    if location:
        candidate = unquote(unescape(urljoin(authorize_url, location)))
        if allowed := allowed_provider_login_url(candidate):
            return allowed

    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type or response.text:
        return find_provider_login_url(response.text, str(response.url))
    return None


async def build_lark_provider_login_url(authorize_url: str) -> str | None:
    provider_name = os.getenv("OIDC_DIRECT_PROVIDER", "").strip()
    if not provider_name:
        return None
    app_id = os.getenv("OIDC_CASDOOR_APPLICATION_ID", "admin/ai-token-dashboard").strip()
    issuer = os.getenv("OIDC_ISSUER_URL", "").strip()
    if not app_id or not issuer:
        return None
    casdoor_base = issuer.removesuffix("/.well-known/openid-configuration").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0)) as http_client:
            response = await http_client.get(f"{casdoor_base}/api/get-application", params={"id": app_id})
            response.raise_for_status()
            application = (response.json() or {}).get("data") or {}
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("provider shortcut application lookup failed: %s", exc.__class__.__name__)
        return None

    provider = None
    for item in application.get("providers") or []:
        candidate = item.get("provider") if isinstance(item, dict) else None
        if isinstance(candidate, dict) and candidate.get("name") == provider_name:
            provider = candidate
            break
    if not provider or provider.get("type") != "Lark" or not provider.get("clientId"):
        logger.warning("provider shortcut missing Lark provider: %s", provider_name)
        return None

    parsed = urlparse(authorize_url)
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in {"provider_hint", "provider", "method", "application"}
    ]
    method = os.getenv("OIDC_DIRECT_METHOD", "signup").strip() or "signup"
    query_pairs.extend(
        [
            ("application", application.get("name") or app_id.rsplit("/", 1)[-1]),
            ("provider", provider_name),
            ("method", method),
        ]
    )
    state_payload = "?" + urlencode(query_pairs)
    state = base64.b64encode(state_payload.encode("utf-8")).decode("ascii")
    provider_host = os.getenv("OIDC_PROVIDER_LOGIN_HOST", "accounts.feishu.cn").strip()
    provider_query = urlencode(
        {
            "app_id": provider["clientId"],
            "redirect_uri": f"{casdoor_base}/callback",
            "state": state,
        }
    )
    provider_url = f"https://{provider_host}/open-apis/authen/v1/index?{provider_query}"
    return allowed_provider_login_url(provider_url)


def auth_error_response(message: str, status_code: int = 400) -> HTMLResponse:
    html = f"""
    <!doctype html>
    <html lang="zh-CN">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>登录失败</title>
        <style>
          body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f6f8f5; color: #16231f; font-family: "Microsoft YaHei", "PingFang SC", sans-serif; }}
          main {{ width: min(520px, calc(100vw - 40px)); padding: 32px; border: 1px solid #dfe8df; border-radius: 24px; background: rgba(255,255,255,.86); box-shadow: 0 24px 60px rgba(24,44,36,.12); }}
          h1 {{ margin: 0 0 12px; font-size: 24px; }}
          p {{ margin: 0 0 22px; color: #64716c; line-height: 1.7; }}
          a {{ display: inline-flex; padding: 12px 18px; border-radius: 999px; background: #163f35; color: white; text-decoration: none; font-weight: 700; }}
        </style>
      </head>
      <body><main><h1>登录没有完成</h1><p>{message}</p><a href="/">返回首页重新扫码</a></main></body>
    </html>
    """
    return HTMLResponse(html, status_code=status_code)


def client() -> LiteLLMClient:
    global _litellm_client
    try:
        if _litellm_client is None:
            _litellm_client = LiteLLMClient()
        return _litellm_client
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.on_event("shutdown")
async def close_litellm_client() -> None:
    global _litellm_client
    if _litellm_client is not None:
        await _litellm_client.close()
        _litellm_client = None


def safe_provider_name() -> str:
    value = os.getenv("OAUTH_PROVIDER_NAME", "").strip()
    if not value or "\ufffd" in value:
        return "飞书扫码登录"
    if any(ord(char) < 32 for char in value):
        return "飞书扫码登录"
    if not any(word in value for word in ("飞书", "扫码", "登录", "企业", "SSO", "sso")):
        return "飞书扫码登录"
    return value


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


async def cached_resolve_user(email: str) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized_email = email.strip().lower()
    cache_key = f"user-map:{normalized_email}"
    hit, value, ttl_seconds = user_mapping_cache.get(cache_key)
    if hit:
        return value, {"hit": True, "ttlSeconds": ttl_seconds}
    upstream = await client().resolve_user(normalized_email)
    user_mapping_cache.set(cache_key, upstream, env_int("USER_MAPPING_CACHE_TTL_SECONDS", 1800))
    return upstream, {"hit": False, "ttlSeconds": 0}


def personal_usage_cache_key(email: str, start_date: str, end_date: str, source: str) -> str:
    return f"usage:v2:{email.strip().lower()}:{start_date}:{end_date}:{source or 'all'}"


def admin_usage_cache_key(email: str, start_date: str, end_date: str, source: str, employee: str | None) -> str:
    return f"admin-usage:v1:{email.strip().lower()}:{start_date}:{end_date}:{source or 'all'}:{(employee or '').strip().lower()}"


def department_usage_cache_key(email: str, start_date: str, end_date: str, source: str, department: str | None) -> str:
    return f"department-usage:v1:{email.strip().lower()}:{start_date}:{end_date}:{source or 'all'}:{(department or '').strip().lower()}"


def empty_usage_totals() -> dict[str, Any]:
    return {
        "promptTokens": 0,
        "completionTokens": 0,
        "totalTokens": 0,
        "requestCount": 0,
        "successCount": 0,
        "failureCount": 0,
        "spend": 0.0,
    }


def add_usage_totals(target: dict[str, Any], row: dict[str, Any]) -> None:
    for field in ("promptTokens", "completionTokens", "totalTokens", "requestCount", "successCount", "failureCount"):
        target[field] += int(row.get(field) or 0)
    target["spend"] += float(row.get("spend") or 0)


def usage_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_date: dict[str, dict[str, Any]] = {}
    by_source: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    range_total = empty_usage_totals()

    for row in rows:
        add_usage_totals(range_total, row)
        day = str(row.get("date") or "")
        if day:
            date_bucket = by_date.setdefault(day, {"date": day, **empty_usage_totals()})
            add_usage_totals(date_bucket, row)
        source = str(row.get("source") or "其他")
        source_bucket = by_source.setdefault(source, {"source": source, **empty_usage_totals()})
        add_usage_totals(source_bucket, row)
        model = str(row.get("model") or "未知模型")
        model_bucket = by_model.setdefault(model, {"model": model, **empty_usage_totals()})
        add_usage_totals(model_bucket, row)

    latest_day = None
    if by_date:
        latest_key = sorted(by_date)[-1]
        latest_day = by_date[latest_key]

    return {
        "latestDay": latest_day,
        "rangeTotal": range_total,
        "sourceBreakdown": sorted(by_source.values(), key=lambda item: item["totalTokens"], reverse=True),
        "modelBreakdown": sorted(by_model.values(), key=lambda item: item["totalTokens"], reverse=True),
    }


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


async def personal_usage_payload(app_user: dict[str, Any], start_date: str, end_date: str, source: str, refresh: bool = False) -> dict[str, Any]:
    cache_key = personal_usage_cache_key(app_user["email"], start_date, end_date, source)
    hit, value, ttl_seconds = personal_usage_cache.get(cache_key)
    if hit and not refresh:
        payload = dict(value)
        payload["cache"] = {"hit": True, "ttlSeconds": ttl_seconds}
        return payload

    upstream_user, mapping_cache = await cached_resolve_user(app_user["email"])
    user_ids = upstream_user_ids(upstream_user)
    if not user_ids:
        raise HTTPException(status_code=502, detail="上游员工记录缺少 user_id")
    rows = await client().usage_rows_for_user_ids(user_ids, start_date, end_date, source)
    payload = {
        "user": app_user,
        "startDate": start_date,
        "endDate": end_date,
        "source": source,
        "rows": rows,
        "summary": usage_summary(rows),
        "mappingCache": mapping_cache,
    }
    personal_usage_cache.set(cache_key, payload, env_int("PERSONAL_USAGE_CACHE_TTL_SECONDS", 300))
    payload = dict(payload)
    payload["cache"] = {"hit": False, "ttlSeconds": 0}
    return payload


async def admin_usage_payload(admin: dict[str, Any], start_date: str, end_date: str, source: str, employee: str | None, refresh: bool = False) -> dict[str, Any]:
    cache_key = admin_usage_cache_key(admin["email"], start_date, end_date, source, employee)
    if not refresh:
        hit, value, ttl_seconds = admin_usage_cache.get(cache_key)
        if hit:
            payload = dict(value)
            payload["cache"] = {"hit": True, "ttlSeconds": ttl_seconds}
            return payload
    payload = await client().admin_usage_rows(start_date, end_date, source, employee)
    admin_usage_cache.set(cache_key, payload, env_int("ADMIN_USAGE_CACHE_TTL_SECONDS", 300))
    payload = dict(payload)
    payload["cache"] = {"hit": False, "ttlSeconds": 0}
    return payload


async def department_usage_payload(admin: dict[str, Any], start_date: str, end_date: str, source: str, department: str | None, refresh: bool = False) -> dict[str, Any]:
    cache_key = department_usage_cache_key(admin["email"], start_date, end_date, source, department)
    if not refresh:
        hit, value, ttl_seconds = department_usage_cache.get(cache_key)
        if hit:
            payload = dict(value)
            payload["cache"] = {"hit": True, "ttlSeconds": ttl_seconds}
            return payload
    payload = await client().admin_department_usage_rows(start_date, end_date, source, department)
    department_usage_cache.set(cache_key, payload, env_int("DEPARTMENT_USAGE_CACHE_TTL_SECONDS", 300))
    payload = dict(payload)
    payload["cache"] = {"hit": False, "ttlSeconds": 0}
    return payload


async def current_upstream_user(request: Request) -> tuple[dict[str, Any], dict[str, Any]]:
    app_user = require_user(request)
    upstream, _ = await cached_resolve_user(app_user["email"])
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


@app.get("/api/debug/me-usage-compare")
async def debug_me_usage_compare(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    log_pages: int = Query(3, ge=1, le=10),
) -> dict[str, Any]:
    if not env_bool("DEBUG_MAPPING_ENABLED", False):
        raise HTTPException(status_code=404, detail="接口不存在")
    app_user = require_user(request)
    if not start_date or not end_date:
        start_date, end_date = default_date_range()
    upstream, mapping_cache = await cached_resolve_user(app_user["email"])
    user_ids = upstream_user_ids(upstream)
    litellm = client()
    current_rows = await litellm.usage_rows_for_user_ids(user_ids, start_date, end_date, "all")
    daily_rows: list[dict[str, Any]] = []
    log_rows: list[dict[str, Any]] = []
    for user_id in user_ids:
        daily_rows.extend(await litellm.usage_from_daily_activity_for_debug(user_id, start_date, end_date))
        log_rows.extend(await litellm.usage_from_logs_for_debug(user_id, start_date, end_date, log_pages))
    return {
        "user": {"email": app_user["email"], "name": app_user["name"]},
        "userIds": user_ids,
        "startDate": start_date,
        "endDate": end_date,
        "mappingCache": mapping_cache,
        "current": usage_summary(current_rows),
        "dailyActivity": usage_summary(daily_rows),
        "spendLogsSample": {"pages": log_pages, "summary": usage_summary(log_rows)},
    }


@app.get("/api/debug/admin-usage-compare")
async def debug_admin_usage_compare(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    source: str = Query("all"),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    if not env_bool("DEBUG_MAPPING_ENABLED", False):
        raise HTTPException(status_code=404, detail="接口不存在")
    require_admin(request)
    if not start_date or not end_date:
        start_date, end_date = default_date_range()
    return await client().admin_usage_compare(start_date, end_date, source)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/auth/config")
async def auth_config() -> dict[str, Any]:
    return {
        "devLoginEnabled": env_bool("DEV_LOGIN_ENABLED", False),
        "oidcConfigured": oidc_configured(),
        "providerName": safe_provider_name(),
        "allowedEmailDomain": allowed_email_domain(),
    }


@app.get("/api/auth/me")
async def auth_me(request: Request) -> dict[str, Any]:
    return require_user(request)


@app.post("/api/auth/dev-login")
async def dev_login(request: Request) -> dict[str, Any]:
    if not env_bool("DEV_LOGIN_ENABLED", False):
        raise HTTPException(status_code=403, detail="开发登录未启用，请使用企业统一认证")
    payload = await request.json()
    email = str(payload.get("email", "")).strip()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="请输入有效的企业邮箱")
    email = validate_company_email(email)
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
    casdoor_response = await oauth.company.authorize_redirect(request, redirect_uri, **authorize_params)
    casdoor_url = casdoor_response.headers.get("location")
    if env_bool("FEISHU_DIRECT_LOGIN_ENABLED", False) and casdoor_url:
        return RedirectResponse(feishu_direct_url(casdoor_url))
    if env_bool("OIDC_SKIP_CASDOOR_PAGE", False) and casdoor_url:
        provider_url = await resolve_provider_login_url(casdoor_url)
        if provider_url:
            return RedirectResponse(provider_url)
        logger.warning("provider shortcut unavailable; falling back to Casdoor authorize page")
    return casdoor_response


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
    refresh: bool = Query(False),
) -> dict[str, Any]:
    app_user = require_user(request)
    if not start_date or not end_date:
        start_date, end_date = default_date_range()
    return await personal_usage_payload(app_user, start_date, end_date, source, refresh)


@app.get("/api/me/usage/logs")
async def my_usage_logs(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    source: str = Query("all"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
) -> dict[str, Any]:
    app_user = require_user(request)
    if not start_date or not end_date:
        start_date, end_date = default_date_range()
    payload = await personal_usage_payload(app_user, start_date, end_date, source)
    rows = payload["rows"]
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "user": app_user,
        "rows": rows[start:end],
        "total": len(rows),
        "page": page,
        "pageSize": page_size,
        "cache": payload.get("cache", {"hit": False, "ttlSeconds": 0}),
    }


@app.get("/api/admin/usage")
async def admin_usage(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    source: str = Query("all"),
    employee: str | None = None,
    refresh: bool = Query(False),
) -> dict[str, Any]:
    admin = require_admin(request)
    if not start_date or not end_date:
        start_date, end_date = default_date_range()
    payload = await admin_usage_payload(admin, start_date, end_date, source, employee, refresh)
    return {
        "admin": {"email": admin["email"], "name": admin["name"]},
        "startDate": start_date,
        "endDate": end_date,
        "source": source,
        "employee": employee or "",
        **payload,
    }


@app.get("/api/admin/users")
async def admin_users(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    source: str = Query("all"),
    q: str | None = None,
    refresh: bool = Query(False),
) -> dict[str, Any]:
    admin = require_admin(request)
    if not start_date or not end_date:
        start_date, end_date = default_date_range()
    payload = await admin_usage_payload(admin, start_date, end_date, source, q, refresh)
    return {"users": payload["employees"], "total": len(payload["employees"]), "startDate": start_date, "endDate": end_date, "source": source, "cache": payload.get("cache", {"hit": False, "ttlSeconds": 0})}


@app.get("/api/admin/departments/usage")
async def admin_departments_usage(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    source: str = Query("all"),
    department: str | None = None,
    refresh: bool = Query(False),
) -> dict[str, Any]:
    admin = require_admin(request)
    if not start_date or not end_date:
        start_date, end_date = default_date_range()
    payload = await department_usage_payload(admin, start_date, end_date, source, department, refresh)
    return {
        "admin": {"email": admin["email"], "name": admin["name"]},
        "startDate": start_date,
        "endDate": end_date,
        "source": source,
        "department": department or "",
        **payload,
    }


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
