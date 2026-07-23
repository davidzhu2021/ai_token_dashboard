import base64
import asyncio
import hashlib
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from base64 import urlsafe_b64encode
from html import unescape
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, unquote, urljoin, urlparse, urlunparse

import httpx
from authlib.integrations.base_client import OAuthError
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
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
from .litellm_client import LiteLLMClient, default_date_range, mask_key, usage_today
from .key_vault import KeyVault, KeyVaultError
from .usage_store import UsageStore
from .usage_sync import UsageSynchronizer


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ai-token-dashboard")
logging.getLogger("httpx").setLevel(logging.WARNING)

SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "ai_token_dashboard_session")
OIDC_STATE_PREFIX = "_state_company_"

@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    await start_usage_sync()
    try:
        yield
    finally:
        await close_litellm_client()


app = FastAPI(title="AI 用量中心", lifespan=app_lifespan)
app.mount("/assets", StaticFiles(directory=ROOT_DIR / "assets"), name="assets")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "dev-session-secret-change-me"),
    session_cookie=SESSION_COOKIE_NAME,
    same_site="lax",
    https_only=os.getenv("APP_BASE_URL", "").startswith("https://"),
)
oauth = build_oauth()
user_mapping_cache = TTLCache()
personal_usage_cache = TTLCache()
admin_usage_cache = TTLCache()
department_usage_cache = TTLCache()
team_auth_cache = TTLCache()
team_usage_cache = TTLCache()
team_member_usage_cache = TTLCache()
_litellm_client: LiteLLMClient | None = None
_key_vault: KeyVault | None = None
_usage_store: UsageStore | None = UsageStore.from_environment()
_usage_sync_task: asyncio.Task[Any] | None = None
_usage_refresh_task: asyncio.Task[Any] | None = None
_usage_sync_stop: asyncio.Event | None = None
_usage_sync_status: dict[str, Any] = {"status": "disabled", "lastRun": None}


def allowed_provider_login_url(url: str) -> str | None:
    parsed = urlparse(url)
    allowed_host = os.getenv("OIDC_PROVIDER_LOGIN_HOST", "accounts.feishu.cn").strip().lower()
    if parsed.scheme != "https" or parsed.hostname != allowed_host:
        return None
    return url


def oidc_state_keys(request: Request) -> list[str]:
    return sorted(key for key in request.session if key.startswith(OIDC_STATE_PREFIX))


def request_host(value: str | None) -> str:
    if not value:
        return ""
    return urlparse(value).hostname or ""


def callback_query_state_present(request: Request) -> bool:
    return bool(str(request.query_params.get("state") or "").strip())


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


def key_vault() -> KeyVault:
    global _key_vault
    if _key_vault is None:
        _key_vault = KeyVault.from_environment(ROOT_DIR)
    return _key_vault


def usage_store() -> UsageStore | None:
    return _usage_store


def usage_backend_ids() -> list[str]:
    return [backend.id for backend in client().backends]


def usage_data_freshness(last_synced: datetime | None, start_date: str, end_date: str) -> dict[str, Any]:
    """Mark only ranges containing today as stale when their snapshot is old."""
    max_age = max(60, env_int("USAGE_LIVE_REFRESH_MAX_AGE_SECONDS", 1800))
    today = usage_today().isoformat()
    stale = False
    if end_date >= today:
        stale = last_synced is None or (datetime.now(timezone.utc) - last_synced).total_seconds() >= max_age
    return {
        "source": "database",
        "lastSyncedAt": last_synced.isoformat() if last_synced else None,
        "stale": stale,
    }


async def run_usage_sync(days: int) -> dict[str, Any]:
    store = usage_store()
    if store is None:
        return {"status": "disabled", "rowCount": 0, "backendCount": 0}
    try:
        await store.connect()
        result = await UsageSynchronizer(client(), store).sync(
            *UsageSynchronizer.date_range(days),
        )
        _usage_sync_status.update(
            {
                "status": result.get("status", "ok"),
                "lastRun": datetime.now(timezone.utc).isoformat(),
                "rowCount": result.get("rowCount", 0),
                "backendCount": result.get("backendCount", 0),
                "errors": result.get("errors", []),
            }
        )
        return result
    except Exception as exc:
        logger.exception("usage sync failed")
        _usage_sync_status.update(
            {
                "status": "error",
                "lastRun": datetime.now(timezone.utc).isoformat(),
                "error": exc.__class__.__name__,
            }
        )
        return {"status": "error", "rowCount": 0, "backendCount": 0}


async def usage_sync_loop() -> None:
    initial_days = max(1, env_int("USAGE_INITIAL_BACKFILL_DAYS", 90))
    lookback_days = max(1, env_int("USAGE_SYNC_LOOKBACK_DAYS", 3))
    interval_seconds = max(60, env_int("USAGE_SYNC_INTERVAL_SECONDS", 1800))
    store = usage_store()
    try:
        if store is not None:
            await store.connect()
        backend_ids = usage_backend_ids()
        previous_day = usage_today() - timedelta(days=1)
        start_date, end_date = UsageSynchronizer.date_range(initial_days, previous_day)
        has_history = bool(store and await store.has_complete_coverage(start_date, end_date, backend_ids))
        if not has_history:
            await run_usage_sync(initial_days)
        else:
            _usage_sync_status.update({"status": "ready", "lastRun": None, "initialBackfill": "complete"})
    except Exception:
        logger.exception("initial usage coverage check failed")
        await run_usage_sync(initial_days)
    while _usage_sync_stop is not None and not _usage_sync_stop.is_set():
        try:
            await asyncio.wait_for(_usage_sync_stop.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            await run_usage_sync(lookback_days)


async def schedule_usage_refresh(start_date: str, end_date: str, force: bool = False) -> None:
    store = usage_store()
    if store is None:
        return
    today = usage_today().isoformat()
    if end_date < today:
        return
    backend_ids = usage_backend_ids()
    covered = await store.covered_backend_ids(start_date, end_date, backend_ids)
    last_sync = await store.latest_sync_at(start_date, end_date, covered)
    stale = set(covered) != set(backend_ids) or usage_data_freshness(last_sync, start_date, end_date)["stale"]
    if not force and not stale:
        return
    await run_usage_sync(max(1, env_int("USAGE_SYNC_LOOKBACK_DAYS", 3)))


async def prepare_usage_refresh(start_date: str, end_date: str, force: bool = False) -> None:
    # 手动刷新只重读 SQL 快照，避免把一次页面刷新升级成全量上游同步。
    if force:
        logger.info(
            "manual refresh skips upstream usage sync start=%s end=%s",
            start_date,
            end_date,
        )
        return
    trigger_usage_refresh(start_date, end_date)


def manual_refresh_database_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="用量数据库暂时不可用，请稍后重试")


