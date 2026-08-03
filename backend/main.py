import base64
import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import smtplib
import socket
import ssl
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from base64 import urlsafe_b64encode
from email.message import EmailMessage
from html import unescape
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, unquote, urljoin, urlparse, urlunparse

import httpx
from authlib.integrations.base_client import OAuthError
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .cache import TTLCache
from .auth import (
    SESSION_USER_KEY,
    allowed_email_domain,
    build_oauth,
    claim_value,
    clear_server_session,
    csrf_token,
    env_bool,
    generate_auth_token,
    generate_numeric_code,
    get_server_session_token,
    hash_auth_token,
    hash_password,
    is_platform_admin_email,
    normalize_user,
    oidc_configured,
    password_needs_rehash,
    require_admin,
    require_platform_admin,
    require_user,
    set_server_session,
    validate_company_email,
    verify_csrf_token,
    verify_password,
)
from . import billing
from .auth_store import AuthStore, AuthStoreConfigError, DuplicateEmailError
from .billing_store import (
    BillingStore,
    BillingStoreError,
    CHANNEL_EPAY,
    CHANNEL_MANUAL_QR,
    ORDER_PENDING,
    SYNC_DONE,
    SYNC_PENDING,
)
from .litellm_client import (
    LiteLLMClient,
    default_date_range,
    mask_key,
    model_display_name,
    normalize_model_display_name,
    usage_today,
)
from .key_vault import KeyVault, KeyVaultError
from .organization_store import (
    DEFAULT_TOKEN_DAILY_BUDGET_USD,
    MAX_MODELS_PER_TOKEN,
    MAX_TOKEN_DAILY_BUDGET_USD,
    MIN_TOKEN_DAILY_BUDGET_USD,
    ORGANIZATION_TOKEN_MODELS,
    DuplicateMemberEmailError,
    InMemoryOrganizationStore,
    OrganizationConflictError,
    OrganizationNotFoundError,
    OrganizationPermissionError,
    OrganizationStore,
    OrganizationStoreError,
    OrganizationValidationError,
)
from .usage_store import UsageStore
from .usage_sync import UsageSynchronizer, run_sync_with_recent_refresh


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ai-token-dashboard")
logging.getLogger("httpx").setLevel(logging.WARNING)

SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "ai_token_dashboard_session")
OIDC_STATE_PREFIX = "_state_company_"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def session_cookie_max_age() -> int:
    """Keep the signed session cookie lifetime aligned with server sessions."""
    try:
        configured = int(os.getenv("AUTH_SESSION_TTL_SECONDS", "1209600"))
    except ValueError:
        configured = 1_209_600
    return max(300, configured)

@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    validate_runtime_auth_config()
    await start_billing_store()
    await start_usage_sync()
    try:
        yield
    finally:
        await close_litellm_client()


app = FastAPI(title="通衢 API", lifespan=app_lifespan)
# 首屏要先下载 index.html 与 app.js 才能发出任何接口请求，两者合计 500KB 以上。
# 它们是纯文本，压缩后只剩两成，是首屏可感知延迟里最便宜的一段。
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.mount("/assets", StaticFiles(directory=ROOT_DIR / "assets"), name="assets")


@app.middleware("http")
async def hydrate_server_session(request: Request, call_next):
    """Hydrate sync route dependencies from an opaque server-side session."""
    token = get_server_session_token(request)
    if token:
        session = await auth_store_call("get_session", token)
        user = await auth_store_call("get_user", session["user_id"]) if session else None
        if user and str(user.get("status") or "active") == "active":
            request.session[SESSION_USER_KEY] = await auth_user_payload(user)
        else:
            clear_server_session(request)
    try:
        return await call_next(request)
    finally:
        # Do not serialize profile data back into local-auth cookies.
        if get_server_session_token(request):
            request.session.pop(SESSION_USER_KEY, None)


app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "dev-session-secret-change-me"),
    session_cookie=SESSION_COOKIE_NAME,
    max_age=session_cookie_max_age(),
    same_site="lax",
    https_only=urlparse(os.getenv("APP_BASE_URL", "").strip()).scheme.lower() == "https",
)
oauth = build_oauth()
user_mapping_cache = TTLCache()
personal_usage_cache = TTLCache()
admin_usage_cache = TTLCache()
department_usage_cache = TTLCache()
team_auth_cache = TTLCache()
team_usage_cache = TTLCache()
team_member_usage_cache = TTLCache()
# Generated customer-demo boards are isolated from the production board
# caches. Their keys are derived from the server-resolved organization scope.
organization_usage_cache = TTLCache()
_litellm_client: LiteLLMClient | None = None
_key_vault: KeyVault | None = None
_usage_store: UsageStore | None = UsageStore.from_environment()
_billing_store: BillingStore | None = BillingStore.from_environment()
_auth_store: AuthStore | None = None
_organization_store: OrganizationStore | None = None
local_entitlement_cache = TTLCache()
_usage_sync_task: asyncio.Task[Any] | None = None
_usage_refresh_task: asyncio.Task[Any] | None = None
_usage_sync_stop: asyncio.Event | None = None
_usage_sync_status: dict[str, Any] = {"status": "disabled", "lastRun": None}


def validate_runtime_auth_config() -> None:
    """Reject unsafe public deployments while preserving loopback development."""
    app_base_url = os.getenv("APP_BASE_URL", "").strip()
    parsed_base_url = urlparse(app_base_url)
    app_host = (parsed_base_url.hostname or "").lower()
    auth_requested = any(
        env_bool(name, False)
        for name in ("AUTH_ENABLED", "PASSWORD_LOGIN_ENABLED", "PUBLIC_SIGNUP_ENABLED")
    )
    loopback_development = parsed_base_url.scheme.lower() == "http" and app_host in LOOPBACK_HOSTS
    if auth_requested and not loopback_development and parsed_base_url.scheme.lower() != "https":
        raise RuntimeError("启用邮箱认证时 APP_BASE_URL 必须使用 HTTPS；仅允许本机回环地址使用 HTTP")
    if parsed_base_url.scheme.lower() != "https":
        return
    secret = os.getenv("SESSION_SECRET", "").strip()
    weak_values = {"", "dev-session-secret-change-me", "replace-with-a-random-long-string"}
    if secret in weak_values or len(secret) < 32:
        raise RuntimeError("HTTPS 部署必须配置至少 32 个字符的随机 SESSION_SECRET")
    if env_bool("AUTH_EMAIL_DEBUG", False):
        raise RuntimeError("HTTPS 部署不能启用 AUTH_EMAIL_DEBUG")
    if env_bool("DEV_LOGIN_ENABLED", False):
        raise RuntimeError("HTTPS 部署不能启用 DEV_LOGIN_ENABLED")
    if env_bool("SMTP_SSL", False) and env_bool("SMTP_STARTTLS", True):
        raise RuntimeError("SMTP_SSL 和 SMTP_STARTTLS 不能同时启用")
    # Missing optional local-auth dependencies must not take the existing SSO
    # service down. The auth config endpoint and each route expose a precise
    # unavailable status instead, leaving the email entry points closed.


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


def billing_store() -> BillingStore | None:
    return _billing_store


def organization_demo_enabled() -> bool:
    """Whether the side-effect-free enterprise organization demo is available."""
    return env_bool("ORGANIZATION_DEMO_ENABLED", False)


def organization_store() -> OrganizationStore:
    """Return the process-local demo store only after the feature is enabled."""
    global _organization_store
    if not organization_demo_enabled():
        raise HTTPException(status_code=404, detail="企业组织演示功能尚未启用")
    if _organization_store is None:
        _organization_store = InMemoryOrganizationStore()
    return _organization_store


async def organization_store_call(method: str, *args: Any, **kwargs: Any) -> Any:
    function = getattr(organization_store(), method)
    return await asyncio.to_thread(function, *args, **kwargs)


def require_billing_store() -> BillingStore:
    """取充值账本，未启用时按未实现的功能返回 404。

    这样未配置的部署完全看不到充值能力，行为与上线前保持一致。
    """
    store = billing_store()
    if store is None or store.pool is None:
        raise HTTPException(status_code=404, detail="充值功能尚未开放")
    return store


def auth_store() -> AuthStore:
    global _auth_store
    if _auth_store is None:
        _auth_store = AuthStore.from_environment(ROOT_DIR)
    return _auth_store


async def auth_store_call(method: str, *args: Any, **kwargs: Any) -> Any:
    function = getattr(auth_store(), method)
    return await asyncio.to_thread(function, *args, **kwargs)


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
        result = await run_sync_with_recent_refresh(client(), store, days)
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


async def start_billing_store() -> None:
    """建立充值账本连接。

    连接失败不阻止应用启动——用量看板与登录不该被充值功能拖垮，路由层会把
    未连接的账本当成"功能未开放"。
    """
    store = billing_store()
    if store is None:
        return
    try:
        await store.connect()
    except Exception:
        logger.exception("billing store connect failed; topup routes stay disabled")


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
    if billing_store() is not None:
        await billing_store().close()
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


def request_ip(request: Request) -> str:
    peer = str(request.client.host if request.client else "").strip()
    if not _trusted_proxy_ip(peer):
        return peer[:128]

    # Walk from the nearest hop towards the client. This avoids trusting a
    # spoofed left-most value when a trusted proxy appends to an existing XFF.
    forwarded = [item.strip() for item in str(request.headers.get("x-forwarded-for") or "").split(",") if item.strip()]
    for candidate in reversed([*forwarded, peer]):
        try:
            normalized = str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
        if not _trusted_proxy_ip(normalized):
            return normalized[:128]
    return peer[:128]


def _trusted_proxy_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return False
    for item in os.getenv("AUTH_TRUSTED_PROXY_IPS", "").split(","):
        configured = item.strip()
        if not configured:
            continue
        try:
            if address in ipaddress.ip_network(configured, strict=False):
                return True
        except ValueError:
            logger.warning("ignoring invalid AUTH_TRUSTED_PROXY_IPS entry")
    return False