def trigger_usage_refresh(start_date: str, end_date: str, force: bool = False) -> None:
    global _usage_refresh_task
    if _usage_refresh_task is not None and not _usage_refresh_task.done():
        return

    async def refresh() -> None:
        try:
            await schedule_usage_refresh(start_date, end_date, force)
        except Exception:
            logger.exception("usage refresh failed")

    _usage_refresh_task = asyncio.create_task(refresh(), name="usage-live-refresh")


async def start_usage_sync() -> None:
    global _usage_sync_task, _usage_sync_stop
    if usage_store() is None:
        return
    if _usage_sync_task is not None and not _usage_sync_task.done():
        return
    _usage_sync_status.update({"status": "starting", "lastRun": None})
    _usage_sync_stop = asyncio.Event()
    _usage_sync_task = asyncio.create_task(usage_sync_loop(), name="usage-sync-loop")


async def close_litellm_client() -> None:
    global _usage_sync_task, _usage_refresh_task, _usage_sync_stop
    if _usage_sync_stop is not None:
        _usage_sync_stop.set()
    if _usage_sync_task is not None:
        _usage_sync_task.cancel()
        try:
            await _usage_sync_task
        except asyncio.CancelledError:
            pass
        _usage_sync_task = None
        _usage_sync_stop = None
    if _usage_refresh_task is not None:
        _usage_refresh_task.cancel()
        try:
            await _usage_refresh_task
        except asyncio.CancelledError:
            pass
        _usage_refresh_task = None
    if usage_store() is not None:
        await usage_store().close()
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


async def cached_resolve_user(email: str, name: str | None = None, refresh: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized_email = email.strip().lower()
    normalized_name = str(name or "").strip()
    cache_key = f"user-map:v2:{normalized_email}:{normalized_name}"
    hit, value, ttl_seconds = user_mapping_cache.get(cache_key)
    if hit and not refresh:
        return value, {"hit": True, "ttlSeconds": ttl_seconds}
    upstream = await client().resolve_user(normalized_email, normalized_name)
    user_mapping_cache.set(cache_key, upstream, env_int("USER_MAPPING_CACHE_TTL_SECONDS", 1800))
    return upstream, {"hit": False, "ttlSeconds": 0}


def personal_usage_cache_key(email: str, start_date: str, end_date: str, source: str) -> str:
    return f"usage:v6:{email.strip().lower()}:{start_date}:{end_date}:{source or 'all'}"


def admin_usage_cache_key(email: str, start_date: str, end_date: str, source: str, employee: str | None) -> str:
    return f"admin-usage:v4:{email.strip().lower()}:{start_date}:{end_date}:{source or 'all'}:{(employee or '').strip().lower()}"


def department_usage_cache_key(email: str, start_date: str, end_date: str, source: str, department: str | None) -> str:
    return f"department-usage:v4:{email.strip().lower()}:{start_date}:{end_date}:{source or 'all'}:{(department or '').strip().lower()}"


def team_auth_cache_key(email: str, name: str | None) -> str:
    return f"team-auth:v2:{email.strip().lower()}:{str(name or '').strip()}"


def team_usage_cache_key(email: str, team: dict[str, Any], start_date: str, end_date: str, source: str) -> str:
    return f"team-usage:v6:{email.strip().lower()}:{team.get('backend')}:{team.get('id')}:{start_date}:{end_date}:{source or 'all'}"


def team_member_usage_cache_key(email: str, team: dict[str, Any], employee: str, start_date: str, end_date: str, source: str) -> str:
    return f"team-member-usage:v4:{email.strip().lower()}:{team.get('backend')}:{team.get('id')}:{employee.strip().lower()}:{start_date}:{end_date}:{source or 'all'}"


def team_ref(team: dict[str, Any]) -> str:
    raw = f"{team.get('backend')}:{team.get('id')}".encode("utf-8")
    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest()[:12]).decode("ascii").rstrip("=")


def public_team(team: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(team, dict):
        return None
    return {
        "teamRef": team_ref(team),
        "id": team.get("id"),
        "name": team.get("name"),
        "memberCount": team.get("memberCount"),
    }


def public_team_from_payload(authorized_team: dict[str, Any], payload_team: dict[str, Any] | None = None) -> dict[str, Any]:
    result = public_team(authorized_team) or {}
    if isinstance(payload_team, dict):
        for key in ("id", "name", "memberCount"):
            if payload_team.get(key) is not None:
                result[key] = payload_team[key]
    return result


def select_authorized_team(scope: dict[str, Any], team_ref_value: str | None = None) -> dict[str, Any]:
    leader_teams = [team for team in scope.get("leaderTeams") or [] if isinstance(team, dict)]
    if not leader_teams:
        raise HTTPException(status_code=403, detail="当前账号还没有团队负责人权限")
    if team_ref_value:
        for team in leader_teams:
            if team_ref(team) == team_ref_value:
                return team
        raise HTTPException(status_code=403, detail="当前账号无权查看该团队看板")
    selected = scope.get("team") if isinstance(scope.get("team"), dict) else None
    if selected:
        return selected
    return leader_teams[0]


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
    request_started = asyncio.get_running_loop().time()
    cache_key = personal_usage_cache_key(app_user["email"], start_date, end_date, source)
    hit, value, ttl_seconds = personal_usage_cache.get(cache_key)
    if hit and not refresh:
        payload = dict(value)
        payload["cache"] = {"hit": True, "ttlSeconds": ttl_seconds}
        return payload

    store = usage_store()
    if store is not None:
        try:
            db_started = asyncio.get_running_loop().time()
            await store.connect()
            connected_at = asyncio.get_running_loop().time()
            await prepare_usage_refresh(start_date, end_date, refresh)
            stored = await store.personal_rows(app_user["email"], start_date, end_date, source, usage_backend_ids())
            queried_at = asyncio.get_running_loop().time()
            logger.info("personal usage sql refresh=%s connect_ms=%.0f query_ms=%.0f total_ms=%.0f", refresh, (connected_at - db_started) * 1000, (queried_at - connected_at) * 1000, (queried_at - request_started) * 1000)
            if stored is not None:
                rows = stored["rows"]
                payload = {
                    "user": app_user,
                    "startDate": start_date,
                    "endDate": end_date,
                    "source": source,
                    "rows": rows,
                    "summary": usage_summary(rows),
                    "mappingCache": {"hit": True, "ttlSeconds": 0},
                    "dataFreshness": usage_data_freshness(stored.get("lastSyncedAt"), start_date, end_date),
                }
                personal_usage_cache.set(cache_key, payload, env_int("PERSONAL_USAGE_CACHE_TTL_SECONDS", 300))
                payload["cache"] = {"hit": False, "ttlSeconds": 0}
                return payload
        except Exception:
            logger.exception("local personal usage query failed; falling back to upstream")

    if refresh:
        raise manual_refresh_database_unavailable()

    upstream_user, mapping_cache = await cached_resolve_user(app_user["email"], app_user.get("name"), refresh)
    user_ids = list(dict.fromkeys(upstream_user_ids(upstream_user)))
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


async def person_usage_rows(email: str, name: str | None, start_date: str, end_date: str, source: str, refresh: bool = False, extra_user_ids: list[str] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    payload = await personal_usage_payload({"email": email, "name": name or email}, start_date, end_date, source, refresh)
    upstream, _ = await cached_resolve_user(email, name, refresh)
    user_ids = upstream_user_ids(upstream) if upstream.get("matched_accounts") else []
    user_ids.extend(str(item).strip() for item in (extra_user_ids or []) if str(item).strip())
    user_ids = list(dict.fromkeys(user_ids or upstream_user_ids(upstream)))
    if extra_user_ids:
        resolved = set(upstream_user_ids(upstream))
        user_ids = [item for item in user_ids if item in resolved or item in {str(value).strip() for value in extra_user_ids}]
    if not user_ids:
        raise HTTPException(status_code=502, detail="上游员工记录缺少 user_id")
    return payload["rows"], user_ids


async def admin_usage_payload(admin: dict[str, Any], start_date: str, end_date: str, source: str, employee: str | None, refresh: bool = False) -> dict[str, Any]:
    request_started = asyncio.get_running_loop().time()
    cache_key = admin_usage_cache_key(admin["email"], start_date, end_date, source, employee)
    if not refresh:
        hit, value, ttl_seconds = admin_usage_cache.get(cache_key)
        if hit:
            payload = dict(value)
            payload["cache"] = {"hit": True, "ttlSeconds": ttl_seconds}
            return payload
    store = usage_store()
    if store is not None:
        try:
            db_started = asyncio.get_running_loop().time()
            await store.connect()
            connected_at = asyncio.get_running_loop().time()
            await prepare_usage_refresh(start_date, end_date, refresh)
            stored = await store.admin_rows(start_date, end_date, source, employee, usage_backend_ids())
            queried_at = asyncio.get_running_loop().time()
            logger.info("admin usage sql refresh=%s connect_ms=%.0f query_ms=%.0f total_ms=%.0f", refresh, (connected_at - db_started) * 1000, (queried_at - connected_at) * 1000, (queried_at - request_started) * 1000)
            if stored is not None:
                stored = dict(stored)
                last_synced = stored.pop("lastSyncedAt", None)
                stored["dataFreshness"] = usage_data_freshness(last_synced, start_date, end_date)
                admin_usage_cache.set(cache_key, stored, env_int("ADMIN_USAGE_CACHE_TTL_SECONDS", 300))
                stored["cache"] = {"hit": False, "ttlSeconds": 0}
                return stored
        except Exception:
            logger.exception("local admin usage query failed; falling back to upstream")
    if refresh:
        raise manual_refresh_database_unavailable()
    payload = await client().admin_usage_rows(start_date, end_date, source, employee)
    admin_usage_cache.set(cache_key, payload, env_int("ADMIN_USAGE_CACHE_TTL_SECONDS", 300))
    payload = dict(payload)
    payload["cache"] = {"hit": False, "ttlSeconds": 0}
    return payload


async def department_usage_payload(admin: dict[str, Any], start_date: str, end_date: str, source: str, department: str | None, refresh: bool = False) -> dict[str, Any]:
    request_started = asyncio.get_running_loop().time()
    cache_key = department_usage_cache_key(admin["email"], start_date, end_date, source, department)
    if not refresh:
        hit, value, ttl_seconds = department_usage_cache.get(cache_key)
        if hit:
            payload = dict(value)
            payload["cache"] = {"hit": True, "ttlSeconds": ttl_seconds}
            return payload
    store = usage_store()
    if store is not None:
        try:
            db_started = asyncio.get_running_loop().time()
            await store.connect()
            connected_at = asyncio.get_running_loop().time()
            await prepare_usage_refresh(start_date, end_date, refresh)
            stored = await store.department_rows(start_date, end_date, source, department, usage_backend_ids())
            queried_at = asyncio.get_running_loop().time()
            logger.info("department usage sql refresh=%s connect_ms=%.0f query_ms=%.0f total_ms=%.0f", refresh, (connected_at - db_started) * 1000, (queried_at - connected_at) * 1000, (queried_at - request_started) * 1000)
            if stored is not None:
                stored = dict(stored)
                last_synced = stored.pop("lastSyncedAt", None)
                stored["dataFreshness"] = usage_data_freshness(last_synced, start_date, end_date)
                department_usage_cache.set(cache_key, stored, env_int("DEPARTMENT_USAGE_CACHE_TTL_SECONDS", 300))
                stored["cache"] = {"hit": False, "ttlSeconds": 0}
                return stored
        except Exception:
            logger.exception("local department usage query failed; falling back to upstream")
    if refresh:
        raise manual_refresh_database_unavailable()
    payload = await client().admin_department_usage_rows(start_date, end_date, source, department)
    department_usage_cache.set(cache_key, payload, env_int("DEPARTMENT_USAGE_CACHE_TTL_SECONDS", 300))
    payload = dict(payload)
    payload["cache"] = {"hit": False, "ttlSeconds": 0}
    return payload


async def team_scope_for_user(app_user: dict[str, Any], refresh: bool = False) -> dict[str, Any]:
    cache_key = team_auth_cache_key(app_user["email"], app_user.get("name"))
    if not refresh:
        hit, value, ttl_seconds = team_auth_cache.get(cache_key)
        if hit:
            scope = dict(value)
            scope["cache"] = {"hit": True, "ttlSeconds": ttl_seconds}
            return scope
    try:
        upstream_user, _ = await cached_resolve_user(app_user["email"], app_user.get("name"), refresh)
        scope = await client().team_leader_scope(upstream_user)
    except HTTPException as exc:
        if exc.status_code == 404:
            scope = {"isTeamLeader": False, "teamBoardStatus": "none", "team": None, "leaderTeams": []}
        else:
            raise
    team_auth_cache.set(cache_key, scope, env_int("TEAM_AUTH_CACHE_TTL_SECONDS", 300))
    scope = dict(scope)
    scope["cache"] = {"hit": False, "ttlSeconds": 0}
    return scope


async def app_user_with_team_scope(app_user: dict[str, Any]) -> dict[str, Any]:
    scope = await team_scope_for_user(app_user)
    selected_team = public_team(scope.get("team"))
    public_teams = [team for team in (public_team(item) for item in scope.get("leaderTeams") or []) if team]
    enriched = dict(app_user)
    enriched.update(
        {
            "isTeamLeader": bool(scope.get("isTeamLeader")),
            "teamBoardStatus": scope.get("teamBoardStatus", "none"),
            "team": selected_team,
            "leaderTeams": public_teams,
        }
    )
    return enriched


async def team_usage_payload(
    app_user: dict[str, Any],
    start_date: str,
    end_date: str,
    source: str,
    refresh: bool = False,
    team_ref_value: str | None = None,
    enrich_member_rankings: bool = True,
) -> dict[str, Any]:
    request_started = asyncio.get_running_loop().time()
    # 权限范围不是用量数据，刷新用量时沿用缓存，避免再次访问上游。
    scope = await team_scope_for_user(app_user, False)
    if not scope.get("isTeamLeader"):
        raise HTTPException(status_code=403, detail="当前账号还没有团队负责人权限")
    team = select_authorized_team(scope, team_ref_value)
    cache_key = team_usage_cache_key(app_user["email"], team, start_date, end_date, source)
    if enrich_member_rankings and not refresh:
        hit, value, ttl_seconds = team_usage_cache.get(cache_key)
        if hit:
            payload = dict(value)
            payload["cache"] = {"hit": True, "ttlSeconds": ttl_seconds}
            return payload
    store = usage_store()
    payload = None
    if store is not None:
        try:
            db_started = asyncio.get_running_loop().time()
            await store.connect()
            connected_at = asyncio.get_running_loop().time()
            await prepare_usage_refresh(start_date, end_date, refresh)
            if not enrich_member_rankings:
                payload = await store.team_rows(str(team["backend"]), str(team["id"]), start_date, end_date, source)
            else:
                payload = await store.team_rows(str(team["backend"]), str(team["id"]), start_date, end_date, source)
            queried_at = asyncio.get_running_loop().time()
            logger.info("team usage sql refresh=%s connect_ms=%.0f query_ms=%.0f total_ms=%.0f", refresh, (connected_at - db_started) * 1000, (queried_at - connected_at) * 1000, (queried_at - request_started) * 1000)
            if payload is not None:
                payload = dict(payload)
                last_synced = payload.pop("lastSyncedAt", None)
                payload["dataFreshness"] = usage_data_freshness(last_synced, start_date, end_date)
        except Exception:
            logger.exception("local team usage query failed; falling back to upstream")
            payload = None
    if payload is None:
        if refresh:
            raise manual_refresh_database_unavailable()
        payload = dict(await client().team_usage_rows(str(team["backend"]), str(team["id"]), start_date, end_date, source))
    if enrich_member_rankings:
        payload["employees"] = await team_member_rankings_from_accounts(payload.get("employees") or [], start_date, end_date, source, refresh)
    payload["team"] = public_team_from_payload(team, payload.get("team"))
    if enrich_member_rankings:
        team_usage_cache.set(cache_key, payload, env_int("TEAM_USAGE_CACHE_TTL_SECONDS", 300))
    payload = dict(payload)
    payload["cache"] = {"hit": False, "ttlSeconds": 0}
    return payload


def clean_identifier(value: Any) -> str:
    return str(value or "").strip()


def team_employee_public_user(employee: dict[str, Any], selected_team: dict[str, Any]) -> dict[str, Any]:
    return {
        "email": clean_identifier(employee.get("employeeEmail")),
        "name": clean_identifier(employee.get("employeeName")) or clean_identifier(employee.get("employeeId")) or "团队成员",
        "avatar": initials_text(clean_identifier(employee.get("employeeEmail")), clean_identifier(employee.get("employeeName"))),
        "department": clean_identifier(selected_team.get("name")) or "团队",
        "isAdmin": False,
        "isTeamLeader": False,
        "team": public_team(selected_team),
        "employeeId": clean_identifier(employee.get("employeeId")),
        "teamRole": clean_identifier(employee.get("teamRole")) or "user",
        "bindStatus": clean_identifier(employee.get("bindStatus")),
    }


def initials_text(email: str, name: str | None = None) -> str:
    text = (name or email or "员工").strip()
    return text[:1].upper()


def employee_match_values(employee: dict[str, Any]) -> set[str]:
    values = {
        clean_identifier(employee.get("employeeId")).lower(),
        clean_identifier(employee.get("employeeEmail")).lower(),
        clean_identifier(employee.get("employeeName")).lower(),
    }
    user_ids = employee.get("userIds")
    if isinstance(user_ids, list):
        values.update(clean_identifier(item).lower() for item in user_ids)
    return {value for value in values if value}


def find_team_employee(payload: dict[str, Any], employee: str) -> dict[str, Any]:
    normalized = employee.strip().lower()
    if not normalized:
        raise HTTPException(status_code=400, detail="请选择要查看的团队成员")
    for item in payload.get("employees") or []:
        if isinstance(item, dict) and normalized in employee_match_values(item):
            return item
    raise HTTPException(status_code=404, detail="未找到该团队成员")


async def user_ids_for_team_employee(employee: dict[str, Any], refresh: bool) -> list[str]:
    resolved_ids: list[str] = []
    email = clean_identifier(employee.get("employeeEmail")).lower()
    if email:
        upstream_user, _ = await cached_resolve_user(email, clean_identifier(employee.get("employeeName")), refresh)
        if upstream_user.get("matched_accounts"):
            resolved_ids.extend(upstream_user_ids(upstream_user))
    user_ids = employee.get("userIds")
    if isinstance(user_ids, list):
        resolved_ids.extend(clean_identifier(item) for item in user_ids if clean_identifier(item))

    employee_id = clean_identifier(employee.get("employeeId"))
    if not resolved_ids and employee_id:
        resolved_ids.append(employee_id)
    return list(dict.fromkeys(resolved_ids))


def team_employee_empty_summary(employee: dict[str, Any]) -> dict[str, Any]:
    employee_id = clean_identifier(employee.get("employeeId")) or clean_identifier(employee.get("employeeEmail"))
    user_ids = employee.get("userIds")
    clean_user_ids = [clean_identifier(item) for item in user_ids if clean_identifier(item)] if isinstance(user_ids, list) else []
    return {
        "employeeId": employee_id,
        "employeeName": clean_identifier(employee.get("employeeName")) or employee_id or "团队成员",
        "employeeEmail": clean_identifier(employee.get("employeeEmail")),
        "bindStatus": clean_identifier(employee.get("bindStatus")) or "未绑定邮箱",
        "userIds": clean_user_ids,
        "promptTokens": 0,
        "completionTokens": 0,
        "totalTokens": 0,
        "requestCount": 0,
        "successCount": 0,
        "failureCount": 0,
        "spend": 0.0,
        "primarySource": "其他",
        "teamRole": clean_identifier(employee.get("teamRole")) or "user",
    }


def team_employee_sort_key(employee: dict[str, Any]) -> tuple[float, float, float, str]:
    name = clean_identifier(employee.get("employeeName")) or clean_identifier(employee.get("employeeEmail")) or clean_identifier(employee.get("employeeId"))
    return (
        -float(employee.get("totalTokens") or 0),
        -float(employee.get("spend") or 0),
        -float(employee.get("requestCount") or 0),
        name.lower(),
    )


async def team_member_rankings_from_accounts(
    employees: list[dict[str, Any]],
    start_date: str,
    end_date: str,
    source: str,
    refresh: bool,
) -> list[dict[str, Any]]:
    rankings: list[dict[str, Any]] = []
    litellm = client()
    for employee in employees:
        if not isinstance(employee, dict):
            continue
        summary = team_employee_empty_summary(employee)
        try:
            email = clean_identifier(employee.get("employeeEmail"))
            user_ids = await user_ids_for_team_employee(employee, refresh)
            summary["userIds"] = user_ids
            if email:
                rows, user_ids = await person_usage_rows(email, clean_identifier(employee.get("employeeName")), start_date, end_date, source, refresh, user_ids)
                summary["userIds"] = user_ids
            else:
                rows = await litellm.usage_rows_for_user_ids(user_ids, start_date, end_date, source) if user_ids else []
        except HTTPException:
            rows = []
        totals = usage_summary(rows)
        range_total = totals["rangeTotal"]
        for field in ("promptTokens", "completionTokens", "totalTokens", "requestCount", "successCount", "failureCount"):
            summary[field] = range_total[field]
        summary["spend"] = range_total["spend"]
        source_breakdown = totals.get("sourceBreakdown") or []
        if source_breakdown:
            summary["primarySource"] = source_breakdown[0].get("source") or "其他"
        rankings.append(summary)
    return sorted(rankings, key=team_employee_sort_key)


async def team_member_usage_payload(
    app_user: dict[str, Any],
    start_date: str,
    end_date: str,
    source: str,
    employee: str,
    refresh: bool = False,
    team_ref_value: str | None = None,
) -> dict[str, Any]:
    request_started = asyncio.get_running_loop().time()
    # 成员明细刷新只重读 SQL，不刷新团队权限缓存。
    scope = await team_scope_for_user(app_user, False)
    if not scope.get("isTeamLeader"):
        raise HTTPException(status_code=403, detail="当前账号还没有团队负责人权限")

    team = select_authorized_team(scope, team_ref_value)
    cache_key = team_member_usage_cache_key(app_user["email"], team, employee, start_date, end_date, source)
    if not refresh:
        hit, value, ttl_seconds = team_member_usage_cache.get(cache_key)
        if hit:
            payload = dict(value)
            payload["cache"] = {"hit": True, "ttlSeconds": ttl_seconds}
            return payload

    team_payload = await team_usage_payload(app_user, start_date, end_date, source, refresh, team_ref_value, enrich_member_rankings=False)
    selected_employee = find_team_employee(team_payload, employee)
    rows: list[dict[str, Any]] | None = None
    stored_payload: dict[str, Any] | None = None
    store = usage_store()
    if store is not None:
        try:
            db_started = asyncio.get_running_loop().time()
            await store.connect()
            connected_at = asyncio.get_running_loop().time()
            await prepare_usage_refresh(start_date, end_date, refresh)
            stored_payload = await store.team_member_rows(str(team["backend"]), str(team["id"]), employee, start_date, end_date, source)
            queried_at = asyncio.get_running_loop().time()
            logger.info("team member usage sql refresh=%s connect_ms=%.0f query_ms=%.0f total_ms=%.0f", refresh, (connected_at - db_started) * 1000, (queried_at - connected_at) * 1000, (queried_at - request_started) * 1000)
            if stored_payload is not None:
                rows = stored_payload["rows"]
        except Exception:
            logger.exception("local team member usage query failed; falling back to upstream")
    if refresh and rows is None:
        raise manual_refresh_database_unavailable()
    user_ids = await user_ids_for_team_employee(selected_employee, refresh)
    email = clean_identifier(selected_employee.get("employeeEmail"))
    if email:
        rows, user_ids = await person_usage_rows(email, clean_identifier(selected_employee.get("employeeName")), start_date, end_date, source, refresh, user_ids)
        stored_payload = None
    if not user_ids:
        raise HTTPException(status_code=404, detail="该团队成员缺少可查询的用量账号")

    if rows is None:
        rows = await client().usage_rows_for_user_ids(user_ids, start_date, end_date, source)
    public_user = team_employee_public_user(selected_employee, team)
    payload = {
        "user": public_user,
        "team": public_team_from_payload(team, team_payload.get("team")),
        "teamRef": team_payload.get("team", {}).get("teamRef", team_ref_value or ""),
        "startDate": start_date,
        "endDate": end_date,
        "source": source,
        "rows": rows,
        "summary": usage_summary(rows),
        "employee": {
            "employeeId": selected_employee.get("employeeId"),
            "employeeName": selected_employee.get("employeeName"),
            "employeeEmail": selected_employee.get("employeeEmail"),
            "teamRole": selected_employee.get("teamRole"),
            "bindStatus": selected_employee.get("bindStatus"),
        },
    }
    if stored_payload is not None:
        last_synced = stored_payload.get("lastSyncedAt")
        payload["dataFreshness"] = usage_data_freshness(last_synced, start_date, end_date)
    team_member_usage_cache.set(cache_key, payload, env_int("TEAM_MEMBER_USAGE_CACHE_TTL_SECONDS", 300))
    payload = dict(payload)
    payload["cache"] = {"hit": False, "ttlSeconds": 0}
    return payload


async def current_upstream_user(request: Request, refresh: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    app_user = require_user(request)
    upstream, _ = await cached_resolve_user(app_user["email"], app_user.get("name"), refresh)
    return app_user, upstream


def upstream_user_ids(upstream_user: dict[str, Any]) -> list[str]:
    ids = upstream_user.get("matched_user_ids")
    if isinstance(ids, list):
        cleaned = [str(item) for item in ids if item]
        if cleaned:
            return cleaned
    user_id = upstream_user.get("user_id")
    return [str(user_id)] if user_id else []


def primary_upstream_user_id(upstream_user: dict[str, Any]) -> str:
    accounts = upstream_user.get("matched_accounts")
    if isinstance(accounts, list):
        for account in accounts:
            if isinstance(account, dict) and account.get("backend") == "primary" and account.get("user_id"):
                return str(account["user_id"])
    for user_id in upstream_user_ids(upstream_user):
        if ":" not in user_id:
            return user_id
    raise HTTPException(status_code=502, detail="未找到当前员工的主访问账号")


class CreatePersonalKeyRequest(BaseModel):
    name: str = Field(min_length=2, max_length=50)
    purpose: str = Field(default="", max_length=200)
    duration: Literal["never", "30d", "90d"] = "never"
    models: list[str] = Field(default_factory=list)

    @field_validator("name", "purpose")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("name")
    @classmethod
    def validate_name_not_blank(cls, value: str) -> str:
        if len(value) < 2:
            raise ValueError("名称至少需要 2 个字符")
        return value


class DisableOldKeyRequest(BaseModel):
    replacementKeyId: str = Field(min_length=1, max_length=128)


def write_key_audit(event: str, email: str, key_id: str, request: Request, result: str) -> None:
    audit_key_id = hashlib.sha256(key_id.encode("utf-8")).hexdigest() if key_id.startswith("sk-") else key_id
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", audit_key_id)[:64] or "-"
    client_host = request.client.host if request.client else "-"
    audit_line = f"{datetime.now(timezone.utc).isoformat()}\t{event}\t{email}\t{safe_id}\t{client_host}\t{result}\n"
    try:
        with (ROOT_DIR / "audit.log").open("a", encoding="utf-8") as audit:
            audit.write(audit_line)
    except OSError:
        logger.exception("failed to write audit log")


def public_key(key: dict[str, Any], revealable: bool) -> dict[str, Any]:
    return {**{name: value for name, value in key.items() if not name.startswith("_")}, "revealable": revealable}


def add_revealability(keys: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        vault = key_vault()
        key_scopes = {
            (
                str(key.get("_backendId") or "primary"),
                str(key.get("_userId") or ""),
                str(key.get("id") or ""),
            )
            for key in keys
        }
        pending_by_scope: dict[tuple[str, str, str], dict[str, Any]] = {}
        scopes = {
            (str(key.get("_backendId") or "primary"), str(key.get("_userId") or ""))
            for key in keys
        }
        for backend_id, user_id in scopes:
            for pending in vault.pending_rotations(backend_id, user_id):
                old_scope = (backend_id, user_id, str(pending["oldKeyId"]))
                replacement_scope = (backend_id, user_id, str(pending["replacementKeyId"]))
                display_scope = old_scope if old_scope in key_scopes else replacement_scope
                if display_scope in key_scopes:
                    pending_by_scope[display_scope] = pending
        return [
            {
                **public_key(
                    key,
                    vault.has(str(key.get("_backendId") or "primary"), str(key.get("_userId") or ""), str(key.get("id") or "")),
                ),
                **(
                    {
                        "cleanupRequired": pending_by_scope[
                            (str(key.get("_backendId") or "primary"), str(key.get("_userId") or ""), str(key.get("id") or ""))
                        ]["cleanupTarget"]
                        == "old",
                        "recoveryRequired": pending_by_scope[
                            (str(key.get("_backendId") or "primary"), str(key.get("_userId") or ""), str(key.get("id") or ""))
                        ]["cleanupTarget"]
                        == "replacement",
                        "oldKeyId": pending_by_scope[
                            (str(key.get("_backendId") or "primary"), str(key.get("_userId") or ""), str(key.get("id") or ""))
                        ]["oldKeyId"],
                        "replacementKeyId": pending_by_scope[
                            (str(key.get("_backendId") or "primary"), str(key.get("_userId") or ""), str(key.get("id") or ""))
                        ]["replacementKeyId"],
                    }
                    if (
                        str(key.get("_backendId") or "primary"),
                        str(key.get("_userId") or ""),
                        str(key.get("id") or ""),
                    )
                    in pending_by_scope
                    else {}
                ),
            }
            for key in keys
        ]
    except KeyVaultError:
        logger.exception("failed to read key vault state")
        return [public_key(key, False) for key in keys]


def store_created_key(user_id: str, created: dict[str, str]) -> str:
    key_id = str(created.get("id") or "")
    plaintext = str(created.get("key") or "")
    try:
        key_vault().store("primary", user_id, key_id, plaintext)
        return ""
    except KeyVaultError:
        logger.exception("failed to store created key in vault")
        return "密钥已创建，但加密保管失败；关闭后将无法再次查看，请立即复制并安全保存。"


@app.get("/api/debug/me-mapping")
async def debug_me_mapping(request: Request, refresh: bool = Query(False)) -> dict[str, Any]:
    if not env_bool("DEBUG_MAPPING_ENABLED", False):
        raise HTTPException(status_code=404, detail="接口不存在")
    app_user, upstream_user = await current_upstream_user(request, refresh)
    return {
        "email": app_user["email"],
        "userIds": upstream_user_ids(upstream_user),
        "matchedBy": upstream_user.get("matched_by"),
        "matchedSources": upstream_user.get("matched_sources", {}),
        "matchedAccounts": upstream_user.get("matched_accounts", []),
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
    upstream, mapping_cache = await cached_resolve_user(app_user["email"], app_user.get("name"))
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
async def health() -> dict[str, Any]:
    result: dict[str, Any] = {"status": "ok", "usageSync": dict(_usage_sync_status)}
    store = usage_store()
    if store is not None:
        result["usageDatabase"] = await store.health()
        if result["usageDatabase"].get("status") in {"error", "disconnected"}:
            result["status"] = "degraded"
    else:
        result["usageDatabase"] = {"enabled": False, "connected": False, "status": "disabled"}
    if result["usageSync"].get("status") in {"error", "failed", "partial"}:
        result["status"] = "degraded"
    return result


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
    user = dict(require_user(request))
    user.update({"isTeamLeader": False, "teamBoardStatus": "loading", "team": None, "leaderTeams": []})
    return user


@app.get("/api/auth/scope")
async def auth_scope(request: Request) -> dict[str, Any]:
    user = require_user(request)
    started = asyncio.get_running_loop().time()
    scope = await team_scope_for_user(user)
    payload = {
        "isTeamLeader": bool(scope.get("isTeamLeader")),
        "teamBoardStatus": scope.get("teamBoardStatus", "none"),
        "team": public_team(scope.get("team")),
        "leaderTeams": [team for team in (public_team(item) for item in scope.get("leaderTeams") or []) if team],
    }
    logger.info("auth scope resolved email=%s cache=%s duration_ms=%.0f", user.get("email"), scope.get("cache", {}).get("hit"), (asyncio.get_running_loop().time() - started) * 1000)
    return payload


@app.post("/api/auth/dev-login")
async def dev_login(request: Request) -> dict[str, Any]:
    if not env_bool("DEV_LOGIN_ENABLED", False):
        raise HTTPException(status_code=403, detail="开发登录未启用，请使用企业统一认证")
    payload = await request.json()
    email = str(payload.get("email", "")).strip()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="请输入有效的企业邮箱")
    email = validate_company_email(email)
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
    logger.info(
        "oidc start redirect_host=%s state_count=%s direct_provider=%s skip_casdoor=%s feishu_direct=%s cookie_present=%s",
        request_host(redirect_uri),
        len(oidc_state_keys(request)),
        bool(direct_provider),
        env_bool("OIDC_SKIP_CASDOOR_PAGE", False),
        env_bool("FEISHU_DIRECT_LOGIN_ENABLED", False),
        SESSION_COOKIE_NAME in request.cookies,
    )
    if env_bool("FEISHU_DIRECT_LOGIN_ENABLED", False) and casdoor_url:
        return RedirectResponse(feishu_direct_url(casdoor_url))
    if env_bool("OIDC_SKIP_CASDOOR_PAGE", False) and casdoor_url:
        provider_url = await resolve_provider_login_url(casdoor_url)
        if provider_url:
            logger.info("oidc start provider shortcut host=%s", request_host(provider_url))
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
        return RedirectResponse("/?auth_callback=success")
    except OAuthError as exc:
        state_keys = oidc_state_keys(request)
        logger.warning(
            "oidc callback oauth error=%s cookie_present=%s query_state_present=%s state_count=%s has_user=%s",
            exc.__class__.__name__,
            SESSION_COOKIE_NAME in request.cookies,
            callback_query_state_present(request),
            len(state_keys),
            SESSION_USER_KEY in request.session,
        )
        if exc.__class__.__name__ == "MismatchingStateError":
            return auth_error_response("登录状态已失效或扫码链接已过期，请从首页重新点击飞书扫码登录。", 400)
        if SESSION_USER_KEY not in request.session:
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


@app.get("/api/team/usage")
async def team_usage(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    source: str = Query("all"),
    team_ref: str | None = None,
    refresh: bool = Query(False),
) -> dict[str, Any]:
    app_user = require_user(request)
    if not start_date or not end_date:
        start_date, end_date = default_date_range()
    payload = await team_usage_payload(app_user, start_date, end_date, source, refresh, team_ref)
    return {
        "leader": {"email": app_user["email"], "name": app_user["name"]},
        "startDate": start_date,
        "endDate": end_date,
        "source": source,
        "teamRef": payload.get("team", {}).get("teamRef", team_ref or ""),
        **payload,
    }


@app.get("/api/team/member/usage")
async def team_member_usage(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    source: str = Query("all"),
    team_ref: str | None = None,
    employee: str | None = None,
    refresh: bool = Query(False),
) -> dict[str, Any]:
    app_user = require_user(request)
    if not start_date or not end_date:
        start_date, end_date = default_date_range()
    if not employee:
        raise HTTPException(status_code=400, detail="请选择要查看的团队成员")
    payload = await team_member_usage_payload(app_user, start_date, end_date, source, employee, refresh, team_ref)
    return {
        "leader": {"email": app_user["email"], "name": app_user["name"]},
        "startDate": start_date,
        "endDate": end_date,
        "source": source,
        "teamRef": payload.get("team", {}).get("teamRef", team_ref or ""),
        **payload,
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
async def my_keys(request: Request, refresh: bool = Query(False)) -> dict[str, Any]:
    _, upstream_user = await current_upstream_user(request)
    user_ids = upstream_user_ids(upstream_user)
    if not user_ids:
        raise HTTPException(status_code=502, detail="上游员工记录缺少 user_id")
    primary_user_id = primary_upstream_user_id(upstream_user)
    available_models, unrestricted = await client().available_key_models(primary_user_id)
    keys = await client().keys_for_user_ids(user_ids, refresh)
    return {
        "keys": add_revealability(keys),
        "availableModels": [model for model in available_models if model not in {"no-default-models", "all-proxy-models"}],
        "unrestrictedModels": unrestricted,
    }


@app.post("/api/me/keys")
async def create_my_key(data: CreatePersonalKeyRequest, request: Request) -> JSONResponse:
    app_user, upstream_user = await current_upstream_user(request)
    primary_user_id = primary_upstream_user_id(upstream_user)
    try:
        created = await client().create_key(
            primary_user_id,
            data.name,
            data.purpose,
            data.duration,
            data.models,
            app_user["email"],
        )
    except HTTPException:
        write_key_audit("create", app_user["email"], "-", request, "failed")
        raise
    warning = store_created_key(primary_user_id, created)
    write_key_audit("create", app_user["email"], created.get("id", "-"), request, "success_vault_failed" if warning else "success")
    return JSONResponse(
        {**created, "revealable": not warning, "warning": warning},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@app.post("/api/me/keys/{key_id:path}/regenerate")
async def regenerate_my_key(key_id: str, request: Request) -> JSONResponse:
    app_user, upstream_user = await current_upstream_user(request)
    user_ids = upstream_user_ids(upstream_user)
    if not user_ids:
        raise HTTPException(status_code=502, detail="上游员工记录缺少 user_id")
    regenerated = None
    regenerated_user_id = ""
    regenerated_backend_id = "primary"
    last_error: HTTPException | None = None
    for user_id in user_ids:
        try:
            backend, raw_user_id = client()._decode_account_id(user_id)
            regenerated_backend_id = backend.id
            regenerated_user_id = raw_user_id
            pending = key_vault().pending_rotation(regenerated_backend_id, regenerated_user_id, key_id)
            if pending is not None:
                raise HTTPException(status_code=409, detail="该密钥已有待完成的更新，请先停用旧密钥")
            if await client().supports_atomic_key_regeneration(user_id):
                try:
                    regenerated = await client().regenerate_key(key_id, user_id, app_user["email"])
                    rotation_mode = "atomic"
                except HTTPException as exc:
                    if exc.status_code != 501:
                        raise
                    regenerated = await client().create_replacement_key(key_id, user_id, app_user["email"])
                    rotation_mode = "replacement"
            else:
                regenerated = await client().create_replacement_key(key_id, user_id, app_user["email"])
                rotation_mode = "replacement"
            break
        except HTTPException as exc:
            last_error = exc
            if exc.status_code not in {403, 404}:
                break
    if regenerated is None:
        write_key_audit("regenerate", app_user["email"], key_id, request, "failed")
        raise last_error or HTTPException(status_code=403, detail="不能更新不属于自己的访问密钥")
    warning = ""
    cleanup_required = False
    recovery_required = False
    old_key_disabled = rotation_mode == "atomic"
    revealable = False
    if rotation_mode == "atomic":
        try:
            key_vault().replace(regenerated_backend_id, regenerated_user_id, key_id, regenerated["id"], regenerated["key"])
            revealable = True
        except KeyVaultError:
            logger.exception("failed to store atomically regenerated key in vault")
            warning = "密钥已更新，但加密保管失败；关闭后将无法再次查看，请立即复制并安全保存。"
    else:
        try:
            key_vault().store(regenerated_backend_id, regenerated_user_id, regenerated["id"], regenerated["key"])
            revealable = True
        except KeyVaultError:
            logger.exception("failed to store replacement key in vault")
            try:
                await client().delete_key(regenerated["id"], user_id, app_user["email"])
            except HTTPException:
                logger.exception("failed to compensate replacement key after vault failure")
                recovery_required = True
                warning = "高风险：新密钥未能加密保管，且自动撤销失败。旧密钥仍然有效，请立即复制本次新密钥并联系管理员清理新密钥。"
            else:
                write_key_audit("regenerate_replacement", app_user["email"], key_id, request, "vault_failed_compensated")
                raise HTTPException(status_code=503, detail="新密钥保管失败，系统已撤销本次新密钥，旧密钥仍可继续使用，请稍后重试")
        if revealable:
            try:
                await client().delete_key(key_id, user_id, app_user["email"])
                old_key_disabled = True
            except HTTPException as exc:
                cleanup_required = True
                warning = "新密钥已创建并保管，但旧密钥暂未停用；当前两把密钥均可使用，请重试停用旧密钥。"
                try:
                    key_vault().record_pending_rotation(
                        regenerated_backend_id,
                        regenerated_user_id,
                        key_id,
                        regenerated["id"],
                        "old",
                        str(exc.detail),
                    )
                except KeyVaultError:
                    logger.exception("failed to persist pending old-key cleanup")
                    cleanup_required = False
                    recovery_required = True
                    warning = "高风险：新密钥已创建并保管，但旧密钥未停用，且待处理状态保存失败。当前两把密钥均可使用，请联系管理员处理。"
            else:
                try:
                    key_vault().delete(regenerated_backend_id, regenerated_user_id, key_id)
                except KeyVaultError:
                    logger.exception("failed to remove disabled old key from vault")
                    warning = "新密钥已更新并可使用，但旧密钥的本地保管记录清理失败，请联系管理员处理。"
    audit_result = "success"
    if recovery_required:
        audit_result = "replacement_cleanup_failed"
    elif cleanup_required:
        audit_result = "old_key_disable_failed"
    elif warning:
        audit_result = "success_vault_failed"
    write_key_audit("regenerate", app_user["email"], key_id, request, audit_result)
    return JSONResponse(
        {
            "key": regenerated["key"],
            "id": regenerated["id"],
            "masked": mask_key(regenerated["key"]),
            "revealable": revealable,
            "warning": warning,
            "rotationMode": rotation_mode,
            "oldKeyDisabled": old_key_disabled,
            "cleanupRequired": cleanup_required,
            "recoveryRequired": recovery_required,
            "oldKeyId": key_id,
            "replacementKeyId": regenerated["id"],
            "expiresAt": regenerated.get("expiresAt", "永不过期"),
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@app.post("/api/me/keys/{old_key_id:path}/disable-old")
async def disable_old_key(old_key_id: str, data: DisableOldKeyRequest, request: Request) -> JSONResponse:
    app_user, upstream_user = await current_upstream_user(request)
    last_error: HTTPException | None = None
    for user_id in upstream_user_ids(upstream_user):
        backend, raw_user_id = client()._decode_account_id(user_id)
        try:
            pending = key_vault().pending_rotation(backend.id, raw_user_id, old_key_id)
        except KeyVaultError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if pending is None or pending.get("cleanupTarget") != "old":
            continue
        replacement_key_id = str(pending["replacementKeyId"])
        if data.replacementKeyId != replacement_key_id:
            raise HTTPException(status_code=409, detail="替代密钥与待处理记录不一致，请刷新页面后重试")
        try:
            await client().disable_pending_old_key(old_key_id, replacement_key_id, user_id, app_user["email"])
        except HTTPException as exc:
            last_error = exc
            break
        key_vault().complete_pending_rotation(backend.id, raw_user_id, old_key_id)
        write_key_audit("disable_old_key", app_user["email"], old_key_id, request, "success")
        return JSONResponse(
            {
                "oldKeyDisabled": True,
                "cleanupRequired": False,
                "oldKeyId": old_key_id,
                "replacementKeyId": replacement_key_id,
                "warning": "",
            },
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )
    write_key_audit("disable_old_key", app_user["email"], old_key_id, request, "failed")
    raise last_error or HTTPException(status_code=404, detail="未找到待完成的密钥更新记录")


@app.delete("/api/me/keys/{key_id:path}")
async def delete_my_key(key_id: str, request: Request) -> dict[str, Any]:
    app_user, upstream_user = await current_upstream_user(request)
    user_ids = upstream_user_ids(upstream_user)
    if not user_ids:
        raise HTTPException(status_code=502, detail="上游员工记录缺少 user_id")
    deleted_user_id = ""
    deleted_backend_id = "primary"
    last_error: HTTPException | None = None
    for user_id in user_ids:
        try:
            await client().delete_key(key_id, user_id, app_user["email"])
            backend, raw_user_id = client()._decode_account_id(user_id)
            deleted_backend_id = backend.id
            deleted_user_id = raw_user_id
            break
        except HTTPException as exc:
            last_error = exc
            if exc.status_code != 403:
                break
    if not deleted_user_id:
        write_key_audit("delete", app_user["email"], key_id, request, "failed")
        raise last_error or HTTPException(status_code=403, detail="不能删除不属于自己的访问密钥")
    try:
        key_vault().delete(deleted_backend_id, deleted_user_id, key_id)
        warning = ""
    except KeyVaultError:
        logger.exception("failed to delete key from vault")
        warning = "密钥已删除并立即失效，但本地加密保管记录清理失败，请联系管理员处理。"
    write_key_audit("delete", app_user["email"], key_id, request, "success_vault_failed" if warning else "success")
    return {"deleted": True, "warning": warning}


@app.post("/api/me/keys/{key_id:path}/reveal")
async def reveal_my_key(key_id: str, request: Request) -> JSONResponse:
    app_user, upstream_user = await current_upstream_user(request)
    keys = await client().keys_for_user_ids(upstream_user_ids(upstream_user), refresh=True)
    owned = next((key for key in keys if str(key.get("id") or "") == key_id), None)
    if owned is None:
        write_key_audit("reveal", app_user["email"], key_id, request, "forbidden")
        raise HTTPException(status_code=403, detail="不能查看不属于自己的访问密钥")
    try:
        plaintext = key_vault().reveal(
            str(owned.get("_backendId") or "primary"),
            str(owned.get("_userId") or ""),
            key_id,
        )
    except KeyVaultError as exc:
        write_key_audit("reveal", app_user["email"], key_id, request, "failed")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if plaintext is None:
        write_key_audit("reveal", app_user["email"], key_id, request, "not_stored")
        raise HTTPException(status_code=404, detail="该密钥创建时未保管完整值，请再生成后查看")
    write_key_audit("reveal", app_user["email"], key_id, request, "success")
    return JSONResponse({"key": plaintext}, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@app.get("/api/models")
async def models(request: Request) -> dict[str, Any]:
    require_user(request)
    usage_counts: dict[str, int] | None = None
    store = usage_store()
    if store is not None:
        try:
            await store.connect()
            end_day = usage_today()
            start_day = end_day - timedelta(days=29)
            usage_counts = await store.model_usage_counts(
                start_day.isoformat(),
                end_day.isoformat(),
                usage_backend_ids(),
            )
        except Exception:
            logger.exception("local model usage query failed; falling back to upstream")
    return {"models": await client().models(usage_counts)}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT_DIR / "index.html", headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@app.get("/{path:path}")
async def spa_fallback(path: str) -> FileResponse:
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="接口不存在")
    return FileResponse(ROOT_DIR / "index.html", headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