def auth_http_error(status_code: int, detail: str, code: str, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error": detail, "code": code}, headers=headers)


def organization_access_fields(
    user: dict[str, Any],
    membership: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return customer-org capabilities without changing platform privileges.

    The old V1 mock elevated a platform admin to a synthetic customer.  V2
    deliberately removes that shortcut: seller admins browse
    customers through /api/platform/organizations and are not members of any
    customer organization.
    """

    enabled = organization_demo_enabled()
    active_membership = membership if isinstance(membership, dict) and membership.get("status") == "active" else None
    role = str(active_membership.get("role") or "") if active_membership else None
    if role not in {"admin", "member"}:
        role = None
    organization_id = str(
        (active_membership or {}).get("organizationId")
        or (active_membership or {}).get("organization_id")
        or ""
    ) or None
    organization = (
        dict(active_membership.get("organization"))
        if isinstance((active_membership or {}).get("organization"), dict)
        else None
    )
    organization_name = str((organization or {}).get("name") or "")
    # Enterprise administrators own the complete customer-scoped workspace.
    can_view_usage = role == "admin"
    can_view_billing = role == "admin"
    return {
        "organizationDemoEnabled": enabled,
        "isPlatformAdmin": bool(user.get("isPlatformAdmin")),
        "organizationId": organization_id,
        # The customer name is safe display context for scoped boards. It lets
        # a customer admin identify their tenant without exposing the seller's
        # customer directory or enabling the master-data workspace.
        "organization": organization,
        "organizationName": organization_name or None,
        "organizationRole": role,
        "canViewOrganizationUsage": can_view_usage,
        "canViewOrganizationBilling": can_view_billing,
        "canSimulateOrganizationTopup": can_view_billing,
        "canManageOrganization": bool(enabled and role == "admin"),
        # Keep the explicit V2 capability separate from the legacy alias while
        # older browser bundles are still in circulation.
        "canManageCustomerOrganizations": bool(enabled and user.get("isPlatformAdmin")),
        "canManageCustomers": bool(enabled and user.get("isPlatformAdmin")),
    }


def organization_identity_status_fields(
    memberships: list[dict[str, Any]],
) -> dict[str, Any]:
    """Expose an inactive demo identity without restoring its permissions.

    The client needs a stable signal to render a useful pending/suspended
    state and avoid seller-only navigation.  This deliberately omits customer
    role and tenant id unless the membership is active, which keeps the
    authorization boundary server derived.
    """

    membership = next((item for item in memberships if isinstance(item, dict)), None)
    if membership is None:
        return {
            "isKnownDemoCustomerIdentity": False,
            "organizationAccessStatus": None,
        }
    organization = membership.get("organization")
    organization_status = (
        str(organization.get("status") or "")
        if isinstance(organization, dict)
        else str(membership.get("organizationStatus") or "")
    )
    member_status = str(membership.get("status") or "")
    if organization_status in {"archived", "suspended"}:
        access_status = "archived" if organization_status == "archived" else "organization_suspended"
    elif member_status in {"invited", "suspended"}:
        access_status = member_status
    else:
        access_status = "active"
    return {
        "isKnownDemoCustomerIdentity": True,
        "organizationAccessStatus": access_status,
    }


def _organization_membership_items(value: Any) -> list[dict[str, Any]]:
    """Normalize both the V2 multi-store list and its old single-item shape."""

    if isinstance(value, dict):
        items = value.get("items") or value.get("memberships") or value.get("organizations")
        if isinstance(items, list):
            values = [item for item in items if isinstance(item, dict)]
        else:
            values = [value]
    elif isinstance(value, list):
        values = [item for item in value if isinstance(item, dict)]
    else:
        return []
    normalized: list[dict[str, Any]] = []
    for item in values:
        member = item.get("member") if isinstance(item.get("member"), dict) else item
        organization = item.get("organization") if isinstance(item.get("organization"), dict) else {}
        normalized.append(
            {
                **member,
                "organizationId": item.get("organizationId") or item.get("organization_id") or member.get("organizationId"),
                "organization_id": item.get("organization_id") or item.get("organizationId") or member.get("organization_id"),
                "organization": organization,
                "organizationStatus": organization.get("status") if organization else item.get("organizationStatus"),
            }
        )
    return normalized


async def organization_memberships_for_user(user: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve active Mock memberships; password identities never inherit them."""

    if (
        not organization_demo_enabled()
        or user.get("authType") == "password"
        or is_platform_admin_email(str(user.get("email") or ""))
    ):
        return []
    email = str(user.get("email") or "")
    try:
        # V2 store: a user may have at most one effective Mock customer.  The
        # fallback preserves V1 tests until the store migration lands.
        try:
            result = await organization_store_call("resolve_members_by_email", email)
        except AttributeError:
            result = await organization_store_call("get_member_by_email", email)
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    return _organization_membership_items(result)


async def organization_access_fields_for_user(user: dict[str, Any]) -> dict[str, Any]:
    """Resolve bootstrap capabilities without granting platform admins membership."""

    if not organization_demo_enabled():
        return organization_access_fields(user)
    try:
        memberships = await organization_memberships_for_user(user)
    except HTTPException:
        logger.exception("failed to resolve organization demo membership")
        return organization_access_fields(user)
    active = next(
        (
            item
            for item in memberships
            if item.get("status") == "active" and item.get("organizationStatus", "active") == "active"
        ),
        None,
    )
    return {
        **organization_access_fields(user, active),
        **organization_identity_status_fields(memberships),
    }


async def organization_scope_fields_for_user(user: dict[str, Any]) -> dict[str, Any]:
    """Avoid changing legacy scope payloads while the demo remains disabled."""

    # A local password identity is deliberately a different principal from an
    # SSO-backed customer member.  Do not add demo capabilities to its legacy
    # scope response, even when the email happens to match a seeded member.
    if not organization_demo_enabled() or user.get("authType") == "password":
        return {}
    return await organization_access_fields_for_user(user)


async def organization_user(request: Request) -> dict[str, Any]:
    """Require an active customer membership derived from the server session."""

    if not organization_demo_enabled():
        raise HTTPException(status_code=404, detail="企业组织演示功能尚未启用")
    user = require_user(request)
    if user.get("authType") == "password":
        raise auth_http_error(403, "本地密码账号不能继承企业组织权限", "ORGANIZATION_SSO_REQUIRED")
    memberships = await organization_memberships_for_user(user)
    if any(item.get("status") != "active" or item.get("organizationStatus", "active") != "active" for item in memberships):
        raise auth_http_error(403, "当前企业成员尚未启用或已被暂停", "ORGANIZATION_MEMBER_INACTIVE")
    membership = next(
        (
            item
            for item in memberships
            if item.get("status") == "active" and item.get("organizationStatus", "active") == "active"
        ),
        None,
    )
    if not membership:
        # A seller-side operator is intentionally not a customer membership.
        raise auth_http_error(403, "当前账号不属于任何客户企业", "ORGANIZATION_MEMBERSHIP_REQUIRED")
    fields = organization_access_fields(user, membership)
    return {**user, "organizationMember": membership, **fields}


async def require_organization_usage_viewer(request: Request) -> dict[str, Any]:
    user = await organization_user(request)
    if not user.get("canViewOrganizationUsage"):
        raise auth_http_error(403, "当前成员没有企业全员或部门看板权限", "ORGANIZATION_USAGE_FORBIDDEN")
    return user


async def require_organization_billing_viewer(request: Request) -> dict[str, Any]:
    """Require an active customer administrator for Mock enterprise credit."""

    user = await organization_user(request)
    if not user.get("canViewOrganizationBilling"):
        raise auth_http_error(403, "当前成员没有企业额度查看权限", "ORGANIZATION_BILLING_FORBIDDEN")
    return user


async def require_organization_billing_topup_operator(request: Request) -> dict[str, Any]:
    """Keep simulated top-up authorization independent from analytics roles."""

    user = await require_organization_billing_viewer(request)
    if not user.get("canSimulateOrganizationTopup"):
        raise auth_http_error(403, "当前成员没有企业额度充值权限", "ORGANIZATION_TOPUP_FORBIDDEN")
    return user


async def require_organization_directory_viewer(request: Request) -> dict[str, Any]:
    """Allow the customer directory only to the company's analytics roles.

    A regular member (including a team leader whose role is still ``member``)
    must not learn the rest of the customer's member list through an otherwise
    harmless-looking organization endpoint.  Their dashboard contract is
    limited to personal usage or their assigned team.
    """

    user = await organization_user(request)
    if not user.get("canViewOrganizationUsage"):
        raise auth_http_error(403, "当前成员没有企业组织目录查看权限", "ORGANIZATION_DIRECTORY_FORBIDDEN")
    return user


async def require_organization_demo_manager(request: Request) -> dict[str, Any]:
    """Require an active customer administrator for scoped directory writes."""

    user = await organization_user(request)
    if not user.get("canManageOrganization"):
        raise auth_http_error(403, "当前成员没有企业组织管理权限", "ORGANIZATION_MANAGE_FORBIDDEN")
    return user


def organization_current_member(user: dict[str, Any]) -> dict[str, Any]:
    membership = user.get("organizationMember")
    return dict(membership) if isinstance(membership, dict) else {}


def organization_identifier(membership: dict[str, Any]) -> str:
    """Extract the server-side tenant identifier from a resolved membership."""

    identifier = str(membership.get("organizationId") or membership.get("organization_id") or "").strip()
    if not identifier:
        raise auth_http_error(403, "当前企业成员缺少有效的企业范围", "ORGANIZATION_SCOPE_INVALID")
    return identifier


async def organization_scoped_store_call(
    organization_id: str,
    method: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Call a V2 scoped facade, falling back to the keyword-compatible API."""

    store = organization_store()
    facade_factory = getattr(store, "for_organization", None)
    if callable(facade_factory):
        facade = await asyncio.to_thread(facade_factory, organization_id)
        function = getattr(facade, method)
        return await asyncio.to_thread(function, *args, **kwargs)
    function = getattr(store, method)
    try:
        return await asyncio.to_thread(function, *args, organization_id=organization_id, **kwargs)
    except TypeError:
        # Legacy V1 compatibility exists only for existing tests.  A V2 store
        # must expose either for_organization() or organization_id keywords.
        if organization_id == "org-demo":
            return await asyncio.to_thread(function, *args, **kwargs)
        raise


async def platform_organization_store_call(method: str, *args: Any, **kwargs: Any) -> Any:
    """Call a V2 seller-side store operation and keep failures typed."""

    return await organization_store_call(method, *args, **kwargs)


async def organization_usage_cache_key(
    organization_id: str,
    method: str,
    *,
    start_date: str,
    end_date: str,
    source: str,
    **filters: Any,
) -> str:
    """Build a server-owned cache key for one generated customer board.

    The organization is resolved from the session or a platform URL before
    this helper is reached.  Include the store's private revision so member
    and department changes cannot leave a stale board visible for the normal
    cache TTL.
    """

    try:
        revision = await organization_scoped_store_call(
            organization_id, "usage_cache_fingerprint"
        )
    except AttributeError:
        # Older test doubles have no revision API. They must still never share
        # a key across organizations or request shapes.
        revision = organization_id
    normalized_filters = ":".join(
        f"{key}={str(value or '').strip().casefold()}"
        for key, value in sorted(filters.items())
    )
    return (
        f"organization-usage:v2:{method}:{revision}:{start_date}:{end_date}:"
        f"{str(source or 'all').strip().casefold()}:{normalized_filters}"
    )


def invalidate_organization_usage_cache() -> None:
    """Clear generated Mock board data after a seller-side directory write."""

    organization_usage_cache.clear()


async def cached_mock_organization_usage_payload(
    method: str,
    organization_id: str,
    *,
    start_date: str,
    end_date: str,
    source: str,
    refresh: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Cache only company/department boards, never personal or team access."""

    cache_key = await organization_usage_cache_key(
        organization_id,
        method,
        start_date=start_date,
        end_date=end_date,
        source=source,
        **kwargs,
    )
    if not refresh:
        hit, value, ttl_seconds = organization_usage_cache.get(cache_key)
        if hit:
            payload = dict(value)
            payload["cache"] = {"hit": True, "ttlSeconds": ttl_seconds}
            return payload
    payload = await mock_usage_payload(
        method,
        organization_id,
        start_date=start_date,
        end_date=end_date,
        source=source,
        **kwargs,
    )
    stored = dict(payload)
    organization_usage_cache.set(
        cache_key, stored, env_int("ORGANIZATION_USAGE_CACHE_TTL_SECONDS", 120)
    )
    result = dict(stored)
    result["cache"] = {"hit": False, "ttlSeconds": 0}
    return result


async def require_platform_organization(request: Request, organization_id: str) -> dict[str, Any]:
    """Authorize a seller operator and resolve exactly the requested customer."""

    if not organization_demo_enabled():
        raise HTTPException(status_code=404, detail="企业组织演示功能尚未启用")
    user = require_platform_admin(request)
    try:
        organization = await platform_organization_store_call("get_organization", organization_id)
    except AttributeError:
        # The V1 store exposes no customer list and only keeps org-demo.
        if organization_id == "org-demo":
            organization = (await organization_store_call("get_current")).get("organization")
        else:
            organization = None
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    if not isinstance(organization, dict):
        raise auth_http_error(404, "未找到对应客户企业", "ORGANIZATION_NOT_FOUND")
    return {**user, "selectedOrganization": organization, "selectedOrganizationId": organization_id}


async def mock_usage_payload(
    method: str,
    organization_id: str,
    *,
    start_date: str,
    end_date: str,
    source: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Read deterministic Mock usage only; never fall through to an upstream.

    The V2 store owns these methods.  An unavailable method is deliberately a
    503 rather than a tempting legacy LiteLLM fallback, because the latter
    could reveal seller-wide data to a customer demo account.
    """

    method_names = [method]
    legacy_method = {
        "mock_personal_usage": "member_usage_payload",
        "mock_organization_usage": "usage_payload",
        "mock_department_usage": "department_usage_payload",
        "mock_team_usage": "team_usage_payload",
        "mock_team_member_usage": "team_member_usage_payload",
    }.get(method)
    if legacy_method:
        method_names.append(legacy_method)
    last_missing: AttributeError | None = None
    for candidate in method_names:
        try:
            payload = await organization_scoped_store_call(
                organization_id,
                candidate,
                start_date=start_date,
                end_date=end_date,
                source=source,
                **kwargs,
            )
            break
        except AttributeError as exc:
            last_missing = exc
        except OrganizationStoreError as exc:
            raise organization_store_error(exc) from exc
    else:
        logger.warning("organization demo Mock usage method is unavailable methods=%s", method_names)
        raise auth_http_error(503, "企业演示用量数据正在初始化，请稍后重试", "ORGANIZATION_USAGE_UNAVAILABLE") from last_missing
    if not isinstance(payload, dict):
        raise auth_http_error(503, "企业演示用量数据暂不可用", "ORGANIZATION_USAGE_UNAVAILABLE")
    return payload


async def is_demo_customer_user(app_user: dict[str, Any]) -> bool:
    """True only for an active customer membership, never seller operators."""

    if not organization_demo_enabled() or app_user.get("authType") == "password":
        return False
    try:
        memberships = await organization_memberships_for_user(app_user)
    except HTTPException:
        return False
    return any(
        item.get("status") == "active" and item.get("organizationStatus", "active") == "active"
        for item in memberships
    )


async def is_known_demo_customer_identity(app_user: dict[str, Any]) -> bool:
    """Recognize suspended and archived demo identities before upstream fallback.

    An inactive customer member must be denied rather than falling through to
    a same-email seller account or a real upstream query.
    """

    if not organization_demo_enabled() or app_user.get("authType") == "password":
        return False
    try:
        return bool(await organization_memberships_for_user(app_user))
    except HTTPException:
        return False


async def require_non_inactive_demo_identity(app_user: dict[str, Any]) -> None:
    """Fail closed before any non-Mock path can resolve a disabled customer.

    Active customer identities are handled by their Mock adapters. Invited,
    suspended, and archived customer identities must not fall through to a
    same-email platform account or an upstream usage lookup.
    """

    if await is_demo_customer_user(app_user):
        return
    if await is_known_demo_customer_identity(app_user):
        raise inactive_demo_customer_error()


def inactive_demo_customer_error() -> HTTPException:
    return auth_http_error(
        403,
        "当前企业成员尚未启用或所属客户已归档",
        "ORGANIZATION_MEMBER_INACTIVE",
    )


async def demo_team_scope_for_user(app_user: dict[str, Any]) -> dict[str, Any]:
    """Resolve a Mock team-leader scope without calling the upstream client."""

    memberships = await organization_memberships_for_user(app_user)
    membership = next(
        (
            item
            for item in memberships
            if item.get("status") == "active" and item.get("organizationStatus", "active") == "active"
        ),
        None,
    )
    if not membership:
        return {"isTeamLeader": False, "teamBoardStatus": "none", "team": None, "leaderTeams": []}
    organization_id = organization_identifier(membership)
    try:
        scope = await organization_scoped_store_call(
            organization_id, "team_scope_for_member", email=str(app_user.get("email") or "")
        )
    except (AttributeError, TypeError):
        return {"isTeamLeader": False, "teamBoardStatus": "none", "team": None, "leaderTeams": []}
    if not isinstance(scope, dict):
        return {"isTeamLeader": False, "teamBoardStatus": "none", "team": None, "leaderTeams": []}
    return {
        "isTeamLeader": bool(scope.get("isTeamLeader")),
        "teamBoardStatus": str(scope.get("teamBoardStatus") or "none"),
        "team": public_team(scope.get("team")),
        "leaderTeams": [team for team in (public_team(item) for item in scope.get("leaderTeams") or []) if team],
    }


async def known_demo_member_email(email: str) -> bool:
    """Check membership existence for the narrowly-scoped loopback dev login exception."""

    if not organization_demo_enabled():
        return False
    try:
        memberships = await organization_memberships_for_user({"email": email})
    except HTTPException:
        return False
    return bool(memberships)


def organization_store_error(exc: OrganizationStoreError) -> HTTPException:
    """Keep storage validation details out of the public API contract."""
    if isinstance(exc, OrganizationNotFoundError):
        return auth_http_error(404, "未找到对应的部门或成员", "ORGANIZATION_NOT_FOUND")
    if isinstance(exc, DuplicateMemberEmailError):
        return auth_http_error(409, "该邮箱已在企业成员列表中", "ORGANIZATION_MEMBER_EXISTS")
    if isinstance(exc, OrganizationPermissionError):
        return auth_http_error(403, "当前成员无权查看该团队范围", "ORGANIZATION_SCOPE_FORBIDDEN")
    if isinstance(exc, OrganizationConflictError):
        return auth_http_error(409, "当前组织状态不允许此操作，请先调整成员或管理员", "ORGANIZATION_CONFLICT")
    if isinstance(exc, OrganizationValidationError):
        return auth_http_error(400, "请检查部门或成员信息后重试", "ORGANIZATION_INVALID_INPUT")
    logger.warning("organization demo store error type=%s", exc.__class__.__name__)
    return auth_http_error(400, "企业组织数据处理失败，请检查输入后重试", "ORGANIZATION_STORE_ERROR")


def organization_token_store_error(exc: OrganizationStoreError) -> HTTPException:
    """Map token failures to token-specific copy without reusing member wording."""

    if isinstance(exc, OrganizationNotFoundError):
        return auth_http_error(404, "未找到对应的令牌或成员", "ORGANIZATION_TOKEN_NOT_FOUND")
    if isinstance(exc, OrganizationConflictError):
        return auth_http_error(409, "当前令牌状态不允许此操作", "ORGANIZATION_TOKEN_CONFLICT")
    if isinstance(exc, OrganizationValidationError):
        return auth_http_error(400, "请检查令牌名称、模型或额度后重试", "ORGANIZATION_TOKEN_INVALID_INPUT")
    logger.warning("organization token store error type=%s", exc.__class__.__name__)
    return auth_http_error(400, "令牌数据处理失败，请检查输入后重试", "ORGANIZATION_TOKEN_STORE_ERROR")


async def organization_token_model_catalog() -> tuple[str, ...]:
    """企业令牌可选模型目录：网关真实模型名，取不到时回落内置清单。

    企业组织本身仍是演示数据，只有这份目录来自真实上游。取目录失败不能让令牌管理
    整页不可用——``client()`` 在未配置 ``LITELLM_BASE_URL`` 时直接抛 500，而演示环境
    常常没有上游凭据，所以这里把所有失败都收敛成回落。
    """
    try:
        names = await client().organization_token_models()
    except HTTPException:
        # 未配置上游：演示环境的常态，不值得记 error。
        return ORGANIZATION_TOKEN_MODELS
    except Exception:
        logger.warning("organization token model catalog unavailable; using the built-in list")
        return ORGANIZATION_TOKEN_MODELS
    catalog = tuple(name for name in names if name)
    return catalog or ORGANIZATION_TOKEN_MODELS


def organization_token_model_options(catalog: tuple[str, ...]) -> list[dict[str, Any]]:
    """把原始模型名按脱敏展示名归组。

    同一个模型在网关上常有多条线路部署（``wangsu-claude-opus-5`` 与 ``claude-opus-5``
    的展示名相同），逐条列出会在弹窗里出现两个看不出区别的勾选框——而按产品边界又
    不能靠线路代号区分它们。所以一个展示名就是一个选项，勾选即授权它名下的全部线路：

    - ``displayName`` 是唯一可见文本；
    - ``names`` 是该选项覆盖的全部上游原始名，也是令牌实际存储的值。
    """
    grouped: dict[str, list[str]] = {}
    for name in catalog:
        label = model_display_name(name) or name
        grouped.setdefault(label, []).append(name)
    return [
        {"displayName": label, "names": names}
        for label, names in sorted(grouped.items())
    ]


def organization_token_list_payload(payload: dict[str, Any], catalog: tuple[str, ...]) -> dict[str, Any]:
    """给列表补上脱敏展示名。

    已签发令牌引用的模型可能已经不在当前目录里（上游下线、或早期演示数据），它仍然
    是历史事实要照原样展示，所以每条令牌都单独算展示名，而不是只依赖目录。同一令牌
    里属于同一模型的多条线路合并成一个标签，与创建弹窗的选项粒度保持一致。
    """
    items = payload.get("items")
    if isinstance(items, list):
        enriched = []
        for item in items:
            if not isinstance(item, dict):
                enriched.append(item)
                continue
            models = item.get("models") if isinstance(item.get("models"), list) else []
            labels: list[str] = []
            for model in models:
                label = model_display_name(str(model)) or str(model)
                if label not in labels:
                    labels.append(label)
            enriched.append({**item, "modelLabels": labels})
        payload = {**payload, "items": enriched}
    return {**payload, "availableModelOptions": organization_token_model_options(catalog)}


async def organization_current_payload(user: dict[str, Any]) -> dict[str, Any]:
    try:
        snapshot = await organization_scoped_store_call(
            organization_identifier(organization_current_member(user)), "organization_snapshot"
        )
    except AttributeError:
        # V1 compatibility for the existing one-company demo; V2 always uses
        # organization_snapshot so customer records cannot be mixed.
        snapshot = await organization_store_call("get_current")
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    return {
        **snapshot,
        # The UI uses this explicit context label when it reuses the existing
        # full-member and department boards for a customer-scoped session.
        "organizationName": str((snapshot.get("organization") or {}).get("name") or ""),
        "currentMember": organization_current_member(user),
        "capabilities": {"canManageOrganization": bool(user.get("canManageOrganization"))},
        **organization_access_fields(user, organization_current_member(user)),
    }


def local_auth_enabled() -> bool:
    return env_bool("AUTH_ENABLED", False) and env_bool("PASSWORD_LOGIN_ENABLED", False)


def local_signup_enabled() -> bool:
    return local_auth_enabled() and env_bool("PUBLIC_SIGNUP_ENABLED", False)


def allowed_signup_domains() -> list[str]:
    return sorted(
        {
            item.strip().lower().lstrip("@")
            for item in os.getenv("AUTH_ALLOWED_EMAIL_DOMAINS", "").split(",")
            if item.strip()
        }
    )


def auth_database_configured() -> bool:
    configured_path = os.getenv("AUTH_DATABASE_PATH", "").strip()
    configured_url = os.getenv("AUTH_DATABASE_URL", "").strip()
    if configured_path:
        return True
    if not configured_url:
        return False
    # AuthStore accepts SQLite URLs only. Treat an invalid value as not ready
    # rather than advertising a login form that will fail on first use.
    try:
        AuthStore._sqlite_path_from_url(configured_url)
    except AuthStoreConfigError:
        return False
    return True


def is_loopback_development() -> bool:
    parsed_base_url = urlparse(os.getenv("APP_BASE_URL", "").strip())
    return parsed_base_url.scheme.lower() == "http" and (parsed_base_url.hostname or "").lower() in LOOPBACK_HOSTS


def is_loopback_request_peer(request: Request) -> bool:
    """Return whether the transport peer is local, without trusting Host.

    ``testclient`` is emitted only by Starlette's in-process ASGI transport;
    a Uvicorn TCP peer is always an IP address. Keeping that compatibility
    lets development login enforce the actual peer in deployed processes.
    """

    peer = str(request.client.host if request.client else "").strip().lower()
    if peer == "testclient":
        return True
    try:
        return ipaddress.ip_address(peer).is_loopback
    except ValueError:
        return False


def smtp_host_is_private_relay() -> bool:
    """Return whether SMTP_HOST resolves only to private, non-routable addresses.

    A self-hosted MTA reached over the container network needs neither
    credentials nor TLS, because the hop never leaves the host. Resolving the
    name and requiring every answer to be private is what makes that safe: if
    the name resolves anywhere public, credentials or message bodies would
    cross the internet in the clear, so such a host is not treated as local.
    """
    host = os.getenv("SMTP_HOST", "").strip()
    if not host:
        return False
    try:
        candidates = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return False
    addresses = {str(entry[4][0]) for entry in candidates}
    if not addresses:
        return False
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return False
        if parsed.is_global or not (parsed.is_private or parsed.is_loopback or parsed.is_link_local):
            return False
    return True


def smtp_local_relay_enabled() -> bool:
    """Allow an unauthenticated, cleartext hop to a self-hosted MTA."""
    return env_bool("SMTP_LOCAL_RELAY", False) and smtp_host_is_private_relay()


def smtp_configured() -> bool:
    """Require an authenticated, encrypted SMTP transport for public email."""
    if is_loopback_development() and (env_bool("AUTH_EMAIL_DEBUG", False) or env_bool("DEV_LOGIN_ENABLED", False)):
        return True
    if not os.getenv("SMTP_HOST", "").strip() or not os.getenv("SMTP_FROM", "").strip():
        return False
    # A private-only relay carries no credentials, so the TLS and username
    # requirements below do not apply to it.
    if smtp_local_relay_enabled():
        return True
    if not os.getenv("SMTP_USERNAME", "").strip() or not os.getenv("SMTP_PASSWORD", ""):
        return False
    return env_bool("SMTP_SSL", False) != env_bool("SMTP_STARTTLS", True)


def bot_protection_opt_out() -> bool:
    """Allow internet-facing password auth without Turnstile, by explicit choice.

    Turnstile is the only bot protection in front of the public signup and
    verification-code endpoints. Opting out means scripted clients can burn the
    per-email and per-IP rate-limit budget and the outbound mail quota, so this
    must be set deliberately rather than defaulted on.
    """
    return env_bool("AUTH_ALLOW_NO_BOT_PROTECTION", False)


def password_login_unavailable_code() -> str:
    if not local_auth_enabled():
        return "AUTH_PASSWORD_LOGIN_DISABLED"
    if is_loopback_development():
        return ""
    parsed_base_url = urlparse(os.getenv("APP_BASE_URL", "").strip())
    if parsed_base_url.scheme.lower() != "https":
        return "AUTH_PASSWORD_LOGIN_HTTPS_REQUIRED"
    if not auth_database_configured():
        return "AUTH_DATABASE_NOT_CONFIGURED"
    if (not turnstile_enabled() or not turnstile_configured()) and not bot_protection_opt_out():
        return "AUTH_TURNSTILE_NOT_CONFIGURED"
    return ""


def password_login_configured() -> bool:
    """Return whether password login itself is ready, independent of email delivery."""
    return not password_login_unavailable_code()


def signup_unavailable_code() -> str:
    if not local_auth_enabled():
        return "AUTH_PASSWORD_LOGIN_DISABLED"
    if not local_signup_enabled():
        return "AUTH_SIGNUP_DISABLED"
    if password_code := password_login_unavailable_code():
        return password_code
    # Email verification is mandatory for any public registration endpoint,
    # including local test environments.
    if not env_bool("EMAIL_VERIFICATION_REQUIRED", True):
        return "AUTH_SIGNUP_EMAIL_VERIFICATION_REQUIRED"
    if not allowed_signup_domains():
        return "AUTH_SIGNUP_DOMAINS_NOT_CONFIGURED"
    if not smtp_configured():
        return "AUTH_SIGNUP_EMAIL_NOT_CONFIGURED"
    return ""


def password_recovery_unavailable_code() -> str:
    if password_code := password_login_unavailable_code():
        return password_code
    if not smtp_configured():
        return "AUTH_PASSWORD_EMAIL_NOT_CONFIGURED"
    return ""


def password_recovery_configured() -> bool:
    return not password_recovery_unavailable_code()


def password_recovery_enabled() -> bool:
    """Password recovery is available only to deployments with local passwords."""
    return local_auth_enabled()


def local_auth_unavailable_message(code: str) -> str:
    return {
        "AUTH_PASSWORD_LOGIN_DISABLED": "邮箱密码登录暂未开放。",
        "AUTH_PASSWORD_LOGIN_HTTPS_REQUIRED": "账号服务必须使用 HTTPS，邮箱登录与注册暂时不可用。",
        "AUTH_DATABASE_NOT_CONFIGURED": "账号服务尚未配置完成，邮箱登录与注册暂时不可用。",
        "AUTH_PASSWORD_EMAIL_NOT_CONFIGURED": "账号邮件服务尚未配置完成，密码找回暂时不可用。",
        "AUTH_TURNSTILE_NOT_CONFIGURED": "安全验证尚未配置完成，邮箱登录与注册暂时不可用。",
        "AUTH_SIGNUP_DISABLED": "邮箱注册暂未开放。",
        "AUTH_SIGNUP_DOMAINS_NOT_CONFIGURED": "注册邮箱范围尚未配置完成，暂时无法创建账号。",
        "AUTH_SIGNUP_EMAIL_VERIFICATION_REQUIRED": "生产注册必须启用邮箱验证。",
        "AUTH_SIGNUP_EMAIL_NOT_CONFIGURED": "注册邮件服务尚未配置完成，暂时无法发送验证码。",
    }.get(code, "")


def local_signup_ready() -> bool:
    # Explicit loopback debug delivery is considered ready by smtp_configured.
    return not signup_unavailable_code()


def auth_unavailable_status(code: str) -> int:
    return 403 if code.endswith("_DISABLED") else 503


def require_password_login_ready() -> None:
    if code := password_login_unavailable_code():
        raise auth_http_error(auth_unavailable_status(code), local_auth_unavailable_message(code), code)


def require_signup_ready() -> None:
    if code := signup_unavailable_code():
        raise auth_http_error(auth_unavailable_status(code), local_auth_unavailable_message(code), code)


def require_password_recovery_ready() -> None:
    if code := password_recovery_unavailable_code():
        raise auth_http_error(auth_unavailable_status(code), local_auth_unavailable_message(code), code)


def validate_public_signup_email(email: str) -> str:
    try:
        normalized = auth_store().normalize_email(email)
    except ValueError as exc:
        raise auth_http_error(400, "请输入有效邮箱", "AUTH_INVALID_EMAIL") from exc
    allowed_domains = set(allowed_signup_domains())
    if allowed_domains and normalized.rsplit("@", 1)[1] not in allowed_domains:
        raise auth_http_error(403, "当前邮箱域暂未开放注册", "AUTH_EMAIL_DOMAIN_NOT_ALLOWED")
    return normalized


def turnstile_enabled() -> bool:
    return env_bool("TURNSTILE_ENABLED", False)


def turnstile_configured() -> bool:
    return bool(os.getenv("TURNSTILE_SITE_KEY", "").strip() and os.getenv("TURNSTILE_SECRET_KEY", "").strip())


async def enforce_csrf(request: Request) -> None:
    if not verify_csrf_token(request):
        raise auth_http_error(403, "页面安全凭证已失效，请刷新后重试", "AUTH_CSRF_INVALID")
    origin = str(request.headers.get("origin") or "").strip()
    if not origin:
        return
    configured = os.getenv("APP_BASE_URL", "").strip()
    configured_origin = urlparse(configured)
    origin_url = urlparse(origin)
    if configured:
        same_origin = (
            origin_url.scheme.lower() == configured_origin.scheme.lower()
            and origin_url.netloc.lower() == configured_origin.netloc.lower()
        )
    else:
        same_origin = origin_url.netloc.lower() == str(request.headers.get("host") or "").lower()
    if not same_origin:
        raise auth_http_error(403, "请求来源不受信任", "AUTH_ORIGIN_INVALID")


async def verify_turnstile(request: Request, token: str) -> None:
    if not turnstile_enabled():
        return
    if not turnstile_configured():
        raise auth_http_error(503, "人机验证尚未正确配置，请联系管理员", "AUTH_TURNSTILE_MISCONFIGURED")
    if not token:
        raise auth_http_error(400, "请完成人机验证", "AUTH_TURNSTILE_REQUIRED")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0)) as http_client:
            response = await http_client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={
                    "secret": os.getenv("TURNSTILE_SECRET_KEY", "").strip(),
                    "response": token,
                    "remoteip": request_ip(request),
                },
            )
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("turnstile verification unavailable: %s", exc.__class__.__name__)
        raise auth_http_error(503, "人机验证服务暂时不可用，请稍后重试", "AUTH_TURNSTILE_UNAVAILABLE") from exc
    if not payload.get("success"):
        raise auth_http_error(400, "人机验证未通过，请重试", "AUTH_TURNSTILE_FAILED")


async def enforce_rate_limit(action: str, key: str, limit: int, window_seconds: int) -> None:
    result = await auth_store_call("check_rate_limit", action, key, limit, window_seconds)
    if result.get("limited"):
        retry_after = max(1, int(result.get("retryAfter") or window_seconds))
        raise auth_http_error(
            429,
            "操作过于频繁，请稍后重试",
            "AUTH_RATE_LIMITED",
            {"Retry-After": str(retry_after)},
        )


def send_auth_email_sync(recipient: str, subject: str, body: str) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    sender = os.getenv("SMTP_FROM", "").strip()
    if not host or not sender:
        if env_bool("AUTH_EMAIL_DEBUG", False) or env_bool("DEV_LOGIN_ENABLED", False):
            logger.info("auth email debug recipient=%s subject=%s body=%s", recipient, subject, body)
            return
        raise RuntimeError("邮件服务尚未配置")
    port = env_int("SMTP_PORT", 587)
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    smtp_ssl = env_bool("SMTP_SSL", False)
    smtp_starttls = env_bool("SMTP_STARTTLS", True)
    if smtp_local_relay_enabled():
        # The self-hosted MTA is reachable only on the private container
        # network and accepts no credentials, so skip TLS on this hop and let
        # the MTA negotiate STARTTLS with each recipient's server itself.
        smtp_ssl = False
        smtp_starttls = False
        username = ""
        password = ""
    if smtp_ssl and smtp_starttls:
        raise RuntimeError("SMTP_SSL 和 SMTP_STARTTLS 不能同时启用")
    if (username or password) and not (smtp_ssl or smtp_starttls):
        raise RuntimeError("SMTP 凭据必须通过 TLS 连接发送")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    if smtp_ssl:
        connection: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context())
    else:
        connection = smtplib.SMTP(host, port, timeout=15)
    try:
        if smtp_starttls and not smtp_ssl:
            connection.starttls(context=ssl.create_default_context())
        if username:
            connection.login(username, password)
        connection.send_message(message)
    finally:
        connection.quit()


async def send_auth_email(recipient: str, subject: str, body: str) -> None:
    try:
        await asyncio.to_thread(send_auth_email_sync, recipient, subject, body)
    except (OSError, RuntimeError, smtplib.SMTPException) as exc:
        logger.warning("auth email delivery failed: %s", exc.__class__.__name__)
        raise auth_http_error(503, "邮件暂时无法发送，请稍后重试", "AUTH_EMAIL_UNAVAILABLE") from exc


def upstream_user_owned_by_local_account(info: dict[str, Any], user: dict[str, Any], upstream_user_id: str) -> bool:
    """Require both stable identity metadata and email before reconciling a duplicate."""
    if not isinstance(info, dict):
        return False
    actual_id = str(info.get("user_id") or "").strip()
    actual_email = str(info.get("user_email") or info.get("sso_user_id") or "").strip().lower()
    metadata = info.get("metadata") if isinstance(info.get("metadata"), dict) else {}
    return bool(
        actual_id == upstream_user_id
        and actual_email == str(user.get("email") or "").strip().lower()
        and str(metadata.get("local_user_id") or "").strip() == upstream_user_id
        and str(metadata.get("created_via") or "").strip() == "ai-token-dashboard"
    )


async def auth_user_payload(user: dict[str, Any], *, refresh_entitlement: bool = False) -> dict[str, Any]:
    account = await auth_store_call("get_upstream_account", str(user["id"]), "primary")
    account_status = str((account or {}).get("status") or "provisioning")
    entitlement_status = "inactive"
    upstream_user_id = str((account or {}).get("upstream_user_id") or "")
    if account_status == "provisioned" and upstream_user_id:
        cache_key = f"local-entitlement:{upstream_user_id}"
        hit, cached_status, _ = local_entitlement_cache.get(cache_key)
        if hit and not refresh_entitlement:
            entitlement_status = str(cached_status)
        else:
            try:
                info = await client().user_info(upstream_user_id)
                models = info.get("models") if isinstance(info, dict) else None
                blocked = bool(info.get("blocked")) if isinstance(info, dict) else False
                clean_models = [str(item) for item in (models or []) if str(item) != "no-default-models"]
                # The dashboard provisions ``no-default-models`` for every
                # local signup. Treat a missing or empty list as unprovisioned
                # rather than inheriting LiteLLM's unrestricted-list semantics:
                # access is opened only by an explicit model grant.
                entitlement_status = "active" if not blocked and bool(clean_models) else "inactive"
                local_entitlement_cache.set(
                    cache_key,
                    entitlement_status,
                    max(5, env_int("AUTH_ENTITLEMENT_CACHE_TTL_SECONDS", 30)),
                )
            except (HTTPException, RuntimeError):
                # Entitlements are advisory UI state. A transient upstream lookup
                # must not invalidate an otherwise valid dashboard login.
                entitlement_status = "inactive"
    normalized = normalize_user(str(user["email"]), str(user.get("name") or ""))
    # Password accounts are intentionally separate from SSO identities, even
    # when they use the same email address. Do not inherit admin privileges.
    normalized["isAdmin"] = False
    normalized["isPlatformAdmin"] = False
    return {
        **normalized,
        "id": str(user["id"]),
        "authType": "password",
        "emailVerified": bool(user.get("email_verified") or user.get("emailVerified")),
        "authMethods": ["password"],
        "accountStatus": account_status,
        "entitlementStatus": entitlement_status,
    }


async def current_local_auth_user(request: Request) -> dict[str, Any] | None:
    token = get_server_session_token(request)
    if not token:
        return None
    session = await auth_store_call("get_session", token)
    if not session:
        clear_server_session(request)
        return None
    user = await auth_store_call("get_user", session["user_id"])
    if not user or str(user.get("status") or "active") != "active":
        await auth_store_call("revoke_session", token)
        clear_server_session(request)
        return None
    return user


async def create_local_session(request: Request, user: dict[str, Any]) -> tuple[dict[str, Any], str]:
    old_token = get_server_session_token(request)
    if old_token:
        await auth_store_call("revoke_session", old_token)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=session_cookie_max_age())
    session = await auth_store_call(
        "create_session",
        str(user["id"]),
        expires_at,
        request_ip(request),
        str(request.headers.get("user-agent") or "")[:512],
    )
    csrf_value = set_server_session(request, str(session["token"]))
    return await auth_user_payload(user), csrf_value


async def provision_local_user(user: dict[str, Any]) -> dict[str, Any]:
    user_id = str(user["id"])
    upstream_user_id = f"local-{user_id}"
    await auth_store_call("set_provisioning_status", user_id, "provisioning", "primary", upstream_user_id, "")
    try:
        created = await client().create_internal_user(upstream_user_id, str(user["email"]), str(user.get("name") or ""))
        actual_id = str(created.get("user_id") or upstream_user_id)
        account = await auth_store_call("set_provisioning_status", user_id, "provisioned", "primary", actual_id, "")
        await auth_store_call("complete_provisioning_jobs", user_id, "primary")
        return account
    except HTTPException as exc:
        # A retry may race a prior successful request. Reconcile duplicate-id
        # responses before placing the account into the retry queue.
        if exc.status_code in {400, 409}:
            try:
                existing = await client().user_info(upstream_user_id)
                if upstream_user_owned_by_local_account(existing, user, upstream_user_id):
                    actual_id = str(existing.get("user_id") or upstream_user_id)
                    account = await auth_store_call("set_provisioning_status", user_id, "provisioned", "primary", actual_id, "")
                    await auth_store_call("complete_provisioning_jobs", user_id, "primary")
                    return account
                logger.error("refusing to bind local account to mismatched upstream user user_id=%s", user_id)
            except HTTPException:
                pass
        await auth_store_call("set_provisioning_status", user_id, "provisioning_failed", "primary", upstream_user_id, str(exc.detail))
        await auth_store_call("enqueue_provisioning", user_id, "primary", str(exc.detail))
        logger.warning("local user provisioning deferred user_id=%s status=%s", user_id, exc.status_code)
        return await auth_store_call("get_upstream_account", user_id, "primary") or {}
    except Exception as exc:
        error = f"{exc.__class__.__name__}: {exc}"
        await auth_store_call("set_provisioning_status", user_id, "provisioning_failed", "primary", upstream_user_id, error)
        await auth_store_call("enqueue_provisioning", user_id, "primary", error)
        logger.exception("local user provisioning deferred after unexpected failure user_id=%s", user_id)
        return await auth_store_call("get_upstream_account", user_id, "primary") or {}


async def retry_local_provisioning(user: dict[str, Any]) -> dict[str, Any] | None:
    account = await auth_store_call("get_upstream_account", str(user["id"]), "primary")
    if not account or account.get("status") not in {"pending", "provisioning", "provisioning_failed", "failed"}:
        return account
    updated_at = str(account.get("updated_at") or account.get("updatedAt") or "")
    if updated_at:
        try:
            parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - parsed).total_seconds() < max(5, env_int("AUTH_PROVISIONING_RETRY_SECONDS", 30)):
                return account
        except ValueError:
            pass
    return await provision_local_user(user)


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
    return f"usage:v7:{email.strip().lower()}:{start_date}:{end_date}:{source or 'all'}"


def local_personal_usage_cache_key(user_id: str, start_date: str, end_date: str, source: str) -> str:
    return f"usage:local:v2:{user_id}:{start_date}:{end_date}:{source or 'all'}"


def admin_usage_cache_key(email: str, start_date: str, end_date: str, source: str, employee: str | None) -> str:
    return f"admin-usage:v5:{email.strip().lower()}:{start_date}:{end_date}:{source or 'all'}:{(employee or '').strip().lower()}"


def department_usage_cache_key(email: str, start_date: str, end_date: str, source: str, department: str | None) -> str:
    return f"department-usage:v6:{email.strip().lower()}:{start_date}:{end_date}:{source or 'all'}:{(department or '').strip().lower()}"


def team_auth_cache_key(email: str, name: str | None) -> str:
    return f"team-auth:v3:{email.strip().lower()}:{str(name or '').strip()}"


def team_scope_items(team: dict[str, Any]) -> list[dict[str, Any]]:
    scopes = team.get("teamScopes")
    if isinstance(scopes, list) and scopes:
        return [item for item in scopes if isinstance(item, dict) and item.get("backend") and item.get("id")]
    return [team]


def team_scope_fingerprint(team: dict[str, Any]) -> str:
    return ",".join(
        sorted(
            f"{item.get('backend')}:{str(item.get('id') or '').strip().casefold()}:{' '.join(str(item.get('name') or '').split()).casefold()}"
            for item in team_scope_items(team)
        )
    )


def team_usage_cache_key(email: str, team: dict[str, Any], start_date: str, end_date: str, source: str) -> str:
    return f"team-usage:v9:{email.strip().lower()}:{team_scope_fingerprint(team)}:{start_date}:{end_date}:{source or 'all'}"


def team_member_usage_cache_key(email: str, team: dict[str, Any], employee: str, start_date: str, end_date: str, source: str) -> str:
    return f"team-member-usage:v8:{email.strip().lower()}:{team_scope_fingerprint(team)}:{employee.strip().lower()}:{start_date}:{end_date}:{source or 'all'}"


def team_ref(team: dict[str, Any]) -> str:
    existing = str(team.get("teamRef") or "").strip()
    if existing:
        return existing
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
        # The organization id is safe scope context for the Mock team board;
        # it lets the browser label the selected customer without exposing a
        # mutable authorization handle.
        "organizationId": team.get("organizationId"),
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


def merge_team_member_usage_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge one member's normalized model rows without mixing call sources."""
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        model = normalize_model_display_name(row.get("model")) or "未知模型"
        source = str(row.get("source") or "其他")
        key = (str(row.get("date") or ""), source, model)
        current = grouped.get(key)
        if current is None:
            current = dict(row)
            current["source"] = source
            current["model"] = model
            current.update(empty_usage_totals())
            grouped[key] = current
        add_usage_totals(current, row)
    return sorted(grouped.values(), key=lambda item: (str(item.get("date") or ""), str(item.get("source") or ""), str(item.get("model") or "")))


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
    if app_user.get("id") and (
        app_user.get("accountStatus") != "provisioned"
        or app_user.get("entitlementStatus") != "active"
    ):
        rows: list[dict[str, Any]] = []
        return {
            "user": app_user,
            "startDate": start_date,
            "endDate": end_date,
            "source": source,
            "rows": rows,
            "summary": usage_summary(rows),
            "accountStatus": app_user.get("accountStatus", "provisioning"),
            "entitlementStatus": app_user.get("entitlementStatus", "inactive"),
            "mappingCache": {"hit": True, "ttlSeconds": 0},
            "cache": {"hit": False, "ttlSeconds": 0},
        }
    if app_user.get("id"):
        return await local_personal_usage_payload(app_user, start_date, end_date, source, refresh)
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


async def local_personal_usage_payload(
    app_user: dict[str, Any],
    start_date: str,
    end_date: str,
    source: str,
    refresh: bool = False,
) -> dict[str, Any]:
    """Read password-account usage only from its provisioned upstream identity."""
    local_user_id = str(app_user["id"])
    cache_key = local_personal_usage_cache_key(local_user_id, start_date, end_date, source)
    if not refresh:
        hit, value, ttl_seconds = personal_usage_cache.get(cache_key)
        if hit:
            payload = dict(value)
            payload["cache"] = {"hit": True, "ttlSeconds": ttl_seconds}
            return payload
    account = await auth_store_call("get_upstream_account", local_user_id, "primary")
    upstream_user_id = str((account or {}).get("upstream_user_id") or "")
    if not account or account.get("status") != "provisioned" or not upstream_user_id:
        raise auth_http_error(409, "账号仍在开通中，请稍后重试", "AUTH_PROVISIONING_PENDING")
    if refresh:
        raise manual_refresh_database_unavailable()
    rows = await client().usage_rows_for_user_ids([upstream_user_id], start_date, end_date, source)
    payload = {
        "user": app_user,
        "startDate": start_date,
        "endDate": end_date,
        "source": source,
        "rows": rows,
        "summary": usage_summary(rows),
        "mappingCache": {"hit": True, "ttlSeconds": 0},
    }
    personal_usage_cache.set(cache_key, payload, env_int("PERSONAL_USAGE_CACHE_TTL_SECONDS", 300))
    payload = dict(payload)
    payload["cache"] = {"hit": False, "ttlSeconds": 0}
    return payload


async def batched_person_usage_rows(
    emails: list[str],
    start_date: str,
    end_date: str,
    source: str,
    refresh: bool = False,
) -> dict[str, dict[str, Any]] | None:
    store = usage_store()
    if store is None:
        return None
    try:
        await store.connect()
        stored = await store.rows_by_employee_emails(emails, start_date, end_date, source, usage_backend_ids())
        if stored is not None:
            return stored
    except Exception:
        logger.exception("batched employee usage SQL query failed; falling back to upstream")
    return None


async def person_usage_rows(email: str, name: str | None, start_date: str, end_date: str, source: str, refresh: bool = False, extra_user_ids: list[str] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    stored = await batched_person_usage_rows([email], start_date, end_date, source, refresh)
    stored_item = stored.get(email.strip().lower()) if stored is not None else None
    if stored_item is not None:
        user_ids = list(dict.fromkeys([str(item).strip() for item in (stored_item.get("userIds") or []) if str(item).strip()] + [str(item).strip() for item in (extra_user_ids or []) if str(item).strip()]))
        return stored_item["rows"], user_ids
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
            logger.info("admin usage cache hit records=%s", len(payload.get("rows") or []))
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
            logger.info("department usage cache hit records=%s", len(payload.get("rows") or []))
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
            logger.info(
                "department usage sql refresh=%s connect_ms=%.0f query_ms=%.0f total_ms=%.0f backends=%s records=%s",
                refresh,
                (connected_at - db_started) * 1000,
                (queried_at - connected_at) * 1000,
                (queried_at - request_started) * 1000,
                len(stored.get("dataQuality", {}).get("backends") or []) if stored else 0,
                len(stored.get("rows") or []) if stored else 0,
            )
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
    # Password and enterprise accounts are intentionally not merged by email.
    # Local signups therefore never inherit a team-leader scope from SSO data.
    if app_user.get("id"):
        return {"isTeamLeader": False, "teamBoardStatus": "none", "team": None, "leaderTeams": [], "cache": {"hit": True, "ttlSeconds": 0}}
    cache_key = team_auth_cache_key(app_user["email"], app_user.get("name"))
    if not refresh:
        hit, value, ttl_seconds = team_auth_cache.get(cache_key)
        if hit:
            scope = dict(value)
            scope["cache"] = {"hit": True, "ttlSeconds": ttl_seconds}
            logger.info(
                "team auth cache hit teams=%s scopes=%s",
                len(scope.get("leaderTeams") or []),
                sum(len(team_scope_items(team)) for team in scope.get("leaderTeams") or [] if isinstance(team, dict)),
            )
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
            logger.info(
                "team usage cache hit backends=%s scopes=%s records=%s",
                len({item.get("backend") for item in team_scope_items(team)}),
                len(team_scope_items(team)),
                len(payload.get("rows") or []),
            )
            return payload
    store = usage_store()
    payload = None
    if store is not None:
        try:
            db_started = asyncio.get_running_loop().time()
            await store.connect()
            connected_at = asyncio.get_running_loop().time()
            await prepare_usage_refresh(start_date, end_date, refresh)
            payload = await store.team_rows(team_scope_items(team), start_date, end_date, source)
            queried_at = asyncio.get_running_loop().time()
            logger.info(
                "team usage sql refresh=%s connect_ms=%.0f query_ms=%.0f total_ms=%.0f backends=%s scopes=%s records=%s",
                refresh,
                (connected_at - db_started) * 1000,
                (queried_at - connected_at) * 1000,
                (queried_at - request_started) * 1000,
                len({item.get("backend") for item in team_scope_items(team)}),
                len(team_scope_items(team)),
                len(payload.get("rows") or []) if payload else 0,
            )
            if payload is not None:
                payload = dict(payload)
                last_synced = payload.pop("lastSyncedAt", None)
                payload["dataFreshness"] = usage_data_freshness(last_synced, start_date, end_date)
                payload.setdefault("dataQuality", {})["backends"] = [item.get("backend") for item in team_scope_items(team)]
        except Exception:
            logger.exception("local team usage query failed; falling back to upstream")
            payload = None
    if payload is None:
        if refresh:
            raise manual_refresh_database_unavailable()
        try:
            payload = dict(await client().team_usage_rows(team_scope_items(team), start_date, end_date, source))
        except TypeError:
            # Keep test doubles and older clients compatible with the legacy signature.
            payload = dict(await client().team_usage_rows(str(team["backend"]), str(team["id"]), start_date, end_date, source))
    if enrich_member_rankings:
        # team_rows/team_usage_rows already aggregate through team membership. Do not
        # replace that scoped result with an email-wide personal usage query.
        payload["employees"] = payload.get("employees") or []
        payload.setdefault("dataQuality", {})["rankingSource"] = "team_membership_database" if store is not None and payload.get("dataQuality", {}).get("summarySource") == "database" else "team_membership_upstream"
        payload["dataQuality"]["rankingScope"] = "selected_team"
        payload["dataQuality"]["memberIdentityMatch"] = "user_id_or_email"
    payload["team"] = public_team_from_payload(team, payload.get("team"))
    # 摘要和排行来自同一批结果；首个请求即写缓存，后续排行请求不重复查库。
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
            logger.info(
                "team member usage cache hit backends=%s scopes=%s records=%s",
                len({item.get("backend") for item in team_scope_items(team)}),
                len(team_scope_items(team)),
                len(payload.get("rows") or []),
            )
            return payload

    stored_payload: dict[str, Any] | None = None
    store = usage_store()
    if store is not None:
        try:
            db_started = asyncio.get_running_loop().time()
            await store.connect()
            connected_at = asyncio.get_running_loop().time()
            await prepare_usage_refresh(start_date, end_date, refresh)
            stored_payload = await store.team_member_rows(team_scope_items(team), employee, start_date, end_date, source)
            queried_at = asyncio.get_running_loop().time()
            logger.info(
                "team member usage sql refresh=%s connect_ms=%.0f query_ms=%.0f total_ms=%.0f backends=%s scopes=%s records=%s",
                refresh,
                (connected_at - db_started) * 1000,
                (queried_at - connected_at) * 1000,
                (queried_at - request_started) * 1000,
                len({item.get("backend") for item in team_scope_items(team)}),
                len(team_scope_items(team)),
                len(stored_payload.get("rows") or []) if stored_payload else 0,
            )
        except Exception:
            logger.exception("local team member usage query failed")
    if stored_payload is not None:
        rows = stored_payload.get("rows") or []
        selected_employee = stored_payload.get("employee") or {}
        if not selected_employee:
            raise HTTPException(status_code=404, detail="未找到该团队成员")
        team_payload = {"team": stored_payload.get("team") or {}, "dataQuality": stored_payload.get("dataQuality") or {}}
    else:
        if refresh:
            raise manual_refresh_database_unavailable()
        team_payload = await team_usage_payload(app_user, start_date, end_date, source, False, team_ref_value, enrich_member_rankings=False)
        selected_employee = find_team_employee(team_payload, employee)
        selected_values = employee_match_values(selected_employee)
        rows = [row for row in team_payload.get("rows") or [] if employee_match_values(row) & selected_values]
    rows = merge_team_member_usage_rows(rows)
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
        "dataQuality": stored_payload.get("dataQuality") if stored_payload is not None else team_payload.get("dataQuality", {}),
    }
    if stored_payload is not None:
        payload["dataFreshness"] = usage_data_freshness(stored_payload.get("lastSyncedAt"), start_date, end_date)
    elif team_payload.get("dataFreshness"):
        payload["dataFreshness"] = team_payload["dataFreshness"]
    team_member_usage_cache.set(cache_key, payload, env_int("TEAM_MEMBER_USAGE_CACHE_TTL_SECONDS", 300))
    payload = dict(payload)
    payload["cache"] = {"hit": False, "ttlSeconds": 0}
    return payload


async def current_upstream_user(request: Request, refresh: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    app_user = require_user(request)
    if await is_demo_customer_user(app_user):
        # Customer demo identities deliberately have no upstream account.  Any
        # endpoint that still tries to use one must fail closed instead of
        # resolving a same-email seller account.
        raise auth_http_error(403, "企业演示账号不提供个人令牌或上游账户操作", "ORGANIZATION_UPSTREAM_FORBIDDEN")
    await require_non_inactive_demo_identity(app_user)
    local_user_id = str(app_user.get("id") or "")
    if local_user_id:
        local_user = await auth_store_call("get_user", local_user_id)
        if not local_user or str(local_user.get("status") or "active") != "active":
            raise auth_http_error(401, "本地登录已失效，请重新登录", "AUTH_LOGIN_REQUIRED")
        app_user = await auth_user_payload(local_user, refresh_entitlement=True)
        account = await auth_store_call("get_upstream_account", local_user_id, "primary")
        if not account or account.get("status") != "provisioned" or not account.get("upstream_user_id"):
            raise auth_http_error(409, "账号仍在开通中，请稍后重试", "AUTH_PROVISIONING_PENDING")
        upstream_user_id = str(account["upstream_user_id"])
        return app_user, {
            "user_id": upstream_user_id,
            "user_email": app_user["email"],
            "user_alias": app_user.get("name") or app_user["email"],
            "matched_user_ids": [upstream_user_id],
            "matched_accounts": [
                {"backend": "primary", "source": "通衢 API", "user_id": upstream_user_id, "account_id": upstream_user_id, "matchSources": ["local_mapping"]}
            ],
            "matched_sources": {upstream_user_id: ["local_mapping"]},
            "matched_by": "local_mapping",
        }
    upstream, _ = await cached_resolve_user(app_user["email"], app_user.get("name"), refresh)
    return app_user, upstream


def local_account_is_active(app_user: dict[str, Any]) -> bool:
    return bool(app_user.get("id")) and app_user.get("accountStatus") == "provisioned" and app_user.get("entitlementStatus") == "active"


def require_active_local_entitlement(app_user: dict[str, Any]) -> None:
    if app_user.get("id") and not local_account_is_active(app_user):
        raise auth_http_error(403, "当前账号尚未获得模型和额度权限", "AUTH_ENTITLEMENT_INACTIVE")


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


class VerificationRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    purpose: Literal["signup"] = "signup"
    turnstileToken: str = Field(default="", max_length=4096)


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=128)
    verificationCode: str = Field(default="", max_length=12)
    turnstileToken: str = Field(default="", max_length=4096)


class PasswordLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)
    turnstileToken: str = Field(default="", max_length=4096)


class ForgotPasswordRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    turnstileToken: str = Field(default="", max_length=4096)


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=16, max_length=512)
    newPassword: str = Field(min_length=8, max_length=128)


class ChangePasswordRequest(BaseModel):
    currentPassword: str = Field(min_length=1, max_length=128)
    newPassword: str = Field(min_length=8, max_length=128)


class RedeemRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.strip().upper()


class CreateTopupOrderRequest(BaseModel):
    amount: float = Field(gt=0)
    paymentMethod: Literal["alipay", "wxpay"] = "alipay"
    channel: Literal["epay", "manual_qr"] = "epay"


class SubmitManualPaymentRequest(BaseModel):
    payerNote: str = Field(default="", max_length=500)

    @field_validator("payerNote")
    @classmethod
    def strip_note(cls, value: str) -> str:
        return value.strip()


class ReviewOrderRequest(BaseModel):
    note: str = Field(default="", max_length=500)

    @field_validator("note")
    @classmethod
    def strip_note(cls, value: str) -> str:
        return value.strip()


class CreateRedemptionRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=200)
    amount: float = Field(gt=0)
    name: str = Field(default="", max_length=100)
    expiresInDays: int = Field(default=0, ge=0, le=3650)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return value.strip()


class OrganizationDepartmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return value.strip()


class OrganizationMemberCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    departmentId: str = Field(min_length=1, max_length=128)
    role: Literal["admin", "member"] = "member"

    @field_validator("name", "email", "departmentId")
    @classmethod
    def strip_member_text(cls, value: str) -> str:
        return value.strip()


class OrganizationMemberUpdateRequest(BaseModel):
    """All member fields are optional, but at least one must be supplied."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    departmentId: str | None = Field(default=None, min_length=1, max_length=128)
    role: Literal["admin", "member"] | None = None
    status: Literal["invited", "pending", "active", "suspended"] | None = None

    @field_validator("name", "departmentId")
    @classmethod
    def strip_optional_member_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else value


class OrganizationEmptyRequest(BaseModel):
    """Validate body-less organization mutations without accepting tenant IDs."""

    model_config = ConfigDict(extra="forbid")


class PlatformOrganizationRequest(BaseModel):
    """Seller-side customer organization create/update request."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        return value.strip()


class PlatformOrganizationCreateRequest(PlatformOrganizationRequest):
    """Create one customer and its first active customer administrator atomically."""

    adminName: str = Field(min_length=1, max_length=120)
    adminEmail: str = Field(min_length=3, max_length=254)

    @field_validator("adminName", "adminEmail")
    @classmethod
    def strip_admin_text(cls, value: str) -> str:
        return value.strip()


class OrganizationBillingTopupRequest(BaseModel):
    """Strict payload for a local-only Mock enterprise credit simulation."""

    model_config = ConfigDict(extra="forbid")

    amountUsd: Decimal

    @field_validator("amountUsd", mode="before")
    @classmethod
    def require_numeric_amount_usd(cls, value: Any) -> Any:
        # A quoted amount looks harmless but weakens the public JSON contract.
        # Keep this Mock endpoint strict so it can later map to a real API.
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValueError("amountUsd must be a number")
        return value

    @field_validator("amountUsd")
    @classmethod
    def validate_amount_usd(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("amountUsd must be a finite number")
        if value.as_tuple().exponent < -2:
            raise ValueError("amountUsd must have at most two decimal places")
        if value < Decimal("1.00") or value > Decimal("100000.00"):
            raise ValueError("amountUsd must be between 1.00 and 100000.00")
        return value.quantize(Decimal("0.01"))


class OrganizationTokenCreateRequest(BaseModel):
    """Strict payload for issuing one customer-scoped demo access token."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    models: list[str] = Field(min_length=1, max_length=MAX_MODELS_PER_TOKEN)
    memberId: str = Field(default="", max_length=128)
    duration: Literal["never", "30d", "90d"] = "never"
    dailyBudgetUsd: Decimal = DEFAULT_TOKEN_DAILY_BUDGET_USD

    @field_validator("name", "memberId")
    @classmethod
    def strip_token_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("models")
    @classmethod
    def validate_token_models(cls, value: list[str]) -> list[str]:
        selected: list[str] = []
        for item in value:
            name = item.strip()
            if not name:
                raise ValueError("models must not contain empty entries")
            if name not in selected:
                selected.append(name)
        if not selected:
            raise ValueError("select at least one model")
        return selected

    @field_validator("dailyBudgetUsd", mode="before")
    @classmethod
    def require_numeric_daily_budget(cls, value: Any) -> Any:
        # Match the top-up contract: a quoted number would silently widen the
        # public JSON shape this Mock endpoint must keep stable.
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValueError("dailyBudgetUsd must be a number")
        return value

    @field_validator("dailyBudgetUsd")
    @classmethod
    def validate_daily_budget(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("dailyBudgetUsd must be a finite number")
        if value.as_tuple().exponent < -2:
            raise ValueError("dailyBudgetUsd must have at most two decimal places")
        if value < MIN_TOKEN_DAILY_BUDGET_USD or value > MAX_TOKEN_DAILY_BUDGET_USD:
            raise ValueError("dailyBudgetUsd must be between 1.00 and 5000.00")
        return value.quantize(Decimal("0.01"))


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
    app_user = require_user(request)
    if await is_demo_customer_user(app_user):
        raise auth_http_error(403, "企业演示账号不提供上游调试查询", "ORGANIZATION_UPSTREAM_FORBIDDEN")
    await require_non_inactive_demo_identity(app_user)
    if app_user.get("id"):
        raise auth_http_error(403, "本地密码账号不能使用调试接口", "AUTH_LOCAL_DEBUG_UNAVAILABLE")
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
    if await is_demo_customer_user(app_user):
        raise auth_http_error(403, "企业演示账号不提供上游调试查询", "ORGANIZATION_UPSTREAM_FORBIDDEN")
    if app_user.get("id"):
        raise auth_http_error(403, "本地密码账号不能使用调试接口", "AUTH_LOCAL_DEBUG_UNAVAILABLE")
    await require_non_inactive_demo_identity(app_user)
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
    password_unavailable = password_login_unavailable_code()
    signup_unavailable = signup_unavailable_code()
    recovery_unavailable = password_recovery_unavailable_code()
    result["authReadiness"] = {
        "passwordLogin": {
            "enabled": local_auth_enabled(),
            "available": not password_unavailable,
            "unavailableCode": password_unavailable,
        },
        "publicSignup": {
            "enabled": local_signup_enabled(),
            "available": not signup_unavailable,
            "unavailableCode": signup_unavailable,
        },
        "passwordRecovery": {
            "enabled": password_recovery_enabled(),
            "available": not recovery_unavailable,
            "unavailableCode": recovery_unavailable,
        },
    }
    if local_auth_enabled() and auth_database_configured():
        result["authDatabase"] = await auth_store_call("health")
        if result["authDatabase"].get("status") == "error":
            result["status"] = "degraded"
    elif local_auth_enabled():
        result["authDatabase"] = {"enabled": True, "status": "error", "configured": False}
        result["status"] = "degraded"
    else:
        result["authDatabase"] = {"enabled": False, "status": "disabled"}
    store = usage_store()
    if store is not None:
        result["usageDatabase"] = await store.health()
        if result["usageDatabase"].get("status") in {"error", "disconnected"}:
            result["status"] = "degraded"
    else:
        result["usageDatabase"] = {"enabled": False, "connected": False, "status": "disabled"}
    if result["usageSync"].get("status") in {"error", "failed", "partial"}:
        result["status"] = "degraded"
    billing_ledger = billing_store()
    if billing_ledger is None:
        result["billing"] = {"enabled": False, "status": "disabled"}
    elif billing_ledger.pool is None:
        result["billing"] = {"enabled": True, "connected": False, "status": "error"}
        result["status"] = "degraded"
    else:
        try:
            pending_sync = await billing_ledger.pending_sync_count()
            pending_review = await billing_ledger.pending_review_count()
        except Exception:
            logger.exception("billing health check failed")
            result["billing"] = {"enabled": True, "connected": False, "status": "error"}
            result["status"] = "degraded"
        else:
            # 待同步积压意味着钱已收到但上游额度没写上，必须让运维看见。
            # 待确认只是等人工处理，不算异常，仅暴露数量。
            result["billing"] = {
                "enabled": True,
                "connected": True,
                "status": "degraded" if pending_sync else "ok",
                "pendingSyncCount": pending_sync,
                "pendingReviewCount": pending_review,
            }
            if pending_sync:
                result["status"] = "degraded"
    if turnstile_enabled() and not turnstile_configured():
        result["turnstile"] = {"enabled": True, "configured": False, "status": "error"}
        result["status"] = "degraded"
    else:
        result["turnstile"] = {"enabled": turnstile_enabled(), "configured": turnstile_configured(), "status": "ok" if turnstile_enabled() else "disabled"}
    if local_auth_enabled() and password_unavailable:
        result["status"] = "degraded"
    if local_signup_enabled() and signup_unavailable:
        result["status"] = "degraded"
    return result


@app.get("/api/auth/config")
async def auth_config() -> dict[str, Any]:
    password_unavailable_code = password_login_unavailable_code()
    signup_unavailable = signup_unavailable_code()
    recovery_unavailable_code = password_recovery_unavailable_code()
    password_ready = password_login_configured()
    signup_ready = local_signup_ready()
    recovery_ready = password_recovery_configured()
    return {
        "devLoginEnabled": env_bool("DEV_LOGIN_ENABLED", False),
        "oidcConfigured": oidc_configured(),
        "providerName": safe_provider_name(),
        "allowedEmailDomain": allowed_email_domain(),
        "passwordLoginEnabled": local_auth_enabled(),
        "passwordLoginConfigured": password_ready,
        "passwordLoginAvailable": password_ready,
        "passwordLoginUnavailableCode": password_unavailable_code,
        "passwordLoginUnavailableReason": local_auth_unavailable_message(password_unavailable_code),
        "publicSignupEnabled": local_signup_enabled(),
        "publicSignupConfigured": signup_ready,
        "publicSignupAvailable": signup_ready,
        "publicSignupUnavailableCode": signup_unavailable,
        "publicSignupUnavailableReason": local_auth_unavailable_message(signup_unavailable),
        "passwordRecoveryEnabled": password_recovery_enabled(),
        "passwordRecoveryConfigured": recovery_ready,
        "passwordRecoveryAvailable": recovery_ready,
        "passwordRecoveryUnavailableCode": recovery_unavailable_code,
        "passwordRecoveryUnavailableReason": local_auth_unavailable_message(recovery_unavailable_code),
        "allowedSignupDomains": allowed_signup_domains(),
        "emailVerificationRequired": env_bool("EMAIL_VERIFICATION_REQUIRED", True),
        "turnstileEnabled": turnstile_enabled(),
        "turnstileConfigured": turnstile_configured(),
        "turnstileSiteKey": os.getenv("TURNSTILE_SITE_KEY", "").strip() if turnstile_configured() else "",
    }


@app.get("/api/auth/csrf")
async def auth_csrf(request: Request) -> dict[str, str]:
    return {"csrfToken": csrf_token(request)}


@app.get("/api/auth/me")
async def auth_me(request: Request) -> dict[str, Any]:
    local_user = await current_local_auth_user(request)
    if local_user:
        await retry_local_provisioning(local_user)
    user = await auth_user_payload(local_user, refresh_entitlement=True) if local_user else dict(require_user(request))
    if await is_demo_customer_user(user):
        user.update(await demo_team_scope_for_user(user))
    else:
        user.update({"isTeamLeader": False, "teamBoardStatus": "loading", "team": None, "leaderTeams": []})
    user.update(await organization_access_fields_for_user(user))
    user["csrfToken"] = csrf_token(request)
    return user


@app.post("/api/auth/verification/request")
async def request_verification(data: VerificationRequest, request: Request) -> dict[str, Any]:
    require_signup_ready()
    await enforce_csrf(request)
    await verify_turnstile(request, data.turnstileToken)
    email = validate_public_signup_email(data.email)
    await enforce_rate_limit("verification_email", email, 5, 3600)
    await enforce_rate_limit("verification_ip", request_ip(request), 20, 3600)
    user = await auth_store_call("get_user_by_email", email)
    expires_in = max(60, env_int("AUTH_VERIFICATION_TTL_SECONDS", 600))
    if user is None:
        code = generate_numeric_code(6)
        subject = "通衢 API 验证码"
        body = f"您的验证码是：{code}\n\n验证码 {expires_in // 60} 分钟内有效，请勿转发给他人。"
        await send_auth_email(email, subject, body)
        # Persist only after delivery succeeds so an SMTP failure does not
        # consume the user's previously delivered, still-valid code.
        await auth_store_call(
            "create_verification_code",
            email,
            data.purpose,
            hash_auth_token(code),
            datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            5,
        )
    await auth_store_call(
        "record_audit_event",
        "verification_requested",
        str(user["id"]) if user else None,
        email,
        request_ip(request),
        True,
        {"purpose": data.purpose},
    )
    return {"ok": True, "message": "如果邮箱可以使用，验证码已发送", "expiresIn": expires_in}


@app.post("/api/auth/register")
async def register(data: RegisterRequest, request: Request) -> dict[str, Any]:
    require_signup_ready()
    await enforce_csrf(request)
    await verify_turnstile(request, data.turnstileToken)
    email = validate_public_signup_email(data.email)
    await enforce_rate_limit("register_ip", request_ip(request), 10, 3600)
    if await auth_store_call("get_user_by_email", email):
        raise auth_http_error(409, "该邮箱已注册，请直接登录", "AUTH_EMAIL_EXISTS")
    try:
        password_hash = await asyncio.to_thread(hash_password, data.password)
        if env_bool("EMAIL_VERIFICATION_REQUIRED", True):
            user = await auth_store_call(
                "create_user_from_verification",
                email,
                data.name.strip(),
                password_hash,
                "signup",
                hash_auth_token(data.verificationCode.strip()),
                status="active",
            )
            if user is None:
                raise auth_http_error(400, "验证码无效或已过期", "AUTH_CODE_INVALID")
        else:
            user = await auth_store_call(
                "create_user",
                email,
                data.name.strip(),
                password_hash,
                True,
                "active",
            )
    except DuplicateEmailError as exc:
        raise auth_http_error(409, "该邮箱已注册，请直接登录", "AUTH_EMAIL_EXISTS") from exc
    except ValueError as exc:
        raise auth_http_error(400, str(exc), "AUTH_INVALID_INPUT") from exc
    await auth_store_call("set_provisioning_status", str(user["id"]), "provisioning", "primary", f"local-{user['id']}", "")
    account = await provision_local_user(user)
    await auth_store_call("record_audit_event", "registered", str(user["id"]), email, request_ip(request), True, {"accountStatus": account.get("status")})
    payload = await auth_user_payload(user)
    return {"ok": True, "user": payload, "message": "注册成功，请登录"}


@app.post("/api/auth/login")
async def password_login(data: PasswordLoginRequest, request: Request) -> dict[str, Any]:
    require_password_login_ready()
    await enforce_csrf(request)
    await verify_turnstile(request, data.turnstileToken)
    try:
        email = auth_store().normalize_email(data.email)
    except ValueError as exc:
        raise auth_http_error(401, "邮箱或密码不正确", "AUTH_INVALID_CREDENTIALS") from exc
    await enforce_rate_limit("login_email", email, 10, 60)
    await enforce_rate_limit("login_ip", request_ip(request), 30, 60)
    user = await auth_store_call("get_user_by_email", email)
    valid = bool(user and await asyncio.to_thread(verify_password, data.password, str(user.get("password_hash") or user.get("passwordHash") or "")))
    if not valid:
        await auth_store_call("record_audit_event", "login_failed", str(user["id"]) if user else None, email, request_ip(request), False, {})
        raise auth_http_error(401, "邮箱或密码不正确", "AUTH_INVALID_CREDENTIALS")
    if str(user.get("status") or "active") != "active":
        raise auth_http_error(403, "账号当前不可登录，请联系管理员", "AUTH_ACCOUNT_SUSPENDED")
    if env_bool("EMAIL_VERIFICATION_REQUIRED", True) and not bool(user.get("email_verified") or user.get("emailVerified")):
        raise auth_http_error(403, "请先完成邮箱验证", "AUTH_EMAIL_UNVERIFIED")
    stored_hash = str(user.get("password_hash") or user.get("passwordHash") or "")
    if password_needs_rehash(stored_hash):
        await auth_store_call("update_password", str(user["id"]), await asyncio.to_thread(hash_password, data.password))
        user = await auth_store_call("get_user", str(user["id"])) or user
    await auth_store_call("touch_last_login", str(user["id"]))
    payload, csrf_value = await create_local_session(request, user)
    await auth_store_call("record_audit_event", "login_success", str(user["id"]), email, request_ip(request), True, {})
    return {"user": payload, "csrfToken": csrf_value}


@app.post("/api/auth/password/forgot")
async def forgot_password(data: ForgotPasswordRequest, request: Request) -> dict[str, Any]:
    require_password_recovery_ready()
    await enforce_csrf(request)
    await verify_turnstile(request, data.turnstileToken)
    try:
        email = auth_store().normalize_email(data.email)
    except ValueError:
        email = None
    rate_limit_key = email or f"invalid:{hashlib.sha256(data.email.strip().casefold().encode('utf-8')).hexdigest()}"
    await enforce_rate_limit("forgot_email", rate_limit_key, 5, 3600)
    await enforce_rate_limit("forgot_ip", request_ip(request), 20, 3600)
    user = await auth_store_call("get_user_by_email", email) if email else None
    if user:
        token = generate_auth_token(32)
        expires_in = max(300, env_int("AUTH_PASSWORD_RESET_TTL_SECONDS", 1800))
        pending_token = await auth_store_call(
            "create_password_reset_token",
            str(user["id"]),
            hash_auth_token(token),
            datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            activate=False,
        )
        base_url = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
        reset_url = f"{base_url}/?reset_token={token}"
        try:
            await send_auth_email(email, "重置通衢 API 密码", f"请在 {expires_in // 60} 分钟内打开以下链接设置新密码：\n\n{reset_url}\n\n如非本人操作，请忽略本邮件。")
            activated = await auth_store_call("activate_password_reset_token", str(pending_token["id"]), str(user["id"]))
            if not activated:
                logger.error("password reset token activation failed user_id=%s", user["id"])
        except HTTPException:
            # Password recovery always returns the same public response. Email
            # delivery failures must not invalidate a previously delivered link.
            await auth_store_call("delete_password_reset_token", str(pending_token["id"]))
            logger.warning("password reset email delivery deferred")
    await auth_store_call("record_audit_event", "password_reset_requested", str(user["id"]) if user else None, email, request_ip(request), True, {})
    return {"ok": True, "message": "如果账号存在，重置邮件已发送"}


@app.post("/api/auth/password/reset")
async def reset_password(data: ResetPasswordRequest, request: Request) -> dict[str, Any]:
    require_password_login_ready()
    await enforce_csrf(request)
    try:
        new_hash = await asyncio.to_thread(hash_password, data.newPassword)
    except ValueError as exc:
        raise auth_http_error(400, str(exc), "AUTH_PASSWORD_INVALID") from exc
    user_id = await auth_store_call("reset_password_with_token", hash_auth_token(data.token), new_hash)
    if not user_id:
        raise auth_http_error(400, "重置链接无效或已过期", "AUTH_RESET_TOKEN_INVALID")
    clear_server_session(request)
    await auth_store_call("record_audit_event", "password_reset_completed", user_id, None, request_ip(request), True, {})
    return {"ok": True, "message": "密码已重置，请重新登录"}


@app.post("/api/auth/password/change")
async def change_password(data: ChangePasswordRequest, request: Request) -> dict[str, Any]:
    require_password_login_ready()
    await enforce_csrf(request)
    user = await current_local_auth_user(request)
    if not user:
        raise auth_http_error(401, "请先使用密码账号登录", "AUTH_LOGIN_REQUIRED")
    current_hash = str(user.get("password_hash") or user.get("passwordHash") or "")
    if not await asyncio.to_thread(verify_password, data.currentPassword, current_hash):
        raise auth_http_error(400, "当前密码不正确", "AUTH_INVALID_CURRENT_PASSWORD")
    try:
        new_hash = await asyncio.to_thread(hash_password, data.newPassword)
    except ValueError as exc:
        raise auth_http_error(400, str(exc), "AUTH_PASSWORD_INVALID") from exc
    user_id = str(user["id"])
    await auth_store_call("update_password", user_id, new_hash)
    await auth_store_call("revoke_user_sessions", user_id)
    updated = await auth_store_call("get_user", user_id) or user
    payload, csrf_value = await create_local_session(request, updated)
    await auth_store_call("record_audit_event", "password_changed", user_id, str(user["email"]), request_ip(request), True, {})
    return {"ok": True, "user": payload, "csrfToken": csrf_value}


def self_service_billing_available(user: dict[str, Any]) -> bool:
    """Whether the sidebar top-up destination applies to this identity.

    Deliberately I/O free: it answers only the navigation-visibility question
    that :func:`billing_identity` would otherwise answer at the cost of an
    upstream ``user_info`` round trip.  Balance and orders stay on
    ``/api/me/billing``, which the client calls when the view is opened.

    The checks mirror ``billing_identity``'s own gates: the feature must be
    configured, the ledger connected, and the caller must be a local account.
    An SSO employee uses a department budget and never self-serves top-ups.
    """

    if not billing.billing_enabled():
        return False
    store = billing_store()
    if store is None or store.pool is None:
        return False
    return bool(user.get("id"))


@app.get("/api/auth/scope")
async def auth_scope(request: Request) -> dict[str, Any]:
    user = require_user(request)
    if organization_demo_enabled() and user.get("isPlatformAdmin"):
        # The seller's local customer-console demo is fully side-effect free.
        # Do not wait on the legacy upstream team resolver merely to bootstrap
        # a platform administrator who has no customer membership or team
        # scope in this mode.
        return {
            "isTeamLeader": False,
            "teamBoardStatus": "none",
            "team": None,
            "leaderTeams": [],
            **(await organization_scope_fields_for_user(user)),
            "billingAvailable": self_service_billing_available(user),
        }
    if await is_demo_customer_user(user):
        # A demo customer settles through its enterprise credit contract, so the
        # personal top-up destination stays hidden for them by construction.
        return {
            **(await demo_team_scope_for_user(user)),
            **(await organization_scope_fields_for_user(user)),
            "billingAvailable": False,
        }
    # An invited, suspended, or archived Mock identity must never fall through
    # to the legacy upstream team resolver.  It is a known customer identity
    # whose access is deliberately inactive.
    await require_non_inactive_demo_identity(user)
    if user.get("id"):
        return {
            "isTeamLeader": False,
            "teamBoardStatus": "none",
            "team": None,
            "leaderTeams": [],
            **(await organization_scope_fields_for_user(user)),
            "billingAvailable": self_service_billing_available(user),
        }
    started = asyncio.get_running_loop().time()
    scope = await team_scope_for_user(user)
    payload = {
        "isTeamLeader": bool(scope.get("isTeamLeader")),
        "teamBoardStatus": scope.get("teamBoardStatus", "none"),
        "team": public_team(scope.get("team")),
        "leaderTeams": [team for team in (public_team(item) for item in scope.get("leaderTeams") or []) if team],
        **(await organization_scope_fields_for_user(user)),
        "billingAvailable": self_service_billing_available(user),
    }
    logger.info("auth scope resolved email=%s cache=%s duration_ms=%.0f", user.get("email"), scope.get("cache", {}).get("hit"), (asyncio.get_running_loop().time() - started) * 1000)
    return payload


@app.post("/api/auth/dev-login")
async def dev_login(request: Request) -> dict[str, Any]:
    if not env_bool("DEV_LOGIN_ENABLED", False):
        raise HTTPException(status_code=403, detail="开发登录未启用，请使用企业统一认证")
    app_base_url = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").strip()
    app_host = (urlparse(app_base_url).hostname or "").lower()
    request_host_name = (request.url.hostname or "").lower()
    if (
        urlparse(app_base_url).scheme.lower() == "https"
        or app_host not in LOOPBACK_HOSTS
        or request_host_name not in LOOPBACK_HOSTS | {"testserver"}
        or not is_loopback_request_peer(request)
    ):
        raise HTTPException(status_code=403, detail="开发登录仅允许在本机开发环境使用")
    await enforce_csrf(request)
    payload = await request.json()
    email = str(payload.get("email", "")).strip()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="请输入有效的企业邮箱")
    # The controlled Mock includes customer domains that intentionally differ
    # from the seller's normal SSO allow-list.  It is only usable from a local
    # loopback dev-login request and only for a seeded/created demo member.
    normalized_email = email.lower()
    try:
        email = validate_company_email(email)
    except HTTPException:
        if not (organization_demo_enabled() and await known_demo_member_email(normalized_email)):
            raise
        email = normalized_email
    user = normalize_user(email)
    token = get_server_session_token(request)
    if token:
        await auth_store_call("revoke_session", token)
        clear_server_session(request)
    request.session[SESSION_USER_KEY] = user
    return {
        **user,
        **await organization_access_fields_for_user(user),
        "csrfToken": csrf_token(request),
    }


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
        server_token = get_server_session_token(request)
        if server_token:
            await auth_store_call("revoke_session", server_token)
            clear_server_session(request)
        request.session[SESSION_USER_KEY] = user
        csrf_token(request)
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
    await enforce_csrf(request)
    token = get_server_session_token(request)
    if token:
        await auth_store_call("revoke_session", token)
    clear_server_session(request)
    return {"ok": True}


@app.get("/api/organization/current")
async def organization_current(request: Request) -> dict[str, Any]:
    user = await require_organization_directory_viewer(request)
    return await organization_current_payload(user)


@app.get("/api/organization/current/members")
async def organization_members(
    request: Request,
    search: str = Query("", max_length=120),
    keyword: str = Query("", max_length=120),
    departmentId: str = Query("", max_length=128),
    role: str = Query("", max_length=16),
    status: str = Query("", max_length=16),
    page: int = Query(1, ge=1, le=100000),
    pageSize: int = Query(50, ge=1, le=100),
) -> dict[str, Any]:
    user = await require_organization_directory_viewer(request)
    # ``pending`` is the UI wording for a member whose invitation is waiting.
    normalized_status = "invited" if status == "pending" else status
    try:
        return await organization_scoped_store_call(
            organization_identifier(organization_current_member(user)),
            "list_members",
            keyword=search or keyword,
            department_id=departmentId,
            role=role,
            status=normalized_status,
            page=page,
            page_size=pageSize,
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc


@app.get("/api/organization/current/usage")
async def organization_current_usage(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    source: str = Query("all"),
    employee: str | None = Query(None, max_length=320),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    """Return Mock full-member usage for the authenticated customer only."""

    user = await require_organization_usage_viewer(request)
    if not start_date or not end_date:
        start_date, end_date = default_date_range()
    organization_id = organization_identifier(organization_current_member(user))
    payload = await cached_mock_organization_usage_payload(
        "mock_organization_usage",
        organization_id,
        start_date=start_date,
        end_date=end_date,
        source=source,
        employee=(employee or "").strip(),
        refresh=refresh,
    )
    return {
        "organization": {"id": organization_id},
        "startDate": start_date,
        "endDate": end_date,
        "source": source,
        "employee": (employee or "").strip(),
        **payload,
    }


@app.get("/api/organization/current/departments/usage")
async def organization_current_department_usage(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    source: str = Query("all"),
    department: str | None = Query(None, max_length=128),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    """Return Mock department usage scoped to the authenticated customer."""

    user = await require_organization_usage_viewer(request)
    if not start_date or not end_date:
        start_date, end_date = default_date_range()
    organization_id = organization_identifier(organization_current_member(user))
    payload = await cached_mock_organization_usage_payload(
        "mock_department_usage",
        organization_id,
        start_date=start_date,
        end_date=end_date,
        source=source,
        department=(department or "").strip(),
        refresh=refresh,
    )
    return {
        "organization": {"id": organization_id},
        "startDate": start_date,
        "endDate": end_date,
        "source": source,
        "department": (department or "").strip(),
        **payload,
    }


@app.get("/api/organization/current/billing")
async def organization_current_billing(
    request: Request,
    page: int = Query(1, ge=1, le=100000),
    pageSize: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Return a customer administrator's isolated Mock enterprise credit balance."""

    user = await require_organization_billing_viewer(request)
    organization_id = organization_identifier(organization_current_member(user))
    try:
        return await organization_scoped_store_call(
            organization_id,
            "billing_payload",
            page=page,
            page_size=pageSize,
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc


@app.post("/api/organization/current/billing/topups")
async def organization_current_billing_topup(
    data: OrganizationBillingTopupRequest,
    request: Request,
    page: int = Query(1, ge=1, le=100000),
    pageSize: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Credit only the session-derived customer balance; never take payment."""

    await enforce_csrf(request)
    user = await require_organization_billing_topup_operator(request)
    organization_id = organization_identifier(organization_current_member(user))
    try:
        result = await organization_scoped_store_call(
            organization_id,
            "simulate_billing_topup",
            data.amountUsd,
            operator=str(user.get("name") or user.get("email") or "Customer administrator"),
            operator_email=str(user.get("email") or ""),
            page=page,
            page_size=pageSize,
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    return {"ok": True, **result}


@app.post("/api/organization/current/departments")
async def organization_create_department(data: OrganizationDepartmentRequest, request: Request) -> dict[str, Any]:
    await enforce_csrf(request)
    user = await require_organization_demo_manager(request)
    try:
        department = await organization_scoped_store_call(
            organization_identifier(organization_current_member(user)), "create_department", data.name
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    invalidate_organization_usage_cache()
    return {"department": department}


@app.patch("/api/organization/current/departments/{department_id}")
async def organization_update_department(
    department_id: str, data: OrganizationDepartmentRequest, request: Request
) -> dict[str, Any]:
    await enforce_csrf(request)
    user = await require_organization_demo_manager(request)
    try:
        department = await organization_scoped_store_call(
            organization_identifier(organization_current_member(user)), "update_department", department_id, data.name
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    invalidate_organization_usage_cache()
    return {"department": department}


@app.post("/api/organization/current/departments/{department_id}/archive")
async def organization_archive_department(
    department_id: str,
    request: Request,
    _data: OrganizationEmptyRequest | None = None,
) -> dict[str, Any]:
    await enforce_csrf(request)
    user = await require_organization_demo_manager(request)
    try:
        department = await organization_scoped_store_call(
            organization_identifier(organization_current_member(user)), "archive_department", department_id
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    invalidate_organization_usage_cache()
    return {"department": department}


@app.post("/api/organization/current/members")
async def organization_create_member(data: OrganizationMemberCreateRequest, request: Request) -> dict[str, Any]:
    await enforce_csrf(request)
    user = await require_organization_demo_manager(request)
    try:
        member = await organization_scoped_store_call(
            organization_identifier(organization_current_member(user)),
            "create_member",
            data.name,
            data.email,
            data.departmentId,
            data.role,
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    invalidate_organization_usage_cache()
    return {"member": member}


@app.patch("/api/organization/current/members/{member_id}")
async def organization_update_member(
    member_id: str, data: OrganizationMemberUpdateRequest, request: Request
) -> dict[str, Any]:
    await enforce_csrf(request)
    user = await require_organization_demo_manager(request)
    fields = data.model_fields_set
    if not fields:
        raise auth_http_error(400, "请至少填写一项需要更新的成员信息", "ORGANIZATION_INVALID_INPUT")
    updates: dict[str, Any] = {}
    if "name" in fields:
        updates["name"] = data.name
    if "departmentId" in fields:
        updates["department_id"] = data.departmentId
    if "role" in fields:
        updates["role"] = data.role
    if "status" in fields:
        updates["status"] = "invited" if data.status == "pending" else data.status
    try:
        member = await organization_scoped_store_call(
            organization_identifier(organization_current_member(user)), "update_member", member_id, **updates
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    invalidate_organization_usage_cache()
    return {"member": member}


@app.get("/api/organization/current/tokens")
async def organization_current_tokens(
    request: Request,
    search: str = Query("", max_length=120),
    status: str = Query("", max_length=16),
    memberId: str = Query("", max_length=128),
    page: int = Query(1, ge=1, le=100000),
    pageSize: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """List the authenticated customer's own tokens, masked values only."""

    user = await require_organization_demo_manager(request)
    catalog = await organization_token_model_catalog()
    try:
        payload = await organization_scoped_store_call(
            organization_identifier(organization_current_member(user)),
            "list_tokens",
            keyword=search,
            status=status,
            member_id=memberId,
            page=page,
            page_size=pageSize,
            available_models=catalog,
        )
    except OrganizationStoreError as exc:
        raise organization_token_store_error(exc) from exc
    return organization_token_list_payload(payload, catalog)


@app.post("/api/organization/current/tokens")
async def organization_create_token(
    data: OrganizationTokenCreateRequest, request: Request
) -> JSONResponse:
    """Issue one token for the session-derived customer and reveal it once."""

    await enforce_csrf(request)
    user = await require_organization_demo_manager(request)
    catalog = await organization_token_model_catalog()
    try:
        result = await organization_scoped_store_call(
            organization_identifier(organization_current_member(user)),
            "create_token",
            data.name,
            data.models,
            member_id=data.memberId,
            duration=data.duration,
            daily_budget_usd=data.dailyBudgetUsd,
            available_models=catalog,
        )
    except OrganizationStoreError as exc:
        raise organization_token_store_error(exc) from exc
    # The plaintext value exists only in this response body, so it must never
    # be stored by a shared cache or an intermediate proxy.
    return JSONResponse(
        {"ok": True, **result},
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/organization/current/tokens/{token_id}/revoke")
async def organization_revoke_token(
    token_id: str,
    request: Request,
    _data: OrganizationEmptyRequest | None = None,
) -> dict[str, Any]:
    """Disable one of the authenticated customer's tokens."""

    await enforce_csrf(request)
    user = await require_organization_demo_manager(request)
    try:
        token = await organization_scoped_store_call(
            organization_identifier(organization_current_member(user)),
            "revoke_token",
            token_id,
        )
    except OrganizationStoreError as exc:
        raise organization_token_store_error(exc) from exc
    return {"ok": True, "token": token}


@app.get("/api/platform/organizations")
async def platform_organizations(
    request: Request,
    search: str = Query("", max_length=120),
    status: str = Query("", max_length=16),
    page: int = Query(1, ge=1, le=100000),
    pageSize: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """List seller-managed customer organizations, never customer memberships."""

    if not organization_demo_enabled():
        raise HTTPException(status_code=404, detail="客户企业演示功能尚未启用")
    require_platform_admin(request)
    try:
        return await platform_organization_store_call(
            "list_organizations",
            keyword=search,
            status=status,
            page=page,
            page_size=pageSize,
            include_archived=True,
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc


@app.post("/api/platform/organizations")
async def platform_create_organization(
    data: PlatformOrganizationCreateRequest,
    request: Request,
) -> dict[str, Any]:
    """Create a Mock customer with its default department and first administrator."""

    if not organization_demo_enabled():
        raise HTTPException(status_code=404, detail="客户企业演示功能尚未启用")
    await enforce_csrf(request)
    require_platform_admin(request)
    try:
        created = await platform_organization_store_call(
            "create_organization_with_admin",
            data.name,
            data.adminName,
            data.adminEmail,
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    invalidate_organization_usage_cache()
    return created


@app.get("/api/platform/organizations/{organization_id}")
async def platform_organization_detail(organization_id: str, request: Request) -> dict[str, Any]:
    selected = await require_platform_organization(request, organization_id)
    try:
        return await organization_scoped_store_call(
            str(selected["selectedOrganizationId"]), "organization_snapshot"
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc


@app.patch("/api/platform/organizations/{organization_id}")
async def platform_update_organization(
    organization_id: str,
    data: PlatformOrganizationRequest,
    request: Request,
) -> dict[str, Any]:
    await enforce_csrf(request)
    await require_platform_organization(request, organization_id)
    try:
        organization = await platform_organization_store_call(
            "update_organization", organization_id, data.name
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    invalidate_organization_usage_cache()
    return {"organization": organization}


@app.post("/api/platform/organizations/{organization_id}/archive")
async def platform_archive_organization(
    organization_id: str,
    request: Request,
    _data: OrganizationEmptyRequest | None = None,
) -> dict[str, Any]:
    await enforce_csrf(request)
    await require_platform_organization(request, organization_id)
    try:
        organization = await platform_organization_store_call("archive_organization", organization_id)
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    invalidate_organization_usage_cache()
    return {"organization": organization}


@app.get("/api/platform/organizations/{organization_id}/members")
async def platform_organization_members(
    organization_id: str,
    request: Request,
    search: str = Query("", max_length=120),
    keyword: str = Query("", max_length=120),
    departmentId: str = Query("", max_length=128),
    role: str = Query("", max_length=16),
    status: str = Query("", max_length=16),
    page: int = Query(1, ge=1, le=100000),
    pageSize: int = Query(50, ge=1, le=100),
) -> dict[str, Any]:
    await require_platform_organization(request, organization_id)
    try:
        return await organization_scoped_store_call(
            organization_id,
            "list_members",
            keyword=search or keyword,
            department_id=departmentId,
            role=role,
            status="invited" if status == "pending" else status,
            page=page,
            page_size=pageSize,
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc


@app.post("/api/platform/organizations/{organization_id}/departments")
async def platform_create_department(
    organization_id: str, data: OrganizationDepartmentRequest, request: Request
) -> dict[str, Any]:
    await enforce_csrf(request)
    await require_platform_organization(request, organization_id)
    try:
        department = await organization_scoped_store_call(organization_id, "create_department", data.name)
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    invalidate_organization_usage_cache()
    return {"department": department}


@app.patch("/api/platform/organizations/{organization_id}/departments/{department_id}")
async def platform_update_department(
    organization_id: str,
    department_id: str,
    data: OrganizationDepartmentRequest,
    request: Request,
) -> dict[str, Any]:
    await enforce_csrf(request)
    await require_platform_organization(request, organization_id)
    try:
        department = await organization_scoped_store_call(
            organization_id, "update_department", department_id, data.name
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    invalidate_organization_usage_cache()
    return {"department": department}


@app.post("/api/platform/organizations/{organization_id}/departments/{department_id}/archive")
async def platform_archive_department(
    organization_id: str,
    department_id: str,
    request: Request,
    _data: OrganizationEmptyRequest | None = None,
) -> dict[str, Any]:
    await enforce_csrf(request)
    await require_platform_organization(request, organization_id)
    try:
        department = await organization_scoped_store_call(
            organization_id, "archive_department", department_id
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    invalidate_organization_usage_cache()
    return {"department": department}


@app.post("/api/platform/organizations/{organization_id}/members")
async def platform_create_member(
    organization_id: str, data: OrganizationMemberCreateRequest, request: Request
) -> dict[str, Any]:
    await enforce_csrf(request)
    await require_platform_organization(request, organization_id)
    try:
        member = await organization_scoped_store_call(
            organization_id, "create_member", data.name, data.email, data.departmentId, data.role
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    invalidate_organization_usage_cache()
    return {"member": member}


@app.patch("/api/platform/organizations/{organization_id}/members/{member_id}")
async def platform_update_member(
    organization_id: str,
    member_id: str,
    data: OrganizationMemberUpdateRequest,
    request: Request,
) -> dict[str, Any]:
    await enforce_csrf(request)
    await require_platform_organization(request, organization_id)
    fields = data.model_fields_set
    if not fields:
        raise auth_http_error(400, "请至少填写一项需要更新的成员信息", "ORGANIZATION_INVALID_INPUT")
    updates: dict[str, Any] = {}
    if "name" in fields:
        updates["name"] = data.name
    if "departmentId" in fields:
        updates["department_id"] = data.departmentId
    if "role" in fields:
        updates["role"] = data.role
    if "status" in fields:
        updates["status"] = "invited" if data.status == "pending" else data.status
    try:
        member = await organization_scoped_store_call(
            organization_id, "update_member", member_id, **updates
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    invalidate_organization_usage_cache()
    return {"member": member}


@app.get("/api/platform/organizations/{organization_id}/usage")
async def platform_organization_usage(
    organization_id: str,
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    source: str = Query("all"),
    employee: str | None = Query(None, max_length=320),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    await require_platform_organization(request, organization_id)
    if not start_date or not end_date:
        start_date, end_date = default_date_range()
    payload = await cached_mock_organization_usage_payload(
        "mock_organization_usage",
        organization_id,
        start_date=start_date,
        end_date=end_date,
        source=source,
        employee=(employee or "").strip(),
        refresh=refresh,
    )
    return {"startDate": start_date, "endDate": end_date, "source": source, "employee": (employee or "").strip(), **payload}


@app.get("/api/platform/organizations/{organization_id}/billing")
async def platform_organization_billing(
    organization_id: str,
    request: Request,
    page: int = Query(1, ge=1, le=100000),
    pageSize: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Allow the seller to read one customer's Mock credit history only."""

    selected = await require_platform_organization(request, organization_id)
    try:
        return await organization_scoped_store_call(
            str(selected["selectedOrganizationId"]),
            "billing_payload",
            page=page,
            page_size=pageSize,
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc


@app.get("/api/platform/organizations/{organization_id}/tokens")
async def platform_organization_tokens(
    organization_id: str,
    request: Request,
    search: str = Query("", max_length=120),
    status: str = Query("", max_length=16),
    memberId: str = Query("", max_length=128),
    page: int = Query(1, ge=1, le=100000),
    pageSize: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Let the seller read one customer's token list for support only.

    There is deliberately no seller-side create or revoke route: issuing a
    customer's token stays with that customer's own administrator.
    """

    selected = await require_platform_organization(request, organization_id)
    catalog = await organization_token_model_catalog()
    try:
        payload = await organization_scoped_store_call(
            str(selected["selectedOrganizationId"]),
            "list_tokens",
            keyword=search,
            status=status,
            member_id=memberId,
            page=page,
            page_size=pageSize,
            available_models=catalog,
        )
    except OrganizationStoreError as exc:
        raise organization_token_store_error(exc) from exc
    return organization_token_list_payload(payload, catalog)


@app.get("/api/platform/organizations/{organization_id}/departments/usage")
async def platform_organization_department_usage(
    organization_id: str,
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    source: str = Query("all"),
    department: str | None = Query(None, max_length=128),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    await require_platform_organization(request, organization_id)
    if not start_date or not end_date:
        start_date, end_date = default_date_range()
    payload = await cached_mock_organization_usage_payload(
        "mock_department_usage",
        organization_id,
        start_date=start_date,
        end_date=end_date,
        source=source,
        department=(department or "").strip(),
        refresh=refresh,
    )
    return {"startDate": start_date, "endDate": end_date, "source": source, "department": (department or "").strip(), **payload}


@app.post("/api/platform/organizations/demo/reset")
async def platform_reset_organization_demo(
    request: Request, _data: OrganizationEmptyRequest | None = None
) -> dict[str, Any]:
    if not organization_demo_enabled():
        raise HTTPException(status_code=404, detail="客户企业演示功能尚未启用")
    await enforce_csrf(request)
    require_platform_admin(request)
    try:
        await platform_organization_store_call("reset_all")
        invalidate_organization_usage_cache()
        return {"ok": True, **await platform_organization_store_call("list_organizations")}
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc


@app.get("/api/me/usage")
async def my_usage(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    source: str = Query("all"),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    app_user = require_user(request)
    if await is_demo_customer_user(app_user):
        if not start_date or not end_date:
            start_date, end_date = default_date_range()
        memberships = await organization_memberships_for_user(app_user)
        membership = next(
            item
            for item in memberships
            if item.get("status") == "active" and item.get("organizationStatus", "active") == "active"
        )
        organization_id = organization_identifier(membership)
        payload = await mock_usage_payload(
            "mock_personal_usage",
            organization_id,
            start_date=start_date,
            end_date=end_date,
            source=source,
            email=str(app_user.get("email") or ""),
        )
        return {"user": app_user, "startDate": start_date, "endDate": end_date, "source": source, **payload}
    await require_non_inactive_demo_identity(app_user)
    if app_user.get("id"):
        local_user = await auth_store_call("get_user", str(app_user["id"]))
        if not local_user:
            raise auth_http_error(401, "本地登录已失效，请重新登录", "AUTH_LOGIN_REQUIRED")
        app_user = await auth_user_payload(local_user, refresh_entitlement=True)
        require_active_local_entitlement(app_user)
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
    include_member_rankings: bool = Query(True),
) -> dict[str, Any]:
    app_user = require_user(request)
    if await is_demo_customer_user(app_user):
        if not start_date or not end_date:
            start_date, end_date = default_date_range()
        memberships = await organization_memberships_for_user(app_user)
        membership = next(
            item
            for item in memberships
            if item.get("status") == "active" and item.get("organizationStatus", "active") == "active"
        )
        organization_id = organization_identifier(membership)
        payload = await mock_usage_payload(
            "mock_team_usage",
            organization_id,
            start_date=start_date,
            end_date=end_date,
            source=source,
            email=str(app_user.get("email") or ""),
            team_ref=(team_ref or "").strip(),
            include_member_rankings=include_member_rankings,
        )
        return {"leader": {"email": app_user["email"], "name": app_user["name"]}, "startDate": start_date, "endDate": end_date, "source": source, "teamRef": payload.get("teamRef") or team_ref or "", **payload}
    await require_non_inactive_demo_identity(app_user)
    if app_user.get("id"):
        # Password and enterprise SSO accounts remain separate identities.
        raise auth_http_error(403, "当前账号还没有团队负责人权限", "AUTH_TEAM_SCOPE_UNAVAILABLE")
    if not start_date or not end_date:
        start_date, end_date = default_date_range()
    payload = await team_usage_payload(app_user, start_date, end_date, source, refresh, team_ref, include_member_rankings)
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
    if await is_demo_customer_user(app_user):
        if not start_date or not end_date:
            start_date, end_date = default_date_range()
        if not employee:
            raise HTTPException(status_code=400, detail="请选择要查看的团队成员")
        memberships = await organization_memberships_for_user(app_user)
        membership = next(
            item
            for item in memberships
            if item.get("status") == "active" and item.get("organizationStatus", "active") == "active"
        )
        organization_id = organization_identifier(membership)
        payload = await mock_usage_payload(
            "mock_team_member_usage",
            organization_id,
            start_date=start_date,
            end_date=end_date,
            source=source,
            email=str(app_user.get("email") or ""),
            team_ref=(team_ref or "").strip(),
            employee=employee,
        )
        return {"leader": {"email": app_user["email"], "name": app_user["name"]}, "startDate": start_date, "endDate": end_date, "source": source, "teamRef": payload.get("teamRef") or team_ref or "", **payload}
    await require_non_inactive_demo_identity(app_user)
    if app_user.get("id"):
        # Local accounts never inherit team scopes from a same-email SSO user.
        raise auth_http_error(403, "当前账号还没有团队负责人权限", "AUTH_TEAM_SCOPE_UNAVAILABLE")
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
    if await is_demo_customer_user(app_user):
        if not start_date or not end_date:
            start_date, end_date = default_date_range()
        memberships = await organization_memberships_for_user(app_user)
        membership = next(
            item
            for item in memberships
            if item.get("status") == "active" and item.get("organizationStatus", "active") == "active"
        )
        payload = await mock_usage_payload(
            "mock_personal_usage",
            organization_identifier(membership),
            start_date=start_date,
            end_date=end_date,
            source=source,
            email=str(app_user.get("email") or ""),
        )
        rows = list(payload.get("rows") or [])
        start = (page - 1) * page_size
        return {"user": app_user, "rows": rows[start : start + page_size], "total": len(rows), "page": page, "pageSize": page_size, "cache": {"hit": False, "ttlSeconds": 0}}
    await require_non_inactive_demo_identity(app_user)
    if app_user.get("id"):
        local_user = await auth_store_call("get_user", str(app_user["id"]))
        if not local_user:
            raise auth_http_error(401, "本地登录已失效，请重新登录", "AUTH_LOGIN_REQUIRED")
        app_user = await auth_user_payload(local_user, refresh_entitlement=True)
        require_active_local_entitlement(app_user)
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
    app_user, upstream_user = await current_upstream_user(request)
    require_active_local_entitlement(app_user)
    user_ids = upstream_user_ids(upstream_user)
    if not user_ids:
        raise HTTPException(status_code=502, detail="上游员工记录缺少 user_id")
    primary_user_id = primary_upstream_user_id(upstream_user)
    # 两个上游调用相互独立，并行执行
    (available_models, unrestricted), keys = await asyncio.gather(
        client().available_key_models(primary_user_id),
        client().keys_for_user_ids(user_ids, refresh),
    )
    return {
        "keys": add_revealability(keys),
        "availableModels": [model for model in available_models if model not in {"no-default-models", "all-proxy-models"}],
        "unrestrictedModels": unrestricted,
    }


@app.post("/api/me/keys")
async def create_my_key(data: CreatePersonalKeyRequest, request: Request) -> JSONResponse:
    await enforce_csrf(request)
    app_user, _ = await current_upstream_user(request)
    require_active_local_entitlement(app_user)
    write_key_audit("create", app_user["email"], "-", request, "disabled")
    raise HTTPException(
        status_code=403,
        detail="管理员已暂时关闭新增访问密钥，请联系管理员申请。",
    )


@app.post("/api/me/keys/{key_id:path}/regenerate")
async def regenerate_my_key(key_id: str, request: Request) -> JSONResponse:
    await enforce_csrf(request)
    app_user, upstream_user = await current_upstream_user(request)
    require_active_local_entitlement(app_user)
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
    await enforce_csrf(request)
    app_user, upstream_user = await current_upstream_user(request)
    require_active_local_entitlement(app_user)
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
    await enforce_csrf(request)
    app_user, upstream_user = await current_upstream_user(request)
    require_active_local_entitlement(app_user)
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
    await enforce_csrf(request)
    app_user, upstream_user = await current_upstream_user(request)
    require_active_local_entitlement(app_user)
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


# ---- 充值中心 ----


async def billing_identity(request: Request) -> tuple[dict[str, Any], str]:
    """返回当前用户与其上游账号标识。

    刻意不调用 :func:`require_active_local_entitlement`：新注册用户正是要靠充值
    才能拿到权限，若在这里挡住，新用户永远无法自助开通。
    """
    app_user = require_user(request)
    if await is_demo_customer_user(app_user):
        raise auth_http_error(403, "企业演示账号不提供自助充值", "ORGANIZATION_BILLING_FORBIDDEN")
    await require_non_inactive_demo_identity(app_user)
    local_user_id = str(app_user.get("id") or "")
    if not local_user_id:
        # 企业 SSO 员工走部门预算，不属于自助充值范围。
        raise HTTPException(status_code=403, detail="当前账号无需自助充值")
    local_user = await auth_store_call("get_user", local_user_id)
    if not local_user or str(local_user.get("status") or "active") != "active":
        raise auth_http_error(401, "登录已失效，请重新登录", "AUTH_LOGIN_REQUIRED")
    account = await auth_store_call("get_upstream_account", local_user_id, "primary")
    upstream_user_id = str((account or {}).get("upstream_user_id") or "")
    if not account or account.get("status") != "provisioned" or not upstream_user_id:
        raise auth_http_error(409, "账号仍在开通中，请稍后重试", "AUTH_PROVISIONING_PENDING")
    return app_user, upstream_user_id


async def apply_topup_entitlement(trade_no: str, user_id: str, upstream_user_id: str) -> dict[str, Any]:
    """把已落账的充值同步到上游额度。

    上游写入失败不回滚本地余额——钱已经收到了。这里只把订单标成待同步，由
    :func:`retry_pending_billing_sync` 补偿，并在健康检查里暴露积压数量。
    """
    store = require_billing_store()
    account = await store.get_account(user_id)
    try:
        result = await billing.sync_upstream_entitlement(
            client(), upstream_user_id, float(account["topupTotalUsd"])
        )
    except Exception as exc:
        error = f"{exc.__class__.__name__}: {exc}"
        await store.mark_sync_state(trade_no, SYNC_PENDING, error)
        logger.exception("topup upstream sync failed trade_no=%s", trade_no)
        return {"synced": False, "error": error}
    await store.mark_sync_state(trade_no, SYNC_DONE, "")
    # 权限可能刚被放开，清缓存让前端立刻看到可用状态。
    local_entitlement_cache.delete(f"local-entitlement:{upstream_user_id}")
    return {"synced": True, **result}


async def retry_pending_billing_sync(limit: int = 20) -> int:
    """重试上游额度写入失败的已付订单。"""
    store = billing_store()
    if store is None or store.pool is None:
        return 0
    pending = await store.pending_sync_orders(limit)
    repaired = 0
    for order in pending:
        account = await auth_store_call("get_upstream_account", order["userId"], "primary")
        upstream_user_id = str((account or {}).get("upstream_user_id") or "")
        if not upstream_user_id:
            continue
        outcome = await apply_topup_entitlement(order["tradeNo"], order["userId"], upstream_user_id)
        if outcome.get("synced"):
            repaired += 1
    return repaired


@app.get("/api/me/billing")
async def my_billing(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    store = require_billing_store()
    app_user, upstream_user_id = await billing_identity(request)
    user_id = str(app_user["id"])
    account, orders, upstream_info = await asyncio.gather(
        store.get_account(user_id),
        store.list_user_orders(user_id, limit, offset),
        client().user_info(upstream_user_id),
    )
    # The upstream user record owns the authoritative cumulative spend.  The
    # local ledger owns only successful top-ups, so derive the displayed
    # remaining credit from those two independent values.
    try:
        spent_usd = max(0.0, float(upstream_info.get("spend") or 0.0))
    except (TypeError, ValueError):
        spent_usd = 0.0
    account["spentUsd"] = spent_usd
    account["balanceUsd"] = max(0.0, float(account["topupTotalUsd"]) - spent_usd)
    return {"config": billing.public_config(), "account": account, "orders": orders}


@app.post("/api/me/billing/redeem")
async def redeem_code(data: RedeemRequest, request: Request) -> dict[str, Any]:
    await enforce_csrf(request)
    store = require_billing_store()
    app_user, upstream_user_id = await billing_identity(request)
    user_id = str(app_user["id"])
    try:
        result = await store.redeem(data.code, user_id)
    except BillingStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    sync = await apply_topup_entitlement(result["tradeNo"], user_id, upstream_user_id)
    return {
        "ok": True,
        "amountUsd": result["amountUsd"],
        "account": result["account"],
        "entitlementSynced": bool(sync.get("synced")),
    }


@app.post("/api/me/billing/orders")
async def create_topup_order(data: CreateTopupOrderRequest, request: Request) -> dict[str, Any]:
    await enforce_csrf(request)
    store = require_billing_store()
    app_user, _ = await billing_identity(request)
    channel = data.channel
    if channel == CHANNEL_EPAY and not billing.epay_enabled():
        # 自动支付未开通时优先退到收款码渠道，别把用户堵在死路上。
        channel = CHANNEL_MANUAL_QR if billing.manual_qr_enabled() else channel
    if channel == CHANNEL_EPAY and not billing.epay_enabled():
        raise HTTPException(status_code=503, detail="在线支付暂未开放，请联系管理员")
    if channel == CHANNEL_MANUAL_QR and not billing.manual_qr_enabled():
        raise HTTPException(status_code=503, detail="扫码转账暂未开放，请联系管理员")
    try:
        amount = billing.normalize_amount(data.amount)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    money = billing.money_for_amount(amount)
    if money < 0.01:
        raise HTTPException(status_code=400, detail="充值金额过小，请提高充值额度")
    if channel == CHANNEL_MANUAL_QR:
        allowed = {item["method"] for item in billing.manual_qr_methods()}
        if data.paymentMethod not in allowed:
            raise HTTPException(status_code=400, detail="该支付方式暂未开放")

    trade_no = billing.generate_trade_no(str(app_user["id"]))
    params: dict[str, str] = {}
    submit_url = ""
    if channel == CHANNEL_EPAY:
        try:
            params = billing.epay_purchase_params(
                trade_no,
                money,
                data.paymentMethod,
                "通衢 API 额度充值",
                billing.epay_notify_url(),
                billing.epay_return_url(),
            )
            submit_url = billing.epay_submit_url()
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        await store.create_order(
            trade_no,
            str(app_user["id"]),
            channel,
            amount,
            money,
            billing.exchange_rate(),
            data.paymentMethod,
        )
    except BillingStoreError as exc:
        raise HTTPException(status_code=500, detail="创建充值订单失败，请稍后重试") from exc
    payload: dict[str, Any] = {
        "tradeNo": trade_no,
        "channel": channel,
        "amountUsd": amount,
        "moneyCny": money,
        "paymentMethod": data.paymentMethod,
    }
    if channel == CHANNEL_EPAY:
        payload["submitUrl"] = submit_url
        payload["params"] = params
        payload["redirectUrl"] = billing.epay_redirect_url(params)
    else:
        method = next(
            (item for item in billing.manual_qr_methods() if item["method"] == data.paymentMethod),
            None,
        )
        payload["qrUrl"] = (method or {}).get("qrUrl", "")
        payload["methodLabel"] = (method or {}).get("label", "")
        payload["notice"] = billing.manual_qr_notice()
        payload["contact"] = billing.manual_qr_contact()
        payload["reviewMinutes"] = billing.manual_review_minutes()
    return payload


@app.post("/api/me/billing/orders/{trade_no}/submit")
async def submit_manual_payment(
    trade_no: str, data: SubmitManualPaymentRequest, request: Request
) -> dict[str, Any]:
    """用户扫码付款后回填付款说明，等管理员确认到账。

    个人收款码没有支付回调，平台无法自动判定到账，因此这里只把订单推进"待确认"，
    额度必须由管理员在后台确认后才入账。
    """
    await enforce_csrf(request)
    store = require_billing_store()
    app_user, _ = await billing_identity(request)
    existing = await store.get_order(trade_no)
    if existing is None or existing["userId"] != str(app_user["id"]):
        raise HTTPException(status_code=404, detail="充值订单不存在")
    if existing["channel"] != CHANNEL_MANUAL_QR:
        raise HTTPException(status_code=400, detail="该订单无需提交付款凭证")
    try:
        order = await store.submit_manual_payment(trade_no, str(app_user["id"]), data.payerNote)
    except BillingStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "order": order, "reviewMinutes": billing.manual_review_minutes()}


@app.get("/api/me/billing/orders/{trade_no}")
async def my_billing_order(trade_no: str, request: Request) -> dict[str, Any]:
    store = require_billing_store()
    app_user, _ = await billing_identity(request)
    order = await store.get_order(trade_no)
    # 只能查自己的订单，避免用订单号枚举他人充值记录。
    if order is None or order["userId"] != str(app_user["id"]):
        raise HTTPException(status_code=404, detail="充值订单不存在")
    account = await store.get_account(str(app_user["id"]))
    return {"order": order, "account": account}


@app.api_route("/api/pay/epay/notify", methods=["GET", "POST"])
async def epay_notify(request: Request) -> PlainTextResponse:
    """支付网关异步回调。

    这个端点必须免登录，因此不信任任何调用者身份，只认三道校验：签名、订单处于
    待付状态、金额与下单快照一致。响应体固定为纯文本，否则网关会无限重推。
    """
    store = billing_store()
    if store is None or store.pool is None:
        return PlainTextResponse("fail")
    params: dict[str, str] = {}
    if request.method == "POST":
        # 网关回调是 application/x-www-form-urlencoded，手工解析可以免掉
        # multipart 解析依赖，也避免超大 body 被当表单缓冲。
        body = (await request.body())[:8192].decode("utf-8", "ignore")
        params.update({str(name): str(value) for name, value in parse_qsl(body, keep_blank_values=True)})
    params.update({str(name): str(value) for name, value in request.query_params.items()})

    trade_no = str(params.get("out_trade_no") or "").strip()
    if not billing.epay_verify(params):
        logger.warning("epay notify signature rejected trade_no=%s ip=%s", trade_no, request_ip(request))
        return PlainTextResponse("fail")
    if str(params.get("trade_status") or "").upper() != "TRADE_SUCCESS":
        # 非成功状态无需落账，但要回 success 以免网关持续重推。
        return PlainTextResponse("success")

    order = await store.get_order(trade_no)
    if order is None:
        logger.warning("epay notify for unknown order trade_no=%s", trade_no)
        return PlainTextResponse("fail")
    try:
        paid = round(float(params.get("money") or 0), 2)
    except (TypeError, ValueError):
        logger.warning("epay notify money unparseable trade_no=%s", trade_no)
        return PlainTextResponse("fail")
    if abs(paid - round(float(order["moneyCny"]), 2)) > 0.001:
        # 金额被改写：签名可能来自另一笔订单的重放，坚决不落账。
        logger.error(
            "epay notify amount mismatch trade_no=%s expected=%s paid=%s",
            trade_no, order["moneyCny"], paid,
        )
        return PlainTextResponse("fail")

    try:
        result = await store.settle_order(
            trade_no,
            upstream_trade_no=str(params.get("trade_no") or ""),
            notify_payload=json.dumps(params, ensure_ascii=False),
        )
    except BillingStoreError:
        logger.exception("epay notify settle failed trade_no=%s", trade_no)
        return PlainTextResponse("fail")

    if result["settled"]:
        account = await auth_store_call("get_upstream_account", order["userId"], "primary")
        upstream_user_id = str((account or {}).get("upstream_user_id") or "")
        if upstream_user_id:
            await apply_topup_entitlement(trade_no, order["userId"], upstream_user_id)
        else:
            await store.mark_sync_state(trade_no, SYNC_PENDING, "账号尚未完成开通")
    return PlainTextResponse("success")


# ---- 充值管理 ----


@app.get("/api/admin/billing/redemptions")
async def admin_list_redemptions(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    store = require_billing_store()
    require_admin(request)
    return await store.list_redemptions(limit, offset)


@app.post("/api/admin/billing/redemptions")
async def admin_create_redemptions(data: CreateRedemptionRequest, request: Request) -> JSONResponse:
    await enforce_csrf(request)
    store = require_billing_store()
    admin = require_admin(request)
    try:
        amount = billing.normalize_amount(data.amount)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=data.expiresInDays) if data.expiresInDays else None
    )
    created = await store.create_redemptions(
        data.count, amount, data.name, str(admin.get("email") or ""), expires_at
    )
    # 明文兑换码只在这一次响应里出现，之后库里只有哈希。
    return JSONResponse(
        {"items": created, "count": len(created)},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@app.post("/api/admin/billing/redemptions/{redemption_id}/disable")
async def admin_disable_redemption(redemption_id: str, request: Request) -> dict[str, Any]:
    await enforce_csrf(request)
    store = require_billing_store()
    require_admin(request)
    changed = await store.disable_redemption(redemption_id)
    if not changed:
        raise HTTPException(status_code=400, detail="该兑换码已被使用或已停用")
    return {"ok": True}


@app.get("/api/admin/billing/orders")
async def admin_list_orders(
    request: Request,
    keyword: str = Query("", max_length=100),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    store = require_billing_store()
    require_admin(request)
    payload = await store.list_all_orders(keyword, limit, offset)
    payload["pendingSyncCount"] = await store.pending_sync_count()
    payload["pendingReviewCount"] = await store.pending_review_count()
    payload["pendingReviews"] = await store.list_pending_reviews()
    return payload


@app.post("/api/admin/billing/orders/{trade_no}/complete")
async def admin_complete_order(
    trade_no: str, request: Request, data: ReviewOrderRequest | None = None
) -> dict[str, Any]:
    """人工确认到账。

    既用于收款码转账的到账确认，也用于自动支付成功但回调丢失的补单。确认人写入
    ``reviewed_by``，便于事后追溯是谁放的款。
    """
    await enforce_csrf(request)
    store = require_billing_store()
    app_user = require_admin(request)
    reviewer = str(app_user.get("email") or "")
    order = await store.get_order(trade_no)
    if order is None:
        raise HTTPException(status_code=404, detail="充值订单不存在")
    if order["status"] != ORDER_PENDING:
        raise HTTPException(status_code=400, detail="该订单不处于待支付状态")
    try:
        result = await store.settle_order(
            trade_no,
            notify_payload=json.dumps({"manual_by": reviewer}, ensure_ascii=False),
            reviewed_by=reviewer,
            review_note=(data.note if data else "") or "管理员确认到账",
        )
    except BillingStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    account = await auth_store_call("get_upstream_account", order["userId"], "primary")
    upstream_user_id = str((account or {}).get("upstream_user_id") or "")
    sync: dict[str, Any] = {"synced": False, "error": "账号尚未完成开通"}
    if upstream_user_id:
        sync = await apply_topup_entitlement(trade_no, order["userId"], upstream_user_id)
    else:
        await store.mark_sync_state(trade_no, SYNC_PENDING, "账号尚未完成开通")
    return {
        "ok": True,
        "settled": result["settled"],
        "account": result["account"],
        "entitlementSynced": bool(sync.get("synced")),
    }


@app.post("/api/admin/billing/orders/{trade_no}/reject")
async def admin_reject_order(
    trade_no: str, request: Request, data: ReviewOrderRequest | None = None
) -> dict[str, Any]:
    """驳回未收到款的待确认订单，订单转为已失败，不影响余额。"""
    await enforce_csrf(request)
    store = require_billing_store()
    app_user = require_admin(request)
    order = await store.get_order(trade_no)
    if order is None:
        raise HTTPException(status_code=404, detail="充值订单不存在")
    if order["status"] != ORDER_PENDING:
        raise HTTPException(status_code=400, detail="该订单不处于待支付状态")
    changed = await store.fail_order(
        trade_no,
        (data.note if data else "") or "管理员核对后未查到该笔付款",
        reviewed_by=str(app_user.get("email") or ""),
    )
    if not changed:
        raise HTTPException(status_code=400, detail="该订单已被处理")
    return {"ok": True}


@app.post("/api/admin/billing/sync/retry")
async def admin_retry_billing_sync(request: Request) -> dict[str, Any]:
    """重试积压的上游额度写入。"""
    await enforce_csrf(request)
    store = require_billing_store()
    require_admin(request)
    repaired = await retry_pending_billing_sync()
    return {"ok": True, "repaired": repaired, "pendingSyncCount": await store.pending_sync_count()}


@app.get("/api/models")
async def models(request: Request) -> dict[str, Any]:
    app_user = require_user(request)
    if await is_demo_customer_user(app_user):
        raise auth_http_error(403, "企业演示账号不提供模型目录查询", "ORGANIZATION_MODELS_FORBIDDEN")
    await require_non_inactive_demo_identity(app_user)
    if app_user.get("id"):
        local_user = await auth_store_call("get_user", str(app_user["id"]))
        if not local_user:
            raise auth_http_error(401, "本地登录已失效，请重新登录", "AUTH_LOGIN_REQUIRED")
        app_user = await auth_user_payload(local_user, refresh_entitlement=True)
        require_active_local_entitlement(app_user)
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
    return FileResponse(
        ROOT_DIR / "index.html",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"},
    )


@app.get("/{path:path}")
async def spa_fallback(path: str) -> FileResponse:
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="接口不存在")
    return FileResponse(
        ROOT_DIR / "index.html",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"},
    )
