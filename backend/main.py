import base64
import math
import asyncio
import hashlib
import hmac
import inspect
import ipaddress
import json
import logging
import os
import re
import secrets
import smtplib
import socket
import ssl
import uuid
from contextlib import asynccontextmanager
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from base64 import urlsafe_b64encode
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from html import unescape
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, unquote, urljoin, urlparse, urlunparse

import httpx
from authlib.integrations.base_client import OAuthError
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import Scope

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
from .auth_store import (
    AuthStore,
    AuthStoreConfigError,
    DuplicateEmailError,
    DuplicateLoginNameError,
    ManagedAccountPasswordResetError,
    MembershipClaimStateError,
)
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
    department_key,
    mask_key,
    model_display_name,
    normalize_model_display_name,
    usage_today,
)
from .observability import (
    STABILITY_DEFINITIONS_VERSION,
    metric_envelope,
    monthly_forecast,
    model_state,
    normalize_event,
    reviewed_savings_measurements,
    scenario_details,
    stability_metrics,
    verified_savings,
)
from .key_vault import KeyVault, KeyVaultError
from .organization_store import (
    DEFAULT_TOKEN_DAILY_BUDGET_USD,
    MAX_MODELS_PER_TOKEN,
    MAX_TOKEN_DAILY_BUDGET_USD,
    MEMBER_REMOVED_STATUS,
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
from .organization_repository import PostgreSQLOrganizationRepository
from .organization_provisioning import OrganizationProvisioningService
from .usage_store import UsageStore
from .usage_realtime import UsageRealtimeStore, realtime_enabled
from .usage_realtime_worker import UsageRealtimeWorker
from .usage_sync import (
    UsageSynchronizer,
    run_sync_with_recent_refresh,
    run_usage_backfill_once,
)


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ai-token-dashboard")
logging.getLogger("httpx").setLevel(logging.WARNING)

SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "ai_token_dashboard_session")
OIDC_STATE_PREFIX = "_state_company_"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
REMOTE_DEMO_USAGE_PATHS = frozenset({
    "/api/me/usage",
    "/api/team/usage",
    "/api/team/member/usage",
    "/api/admin/usage",
    "/api/admin/users",
    "/api/admin/departments/usage",
})


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
    await start_organization_service()
    await start_usage_sync()
    try:
        yield
    finally:
        await close_litellm_client()


app = FastAPI(title="通衢 API", lifespan=app_lifespan)
# 首屏要先下载 index.html 与 app.js 才能发出任何接口请求，两者合计 500KB 以上。
# 它们是纯文本，压缩后只剩两成，是首屏可感知延迟里最便宜的一段。
app.add_middleware(GZipMiddleware, minimum_size=1024)
VERSIONED_APP_CACHE_CONTROL = "public, max-age=31536000, immutable"
APP_JS_VERSION_PLACEHOLDER = "__APP_JS_VERSION__"


def app_js_version() -> str:
    """Return a content fingerprint so immutable script URLs never serve stale code."""

    return hashlib.sha256((ROOT_DIR / "assets" / "app.js").read_bytes()).hexdigest()[:16]


def spa_html_response() -> HTMLResponse:
    markup = (ROOT_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(
        markup.replace(APP_JS_VERSION_PLACEHOLDER, app_js_version()),
        headers={"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"},
    )


class VersionedAppStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        query_string = scope.get("query_string", b"")
        query = dict(parse_qsl(query_string.decode("latin-1"), keep_blank_values=True))
        method = str(scope.get("method") or "GET").upper()
        if (
            method in {"GET", "HEAD"}
            and path == "app.js"
            and query.get("v")
            and response.status_code in {200, 304}
        ):
            response.headers["Cache-Control"] = VERSIONED_APP_CACHE_CONTROL
        return response


app.mount("/assets", VersionedAppStaticFiles(directory=ROOT_DIR / "assets"), name="assets")


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


@app.middleware("http")
async def protect_secret_bearing_urls(request: Request, call_next):
    """Keep one-time activation/reset tokens out of browser caches and referrers."""

    response = await call_next(request)
    query = request.query_params
    secret_query = any(
        name in query
        for name in ("organization_claim", "organization_invitation", "reset_token")
    )
    secret_route = (
        request.method == "GET"
        and request.url.path.startswith("/api/auth/organization-claims/")
    ) or (
        request.method == "POST"
        and (
            request.url.path
            == "/api/platform/organization-adoptions/apply"
            or request.url.path.endswith("/membership-claims")
            or request.url.path.endswith("/password-reset")
        )
    )
    if secret_query or secret_route:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.middleware("http")
async def enforce_remote_demo_read_only(request: Request, call_next):
    """Deny writes before handlers can touch the upstream or shared snapshot DB."""

    path = request.url.path
    method = request.method.upper()
    refresh_requested = str(request.query_params.get("refresh") or "").strip().lower() in {
        "1", "true", "yes", "on"
    }
    if remote_demo_read_only() and refresh_requested and path in REMOTE_DEMO_USAGE_PATHS:
        return JSONResponse(
            status_code=403,
            content={"detail": "远端演示环境为只读，不能刷新用量快照", "code": "REMOTE_DEMO_READ_ONLY"},
        )
    if remote_demo_read_only() and path.startswith("/api/") and not remote_demo_request_allowed(method, path):
        return JSONResponse(
            status_code=403,
            content={"detail": "远端演示环境为只读，不能执行此操作", "code": "REMOTE_DEMO_READ_ONLY"},
        )
    return await call_next(request)


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
_observability_refresh_tasks: dict[str, asyncio.Task[Any]] = {}
_observability_refresh_lock = asyncio.Lock()
_observability_memory_snapshots: dict[str, dict[str, Any]] = {}
_usage_singleflight: dict[str, asyncio.Task[Any]] = {}
_usage_singleflight_lock = asyncio.Lock()
_usage_last_good_payloads: dict[str, dict[str, Any]] = {}
_usage_last_good_order: list[str] = []
# Generated customer-demo boards are isolated from the production board
# caches. Their keys are derived from the server-resolved organization scope.
organization_usage_cache = TTLCache()
# Upstream companies that have no local record yet are read-only candidates.
# Cache them separately so browsing the customer directory does not depend on
# an upstream round trip for every page render.
pending_adoption_cache = TTLCache()
_litellm_client: LiteLLMClient | None = None
_key_vault: KeyVault | None = None
_usage_store: UsageStore | None = UsageStore.from_environment()
_billing_store: BillingStore | None = BillingStore.from_environment()
_auth_store: AuthStore | None = None
_organization_store: OrganizationStore | PostgreSQLOrganizationRepository | None = None
_organization_capability_status: dict[str, Any] = {
    "mode": "disabled",
    "status": "disabled",
    "available": False,
    "lastCheckedAt": None,
}
_organization_capability_probe_lock = asyncio.Lock()
local_entitlement_cache = TTLCache()
_usage_sync_task: asyncio.Task[Any] | None = None
_usage_refresh_task: asyncio.Task[Any] | None = None
_usage_realtime_store: UsageRealtimeStore | None = None
_usage_realtime_read_status: dict[str, Any] = {}
_usage_realtime_task: asyncio.Task[Any] | None = None
_usage_realtime_worker: UsageRealtimeWorker | None = None
_usage_sync_stop: asyncio.Event | None = None
_organization_outbox_task: asyncio.Task[Any] | None = None
_organization_outbox_stop: asyncio.Event | None = None
_usage_sync_status: dict[str, Any] = {"status": "disabled", "lastRun": None}


def _observability_snapshot_key(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _observability_cache_meta(
    record: dict[str, Any] | None,
    *,
    state: str,
    refreshing: bool = False,
    layer: str = "database",
    response_bytes: int | None = None,
) -> dict[str, Any]:
    generated = record.get("generated_at") if record else None
    if isinstance(generated, datetime):
        generated_at = generated.astimezone(timezone.utc).isoformat()
        age_seconds = max(0, int((datetime.now(timezone.utc) - generated).total_seconds()))
    else:
        generated_at = str(generated or "") or None
        try:
            parsed = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
            age_seconds = max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
        except (TypeError, ValueError):
            age_seconds = None
    return {
        "state": state,
        "generatedAt": generated_at,
        "ageSeconds": age_seconds,
        "refreshing": refreshing,
        "lastRefreshError": str((record or {}).get("last_refresh_error") or ""),
        "dataRevision": str((record or {}).get("data_revision") or ""),
        "layer": layer,
        "responseBytes": response_bytes,
    }


def _observability_response_bytes(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


def _snapshot_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    if isinstance(payload, dict):
        return dict(payload)
    if isinstance(payload, str):
        decoded = json.loads(payload)
        return dict(decoded) if isinstance(decoded, dict) else {}
    return {}


async def _observability_refresh(
    dashboard_type: str,
    snapshot_key: str,
    builder: Any,
) -> dict[str, Any]:
    store = _admin_observability_store()
    started = asyncio.get_running_loop().time()
    try:
        payload = jsonable_encoder(await asyncio.wait_for(
            builder(), timeout=max(1, env_int("OBSERVABILITY_REFRESH_TIMEOUT_SECONDS", 30))
        ))
        state = await _call_store_optional(store, ("snapshot_state",), default={})
        revision = str((state or {}).get("revision") or (state or {}).get("snapshotRevision") or "")
        record = await _call_store_optional(
            store,
            ("save_observability_snapshot",),
            dashboard_type,
            snapshot_key,
            payload,
            data_revision=revision,
            default=None,
        )
        if not record:
            record = {
                "payload": payload,
                "generated_at": datetime.now(timezone.utc),
                "data_revision": revision,
                "last_refresh_error": "",
            }
        memory_key = f"{dashboard_type}:{snapshot_key}"
        _observability_memory_snapshots[memory_key] = dict(record)
        response_bytes = _observability_response_bytes(payload)
        logger.info(
            "observability refresh dashboard=%s total_ms=%.0f cache_state=stored",
            dashboard_type,
            (asyncio.get_running_loop().time() - started) * 1000,
        )
        return {**payload, "cache": _observability_cache_meta(record, state="fresh", layer="rebuild", response_bytes=response_bytes)}
    except Exception as exc:
        await _call_store_optional(
            store,
            ("mark_observability_snapshot_refresh",),
            dashboard_type,
            snapshot_key,
            refreshing=False,
            error=exc.__class__.__name__,
            default=None,
        )
        logger.exception("observability refresh failed dashboard=%s", dashboard_type)
        raise
    finally:
        async with _observability_refresh_lock:
            _observability_refresh_tasks.pop(f"{dashboard_type}:{snapshot_key}", None)


async def _start_observability_refresh(
    dashboard_type: str, snapshot_key: str, builder: Any
) -> asyncio.Task[Any]:
    task_key = f"{dashboard_type}:{snapshot_key}"
    async with _observability_refresh_lock:
        existing = _observability_refresh_tasks.get(task_key)
        if existing and not existing.done():
            return existing
        await _call_store_optional(
            _admin_observability_store(),
            ("mark_observability_snapshot_refresh",),
            dashboard_type,
            snapshot_key,
            refreshing=True,
            default=None,
        )
        task = asyncio.create_task(
            _observability_refresh(dashboard_type, snapshot_key, builder),
            name=f"observability-refresh-{dashboard_type}",
        )
        _observability_refresh_tasks[task_key] = task
        return task


async def _cached_observability_dashboard(
    dashboard_type: str,
    key_payload: dict[str, Any],
    builder: Any,
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    store = _admin_observability_store()
    snapshot_key = _observability_snapshot_key(key_payload)
    memory_key = f"{dashboard_type}:{snapshot_key}"
    lookup_started = asyncio.get_running_loop().time()
    record = _observability_memory_snapshots.get(memory_key)
    layer = "memory" if record else "database"
    if not record:
        record = await _call_store_optional(
            store, ("get_observability_snapshot",), dashboard_type, snapshot_key, default=None
        )
        if record:
            _observability_memory_snapshots[memory_key] = dict(record)
    fresh_seconds = max(1, env_int("OBSERVABILITY_CACHE_FRESH_SECONDS", 300))
    stale_seconds = max(fresh_seconds, env_int("OBSERVABILITY_CACHE_STALE_MAX_SECONDS", 86400))
    age = None
    if record and isinstance(record.get("generated_at"), datetime):
        age = (datetime.now(timezone.utc) - record["generated_at"]).total_seconds()
    if record and age is not None and age <= fresh_seconds and not refresh:
        payload = _snapshot_payload(record)
        payload["cache"] = _observability_cache_meta(record, state="fresh", layer=layer, response_bytes=_observability_response_bytes(payload))
        logger.info("observability overview dashboard=%s snapshot_ms=%.0f total_ms=%.0f cache_layer=%s cache_state=fresh response_bytes=%s", dashboard_type, (asyncio.get_running_loop().time() - lookup_started) * 1000, (asyncio.get_running_loop().time() - lookup_started) * 1000, layer, payload["cache"]["responseBytes"])
        return payload
    if record and age is not None and age <= stale_seconds:
        task = await _start_observability_refresh(dashboard_type, snapshot_key, builder)
        payload = _snapshot_payload(record)
        payload["cache"] = _observability_cache_meta(record, state="refreshing" if not task.done() else "stale", refreshing=not task.done(), layer=layer, response_bytes=_observability_response_bytes(payload))
        return payload
    task = await _start_observability_refresh(dashboard_type, snapshot_key, builder)
    budget = max(100, env_int("OBSERVABILITY_COLD_QUERY_BUDGET_MS", 1500)) / 1000
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=budget)
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=503,
            detail="看板数据正在生成，请稍后重试",
            headers={"Retry-After": "2"},
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "observability cold query failed dashboard=%s cache_state=unavailable",
            dashboard_type,
        )
        raise HTTPException(
            status_code=503,
            detail="看板数据暂不可用，后台刷新仍在重试",
            headers={"Retry-After": "5"},
        ) from exc


async def _invalidate_observability_dashboard(dashboard_type: str) -> None:
    prefix = f"{dashboard_type}:"
    for key in [item for item in _observability_memory_snapshots if item.startswith(prefix)]:
        _observability_memory_snapshots.pop(key, None)
    await _call_store_optional(
        _admin_observability_store(),
        ("delete_observability_snapshots",),
        dashboard_type,
        default=0,
    )


def usage_sync_role() -> str:
    role = os.getenv("USAGE_SYNC_ROLE", "combined").strip().lower()
    return role if role in {"reader", "worker", "combined"} else "combined"


def remote_demo_read_only() -> bool:
    """Keep the remote demonstration instance outside every production write path."""

    return env_bool("REMOTE_DEMO_READ_ONLY", False)


def remote_demo_get_allowed(path: str) -> bool:
    """Allow only authentication, health, and committed usage snapshot reads."""

    if not remote_demo_read_only():
        return True
    return (
        path == "/api/health"
        or path.startswith("/api/auth/")
        or path.startswith("/api/me/usage")
        or path.startswith("/api/team/usage")
        or path.startswith("/api/team/member/usage")
        or path.startswith("/api/admin/usage")
        or path.startswith("/api/admin/users")
        or path.startswith("/api/admin/departments/usage")
    )


def remote_demo_request_allowed(method: str, path: str) -> bool:
    """Keep auth navigation working without granting local-account write paths."""

    if method in {"GET", "HEAD", "OPTIONS"}:
        return remote_demo_get_allowed(path)
    # Logout only clears the demonstration instance's independently stored session.
    return method == "POST" and path == "/api/auth/logout"


def snapshot_reader_configured() -> bool:
    return usage_sync_role() == "reader" or usage_store() is not None


def usage_reader_config_status() -> dict[str, Any]:
    role = usage_sync_role()
    database_configured = bool(os.getenv("USAGE_DATABASE_URL", "").strip())
    sync_enabled = env_bool("USAGE_SYNC_ENABLED", False) or env_bool(
        "USAGE_REALTIME_ENABLED", False
    )
    realtime_requested = env_bool("USAGE_REALTIME_ENABLED", False)
    redis_configured = bool(os.getenv("USAGE_REDIS_URL", "").strip())
    missing: list[str] = []
    if role == "reader":
        if not database_configured:
            missing.append("USAGE_DATABASE_URL")
        if not sync_enabled:
            missing.append("USAGE_SYNC_ENABLED_OR_USAGE_REALTIME_ENABLED")
        if realtime_requested and not redis_configured:
            missing.append("USAGE_REDIS_URL")
    return {
        "role": role,
        "configured": not missing,
        "databaseConfigured": database_configured,
        "syncEnabled": sync_enabled,
        "realtimeEnabled": realtime_requested,
        "redisConfigured": redis_configured,
        "missing": missing,
    }


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
    if organization_mode() == "demo" and not loopback_development:
        raise RuntimeError(
            "ORGANIZATION_MODE=demo 仅允许 APP_BASE_URL 使用本机回环 HTTP 地址"
        )
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


def organization_mode() -> str:
    """Resolve the organization runtime mode with legacy demo compatibility."""

    configured = os.getenv("ORGANIZATION_MODE", "").strip().lower()
    if configured:
        if configured not in {"disabled", "demo", "real"}:
            raise RuntimeError("ORGANIZATION_MODE 必须是 disabled、demo 或 real")
        return configured
    return "demo" if env_bool("ORGANIZATION_DEMO_ENABLED", False) else "disabled"


def organization_enabled() -> bool:
    return organization_mode() in {"demo", "real"}


def organization_demo_enabled() -> bool:
    return organization_mode() == "demo"


def organization_real_enabled() -> bool:
    return organization_mode() == "real"


def organization_store() -> OrganizationStore | PostgreSQLOrganizationRepository:
    """Return the configured store without ever falling back from real to demo."""

    global _organization_store
    mode = organization_mode()
    if mode == "disabled":
        raise HTTPException(status_code=404, detail="企业组织功能尚未启用")
    if _organization_store is None:
        if mode == "demo":
            _organization_store = InMemoryOrganizationStore()
        else:
            repository = PostgreSQLOrganizationRepository.from_environment()
            if repository is None:
                raise HTTPException(status_code=503, detail="企业组织数据库尚未配置")
            _organization_store = repository
    return _organization_store


async def organization_store_call(method: str, *args: Any, **kwargs: Any) -> Any:
    # A few local-only workflows (notably invitation verification/acceptance)
    # must remain usable while the model gateway is temporarily unavailable.
    # They opt out explicitly; all customer data and upstream-management calls
    # remain fail-closed by default.
    require_capability = kwargs.pop("_require_capability", True)
    if require_capability:
        require_real_organization_capability()
    function = getattr(organization_store(), method)
    if inspect.iscoroutinefunction(function):
        return await function(*args, **kwargs)
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
    if remote_demo_read_only():
        configured = os.getenv("USAGE_SNAPSHOT_BACKEND_IDS", "").split(",")
        backend_ids = list(dict.fromkeys(item.strip() for item in configured if item.strip()))
        if not backend_ids:
            raise HTTPException(
                status_code=503,
                detail="远端演示环境缺少 USAGE_SNAPSHOT_BACKEND_IDS 快照配置",
            )
        return backend_ids
    return [backend.id for backend in client().backends]


def resolve_usage_range(start_date: str | None, end_date: str | None) -> tuple[str, str]:
    """Fall back to the default window and reject ranges that cannot be queried.

    自定义时间筛选让起止日期变成用户可控入参，这里挡掉格式错误和首尾颠倒的组合，
    否则非法值会一路传到 date.fromisoformat 或 SQL 才报 500。
    """
    if not start_date or not end_date:
        return default_date_range()
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="日期格式无效，请重新选择时间范围") from exc
    if start > end:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")
    return start.isoformat(), end.isoformat()


def usage_data_freshness(last_synced: datetime | None, start_date: str, end_date: str) -> dict[str, Any]:
    """Mark only ranges containing today as stale when their snapshot is old."""
    default_age = (
        env_int("USAGE_REALTIME_STALE_SECONDS", 30)
        if realtime_enabled()
        else env_int("USAGE_LIVE_REFRESH_MAX_AGE_SECONDS", 1800)
    )
    max_age = max(10, default_age)
    today = usage_today().isoformat()
    stale = False
    if end_date >= today:
        stale = last_synced is None or (datetime.now(timezone.utc) - last_synced).total_seconds() >= max_age
    lag_seconds = (
        max(0, int((datetime.now(timezone.utc) - last_synced).total_seconds()))
        if last_synced
        else None
    )
    return {
        "source": "database",
        "lastSyncedAt": last_synced.isoformat() if last_synced else None,
        "lagSeconds": lag_seconds,
        "stale": stale,
        "degraded": stale,
        "maxAgeSeconds": max_age,
    }


def remember_usage_payload(cache_key: str, payload: dict[str, Any]) -> None:
    """Keep a bounded last-known-good snapshot beyond the normal response TTL."""

    if cache_key in _usage_last_good_payloads:
        _usage_last_good_order.remove(cache_key)
    _usage_last_good_payloads[cache_key] = dict(payload)
    _usage_last_good_order.append(cache_key)
    limit = max(20, env_int("USAGE_LAST_GOOD_CACHE_MAX_ENTRIES", 500))
    while len(_usage_last_good_order) > limit:
        expired = _usage_last_good_order.pop(0)
        _usage_last_good_payloads.pop(expired, None)


def degraded_cached_usage_payload(
    cache_key: str,
    *,
    refresh_queued: bool = False,
) -> dict[str, Any] | None:
    cached = _usage_last_good_payloads.get(cache_key)
    if cached is None:
        return None
    payload = dict(cached)
    freshness = dict(payload.get("dataFreshness") or {})
    freshness.update(
        {
            "source": "process_cache_fallback",
            "stale": True,
            "degraded": True,
            "databaseAvailable": False,
        }
    )
    payload["dataFreshness"] = freshness
    payload["cache"] = {"hit": True, "ttlSeconds": 0, "stale": True}
    if refresh_queued:
        payload["refreshQueued"] = True
    return payload


def usage_payload_from_cache(
    cache: TTLCache,
    cache_key: str,
    *,
    refresh_queued: bool = False,
) -> dict[str, Any] | None:
    hit, value, ttl_seconds = cache.get(cache_key)
    if not hit:
        return None
    payload = dict(value)
    payload["cache"] = {"hit": True, "ttlSeconds": ttl_seconds}
    if refresh_queued:
        payload["refreshQueued"] = True
    return payload


def cache_usage_payload(
    cache: TTLCache,
    cache_key: str,
    fallback_key: str,
    payload: dict[str, Any],
    ttl_seconds: int,
) -> None:
    cache.set(cache_key, payload, ttl_seconds)
    remember_usage_payload(cache_key, payload)
    remember_usage_payload(fallback_key, payload)


def queue_usage_refresh(start_date: str, end_date: str, requested: bool) -> bool:
    if remote_demo_read_only() or not requested or usage_store() is None:
        return False
    trigger_usage_refresh(start_date, end_date, True)
    return True


async def snapshot_revision(start_date: str, end_date: str) -> str:
    store = usage_store()
    if store is None:
        return "development-upstream"
    try:
        await store.connect()
        revision_loader = getattr(store, "snapshot_revision", None)
        if callable(revision_loader):
            revision = await revision_loader(start_date, end_date, usage_backend_ids())
        else:
            revision = "legacy-test-snapshot"
    except Exception as exc:
        logger.exception("usage snapshot revision query failed")
        raise manual_refresh_database_unavailable() from exc
    if not revision:
        raise HTTPException(
            status_code=503,
            detail="所选日期范围的用量快照尚未就绪，请等待后台同步完成",
        )
    if end_date >= usage_today().isoformat() and realtime_enabled():
        global _usage_realtime_read_status
        realtime_revision = "fallback"
        try:
            realtime = usage_realtime_store()
            if realtime is not None:
                await realtime.connect()
                state = await realtime.status()
                _usage_realtime_read_status = dict(state)
                if state.get("ready"):
                    realtime_revision = str(state.get("revision") or 0)
        except Exception:
            logger.exception("usage realtime revision query failed")
            _usage_realtime_read_status = {"ready": False, "connected": False}
        return f"{revision}:live:{realtime_revision}"
    return revision


def usage_realtime_store() -> UsageRealtimeStore | None:
    global _usage_realtime_store
    if _usage_realtime_store is None:
        _usage_realtime_store = UsageRealtimeStore.from_environment()
    return _usage_realtime_store


def attach_snapshot_freshness(
    payload: dict[str, Any],
    last_synced: datetime | None,
    start_date: str,
    end_date: str,
    revision: str,
) -> dict[str, Any]:
    freshness = usage_data_freshness(last_synced, start_date, end_date)
    freshness["snapshotRevision"] = revision
    if end_date < usage_today().isoformat() or not realtime_enabled():
        freshness["source"] = "database_history"
    elif ":live:fallback" in revision:
        freshness.update(
            {
                "source": "database_fallback",
                "degraded": True,
                "realtimeRevision": None,
                "latestEventAt": None,
            }
        )
    else:
        state = _usage_realtime_read_status
        latest_event = state.get("latestEventAt")
        if isinstance(latest_event, datetime):
            freshness["lastSyncedAt"] = latest_event.isoformat()
        freshness.update(
            {
                "source": "realtime",
                "degraded": bool(state.get("backfillActive")),
                "realtimeRevision": state.get("revision"),
                "latestEventAt": latest_event.isoformat()
                if isinstance(latest_event, datetime)
                else None,
                "lagSeconds": state.get("latestEventLagSeconds"),
                "stale": bool(
                    not state.get("ready")
                    or state.get("backfillActive")
                    or (
                        state.get("latestEventLagSeconds") is not None
                        and int(state.get("latestEventLagSeconds"))
                        > max(10, env_int("USAGE_REALTIME_STALE_SECONDS", 30))
                    )
                ),
                "backfillActive": bool(state.get("backfillActive")),
                "backfillBackends": state.get("backfillBackends", []),
            }
        )
    payload["dataFreshness"] = freshness
    return payload


async def usage_singleflight(key: str, factory: Any) -> Any:
    """Collapse concurrent cold reads of one cache key into a single SQL query."""

    async with _usage_singleflight_lock:
        task = _usage_singleflight.get(key)
        if task is None:
            task = asyncio.create_task(factory())
            _usage_singleflight[key] = task
            # 请求方可能在等待期间断开，靠 finally 出队会把已完成的任务永久留在表里，
            # 后续请求就会一直读到这份旧结果。改由任务自身在完成时出队。
            task.add_done_callback(
                lambda finished, cache_key=key: _usage_singleflight.pop(cache_key, None)
                if _usage_singleflight.get(cache_key) is finished
                else None
            )
    return await asyncio.shield(task)


async def run_usage_sync(days: int) -> dict[str, Any]:
    if remote_demo_read_only():
        return {"status": "read_only", "rowCount": 0, "backendCount": 0}
    store = usage_store()
    if store is None:
        return {"status": "disabled", "rowCount": 0, "backendCount": 0}
    try:
        await store.connect()
        start_date, end_date = UsageSynchronizer.date_range(days)
        repository = await organization_repository_for_usage_sync()
        result = await run_sync_with_recent_refresh(
            client(), store, days, repository, UsageSynchronizer
        )
        if result.get("status") in {"ok", "partial"} and isinstance(
            repository, PostgreSQLOrganizationRepository
        ):
            result["organizationBackfill"] = await run_usage_backfill_once(
                client(), store, repository, max_windows=2
            )
        settlement: dict[str, Any] | None = None
        if result.get("status") == "ok" and organization_real_enabled():
            if isinstance(repository, PostgreSQLOrganizationRepository):
                completed_end = min(date.fromisoformat(end_date), usage_today() - timedelta(days=1))
                if completed_end >= date.fromisoformat(start_date):
                    billing_cutoffs = (
                        await repository.billing_effective_at_by_upstream_organization()
                    )
                    spend_rows = await store.organization_daily_spend(
                        start_date,
                        completed_end.isoformat(),
                        usage_backend_ids(),
                        billing_effective_at_by_organization=billing_cutoffs,
                    )
                    settlement = await repository.settle_usage_rows(spend_rows)
                    result["settlement"] = settlement
        _usage_sync_status.update(
            {
                "status": result.get("status", "ok"),
                "lastRun": datetime.now(timezone.utc).isoformat(),
                "rowCount": result.get("rowCount", 0),
                "backendCount": result.get("backendCount", 0),
                "errors": result.get("errors", []),
                "settlement": settlement,
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
            synchronizer = UsageSynchronizer(
                client(), store, await organization_repository_for_usage_sync()
            )
            await synchronizer.sync_department_directories()
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
    if remote_demo_read_only():
        return
    store = usage_store()
    if store is None:
        return
    if usage_sync_role() == "reader":
        await store.connect()
        enqueue = getattr(store, "enqueue_refresh_request", None)
        if callable(enqueue):
            await enqueue(start_date, end_date)
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
    logger.info(
        "usage request reads committed snapshot only start=%s end=%s refresh=%s",
        start_date,
        end_date,
        force,
    )
    if force and remote_demo_read_only():
        raise HTTPException(status_code=403, detail="远端演示环境为只读，不能刷新用量快照")
    if force and usage_store() is not None:
        trigger_usage_refresh(start_date, end_date, True)


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
    global _usage_sync_task, _usage_sync_stop, _usage_realtime_task, _usage_realtime_worker
    store = usage_store()
    if store is None:
        return
    await store.connect()
    if remote_demo_read_only():
        return
    if realtime_enabled():
        if usage_sync_role() == "reader" or (_usage_realtime_task is not None and not _usage_realtime_task.done()):
            return
        realtime = usage_realtime_store()
        if realtime is None:
            return
        _usage_realtime_worker = UsageRealtimeWorker(
            client(), store, realtime, worker_id=f"combined:{os.getpid()}"
        )
        _usage_realtime_task = asyncio.create_task(
            _usage_realtime_worker.run(), name="usage-realtime-loop"
        )
        return
    if usage_sync_role() == "reader":
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


async def organization_repository_for_usage_sync() -> Any | None:
    """Wait for real organization persistence and its first capability probe.

    Usage synchronization runs in a background task, so waiting here does not
    delay application startup.  The probe records upstream failures instead of
    raising them; a connected local repository can still provide safe token
    attribution while the usage APIs independently report their own failures.
    """

    if not organization_real_enabled():
        return None
    repository = organization_store()
    connect = getattr(repository, "connect", None)
    if callable(connect):
        result = connect()
        if inspect.isawaitable(result):
            await result
    await refresh_organization_capabilities()
    return repository


async def start_organization_service() -> None:
    """Connect durable organization state and probe the upstream contract."""

    mode = organization_mode()
    _organization_capability_status.update(
        {"mode": mode, "status": "disabled", "available": False, "lastCheckedAt": None}
    )
    if mode == "disabled":
        return
    if mode == "demo":
        organization_store()
        _organization_capability_status.update(
            {"status": "ready", "available": True, "lastCheckedAt": datetime.now(timezone.utc).isoformat()}
        )
        return
    # Do not make application startup wait on PostgreSQL or the upstream API.
    # The worker performs the first probe immediately and keeps retrying after
    # a transient outage, while health checks can also trigger a due probe.
    _organization_capability_status.update(
        {"mode": mode, "status": "starting", "available": False, "lastCheckedAt": None}
    )
    await start_organization_outbox_worker()


def organization_capability_recheck_seconds() -> int:
    """Return the minimum interval between automatic capability probes."""

    # Keep a conservative default so health checks cannot create an upstream
    # request storm. Tests and operators may set zero to request immediate
    # retries; malformed negative values fall back to the safe default.
    configured = env_int("ORGANIZATION_CAPABILITY_RECHECK_SECONDS", 60)
    return configured if configured >= 0 else 60


def _capability_last_checked_at() -> datetime | None:
    value = str(_organization_capability_status.get("lastCheckedAt") or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def organization_capability_probe_due(*, force: bool = False) -> bool:
    """Whether a real-mode capability probe should run now."""

    if force:
        return True
    last_checked = _capability_last_checked_at()
    if last_checked is None:
        return True
    elapsed = (datetime.now(timezone.utc) - last_checked).total_seconds()
    return elapsed >= organization_capability_recheck_seconds()


async def refresh_organization_capabilities(*, force: bool = False) -> dict[str, Any]:
    """Re-probe real organization dependencies without blocking startup.

    The lock and interval guard make this safe to call from both ``/api/health``
    and the outbox worker.  A successful probe starts (or leaves running) the
    compensation worker; failures are recorded and retried on a later call.
    """

    if not organization_real_enabled():
        return dict(_organization_capability_status)
    async with _organization_capability_probe_lock:
        # Another concurrent health request may have completed the probe while
        # this caller was waiting for the lock.
        if not organization_capability_probe_due(force=force):
            return dict(_organization_capability_status)
        checked_at = datetime.now(timezone.utc).isoformat()
        try:
            if not os.getenv("ORGANIZATION_INVITATION_SECRET", "").strip():
                raise RuntimeError("ORGANIZATION_INVITATION_SECRET is required in real mode")
            store = organization_store()
            connect = getattr(store, "connect", None)
            if callable(connect):
                result = connect()
                if inspect.isawaitable(result):
                    await result
            capabilities = await client().organization_capabilities()
            _organization_capability_status.update(
                {
                    **capabilities,
                    "mode": "real",
                    "status": "ready" if capabilities.get("available") else "unavailable",
                    "available": bool(capabilities.get("available")),
                    "lastCheckedAt": checked_at,
                }
            )
            _organization_capability_status.pop("error", None)
        except Exception as exc:
            logger.exception("real organization capability probe failed")
            _organization_capability_status.update(
                {
                    "mode": "real",
                    "status": "error",
                    "available": False,
                    "organizations": False,
                    "teams": False,
                    "keys": False,
                    "error": exc.__class__.__name__,
                    "lastCheckedAt": checked_at,
                }
            )
        state = dict(_organization_capability_status)
    if state.get("available"):
        await start_organization_outbox_worker()
    return state


async def organization_outbox_once(limit: int = 20) -> int:
    store = organization_store()
    if not isinstance(store, PostgreSQLOrganizationRepository):
        return 0

    async def mailer(email: str, token: str, _payload: dict[str, Any]) -> None:
        base_url = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
        await send_auth_email(email, "通衢 API 企业邀请", f"请打开以下链接接受企业邀请（72 小时内有效）：\n\n{base_url}/?organization_invitation={token}")

    result = await OrganizationProvisioningService(store, client(), mailer=mailer).process_outbox(limit=limit)
    await reconcile_active_membership_claims()
    await reconcile_baic_pilot_credit()
    return int(result.get("completed", 0))


async def organization_outbox_if_available(limit: int = 20) -> int:
    """Process durable work immediately only when the upstream is ready."""

    if not _organization_capability_status.get("available"):
        return 0
    try:
        return await organization_outbox_once(limit=limit)
    except Exception:
        # The approval and its outbox record are already durable. A transient
        # upstream failure must not make the platform repeat identity approval.
        logger.exception("organization outbox immediate run failed")
        return 0


async def reconcile_active_membership_claims() -> int:
    """Open managed login only after the durable member is fully active."""

    if not organization_real_enabled():
        return 0
    store = organization_store()
    if not isinstance(store, PostgreSQLOrganizationRepository):
        return 0
    claims = await auth_store_call("list_membership_claims", None, 500)
    activated = 0
    for claim in claims:
        status = str(claim.get("status") or "")
        if status not in {"approved", "provisioning"}:
            continue
        auth_user_id = str(claim.get("authUserId") or "")
        organization_id = str(claim.get("organizationId") or "")
        if not auth_user_id or not organization_id:
            continue
        memberships = await store.resolve_members_by_auth_user_id(auth_user_id)
        member = next(
            (
                item.get("member")
                for item in memberships
                if str(item.get("organizationId") or "") == organization_id
                and isinstance(item.get("member"), dict)
            ),
            None,
        )
        if not member and status == "approved":
            try:
                member = await store.create_managed_member(
                    str(claim.get("memberName") or ""),
                    str(claim.get("loginName") or ""),
                    str(claim.get("departmentId") or ""),
                    str(claim.get("role") or "admin"),
                    auth_user_id=auth_user_id,
                    team_role=(
                        "leader"
                        if str(claim.get("role") or "admin") == "admin"
                        else "member"
                    ),
                    organization_id=organization_id,
                )
            except OrganizationStoreError:
                logger.exception(
                    "failed to resume approved organization claim claim_id=%s",
                    claim.get("id"),
                )
                continue
        if status == "approved":
            await auth_store_call(
                "mark_membership_claim_provisioning", str(claim["id"]), ""
            )
        if not member or str(member.get("status") or "") != "active":
            continue
        principal_id = str(claim.get("principalId") or "")
        principal = (
            await store.get_principal(organization_id, principal_id)
            if principal_id
            else await store.ensure_principal(
                organization_id, str(claim.get("memberName") or "")
            )
        )
        if principal is None:
            logger.error(
                "organization claim principal is missing claim_id=%s principal_id=%s",
                claim.get("id"),
                principal_id,
            )
            continue
        await store.link_principal_member(
            organization_id, str(principal["id"]), str(member["id"])
        )
        await auth_store_call("activate_membership_claim", str(claim["id"]))
        activated += 1
    return activated


async def reconcile_baic_pilot_credit() -> bool:
    """Grant the pilot credit only after David has full customer access."""

    if not organization_real_enabled():
        return False
    store = organization_store()
    if not isinstance(store, PostgreSQLOrganizationRepository):
        return False
    organizations = await store.list_organizations(
        keyword="", include_archived=False, page=1, page_size=10
    )
    candidates = [
        item for item in (organizations.get("items") or [])
        if str(item.get("id") or "") == "4b13ec57df104522a59ee910824c7e70"
        and str(item.get("name") or "").strip() == "北汽集团"
    ]
    if len(candidates) != 1:
        return False
    organization_id = str(candidates[0]["id"])
    members = await store.list_members(
        organization_id=organization_id,
        keyword="davidzhu2021@163.com",
        page=1,
        page_size=10,
    )
    david = next(
        (
            item for item in members.get("items", [])
            if str(item.get("email") or "").casefold() == "davidzhu2021@163.com"
            and str(item.get("role") or "") == "admin"
            and str(item.get("status") or "") == "active"
            and str(item.get("upstreamUserId") or "")
        ),
        None,
    )
    if david is None:
        return False
    # Re-read every adopted asset before the grant. A changed upstream scope
    # means the original adoption proof is stale and credit must not be issued.
    backend_id = os.getenv("ORGANIZATION_ADOPTION_BACKEND_ID", "primary").strip() or "primary"
    backend = next((item for item in client().backends if item.id == backend_id), None)
    aliases = ["claude-code-lianghaiqiang", "cursor-lianghaiqiang"]
    organization = await store.get_organization(organization_id)
    departments = await store.list_departments(organization_id=organization_id)
    upstream_organization_id = str(
        (organization or {}).get("upstreamOrganizationId") or ""
    )
    upstream_team_ids = {
        str(item.get("upstreamTeamId") or "")
        for item in departments
        if str(item.get("upstreamTeamId") or "")
    }
    if backend is None or not aliases or not upstream_organization_id or len(upstream_team_ids) != 1:
        return False
    upstream_team_id = next(iter(upstream_team_ids))
    mappings = await store.usage_token_attribution_map()
    report_only_hashes = {
        str(item.get("upstreamKeyHash") or "").lower()
        for item in mappings
        if str(item.get("mode") or "") == "report_only"
        and str(item.get("organizationId") or "") == upstream_organization_id
        and str(item.get("teamId") or "") == upstream_team_id
        and item.get("billingEligible") is False
    }
    expected_hashes: set[str] = set()
    for alias in aliases:
        records = await client().list_keys_exact(key_alias=alias, backend=backend)
        if len(records) != 1:
            return False
        identity = client().report_only_key_identity(records[0])
        if (
            str(identity.get("organizationId") or "") != upstream_organization_id
            or str(identity.get("teamId") or "") != upstream_team_id
        ):
            return False
        key_hash = str(identity.get("hash") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", key_hash):
            return False
        expected_hashes.add(key_hash)
    if len(expected_hashes) != len(aliases) or not expected_hashes.issubset(
        report_only_hashes
    ):
        return False
    await store.adjust_billing(
        organization_id,
        operation="grant",
        amount_usd="5000.00",
        reason="北汽集团试点初始授信",
        operator="baic-pilot-reconciler",
        operator_email="",
        external_reference="BAIC-PILOT-INITIAL-5000",
        idempotency_key="baic-pilot-initial-credit-v1",
    )
    return True


async def organization_outbox_loop() -> None:
    while _organization_outbox_stop is not None and not _organization_outbox_stop.is_set():
        try:
            capability = await refresh_organization_capabilities()
            if capability.get("available"):
                await organization_outbox_once()
        except Exception:
            logger.exception("organization outbox worker failed")
        try:
            await asyncio.wait_for(_organization_outbox_stop.wait(), timeout=max(10, env_int("ORGANIZATION_OUTBOX_INTERVAL_SECONDS", 30)))
        except asyncio.TimeoutError:
            continue


async def start_organization_outbox_worker() -> None:
    global _organization_outbox_task, _organization_outbox_stop
    if _organization_outbox_task is not None and not _organization_outbox_task.done():
        return
    _organization_outbox_stop = asyncio.Event()
    _organization_outbox_task = asyncio.create_task(organization_outbox_loop(), name="organization-outbox-loop")


def require_real_organization_capability() -> None:
    if organization_real_enabled() and not _organization_capability_status.get("available"):
        raise auth_http_error(
            503,
            "企业组织能力暂不可用，请联系平台管理员检查上游数据库或许可配置",
            "ORGANIZATION_UPSTREAM_UNAVAILABLE",
        )


async def close_litellm_client() -> None:
    global _usage_sync_task, _usage_refresh_task, _usage_sync_stop, _organization_outbox_task, _organization_outbox_stop, _usage_realtime_task, _usage_realtime_worker
    if _usage_realtime_worker is not None:
        _usage_realtime_worker.stop_event.set()
    if _usage_realtime_task is not None:
        _usage_realtime_task.cancel()
        try:
            await _usage_realtime_task
        except asyncio.CancelledError:
            pass
        _usage_realtime_task = None
        _usage_realtime_worker = None
    if _organization_outbox_stop is not None:
        _organization_outbox_stop.set()
    if _organization_outbox_task is not None:
        _organization_outbox_task.cancel()
        try:
            await _organization_outbox_task
        except asyncio.CancelledError:
            pass
        _organization_outbox_task = None
        _organization_outbox_stop = None
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
    global _usage_realtime_store
    if _usage_realtime_store is not None:
        await _usage_realtime_store.close()
        _usage_realtime_store = None
    if billing_store() is not None:
        await billing_store().close()
    global _organization_store
    if _organization_store is not None:
        close = getattr(_organization_store, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result
        _organization_store = None
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


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
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

    enabled = organization_enabled()
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
    observability_enabled = bool(
        user.get("isPlatformAdmin")
        and env_bool("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", False)
    )
    return {
        "organizationEnabled": enabled,
        "organizationMode": organization_mode(),
        "organizationAvailable": bool(
            enabled
            and (
                organization_demo_enabled()
                or _organization_capability_status.get("available")
            )
        ),
        "organizationCapabilityStatus": str(
            _organization_capability_status.get("status") or "disabled"
        ),
        # Legacy browser bundles still read this field; real mode must not
        # advertise demo controls.
        "organizationDemoEnabled": organization_demo_enabled(),
        "isPlatformAdmin": bool(user.get("isPlatformAdmin")),
        "observabilityDashboardsEnabled": observability_enabled,
        "observabilityCapabilities": {
            "stabilityView": observability_enabled,
            "stabilityManage": observability_enabled,
            "costView": observability_enabled,
            "costManage": observability_enabled,
            "costReconcile": observability_enabled,
        },
        "organizationId": organization_id,
        # The customer name is safe display context for scoped boards. It lets
        # a customer admin identify their tenant without exposing the seller's
        # customer directory or enabling the master-data workspace.
        "organization": organization,
        "organizationName": organization_name or None,
        "organizationRole": role,
        "canViewOrganizationUsage": can_view_usage,
        "canViewOrganizationBilling": can_view_billing,
        "canSimulateOrganizationTopup": bool(organization_demo_enabled() and can_view_billing),
        "canManageOrganizationTokens": bool(enabled and role == "admin"),
        "canAdjustOrganizationCredit": bool(enabled and user.get("isPlatformAdmin")),
        "canManageOrganization": bool(enabled and role == "admin"),
        # Keep the explicit V2 capability separate from the legacy alias while
        # older browser bundles are still in circulation.
        "canManageCustomerOrganizations": bool(enabled and user.get("isPlatformAdmin")),
        "canManageCustomers": bool(enabled and user.get("isPlatformAdmin")),
        "isKnownOrganizationIdentity": bool(membership),
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
            "isKnownOrganizationIdentity": False,
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
        "isKnownOrganizationIdentity": True,
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
    """Resolve persisted memberships without granting seller-side access.

    Demo password accounts remain separate test principals. In real mode the
    invitation acceptance flow explicitly binds a password account, so that
    account is allowed to resolve its durable customer membership.
    """

    if (
        not organization_enabled()
        or (organization_demo_enabled() and user.get("authType") == "password")
        or is_platform_admin_email(str(user.get("email") or ""))
    ):
        return []
    try:
        if organization_real_enabled():
            # Real customer access is bound by invitation to a local account
            # id; matching only an email would silently create tenant access.
            local_user_id = str(user.get("id") or "").strip()
            if not local_user_id:
                return []
            result = await organization_store_call(
                "resolve_members_by_auth_user_id",
                local_user_id,
                _require_capability=False,
            )
        else:
            email = str(user.get("email") or "")
            # V2 store: a user may have at most one effective Mock customer.
            # The fallback preserves V1 tests until the store migration lands.
            try:
                result = await organization_store_call("resolve_members_by_email", email)
            except AttributeError:
                result = await organization_store_call("get_member_by_email", email)
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    return _organization_membership_items(result)


async def active_real_organization_membership(
    user: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the one active real-organization membership for this account."""

    if not organization_real_enabled():
        return None
    memberships = await organization_memberships_for_user(user)
    return next(
        (
            item
            for item in memberships
            if item.get("status") == "active"
            and item.get("organizationStatus", "active") == "active"
        ),
        None,
    )


async def organization_access_fields_for_user(user: dict[str, Any]) -> dict[str, Any]:
    """Resolve bootstrap capabilities without granting platform admins membership."""

    if not organization_enabled():
        return organization_access_fields(user)
    try:
        memberships = await organization_memberships_for_user(user)
    except HTTPException:
        if organization_real_enabled():
            raise
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
    if not organization_enabled():
        return {}
    return await organization_access_fields_for_user(user)


async def organization_user(request: Request) -> dict[str, Any]:
    """Require an active customer membership derived from the server session."""

    if not organization_enabled():
        raise HTTPException(status_code=404, detail="企业组织功能尚未启用")
    user = require_user(request)
    if organization_demo_enabled() and user.get("authType") == "password":
        raise auth_http_error(
            403,
            "演示企业账号只能通过企业统一认证登录",
            "ORGANIZATION_SSO_REQUIRED",
        )
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


def reject_direct_real_member_activation(status: Any) -> None:
    """Keep invitation activation exclusively in the upstream provisioning flow.

    In real mode an API caller may suspend or re-invite a member, but must not
    turn an invited row into an active membership by editing the local record.
    The provisioning worker is the only path that has completed the upstream
    user and organization/team membership checks before activation.
    """

    if organization_real_enabled() and str(status or "").strip().lower() == "active":
        raise auth_http_error(
            409,
            "真实企业成员必须接受邀请并完成上游开通后才能启用",
            "ORGANIZATION_MEMBER_ACTIVATION_REQUIRES_PROVISIONING",
        )


def reject_member_removal_via_update(status: Any) -> None:
    """Keep member removal on the dedicated DELETE route.

    Only that route revokes the member's tokens, voids pending invitations and
    unbinds the login account, so letting an edit write the tombstone status
    would leave a member who looks removed but still has upstream access.
    """

    if str(status or "").strip().lower() == MEMBER_REMOVED_STATUS:
        raise auth_http_error(
            400,
            "请通过删除成员操作移除成员",
            "ORGANIZATION_MEMBER_REMOVE_REQUIRED",
        )


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
        facade = facade_factory(organization_id)
        if inspect.isawaitable(facade):
            facade = await facade
        function = getattr(facade, method)
        if inspect.iscoroutinefunction(function):
            return await function(*args, **kwargs)
        return await asyncio.to_thread(function, *args, **kwargs)
    function = getattr(store, method)
    # Repository methods that take the tenant as their first positional
    # argument cannot also receive it as a keyword. Directory CRUD methods use
    # a keyword-only tenant to mirror the legacy protocol.
    positional_scope_methods = {
        "get_organization",
        "get_organization_snapshot",
        "organization_snapshot",
        "usage_cache_fingerprint",
        "billing_payload",
        "list_tokens",
        "revoke_token",
        "delete_token",
    }
    if isinstance(store, PostgreSQLOrganizationRepository) and method in positional_scope_methods:
        if inspect.iscoroutinefunction(function):
            return await function(organization_id, *args, **kwargs)
        return await asyncio.to_thread(function, organization_id, *args, **kwargs)
    call_kwargs = {**kwargs, "organization_id": organization_id}
    if inspect.iscoroutinefunction(function):
        try:
            return await function(*args, **call_kwargs)
        except TypeError:
            if organization_id == "org-demo":
                return await function(*args, **kwargs)
            raise
    try:
        return await asyncio.to_thread(function, *args, **call_kwargs)
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


def _filter_organization_usage_source(rows: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    if not source or source == "all":
        return rows
    return [row for row in rows if str(row.get("source") or "") == source]


def _usage_metric_row(day: str, source: str, model: str, metrics: dict[str, Any]) -> dict[str, Any]:
    prompt = int(metrics.get("prompt_tokens") or metrics.get("promptTokens") or 0)
    completion = int(metrics.get("completion_tokens") or metrics.get("completionTokens") or 0)
    total = int(metrics.get("total_tokens") or metrics.get("totalTokens") or prompt + completion)
    requests = int(metrics.get("api_requests") or metrics.get("request_count") or metrics.get("requestCount") or 0)
    failures = int(metrics.get("failed_requests") or metrics.get("failure_count") or metrics.get("failureCount") or 0)
    successes = int(metrics.get("successful_requests") or metrics.get("success_count") or metrics.get("successCount") or max(0, requests - failures))
    return {
        "date": day,
        "source": source or "其他",
        "model": normalize_model_display_name(model) or model or "未知模型",
        "promptTokens": prompt,
        "completionTokens": completion,
        "totalTokens": total,
        "requestCount": requests,
        "successCount": successes,
        "failureCount": failures,
        "spend": float(metrics.get("spend") or metrics.get("total_spend") or 0),
    }


def normalize_litellm_daily_usage(payload: dict[str, Any], source: str = "all") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in client().daily_usage_rows(payload):
        day = str(item.get("date") or item.get("day") or "")[:10]
        breakdown = item.get("breakdown") if isinstance(item.get("breakdown"), dict) else {}
        models = breakdown.get("models") if isinstance(breakdown.get("models"), dict) else {}
        if models:
            for model, value in models.items():
                metrics = value.get("metrics") if isinstance(value, dict) and isinstance(value.get("metrics"), dict) else value
                rows.append(_usage_metric_row(day, "其他", str(model), metrics if isinstance(metrics, dict) else {}))
            continue
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else item
        model = str(item.get("model") or item.get("model_group") or "全部模型")
        rows.append(_usage_metric_row(day, "其他", model, metrics))
    return _filter_organization_usage_source(rows, source)


async def real_organization_usage_payload(
    organization_id: str,
    *,
    start_date: str,
    end_date: str,
    source: str,
    employee: str = "",
    refresh: bool = False,
) -> dict[str, Any]:
    require_real_organization_capability()
    organization = await organization_scoped_store_call(organization_id, "get_organization")
    upstream_id = str((organization or {}).get("upstreamOrganizationId") or "")
    if not upstream_id or str((organization or {}).get("upstreamStatus") or "") != "active":
        raise auth_http_error(409, "企业账号仍在开通中，请稍后重试", "ORGANIZATION_PROVISIONING_PENDING")
    store = usage_store()
    if store is None:
        raise auth_http_error(
            503,
            "企业用量快照暂不可用，请等待同步完成后重试",
            "ORGANIZATION_USAGE_SNAPSHOT_UNAVAILABLE",
        )
    try:
        await store.connect()
        revision = await snapshot_revision(start_date, end_date)
        stored = await store.organization_rows(
            upstream_id,
            start_date,
            end_date,
            source,
            usage_backend_ids(),
            employee=employee,
        )
    except Exception as exc:
        logger.exception(
            "organization usage snapshot query failed organization_id=%s upstream_id=%s",
            organization_id,
            upstream_id,
        )
        raise auth_http_error(
            503,
            "企业用量快照暂不可用，请稍后重试",
            "ORGANIZATION_USAGE_SNAPSHOT_UNAVAILABLE",
        ) from exc
    if stored is None:
        raise auth_http_error(
            503,
            "企业用量仍在同步中，请稍后重试",
            "ORGANIZATION_USAGE_SNAPSHOT_UNAVAILABLE",
        )
    last_synced = stored.get("lastSyncedAt")
    stored["organization"] = organization
    attach_snapshot_freshness(stored,
        last_synced if isinstance(last_synced, datetime) else None,
        start_date,
        end_date,
        revision,
    )
    stored["cache"] = {"hit": False, "ttlSeconds": 0}
    return stored


async def real_organization_department_usage_payload(
    organization_id: str,
    *,
    start_date: str,
    end_date: str,
    source: str,
    department: str = "",
    refresh: bool = False,
) -> dict[str, Any]:
    require_real_organization_capability()
    organization = await organization_scoped_store_call(organization_id, "get_organization")
    upstream_organization_id = str((organization or {}).get("upstreamOrganizationId") or "")
    if not organization or str(organization.get("upstreamStatus") or "") != "active":
        raise auth_http_error(409, "企业账号仍在开通中，请稍后重试", "ORGANIZATION_PROVISIONING_PENDING")
    store = usage_store()
    if store is None:
        raise auth_http_error(
            503,
            "部门用量快照暂不可用，请等待同步完成后重试",
            "ORGANIZATION_USAGE_SNAPSHOT_UNAVAILABLE",
        )
    try:
        await store.connect()
        revision = await snapshot_revision(start_date, end_date)
        stored = await store.organization_rows(
            upstream_organization_id,
            start_date,
            end_date,
            source,
            usage_backend_ids(),
        )
    except Exception as exc:
        logger.exception(
            "organization department snapshot query failed organization_id=%s upstream_id=%s",
            organization_id,
            upstream_organization_id,
        )
        raise auth_http_error(
            503,
            "部门用量快照暂不可用，请稍后重试",
            "ORGANIZATION_USAGE_SNAPSHOT_UNAVAILABLE",
        ) from exc
    if stored is None:
        raise auth_http_error(
            503,
            "部门用量仍在同步中，请稍后重试",
            "ORGANIZATION_USAGE_SNAPSHOT_UNAVAILABLE",
        )
    last_synced = stored.get("lastSyncedAt")
    attach_snapshot_freshness(
        stored,
        last_synced if isinstance(last_synced, datetime) else None,
        start_date,
        end_date,
        revision,
    )
    departments = list(stored.get("departments") or [])
    rows = list(stored.get("rows") or [])
    local_departments = await organization_scoped_store_call(
        organization_id,
        "list_departments",
        include_archived=False,
    )
    usage_by_id = {
        str(item.get("departmentId") or ""): item for item in departments
    }
    department_options = []
    for item in local_departments:
        upstream_team_id = str(item.get("upstreamTeamId") or "")
        if not upstream_team_id:
            continue
        option = {
            "departmentKey": department_key(upstream_team_id, str(item.get("name") or upstream_team_id)),
            "departmentId": upstream_team_id,
            "departmentName": str(item.get("name") or upstream_team_id),
            "organizationId": upstream_organization_id,
            "status": "active",
            "promptTokens": 0,
            "completionTokens": 0,
            "totalTokens": 0,
            "requestCount": 0,
            "successCount": 0,
            "failureCount": 0,
            "spend": 0.0,
            "primarySource": "",
            "activeEmployees": 0,
        }
        option.update(usage_by_id.get(upstream_team_id, {}))
        option["departmentName"] = str(item.get("name") or option["departmentName"])
        option["departmentKey"] = department_key(upstream_team_id, option["departmentName"])
        department_options.append(option)
    matched_ids: set[str] = set()
    if department:
        matched_ids = {
            str(item.get("departmentId") or "") for item in department_options
            if department in {
                str(item.get("departmentId") or ""),
                str(item.get("departmentKey") or ""),
                str(item.get("departmentName") or ""),
            }
        }
        departments = [
            item for item in departments
            if str(item.get("departmentId") or "") in matched_ids
        ]
        rows = [
            item for item in rows
            if str(item.get("departmentId") or "") in matched_ids
        ]
        if not matched_ids:
            departments = []
            rows = []
    last_synced = stored.get("lastSyncedAt")
    return {
        **stored,
        "rows": rows,
        "summaryRows": UsageStore._group_rows(rows, ("date", "source", "model")),
        "departments": departments,
        "departmentOptions": department_options,
        "department": department,
        "totalRecords": len(rows),
        "dataFreshness": stored["dataFreshness"],
        "cache": {"hit": False, "ttlSeconds": 0},
    }


async def require_platform_organization(
    request: Request,
    organization_id: str,
    *,
    require_capability: bool = True,
) -> dict[str, Any]:
    """Authorize a seller operator and resolve exactly the requested customer."""

    if not organization_enabled():
        raise HTTPException(status_code=404, detail="企业组织功能尚未启用")
    user = require_platform_admin(request)
    try:
        organization = await platform_organization_store_call(
            "get_organization",
            organization_id,
            _require_capability=require_capability,
        )
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


async def require_platform_claim_organization(
    request: Request, organization_id: str
) -> dict[str, Any]:
    """Resolve a platform customer for local claim operations.

    Claim approval/revocation is a durable local decision and must remain
    available while the upstream provisioning capability is recovering. The
    compatibility fallback keeps older test doubles and integrations working.
    """

    try:
        return await require_platform_organization(
            request, organization_id, require_capability=False
        )
    except TypeError as exc:
        if "require_capability" not in str(exc):
            raise
        return await require_platform_organization(request, organization_id)


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
    """True only for an active customer in the explicit demo runtime."""

    if not organization_demo_enabled():
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

    if not organization_demo_enabled():
        return False
    try:
        return bool(await organization_memberships_for_user(app_user))
    except HTTPException:
        return False


async def require_non_inactive_demo_identity(app_user: dict[str, Any]) -> None:
    """Fail closed before any non-organization path can resolve a disabled customer.

    Active customer identities are handled by their organization-scoped paths.
    Invited, suspended, and archived identities must not fall through to a
    same-email platform account or an unrelated upstream usage lookup.
    """

    if not organization_enabled():
        return
    try:
        memberships = await organization_memberships_for_user(app_user)
    except HTTPException:
        # Preserve an upstream capability error instead of treating the user
        # as an unrelated non-organization account.
        if organization_real_enabled():
            raise
        memberships = []
    if any(
        item.get("status") == "active"
        and item.get("organizationStatus", "active") == "active"
        for item in memberships
    ):
        return
    if memberships or await is_known_demo_customer_identity(app_user):
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
        if organization_real_enabled():
            raise auth_http_error(503, "模型目录暂不可用，当前不能创建企业 Token", "ORGANIZATION_MODEL_CATALOG_UNAVAILABLE")
        return ORGANIZATION_TOKEN_MODELS
    except Exception:
        if organization_real_enabled():
            logger.exception("organization token model catalog unavailable")
            raise auth_http_error(503, "模型目录暂不可用，当前不能创建企业 Token", "ORGANIZATION_MODEL_CATALOG_UNAVAILABLE")
        logger.warning("organization token model catalog unavailable; using the built-in list")
        return ORGANIZATION_TOKEN_MODELS
    catalog = tuple(name for name in names if name)
    if not catalog and organization_real_enabled():
        raise auth_http_error(503, "模型目录暂不可用，当前不能创建企业 Token", "ORGANIZATION_MODEL_CATALOG_UNAVAILABLE")
    return catalog or ORGANIZATION_TOKEN_MODELS


async def create_real_organization_token(
    organization_id: str,
    data: "OrganizationTokenCreateRequest",
    catalog: tuple[str, ...],
    *,
    changed_by: str = "",
) -> dict[str, Any]:
    """Provision a durable local token and its LiteLLM key atomically enough to retry.

    The local row is intentionally created first in ``provisioning`` state.  A
    failed upstream request therefore never looks like an active credential;
    retries can find the row by its stable alias and either finalize it or
    clean up the orphan upstream key.
    """

    require_real_organization_capability()
    selected_models = list(dict.fromkeys(model.strip() for model in data.models))
    unknown = [model for model in selected_models if model not in catalog]
    if unknown:
        raise auth_http_error(400, "所选模型当前不可用", "ORGANIZATION_TOKEN_MODEL_UNAVAILABLE")
    store = organization_store()
    if not isinstance(store, PostgreSQLOrganizationRepository):
        raise auth_http_error(503, "企业 Token 持久化能力暂不可用", "ORGANIZATION_TOKEN_STORE_UNAVAILABLE")
    member_id = str(data.memberId or "").strip()
    member: dict[str, Any] | None = None
    if member_id:
        member = await store.get_member(member_id, organization_id=organization_id)
        if not member or member.get("status") != "active":
            raise auth_http_error(409, "只能为已启用成员创建 Token", "ORGANIZATION_TOKEN_MEMBER_INACTIVE")
        if not str(member.get("upstreamUserId") or "").strip():
            raise auth_http_error(503, "成员上游账号仍在开通中", "ORGANIZATION_MEMBER_PROVISIONING")
    organization = await store.get_organization(organization_id)
    upstream_org_id = str((organization or {}).get("upstreamOrganizationId") or "").strip()
    if not upstream_org_id:
        raise auth_http_error(503, "企业上游账号仍在开通中", "ORGANIZATION_UPSTREAM_PROVISIONING")
    if str((organization or {}).get("billingStatus") or "past_due") != "active":
        raise auth_http_error(
            409,
            "企业额度不足或尚未生效，当前不能创建新的企业 Token",
            "ORGANIZATION_BILLING_INACTIVE",
        )
    upstream_team_id = ""
    if member:
        department = await store.get_department(
            str(member.get("departmentId") or ""), organization_id=organization_id
        )
        upstream_team_id = str((department or {}).get("upstreamTeamId") or "")
    expires_at = None
    if data.duration != "never":
        expires_at = datetime.now(timezone.utc) + timedelta(days=int(data.duration[:-1]))
    # Alias is deterministic for the requested scope and payload, allowing a
    # retried request to recover a key created before a local timeout.
    alias_seed = json.dumps(
        [organization_id, member_id, data.name.strip(), selected_models, str(data.dailyBudgetUsd), data.duration],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    alias = "ai-org-" + hashlib.sha256(alias_seed.encode("utf-8")).hexdigest()[:24]
    existing = await store.get_token_by_alias(alias)
    if existing and existing.get("status") == "active":
        # The secret was intentionally never persisted; an idempotent retry
        # can only return the masked durable projection.
        return {"token": existing}
    if existing:
        token_record = existing
    else:
        try:
            token_record = await store.create_token_record(
                organization_id,
                data.name,
                selected_models,
                member_id=member_id,
                daily_budget_usd=data.dailyBudgetUsd,
                duration=data.duration,
                expires_at=expires_at,
                upstream_key_alias=alias,
            )
        except OrganizationConflictError:
            # A concurrent process may have inserted the same stable alias
            # after the optimistic lookup. Reuse that durable row; the alias
            # remains the only cross-process idempotency primitive because
            # LiteLLM 1.92 ignores the HTTP Idempotency-Key header.
            token_record = await store.get_token_by_alias(alias)
            if not token_record:
                raise
            if token_record.get("status") == "active":
                return {"token": token_record}
            # Another request owns the upstream provisioning attempt for this
            # alias. Do not issue a second key while that durable row is still
            # pending reconciliation.
            raise auth_http_error(
                409,
                "相同请求的企业 Token 正在开通中，请稍后刷新列表",
                "ORGANIZATION_TOKEN_PROVISIONING",
            )
    try:
        upstream = await client().create_organization_key(
            upstream_org_id,
            key_alias=alias,
            models=selected_models,
            daily_budget_usd=float(data.dailyBudgetUsd),
            team_id=upstream_team_id or None,
            user_id=str((member or {}).get("upstreamUserId") or "") or None,
            duration=data.duration,
            changed_by=changed_by,
            idempotency_key=alias,
        )
    except Exception as exc:
        # A timeout/transport error can occur after the proxy committed the
        # key. Reconcile every uncertain create by stable alias; the worker
        # will either recover the secret-free projection or delete an orphan.
        if isinstance(exc, HTTPException) and exc.status_code < 500:
            # A stable alias may already exist upstream after a previous
            # request timed out. Treat the proxy's duplicate response as an
            # uncertain outcome and let durable reconciliation recover it.
            if exc.status_code != 409:
                raise
        try:
            await store.enqueue_token_reconciliation(
                organization_id,
                str(token_record["id"]),
                upstream_organization_id=upstream_org_id,
                upstream_team_id=upstream_team_id,
                upstream_user_id=str((member or {}).get("upstreamUserId") or ""),
                upstream_key_alias=alias,
            )
        except Exception:
            logger.exception(
                "failed to enqueue timed-out organization token reconciliation token_id=%s",
                token_record.get("id"),
            )
        raise auth_http_error(
            503,
            "企业 Token 开通结果暂未确认，正在自动对账，请稍后刷新列表",
            "ORGANIZATION_TOKEN_RECONCILIATION_PENDING",
        ) from exc
    secret = str(upstream.get("key") or "")
    token_id = str(token_record["id"])
    try:
        finalized = await store.finalize_token_record(
            token_id,
            upstream_key_id=str(upstream.get("id") or ""),
            upstream_key_hash=hashlib.sha256(secret.encode("utf-8")).hexdigest(),
            status="active",
            plaintext_token=secret,
        )
    except Exception as exc:
        # The key may already exist upstream even though the local commit
        # failed. Persist only stable identifiers so the outbox can recover or
        # revoke it without ever storing the plaintext credential.
        try:
            await store.enqueue_token_reconciliation(
                organization_id,
                token_id,
                upstream_organization_id=upstream_org_id,
                upstream_team_id=upstream_team_id,
                upstream_user_id=str((member or {}).get("upstreamUserId") or ""),
                upstream_key_alias=alias,
            )
        except Exception:
            logger.exception(
                "failed to enqueue organization token reconciliation token_id=%s",
                token_id,
            )
        # A rejected CAS means eligibility changed while the upstream key was
        # being issued; remove it immediately. Transient database failures are
        # left for reconciliation so a valid key is not deleted accidentally.
        if isinstance(exc, OrganizationConflictError):
            try:
                upstream_id = str(upstream.get("id") or "").strip()
                if upstream_id:
                    await client().revoke_organization_key(
                        upstream_id,
                        changed_by=changed_by,
                        idempotency_key=f"finalize-failed:{token_id}",
                    )
            except Exception:
                logger.exception(
                    "failed immediate cleanup after organization token finalize token_id=%s",
                    token_id,
                )
        raise auth_http_error(
            503,
            "企业 Token 已提交开通，正在自动对账，请稍后刷新列表",
            "ORGANIZATION_TOKEN_RECONCILIATION_PENDING",
        ) from exc
    return {"token": finalized}


async def revoke_real_organization_token(
    organization_id: str,
    token_id: str,
    *,
    changed_by: str = "",
) -> dict[str, Any]:
    """Revoke upstream first, then mark the tenant-scoped local record."""

    require_real_organization_capability()
    store = organization_store()
    if not isinstance(store, PostgreSQLOrganizationRepository):
        raise auth_http_error(503, "企业 Token 持久化能力暂不可用", "ORGANIZATION_TOKEN_STORE_UNAVAILABLE")
    token = await store.get_token(organization_id, token_id)
    if not token:
        raise auth_http_error(404, "未找到对应的 Token", "ORGANIZATION_TOKEN_NOT_FOUND")
    upstream_id = str(token.get("upstreamKeyId") or "").strip()
    if not upstream_id:
        organization = await store.get_organization(organization_id)
        upstream_org_id = str((organization or {}).get("upstreamOrganizationId") or "").strip()
        alias = str(token.get("upstreamKeyAlias") or "").strip()
        if upstream_org_id and alias:
            upstream_record = await client().find_organization_key_by_alias(
                upstream_org_id,
                alias,
                team_id=str(token.get("upstreamTeamId") or "").strip() or None,
            )
            if upstream_record is not None:
                upstream_id = str(
                    client().organization_key_identity(upstream_record).get("id") or ""
                ).strip()
        if not upstream_id:
            await store.enqueue_token_reconciliation(
                organization_id,
                token_id,
                upstream_organization_id=upstream_org_id,
                upstream_team_id=str(token.get("upstreamTeamId") or ""),
                upstream_key_alias=alias,
            )
            raise auth_http_error(
                409,
                "Token 开通结果正在确认，系统会在确认后自动撤销",
                "ORGANIZATION_TOKEN_PROVISIONING",
            )
    # Keep retries idempotent: if the upstream delete succeeded but the local
    # transaction failed, a later attempt treats the upstream 404 as already
    # revoked and can safely converge the local record.
    await client().revoke_organization_key(
        upstream_id,
        changed_by=changed_by,
        idempotency_key=f"organization-token-revoke:{organization_id}:{token_id}",
    )
    return await store.mark_token_revoked(organization_id, token_id)


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
    message = MIMEText(body, "plain", "utf-8")
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False, usegmt=True)
    message_id_domain = os.getenv("SMTP_MESSAGE_ID_DOMAIN", "example.com").strip() or "example.com"
    message["Message-ID"] = make_msgid(domain=message_id_domain)
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
    account_type = str(user.get("account_type") or user.get("accountType") or "personal")
    account_status = str((account or {}).get("status") or "provisioning")
    if account_type == "enterprise_managed":
        # Managed identities are provisioned through customer_member. They do
        # not create or depend on the personal auth_upstream_accounts mapping.
        account_status = (
            "provisioned"
            if str(user.get("status") or "") == "active"
            else str(user.get("status") or "provisioning")
        )
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
    # 客户企业成员的权限和资料都来自成员关系，不是个人注册时那个从未授予过模型的
    # 上游账号。用户名账号根本没有个人映射，而邮箱注册的账号被平台管理员绑定到成员
    # 之后同样如此，所以这里对两种账号类型一视同仁。
    member = await provisioned_member_for_account(user)
    if member is not None:
        entitlement_status = "active"
    contact_email = str(user.get("email") or user.get("contactEmail") or "").strip().lower()
    login_name = str(user.get("login_name") or user.get("loginName") or "").strip()
    display_identifier = contact_email or login_name
    # 成员花名册由企业管理员维护，比注册时自己填的名字更权威；换绑登录账号后也不
    # 该继续顶着上一个账号的名字。
    member_name = str((member or {}).get("name") or "").strip()
    normalized = normalize_user(display_identifier, member_name or str(user.get("name") or ""))
    # Username-only managed accounts must not leak a fabricated email into
    # auth/me or downstream authorization checks.
    normalized["email"] = contact_email or None
    # Password accounts are intentionally separate from SSO identities, even
    # when they use the same email address. Do not inherit admin privileges.
    normalized["isAdmin"] = False
    normalized["isPlatformAdmin"] = False
    return {
        **normalized,
        "id": str(user["id"]),
        "authType": "password",
        "loginName": login_name or None,
        "contactEmail": contact_email or None,
        "displayIdentifier": display_identifier,
        "accountType": account_type,
        "identityStatus": str(user.get("identity_status") or user.get("identityStatus") or "verified"),
        "identityVerifiedAt": user.get("identity_verified_at") or user.get("identityVerifiedAt"),
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


async def provisioned_member_for_account(user: dict[str, Any]) -> dict[str, Any] | None:
    """Return the active customer membership that provisions this account.

    Membership is the grant: a member counts as provisioned once the customer
    tenant is adopted upstream and the account owns at least one usage identity
    there.  It is also the authoritative profile — a customer administrator
    maintains the member roster, not the name typed at signup.
    """

    user_id = str(user.get("id") or "")
    if not organization_real_enabled() or not user_id:
        return None
    try:
        memberships = await organization_store_call(
            "resolve_members_by_auth_user_id", user_id, _require_capability=False
        )
    except Exception:
        return None
    return next(
        (
            item
            for item in _organization_membership_items(memberships)
            if item.get("status") == "active"
            and item.get("organizationStatus", "active") == "active"
            # 历史用量身份和建档时生成的上游 id 都算数：老成员可能只有前者。
            and (str(item.get("upstreamUserId") or "") or list(item.get("principalIds") or []))
        ),
        None,
    )


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


def personal_usage_cache_key(email: str, start_date: str, end_date: str, source: str, revision: str = "") -> str:
    return f"usage:v8:{revision}:{email.strip().lower()}:{start_date}:{end_date}:{source or 'all'}"


def local_personal_usage_cache_key(user_id: str, start_date: str, end_date: str, source: str, revision: str = "") -> str:
    return f"usage:local:v3:{revision}:{user_id}:{start_date}:{end_date}:{source or 'all'}"


def admin_usage_cache_key(email: str, start_date: str, end_date: str, source: str, employee: str | None, revision: str = "") -> str:
    return f"admin-usage:v6:{revision}:{email.strip().lower()}:{start_date}:{end_date}:{source or 'all'}:{(employee or '').strip().lower()}"


def department_usage_cache_key(email: str, start_date: str, end_date: str, source: str, department: str | None, revision: str = "") -> str:
    return f"department-usage:v7:{revision}:{email.strip().lower()}:{start_date}:{end_date}:{source or 'all'}:{(department or '').strip().lower()}"


def team_auth_cache_key(email: str, name: str | None, revision: str = "") -> str:
    return f"team-auth:v4:{revision}:{email.strip().lower()}:{str(name or '').strip()}"


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


def team_usage_cache_key(email: str, team: dict[str, Any], start_date: str, end_date: str, source: str, revision: str = "") -> str:
    return f"team-usage:v10:{revision}:{email.strip().lower()}:{team_scope_fingerprint(team)}:{start_date}:{end_date}:{source or 'all'}"


def team_member_usage_cache_key(email: str, team: dict[str, Any], employee: str, start_date: str, end_date: str, source: str, revision: str = "") -> str:
    return f"team-member-usage:v9:{revision}:{email.strip().lower()}:{team_scope_fingerprint(team)}:{employee.strip().lower()}:{start_date}:{end_date}:{source or 'all'}"


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


async def _personal_usage_payload(app_user: dict[str, Any], start_date: str, end_date: str, source: str, refresh: bool = False) -> dict[str, Any]:
    account_type = str(
        app_user.get("accountType") or app_user.get("account_type") or "personal"
    )
    if organization_real_enabled():
        memberships = await organization_memberships_for_user(app_user)
        membership = next(
            (
                item
                for item in memberships
                if item.get("status") == "active"
                and item.get("organizationStatus", "active") == "active"
            ),
            None,
        )
        if membership is not None:
            return await real_organization_member_usage_payload(
                app_user,
                membership,
                start_date=start_date,
                end_date=end_date,
                source=source,
                refresh=refresh,
            )
    if account_type == "enterprise_managed":
        # Username-only managed identities never fall through to the personal
        # upstream account path. Until their customer membership is active,
        # return an explicit empty/provisioning view instead of querying by a
        # fabricated or missing email address.
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
            "organizationAccessStatus": app_user.get(
                "organizationAccessStatus", "provisioning"
            ),
            "mappingCache": {"hit": True, "ttlSeconds": 0},
            "cache": {"hit": False, "ttlSeconds": 0},
        }
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
    revision = "development-upstream"
    fallback_key = personal_usage_cache_key(app_user["email"], start_date, end_date, source)
    refresh_queued = queue_usage_refresh(start_date, end_date, refresh)
    if usage_store() is not None:
        try:
            revision = await snapshot_revision(start_date, end_date)
        except HTTPException as exc:
            if exc.status_code == 503:
                cached = degraded_cached_usage_payload(
                    fallback_key, refresh_queued=refresh_queued
                )
                if cached is not None:
                    return cached
            raise
    cache_key = personal_usage_cache_key(app_user["email"], start_date, end_date, source, revision)
    if cached := usage_payload_from_cache(
        personal_usage_cache,
        cache_key,
        refresh_queued=refresh_queued,
    ):
        return cached

    store = usage_store()
    if store is not None:
        try:
            db_started = asyncio.get_running_loop().time()
            await store.connect()
            connected_at = asyncio.get_running_loop().time()
            stored = await store.personal_rows(app_user["email"], start_date, end_date, source, usage_backend_ids())
            queried_at = asyncio.get_running_loop().time()
            logger.info("personal usage sql refresh=%s connect_ms=%.0f query_ms=%.0f total_ms=%.0f", refresh, (connected_at - db_started) * 1000, (queried_at - connected_at) * 1000, (queried_at - request_started) * 1000)
            if stored is not None:
                rows = stored["rows"]
                payload = attach_snapshot_freshness({
                    "user": app_user,
                    "startDate": start_date,
                    "endDate": end_date,
                    "source": source,
                    "rows": rows,
                    "summary": usage_summary(rows),
                    "mappingCache": {"hit": True, "ttlSeconds": 0},
                }, stored.get("lastSyncedAt"), start_date, end_date, revision)
                cache_usage_payload(
                    personal_usage_cache,
                    cache_key,
                    fallback_key,
                    payload,
                    env_int("PERSONAL_USAGE_CACHE_TTL_SECONDS", 300),
                )
                payload["cache"] = {"hit": False, "ttlSeconds": 0}
                if refresh_queued:
                    payload["refreshQueued"] = True
                return payload
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("local personal usage query failed")
            if snapshot_reader_configured():
                cached = degraded_cached_usage_payload(
                    fallback_key, refresh_queued=refresh_queued
                )
                if cached is not None:
                    return cached
                raise manual_refresh_database_unavailable() from exc

        if snapshot_reader_configured():
            raise HTTPException(
                status_code=503,
                detail="个人用量快照尚未就绪，请等待后台同步完成",
            )

    if refresh and snapshot_reader_configured():
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
    cache_usage_payload(
        personal_usage_cache,
        cache_key,
        fallback_key,
        payload,
        env_int("PERSONAL_USAGE_CACHE_TTL_SECONDS", 300),
    )
    payload = dict(payload)
    payload["cache"] = {"hit": False, "ttlSeconds": 0}
    return payload


async def personal_usage_payload(
    app_user: dict[str, Any],
    start_date: str,
    end_date: str,
    source: str,
    refresh: bool = False,
) -> dict[str, Any]:
    identity = str(app_user.get("id") or app_user.get("email") or "anonymous").lower()
    key = f"personal:{identity}:{start_date}:{end_date}:{source}:{int(refresh)}"
    return await usage_singleflight(
        key,
        lambda: _personal_usage_payload(app_user, start_date, end_date, source, refresh),
    )


async def real_organization_member_usage_payload(
    app_user: dict[str, Any],
    membership: dict[str, Any],
    *,
    start_date: str,
    end_date: str,
    source: str,
    refresh: bool = False,
) -> dict[str, Any]:
    """Read a real member without using email as a tenant or identity key."""

    require_real_organization_capability()
    organization_id = organization_identifier(membership)
    organization = membership.get("organization")
    upstream_organization_id = str(
        (organization or {}).get("upstreamOrganizationId")
        if isinstance(organization, dict)
        else ""
    ).strip()
    upstream_user_id = str(membership.get("upstreamUserId") or "").strip()
    upstream_user_ids = [upstream_user_id] if upstream_user_id else []
    # Spend earned before the tenant was adopted is attributed to a usage
    # identity rather than to the synthetic upstream id minted at signup, and it
    # commonly spans several upstream ids, so both must be queried together.
    principal_ids = [
        str(item).strip()
        for item in (membership.get("principalIds") or [])
        if str(item).strip()
    ]
    if not upstream_organization_id or not (upstream_user_ids or principal_ids):
        raise auth_http_error(
            409,
            "企业账号仍在开通中，请稍后重试",
            "ORGANIZATION_MEMBER_PROVISIONING",
        )
    store = usage_store()
    if store is None:
        raise auth_http_error(
            503,
            "企业成员用量暂不可用，请稍后重试",
            "ORGANIZATION_USAGE_UNAVAILABLE",
        )
    try:
        await store.connect()
        revision = await snapshot_revision(start_date, end_date)
        stored = await store.organization_identity_rows(
            upstream_organization_id,
            upstream_user_ids,
            principal_ids,
            start_date,
            end_date,
            source,
            usage_backend_ids(),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "organization member usage query failed organization_id=%s member_id=%s",
            organization_id,
            membership.get("id"),
        )
        raise auth_http_error(
            503,
            "企业成员用量暂不可用，请稍后重试",
            "ORGANIZATION_USAGE_UNAVAILABLE",
        ) from exc
    if stored is None:
        raise auth_http_error(
            503,
            "企业成员用量快照尚未就绪，请稍后重试",
            "ORGANIZATION_USAGE_UNAVAILABLE",
        )
    rows = list(stored.get("rows") or [])
    return {
        "user": app_user,
        "startDate": start_date,
        "endDate": end_date,
        "source": source,
        "rows": rows,
        "summary": usage_summary(rows),
        "mappingCache": {"hit": True, "ttlSeconds": 0},
        "dataFreshness": {
            **usage_data_freshness(
            stored.get("lastSyncedAt"), start_date, end_date
            ),
            "snapshotRevision": revision,
        },
        "dataQuality": {
            "summarySource": "database",
            "organizationScoped": True,
            "memberIdentityMatch": "principal" if principal_ids else "upstream_user_id",
        },
        "cache": {"hit": False, "ttlSeconds": 0},
    }


async def local_personal_usage_payload(
    app_user: dict[str, Any],
    start_date: str,
    end_date: str,
    source: str,
    refresh: bool = False,
) -> dict[str, Any]:
    """Read password-account usage only from its provisioned upstream identity."""
    local_user_id = str(app_user["id"])
    revision = "development-upstream"
    if usage_store() is not None:
        revision = await snapshot_revision(start_date, end_date)
    cache_key = local_personal_usage_cache_key(local_user_id, start_date, end_date, source, revision)
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
    store = usage_store()
    if store is None:
        if refresh:
            raise manual_refresh_database_unavailable()
        rows = await client().usage_rows_for_user_ids([upstream_user_id], start_date, end_date, source)
        last_synced = None
    else:
        await store.connect()
        stored = await store.personal_rows_by_user_ids(
            [upstream_user_id], start_date, end_date, source, usage_backend_ids()
        )
        if stored is None:
            raise HTTPException(status_code=503, detail="个人用量快照尚未就绪，请等待后台同步完成")
        rows = stored["rows"]
        last_synced = stored.get("lastSyncedAt")
    payload = {
        "user": app_user,
        "startDate": start_date,
        "endDate": end_date,
        "source": source,
        "rows": rows,
        "summary": usage_summary(rows),
        "mappingCache": {"hit": True, "ttlSeconds": 0},
    }
    if store is not None:
        attach_snapshot_freshness(payload, last_synced, start_date, end_date, revision)
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
        if snapshot_reader_configured():
            raise HTTPException(status_code=503, detail="用量快照尚未就绪，请等待后台同步完成")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("batched employee usage SQL query failed")
        if snapshot_reader_configured():
            raise manual_refresh_database_unavailable() from exc
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
    revision = "development-upstream"
    if usage_store() is not None:
        revision = await snapshot_revision(start_date, end_date)
    cache_key = admin_usage_cache_key(admin["email"], start_date, end_date, source, employee, revision)
    if not refresh:
        hit, value, ttl_seconds = admin_usage_cache.get(cache_key)
        if hit:
            payload = dict(value)
            payload["cache"] = {"hit": True, "ttlSeconds": ttl_seconds}
            logger.info("admin usage cache hit records=%s", len(payload.get("rows") or []))
            return payload

    async def load() -> dict[str, Any]:
        if not refresh:
            hit, value, ttl_seconds = admin_usage_cache.get(cache_key)
            if hit:
                payload = dict(value)
                payload["cache"] = {"hit": True, "ttlSeconds": ttl_seconds}
                return payload
        store = usage_store()
        if store is None:
            payload = await client().admin_usage_rows(start_date, end_date, source, employee)
            admin_usage_cache.set(cache_key, payload, env_int("ADMIN_USAGE_CACHE_TTL_SECONDS", 300))
            payload = dict(payload)
            payload["cache"] = {"hit": False, "ttlSeconds": 0}
            return payload
        try:
            db_started = asyncio.get_running_loop().time()
            await store.connect()
            connected_at = asyncio.get_running_loop().time()
            stored = await store.admin_rows(start_date, end_date, source, employee, usage_backend_ids())
            queried_at = asyncio.get_running_loop().time()
            logger.info("admin usage sql refresh=%s connect_ms=%.0f query_ms=%.0f total_ms=%.0f", refresh, (connected_at - db_started) * 1000, (queried_at - connected_at) * 1000, (queried_at - request_started) * 1000)
            if stored is not None:
                stored = dict(stored)
                last_synced = stored.pop("lastSyncedAt", None)
                attach_snapshot_freshness(stored, last_synced, start_date, end_date, revision)
                admin_usage_cache.set(cache_key, stored, env_int("ADMIN_USAGE_CACHE_TTL_SECONDS", 300))
                stored["cache"] = {"hit": False, "ttlSeconds": 0}
                return stored
        except Exception as exc:
            logger.exception("local admin usage query failed")
            raise manual_refresh_database_unavailable() from exc
        raise HTTPException(status_code=503, detail="全员用量快照尚未就绪，请等待后台同步完成")

    return await usage_singleflight(cache_key, load)


async def department_usage_payload(admin: dict[str, Any], start_date: str, end_date: str, source: str, department: str | None, refresh: bool = False) -> dict[str, Any]:
    request_started = asyncio.get_running_loop().time()
    revision = "development-upstream"
    if usage_store() is not None:
        revision = await snapshot_revision(start_date, end_date)
    cache_key = department_usage_cache_key(admin["email"], start_date, end_date, source, department, revision)
    if not refresh:
        hit, value, ttl_seconds = department_usage_cache.get(cache_key)
        if hit:
            payload = dict(value)
            payload["cache"] = {"hit": True, "ttlSeconds": ttl_seconds}
            logger.info("department usage cache hit records=%s", len(payload.get("rows") or []))
            return payload

    async def load() -> dict[str, Any]:
        if not refresh:
            hit, value, ttl_seconds = department_usage_cache.get(cache_key)
            if hit:
                payload = dict(value)
                payload["cache"] = {"hit": True, "ttlSeconds": ttl_seconds}
                return payload
        store = usage_store()
        if store is None:
            payload = await client().admin_department_usage_rows(start_date, end_date, source, department)
            department_usage_cache.set(cache_key, payload, env_int("DEPARTMENT_USAGE_CACHE_TTL_SECONDS", 300))
            payload = dict(payload)
            payload["cache"] = {"hit": False, "ttlSeconds": 0}
            return payload
        try:
            db_started = asyncio.get_running_loop().time()
            await store.connect()
            connected_at = asyncio.get_running_loop().time()
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
                attach_snapshot_freshness(stored, last_synced, start_date, end_date, revision)
                department_usage_cache.set(cache_key, stored, env_int("DEPARTMENT_USAGE_CACHE_TTL_SECONDS", 300))
                stored["cache"] = {"hit": False, "ttlSeconds": 0}
                return stored
        except Exception as exc:
            logger.exception("local department usage query failed")
            raise manual_refresh_database_unavailable() from exc
        raise HTTPException(status_code=503, detail="部门用量快照尚未就绪，请等待后台同步完成")

    return await usage_singleflight(cache_key, load)


def empty_team_scope() -> dict[str, Any]:
    return {"isTeamLeader": False, "teamBoardStatus": "none", "team": None, "leaderTeams": []}


def public_team_scope(scope: dict[str, Any]) -> dict[str, Any]:
    """Strip internal scope handles (upstream team ids) from a scope response."""

    return {
        "isTeamLeader": bool(scope.get("isTeamLeader")),
        "teamBoardStatus": str(scope.get("teamBoardStatus") or "none"),
        "team": public_team(scope.get("team")),
        "leaderTeams": [team for team in (public_team(item) for item in scope.get("leaderTeams") or []) if team],
    }


async def real_customer_team_scope(app_user: dict[str, Any]) -> dict[str, Any]:
    """Resolve a real customer department leader's own team board scope.

    The leader identity is derived only from the membership bound to this local
    account id.  Matching by email would let an unrelated principal with the
    same address inherit a customer's team scope, which is exactly what the
    local-account early return in ``team_scope_for_user`` protects against.
    """

    if not organization_real_enabled():
        return empty_team_scope()
    try:
        membership = await active_real_organization_membership(app_user)
    except HTTPException:
        # A recovering capability must not turn into a failed login; the board
        # entry simply stays hidden until the directory is readable again.
        logger.exception("failed to resolve real customer membership for team scope")
        return empty_team_scope()
    if not membership or str(membership.get("teamRole") or "") != "leader":
        return empty_team_scope()
    organization_id = organization_identifier(membership)
    department_id = str(membership.get("departmentId") or "")
    if not organization_id or not department_id:
        return empty_team_scope()
    try:
        department = await organization_scoped_store_call(
            organization_id, "get_department", department_id
        )
    except (HTTPException, OrganizationStoreError, AttributeError, TypeError):
        logger.exception("failed to resolve department for real team scope")
        return empty_team_scope()
    if not isinstance(department, dict) or str(department.get("status") or "") != "active":
        return empty_team_scope()
    upstream_team_id = str(department.get("upstreamTeamId") or "")
    if not upstream_team_id:
        # The department has no upstream Team yet, so there is no usage scope to
        # read.  The board appears on its own once provisioning completes.
        return empty_team_scope()
    department_name = str(department.get("name") or "")
    backends = usage_backend_ids()
    team = {
        "id": department_id,
        "name": department_name,
        "teamRef": f"real-{organization_id}-{department_id}",
        "departmentId": department_id,
        "organizationId": organization_id,
        "memberCount": int(department.get("activeMemberCount") or 0),
        "backend": backends[0] if backends else "",
        # The upstream Team id stays server-side: public_team() never exposes
        # teamScopes, and select_authorized_team() matches on teamRef only.
        "teamScopes": [
            {"backend": backend, "id": upstream_team_id, "name": department_name}
            for backend in backends
        ],
    }
    return {
        "isTeamLeader": True,
        "teamBoardStatus": "single",
        "team": team,
        "leaderTeams": [team],
    }


async def team_scope_for_user(app_user: dict[str, Any], refresh: bool = False) -> dict[str, Any]:
    # Password and enterprise accounts are intentionally not merged by email.
    # Local signups therefore never inherit a team-leader scope from SSO data;
    # a real customer leader is resolved from its own bound membership instead.
    if app_user.get("id"):
        scope = await real_customer_team_scope(app_user)
        # 负责人是本地目录数据，改动应当立刻生效，因此这条路径不走权限缓存。
        return {**scope, "cache": {"hit": True, "ttlSeconds": 0}}
    store = usage_store()
    revision = "development-upstream"
    if store is not None:
        await store.connect()
        state_loader = getattr(store, "snapshot_state", None)
        if callable(state_loader):
            state = await state_loader()
            revision = str(state.get("revision") or "")
        else:
            revision = "legacy-test-snapshot"
        if not revision:
            raise HTTPException(status_code=503, detail="团队权限快照尚未就绪，请等待后台同步完成")
    cache_key = team_auth_cache_key(app_user["email"], app_user.get("name"), revision)
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
    scope_loader = getattr(store, "team_leader_scope", None) if store is not None else None
    if callable(scope_loader):
        # 这条分支只服务 SSO 账号（本地账号已在函数开头返回），团队成员快照里的
        # employee_email 就是可用的身份键，无需再向上游解析用户。
        scope = await scope_loader(app_user["email"], [], usage_backend_ids())
    else:
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
    revision = "development-upstream"
    if usage_store() is not None:
        revision = await snapshot_revision(start_date, end_date)
    cache_key = team_usage_cache_key(app_user["email"], team, start_date, end_date, source, revision)
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
    async def load() -> dict[str, Any]:
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
                    attach_snapshot_freshness(payload, last_synced, start_date, end_date, revision)
                    payload.setdefault("dataQuality", {})["backends"] = [item.get("backend") for item in team_scope_items(team)]
            except Exception as exc:
                logger.exception("local team usage query failed")
                raise manual_refresh_database_unavailable() from exc
        if payload is None:
            if store is not None:
                raise HTTPException(status_code=503, detail="团队用量快照尚未就绪，请等待后台同步完成")
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

    return await usage_singleflight(cache_key, load)


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


async def _team_member_usage_payload(
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
    revision = "development-upstream"
    if usage_store() is not None:
        revision = await snapshot_revision(start_date, end_date)
    cache_key = team_member_usage_cache_key(app_user["email"], team, employee, start_date, end_date, source, revision)
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
        except Exception as exc:
            logger.exception("local team member usage query failed")
            raise manual_refresh_database_unavailable() from exc
    if stored_payload is not None:
        rows = stored_payload.get("rows") or []
        selected_employee = stored_payload.get("employee") or {}
        if not selected_employee:
            raise HTTPException(status_code=404, detail="未找到该团队成员")
        team_payload = {"team": stored_payload.get("team") or {}, "dataQuality": stored_payload.get("dataQuality") or {}}
    else:
        if store is not None:
            raise HTTPException(status_code=503, detail="团队成员用量快照尚未就绪，请等待后台同步完成")
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
        attach_snapshot_freshness(
            payload,
            stored_payload.get("lastSyncedAt"),
            start_date,
            end_date,
            revision,
        )
    elif team_payload.get("dataFreshness"):
        payload["dataFreshness"] = team_payload["dataFreshness"]
    team_member_usage_cache.set(cache_key, payload, env_int("TEAM_MEMBER_USAGE_CACHE_TTL_SECONDS", 300))
    payload = dict(payload)
    payload["cache"] = {"hit": False, "ttlSeconds": 0}
    return payload


async def team_member_usage_payload(
    app_user: dict[str, Any],
    start_date: str,
    end_date: str,
    source: str,
    employee: str,
    refresh: bool = False,
    team_ref_value: str | None = None,
) -> dict[str, Any]:
    identity = str(app_user.get("id") or app_user.get("email") or "anonymous").lower()
    key = (
        f"team-member:{identity}:{team_ref_value or ''}:{employee}:"
        f"{start_date}:{end_date}:{source}:{int(refresh)}"
    )
    return await usage_singleflight(
        key,
        lambda: _team_member_usage_payload(
            app_user,
            start_date,
            end_date,
            source,
            employee,
            refresh,
            team_ref_value,
        ),
    )


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
        if str(local_user.get("account_type") or local_user.get("accountType") or "") == "enterprise_managed":
            raise auth_http_error(
                403,
                "企业托管账号请使用企业令牌管理，不提供个人令牌操作",
                "ORGANIZATION_UPSTREAM_FORBIDDEN",
            )
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


class TeamKeyMutationRequest(BaseModel):
    """团队负责人处置成员密钥的请求体。

    只接受团队标识：密钥归属的上游账号一律由服务端重新推导，浏览器拿不到也
    改不了，避免把上游账号 id 变成可篡改的授权句柄。
    """

    model_config = ConfigDict(extra="forbid")

    teamRef: str = Field(default="", max_length=128)


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
    identifier: str | None = Field(default=None, min_length=3, max_length=320)
    email: str | None = Field(default=None, min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)
    turnstileToken: str = Field(default="", max_length=4096)

    @field_validator("identifier", "email")
    @classmethod
    def strip_login_identifier(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    def resolved_identifier(self) -> str:
        value = str(self.identifier or self.email or "").strip()
        if not value:
            raise ValueError("请输入邮箱或账号")
        return value


class OrganizationInvitationAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=16, max_length=512)
    password: str | None = Field(default=None, min_length=8, max_length=128)

    @field_validator("token")
    @classmethod
    def strip_invitation_token(cls, value: str) -> str:
        return value.strip()


class OrganizationClaimAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=24, max_length=512)
    password: str = Field(min_length=8, max_length=128)
    turnstileToken: str = Field(default="", max_length=4096)

    @field_validator("token")
    @classmethod
    def strip_claim_token(cls, value: str) -> str:
        return value.strip()


class OrganizationMembershipClaimCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memberName: str | None = Field(default=None, min_length=1, max_length=120)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    loginName: str = Field(default="lianghaiqiang", min_length=3, max_length=64)
    departmentId: str = Field(min_length=1, max_length=128)
    role: Literal["admin", "member"] = "admin"

    @field_validator("memberName", "name", "loginName", "departmentId")
    @classmethod
    def strip_claim_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


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
    # 部门职务与企业角色是两件事：负责人只看本部门的团队看板，企业角色才决定
    # 能不能管理整个企业。默认普通成员，避免调用方漏填时静默放大权限。
    teamRole: Literal["leader", "member"] = "member"

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
    teamRole: Literal["leader", "member"] | None = None
    # removed 只为给出清晰错误提示而接受：移除必须走 DELETE 路由，那里才会撤销令牌、
    # 作废邀请并解除登录账号绑定。这里放行到处理函数再拒绝，避免只给一个 422。
    status: Literal["invited", "pending", "active", "suspended", "removed"] | None = None
    # 登录名只在平台侧路由的白名单里放开；客户管理员那条路径不读这个字段。
    loginName: str | None = Field(default=None, min_length=3, max_length=64)

    @field_validator("name", "departmentId", "loginName")
    @classmethod
    def strip_optional_member_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else value


class OrganizationMemberAccountRequest(BaseModel):
    """Bind, rebind, or release the local login account behind one member."""

    model_config = ConfigDict(extra="forbid")

    # 空串是显式的解绑意图，不是缺参数，所以不设 min_length。
    identifier: str = Field(default="", max_length=254)

    @field_validator("identifier")
    @classmethod
    def strip_identifier(cls, value: str) -> str:
        return value.strip()


class OrganizationPrincipalMemberRequest(BaseModel):
    """Associate a usage identity with a member, or release it."""

    model_config = ConfigDict(extra="forbid")

    memberId: str = Field(default="", max_length=128)

    @field_validator("memberId")
    @classmethod
    def strip_member_id(cls, value: str) -> str:
        return value.strip()


class OrganizationEmptyRequest(BaseModel):
    """Validate body-less organization mutations without accepting tenant IDs."""

    model_config = ConfigDict(extra="forbid")


class OrganizationInvitationMutationRequest(OrganizationEmptyRequest):
    """Strict body for invitation resend/revoke actions."""


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


class OrganizationAdoptionPreviewRequest(BaseModel):
    """Read-only platform preflight for an explicitly configured pilot."""

    model_config = ConfigDict(extra="forbid")

    organizationName: str = Field(min_length=1, max_length=120)
    departmentName: str = Field(min_length=1, max_length=120)
    adminName: str = Field(min_length=1, max_length=120)
    adminEmail: str = Field(min_length=3, max_length=254)
    principalName: str = Field(min_length=1, max_length=120)
    organizationCandidates: list[str] = Field(default_factory=list, max_length=20)
    teamCandidates: list[str] = Field(default_factory=list, max_length=20)
    keyAliases: list[str] = Field(default_factory=list, max_length=20)
    effectiveFrom: date
    effectiveThrough: date

    @field_validator(
        "organizationName",
        "departmentName",
        "adminName",
        "adminEmail",
        "principalName",
    )
    @classmethod
    def strip_adoption_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("organizationCandidates", "teamCandidates", "keyAliases")
    @classmethod
    def strip_adoption_candidates(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip()))


class OrganizationAdoptionApplyRequest(OrganizationAdoptionPreviewRequest):
    previewFingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    idempotencyKey: str = Field(min_length=8, max_length=128)

    @field_validator("previewFingerprint", "idempotencyKey")
    @classmethod
    def strip_adoption_proof(cls, value: str) -> str:
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


class OrganizationCreditAdjustmentRequest(BaseModel):
    """Seller-issued enterprise credit adjustment with an idempotency key."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["grant", "revoke"]
    amountUsd: Decimal
    reason: str = Field(min_length=1, max_length=500)
    externalReference: str = Field(default="", max_length=128)
    idempotencyKey: str = Field(min_length=8, max_length=128)

    @field_validator("amountUsd", mode="before")
    @classmethod
    def require_numeric_adjustment_amount(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValueError("amountUsd must be a number")
        return value

    @field_validator("amountUsd")
    @classmethod
    def validate_adjustment_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value.as_tuple().exponent < -2:
            raise ValueError("amountUsd must be a finite amount with at most two decimal places")
        if value < Decimal("0.01") or value > Decimal("100000.00"):
            raise ValueError("amountUsd must be between 0.01 and 100000.00")
        return value.quantize(Decimal("0.01"))

    @field_validator("reason", "externalReference", "idempotencyKey")
    @classmethod
    def strip_adjustment_text(cls, value: str) -> str:
        return value.strip()


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


class CostItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=160)
    vendor: str = Field(default="", max_length=160)
    model: str = Field(default="", max_length=256)
    businessScope: str = Field(default="", max_length=160)
    amount: Decimal = Field(gt=0)
    currency: Literal["USD", "CNY"] = "USD"
    exchangeRate: Decimal = Field(default=Decimal("1"), gt=0)
    serviceStartDate: date
    serviceEndDate: date
    financeBucket: str = Field(default="", max_length=160)
    costBucket: str = Field(default="", max_length=80)
    sourceType: Literal["manual", "subscription", "account_procurement", "infra", "labor", "support", "other"] = "manual"
    provider: str = Field(default="", max_length=160)
    accountId: str = Field(default="", max_length=256)
    accountName: str = Field(default="", max_length=160)
    voucherId: str = Field(default="", max_length=160)
    voucherNo: str = Field(default="", max_length=160)
    invoiceNo: str = Field(default="", max_length=160)
    recognitionStatus: Literal["actual", "committed", "planned"] = "actual"
    reconciliationStatus: Literal["unreconciled", "pending", "matched", "partial", "exception", "waived"] = "unreconciled"
    planVersionId: str = Field(default="", max_length=128)
    scenario: str = Field(default="", max_length=80)
    sourceEvidence: str = Field(default="", max_length=1000)
    notes: str = Field(default="", max_length=1000)
    enabled: bool = True


class CostBudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    budgetUsd: Decimal = Field(ge=0)
    dailyTargetUsd: Decimal = Field(ge=0)


class SavingsActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    baselineDailyCost: Decimal = Field(ge=0)
    implementedDate: date
    verifiedDate: date | None = None
    verifiedDailyCost: Decimal | None = Field(default=None, ge=0)
    owner: str = Field(default="", max_length=160)
    status: Literal["planned", "implemented", "verified"] = "planned"
    expectedDailyCost: Decimal | None = Field(default=None, ge=0)
    expectedStartDate: date | None = None
    provider: str = Field(default="", max_length=160)
    model: str = Field(default="", max_length=256)
    costBucket: str = Field(default="", max_length=80)
    evidenceUrl: str = Field(default="", max_length=1000)
    financeReviewer: str = Field(default="", max_length=160)
    notes: str = Field(default="", max_length=1000)


class StabilityActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    owner: str = Field(default="", max_length=160)
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    status: Literal["open", "in_progress", "resolved", "verified", "closed"] = "open"
    targetDate: date | None = None
    fixReference: str = Field(default="", max_length=1000)
    requestedModelGroup: str = Field(default="", max_length=256)
    scenario: str = Field(default="", max_length=64)
    errorCode: str = Field(default="", max_length=120)
    notes: str = Field(default="", max_length=2000)


class StabilityRegressionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actionId: str = Field(min_length=1, max_length=128)
    baselineStart: date
    baselineEnd: date
    regressionStart: date
    regressionEnd: date
    metric: str = Field(min_length=1, max_length=120)
    baselineValue: Decimal | None = None
    regressionValue: Decimal | None = None
    conclusion: Literal["passed", "failed", "inconclusive"]
    notes: str = Field(default="", max_length=2000)

    @field_validator("baselineEnd")
    @classmethod
    def validate_baseline_window(cls, value: date, info: Any) -> date:
        start = info.data.get("baselineStart")
        if start and value < start:
            raise ValueError("baselineEnd must not be before baselineStart")
        return value

    @field_validator("regressionEnd")
    @classmethod
    def validate_regression_window(cls, value: date, info: Any) -> date:
        start = info.data.get("regressionStart")
        if start and value < start:
            raise ValueError("regressionEnd must not be before regressionStart")
        return value


class CostPlanVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: int = Field(ge=2020, le=2100)
    version: str = Field(min_length=1, max_length=80)
    scenario: Literal["baseline", "optimistic", "conservative"] = "baseline"
    asOf: date
    status: Literal["draft", "approved", "archived"] = "draft"
    notes: str = Field(default="", max_length=2000)
    coverageComplete: bool = False


class SavingsMeasurementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actionId: str = Field(min_length=1, max_length=128)
    scope: str = Field(default="", max_length=256)
    provider: str = Field(default="", max_length=160)
    model: str = Field(default="", max_length=256)
    costBucket: str = Field(default="", max_length=80)
    baselineStart: date
    baselineEnd: date
    measurementStart: date
    measurementEnd: date
    baselineAmountUsd: Decimal = Field(ge=0)
    actualAmountUsd: Decimal = Field(ge=0)
    evidenceUrl: str = Field(default="", max_length=1000)
    financeReviewer: str = Field(default="", max_length=160)
    reviewedAt: datetime | None = None
    status: Literal["pending_evidence", "reviewed", "rejected"] = "pending_evidence"
    notes: str = Field(default="", max_length=2000)


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
    start_date, end_date = resolve_usage_range(start_date, end_date)
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
    start_date, end_date = resolve_usage_range(start_date, end_date)
    return await client().admin_usage_compare(start_date, end_date, source)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    result: dict[str, Any] = {"status": "ok", "usageSync": {"status": "disabled"}}
    reader_config = usage_reader_config_status()
    result["usageReaderConfig"] = reader_config
    if not reader_config["configured"]:
        result["status"] = "degraded"
    if organization_real_enabled() and (
        not _organization_capability_status.get("available")
        or organization_capability_probe_due()
    ):
        await refresh_organization_capabilities()
    result["organization"] = {
        **dict(_organization_capability_status),
        "mode": organization_mode(),
        "configured": bool(
            organization_demo_enabled()
            or (
                organization_real_enabled()
                and bool(os.getenv("USAGE_DATABASE_URL", "").strip())
            )
        ),
    }
    if organization_real_enabled() and not _organization_capability_status.get("available"):
        result["status"] = "degraded"
    if organization_real_enabled() and isinstance(_organization_store, PostgreSQLOrganizationRepository):
        try:
            outbox = await _organization_store.outbox_health()
            result["organization"]["outbox"] = outbox
            if outbox["pendingCount"] or outbox.get("failedCount"):
                result["organization"]["status"] = "degraded"
                result["status"] = "degraded"
        except Exception:
            logger.exception("organization outbox health check failed")
            result["organization"]["outbox"] = {"status": "error"}
            result["status"] = "degraded"
        try:
            result["organization"]["settlement"] = await _organization_store.settlement_health()
        except Exception:
            logger.exception("organization settlement health check failed")
            result["organization"]["settlement"] = {"status": "error"}
            result["status"] = "degraded"
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
    result["remoteDemo"] = {
        "readOnly": remote_demo_read_only(),
        "usageSnapshotOnly": remote_demo_read_only(),
    }
    store = usage_store()
    if store is not None:
        result["usageDatabase"] = await store.health()
        if result["usageDatabase"].get("status") in {"error", "disconnected"}:
            result["status"] = "degraded"
        else:
            state_loader = getattr(store, "sync_state", None)
            if not callable(state_loader):
                # 未接入共享同步状态的旧 store（含测试替身）沿用进程内状态。
                result["usageSync"] = dict(_usage_sync_status)
                if result["usageSync"].get("status") in {"error", "failed", "partial"}:
                    result["status"] = "degraded"
                state = {}
            else:
                state = await state_loader() or {}
            if state:
                now = datetime.now(timezone.utc)
                heartbeat = state.get("heartbeatAt")
                last_success = state.get("lastSuccessAt")
                heartbeat_lag = (
                    max(0, int((now - heartbeat).total_seconds()))
                    if isinstance(heartbeat, datetime)
                    else None
                )
                snapshot_lag = (
                    max(0, int((now - last_success).total_seconds()))
                    if isinstance(last_success, datetime)
                    else None
                )
                result["usageSync"] = {
                    "role": usage_sync_role(),
                    "status": state.get("status", "unknown"),
                    "workerId": state.get("workerId"),
                    "heartbeatAt": heartbeat.isoformat() if isinstance(heartbeat, datetime) else None,
                    "heartbeatLagSeconds": heartbeat_lag,
                    "lastStartedAt": state["lastStartedAt"].isoformat() if isinstance(state.get("lastStartedAt"), datetime) else None,
                    "lastFinishedAt": state["lastFinishedAt"].isoformat() if isinstance(state.get("lastFinishedAt"), datetime) else None,
                    "lastSuccessAt": last_success.isoformat() if isinstance(last_success, datetime) else None,
                    "snapshotLagSeconds": snapshot_lag,
                    "snapshotRevision": state.get("snapshotRevision"),
                    "lastError": state.get("lastError"),
                }
                if (
                    heartbeat_lag is None
                    or heartbeat_lag > max(30, env_int("USAGE_SYNC_HEARTBEAT_MAX_AGE_SECONDS", 120))
                    or snapshot_lag is None
                    or snapshot_lag > max(60, env_int("USAGE_SYNC_SUCCESS_MAX_AGE_SECONDS", 3600))
                    or state.get("status") in {"failed", "error", "partial"}
                ):
                    result["usageSync"]["status"] = "degraded"
                    result["status"] = "degraded"
    else:
        result["usageDatabase"] = {"enabled": False, "connected": False, "status": "disabled"}
        if usage_sync_role() == "reader":
            result["usageSync"] = {
                "role": "reader",
                "status": "misconfigured",
                "missing": reader_config["missing"],
            }
    if realtime_enabled():
        try:
            realtime = usage_realtime_store()
            if realtime is None:
                raise RuntimeError("realtime usage store unavailable")
            await realtime.connect()
            realtime_status = await realtime.status()
            lag = realtime_status.get("latestEventLagSeconds")
            stale_seconds = max(10, env_int("USAGE_REALTIME_STALE_SECONDS", 30))
            status = "ok"
            if (
                not realtime_status.get("ready")
                or realtime_status.get("backfillActive")
                or (lag is not None and lag > stale_seconds)
            ):
                status = "degraded"
            if lag is not None and lag > 120:
                status = "unhealthy"
            result["usageRealtime"] = {
                "status": status,
                "connected": True,
                "ready": bool(realtime_status.get("ready")),
                "revision": realtime_status.get("revision"),
                "latestEventAt": realtime_status["latestEventAt"].isoformat()
                if isinstance(realtime_status.get("latestEventAt"), datetime)
                else None,
                "latestEventLagSeconds": lag,
                "pendingArchiveCount": realtime_status.get("pendingArchiveCount", 0),
                "cursors": realtime_status.get("cursors", {}),
                "backfillActive": bool(realtime_status.get("backfillActive")),
                "backfillBackends": realtime_status.get("backfillBackends", []),
            }
            if status != "ok":
                result["status"] = status
        except Exception:
            logger.exception("usage realtime health check failed")
            result["usageRealtime"] = {
                "status": "unhealthy",
                "connected": False,
                "ready": False,
            }
            result["status"] = "unhealthy"
    else:
        result["usageRealtime"] = {"status": "disabled", "connected": False, "ready": False}
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
        "remoteDemoReadOnly": remote_demo_read_only(),
        "remoteDemoUsageSnapshotOnly": remote_demo_read_only(),
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
        # Invitation-bound organization accounts are provisioned through the
        # organization outbox. Never create a second generic local upstream
        # user for the same password account.
        if not organization_real_enabled():
            await retry_local_provisioning(local_user)
        else:
            try:
                # Any durable membership means this account belongs to the
                # invitation/provisioning state machine, even before it becomes
                # active. Creating the generic personal upstream user here
                # would race and conflict with organization member provisioning.
                managed_account = str(
                    local_user.get("account_type")
                    or local_user.get("accountType")
                    or "personal"
                ) == "enterprise_managed"
                if not managed_account and not await organization_memberships_for_user(local_user):
                    await retry_local_provisioning(local_user)
            except HTTPException:
                # Keep real-mode capability failures visible in the scope
                # response instead of silently creating an unrelated account.
                pass
    user = await auth_user_payload(local_user, refresh_entitlement=True) if local_user else dict(require_user(request))
    if await is_demo_customer_user(user):
        user.update(await demo_team_scope_for_user(user))
    elif organization_real_enabled() and local_user:
        # 真实客户负责人只依赖本地目录，这里就能定论，团队看板入口不必等
        # /api/auth/scope 回来。身份从登录账号本身解析，不看会话里的邮箱。
        user.update(public_team_scope(await real_customer_team_scope(local_user)))
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
        identifier = data.resolved_identifier()
        normalized_identifier = (
            auth_store().normalize_email(identifier)
            if "@" in identifier
            else auth_store().normalize_login_name(identifier)
        )
    except ValueError as exc:
        raise auth_http_error(401, "邮箱、账号或密码不正确", "AUTH_INVALID_CREDENTIALS") from exc
    await enforce_rate_limit("login_identifier", normalized_identifier, 10, 60)
    await enforce_rate_limit("login_ip", request_ip(request), 30, 60)
    user = await auth_store_call("get_user_by_identifier", normalized_identifier)
    valid = bool(user and await asyncio.to_thread(verify_password, data.password, str(user.get("password_hash") or user.get("passwordHash") or "")))
    if not valid:
        audit_email = str(user.get("email") or "") if user else ""
        await auth_store_call(
            "record_audit_event", "login_failed", str(user["id"]) if user else None,
            audit_email or None, request_ip(request), False,
            {"identifierType": "email" if "@" in normalized_identifier else "login_name"},
        )
        raise auth_http_error(401, "邮箱、账号或密码不正确", "AUTH_INVALID_CREDENTIALS")
    if str(user.get("status") or "active") != "active":
        status = str(user.get("status") or "")
        code = "AUTH_PROVISIONING_PENDING" if status in {"pending_approval", "provisioning"} else "AUTH_ACCOUNT_SUSPENDED"
        message = "企业账号仍在审核或开通中" if code == "AUTH_PROVISIONING_PENDING" else "账号当前不可登录，请联系管理员"
        raise auth_http_error(403, message, code)
    if str(user.get("identity_status") or user.get("identityStatus") or "verified") != "verified":
        raise auth_http_error(403, "企业账号仍待平台审核", "AUTH_IDENTITY_PENDING_APPROVAL")
    if (
        str(user.get("account_type") or user.get("accountType") or "personal") == "personal"
        and env_bool("EMAIL_VERIFICATION_REQUIRED", True)
        and not bool(user.get("email_verified") or user.get("emailVerified"))
    ):
        raise auth_http_error(403, "请先完成邮箱验证", "AUTH_EMAIL_UNVERIFIED")
    stored_hash = str(user.get("password_hash") or user.get("passwordHash") or "")
    if password_needs_rehash(stored_hash):
        await auth_store_call("update_password", str(user["id"]), await asyncio.to_thread(hash_password, data.password))
        user = await auth_store_call("get_user", str(user["id"])) or user
    await auth_store_call("touch_last_login", str(user["id"]))
    payload, csrf_value = await create_local_session(request, user)
    await auth_store_call(
        "record_audit_event", "login_success", str(user["id"]),
        str(user.get("email") or "") or None, request_ip(request), True,
        {"identifierType": "email" if "@" in normalized_identifier else "login_name"},
    )
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
    await auth_store_call(
        "record_audit_event",
        "password_changed",
        user_id,
        str(user.get("email") or "") or None,
        request_ip(request),
        True,
        {},
    )
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
    if str(
        user.get("accountType") or user.get("account_type") or "personal"
    ) == "enterprise_managed":
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
            # A local account never inherits an SSO team scope, but a real
            # customer department leader owns one through its own membership.
            **public_team_scope(await real_customer_team_scope(user)),
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


@app.get("/api/auth/invitations/{token}")
async def verify_organization_invitation(token: str) -> dict[str, Any]:
    """Validate an invitation without consuming it or revealing a secret hash."""

    if not organization_real_enabled():
        raise HTTPException(status_code=404, detail="企业邀请功能尚未开放")
    try:
        invitation = await organization_store_call(
            "verify_invitation", token, _require_capability=False
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    if not invitation:
        raise auth_http_error(404, "邀请链接无效、已过期或已被撤销", "ORGANIZATION_INVITATION_INVALID")
    email = str(invitation.get("email") or "").strip().lower()
    existing_account = bool(email and await auth_store_call("get_user_by_email", email))
    return {
        "ok": True,
        "invitation": invitation,
        "existingAccount": existing_account,
        "passwordRequired": not existing_account,
    }


@app.post("/api/auth/invitations/accept")
async def accept_organization_invitation(
    data: OrganizationInvitationAcceptRequest, request: Request
) -> dict[str, Any]:
    """Consume an invitation and bind it to a password account.

    Existing accounts keep their password. New accounts must submit one in the
    invitation request; upstream provisioning is queued before access becomes
    active, so a transient upstream outage never grants temporary membership.
    """

    if not organization_real_enabled():
        raise HTTPException(status_code=404, detail="企业邀请功能尚未开放")
    await enforce_csrf(request)
    invitation = await organization_store_call(
        "verify_invitation", data.token, _require_capability=False
    )
    if not invitation:
        raise auth_http_error(404, "邀请链接无效、已过期或已被撤销", "ORGANIZATION_INVITATION_INVALID")
    email = str(invitation.get("email") or "").strip().lower()
    local_user = await auth_store_call("get_user_by_email", email)
    created_local_user = False
    if local_user is None:
        if not data.password:
            raise auth_http_error(400, "首次接受邀请必须设置密码", "ORGANIZATION_INVITATION_PASSWORD_REQUIRED")
        try:
            local_user = await auth_store_call(
                "create_user",
                email,
                str(invitation.get("name") or email.split("@", 1)[0]),
                await asyncio.to_thread(hash_password, data.password),
                True,
                "active",
            )
            created_local_user = True
        except DuplicateEmailError as exc:
            local_user = await auth_store_call("get_user_by_email", email)
            if local_user is None:
                raise auth_http_error(409, "该邮箱已注册，请直接登录", "AUTH_EMAIL_EXISTS") from exc
    try:
        consumed = await organization_store_call(
            "accept_invitation",
            data.token,
            str(local_user["id"]),
            _require_capability=False,
        )
    except Exception:
        if created_local_user:
            try:
                await auth_store_call("delete_unprovisioned_user", str(local_user["id"]))
            except Exception:
                logger.exception("failed to compensate invitation account creation")
        raise
    if not consumed:
        if created_local_user:
            try:
                await auth_store_call("delete_unprovisioned_user", str(local_user["id"]))
            except Exception:
                logger.exception("failed to compensate rejected invitation account")
        raise auth_http_error(409, "邀请链接已被其他请求使用", "ORGANIZATION_INVITATION_CONSUMED")
    member_id = str(consumed["memberId"])
    organization_id = str(consumed["organizationId"])
    payload = await auth_user_payload(local_user)
    return {
        "ok": True,
        "status": "provisioning",
        "organizationId": organization_id,
        "memberId": member_id,
        "user": payload,
        "message": "邀请已接受，企业账号正在开通中",
    }


@app.get("/api/auth/organization-claims/{token}")
async def verify_organization_claim(token: str, request: Request) -> dict[str, Any]:
    """Expose only the offline claim details needed by the anonymous page."""

    if not organization_real_enabled():
        raise HTTPException(status_code=404, detail="企业账号认领功能尚未开放")
    await enforce_rate_limit("organization_claim_verify_ip", request_ip(request), 60, 60)
    claim = await auth_store_call("get_membership_claim_by_token", token)
    if not claim or str(claim.get("status") or "") != "pending":
        raise auth_http_error(
            404, "激活链接无效、已过期或已被撤销", "ORGANIZATION_CLAIM_INVALID"
        )
    return {
        "ok": True,
        "claim": {
            "organizationName": claim.get("organizationName"),
            "loginName": claim.get("loginName"),
            "memberName": claim.get("memberName"),
            "role": claim.get("role"),
            "expiresAt": claim.get("expiresAt"),
            "status": claim.get("status"),
        },
    }


@app.post("/api/auth/organization-claims/accept")
async def accept_organization_claim(
    data: OrganizationClaimAcceptRequest, request: Request
) -> dict[str, Any]:
    """Set a username password, then wait for the platform's offline approval."""

    if not organization_real_enabled():
        raise HTTPException(status_code=404, detail="企业账号认领功能尚未开放")
    if not password_login_configured():
        raise auth_http_error(
            503,
            "企业账号登录能力尚未配置完成",
            "ORGANIZATION_CLAIM_AUTH_UNAVAILABLE",
        )
    if not turnstile_enabled() or not turnstile_configured():
        raise auth_http_error(
            503,
            "企业账号激活的人机验证尚未配置完成",
            "ORGANIZATION_CLAIM_TURNSTILE_REQUIRED",
        )
    await enforce_csrf(request)
    await enforce_rate_limit("organization_claim_attempt_ip", request_ip(request), 30, 3600)
    await enforce_rate_limit("organization_claim_token", hash_auth_token(data.token), 8, 3600)
    await verify_turnstile(request, data.turnstileToken)
    try:
        accepted = await auth_store_call(
            "accept_membership_claim",
            data.token,
            await asyncio.to_thread(hash_password, data.password),
        )
    except DuplicateLoginNameError as exc:
        raise auth_http_error(
            409, "该企业账号已存在", "ORGANIZATION_CLAIM_LOGIN_EXISTS"
        ) from exc
    if not accepted:
        raise auth_http_error(
            409, "激活链接已使用、已过期或已被撤销", "ORGANIZATION_CLAIM_CONSUMED"
        )
    claim = accepted.get("claim") or {}
    user = accepted.get("user") or {}
    await auth_store_call(
        "record_audit_event",
        "organization_claim_accepted",
        str(user.get("id") or "") or None,
        None,
        request_ip(request),
        True,
        {"claimId": claim.get("id"), "organizationId": claim.get("organizationId")},
    )
    return {
        "ok": True,
        "status": "accepted_pending_approval",
        "claimId": claim.get("id"),
        "loginName": claim.get("loginName"),
        "message": "密码已设置，平台完成本人核验后才会开通登录",
    }


@app.get("/api/organization/current")
async def organization_current(request: Request) -> dict[str, Any]:
    require_real_organization_capability()
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
    require_real_organization_capability()
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
    """Return persisted real usage or deterministic demo usage by mode."""

    user = await require_organization_usage_viewer(request)
    start_date, end_date = resolve_usage_range(start_date, end_date)
    organization_id = organization_identifier(organization_current_member(user))
    if organization_real_enabled():
        payload = await real_organization_usage_payload(
            organization_id,
            start_date=start_date,
            end_date=end_date,
            source=source,
            employee=(employee or "").strip(),
            refresh=refresh,
        )
    else:
        payload = await cached_mock_organization_usage_payload(
            "mock_organization_usage", organization_id, start_date=start_date,
            end_date=end_date, source=source, employee=(employee or "").strip(), refresh=refresh,
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
    start_date, end_date = resolve_usage_range(start_date, end_date)
    organization_id = organization_identifier(organization_current_member(user))
    if organization_real_enabled():
        payload = await real_organization_department_usage_payload(
            organization_id,
            start_date=start_date,
            end_date=end_date,
            source=source,
            department=(department or "").strip(),
            refresh=refresh,
        )
    else:
        payload = await cached_mock_organization_usage_payload(
            "mock_department_usage", organization_id, start_date=start_date,
            end_date=end_date, source=source, department=(department or "").strip(), refresh=refresh,
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

    require_real_organization_capability()
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

    if organization_real_enabled():
        raise auth_http_error(410, "企业模拟充值已下线，请联系平台运营授信", "ORGANIZATION_TOPUP_DISABLED")
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
        organization_id = organization_identifier(organization_current_member(user))
        operation = "create_member_with_invitation" if organization_real_enabled() else "create_member"
        member = await organization_scoped_store_call(
            organization_id,
            operation,
            data.name,
            data.email,
            data.departmentId,
            data.role,
            team_role=data.teamRole,
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
    if "teamRole" in fields:
        updates["team_role"] = data.teamRole
    if "status" in fields:
        reject_member_removal_via_update(data.status)
        reject_direct_real_member_activation(data.status)
        updates["status"] = "invited" if data.status == "pending" else data.status
    try:
        organization_id = organization_identifier(organization_current_member(user))
        member = await organization_scoped_store_call(
            organization_id, "update_member", member_id, **updates
        )
        if organization_real_enabled() and updates.get("status") == "invited":
            store = organization_store()
            if isinstance(store, PostgreSQLOrganizationRepository):
                await store.create_invitation(organization_id, member_id)
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    invalidate_organization_usage_cache()
    return {"member": member}


async def resend_real_member_invitation(
    organization_id: str,
    member_id: str,
) -> dict[str, Any]:
    """Rotate and enqueue one invitation without sending mail in the request."""

    require_real_organization_capability()
    store = organization_store()
    if not isinstance(store, PostgreSQLOrganizationRepository):
        raise auth_http_error(
            503,
            "企业邀请持久化能力暂不可用",
            "ORGANIZATION_INVITATION_STORE_UNAVAILABLE",
        )
    member = await store.get_member(member_id, organization_id=organization_id)
    if not member:
        raise auth_http_error(404, "未找到对应成员", "ORGANIZATION_MEMBER_NOT_FOUND")
    if str(member.get("status") or "") != "invited":
        raise auth_http_error(
            409,
            "只能向待邀请成员重发邀请",
            "ORGANIZATION_INVITATION_MEMBER_NOT_PENDING",
        )
    invitation = await store.create_invitation(organization_id, member_id)
    # The signed token is an internal delivery credential. Never expose it
    # from an authenticated management route.
    return {key: value for key, value in invitation.items() if key != "token"}


async def revoke_real_member_invitation(
    organization_id: str,
    member_id: str,
) -> None:
    """Revoke a tenant-scoped pending invitation by member id."""

    require_real_organization_capability()
    store = organization_store()
    if not isinstance(store, PostgreSQLOrganizationRepository):
        raise auth_http_error(
            503,
            "企业邀请持久化能力暂不可用",
            "ORGANIZATION_INVITATION_STORE_UNAVAILABLE",
        )
    member = await store.get_member(member_id, organization_id=organization_id)
    if not member:
        raise auth_http_error(404, "未找到对应成员", "ORGANIZATION_MEMBER_NOT_FOUND")
    if str(member.get("status") or "") != "invited":
        raise auth_http_error(
            409,
            "只能撤销待邀请成员的邀请",
            "ORGANIZATION_INVITATION_MEMBER_NOT_PENDING",
        )
    if not await store.revoke_member_invitation(organization_id, member_id):
        raise auth_http_error(
            409,
            "当前没有可撤销的有效邀请",
            "ORGANIZATION_INVITATION_NOT_PENDING",
        )


@app.post("/api/organization/current/members/{member_id}/invitation/resend")
async def organization_resend_member_invitation(
    member_id: str,
    request: Request,
    _data: OrganizationInvitationMutationRequest | None = None,
) -> dict[str, Any]:
    """Let a customer administrator rotate and resend a pending invitation."""

    if not organization_real_enabled():
        raise HTTPException(status_code=404, detail="企业邀请功能尚未开放")
    await enforce_csrf(request)
    user = await require_organization_demo_manager(request)
    organization_id = organization_identifier(organization_current_member(user))
    try:
        invitation = await resend_real_member_invitation(organization_id, member_id)
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    return {"ok": True, "invitation": invitation}
    return {"ok": True, "invitation": invitation}


@app.post("/api/organization/current/members/{member_id}/invitation/revoke")
async def organization_revoke_member_invitation(
    member_id: str,
    request: Request,
    _data: OrganizationInvitationMutationRequest | None = None,
) -> dict[str, Any]:
    """Let a customer administrator revoke a pending invitation."""

    if not organization_real_enabled():
        raise HTTPException(status_code=404, detail="企业邀请功能尚未开放")
    await enforce_csrf(request)
    user = await require_organization_demo_manager(request)
    organization_id = organization_identifier(organization_current_member(user))
    try:
        await revoke_real_member_invitation(organization_id, member_id)
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    return {"ok": True, "memberId": member_id}


async def remove_organization_member(organization_id: str, member_id: str) -> dict[str, Any]:
    """把一名待邀请或已暂停成员移出企业，并收尾其残留访问能力。

    store 负责撤销令牌、作废邀请并解除登录账号绑定；这里补上会话撤销，否则对方
    已登录的浏览器还能继续读这家企业的数据。
    """

    try:
        member = await organization_scoped_store_call(
            organization_id, "remove_member", member_id
        )
    except OrganizationConflictError as exc:
        # 通用冲突文案（"请先调整成员或管理员"）解释不了"该成员还没暂停"这种情况。
        detail = str(exc)
        if "already removed" in detail:
            raise auth_http_error(
                409, "该成员已被移除", "ORGANIZATION_MEMBER_ALREADY_REMOVED"
            ) from exc
        if "can be removed" in detail:
            raise auth_http_error(
                409,
                "只能删除待邀请或已暂停的成员，请先暂停该成员",
                "ORGANIZATION_MEMBER_REMOVE_NOT_ALLOWED",
            ) from exc
        raise organization_store_error(exc) from exc
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    if not isinstance(member, dict):
        member = {}
    previous_auth_user_id = str(member.pop("previousAuthUserId", "") or "")
    if previous_auth_user_id:
        await auth_store_call("revoke_user_sessions", previous_auth_user_id)
    invalidate_organization_usage_cache()
    return member


@app.delete("/api/organization/current/members/{member_id}")
async def organization_remove_member(member_id: str, request: Request) -> dict[str, Any]:
    """Let a customer administrator move an invited or suspended member out of the company."""

    await enforce_csrf(request)
    user = await require_organization_demo_manager(request)
    organization_id = organization_identifier(organization_current_member(user))
    member = await remove_organization_member(organization_id, member_id)
    return {"ok": True, "member": member}


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

    require_real_organization_capability()
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
    organization_id = organization_identifier(organization_current_member(user))
    if organization_real_enabled():
        result = await create_real_organization_token(
            organization_id,
            data,
            catalog,
            changed_by=str(user.get("email") or ""),
        )
    else:
        try:
            result = await organization_scoped_store_call(
                organization_id,
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
    organization_id = organization_identifier(organization_current_member(user))
    if organization_real_enabled():
        token = await revoke_real_organization_token(
            organization_id,
            token_id,
            changed_by=str(user.get("email") or ""),
        )
    else:
        try:
            token = await organization_scoped_store_call(
                organization_id,
                "revoke_token",
                token_id,
            )
        except OrganizationStoreError as exc:
            raise organization_token_store_error(exc) from exc
    return {"ok": True, "token": token}


@app.post("/api/organization/current/tokens/{token_id}/delete")
async def organization_delete_token(
    token_id: str,
    request: Request,
    _data: OrganizationEmptyRequest | None = None,
) -> dict[str, Any]:
    """Hide one already revoked token from the authenticated customer's list.

    Deletion is list cleanup only: the stored record survives so the usage this
    token produced keeps its member and department attribution, and the upstream
    key was already removed when the token was revoked.
    """

    await enforce_csrf(request)
    user = await require_organization_demo_manager(request)
    organization_id = organization_identifier(organization_current_member(user))
    if organization_real_enabled():
        store = organization_store()
        if not isinstance(store, PostgreSQLOrganizationRepository):
            raise auth_http_error(
                503, "企业 Token 持久化能力暂不可用", "ORGANIZATION_TOKEN_STORE_UNAVAILABLE"
            )
        try:
            token = await store.delete_token(organization_id, token_id)
        except OrganizationStoreError as exc:
            raise organization_token_store_error(exc) from exc
        await store.record_audit(
            organization_id,
            "organization.token.delete",
            actor=str(user.get("email") or ""),
            target_type="token",
            target_id=token_id,
            details={"name": str(token.get("name") or "")},
        )
    else:
        try:
            token = await organization_scoped_store_call(
                organization_id,
                "delete_token",
                token_id,
            )
        except OrganizationStoreError as exc:
            raise organization_token_store_error(exc) from exc
    return {"ok": True, "tokenId": token_id}


def internal_upstream_organization_ids() -> set[str]:
    """Upstream companies that belong to the seller, not to a customer.

    These are excluded from the pending list so the operator never sees their
    own operating entity offered for onboarding.
    """

    return {
        item.strip()
        for item in os.getenv("ORGANIZATION_INTERNAL_UPSTREAM_IDS", "").split(",")
        if item.strip()
    }


def pending_adoption_entry(record: dict[str, Any]) -> dict[str, Any] | None:
    """Project one upstream company into a secret-free read-only summary.

    Upstream records carry member arrays and permission objects that must never
    reach the browser, so only counted and displayable fields are kept.
    """

    upstream_id = str(
        record.get("organization_id") or record.get("organizationId") or ""
    ).strip()
    if not upstream_id:
        return None
    name = str(
        record.get("organization_alias")
        or record.get("organizationAlias")
        or ""
    ).strip()
    members = record.get("members")
    teams = record.get("teams")
    try:
        spend = round(float(record.get("spend") or 0), 2)
    except (TypeError, ValueError):
        spend = 0.0
    return {
        "upstreamId": upstream_id,
        "name": name or upstream_id,
        "memberCount": len(members) if isinstance(members, list) else 0,
        "teamCount": len(teams) if isinstance(teams, list) else 0,
        "spendUsd": spend,
        "createdAt": str(record.get("created_at") or record.get("createdAt") or ""),
    }


async def pending_adoption_organizations() -> dict[str, Any]:
    """List real companies that still have no local customer record.

    A failure here must stay contained: the adopted customer directory is the
    primary content of the page and keeps rendering when the read fails.
    """

    if not organization_real_enabled():
        return {"items": [], "unavailable": False}
    repository = organization_store()
    if not isinstance(repository, PostgreSQLOrganizationRepository):
        return {"items": [], "unavailable": False}
    try:
        adopted = await repository.adopted_upstream_organization_ids()
    except Exception:
        logger.exception("failed to read adopted upstream organizations")
        return {"items": [], "unavailable": True}
    excluded = adopted | internal_upstream_organization_ids()
    # Adopting a company must drop it from this list immediately, so the
    # excluded set is part of the cache key rather than a plain TTL.
    cache_key = hashlib.sha256(
        "\x1f".join(sorted(excluded)).encode("utf-8")
    ).hexdigest()
    hit, value, _ = pending_adoption_cache.get(cache_key)
    if hit:
        return value
    backend_id = os.getenv("ORGANIZATION_ADOPTION_BACKEND_ID", "primary").strip() or "primary"
    upstream = client()
    backend = next((item for item in upstream.backends if item.id == backend_id), None)
    if backend is None:
        return {"items": [], "unavailable": True}
    try:
        records = await upstream.list_organizations(backend=backend)
    except Exception:
        logger.exception("failed to list upstream organizations for adoption")
        return {"items": [], "unavailable": True}
    items = []
    for record in records:
        if not isinstance(record, dict):
            continue
        entry = pending_adoption_entry(record)
        if entry is None or entry["upstreamId"] in excluded:
            continue
        items.append(entry)
    items.sort(key=lambda item: (-item["spendUsd"], item["name"]))
    payload = {"items": items, "unavailable": False}
    pending_adoption_cache.set(
        cache_key,
        payload,
        env_int("ORGANIZATION_PENDING_ADOPTION_CACHE_TTL_SECONDS", 300),
    )
    return payload


@app.get("/api/platform/organizations")
async def platform_organizations(
    request: Request,
    search: str = Query("", max_length=120),
    status: str = Query("", max_length=16),
    page: int = Query(1, ge=1, le=100000),
    pageSize: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """List seller-managed customer organizations, never customer memberships."""

    if not organization_enabled():
        raise HTTPException(status_code=404, detail="客户企业演示功能尚未启用")
    require_platform_admin(request)
    try:
        payload = await platform_organization_store_call(
            "list_organizations",
            keyword=search,
            status=status,
            page=page,
            page_size=pageSize,
            include_archived=True,
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    # Candidates are a property of the whole directory, not of one filtered
    # page. Only the unfiltered first page carries them.
    if page == 1 and not search and not status:
        payload = {**payload, "pendingAdoption": await pending_adoption_organizations()}
    return payload


@app.post("/api/platform/organizations")
async def platform_create_organization(
    data: PlatformOrganizationCreateRequest,
    request: Request,
) -> dict[str, Any]:
    """Create a Mock customer with its default department and first administrator."""

    if not organization_enabled():
        raise HTTPException(status_code=404, detail="客户企业演示功能尚未启用")
    await enforce_csrf(request)
    require_platform_admin(request)
    if organization_real_enabled():
        existing_account = await auth_store_call("get_user_by_email", data.adminEmail)
        if existing_account is None:
            raise auth_http_error(
                409,
                "首位企业管理员必须先完成通衢账号注册",
                "ORGANIZATION_ADMIN_ACCOUNT_NOT_FOUND",
            )
        if str(existing_account.get("status") or "") != "active" or str(
            existing_account.get("identity_status")
            or existing_account.get("identityStatus")
            or "verified"
        ) != "verified":
            raise auth_http_error(
                409,
                "首位企业管理员账号尚未完成身份验证",
                "ORGANIZATION_ADMIN_ACCOUNT_NOT_VERIFIED",
            )
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


@app.post("/api/platform/organizations/{organization_id}/restore")
async def platform_restore_organization(
    organization_id: str,
    request: Request,
    _data: OrganizationEmptyRequest | None = None,
) -> dict[str, Any]:
    """Undo an archive so a customer relationship can resume.

    Archiving revoked every token upstream and that is not reversible, so this
    restores the company, its departments and its members only. Tokens have to
    be issued again by the customer's own administrator.
    """

    await enforce_csrf(request)
    actor = await require_platform_organization(request, organization_id)
    try:
        organization = await platform_organization_store_call("restore_organization", organization_id)
    except OrganizationConflictError as exc:
        raise auth_http_error(
            409,
            "只有已归档的客户企业可以恢复，请确认当前状态",
            "ORGANIZATION_NOT_ARCHIVED",
        ) from exc
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    # Reviving a terminated customer relationship is high impact, so it is
    # recorded even though archiving predates the audit log.
    repository = organization_store()
    if isinstance(repository, PostgreSQLOrganizationRepository):
        await repository.record_audit(
            organization_id,
            "organization.restored",
            actor=str(actor.get("email") or actor.get("id") or "platform"),
            target_type="organization",
            target_id=organization_id,
            details={"toStatus": "active", "ipAddress": request_ip(request)},
        )
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
    if organization_real_enabled():
        require_real_organization_capability()
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


def require_platform_organization_repository() -> PostgreSQLOrganizationRepository:
    """Return the durable directory or refuse the identity-binding operation.

    Binding decisions are permanent records, so the Mock directory must never
    accept them: a bind that vanishes on restart is worse than a clear refusal.
    """

    repository = organization_store()
    if not isinstance(repository, PostgreSQLOrganizationRepository):
        raise auth_http_error(503, "企业身份数据暂不可用", "ORGANIZATION_IDENTITY_UNAVAILABLE")
    return repository


def platform_account_summary(account: dict[str, Any] | None) -> dict[str, Any] | None:
    """Describe a login account with the fields an operator needs to confirm it."""

    if not isinstance(account, dict):
        return None
    return {
        "id": str(account.get("id") or ""),
        "email": str(account.get("email") or ""),
        "loginName": str(account.get("login_name") or account.get("loginName") or ""),
        "status": str(account.get("status") or ""),
    }


def platform_identity_item(principal: dict[str, Any] | None) -> dict[str, Any]:
    """Re-shape one usage identity for the browser.

    The stored field is named after the upstream gateway's user ids; the
    employee-facing dialog only ever calls them 历史来源, so the browser payload
    must not carry provider vocabulary at all.
    """

    entry = dict(principal or {})
    entry["historySources"] = [
        str(item) for item in (entry.pop("upstreamUserIds", None) or []) if str(item)
    ]
    return entry


def platform_identity_list(principals: dict[str, Any] | None) -> dict[str, Any]:
    """Re-shape a whole usage-identity list for the browser."""

    items = [platform_identity_item(item) for item in ((principals or {}).get("items") or [])]
    return {"items": items, "total": len(items)}


async def platform_member_identity_payload(
    repository: PostgreSQLOrganizationRepository, organization_id: str, member_id: str
) -> dict[str, Any]:
    """Assemble everything the identity-binding dialog renders in one read."""

    member = await repository.get_member(member_id, organization_id=organization_id)
    if not isinstance(member, dict):
        raise auth_http_error(404, "未找到对应成员", "ORGANIZATION_MEMBER_NOT_FOUND")
    auth_user_id = str(member.get("authUserId") or "").strip()
    account = (
        await auth_store_call("get_user", auth_user_id) if auth_user_id else None
    )
    principals = await repository.list_principals(organization_id)
    return {
        "member": member,
        # A bound id whose account is gone still has to be visible, otherwise the
        # operator sees "未绑定" and cannot explain why the member has no usage.
        "account": platform_account_summary(account),
        "accountMissing": bool(auth_user_id) and account is None,
        "principals": platform_identity_list(principals),
    }


@app.get("/api/platform/organizations/{organization_id}/members/{member_id}/identity")
async def platform_member_identity(
    organization_id: str,
    member_id: str,
    request: Request,
) -> dict[str, Any]:
    """Show one member's login account and every usage identity in the tenant."""

    await require_platform_organization(request, organization_id)
    repository = require_platform_organization_repository()
    try:
        return await platform_member_identity_payload(repository, organization_id, member_id)
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc


@app.post("/api/platform/organizations/{organization_id}/members/{member_id}/account")
async def platform_bind_member_account(
    organization_id: str,
    member_id: str,
    data: OrganizationMemberAccountRequest,
    request: Request,
) -> dict[str, Any]:
    """Point a member at a login account, or release the current one.

    A temporary address gets replaced by a real one, so this has to work after
    activation. Sessions belonging to the replaced account are revoked because
    otherwise its browser would keep reading this customer's data.
    """

    await enforce_csrf(request)
    actor = await require_platform_organization(request, organization_id)
    repository = require_platform_organization_repository()
    identifier = data.identifier
    account: dict[str, Any] | None = None
    if identifier:
        account = await auth_store_call("get_user_by_identifier", identifier)
        if not isinstance(account, dict):
            raise auth_http_error(
                404,
                "找不到该登录账号，请确认对方已完成注册",
                "ORGANIZATION_ACCOUNT_NOT_FOUND",
            )
        if str(account.get("status") or "") != "active":
            raise auth_http_error(
                409,
                "该登录账号当前不可用，无法绑定",
                "ORGANIZATION_ACCOUNT_NOT_ACTIVE",
            )
    try:
        member = await repository.set_member_account(
            organization_id,
            member_id,
            str((account or {}).get("id") or "") if identifier else "",
        )
    except OrganizationConflictError as exc:
        raise auth_http_error(
            409,
            "该登录账号已绑定到其他成员，请先解除原有绑定",
            "ORGANIZATION_ACCOUNT_ALREADY_BOUND",
        ) from exc
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    previous_auth_user_id = str(member.pop("previousAuthUserId", "") or "")
    auth_user_id = str(member.get("authUserId") or "")
    if previous_auth_user_id and previous_auth_user_id != auth_user_id:
        await auth_store_call("revoke_user_sessions", previous_auth_user_id)
    await repository.record_audit(
        organization_id,
        "organization.member.account_bound" if identifier else "organization.member.account_unbound",
        actor=str(actor.get("email") or actor.get("id") or "platform"),
        target_type="member",
        target_id=member_id,
        details={
            "memberId": member_id,
            "authUserId": auth_user_id,
            "previousAuthUserId": previous_auth_user_id,
            "ipAddress": request_ip(request),
        },
    )
    invalidate_organization_usage_cache()
    return {
        "ok": True,
        **await platform_member_identity_payload(repository, organization_id, member_id),
    }


@app.post("/api/platform/organizations/{organization_id}/principals/{principal_id}/member")
async def platform_bind_principal_member(
    organization_id: str,
    principal_id: str,
    data: OrganizationPrincipalMemberRequest,
    request: Request,
) -> dict[str, Any]:
    """Attach a usage identity to a member so their spend is attributed."""

    await enforce_csrf(request)
    actor = await require_platform_organization(request, organization_id)
    repository = require_platform_organization_repository()
    member_id = data.memberId
    try:
        existing = await repository.get_principal(organization_id, principal_id)
        principal = await repository.set_principal_member(
            organization_id, principal_id, member_id or None
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    await repository.record_audit(
        organization_id,
        "organization.principal.bound" if member_id else "organization.principal.unbound",
        actor=str(actor.get("email") or actor.get("id") or "platform"),
        target_type="principal",
        target_id=principal_id,
        details={
            "principalId": principal_id,
            "memberId": member_id,
            "previousMemberId": str((existing or {}).get("memberId") or ""),
            "ipAddress": request_ip(request),
        },
    )
    invalidate_organization_usage_cache()
    # The dialog keeps its own member, so only the identity list has to be
    # refreshed here — the caller re-reads the member when it needs one.
    return {
        "ok": True,
        "principal": platform_identity_item(principal),
        "principals": platform_identity_list(await repository.list_principals(organization_id)),
    }


@app.post("/api/platform/organizations/{organization_id}/departments")
async def platform_create_department(
    organization_id: str, data: OrganizationDepartmentRequest, request: Request
) -> dict[str, Any]:
    if organization_real_enabled():
        require_real_organization_capability()
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
    if organization_real_enabled():
        require_real_organization_capability()
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
    if organization_real_enabled():
        require_real_organization_capability()
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
    if organization_real_enabled():
        require_real_organization_capability()
    await enforce_csrf(request)
    await require_platform_organization(request, organization_id)
    if organization_real_enabled():
        existing_account = await auth_store_call("get_user_by_email", data.email)
        if existing_account is None:
            raise auth_http_error(
                409,
                "该邮箱尚未注册通衢账号，请先完成账号注册后再邀请",
                "ORGANIZATION_MEMBER_ACCOUNT_NOT_FOUND",
            )
        if str(existing_account.get("status") or "") != "active" or str(
            existing_account.get("identity_status")
            or existing_account.get("identityStatus")
            or "verified"
        ) != "verified":
            raise auth_http_error(
                409,
                "该账号尚未完成身份验证，暂时不能加入企业",
                "ORGANIZATION_MEMBER_ACCOUNT_NOT_VERIFIED",
            )
    try:
        operation = "create_member_with_invitation" if organization_real_enabled() else "create_member"
        member = await organization_scoped_store_call(
            organization_id,
            operation,
            data.name,
            data.email,
            data.departmentId,
            data.role,
            team_role=data.teamRole,
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
    if organization_real_enabled():
        require_real_organization_capability()
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
    if "teamRole" in fields:
        updates["team_role"] = data.teamRole
    if "status" in fields:
        reject_member_removal_via_update(data.status)
        reject_direct_real_member_activation(data.status)
        updates["status"] = "invited" if data.status == "pending" else data.status
    # 登录名只在平台侧放开：让客户管理员改别人的登录名等于交出账号接管能力。
    # Mock 目录里的成员没有登录名这个概念，所以那里直接拒绝而不是静默忽略。
    if "loginName" in fields:
        require_platform_organization_repository()
        updates["login_name"] = data.loginName
    try:
        member = await organization_scoped_store_call(
            organization_id, "update_member", member_id, **updates
        )
        if organization_real_enabled() and updates.get("status") == "invited":
            store = organization_store()
            if isinstance(store, PostgreSQLOrganizationRepository):
                await store.create_invitation(organization_id, member_id)
    except OrganizationConflictError as exc:
        # 通用冲突文案（"请先调整成员或管理员"）解释不了登录名被占用这种情况。
        if "loginName" in fields:
            raise auth_http_error(
                409,
                "该登录名已被占用，请换一个",
                "ORGANIZATION_LOGIN_NAME_TAKEN",
            ) from exc
        raise organization_store_error(exc) from exc
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    invalidate_organization_usage_cache()
    return {"member": member}


@app.delete("/api/platform/organizations/{organization_id}/members/{member_id}")
async def platform_remove_member(
    organization_id: str,
    member_id: str,
    request: Request,
) -> dict[str, Any]:
    """Let seller operations move an invited or suspended member out of a customer company."""

    if organization_real_enabled():
        require_real_organization_capability()
    await enforce_csrf(request)
    await require_platform_organization(request, organization_id)
    member = await remove_organization_member(organization_id, member_id)
    return {"ok": True, "member": member}


@app.post("/api/platform/organizations/{organization_id}/members/{member_id}/invitation/resend")
async def platform_resend_member_invitation(
    organization_id: str,
    member_id: str,
    request: Request,
    _data: OrganizationInvitationMutationRequest | None = None,
) -> dict[str, Any]:
    """Let a seller operator resend one customer's pending invitation."""

    if not organization_real_enabled():
        raise HTTPException(status_code=404, detail="企业邀请功能尚未开放")
    await enforce_csrf(request)
    await require_platform_organization(request, organization_id)
    try:
        invitation = await resend_real_member_invitation(organization_id, member_id)
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    return {"ok": True, "invitation": invitation}


def configured_organization_adoption_values(name: str) -> list[str]:
    """Resolve server-owned pilot candidates without trusting browser values."""

    return list(
        dict.fromkeys(
            item.strip()
            for item in os.getenv(name, "").split(",")
            if item.strip()
        )
    )


def organization_adoption_stable_id(kind: str, *values: str) -> str:
    """Derive retry-stable, non-secret upstream identifiers for one pilot."""

    material = "\x1f".join(str(value or "").strip().casefold() for value in values)
    value = uuid.uuid5(uuid.NAMESPACE_URL, f"ai-token-dashboard:{kind}:{material}")
    return f"customer-{kind}-{value}"


def parse_optional_upstream_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OrganizationConflictError(
            "upstream key expiry snapshot is invalid"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def adoption_upstream_metadata(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("metadata")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def ensure_adoption_upstream_scope(
    upstream: Any,
    backend: Any,
    *,
    source: dict[str, Any],
    organization_name: str,
    department_name: str,
    changed_by: str,
) -> tuple[str, str]:
    """Create-or-recover the exact upstream scope planned by the preview."""

    action = str(source.get("action") or "adopt")
    organization_id = str(source.get("upstreamOrganizationId") or "").strip()
    team_id = str(source.get("upstreamTeamId") or "").strip()
    if action != "create":
        return organization_id, team_id
    if not organization_id or not team_id:
        raise OrganizationConflictError("planned upstream scope is incomplete")

    expected_org_metadata = {
        "adoption_operation": "baic-pilot",
        "created_via": "ai-token-dashboard",
    }
    organizations = await upstream.find_organizations_exact(
        organization_id=organization_id, backend=backend
    )
    if not organizations:
        try:
            await upstream.create_organization(
                organization_name,
                organization_id=organization_id,
                metadata=expected_org_metadata,
                changed_by=changed_by,
                backend=backend,
            )
        except Exception:
            organizations = await upstream.find_organizations_exact(
                organization_id=organization_id, backend=backend
            )
            if not organizations:
                raise
        else:
            organizations = await upstream.find_organizations_exact(
                organization_id=organization_id, backend=backend
            )
    if len(organizations) != 1:
        raise OrganizationConflictError("planned upstream organization is ambiguous")
    organization_record = organizations[0]
    organization_alias = str(
        organization_record.get("organization_alias")
        or organization_record.get("organizationAlias")
        or organization_record.get("name")
        or ""
    ).strip()
    organization_metadata = adoption_upstream_metadata(organization_record)
    if (
        organization_alias.casefold() != organization_name.casefold()
        or any(
            str(organization_metadata.get(key) or "") != value
            for key, value in expected_org_metadata.items()
        )
    ):
        raise OrganizationConflictError("planned upstream organization fingerprint changed")

    expected_team_metadata = {
        "adoption_operation": "baic-pilot",
        "created_via": "ai-token-dashboard",
        "organization_id": organization_id,
    }
    teams = await upstream.find_teams_exact(
        organization_id=organization_id, team_id=team_id, backend=backend
    )
    if not teams:
        try:
            await upstream.create_team(
                department_name,
                organization_id,
                team_id=team_id,
                metadata=expected_team_metadata,
                changed_by=changed_by,
                backend=backend,
            )
        except Exception:
            teams = await upstream.find_teams_exact(
                organization_id=organization_id, team_id=team_id, backend=backend
            )
            if not teams:
                raise
        else:
            teams = await upstream.find_teams_exact(
                organization_id=organization_id, team_id=team_id, backend=backend
            )
    if len(teams) != 1:
        raise OrganizationConflictError("planned upstream team is ambiguous")
    team_record = teams[0]
    team_alias = str(
        team_record.get("team_alias")
        or team_record.get("teamAlias")
        or team_record.get("name")
        or ""
    ).strip()
    team_organization_id = str(
        team_record.get("organization_id")
        or team_record.get("organizationId")
        or team_record.get("org_id")
        or ""
    ).strip()
    team_metadata = adoption_upstream_metadata(team_record)
    if (
        team_alias.casefold() != department_name.casefold()
        or team_organization_id != organization_id
        or any(
            str(team_metadata.get(key) or "") != value
            for key, value in expected_team_metadata.items()
        )
    ):
        raise OrganizationConflictError("planned upstream team fingerprint changed")
    return organization_id, team_id


async def organization_adoption_preview_payload(
    data: OrganizationAdoptionPreviewRequest,
) -> dict[str, Any]:
    """Build a secret-free fingerprint from exact read-only upstream matches."""

    if data.effectiveFrom > data.effectiveThrough:
        raise auth_http_error(
            400,
            "历史用量时间范围无效",
            "ORGANIZATION_ADOPTION_INVALID_WINDOW",
        )
    if (
        data.organizationName != "北汽集团"
        or data.departmentName != "企业管理"
        or data.adminName != "David Zhu"
        or data.adminEmail.casefold() != "davidzhu2021@163.com"
        or data.principalName != "梁海强"
    ):
        raise auth_http_error(
            409,
            "接管请求与北汽试点预留信息不一致",
            "ORGANIZATION_ADOPTION_PILOT_MISMATCH",
        )
    if data.effectiveThrough > usage_today() + timedelta(days=3660):
        raise auth_http_error(
            400,
            "历史资产归属截止时间超出允许范围",
            "ORGANIZATION_ADOPTION_INVALID_WINDOW",
        )
    account = await auth_store_call("get_user_by_email", data.adminEmail)
    if not account or str(account.get("status") or "") != "active" or str(
        account.get("identity_status") or account.get("identityStatus") or "verified"
    ) != "verified":
        raise auth_http_error(
            409,
            "临时管理员账号不存在或尚未完成验证",
            "ORGANIZATION_ADOPTION_ADMIN_INVALID",
        )

    configured_organizations = configured_organization_adoption_values(
        "ORGANIZATION_ADOPTION_ORGANIZATION_CANDIDATES"
    )
    configured_teams = configured_organization_adoption_values(
        "ORGANIZATION_ADOPTION_TEAM_CANDIDATES"
    )
    configured_aliases = configured_organization_adoption_values(
        "ORGANIZATION_ADOPTION_KEY_ALIASES"
    )
    # Candidate identifiers are intentionally server-owned. The request fields
    # remain in the schema for forward-compatible clients, but are ignored in
    # real mode so a browser cannot steer an adoption lookup at another tenant.
    organization_candidates = configured_organizations
    team_candidates = configured_teams
    key_aliases = configured_aliases
    if not organization_candidates or not team_candidates or not key_aliases:
        raise auth_http_error(
            503,
            "接管候选对象尚未在服务端配置",
            "ORGANIZATION_ADOPTION_NOT_CONFIGURED",
        )
    backend_id = os.getenv("ORGANIZATION_ADOPTION_BACKEND_ID", "primary").strip() or "primary"
    upstream = client()
    backend = next((item for item in upstream.backends if item.id == backend_id), None)
    if backend is None:
        raise auth_http_error(
            503,
            "接管数据来源不可用",
            "ORGANIZATION_ADOPTION_BACKEND_UNAVAILABLE",
        )

    async def exact_organization_candidates(candidate: str) -> list[dict[str, Any]]:
        value = candidate.strip()
        if value.startswith("id:"):
            return await upstream.find_organizations_exact(
                organization_id=value[3:].strip(), backend=backend
            )
        if value.startswith("alias:"):
            return await upstream.find_organizations_exact(
                organization_alias=value[6:].strip(), backend=backend
            )
        by_id = await upstream.find_organizations_exact(
            organization_id=value, backend=backend
        )
        by_alias = await upstream.find_organizations_exact(
            organization_alias=value, backend=backend
        )
        return [*by_id, *by_alias]

    async def exact_team_candidates(candidate: str) -> list[dict[str, Any]]:
        value = candidate.strip()
        if value.startswith("id:"):
            return await upstream.find_teams_exact(
                organization_id=upstream_organization_id,
                team_id=value[3:].strip(),
                backend=backend,
            )
        if value.startswith("alias:"):
            return await upstream.find_teams_exact(
                organization_id=upstream_organization_id,
                team_alias=value[6:].strip(),
                backend=backend,
            )
        by_id = await upstream.find_teams_exact(
            organization_id=upstream_organization_id,
            team_id=value,
            backend=backend,
        )
        by_alias = await upstream.find_teams_exact(
            organization_id=upstream_organization_id,
            team_alias=value,
            backend=backend,
        )
        return [*by_id, *by_alias]

    organization_matches: dict[str, dict[str, Any]] = {}
    for candidate in organization_candidates:
        matches = await exact_organization_candidates(candidate)
        for record in matches:
            identity = str(
                record.get("organization_id")
                or record.get("organizationId")
                or record.get("id")
                or ""
            ).strip()
            if identity:
                organization_matches[identity] = record
    if len(organization_matches) > 1:
        raise auth_http_error(
            409,
            "企业候选对象不是唯一匹配，未执行任何写入",
            "ORGANIZATION_ADOPTION_CONFLICT",
        )
    upstream_organization_id = next(iter(organization_matches), "")

    team_matches: dict[str, dict[str, Any]] = {}
    if upstream_organization_id:
        for candidate in team_candidates:
            matches = await exact_team_candidates(candidate)
            for record in matches:
                identity = str(record.get("team_id") or record.get("teamId") or record.get("id") or "").strip()
                if identity:
                    team_matches[identity] = record
    else:
        # A zero-match create is safe only when none of the configured stable
        # team candidates already exists anywhere. An orphan Team is a
        # partial upstream state and must be resolved by an operator.
        for candidate in team_candidates:
            value = candidate.strip()
            if value.startswith("id:"):
                matches = await upstream.find_teams_exact(
                    team_id=value[3:].strip(), backend=backend
                )
            elif value.startswith("alias:"):
                matches = await upstream.find_teams_exact(
                    team_alias=value[6:].strip(), backend=backend
                )
            else:
                matches = [
                    *await upstream.find_teams_exact(team_id=value, backend=backend),
                    *await upstream.find_teams_exact(team_alias=value, backend=backend),
                ]
            for record in matches:
                identity = str(
                    record.get("team_id")
                    or record.get("teamId")
                    or record.get("id")
                    or ""
                ).strip()
                if identity:
                    team_matches[identity] = record
    if len(team_matches) > 1 or bool(upstream_organization_id) != bool(team_matches):
        raise auth_http_error(
            409,
            "部门候选对象不是唯一匹配，未执行任何写入",
            "ORGANIZATION_ADOPTION_CONFLICT",
        )
    action = "adopt" if upstream_organization_id else "create"
    upstream_team_id = next(iter(team_matches), "")
    if action == "create":
        stable_material = (
            backend_id,
            data.organizationName,
            data.departmentName,
            data.adminEmail,
            data.principalName,
            *sorted(key_aliases),
        )
        upstream_organization_id = organization_adoption_stable_id(
            "organization", *stable_material
        )
        upstream_team_id = organization_adoption_stable_id("team", *stable_material)

    asset_records: list[dict[str, str]] = []
    seen_hashes: set[str] = set()
    user_ids: set[str] = set()
    for alias in key_aliases:
        matches = await upstream.list_keys_exact(key_alias=alias, backend=backend)
        if len(matches) != 1:
            raise auth_http_error(
                409,
                "历史资产不是唯一匹配，未执行任何写入",
                "ORGANIZATION_ADOPTION_CONFLICT",
            )
        identity = upstream.report_only_key_identity(matches[0])
        key_hash = str(identity.get("hash") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", key_hash):
            raise auth_http_error(
                409,
                "历史资产缺少稳定标识，未执行任何写入",
                "ORGANIZATION_ADOPTION_CONFLICT",
            )
        scope_matches = (
            identity.get("organizationId") == upstream_organization_id
            and identity.get("teamId") == upstream_team_id
        ) if action == "adopt" else (
            not str(identity.get("organizationId") or "").strip()
            and not str(identity.get("teamId") or "").strip()
        )
        if (
            key_hash in seen_hashes
            or not scope_matches
            or not str(identity.get("userId") or "").strip()
        ):
            raise auth_http_error(
                409,
                "历史资产范围与候选企业不一致，未执行任何写入",
                "ORGANIZATION_ADOPTION_CONFLICT",
            )
        seen_hashes.add(key_hash)
        user_ids.add(str(identity.get("userId") or ""))
        asset_records.append(
            {
                "alias": alias,
                "keyHash": key_hash,
                "maskedKey": f"sha256:...{key_hash[-8:]}",
                "organizationId": upstream_organization_id,
                "teamId": upstream_team_id,
                "userId": str(identity.get("userId") or ""),
                "models": list(identity.get("models") or []),
                "maxBudget": identity.get("maxBudget"),
                "budgetDuration": str(identity.get("budgetDuration") or ""),
                "spend": identity.get("spend"),
                "expiresAt": str(identity.get("expiresAt") or ""),
                "blocked": bool(identity.get("blocked")),
            }
        )
    fingerprint_source = {
        "organizationName": data.organizationName,
        "departmentName": data.departmentName,
        "adminName": data.adminName,
        "adminEmail": data.adminEmail.lower(),
        "principalName": data.principalName,
        "effectiveFrom": data.effectiveFrom.isoformat(),
        "effectiveThrough": data.effectiveThrough.isoformat(),
        "backendId": backend_id,
        "action": action,
        "upstreamOrganizationId": upstream_organization_id,
        "upstreamTeamId": upstream_team_id,
        "assets": sorted(asset_records, key=lambda item: item["keyHash"]),
    }
    preview_fingerprint = hashlib.sha256(
        json.dumps(fingerprint_source, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "status": "ready",
        "previewFingerprint": preview_fingerprint,
        "idempotencyKey": hashlib.sha256(
            json.dumps(fingerprint_source, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:32],
        "organization": {
            "action": action,
            "name": data.organizationName,
        },
        "department": {
            "action": action,
            "name": data.departmentName,
            "scopeConsistent": True,
        },
        "legacyAssets": {
            "count": len(asset_records),
            "originalIdentityCount": len({item for item in user_ids if item}),
            "principalName": data.principalName,
            "scopeConsistent": True,
            "items": [
                {
                    "maskedKey": item["maskedKey"],
                    "managementMode": "read_only",
                    "billingMode": "report_only",
                }
                for item in asset_records
            ],
        },
        "_apply": fingerprint_source,
    }


async def replayed_organization_adoption(
    data: OrganizationAdoptionApplyRequest,
    repository: PostgreSQLOrganizationRepository,
) -> dict[str, Any] | None:
    """Return a completed adoption before requiring the upstream preflight.

    A retry after a successful apply must still work when the upstream is
    temporarily unavailable. The stored request/preview fingerprints prove
    that the idempotency key is being reused for the same public request.
    """

    operation = await repository.get_adoption_operation(data.idempotencyKey)
    if not operation or str(operation.get("status") or "") != "applied":
        return None
    public_request = data.model_dump(
        exclude={"previewFingerprint", "idempotencyKey"}, mode="json"
    )
    public_fingerprint = hashlib.sha256(
        json.dumps(
            public_request,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if (
        str(operation.get("previewFingerprint") or "") != data.previewFingerprint
        or str(operation.get("requestFingerprint") or "") != public_fingerprint
    ):
        raise auth_http_error(
            409,
            "幂等键已用于另一笔企业接管",
            "ORGANIZATION_ADOPTION_CONFLICT",
        )
    result = operation.get("result") or {}
    return result if isinstance(result, dict) else {"ok": True, "status": "applied"}


@app.post("/api/platform/organization-adoptions/preview")
async def platform_organization_adoption_preview(
    data: OrganizationAdoptionPreviewRequest,
    request: Request,
) -> dict[str, Any]:
    if not organization_real_enabled():
        raise HTTPException(status_code=404, detail="企业接管功能尚未开放")
    require_real_organization_capability()
    await enforce_csrf(request)
    require_platform_admin(request)
    payload = await organization_adoption_preview_payload(data)
    payload.pop("_apply", None)
    return payload


@app.post("/api/platform/organization-adoptions/apply")
async def platform_organization_adoption_apply(
    data: OrganizationAdoptionApplyRequest,
    request: Request,
) -> dict[str, Any]:
    """Apply one previously fingerprinted, server-preflighted pilot adoption."""

    if not organization_real_enabled():
        raise HTTPException(status_code=404, detail="企业接管功能尚未开放")
    require_real_organization_capability()
    await enforce_csrf(request)
    actor = require_platform_admin(request)
    repository = organization_store()
    if not isinstance(repository, PostgreSQLOrganizationRepository):
        raise auth_http_error(
            503,
            "企业接管持久化能力暂不可用",
            "ORGANIZATION_ADOPTION_UNAVAILABLE",
        )
    replay = await replayed_organization_adoption(data, repository)
    if replay is not None:
        return replay
    preview = await organization_adoption_preview_payload(data)
    if preview.get("previewFingerprint") != data.previewFingerprint:
        raise auth_http_error(
            409,
            "接管预检指纹已变化，请重新执行预检",
            "ORGANIZATION_ADOPTION_CONFLICT",
        )
    source = preview.get("_apply") or {}
    upstream = client()
    operation_key = data.idempotencyKey.strip()
    request_fingerprint = hashlib.sha256(
        json.dumps(
            data.model_dump(
                exclude={"previewFingerprint", "idempotencyKey"}, mode="json"
            ),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    actor_id = str(actor.get("email") or actor.get("id") or "platform")
    try:
        operation = await repository.begin_adoption_operation(
            operation_key,
            request_fingerprint=request_fingerprint,
            preview_fingerprint=data.previewFingerprint,
            actor=actor_id,
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    if str(operation.get("status") or "") == "applied":
        result = operation.get("result") or {}
        return result if isinstance(result, dict) else {"ok": True, "status": "applied"}

    operation_id = str(operation["id"])
    try:
        backend = next(
            (
                item
                for item in upstream.backends
                if item.id == source.get("backendId", "primary")
            ),
            None,
        )
        if backend is None:
            raise RuntimeError("adoption backend is unavailable")
        upstream_organization_id, upstream_team_id = (
            await ensure_adoption_upstream_scope(
                upstream,
                backend,
                source=source,
                organization_name=data.organizationName,
                department_name=data.departmentName,
                changed_by=actor_id,
            )
        )

        # Mappings created by a prior partial attempt are safe only when both
        # point to the same local organization selected by this operation.
        mapped_org = await repository.get_organization_by_upstream_id(
            upstream_organization_id
        )
        mapped_team = await repository.get_department_by_upstream_id(upstream_team_id)
        operation_organization_id = str(operation.get("organizationId") or "")
        if mapped_org and not operation_organization_id:
            operation_organization_id = str(mapped_org.get("id") or "")
        if mapped_org and operation_organization_id and str(mapped_org.get("id")) != operation_organization_id:
            raise OrganizationConflictError("upstream organization belongs to another adoption")
        if mapped_team and operation_organization_id and str(mapped_team.get("organizationId") or "") not in {"", operation_organization_id}:
            raise OrganizationConflictError("upstream team belongs to another adoption")
        if mapped_org and mapped_team:
            if str(mapped_team.get("organizationId") or "") != str(mapped_org.get("id") or ""):
                raise OrganizationConflictError("upstream organization and team mappings conflict")
            organization_id = str(mapped_org["id"])
            department_id = str(mapped_team["id"])
        elif mapped_org or mapped_team:
            raise OrganizationConflictError("upstream adoption mapping is incomplete")
        else:
            organizations = await repository.list_organizations(
                keyword=data.organizationName,
                include_archived=False,
                page=1,
                page_size=10,
            )
            items = [
                item for item in (organizations.get("items") or [])
                if str(item.get("name") or "").casefold() == data.organizationName.casefold()
            ]
            if len(items) > 1:
                raise OrganizationConflictError("local organization name is ambiguous")
            if items:
                organization_id = str(items[0]["id"])
                snapshot = await repository.get_organization_snapshot(organization_id)
                departments = [
                    item for item in snapshot.get("departments", [])
                    if str(item.get("name") or "").casefold() == data.departmentName.casefold()
                ]
                if len(departments) != 1:
                    raise OrganizationConflictError("local department is ambiguous")
                department_id = str(departments[0]["id"])
            else:
                created = await repository.create_organization_with_admin(
                    data.organizationName,
                    data.adminName,
                    data.adminEmail,
                    default_department_name=data.departmentName,
                )
                organization_id = str(created["organization"]["id"])
                department_id = str(created["department"]["id"])
            await repository.adopt_existing_upstream_scope(
                organization_id,
                department_id,
                upstream_organization_id=upstream_organization_id,
                upstream_team_id=upstream_team_id,
            )

        members = await repository.list_members(
            organization_id=organization_id,
            keyword=data.adminEmail,
            page=1,
            page_size=10,
        )
        admin_member = next(
            (
                item for item in members.get("items", [])
                if str(item.get("email") or "").casefold() == data.adminEmail.casefold()
            ),
            None,
        )
        if admin_member is None:
            admin_member = await repository.create_member_with_invitation(
                data.adminName,
                data.adminEmail,
                department_id,
                "admin",
                team_role="leader",
                organization_id=organization_id,
            )
        else:
            await repository.ensure_member_invitation(
                organization_id, str(admin_member["id"])
            )

        principal = await repository.ensure_principal(
            organization_id, data.principalName
        )
        imported: list[dict[str, Any]] = []
        effective_from = datetime.combine(
            data.effectiveFrom, datetime.min.time(), tzinfo=timezone.utc
        )
        # Imported legacy keys remain report-only for future requests until a
        # separate, explicit managed-key migration closes this open interval.
        effective_through = None
        for item in source.get("assets", []):
            alias = str(item.get("alias") or "")
            records = await upstream.list_keys_exact(key_alias=alias, backend=backend)
            if len(records) != 1:
                raise OrganizationConflictError("report-only key match changed")
            identity = upstream.report_only_key_identity(records[0])
            action = str(source.get("action") or "adopt")
            identity_organization_id = str(identity.get("organizationId") or "")
            identity_team_id = str(identity.get("teamId") or "")
            expected_scope = (
                identity_organization_id == upstream_organization_id
                and identity_team_id == upstream_team_id
            ) if action == "adopt" else (
                not identity_organization_id and not identity_team_id
            )
            if (
                str(identity.get("hash") or "") != str(item.get("keyHash") or "")
                or not expected_scope
                or str(identity.get("userId") or "") != str(item.get("userId") or "")
            ):
                raise OrganizationConflictError("report-only key fingerprint changed")
            await repository.attach_principal_upstream_identity(
                str(principal["id"]),
                organization_id=organization_id,
                backend_id=str(source.get("backendId") or "primary"),
                upstream_user_id=str(identity.get("userId") or ""),
            )
            imported.append(
                await repository.import_report_only_key_identity(
                    organization_id,
                    backend_id=str(source.get("backendId") or "primary"),
                    upstream_key_hash=str(identity.get("hash") or ""),
                    upstream_key_id=str(identity.get("id") or ""),
                    key_alias=alias,
                    principal_id=str(principal["id"]),
                    member_id="",
                    department_id=department_id,
                    effective_from=effective_from,
                    effective_through=effective_through,
                    idempotency_key=f"{operation_key}:{identity['hash']}",
                    upstream_organization_id_snapshot=upstream_organization_id,
                    upstream_team_id_snapshot=upstream_team_id,
                    upstream_user_id_snapshot=str(identity.get("userId") or ""),
                    models_snapshot=list(identity.get("models") or []),
                    max_budget_usd_snapshot=identity.get("maxBudget"),
                    spend_usd_snapshot=identity.get("spend"),
                    budget_duration_snapshot=str(
                        identity.get("budgetDuration") or ""
                    ),
                    expires_at_snapshot=parse_optional_upstream_datetime(
                        identity.get("expiresAt")
                    ),
                    blocked_snapshot=bool(identity.get("blocked")),
                    import_batch_id=operation_key,
                    reporting_requested_through=data.effectiveThrough,
                    actor=actor_id,
                )
            )
        # Queue historical log backfill separately from the durable asset
        # import. Each worker window is capped at three days and can be
        # retried without changing the report-only billing policy.
        for imported_item in imported:
            await repository.ensure_usage_backfill(
                organization_id,
                principal_id=str(principal["id"]),
                usage_key_identity_id=str(imported_item["id"]),
                backend_id=str(source.get("backendId") or "primary"),
                requested_from=data.effectiveFrom,
                requested_through=data.effectiveThrough,
                import_batch_id=operation_key,
            )
        result = {
            "ok": True,
            "status": "applied",
            "organization": await repository.get_organization(organization_id),
            "admin": {
                "memberId": str(admin_member["id"]),
                "status": str(admin_member.get("status") or "invited"),
            },
            "principal": {
                "id": str(principal["id"]),
                "name": str(principal.get("name") or data.principalName),
            },
            "legacyAssets": {"count": len(imported), "items": imported},
        }
        await repository.record_audit(
            organization_id,
            "organization.adoption.applied",
            actor=actor_id,
            target_type="adoption",
            target_id=operation_key,
            details={
                "previewFingerprint": data.previewFingerprint,
                "legacyAssetCount": len(imported),
                "principalId": str(principal["id"]),
            },
        )
        await repository.complete_adoption_operation(
            operation_id, organization_id, result
        )
        return result
    except Exception as exc:
        await repository.fail_adoption_operation(operation_id, str(exc))
        if isinstance(exc, OrganizationStoreError):
            raise organization_store_error(exc) from exc
        raise


@app.get("/api/platform/organizations/{organization_id}/membership-claims")
async def platform_membership_claims(
    organization_id: str,
    request: Request,
) -> dict[str, Any]:
    """List offline username claims for one selected customer."""

    if not organization_real_enabled():
        raise HTTPException(status_code=404, detail="企业账号认领功能尚未开放")
    selected = await require_platform_claim_organization(request, organization_id)
    items = await auth_store_call(
        "list_membership_claims", str(selected["selectedOrganizationId"]), 200
    )
    return {"items": items, "total": len(items)}


@app.post("/api/platform/organizations/{organization_id}/membership-claims")
async def platform_create_membership_claim(
    organization_id: str,
    data: OrganizationMembershipClaimCreateRequest,
    request: Request,
) -> dict[str, Any]:
    """Issue a short-lived link for an offline-verified enterprise account."""

    if not organization_real_enabled():
        raise HTTPException(status_code=404, detail="企业账号认领功能尚未开放")
    if not password_login_configured() or not turnstile_enabled() or not turnstile_configured():
        raise auth_http_error(
            503,
            "企业账号认领所需的登录与人机验证尚未配置完成",
            "ORGANIZATION_CLAIM_NOT_READY",
        )
    await enforce_csrf(request)
    selected = await require_platform_claim_organization(request, organization_id)
    actor = str(selected.get("email") or selected.get("id") or "platform")
    await enforce_rate_limit(
        "organization_claim_issue",
        f"{actor.casefold()}:{organization_id}:{data.loginName.casefold()}",
        10,
        3600,
    )
    repository = organization_store()
    if not isinstance(repository, PostgreSQLOrganizationRepository):
        raise auth_http_error(503, "企业账号数据暂不可用", "ORGANIZATION_CLAIM_UNAVAILABLE")
    organization = await repository.get_organization(organization_id)
    department = await repository.get_department(data.departmentId, organization_id=organization_id)
    if not organization or not department or str(department.get("status") or "active") != "active":
        raise auth_http_error(404, "未找到对应的企业或部门", "ORGANIZATION_CLAIM_SCOPE_NOT_FOUND")
    if str(organization.get("name") or "").strip() != "北汽集团":
        raise auth_http_error(
            409,
            "当前线下账号认领仅开放给北汽集团试点",
            "ORGANIZATION_CLAIM_PILOT_ONLY",
        )
    if (
        str(data.memberName or data.name or "").strip() != "梁海强"
        or str(data.loginName or "").strip().casefold() != "lianghaiqiang"
        or str(data.role or "") != "admin"
        or str(department.get("name") or "").strip() != "企业管理"
    ):
        raise auth_http_error(
            409,
            "北汽试点认领信息与预留身份不一致",
            "ORGANIZATION_CLAIM_IDENTITY_MISMATCH",
        )
    try:
        member_name = str(data.memberName or data.name or "梁海强").strip()
        principal = await repository.ensure_principal(
            organization_id, member_name
        )
        claim = await auth_store_call(
            "create_membership_claim",
            organization_id,
            str(organization["name"]),
            data.departmentId,
            member_name,
            data.loginName,
            data.role,
            datetime.now(timezone.utc) + timedelta(hours=2),
            actor,
            principal_id=str(principal["id"]),
        )
    except (DuplicateLoginNameError, MembershipClaimStateError) as exc:
        raise auth_http_error(409, str(exc), "ORGANIZATION_CLAIM_CONFLICT") from exc
    token = str(claim.pop("token", ""))
    await repository.record_audit(
        organization_id,
        "organization.membership_claim.created",
        actor=actor,
        target_type="membership_claim",
        target_id=str(claim.get("id") or ""),
        details={
            "loginName": str(claim.get("loginName") or ""),
            "principalId": str(claim.get("principalId") or ""),
            "departmentId": str(claim.get("departmentId") or ""),
            "role": str(claim.get("role") or ""),
            "toStatus": str(claim.get("status") or "pending"),
            "ipAddress": request_ip(request),
        },
    )
    base_url = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    activation_url = f"{base_url}/?organization_claim={token}"
    return {
        "ok": True,
        "claim": claim,
        # This is the only response that includes the plaintext claim token.
        "activationUrl": activation_url,
        "claimUrl": activation_url,
    }


@app.post(
    "/api/platform/organizations/{organization_id}/membership-claims/{claim_id}/approve"
)
async def platform_approve_membership_claim(
    organization_id: str,
    claim_id: str,
    request: Request,
    _data: OrganizationEmptyRequest | None = None,
) -> dict[str, Any]:
    """Approve identity, bind a local member, and queue upstream provisioning."""

    if not organization_real_enabled():
        raise HTTPException(status_code=404, detail="企业账号认领功能尚未开放")
    await enforce_csrf(request)
    selected = await require_platform_claim_organization(request, organization_id)
    actor = str(selected.get("email") or selected.get("id") or "platform")
    claim = await auth_store_call("get_membership_claim", claim_id)
    if not claim or str(claim.get("organizationId") or "") != organization_id:
        raise auth_http_error(404, "未找到对应的账号认领", "ORGANIZATION_CLAIM_NOT_FOUND")
    try:
        approved = await auth_store_call(
            "approve_membership_claim",
            claim_id,
            actor,
        )
        if not approved:
            raise auth_http_error(404, "未找到对应的账号认领", "ORGANIZATION_CLAIM_NOT_FOUND")
        repository = organization_store()
        if not isinstance(repository, PostgreSQLOrganizationRepository):
            raise RuntimeError("organization repository is unavailable")
        member = await repository.create_managed_member(
            str(approved["memberName"]),
            str(approved["loginName"]),
            str(approved["departmentId"]),
            str(approved.get("role") or "admin"),
            auth_user_id=str(approved["authUserId"]),
            team_role="leader" if str(approved.get("role")) == "admin" else "member",
            organization_id=organization_id,
        )
        principal_id = str(approved.get("principalId") or "")
        if principal_id:
            await repository.link_principal_member(
                organization_id, principal_id, str(member["id"])
            )
        await auth_store_call("mark_membership_claim_provisioning", claim_id, "")
        # Do not wait for the periodic worker when the upstream is healthy;
        # process the durable job once and then reconcile account activation.
        await organization_outbox_if_available(limit=20)
        claim_after = await auth_store_call("get_membership_claim", claim_id)
        await repository.record_audit(
            organization_id,
            "organization.membership_claim.approved",
            actor=actor,
            target_type="membership_claim",
            target_id=claim_id,
            details={
                "authUserId": str(approved.get("authUserId") or ""),
                "memberId": str(member.get("id") or ""),
                "principalId": principal_id,
                "fromStatus": str(claim.get("status") or ""),
                "toStatus": str((claim_after or {}).get("status") or "provisioning"),
                "ipAddress": request_ip(request),
            },
        )
    except MembershipClaimStateError as exc:
        raise auth_http_error(409, str(exc), "ORGANIZATION_CLAIM_CONFLICT") from exc
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    return {
        "ok": True,
        "status": str((claim_after or {}).get("status") or "provisioning"),
        "claim": claim_after,
        "member": member,
    }


@app.post(
    "/api/platform/organizations/{organization_id}/membership-claims/{claim_id}/revoke"
)
async def platform_revoke_membership_claim(
    organization_id: str,
    claim_id: str,
    request: Request,
    _data: OrganizationEmptyRequest | None = None,
) -> dict[str, Any]:
    if not organization_real_enabled():
        raise HTTPException(status_code=404, detail="企业账号认领功能尚未开放")
    await enforce_csrf(request)
    selected = await require_platform_claim_organization(request, organization_id)
    actor = str(selected.get("email") or selected.get("id") or "platform")
    claim = await auth_store_call("get_membership_claim", claim_id)
    if not claim or str(claim.get("organizationId") or "") != organization_id:
        raise auth_http_error(404, "未找到对应的账号认领", "ORGANIZATION_CLAIM_NOT_FOUND")
    try:
        revoked = await auth_store_call(
            "revoke_membership_claim",
            claim_id,
            actor,
        )
    except MembershipClaimStateError as exc:
        raise auth_http_error(409, str(exc), "ORGANIZATION_CLAIM_CONFLICT") from exc
    repository = organization_store()
    if not isinstance(repository, PostgreSQLOrganizationRepository):
        raise auth_http_error(503, "企业账号数据暂不可用", "ORGANIZATION_CLAIM_UNAVAILABLE")
    await repository.record_audit(
        organization_id,
        "organization.membership_claim.revoked",
        actor=actor,
        target_type="membership_claim",
        target_id=claim_id,
        details={
            "authUserId": str(claim.get("authUserId") or ""),
            "principalId": str(claim.get("principalId") or ""),
            "fromStatus": str(claim.get("status") or ""),
            "toStatus": str((revoked or {}).get("status") or "revoked"),
            "ipAddress": request_ip(request),
        },
    )
    return {"ok": True, "claim": revoked}


@app.post(
    "/api/platform/organizations/{organization_id}/membership-claims/{claim_id}/password-reset"
)
async def platform_reset_membership_claim_password(
    organization_id: str,
    claim_id: str,
    request: Request,
    _data: OrganizationEmptyRequest | None = None,
) -> dict[str, Any]:
    """Issue a one-time offline password reset link for an active claim."""

    if not organization_real_enabled():
        raise HTTPException(status_code=404, detail="企业账号认领功能尚未开放")
    if not password_login_configured():
        raise auth_http_error(
            503,
            "企业账号登录能力尚未配置完成",
            "ORGANIZATION_ACCOUNT_RESET_UNAVAILABLE",
        )
    await enforce_csrf(request)
    selected = await require_platform_claim_organization(request, organization_id)
    actor = str(selected.get("email") or selected.get("id") or "platform")
    claim = await auth_store_call("get_membership_claim", claim_id)
    if (
        not claim
        or str(claim.get("organizationId") or "") != organization_id
        or str(claim.get("status") or "") != "active"
        or not str(claim.get("authUserId") or "")
    ):
        raise auth_http_error(
            409,
            "只有已生效的企业账号可以签发密码重置链接",
            "ORGANIZATION_ACCOUNT_RESET_NOT_ALLOWED",
        )
    await enforce_rate_limit(
        "organization_managed_password_reset",
        f"{actor.casefold()}:{claim_id}",
        5,
        3600,
    )
    try:
        reset = await auth_store_call(
            "create_managed_account_password_reset",
            str(claim["authUserId"]),
            datetime.now(timezone.utc) + timedelta(hours=2),
        )
    except ManagedAccountPasswordResetError as exc:
        raise auth_http_error(
            409,
            str(exc),
            "ORGANIZATION_ACCOUNT_RESET_NOT_ALLOWED",
        ) from exc
    token = str(reset.pop("token", ""))
    repository = organization_store()
    if not isinstance(repository, PostgreSQLOrganizationRepository):
        raise auth_http_error(503, "企业账号数据暂不可用", "ORGANIZATION_CLAIM_UNAVAILABLE")
    await repository.record_audit(
        organization_id,
        "organization.membership_claim.password_reset_issued",
        actor=actor,
        target_type="membership_claim",
        target_id=claim_id,
        details={
            "authUserId": str(claim.get("authUserId") or ""),
            "loginName": str(claim.get("loginName") or ""),
            "expiresAt": str(reset.get("expiresAt") or ""),
            "ipAddress": request_ip(request),
        },
    )
    base_url = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    reset_url = f"{base_url}/?reset_token={token}"
    return {
        "ok": True,
        "reset": reset,
        # The plaintext reset token is present only in this response.
        "resetUrl": reset_url,
    }


@app.post("/api/platform/organizations/{organization_id}/members/{member_id}/invitation/revoke")
async def platform_revoke_member_invitation(
    organization_id: str,
    member_id: str,
    request: Request,
    _data: OrganizationInvitationMutationRequest | None = None,
) -> dict[str, Any]:
    """Let a seller operator revoke one customer's pending invitation."""

    if not organization_real_enabled():
        raise HTTPException(status_code=404, detail="企业邀请功能尚未开放")
    await enforce_csrf(request)
    await require_platform_organization(request, organization_id)
    try:
        await revoke_real_member_invitation(organization_id, member_id)
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    return {"ok": True, "memberId": member_id}


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
    start_date, end_date = resolve_usage_range(start_date, end_date)
    if organization_real_enabled():
        payload = await real_organization_usage_payload(
            organization_id, start_date=start_date, end_date=end_date, source=source,
            employee=(employee or "").strip(), refresh=refresh,
        )
    else:
        payload = await cached_mock_organization_usage_payload(
            "mock_organization_usage", organization_id, start_date=start_date,
            end_date=end_date, source=source, employee=(employee or "").strip(), refresh=refresh,
        )
    return {"startDate": start_date, "endDate": end_date, "source": source, "employee": (employee or "").strip(), **payload}


@app.get("/api/platform/organizations/{organization_id}/billing")
async def platform_organization_billing(
    organization_id: str,
    request: Request,
    page: int = Query(1, ge=1, le=100000),
    pageSize: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Allow the seller to read one customer's credit history."""

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


@app.post("/api/platform/organizations/{organization_id}/billing/adjustments")
async def platform_organization_billing_adjustment(
    organization_id: str,
    data: OrganizationCreditAdjustmentRequest,
    request: Request,
) -> dict[str, Any]:
    """Apply an idempotent grant/revoke entry to the enterprise ledger."""

    if not organization_enabled():
        raise HTTPException(status_code=404, detail="企业组织功能尚未启用")
    require_real_organization_capability()
    await enforce_csrf(request)
    selected = await require_platform_organization(request, organization_id)
    selected_id = str(selected["selectedOrganizationId"])
    try:
        ledger = organization_store()
        if not isinstance(ledger, PostgreSQLOrganizationRepository):
            # Demo mode keeps the legacy top-up UI, but seller adjustments are
            # intentionally real-only so tests cannot imply a production ledger.
            raise auth_http_error(410, "企业授信调整仅在真实模式可用", "ORGANIZATION_BILLING_ADJUSTMENT_UNAVAILABLE")
        result = await ledger.adjust_billing(
            selected_id,
            operation=data.operation,
            amount_usd=data.amountUsd,
            reason=data.reason,
            operator=str(selected.get("id") or selected.get("email") or "platform"),
            operator_email=str(selected.get("email") or ""),
            external_reference=data.externalReference,
            idempotency_key=data.idempotencyKey,
        )
    except OrganizationStoreError as exc:
        raise organization_store_error(exc) from exc
    return {"ok": True, "record": result}


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
    start_date, end_date = resolve_usage_range(start_date, end_date)
    if organization_real_enabled():
        payload = await real_organization_department_usage_payload(
            organization_id, start_date=start_date, end_date=end_date, source=source,
            department=(department or "").strip(), refresh=refresh,
        )
    else:
        payload = await cached_mock_organization_usage_payload(
            "mock_department_usage", organization_id, start_date=start_date,
            end_date=end_date, source=source, department=(department or "").strip(), refresh=refresh,
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
    if organization_real_enabled() and await active_real_organization_membership(app_user):
        start_date, end_date = resolve_usage_range(start_date, end_date)
        return await personal_usage_payload(app_user, start_date, end_date, source, refresh)
    if await is_demo_customer_user(app_user):
        start_date, end_date = resolve_usage_range(start_date, end_date)
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
        # Session hydration already resolved the entitlement for this request.
        # Rebuilding the local profile here may read the TTL cache, but must not
        # force another upstream user lookup on every dashboard refresh.
        app_user = await auth_user_payload(local_user)
        require_active_local_entitlement(app_user)
    start_date, end_date = resolve_usage_range(start_date, end_date)
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
        start_date, end_date = resolve_usage_range(start_date, end_date)
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
    if app_user.get("id") and not organization_real_enabled():
        # Password and enterprise SSO accounts remain separate identities.  Only a
        # real customer's own department-leader membership opens a team board; the
        # leader check inside team_usage_payload still guards that path.
        raise auth_http_error(403, "当前账号还没有团队负责人权限", "AUTH_TEAM_SCOPE_UNAVAILABLE")
    start_date, end_date = resolve_usage_range(start_date, end_date)
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
        start_date, end_date = resolve_usage_range(start_date, end_date)
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
    if app_user.get("id") and not organization_real_enabled():
        # Local accounts never inherit team scopes from a same-email SSO user; the
        # real-mode leader scope comes from the membership bound to this account.
        raise auth_http_error(403, "当前账号还没有团队负责人权限", "AUTH_TEAM_SCOPE_UNAVAILABLE")
    start_date, end_date = resolve_usage_range(start_date, end_date)
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
    if organization_real_enabled() and await active_real_organization_membership(app_user):
        start_date, end_date = resolve_usage_range(start_date, end_date)
        payload = await personal_usage_payload(app_user, start_date, end_date, source)
        rows = payload["rows"]
        start = (page - 1) * page_size
        return {
            "user": app_user,
            "rows": rows[start : start + page_size],
            "total": len(rows),
            "page": page,
            "pageSize": page_size,
            "cache": payload.get("cache", {"hit": False, "ttlSeconds": 0}),
        }
    if await is_demo_customer_user(app_user):
        start_date, end_date = resolve_usage_range(start_date, end_date)
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
        app_user = await auth_user_payload(local_user)
        require_active_local_entitlement(app_user)
    start_date, end_date = resolve_usage_range(start_date, end_date)
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
    start_date, end_date = resolve_usage_range(start_date, end_date)
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
    start_date, end_date = resolve_usage_range(start_date, end_date)
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
    start_date, end_date = resolve_usage_range(start_date, end_date)
    payload = await department_usage_payload(admin, start_date, end_date, source, department, refresh)
    return {
        "admin": {"email": admin["email"], "name": admin["name"]},
        "startDate": start_date,
        "endDate": end_date,
        "source": source,
        "department": department or "",
        **payload,
    }


def _admin_observability_store() -> UsageStore:
    if not env_bool("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", False):
        raise HTTPException(status_code=404, detail="看板尚未开放")
    require = usage_store()
    if require is None:
        raise HTTPException(status_code=503, detail="看板数据暂不可用")
    return require


def _observability_envelope(data: Any, *, freshness: dict[str, Any] | None = None, coverage: dict[str, Any] | None = None, source: str = "usage snapshot") -> dict[str, Any]:
    return {
        "data": data,
        "freshness": freshness or {"status": "unknown"},
        "coverage": coverage or {"partial": False, "incomplete": False},
        "source": source,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }


def _stability_missing_reasons(
    sync_states: list[dict[str, Any]],
    *,
    start_date: str,
    end_date: str,
    configured_backends: set[str],
    event_count: int,
) -> tuple[bool, list[str]]:
    """Return window coverage and stable machine-readable gaps for the UI."""

    states_by_backend = {
        str(item.get("backend_id") or ""): item
        for item in sync_states
        if str(item.get("backend_id") or "")
    }
    expected = configured_backends or set(states_by_backend)
    reasons: list[str] = []
    if not expected:
        reasons.append("not_synced")
    for backend_id in sorted(expected):
        state = states_by_backend.get(backend_id)
        if state is None:
            reasons.append("not_synced")
            continue
        if bool(state.get("partial")):
            reasons.append("partial_scan")
        if str(state.get("status") or "").lower() in {"error", "failed", "failure"}:
            reasons.append("sync_error")
        window_start = str(state.get("window_start") or "")[:10]
        window_end = str(state.get("window_end") or "")[:10]
        if not window_start or not window_end or window_start > start_date or window_end < end_date:
            reasons.append("backfill_pending")
    if not event_count and not reasons:
        # An empty but otherwise complete window is useful data. Preserve the
        # distinction for filters and operators without calling it incomplete.
        reasons.append("no_events_or_filter_match")
    return not any(reason in {"not_synced", "partial_scan", "sync_error", "backfill_pending"} for reason in reasons), list(dict.fromkeys(reasons))


def _stability_quality(metrics: dict[str, Any]) -> dict[str, Any]:
    quality = dict(metrics.get("quality") or {})
    quality.setdefault("definitionsVersion", STABILITY_DEFINITIONS_VERSION)
    return quality


def _stability_metrics_from_aggregate(
    row: dict[str, Any], attempts: dict[str, Any] | None, *, period: dict[str, str], as_of: str
) -> dict[str, Any]:
    attempts = attempts or {}
    total = int(row.get("request_count") or 0)
    explicit_count = int(row.get("explicit_count") or 0)
    known_count = int(row.get("failure_known_count") or 0)
    failure_count = int(row.get("explicit_failure_count") or 0) if explicit_count else int(row.get("failure_count") or 0)
    failure_samples = explicit_count or known_count
    retry_count = int(attempts.get("retry_count") or row.get("retry_count") or 0)
    retry_recovered = int(attempts.get("retry_recovered_count") or row.get("retry_recovered_count") or 0)
    attempt_count = int(attempts.get("attempt_count") or 0)
    attempt_status_count = int(attempts.get("attempt_status_count") or 0)
    failed_attempt_count = int(attempts.get("failed_attempt_count") or 0)
    fallback_count = int(attempts.get("fallback_count") or 0)
    fallback_recovered = int(attempts.get("fallback_recovered_count") or 0)
    ttft_count = int(row.get("ttft_sample_count") or 0)
    ttft_coverage = ttft_count / total if total else None
    failure_rate = failure_count / failure_samples if failure_samples else None
    result = {
        "requestCount": total,
        "userVisibleFailureCount": failure_count if failure_samples else None,
        "userVisibleFailureRate": failure_rate,
        "finalRequestFailureCount": failure_count if failure_samples else None,
        "finalRequestFailureRate": failure_rate,
        "finalRequestFailureExplicitCoverageRate": explicit_count / total if total else None,
        "finalRequestFailureSource": "explicit" if explicit_count else ("derived" if known_count else None),
        "upstreamExceptionCount": failed_attempt_count if attempt_count else None,
        "upstreamExceptionRate": failed_attempt_count / attempt_status_count if attempt_status_count else None,
        "upstreamAttemptCount": attempt_count or None,
        "retryCount": int(row.get("retry_count") or 0) if row.get("retry_known_count") else None,
        "retryAttemptCount": retry_count or None,
        "retryRecoveryCount": retry_recovered if retry_count else None,
        "retryRecoveryRate": retry_recovered / retry_count if retry_count else None,
        "fallbackAttemptCount": fallback_count or None,
        "fallbackRecoveryCount": fallback_recovered if fallback_count else None,
        "fallbackRecoveryRate": fallback_recovered / fallback_count if fallback_count else None,
        "ttftP95Ms": float(row["ttft_p95_ms"]) if row.get("ttft_p95_ms") is not None else None,
        "ttftSampleCount": ttft_count,
        "ttftCoverageRate": ttft_coverage,
        "statusComplete": int(row.get("status_count") or 0) == total and total > 0,
        "retryComplete": int(row.get("retry_known_count") or 0) == total and total > 0,
        "failureComplete": known_count == total and total > 0,
        "definitionsVersion": STABILITY_DEFINITIONS_VERSION,
    }
    result["metricEnvelopes"] = {
        "finalRequestFailureRate": metric_envelope(failure_rate, "ratio", period=period, as_of=as_of, status="observed" if explicit_count else ("derived" if known_count else "unavailable"), source="final request events", coverage_rate=explicit_count / total if total else None, sample_count=failure_samples, missing_reasons=[] if failure_samples else ["final_request_status_missing"]),
        "upstreamExceptionRate": metric_envelope(result["upstreamExceptionRate"], "ratio", period=period, as_of=as_of, status="observed" if attempt_count else "unavailable", source="upstream attempt events", coverage_rate=attempt_status_count / attempt_count if attempt_count else 0.0, sample_count=attempt_status_count, missing_reasons=[] if attempt_count else ["upstream_attempt_logs_unavailable"]),
        "fallbackRecoveryRate": metric_envelope(result["fallbackRecoveryRate"], "ratio", period=period, as_of=as_of, status="observed" if fallback_count else "unavailable", source="upstream attempt events", coverage_rate=1.0 if fallback_count else 0.0, sample_count=fallback_count, missing_reasons=[] if fallback_count else ["fallback_attempt_logs_unavailable"]),
        "retryRecoveryRate": metric_envelope(result["retryRecoveryRate"], "ratio", period=period, as_of=as_of, status="observed" if retry_count else "unavailable", source="upstream attempt events", coverage_rate=1.0 if retry_count else 0.0, sample_count=retry_count),
        "ttftP95Ms": metric_envelope(result["ttftP95Ms"], "ms", period=period, as_of=as_of, status="observed" if ttft_count else "unavailable", source="final request events", coverage_rate=ttft_coverage, sample_count=ttft_count, missing_reasons=[] if ttft_count else ["ttft_samples_missing"]),
    }
    result["quality"] = {
        "definitionsVersion": STABILITY_DEFINITIONS_VERSION,
        "finalRequestFailure": {"status": result["metricEnvelopes"]["finalRequestFailureRate"]["status"], "completeness": result["finalRequestFailureExplicitCoverageRate"] or 0.0, "sampleCount": failure_samples, "explicitSampleCount": explicit_count},
        "upstreamException": {"status": result["metricEnvelopes"]["upstreamExceptionRate"]["status"], "completeness": attempt_status_count / attempt_count if attempt_count else 0.0, "sampleCount": attempt_status_count},
        "retryRecovery": {"status": result["metricEnvelopes"]["retryRecoveryRate"]["status"], "completeness": 1.0 if retry_count else 0.0},
        "fallbackRecovery": {"status": result["metricEnvelopes"]["fallbackRecoveryRate"]["status"], "completeness": 1.0 if fallback_count else 0.0, "sampleCount": fallback_count},
        "ttft": {"status": result["metricEnvelopes"]["ttftP95Ms"]["status"], "completeness": ttft_coverage or 0.0, "sampleCount": ttft_count},
    }
    result["missingReasons"] = [key for key, value in (("final_request_status_missing", not failure_samples), ("ttft_samples_missing", not ttft_count), ("upstream_attempt_logs_unavailable", not attempt_count), ("fallback_attempt_logs_unavailable", not fallback_count)) if value]
    return result


async def _call_store_optional(store: Any, method_names: tuple[str, ...], *args: Any, default: Any = None, **kwargs: Any) -> Any:
    """Keep v2 routes compatible while UsageStore migrations roll out."""

    for name in method_names:
        method = getattr(store, name, None)
        if not callable(method):
            continue
        try:
            return await method(*args, **kwargs)
        except TypeError:
            if kwargs:
                try:
                    return await method(*args)
                except TypeError:
                    continue
            continue
    return default


async def _stability_attempt_events(
    store: Any,
    start_date: str,
    end_date: str,
    *,
    model: str = "",
    trace_id: str = "",
    request_id: str = "",
) -> list[dict[str, Any]]:
    rows = await _call_store_optional(
        store,
        ("stability_attempt_events", "list_stability_attempt_events"),
        start_date,
        end_date,
        model=model,
        trace_id=trace_id,
        request_id=request_id,
        default=[],
    )
    return [dict(row) for row in (rows or [])]


def _metric_period(start_date: str, end_date: str) -> dict[str, str]:
    return {"startDate": start_date, "endDate": end_date}


_OBSERVABILITY_EVENT_FIELDS = {
    "eventId", "event_id", "backendId", "backend_id", "traceId", "trace_id",
    "requestId", "request_id", "attemptId", "attempt_id", "attemptIndex", "attempt_index",
    "requestedModelGroup", "requested_model_group", "actualModel", "actual_model", "route",
    "provider", "eventType", "event_type", "status", "errorCode", "error_code",
    "errorClass", "error_class", "errorCategory", "error_category", "scenario", "scenarioVersion", "scenario_version",
    "startedAt", "started_at", "endedAt", "ended_at", "eventTime", "event_time", "collectedAt", "collected_at",
    "ttftMs", "ttft_ms", "durationMs", "duration_ms", "retryIndex", "retry_index", "isRetry", "is_retry", "isFallback", "is_fallback",
    "routeName", "route_name",
    "fallbackFrom", "fallback_from", "fallbackTo", "fallback_to",
}
_OBSERVABILITY_FORBIDDEN_EVENT_FIELDS = {
    "prompt", "messages", "response", "completion", "content", "body", "choices",
    "api_key", "apiKey", "authorization", "token", "secret", "traceback", "exception",
}
_OBSERVABILITY_CAMEL_TO_SNAKE = {
    "eventId": "event_id", "backendId": "backend_id", "traceId": "trace_id",
    "requestId": "request_id", "attemptId": "attempt_id", "attemptIndex": "attempt_index",
    "requestedModelGroup": "requested_model_group", "actualModel": "actual_model",
    "eventType": "event_type", "errorCode": "error_code", "errorClass": "error_class",
    "errorCategory": "error_category", "eventTime": "event_time", "collectedAt": "collected_at",
    "scenarioVersion": "scenario_version", "startedAt": "started_at", "endedAt": "ended_at",
    "ttftMs": "ttft_ms", "durationMs": "duration_ms", "retryIndex": "retry_index", "isRetry": "is_retry", "isFallback": "is_fallback",
    "routeName": "route_name", "fallbackFrom": "fallback_from", "fallbackTo": "fallback_to",
}


@app.post("/api/internal/observability/events")
async def internal_observability_events(request: Request) -> dict[str, Any]:
    secret = os.getenv("OBSERVABILITY_INGEST_HMAC_SECRET", "").strip()
    if not secret:
        raise HTTPException(status_code=404, detail="采集接口尚未启用")
    max_body = max(1024, int(os.getenv("OBSERVABILITY_INGEST_MAX_BODY_BYTES", "262144")))
    content_length = int(request.headers.get("content-length") or 0)
    if content_length > max_body:
        raise HTTPException(status_code=413, detail="事件批次过大")
    body = await request.body()
    if len(body) > max_body:
        raise HTTPException(status_code=413, detail="事件批次过大")
    timestamp = request.headers.get("x-observability-timestamp", "").strip()
    signature = request.headers.get("x-observability-signature", "").strip()
    try:
        observed_at = int(timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="采集签名时间戳无效") from exc
    if abs(int(datetime.now(timezone.utc).timestamp()) - observed_at) > int(os.getenv("OBSERVABILITY_INGEST_MAX_SKEW_SECONDS", "300")):
        raise HTTPException(status_code=401, detail="采集签名已过期")
    digest = hashlib.sha256(body).hexdigest()
    expected = hmac.new(secret.encode("utf-8"), f"{timestamp}.{digest}".encode("ascii"), hashlib.sha256).hexdigest()
    supplied = signature.removeprefix("sha256=").lower()
    if not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="采集签名无效")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="事件批次必须是 JSON") from exc
    raw_events = payload.get("events") if isinstance(payload, dict) else payload
    if not isinstance(raw_events, list) or not raw_events or len(raw_events) > 500:
        raise HTTPException(status_code=400, detail="events 必须是 1-500 条事件的数组")
    normalized_events: list[dict[str, Any]] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="每条事件必须是对象")
        forbidden = _OBSERVABILITY_FORBIDDEN_EVENT_FIELDS.intersection(raw)
        unknown = set(raw).difference(_OBSERVABILITY_EVENT_FIELDS)
        if forbidden:
            raise HTTPException(status_code=400, detail=f"事件包含禁止字段: {sorted(forbidden)[0]}")
        if unknown:
            raise HTTPException(status_code=400, detail=f"事件包含未知字段: {sorted(unknown)[0]}")
        event = {_OBSERVABILITY_CAMEL_TO_SNAKE.get(key, key): value for key, value in raw.items()}
        if not str(event.get("event_id") or "").strip() or not str(event.get("backend_id") or "").strip():
            raise HTTPException(status_code=400, detail="eventId 和 backendId 必填")
        event["scenario"] = scenario_details(event)["scenario"]
        event["scenario_version"] = scenario_details(event)["version"]
        event["collected_at"] = datetime.now(timezone.utc).isoformat()
        normalized_events.append(event)
    store = usage_store()
    if store is None:
        raise HTTPException(status_code=503, detail="尝试事件存储暂不可用")
    result = await _call_store_optional(store, ("insert_stability_attempt_events",), normalized_events, default=None)
    if result is None:
        raise HTTPException(status_code=503, detail="尝试事件存储尚未就绪")
    if isinstance(result, int):
        result = {"inserted": result, "received": len(normalized_events), "duplicates": len(normalized_events) - result}
    return _observability_envelope(result, coverage={"partial": False, "incomplete": False}, source="upstream attempt events")


async def _admin_stability_events(start_date: str, end_date: str, model: str = "") -> list[dict[str, Any]]:
    rows = await _admin_observability_store().stability_events(start_date, end_date, model)
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        status = item.get("status")
        explicit_final_failure = item.get("final_request_failure")
        if explicit_final_failure is None:
            explicit_final_failure = item.get("finalRequestFailure")
        stored_failure = item.get("user_visible_failure")
        final_failure = explicit_final_failure if explicit_final_failure is not None else stored_failure
        if final_failure is None and status in {"success", "failure"}:
            final_failure = status == "failure"
        final_failure_source = str(item.get("final_failure_source") or item.get("finalRequestFailureSource") or "").lower() or None
        if final_failure_source not in {"explicit", "derived"}:
            final_failure_source = "explicit" if explicit_final_failure is not None else ("derived" if final_failure is not None else None)
        classified = scenario_details(item)
        item.update({
            # Do not default missing state to successful: older logs may have
            # no status and must remain excluded from final-failure metrics.
            "status": status or "unknown",
            "scenario": classified["scenario"],
            "scenarioSource": classified["source"],
            "scenarioVersion": classified["version"],
            "finalRequestFailure": final_failure,
            "finalRequestFailureSource": final_failure_source,
            "userVisibleFailure": final_failure,
            "attemptedRetries": item.get("attempted_retries"),
            "ttftMs": item.get("ttft_ms"),
        })
        output.append(item)
    return output


def _stability_model_ranking_key(item: dict[str, Any]) -> tuple[int, int, float, int, float, int, float, str]:
    """Sort comparable models by status, then by the metrics shown in the ranking."""

    state_rank = {"\u7a33\u5b9a": 0, "\u89c2\u5bdf": 1, "\u9700\u6cbb\u7406": 2, "\u6682\u65e0\u6570\u636e": 3}

    def metric(name: str) -> tuple[int, float]:
        value = item.get(name)
        if value is None:
            return (1, 0.0)
        try:
            return (0, float(value))
        except (TypeError, ValueError):
            return (1, 0.0)

    failure_missing, failure_rate = metric("finalRequestFailureRate")
    fallback_missing, fallback_rate = metric("fallbackRecoveryRate")
    ttft_missing, ttft_p95 = metric("ttftP95Ms")
    return (
        state_rank.get(str(item.get("state") or "\u6682\u65e0\u6570\u636e"), 3),
        failure_missing,
        failure_rate,
        fallback_missing,
        -fallback_rate,
        ttft_missing,
        ttft_p95,
        str(item.get("requestedModelGroup") or item.get("model") or "").casefold(),
    )


async def _build_stability_overview(start_date: str, end_date: str, model: str) -> dict[str, Any]:
    store = _admin_observability_store()
    aggregate_query = getattr(store, "stability_overview_aggregates", None)
    if callable(aggregate_query):
        metric_period = _metric_period(start_date, end_date)
        aggregates = await aggregate_query(start_date, end_date, model)
        overall_row = aggregates.get("overall") or {}
        attempts = aggregates.get("attempts") or {}
        overview = _stability_metrics_from_aggregate(
            overall_row, attempts, period=metric_period, as_of=end_date
        )
        daily = [
            {
                "date": str(row.get("dimension")),
                **_stability_metrics_from_aggregate(row, None, period=metric_period, as_of=end_date),
            }
            for row in aggregates.get("daily") or []
        ]
        rankings = []
        for row in aggregates.get("models") or []:
            metrics = _stability_metrics_from_aggregate(
                row, None, period=metric_period, as_of=end_date
            )
            rankings.append({
                "model": str(row.get("dimension") or "unknown"),
                **metrics,
                "state": model_state(
                    metrics["userVisibleFailureRate"], metrics["ttftP95Ms"],
                    env_float("STABILITY_FAILURE_STABLE_THRESHOLD", 0.01),
                    env_float("STABILITY_FAILURE_OBSERVE_THRESHOLD", 0.03),
                    env_float("STABILITY_TTFT_STABLE_MS", 2000),
                    env_float("STABILITY_TTFT_OBSERVE_MS", 4000),
                    ttft_coverage_rate=metrics.get("ttftCoverageRate"),
                    ttft_sample_count=metrics.get("ttftSampleCount"),
                    minimum_ttft_coverage=env_float("STABILITY_TTFT_MIN_COVERAGE", 0.8),
                    minimum_ttft_samples=int(env_float("STABILITY_TTFT_MIN_SAMPLES", 30)),
                ),
            })
        scenarios = []
        for row in aggregates.get("scenarios") or []:
            sample_ids = list(row.get("sample_request_ids") or [])
            scenarios.append({
                "scenario": str(row.get("scenario") or "unknown"),
                "count": int(row.get("count") or 0),
                "model": str(row.get("requested_model_group") or "unknown"),
                "requestedModelGroup": str(row.get("requested_model_group") or "unknown"),
                "errorCode": str(row.get("error_code") or "") or None,
                "sampleRequestIds": sample_ids,
                "sampleRequests": [{"requestId": item} for item in sample_ids],
                **_stability_metrics_from_aggregate(row, None, period=metric_period, as_of=end_date),
            })
        sync_states = await store.stability_sync_states()
        event_count = int(overall_row.get("request_count") or 0)
        window_covered, missing_reasons = _stability_missing_reasons(
            sync_states, start_date=start_date, end_date=end_date,
            configured_backends=set(usage_backend_ids()), event_count=event_count,
        )
        actions, regressions = await asyncio.gather(
            _call_store_optional(store, ("list_stability_actions",), model=model, default=[]),
            _call_store_optional(store, ("list_stability_regressions",), default=[]),
        )
        return _observability_envelope(
            {
                "overview": overview,
                "daily": daily,
                "modelRankings": sorted(rankings, key=_stability_model_ranking_key),
                "topScenarios": scenarios,
                "actions": [dict(item) for item in (actions or [])[:5]],
                "regressions": [dict(item) for item in (regressions or [])[:5]],
                "attemptEventsAvailableFrom": attempts.get("available_from"),
                "quality": _stability_quality(overview),
                "definitionsVersion": STABILITY_DEFINITIONS_VERSION,
            },
            freshness={"status": "available" if event_count else "empty", "latestCollectedAt": overall_row.get("latest_collected_at")},
            coverage={"partial": not window_covered, "incomplete": not window_covered, "eventCount": event_count, "window": {"startDate": start_date, "endDate": end_date}, "syncStates": sync_states, "missingReasons": missing_reasons, "definitionsVersion": STABILITY_DEFINITIONS_VERSION},
            source="稳定性事件快照",
        ) | {"startDate": start_date, "endDate": end_date, "model": model}
    events = await _admin_stability_events(start_date, end_date, model)
    store = _admin_observability_store()
    sync_states = await store.stability_sync_states()
    attempt_events = await _stability_attempt_events(store, start_date, end_date, model=model)
    metric_period = _metric_period(start_date, end_date)
    overview = stability_metrics(events, attempt_events, period=metric_period, as_of=end_date)
    by_day: dict[str, list[dict[str, Any]]] = {}
    by_model: dict[str, list[dict[str, Any]]] = {}
    by_scenario: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for event in events:
        day = str(event.get("usage_date") or event.get("event_time") or "")[:10]
        by_day.setdefault(day, []).append(event)
        requested_model = str(event.get("model_group") or event.get("model") or "unknown")
        by_model.setdefault(requested_model, []).append(event)
        attempted_retries = event.get("attemptedRetries")
        is_exception_sample = bool(
            event.get("userVisibleFailure")
            or (attempted_retries is not None and int(attempted_retries) > 0)
            or event.get("error_code")
            or event.get("error_class")
        )
        scenario_name = str(event.get("scenario") or "unknown")
        if is_exception_sample or scenario_name != "unknown":
            by_scenario.setdefault((requested_model, scenario_name, str(event.get("error_code") or "")), []).append(event)
    daily = [{"date": day, **stability_metrics(items, [item for item in attempt_events if str(item.get("started_at") or item.get("event_time") or "")[:10] == day], period=metric_period, as_of=end_date)} for day, items in sorted(by_day.items())]
    rankings = []
    for name, items in by_model.items():
        model_attempts = [item for item in attempt_events if str(item.get("requested_model_group") or item.get("requestedModelGroup") or item.get("actual_model") or item.get("actualModel") or "unknown") == name]
        metrics = stability_metrics(items, model_attempts, period=metric_period, as_of=end_date)
        rankings.append({
            "model": name,
            **metrics,
            "state": model_state(
                metrics["userVisibleFailureRate"],
                metrics["ttftP95Ms"],
                env_float("STABILITY_FAILURE_STABLE_THRESHOLD", 0.01),
                env_float("STABILITY_FAILURE_OBSERVE_THRESHOLD", 0.03),
                env_float("STABILITY_TTFT_STABLE_MS", 2000),
                env_float("STABILITY_TTFT_OBSERVE_MS", 4000),
                ttft_coverage_rate=metrics.get("ttftCoverageRate"),
                ttft_sample_count=metrics.get("ttftSampleCount"),
                minimum_ttft_coverage=env_float("STABILITY_TTFT_MIN_COVERAGE", 0.8),
                minimum_ttft_samples=int(env_float("STABILITY_TTFT_MIN_SAMPLES", 30)),
            ),
        })
    scenarios = []
    for (requested_model, name, error_code), items in sorted(by_scenario.items(), key=lambda pair: len(pair[1]), reverse=True)[:10]:
        scenario_attempts = [
            item for item in attempt_events
            if str(item.get("requested_model_group") or item.get("requestedModelGroup") or "unknown") == requested_model
            and str(item.get("scenario") or "unknown") == name
            and str(item.get("error_code") or item.get("errorCode") or "") == error_code
        ]
        scenarios.append({
            "scenario": name,
            "count": len(items),
            "model": requested_model,
            "requestedModelGroup": requested_model,
            "errorCode": error_code or None,
            "sampleRequestIds": [item.get("request_id") for item in items[:5]],
            "sampleRequests": [
                {"requestId": item.get("request_id"), "backendId": item.get("backend_id")}
                for item in items[:5]
            ],
            **stability_metrics(items, scenario_attempts, period=metric_period, as_of=end_date),
        })
    actions = await _call_store_optional(store, ("list_stability_actions",), model=model, default=[])
    regressions = await _call_store_optional(store, ("list_stability_regressions",), default=[])
    window_covered, missing_reasons = _stability_missing_reasons(
        sync_states,
        start_date=start_date,
        end_date=end_date,
        configured_backends=set(usage_backend_ids()),
        event_count=len(events),
    )
    covered = {
        "partial": not window_covered,
        "incomplete": not window_covered,
        "eventCount": len(events),
        "window": {"startDate": start_date, "endDate": end_date},
        "syncStates": sync_states,
        "missingReasons": missing_reasons,
        "definitionsVersion": STABILITY_DEFINITIONS_VERSION,
    }
    freshness = {"status": "available" if events else "empty", "latestCollectedAt": max((str(item.get("collected_at") or "") for item in events), default=None)}
    return _observability_envelope(
        {
            "overview": overview,
            "daily": daily,
            "modelRankings": sorted(rankings, key=_stability_model_ranking_key),
            "topScenarios": scenarios,
            "actions": actions or [],
            "regressions": regressions or [],
            "attemptEventsAvailableFrom": min((str(item.get("started_at") or item.get("event_time") or "") for item in attempt_events), default=None),
            "quality": _stability_quality(overview),
            "definitionsVersion": STABILITY_DEFINITIONS_VERSION,
        },
        freshness=freshness,
        coverage=covered,
        source="稳定性事件快照",
    ) | {"startDate": start_date, "endDate": end_date, "model": model}


@app.get("/api/admin/stability/overview")
async def admin_stability_overview(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    model: str = "",
    refresh: int = 0,
) -> JSONResponse:
    require_platform_admin(request)
    if not env_bool("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", False):
        raise HTTPException(status_code=404, detail="看板尚未开放")
    start_date, end_date = resolve_usage_range(start_date, end_date)
    payload = await _cached_observability_dashboard(
        "stability",
        {"startDate": start_date, "endDate": end_date, "model": model, "definition": STABILITY_DEFINITIONS_VERSION},
        lambda: _build_stability_overview(start_date, end_date, model),
        refresh=bool(refresh),
    )
    return JSONResponse(payload, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@app.get("/api/admin/stability/scenarios")
async def admin_stability_scenarios(request: Request, start_date: str | None = None, end_date: str | None = None, model: str = "", scenario: str = "", error_code: str = "", page: int = 1, page_size: int = 20) -> dict[str, Any]:
    require_platform_admin(request)
    if not env_bool("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", False):
        raise HTTPException(status_code=404, detail="看板尚未开放")
    start_date, end_date = resolve_usage_range(start_date, end_date)
    page = max(1, page)
    page_size = max(1, min(100, page_size))
    store = _admin_observability_store()
    query = getattr(store, "stability_scenario_samples", None)
    if callable(query):
        result = await query(start_date, end_date, model=model, scenario=scenario, error_code=error_code, page=page, page_size=page_size)
        raw_items, total = result.get("items", []), int(result.get("total") or 0)
    else:
        events = await _admin_stability_events(start_date, end_date, model)
        filtered = [item for item in events if (not scenario or item.get("scenario") == scenario) and (not error_code or item.get("error_code") == error_code)]
        start = (page - 1) * page_size
        raw_items, total = filtered[start:start + page_size], len(filtered)
    samples = []
    for item in raw_items:
        normalized = normalize_event(dict(item))
        final_failure = normalized.get("finalRequestFailure")
        samples.append({
            "requestId": item.get("request_id"),
            "backendId": item.get("backend_id"),
            "eventTime": item.get("event_time"),
            "model": item.get("model"),
            "requestedModelGroup": item.get("model_group") or item.get("model") or "unknown",
            "scenario": scenario_details(dict(item))["scenario"],
            "scenarioVersion": scenario_details(dict(item))["version"],
            "errorCode": item.get("error_code"),
            "status": item.get("status") or "unknown",
            "finalRequestFailure": final_failure,
            "finalRequestFailureSource": item.get("final_failure_source") or normalized.get("finalRequestFailureSource"),
            "userVisibleFailure": final_failure,
            "attemptedRetries": item.get("attempted_retries"),
            "maxRetries": item.get("max_retries"),
            "ttftMs": item.get("ttft_ms"),
            "actions": await _call_store_optional(store, ("list_stability_actions",), model=str(item.get("model_group") or item.get("model") or ""), scenario=str(item.get("scenario") or ""), default=[]),
        })
    sync_states = await store.stability_sync_states()
    window_covered, missing_reasons = _stability_missing_reasons(
        sync_states,
        start_date=start_date,
        end_date=end_date,
        configured_backends=set(usage_backend_ids()),
        event_count=total,
    )
    return _observability_envelope(
        {"items": samples, "total": total, "page": page, "pageSize": page_size},
        coverage={
            "partial": not window_covered,
            "incomplete": not window_covered,
            "syncStates": sync_states,
            "missingReasons": missing_reasons,
            "definitionsVersion": STABILITY_DEFINITIONS_VERSION,
        },
        source="稳定性事件快照",
    )


@app.get("/api/admin/stability/requests/{request_id}")
async def admin_stability_request(request: Request, request_id: str, backend_id: str = "") -> dict[str, Any]:
    require_platform_admin(request)
    if not env_bool("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", False):
        raise HTTPException(status_code=404, detail="看板尚未开放")
    store = _admin_observability_store()
    try:
        record = await store.stability_request(request_id, backend_id)
    except TypeError:
        record = await store.stability_request(request_id)
    if record is None:
        raise HTTPException(status_code=404, detail="请求样本不存在")
    safe_fields = {key: record.get(key) for key in ("backend_id", "request_id", "event_time", "model", "provider", "model_group", "model_id", "source", "status", "error_code", "error_class", "error_message", "scenario", "request_duration_ms", "ttft_ms", "prompt_tokens", "completion_tokens", "total_tokens", "attempted_retries", "max_retries", "trace_id", "user_visible_failure", "organization_id", "team_id", "principal_id", "collected_at")}
    timeline = await _call_store_optional(store, ("stability_attempt_timeline",), request_id, backend_id, default=None)
    if timeline is None:
        event_day = str(record.get("event_time") or date.today().isoformat())[:10]
        timeline = await _stability_attempt_events(store, event_day, event_day, trace_id=str(record.get("trace_id") or ""), request_id=request_id)
    action_list = await _call_store_optional(store, ("list_stability_actions",), request_id=request_id, scenario=str(record.get("scenario") or ""), default=[])
    action_ids = {str(item.get("id") or "") for item in (action_list or [])}
    regressions = await _call_store_optional(store, ("list_stability_regressions",), default=[])
    normalized = normalize_event(record)
    return _observability_envelope(
        {
            **safe_fields,
            "finalRequestFailure": normalized.get("finalRequestFailure"),
            "finalRequestFailureSource": normalized.get("finalRequestFailureSource"),
            "timeline": timeline or [],
            "actions": action_list or [],
            "regressions": [item for item in (regressions or []) if str(item.get("action_id") or item.get("actionId") or "") in action_ids],
        },
        coverage={"partial": False, "incomplete": not bool(timeline), "missingReasons": [] if timeline else ["upstream_attempt_logs_unavailable"]},
        source="stability events and attempt timeline",
    )


def _stability_action_input(data: StabilityActionRequest, action_id: str | None = None) -> dict[str, Any]:
    return {
        "id": action_id or uuid.uuid4().hex,
        "title": data.title.strip(),
        "description": data.notes.strip(),
        "owner": data.owner.strip(),
        "severity": data.severity,
        "status": data.status,
        "targetDate": data.targetDate.isoformat() if data.targetDate else None,
        "fixReference": data.fixReference.strip(),
        "requestedModelGroup": data.requestedModelGroup.strip(),
        "scenario": data.scenario.strip(),
        "errorCode": data.errorCode.strip(),
        "notes": data.notes.strip(),
    }


def _stability_regression_input(data: StabilityRegressionRequest, regression_id: str | None = None) -> dict[str, Any]:
    return {
        "id": regression_id or uuid.uuid4().hex,
        "actionId": data.actionId.strip(),
        "baselineStartDate": data.baselineStart.isoformat(),
        "baselineEndDate": data.baselineEnd.isoformat(),
        "regressionStartDate": data.regressionStart.isoformat(),
        "regressionEndDate": data.regressionEnd.isoformat(),
        "metricName": data.metric.strip(),
        "baselineValue": data.baselineValue,
        "regressionValue": data.regressionValue,
        "status": "verified",
        "conclusion": data.conclusion,
        "notes": data.notes.strip(),
        "notes": data.notes.strip(),
    }


@app.get("/api/admin/stability/actions")
async def admin_stability_actions(request: Request, status: str = "", owner: str = "", scenario: str = "") -> dict[str, Any]:
    require_platform_admin(request)
    store = _admin_observability_store()
    items = await _call_store_optional(store, ("list_stability_actions",), status=status, owner=owner, scenario=scenario, default=[])
    return _observability_envelope(items or [], source="stability governance")


@app.post("/api/admin/stability/actions")
async def admin_create_stability_action(data: StabilityActionRequest, request: Request) -> dict[str, Any]:
    require_platform_admin(request)
    await enforce_csrf(request)
    store = _admin_observability_store()
    item = await _call_store_optional(store, ("create_stability_action",), _stability_action_input(data), default=None)
    if item is None:
        raise HTTPException(status_code=503, detail="稳定性治理存储尚未就绪")
    await _invalidate_observability_dashboard("stability")
    return _observability_envelope(item, source="stability governance")


@app.patch("/api/admin/stability/actions/{action_id}")
async def admin_update_stability_action(action_id: str, data: StabilityActionRequest, request: Request) -> dict[str, Any]:
    require_platform_admin(request)
    await enforce_csrf(request)
    store = _admin_observability_store()
    item = await _call_store_optional(store, ("update_stability_action",), action_id, _stability_action_input(data, action_id), default=None)
    if item is None:
        raise HTTPException(status_code=404, detail="稳定性治理动作不存在")
    await _invalidate_observability_dashboard("stability")
    return _observability_envelope(item, source="stability governance")


@app.delete("/api/admin/stability/actions/{action_id}")
async def admin_delete_stability_action(action_id: str, request: Request) -> dict[str, Any]:
    require_platform_admin(request)
    await enforce_csrf(request)
    deleted = await _call_store_optional(_admin_observability_store(), ("delete_stability_action",), action_id, default=False)
    if not deleted:
        raise HTTPException(status_code=404, detail="稳定性治理动作不存在")
    await _invalidate_observability_dashboard("stability")
    return _observability_envelope({"deleted": True}, source="stability governance")


@app.get("/api/admin/stability/regressions")
async def admin_stability_regressions(request: Request, action_id: str = "", scenario: str = "") -> dict[str, Any]:
    require_platform_admin(request)
    items = await _call_store_optional(_admin_observability_store(), ("list_stability_regressions",), action_id=action_id, scenario=scenario, default=[])
    return _observability_envelope(items or [], source="stability governance")


@app.post("/api/admin/stability/regressions")
async def admin_create_stability_regression(data: StabilityRegressionRequest, request: Request) -> dict[str, Any]:
    require_platform_admin(request)
    await enforce_csrf(request)
    item = await _call_store_optional(_admin_observability_store(), ("create_stability_regression",), _stability_regression_input(data), default=None)
    if item is None:
        raise HTTPException(status_code=503, detail="回归验证存储尚未就绪")
    await _invalidate_observability_dashboard("stability")
    return _observability_envelope(item, source="stability governance")


@app.patch("/api/admin/stability/regressions/{regression_id}")
async def admin_update_stability_regression(regression_id: str, data: StabilityRegressionRequest, request: Request) -> dict[str, Any]:
    require_platform_admin(request)
    await enforce_csrf(request)
    item = await _call_store_optional(_admin_observability_store(), ("update_stability_regression",), regression_id, _stability_regression_input(data, regression_id), default=None)
    if item is None:
        raise HTTPException(status_code=404, detail="回归验证记录不存在")
    await _invalidate_observability_dashboard("stability")
    return _observability_envelope(item, source="stability governance")


@app.delete("/api/admin/stability/regressions/{regression_id}")
async def admin_delete_stability_regression(regression_id: str, request: Request) -> dict[str, Any]:
    require_platform_admin(request)
    await enforce_csrf(request)
    deleted = await _call_store_optional(_admin_observability_store(), ("delete_stability_regression",), regression_id, default=False)
    if not deleted:
        raise HTTPException(status_code=404, detail="回归验证记录不存在")
    await _invalidate_observability_dashboard("stability")
    return _observability_envelope({"deleted": True}, source="stability governance")


def _cost_item_payload(item: dict[str, Any]) -> dict[str, Any]:
    category = str(item.get("category") or "")
    vendor = str(item.get("vendor") or "")
    raw_bucket = item.get("cost_bucket") or item.get("costBucket") or category or "other"
    return {
        "id": str(item.get("id")),
        "category": category,
        "name": item.get("name"),
        "vendor": vendor,
        "model": item.get("model"),
        "businessScope": item.get("business_scope"),
        "amount": float(item.get("amount") or 0),
        "currency": item.get("currency"),
        "exchangeRate": float(item.get("exchange_rate") or 1),
        "amountUsd": float(item.get("amount_usd") or 0),
        "serviceStartDate": str(item.get("service_start_date")),
        "serviceEndDate": str(item.get("service_end_date")),
        "financeBucket": item.get("finance_bucket"),
        "costBucket": _canonical_cost_bucket(raw_bucket),
        "sourceType": item.get("source_type") or "manual",
        "provider": item.get("provider") or vendor or None,
        "accountId": item.get("account_id") or None,
        "accountName": item.get("account_name") or None,
        "voucherId": item.get("voucher_id") or None,
        "voucherNo": item.get("voucher_no") or None,
        "invoiceNo": item.get("invoice_no") or None,
        "recognitionStatus": item.get("recognition_status") or "actual",
        "reconciliationStatus": item.get("reconciliation_status") or "unreconciled",
        "planVersionId": item.get("plan_version_id") or item.get("planVersionId") or None,
        "scenario": item.get("scenario") or None,
        "sourceEvidence": item.get("source_evidence") or item.get("sourceEvidence") or None,
        "notes": item.get("notes"),
        "enabled": bool(item.get("enabled")),
    }


def _cost_item_overlap_usd(item: dict[str, Any], start: date, end: date) -> float:
    if not bool(item.get("enabled")):
        return 0.0
    service_start = item["service_start_date"] if isinstance(item["service_start_date"], date) else date.fromisoformat(str(item["service_start_date"]))
    service_end = item["service_end_date"] if isinstance(item["service_end_date"], date) else date.fromisoformat(str(item["service_end_date"]))
    overlap_start = max(start, service_start)
    overlap_end = min(end, service_end)
    if overlap_end < overlap_start:
        return 0.0
    service_days = max(1, (service_end - service_start).days + 1)
    overlap_days = (overlap_end - overlap_start).days + 1
    return float(item.get("amount_usd") or 0) / service_days * overlap_days


def _recognition_status(item: dict[str, Any]) -> str:
    return str(item.get("recognition_status") or item.get("recognitionStatus") or "actual").strip().lower()


def _actual_cost_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if _recognition_status(item) == "actual"]


def _plan_version_payload(item: dict[str, Any]) -> dict[str, Any]:
    coverage_status = str(item.get("coverage_status") or item.get("coverageStatus") or "")
    return {
        "id": str(item.get("id") or ""),
        "year": int(item.get("year") or item.get("plan_year") or item.get("planYear") or 0),
        "version": item.get("version"),
        "scenario": item.get("scenario") or "baseline",
        "asOf": str(item.get("as_of") or item.get("as_of_date") or item.get("asOfDate") or item.get("asOf") or "")[:10] or None,
        "status": item.get("status") or "draft",
        "coverageComplete": coverage_status == "complete" or bool(item.get("coverage_complete") if item.get("coverage_complete") is not None else item.get("coverageComplete")),
        "approvedBy": item.get("approved_by") or item.get("approvedBy") or None,
        "approvedAt": str(item.get("approved_at") or item.get("approvedAt") or "") or None,
        "activatedAt": str(item.get("activated_at") or item.get("activatedAt") or "") or None,
        "notes": item.get("notes") or "",
    }


def _active_baseline_plan(plans: list[dict[str, Any]], year: int) -> dict[str, Any] | None:
    candidates = [
        item for item in plans
        if int(item.get("year") or item.get("plan_year") or item.get("planYear") or 0) == year
        and str(item.get("scenario") or "baseline") == "baseline"
        and str(item.get("status") or "").lower() == "approved"
        and bool(item.get("active") if item.get("active") is not None else item.get("is_active") if item.get("is_active") is not None else item.get("activated_at") or item.get("activatedAt"))
    ]
    return max(candidates, key=lambda item: str(item.get("activated_at") or item.get("activatedAt") or item.get("approved_at") or item.get("approvedAt") or ""), default=None)


def _official_plan_rows(items: list[dict[str, Any]], plan: dict[str, Any] | None, start: date, end: date) -> list[dict[str, Any]]:
    coverage_complete = bool(plan and (str(plan.get("coverage_status") or plan.get("coverageStatus") or "") == "complete" or (plan.get("coverage_complete") if plan.get("coverage_complete") is not None else plan.get("coverageComplete"))))
    if not plan or not coverage_complete:
        return []
    plan_id = str(plan.get("id") or "")
    rows: list[dict[str, Any]] = []
    for item in items:
        if str(item.get("plan_version_id") or item.get("planVersionId") or "") != plan_id:
            continue
        if _recognition_status(item) not in {"committed", "planned"}:
            continue
        rows.extend(_manual_cost_ledger_rows(item, start, end))
    return rows


async def _official_plan_items(store: Any, plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not plan:
        return []
    plan_id = str(plan.get("id") or "")
    rows = await _call_store_optional(
        store,
        ("list_cost_items",),
        plan_version_id=plan_id,
        default=None,
    )
    if rows is None:
        rows = await store.list_cost_items()
    return [dict(item) for item in rows if bool(item.get("enabled")) and str(item.get("plan_version_id") or item.get("planVersionId") or "") == plan_id]


_COST_BUCKET_LABELS = {
    "api_usage": "API Token",
    "account_procurement": "账号采购",
    "fallback_channel": "兜底渠道",
    "feishu_surrounding": "飞书 / 周边",
    "infrastructure": "基础设施",
    "other": "其他成本",
}

# Existing rows used free-form categories and a few early implementation names.
# Keep them queryable while presenting one finance-facing composition taxonomy.
_COST_BUCKET_ALIASES = {
    "subscription": "account_procurement",
    "account_purchase": "account_procurement",
    "fallback": "fallback_channel",
    "backup_api": "fallback_channel",
    "feishu": "feishu_surrounding",
    "surrounding": "feishu_surrounding",
    "infra": "infrastructure",
    "labor": "other",
    "support": "other",
}


def _canonical_cost_bucket(value: Any) -> str:
    bucket = str(value or "other").strip() or "other"
    return _COST_BUCKET_ALIASES.get(bucket, bucket)


def _cost_bucket(item: dict[str, Any], *, api: bool = False) -> str:
    if api:
        return "api_usage"
    raw = str(item.get("cost_bucket") or item.get("costBucket") or item.get("category") or "other").strip() or "other"
    return _canonical_cost_bucket(raw)


def _cost_composition_bucket(item: dict[str, Any]) -> str:
    return _canonical_cost_bucket(item.get("costBucket") or item.get("cost_bucket") or item.get("category"))


def _cost_account_id(row: dict[str, Any]) -> str:
    return str(
        row.get("account_id")
        or row.get("accountId")
        or row.get("user_id")
        or row.get("raw_user_id")
        or row.get("principal_id")
        # A key id is only a last-resort legacy dimension. Prefer the
        # billable account identity so the cost drawer never promotes a
        # credential identifier to the primary account label.
        or row.get("key_id")
        or ""
    ).strip()


def _cost_account_name(row: dict[str, Any]) -> str:
    return str(row.get("account_name") or row.get("accountName") or "").strip()


def _cost_provider(row: dict[str, Any], *, api: bool = False) -> str:
    if api:
        return str(row.get("provider") or "").strip()
    return str(row.get("provider") or row.get("vendor") or "").strip()


def _cost_value_matches(value: Any, expected: str) -> bool:
    return not expected or str(value or "").strip() == expected


def _cost_manual_matches(
    item: dict[str, Any],
    *,
    category: str = "",
    cost_bucket: str = "",
    model: str = "",
    vendor: str = "",
    provider: str = "",
    account_id: str = "",
    reconciliation_status: str = "",
    recognition_status: str = "",
) -> bool:
    if category and str(item.get("category") or "") != category:
        return False
    if cost_bucket and _cost_bucket(item) != cost_bucket:
        return False
    if not _cost_value_matches(item.get("model"), model):
        return False
    item_provider = _cost_provider(item)
    if vendor and item_provider != vendor and str(item.get("vendor") or "") != vendor:
        return False
    if provider and item_provider != provider:
        return False
    if account_id and _cost_account_id(item) != account_id:
        return False
    if not _cost_value_matches(
        item.get("reconciliation_status") or item.get("reconciliationStatus") or "unreconciled",
        reconciliation_status,
    ):
        return False
    return _cost_value_matches(
        item.get("recognition_status") or item.get("recognitionStatus") or "actual",
        recognition_status,
    )


def _filter_cost_sources(
    api_rows: list[dict[str, Any]],
    items: list[dict[str, Any]],
    *,
    category: str = "",
    cost_bucket: str = "",
    model: str = "",
    vendor: str = "",
    provider: str = "",
    account_id: str = "",
    reconciliation_status: str = "",
    recognition_status: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    filtered_api = list(api_rows)
    filtered_items = list(items)
    if category:
        if category == "API Token":
            filtered_items = []
        else:
            filtered_api = []
            filtered_items = [item for item in filtered_items if item.get("category") == category]
    if cost_bucket:
        if cost_bucket == "api_usage":
            filtered_items = []
        else:
            filtered_api = []
            filtered_items = [item for item in filtered_items if _cost_bucket(item) == cost_bucket]
    if model:
        filtered_api = [item for item in filtered_api if item.get("model") == model]
        filtered_items = [item for item in filtered_items if item.get("model") == model]
    if vendor:
        filtered_api = [item for item in filtered_api if item.get("source") == vendor]
        filtered_items = [item for item in filtered_items if item.get("vendor") == vendor]
    if provider:
        filtered_api = [item for item in filtered_api if item.get("provider") == provider]
        filtered_items = [item for item in filtered_items if _cost_provider(item) == provider]
    if account_id:
        filtered_api = [item for item in filtered_api if _cost_account_id(item) == account_id]
        filtered_items = [item for item in filtered_items if _cost_account_id(item) == account_id]
    if reconciliation_status:
        # API rows are operational spend and do not participate in finance
        # reconciliation until a separate voucher is attached.
        filtered_api = []
        filtered_items = [
            item for item in filtered_items
            if _cost_value_matches(
                item.get("reconciliation_status") or item.get("reconciliationStatus") or "unreconciled",
                reconciliation_status,
            )
        ]
    if recognition_status:
        if recognition_status != "actual":
            filtered_api = []
        filtered_items = [
            item for item in filtered_items
            if _cost_value_matches(
                item.get("recognition_status") or item.get("recognitionStatus") or "actual",
                recognition_status,
            )
        ]
    return filtered_api, filtered_items


def _manual_cost_ledger_rows(item: dict[str, Any], start: date, end: date) -> list[dict[str, Any]]:
    if not bool(item.get("enabled")):
        return []
    original_start = item["service_start_date"] if isinstance(item["service_start_date"], date) else date.fromisoformat(str(item["service_start_date"]))
    original_end = item["service_end_date"] if isinstance(item["service_end_date"], date) else date.fromisoformat(str(item["service_end_date"]))
    overlap_start = max(start, original_start)
    overlap_end = min(end, original_end)
    if overlap_end < overlap_start:
        return []
    service_days = max(1, (original_end - original_start).days + 1)
    per_day = float(item.get("amount_usd") or 0) / service_days
    currency = str(item.get("currency") or "USD")
    per_day_original = float(item.get("amount") or 0) / service_days
    cost_bucket = _cost_composition_bucket(item)
    result: list[dict[str, Any]] = []
    current = overlap_start
    while current <= overlap_end:
        result.append(
            {
                "id": f"manual:{item.get('id')}:{current.isoformat()}",
                "date": current.isoformat(),
                "sourceType": item.get("source_type") or "manual",
                "costBucket": cost_bucket,
                "category": item.get("category"),
                "name": item.get("name"),
                "backendId": None,
                "accountId": _cost_account_id(item) or None,
                "accountName": _cost_account_name(item) or None,
                "provider": _cost_provider(item) or None,
                "vendor": item.get("vendor") or None,
                "model": item.get("model") or None,
                "organizationId": None,
                "teamId": None,
                "principalId": None,
                "amountUsd": per_day,
                "currency": currency,
                "amount": per_day_original,
                "financeBucket": item.get("finance_bucket") or None,
                "voucherId": item.get("voucher_id") or None,
                "voucherNo": item.get("voucher_no") or None,
                "invoiceNo": item.get("invoice_no") or None,
                "reconciliationStatus": item.get("reconciliation_status") or "unreconciled",
                "recognitionStatus": item.get("recognition_status") or "actual",
                "sourceItemId": str(item.get("id") or ""),
                "requestId": None,
                "coverage": {"dimensionsComplete": True},
            }
        )
        current += timedelta(days=1)
    return result


def _api_cost_ledger_row(row: dict[str, Any]) -> dict[str, Any]:
    account_id = _cost_account_id(row)
    request_id = row.get("request_id") or row.get("requestId") or None
    return {
        "id": "api:" + ":".join(
            str(value or "-")
            for value in (
                row.get("backend_id"), row.get("usage_date"), account_id,
                row.get("source"), row.get("model"), row.get("organization_id"),
                row.get("team_id"), row.get("key_id"), row.get("request_id") or row.get("id"),
            )
        ),
        "date": str(row.get("usage_date")),
        "sourceType": "api_usage",
        "costBucket": "api_usage",
        "category": "API Token",
        "name": row.get("model") or "API Token",
        "backendId": row.get("backend_id") or None,
        "requestId": request_id,
        "accountId": account_id or None,
        "accountName": _cost_account_name(row) or None,
        "provider": _cost_provider(row, api=True) or None,
        "vendor": None,
        "model": row.get("model") or None,
        "organizationId": row.get("organization_id") or None,
        "teamId": row.get("team_id") or None,
        "principalId": row.get("principal_id") or None,
        "amountUsd": float(row.get("spend") or 0),
        "currency": "USD",
        "amount": float(row.get("spend") or 0),
        "financeBucket": None,
        "voucherId": None,
        "voucherNo": None,
        "invoiceNo": None,
        "reconciliationStatus": None,
        "recognitionStatus": "actual",
        "sourceItemId": None,
        "coverage": {
            "dimensionsComplete": bool(account_id and _cost_provider(row, api=True)),
            "missingDimensions": [
                field
                for field, present in (
                    ("accountId", bool(account_id)),
                    ("provider", bool(_cost_provider(row, api=True))),
                )
                if not present
            ],
        },
    }


def _savings_totals(actions: list[dict[str, Any]], as_of: date, period_end: date) -> dict[str, float]:
    realized = verified_savings(actions, as_of)
    remaining = 0.0
    for action in actions:
        status = str(action.get("status") or "").lower()
        if status not in {"planned", "implemented"}:
            continue
        expected_daily = action.get("expectedDailyCost")
        if expected_daily is None:
            continue
        expected_start_text = action.get("expectedStartDate") or action.get("implementedDate")
        try:
            expected_start = date.fromisoformat(str(expected_start_text)[:10])
        except (TypeError, ValueError):
            continue
        start = max(as_of + timedelta(days=1), expected_start)
        if start > period_end:
            continue
        daily_savings = max(0.0, float(action.get("baselineDailyCost") or 0) - float(expected_daily))
        remaining += daily_savings * ((period_end - start).days + 1)
    return {
        "realizedSavingsToDate": round(realized, 2),
        "forecastSavingsRemaining": round(remaining, 2),
    }


async def _cost_api_rows(
    store: Any,
    start: date,
    end: date,
    *,
    model: str = "",
    provider: str = "",
    account_id: str = "",
) -> tuple[list[dict[str, Any]], bool]:
    query = getattr(store, "api_cost_ledger_rows", None)
    if callable(query):
        rows = await query(
            start.isoformat(),
            end.isoformat(),
            model=model,
            provider=provider,
            account_id=account_id,
        )
        if rows:
            return rows, True
    rows = await store.api_cost_rows(start.isoformat(), end.isoformat())
    return rows, False


async def _cost_actual_items(
    store: Any,
    as_of: date,
    *,
    model: str = "",
    provider: str = "",
    account_id: str = "",
    cost_bucket: str = "",
) -> list[dict[str, Any]]:
    rows = await _call_store_optional(
        store,
        ("list_actual_cost_items",),
        as_of.isoformat(),
        model=model,
        provider=provider,
        account_id=account_id,
        cost_bucket=cost_bucket,
        default=None,
    )
    if rows is None:
        rows = await store.list_cost_items()
    return _actual_cost_items([dict(item) for item in rows if bool(item.get("enabled"))])


def _cost_summary_splits(ledger_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Stable P1 aggregates across the unified finance-facing ledger."""

    accounts: dict[str, float] = defaultdict(float)
    providers: dict[str, float] = defaultdict(float)
    reconciliation: dict[str, float] = defaultdict(float)
    for row in ledger_rows:
        account_id = str(row.get("accountId") or "unknown")
        provider = str(row.get("provider") or "unknown")
        accounts[account_id] += float(row.get("amountUsd") or 0)
        providers[provider] += float(row.get("amountUsd") or 0)
        status = row.get("reconciliationStatus") or "unreconciled"
        reconciliation[str(status)] += float(row.get("amountUsd") or 0)
    return {
        "accountSplit": [
            {"accountId": key, "amountUsd": round(value, 2)}
            for key, value in sorted(accounts.items(), key=lambda pair: pair[1], reverse=True)
        ],
        "providerSplit": [
            {"provider": key, "amountUsd": round(value, 2)}
            for key, value in sorted(providers.items(), key=lambda pair: pair[1], reverse=True)
        ],
        "reconciliationSummary": [
            {"status": key, "amountUsd": round(value, 2), "count": sum(1 for row in ledger_rows if str(row.get("reconciliationStatus") or "unreconciled") == key)}
            for key, value in sorted(reconciliation.items(), key=lambda pair: pair[1], reverse=True)
        ],
    }


@app.get("/api/admin/costs/items")
async def admin_cost_items(request: Request) -> dict[str, Any]:
    require_platform_admin(request)
    return _observability_envelope([_cost_item_payload(item) for item in await _admin_observability_store().list_cost_items()], source="费用控制账本")


def _cost_item_input(data: CostItemRequest, item_id: str | None = None) -> dict[str, Any]:
    if data.serviceEndDate < data.serviceStartDate:
        raise HTTPException(status_code=400, detail="服务结束日期不能早于开始日期")
    rate = data.exchangeRate if data.currency == "CNY" else Decimal("1")
    return {
        "id": item_id or uuid.uuid4().hex,
        "category": data.category.strip(),
        "name": data.name.strip(),
        "vendor": data.vendor.strip(),
        "model": data.model.strip(),
        "businessScope": data.businessScope.strip(),
        "amount": data.amount,
        "currency": data.currency,
        "exchangeRate": rate,
        "amountUsd": data.amount / rate,
        "serviceStartDate": data.serviceStartDate.isoformat(),
        "serviceEndDate": data.serviceEndDate.isoformat(),
        "financeBucket": data.financeBucket.strip(),
        "costBucket": _canonical_cost_bucket(data.costBucket.strip() or data.category.strip()),
        "sourceType": data.sourceType,
        "provider": data.provider.strip() or data.vendor.strip(),
        "accountId": data.accountId.strip(),
        "accountName": data.accountName.strip(),
        "voucherId": data.voucherId.strip(),
        "voucherNo": data.voucherNo.strip(),
        "invoiceNo": data.invoiceNo.strip(),
        "recognitionStatus": data.recognitionStatus,
        "reconciliationStatus": data.reconciliationStatus,
        "planVersionId": data.planVersionId.strip(),
        "scenario": data.scenario.strip(),
        "sourceEvidence": data.sourceEvidence.strip(),
        "notes": data.notes.strip(),
        "enabled": data.enabled,
    }


@app.post("/api/admin/costs/items")
async def admin_create_cost_item(data: CostItemRequest, request: Request) -> dict[str, Any]:
    require_platform_admin(request)
    await enforce_csrf(request)
    item = _cost_item_payload(await _admin_observability_store().create_cost_item(_cost_item_input(data)))
    await _invalidate_observability_dashboard("cost")
    return _observability_envelope(item, source="费用控制账本")


@app.patch("/api/admin/costs/items/{item_id}")
async def admin_update_cost_item(item_id: str, data: CostItemRequest, request: Request) -> dict[str, Any]:
    require_platform_admin(request)
    await enforce_csrf(request)
    record = await _admin_observability_store().update_cost_item(item_id, _cost_item_input(data, item_id))
    if not record:
        raise HTTPException(status_code=404, detail="成本项不存在")
    await _invalidate_observability_dashboard("cost")
    return _observability_envelope(_cost_item_payload(record), source="费用控制账本")


@app.delete("/api/admin/costs/items/{item_id}")
async def admin_delete_cost_item(item_id: str, request: Request) -> dict[str, Any]:
    require_platform_admin(request)
    await enforce_csrf(request)
    if not await _admin_observability_store().delete_cost_item(item_id):
        raise HTTPException(status_code=404, detail="成本项不存在")
    await _invalidate_observability_dashboard("cost")
    return _observability_envelope({"deleted": True}, source="费用控制账本")


@app.get("/api/admin/costs/budgets")
async def admin_cost_budgets(request: Request) -> dict[str, Any]:
    require_platform_admin(request)
    return _observability_envelope([{ "month": str(item.get("month")), "budgetUsd": float(item.get("budget_usd") or 0), "dailyTargetUsd": float(item.get("daily_target_usd") or 0)} for item in await _admin_observability_store().list_cost_budgets()], source="费用控制账本")


@app.put("/api/admin/costs/budgets/{month}")
async def admin_update_cost_budget(month: str, data: CostBudgetRequest, request: Request) -> dict[str, Any]:
    require_platform_admin(request)
    await enforce_csrf(request)
    try:
        date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="月份格式应为 YYYY-MM") from exc
    item = await _admin_observability_store().upsert_cost_budget(month, float(data.budgetUsd), float(data.dailyTargetUsd))
    await _invalidate_observability_dashboard("cost")
    return _observability_envelope({"month": str(item.get("month")), "budgetUsd": float(item.get("budget_usd") or 0), "dailyTargetUsd": float(item.get("daily_target_usd") or 0)}, source="费用控制账本")


def _savings_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("id")),
        "name": item.get("name"),
        "baselineDailyCost": float(item.get("baseline_daily_cost") or 0),
        "implementedDate": str(item.get("implemented_date")),
        "verifiedDate": str(item.get("verified_date")) if item.get("verified_date") else None,
        "verifiedDailyCost": float(item.get("verified_daily_cost")) if item.get("verified_daily_cost") is not None else None,
        "owner": item.get("owner"),
        "status": item.get("status"),
        "expectedDailyCost": float(item.get("expected_daily_cost")) if item.get("expected_daily_cost") is not None else None,
        "expectedStartDate": str(item.get("expected_start_date")) if item.get("expected_start_date") else None,
        "provider": item.get("provider") or None,
        "model": item.get("model") or None,
        "costBucket": item.get("cost_bucket") or None,
        "evidenceUrl": item.get("evidence_url") or None,
        "financeReviewer": item.get("finance_reviewer") or None,
        "notes": item.get("notes"),
    }


def _cost_plan_input(data: CostPlanVersionRequest, plan_id: str | None = None) -> dict[str, Any]:
    return {
        "id": plan_id or uuid.uuid4().hex,
        "year": data.year,
        "version": data.version.strip(),
        "scenario": data.scenario,
        "asOf": data.asOf.isoformat(),
        "status": data.status,
        "coverageComplete": data.coverageComplete,
        "notes": data.notes.strip(),
    }


@app.get("/api/admin/costs/plan-versions")
async def admin_cost_plan_versions(request: Request, year: int | None = None, status: str = "", scenario: str = "") -> dict[str, Any]:
    require_platform_admin(request)
    items = await _call_store_optional(_admin_observability_store(), ("list_cost_plan_versions",), year=year, status=status, scenario=scenario, default=[])
    return _observability_envelope([_plan_version_payload(dict(item)) for item in (items or [])], source="cost plan ledger")


@app.post("/api/admin/costs/plan-versions")
async def admin_create_cost_plan_version(data: CostPlanVersionRequest, request: Request) -> dict[str, Any]:
    require_platform_admin(request)
    await enforce_csrf(request)
    item = await _call_store_optional(_admin_observability_store(), ("create_cost_plan_version",), _cost_plan_input(data), default=None)
    if item is None:
        raise HTTPException(status_code=503, detail="费用计划存储尚未就绪")
    await _invalidate_observability_dashboard("cost")
    return _observability_envelope(_plan_version_payload(dict(item)), source="cost plan ledger")


@app.patch("/api/admin/costs/plan-versions/{plan_id}")
async def admin_update_cost_plan_version(plan_id: str, data: CostPlanVersionRequest, request: Request) -> dict[str, Any]:
    require_platform_admin(request)
    await enforce_csrf(request)
    item = await _call_store_optional(_admin_observability_store(), ("update_cost_plan_version",), plan_id, _cost_plan_input(data, plan_id), default=None)
    if item is None:
        raise HTTPException(status_code=404, detail="费用计划版本不存在")
    await _invalidate_observability_dashboard("cost")
    return _observability_envelope(_plan_version_payload(dict(item)), source="cost plan ledger")


async def _change_cost_plan_state(plan_id: str, request: Request, operation: str) -> dict[str, Any]:
    admin = require_platform_admin(request)
    await enforce_csrf(request)
    item = await _call_store_optional(
        _admin_observability_store(),
        (f"{operation}_cost_plan_version", f"cost_plan_version_{operation}"),
        plan_id,
        str(admin.get("email") or admin.get("name") or "platform-admin"),
        default=None,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="费用计划版本不存在或状态不可变更")
    await _invalidate_observability_dashboard("cost")
    return _observability_envelope(_plan_version_payload(dict(item)), source="cost plan ledger")


@app.post("/api/admin/costs/plan-versions/{plan_id}/approve")
async def admin_approve_cost_plan_version(plan_id: str, request: Request) -> dict[str, Any]:
    return await _change_cost_plan_state(plan_id, request, "approve")


@app.post("/api/admin/costs/plan-versions/{plan_id}/activate")
async def admin_activate_cost_plan_version(plan_id: str, request: Request) -> dict[str, Any]:
    return await _change_cost_plan_state(plan_id, request, "activate")


@app.post("/api/admin/costs/plan-versions/{plan_id}/archive")
async def admin_archive_cost_plan_version(plan_id: str, request: Request) -> dict[str, Any]:
    return await _change_cost_plan_state(plan_id, request, "archive")


def _savings_measurement_input(data: SavingsMeasurementRequest, measurement_id: str | None = None) -> dict[str, Any]:
    return {
        "id": measurement_id or uuid.uuid4().hex,
        "actionId": data.actionId.strip(), "scopeKey": data.scope.strip(),
        "provider": data.provider.strip(), "model": data.model.strip(), "costBucket": data.costBucket.strip(),
        "baselineStartDate": data.baselineStart.isoformat(), "baselineEndDate": data.baselineEnd.isoformat(),
        "measurementStartDate": data.measurementStart.isoformat(), "measurementEndDate": data.measurementEnd.isoformat(),
        "baselineAmountUsd": data.baselineAmountUsd, "actualAmountUsd": data.actualAmountUsd,
        "evidenceUrl": data.evidenceUrl.strip(), "financeReviewer": data.financeReviewer.strip(),
        "reviewedAt": data.reviewedAt.isoformat() if data.reviewedAt else None,
        "status": data.status, "notes": data.notes.strip(),
    }


@app.get("/api/admin/costs/savings-measurements")
async def admin_savings_measurements(request: Request, as_of: str | None = None, year: int | None = None) -> dict[str, Any]:
    require_platform_admin(request)
    try:
        cutoff = date.fromisoformat(as_of) if as_of else date.today()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="as_of 格式应为 YYYY-MM-DD") from exc
    items = await _call_store_optional(_admin_observability_store(), ("list_savings_measurements",), as_of=cutoff.isoformat(), year=year, default=[])
    return _observability_envelope(reviewed_savings_measurements([dict(item) for item in (items or [])], cutoff), source="reviewed savings measurements")


@app.post("/api/admin/costs/savings-measurements")
async def admin_create_savings_measurement(data: SavingsMeasurementRequest, request: Request) -> dict[str, Any]:
    require_platform_admin(request)
    await enforce_csrf(request)
    item = await _call_store_optional(_admin_observability_store(), ("create_savings_measurement",), _savings_measurement_input(data), default=None)
    if item is None:
        raise HTTPException(status_code=503, detail="节省核验存储尚未就绪")
    await _invalidate_observability_dashboard("cost")
    return _observability_envelope(item, source="reviewed savings measurements")


@app.patch("/api/admin/costs/savings-measurements/{measurement_id}")
async def admin_update_savings_measurement(measurement_id: str, data: SavingsMeasurementRequest, request: Request) -> dict[str, Any]:
    require_platform_admin(request)
    await enforce_csrf(request)
    item = await _call_store_optional(_admin_observability_store(), ("update_savings_measurement",), measurement_id, _savings_measurement_input(data, measurement_id), default=None)
    if item is None:
        raise HTTPException(status_code=404, detail="节省核验记录不存在")
    await _invalidate_observability_dashboard("cost")
    return _observability_envelope(item, source="reviewed savings measurements")


@app.delete("/api/admin/costs/savings-measurements/{measurement_id}")
async def admin_delete_savings_measurement(measurement_id: str, request: Request) -> dict[str, Any]:
    require_platform_admin(request)
    await enforce_csrf(request)
    deleted = await _call_store_optional(_admin_observability_store(), ("delete_savings_measurement",), measurement_id, default=False)
    if not deleted:
        raise HTTPException(status_code=404, detail="节省核验记录不存在")
    await _invalidate_observability_dashboard("cost")
    return _observability_envelope({"deleted": True}, source="reviewed savings measurements")


@app.get("/api/admin/costs/savings-actions")
async def admin_savings_actions(request: Request) -> dict[str, Any]:
    require_platform_admin(request)
    return _observability_envelope([_savings_payload(item) for item in await _admin_observability_store().list_savings_actions()], source="费用控制账本")


def _savings_input(data: SavingsActionRequest, action_id: str | None = None) -> dict[str, Any]:
    if data.verifiedDate and data.verifiedDate < data.implementedDate:
        raise HTTPException(status_code=400, detail="验证日期不能早于实施日期")
    if data.status == "verified" and (data.verifiedDate is None or data.verifiedDailyCost is None):
        raise HTTPException(status_code=400, detail="已验证动作必须填写验证日期和验证后日均成本")
    if data.expectedStartDate and data.expectedStartDate < data.implementedDate:
        raise HTTPException(status_code=400, detail="预计节省起始日期不能早于实施日期")
    return {
        "id": action_id or uuid.uuid4().hex,
        "name": data.name.strip(),
        "baselineDailyCost": data.baselineDailyCost,
        "implementedDate": data.implementedDate.isoformat(),
        "verifiedDate": data.verifiedDate.isoformat() if data.verifiedDate else None,
        "verifiedDailyCost": data.verifiedDailyCost,
        "owner": data.owner.strip(),
        "status": data.status,
        "expectedDailyCost": data.expectedDailyCost,
        "expectedStartDate": data.expectedStartDate.isoformat() if data.expectedStartDate else None,
        "provider": data.provider.strip(),
        "model": data.model.strip(),
        "costBucket": data.costBucket.strip(),
        "evidenceUrl": data.evidenceUrl.strip(),
        "financeReviewer": data.financeReviewer.strip(),
        "notes": data.notes.strip(),
    }


@app.post("/api/admin/costs/savings-actions")
async def admin_create_savings_action(data: SavingsActionRequest, request: Request) -> dict[str, Any]:
    require_platform_admin(request)
    await enforce_csrf(request)
    return _observability_envelope(_savings_payload(await _admin_observability_store().create_savings_action(_savings_input(data))), source="费用控制账本")


@app.patch("/api/admin/costs/savings-actions/{action_id}")
async def admin_update_savings_action(action_id: str, data: SavingsActionRequest, request: Request) -> dict[str, Any]:
    require_platform_admin(request)
    await enforce_csrf(request)
    item = await _admin_observability_store().update_savings_action(action_id, _savings_input(data, action_id))
    if not item:
        raise HTTPException(status_code=404, detail="降本动作不存在")
    return _observability_envelope(_savings_payload(item), source="费用控制账本")


async def _build_costs_overview(
    month: str | None = None,
    category: str = "",
    cost_bucket: str = "",
    model: str = "",
    vendor: str = "",
    provider: str = "",
    account_id: str = "",
    reconciliation_status: str = "",
    recognition_status: str = "",
    as_of: str | None = None,
) -> dict[str, Any]:
    target = month or date.today().strftime("%Y-%m")
    try:
        start = date.fromisoformat(f"{target}-01")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="月份格式应为 YYYY-MM") from exc
    next_month = date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
    end = next_month - timedelta(days=1)
    try:
        cutoff = date.fromisoformat(as_of) if as_of else date.today()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="as_of 格式应为 YYYY-MM-DD") from exc
    today = min(cutoff, end)
    store = _admin_observability_store()
    api_rows, api_dimensions_complete = await _cost_api_rows(
        store, start, today, model=model, provider=provider, account_id=account_id
    )
    all_items = [item for item in await store.list_cost_items() if bool(item.get("enabled"))]
    items = await _cost_actual_items(store, today, model=model, provider=provider, account_id=account_id, cost_bucket=cost_bucket)
    has_api_costs = bool(api_rows)
    has_manual_costs = bool(items)
    available_models = sorted({str(item.get("model") or "") for item in api_rows + items if item.get("model")})
    available_vendors = sorted(
        {str(item.get("source") or "") for item in api_rows if item.get("source")}
        | {str(item.get("vendor") or "") for item in items if item.get("vendor")}
        | {str(item.get("provider") or "") for item in items if item.get("provider")}
    )
    available_categories = sorted(
        ({"API Token"} if api_rows else set())
        | {str(item.get("category") or "") for item in items if item.get("category")}
    )
    available_cost_buckets = sorted(
        ({"api_usage"} if api_rows else set()) | {_cost_bucket(item) for item in items}
    )
    api_rows, items = _filter_cost_sources(
        api_rows,
        items,
        category=category,
        cost_bucket=cost_bucket,
        model=model,
        vendor=vendor,
        provider=provider,
        account_id=account_id,
        reconciliation_status=reconciliation_status,
        recognition_status=recognition_status,
    )
    ledger_rows = [_api_cost_ledger_row(row) for row in api_rows]
    for item in items:
        ledger_rows.extend(_manual_cost_ledger_rows(item, start, today))
    summary = _cost_summary_splits(ledger_rows)
    daily_non_api: dict[str, float] = defaultdict(float)
    daily: dict[str, dict[str, float]] = defaultdict(lambda: {"api": 0.0, "nonApi": 0.0})
    bucket_daily: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in ledger_rows:
        day = str(row.get("date") or "")
        bucket = str(row.get("costBucket") or "other")
        amount = float(row.get("amountUsd") or 0)
        if bucket == "api_usage":
            daily[day]["api"] += amount
        else:
            daily_non_api[day] += amount
            daily[day]["nonApi"] += amount
        bucket_daily[day][bucket] += amount
    actual = sum(sum(value.values()) for value in daily.values())
    budgets = await store.list_cost_budgets()
    budget_record = next((item for item in budgets if str(item.get("month"))[:7] == target), None)
    budget = (
        float(budget_record.get("budget_usd") or 0)
        if budget_record
        else env_float("COST_DEFAULT_MONTHLY_BUDGET_USD", 60000)
    )
    daily_target = (
        float(budget_record.get("daily_target_usd") or 0)
        if budget_record
        else env_float("COST_DEFAULT_DAILY_TARGET_USD", 2000)
    )
    actions = [_savings_payload(item) for item in await store.list_savings_actions()]
    measurements = await _call_store_optional(store, ("list_savings_measurements",), as_of=today.isoformat(), default=[])
    audited_savings = reviewed_savings_measurements([dict(item) for item in (measurements or [])], today)
    savings = _savings_totals(actions, today, end)
    forecast = monthly_forecast(actual, start, today, budget)
    model_split: dict[str, float] = defaultdict(float)
    for row in ledger_rows:
        if str(row.get("costBucket") or "other") == "api_usage" and row.get("model"):
            model_split[str(row.get("model") or "unknown")] += float(row.get("amountUsd") or 0)
    trend_end = end if target != date.today().strftime("%Y-%m") else today
    all_days = [start + timedelta(days=offset) for offset in range((trend_end - start).days + 1)]
    month_days = (end - start).days + 1
    budget_daily = budget / month_days if budget is not None else None
    elapsed_days = max(1, (today - start).days + 1)
    projected_daily = actual / elapsed_days if elapsed_days else 0.0
    response = _observability_envelope(
        {
            "month": target,
            "summary": summary,
            "metrics": {
                **forecast,
                "dailyTarget": daily_target,
                "verifiedSavings": audited_savings["realizedSavingsUsd"],
                **savings,
                "monthToDateActual": round(actual, 2),
                "monthForecast": round(float(forecast.get("forecast") or 0), 2),
                "monthBudget": round(budget, 2) if budget is not None else None,
                "monthVariance": (
                    round(float(forecast.get("budgetDelta") or 0), 2)
                    if forecast.get("budgetDelta") is not None
                    else None
                ),
                "dailyAverage": round(float(forecast.get("dailyAverage") or 0), 2),
                "metricEnvelopes": {
                    "monthToDateActual": metric_envelope(round(actual, 2), "USD", period=_metric_period(start.isoformat(), today.isoformat()), as_of=today, source="actual ledger", coverage_rate=1.0, sample_count=len(ledger_rows)),
                    "forecast": metric_envelope(round(float(forecast.get("forecast") or 0), 2), "USD", period=_metric_period(start.isoformat(), end.isoformat()), as_of=today, source="actual ledger run-rate", coverage_rate=1.0 if ledger_rows else 0.0, sample_count=len(ledger_rows), status="derived" if ledger_rows else "unavailable", missing_reasons=[] if ledger_rows else ["actual_ledger_missing"]),
                    "verifiedSavings": metric_envelope(audited_savings["realizedSavingsUsd"], "USD", period=_metric_period(start.isoformat(), today.isoformat()), as_of=today, source="reviewed savings measurements", coverage_rate=1.0 if measurements else 0.0, sample_count=audited_savings["reviewedCount"], status="observed" if audited_savings["reviewedCount"] else "unavailable", missing_reasons=[] if audited_savings["reviewedCount"] else ["reviewed_savings_measurements_missing"]),
                    "dailyTarget": metric_envelope(daily_target, "USD/day", period=_metric_period(start.isoformat(), end.isoformat()), as_of=today, source="approved budget" if budget_record else "environment default", coverage_rate=1.0 if budget_record else 0.0, sample_count=1, status="observed" if budget_record else "default", missing_reasons=[] if budget_record else ["budget_record_missing"]),
                },
            },
            "trend": [
                {
                    "date": day.isoformat(),
                    "actual": sum(value.values()),
                    "forecast": sum(value.values()) if day <= today else projected_daily,
                    "budget": budget_daily,
                    "api": value["api"],
                    "nonApi": value["nonApi"],
                    "costBuckets": dict(bucket_daily.get(day.isoformat(), {})),
                }
                for day in all_days
                for value in [daily.get(day.isoformat(), {"api": 0.0, "nonApi": 0.0})]
            ],
            "modelSplit": [
                {"model": key, "spend": value}
                for key, value in sorted(model_split.items(), key=lambda item: item[1], reverse=True)
            ],
            "providerSplit": [
                {"provider": key, "spend": value}
                for key, value in sorted(
                    ((key, sum(float(row.get("amountUsd") or 0) for row in ledger_rows if str(row.get("provider") or "unknown") == key)) for key in {str(row.get("provider") or "unknown") for row in ledger_rows}),
                    key=lambda item: item[1], reverse=True
                )
            ],
            "bucketSplit": [
                {"costBucket": key, "label": _COST_BUCKET_LABELS.get(key, key), "spend": value}
                for key, value in sorted(
                    ((key, sum(float(row.get("amountUsd") or 0) for row in ledger_rows if str(row.get("costBucket") or "other") == key)) for key in {str(row.get("costBucket") or "other") for row in ledger_rows}),
                    key=lambda item: item[1], reverse=True
                )
            ],
            "ledger": {"rows": ledger_rows, "total": len(ledger_rows)},
            # Keep the legacy overview fields while the dedicated endpoints migrate consumers.
            "costItems": [_cost_item_payload(item) for item in items],
            "savingsActions": actions,
            "savingsMeasurements": audited_savings,
            "asOf": today.isoformat(),
            "composition": [
                {
                    "key": key,
                    "label": _COST_BUCKET_LABELS.get(key, key),
                    "amountUsd": round(value, 2),
                }
                for key, value in sorted(
                    ((key, sum(float(row.get("amountUsd") or 0) for row in ledger_rows if str(row.get("costBucket") or "other") == key)) for key in {str(row.get("costBucket") or "other") for row in ledger_rows}),
                    key=lambda item: item[1],
                    reverse=True,
                )
            ],
            **_cost_summary_splits(ledger_rows),
            "filters": {
                "categories": available_categories,
                "models": available_models,
                "vendors": available_vendors,
                "providers": available_vendors,
                "accounts": sorted({str(item.get("accountId") or "") for item in ledger_rows if item.get("accountId")}),
                "costBuckets": available_cost_buckets,
                "reconciliationStatuses": ["unreconciled", "pending", "matched", "partial", "exception", "waived"],
                "recognitionStatuses": ["actual", "committed", "planned"],
            },
        },
        coverage={
            "partial": False,
            "incomplete": not has_api_costs or not has_manual_costs or not api_dimensions_complete,
            "sources": {"api": has_api_costs, "manual": has_manual_costs},
            "missingDimensions": [] if api_dimensions_complete else ["provider"],
        },
        source="费用控制账本",
    )
    annual, all_budgets = await asyncio.gather(
        _build_costs_annual(
            year=start.year, as_of=today.isoformat(), category=category,
            cost_bucket=cost_bucket, model=model, vendor=vendor, provider=provider,
            account_id=account_id, reconciliation_status=reconciliation_status,
            recognition_status=recognition_status,
        ),
        store.list_cost_budgets(),
    )
    response["data"]["annual"] = annual.get("data") or {}
    response["data"]["budgets"] = [
        {"month": str(item.get("month")), "budgetUsd": float(item.get("budget_usd") or 0), "dailyTargetUsd": float(item.get("daily_target_usd") or 0)}
        for item in all_budgets
    ]
    response["data"]["savingsSummary"] = audited_savings
    return response


@app.get("/api/admin/costs/overview")
async def admin_costs_overview(
    request: Request,
    month: str | None = None,
    category: str = "", cost_bucket: str = "", model: str = "", vendor: str = "",
    provider: str = "", account_id: str = "", reconciliation_status: str = "",
    recognition_status: str = "", as_of: str | None = None, refresh: int = 0,
) -> dict[str, Any]:
    require_platform_admin(request)
    target = month or date.today().strftime("%Y-%m")
    cutoff = as_of or date.today().isoformat()
    key = {"month": target, "asOf": cutoff, "category": category, "costBucket": cost_bucket, "model": model, "vendor": vendor, "provider": provider, "accountId": account_id, "reconciliation": reconciliation_status, "recognition": recognition_status, "definition": "cost-v2"}
    return await _cached_observability_dashboard(
        "cost", key,
        lambda: _build_costs_overview(target, category, cost_bucket, model, vendor, provider, account_id, reconciliation_status, recognition_status, cutoff),
        refresh=bool(refresh),
    )


@app.get("/api/admin/costs/ledger")
async def admin_costs_ledger(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    cost_bucket: str = "",
    category: str = "",
    provider: str = "",
    vendor: str = "",
    model: str = "",
    account_id: str = "",
    reconciliation_status: str = "",
    recognition_status: str = "",
    as_of: str | None = None,
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    require_platform_admin(request)
    try:
        cutoff = date.fromisoformat(as_of) if as_of else date.today()
        end = date.fromisoformat(end_date) if end_date else cutoff
        start = date.fromisoformat(start_date) if start_date else end.replace(day=1)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="日期格式应为 YYYY-MM-DD") from exc
    if end < start:
        raise HTTPException(status_code=400, detail="结束日期不能早于开始日期")
    if cutoff < start:
        raise HTTPException(status_code=400, detail="as_of 不能早于查询开始日期")
    today = min(cutoff, end)
    store = _admin_observability_store()
    api_rows, api_dimensions_complete = await _cost_api_rows(
        store, start, today, model=model, provider=provider, account_id=account_id
    )
    items = await _cost_actual_items(store, today, model=model, provider=provider, account_id=account_id, cost_bucket=cost_bucket)
    api_rows, items = _filter_cost_sources(
        api_rows,
        items,
        category=category,
        cost_bucket=cost_bucket,
        model=model,
        vendor=vendor,
        provider=provider,
        account_id=account_id,
        reconciliation_status=reconciliation_status,
        recognition_status=recognition_status,
    )
    ledger_rows = [_api_cost_ledger_row(row) for row in api_rows]
    for item in items:
        ledger_rows.extend(_manual_cost_ledger_rows(item, start, today))
    ledger_rows.sort(key=lambda item: (str(item.get("date") or ""), float(item.get("amountUsd") or 0)), reverse=True)
    page_size = max(1, min(500, page_size))
    offset = (max(1, page) - 1) * page_size
    total_pages = math.ceil(len(ledger_rows) / page_size) if ledger_rows else 0
    return _observability_envelope(
        {
            "items": ledger_rows[offset : offset + page_size],
            "total": len(ledger_rows),
            "page": max(1, page),
            "pageSize": page_size,
            "totalPages": total_pages,
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "asOf": today.isoformat(),
        },
        coverage={
            "partial": False,
            "incomplete": not bool(api_rows or items) or not api_dimensions_complete,
            "missingDimensions": [] if api_dimensions_complete else ["provider"],
        },
        source="cost ledger",
    )


async def _build_costs_annual(
    year: int | None = None,
    as_of: str | None = None,
    category: str = "",
    cost_bucket: str = "",
    model: str = "",
    vendor: str = "",
    provider: str = "",
    account_id: str = "",
    reconciliation_status: str = "",
    recognition_status: str = "",
) -> dict[str, Any]:
    target_year = year or date.today().year
    start = date(target_year, 1, 1)
    end = date(target_year, 12, 31)
    try:
        cutoff = date.fromisoformat(as_of) if as_of else date.today()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="as_of 格式应为 YYYY-MM-DD") from exc
    today = min(cutoff, end)
    store = _admin_observability_store()
    api_rows, api_dimensions_complete = await _cost_api_rows(store, start, today, model=model, provider=provider, account_id=account_id)
    all_items = [item for item in await store.list_cost_items() if bool(item.get("enabled"))]
    actual_items = await _cost_actual_items(store, today, model=model, provider=provider, account_id=account_id, cost_bucket=cost_bucket)
    api_rows, actual_items = _filter_cost_sources(
        api_rows, actual_items, category=category, cost_bucket=cost_bucket, model=model,
        vendor=vendor, provider=provider, account_id=account_id,
        reconciliation_status=reconciliation_status,
        recognition_status="actual" if recognition_status in {"", "actual"} else recognition_status,
    )
    monthly: dict[str, float] = defaultdict(float)
    for row in api_rows:
        monthly[str(row.get("usage_date"))[:7]] += float(row.get("spend") or 0)
    for item in actual_items:
        for row in _manual_cost_ledger_rows(item, start, today):
            monthly[str(row.get("date") or "")[:7]] += float(row.get("amountUsd") or 0)
    months: list[dict[str, Any]] = []
    for month_no in range(1, 13):
        month_start = date(target_year, month_no, 1)
        next_start = date(target_year + 1, 1, 1) if month_no == 12 else date(target_year, month_no + 1, 1)
        month_end = next_start - timedelta(days=1)
        actual_month = min(today, month_end)
        elapsed = max(0, (actual_month - month_start).days + 1) if month_start <= actual_month else 0
        month_actual = monthly.get(month_start.strftime("%Y-%m"), 0.0)
        month_forecast = (
            month_actual + max(0, (month_end - month_start).days + 1 - elapsed) * (month_actual / elapsed)
            if elapsed else None
        )
        months.append({
            "month": month_start.strftime("%Y-%m"),
            "actual": round(month_actual, 2),
            "forecast": round(month_forecast, 2) if month_forecast is not None else None,
            "daysElapsed": elapsed,
            "daysInMonth": (month_end - month_start).days + 1,
            "dailyAverage": round(month_actual / elapsed, 2) if elapsed else None,
            "budgetUsd": None,
        })
    actual_total = sum(float(item["actual"]) for item in months)
    run_rate_total = sum(float(item["forecast"] or item["actual"]) for item in months)
    plans = await _call_store_optional(store, ("list_cost_plan_versions",), year=target_year, default=[])
    active_plan = _active_baseline_plan([dict(item) for item in (plans or [])], target_year)
    plan_items = await _official_plan_items(store, active_plan)
    future_plan_rows = _official_plan_rows(plan_items or all_items, active_plan, today + timedelta(days=1), end)
    future_plan_rows = [row for row in future_plan_rows if (
        (not category or str(row.get("category") or "") == category)
        and (not cost_bucket or str(row.get("costBucket") or "") == cost_bucket)
        and (not model or str(row.get("model") or "") == model)
        and (not vendor or str(row.get("vendor") or "") == vendor)
        and (not provider or str(row.get("provider") or "") == provider)
        and (not account_id or str(row.get("accountId") or "") == account_id)
        and (not reconciliation_status or str(row.get("reconciliationStatus") or "") == reconciliation_status)
        and (not recognition_status or str(row.get("recognitionStatus") or "") == recognition_status)
    )]
    official_forecast = round(actual_total + sum(float(row.get("amountUsd") or 0) for row in future_plan_rows), 2) if active_plan else None
    budgets = await store.list_cost_budgets()
    for month_item in months:
        budget_record = next(
            (item for item in budgets if str(item.get("month"))[:7] == month_item["month"]),
            None,
        )
        if budget_record is not None:
            month_item["budgetUsd"] = round(float(budget_record.get("budget_usd") or 0), 2)
    budget_total = sum(
        float(item.get("budgetUsd") or 0)
        for item in months
        if item.get("budgetUsd") is not None
    )
    budgeted_months = sum(1 for item in months if item.get("budgetUsd") is not None)
    year_budget = (
        round(budget_total / budgeted_months * 12, 2)
        if budgeted_months
        else (
            round(float(env_float("COST_DEFAULT_MONTHLY_BUDGET_USD", 60000)) * 12, 2)
        )
    )
    return _observability_envelope(
        {
            "year": target_year,
            "months": months,
            "actual": round(actual_total, 2),
            "forecast": official_forecast,
            "actualToDate": round(actual_total, 2),
            "yearToDateForecast": official_forecast,
            "officialForecast": official_forecast,
            "runRateScenario": round(run_rate_total, 2),
            "activePlan": _plan_version_payload(active_plan) if active_plan else None,
            "budget": year_budget,
            "budgetDelta": round(official_forecast - year_budget, 2) if year_budget is not None and official_forecast is not None else None,
            "metricEnvelopes": {
                "actualToDate": metric_envelope(round(actual_total, 2), "USD", period=_metric_period(start.isoformat(), today.isoformat()), as_of=today, source="actual ledger", coverage_rate=1.0, sample_count=len(api_rows) + len(actual_items)),
                "officialForecast": metric_envelope(official_forecast, "USD", period=_metric_period(start.isoformat(), end.isoformat()), as_of=today, status="observed" if official_forecast is not None else "unavailable", source="actual ledger + active approved baseline plan", coverage_rate=1.0 if official_forecast is not None else 0.0, sample_count=len(future_plan_rows), missing_reasons=[] if official_forecast is not None else ["active_approved_plan_missing_or_incomplete"]),
                "runRateScenario": metric_envelope(round(run_rate_total, 2), "USD", period=_metric_period(start.isoformat(), end.isoformat()), as_of=today, status="derived", source="daily run-rate scenario", coverage_rate=1.0, sample_count=len(api_rows) + len(actual_items)),
            },
            "annualTrend": [
                {
                    "month": item["month"],
                    "actual": item["actual"],
                    "forecast": item["forecast"],
                    "budget": item["budgetUsd"],
                }
                for item in months
            ],
            "throughDate": today.isoformat(),
            "asOf": today.isoformat(),
        },
        coverage={
            "partial": False,
            "incomplete": not bool(api_rows or actual_items) or not api_dimensions_complete,
            "missingDimensions": [] if api_dimensions_complete else ["provider"],
        },
        source="cost ledger",
    )


@app.get("/api/admin/costs/annual")
async def admin_costs_annual(
    request: Request,
    year: int | None = None,
    as_of: str | None = None,
    category: str = "",
    cost_bucket: str = "",
    model: str = "",
    vendor: str = "",
    provider: str = "",
    account_id: str = "",
    reconciliation_status: str = "",
    recognition_status: str = "",
) -> dict[str, Any]:
    require_platform_admin(request)
    return await _build_costs_annual(
        year=year,
        as_of=as_of,
        category=category,
        cost_bucket=cost_bucket,
        model=model,
        vendor=vendor,
        provider=provider,
        account_id=account_id,
        reconciliation_status=reconciliation_status,
        recognition_status=recognition_status,
    )


@app.get("/api/admin/costs/savings/overview")
async def admin_costs_savings_overview(request: Request, as_of: str | None = None) -> dict[str, Any]:
    require_platform_admin(request)
    try:
        today = date.fromisoformat(as_of) if as_of else date.today()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="as_of 格式应为 YYYY-MM-DD") from exc
    store = _admin_observability_store()
    actions = [_savings_payload(item) for item in await store.list_savings_actions()]
    measurements = await _call_store_optional(store, ("list_savings_measurements",), as_of=today.isoformat(), default=[])
    audited = reviewed_savings_measurements([dict(item) for item in (measurements or [])], today)
    return _observability_envelope(
        {
            "metrics": {
                **_savings_totals(actions, today, date(today.year, 12, 31)),
                "realizedSavingsToDate": audited["realizedSavingsUsd"],
                "metricEnvelopes": {
                    "realizedSavingsToDate": metric_envelope(audited["realizedSavingsUsd"], "USD", period=_metric_period(date(today.year, 1, 1).isoformat(), today.isoformat()), as_of=today, source="reviewed savings measurements", coverage_rate=1.0 if measurements else 0.0, sample_count=audited["reviewedCount"], status="observed" if audited["reviewedCount"] else "unavailable", missing_reasons=[] if audited["reviewedCount"] else ["reviewed_savings_measurements_missing"]),
                },
            },
            "actions": actions,
            "measurements": audited,
            "asOf": today.isoformat(),
        },
        coverage={"partial": False, "incomplete": False},
        source="cost ledger",
    )


@app.get("/api/me/keys")
async def my_keys(
    request: Request,
    refresh: bool = Query(False),
    include_models: bool = Query(True),
) -> dict[str, Any]:
    app_user, upstream_user = await current_upstream_user(request)
    require_active_local_entitlement(app_user)
    user_ids = upstream_user_ids(upstream_user)
    if not user_ids:
        raise HTTPException(status_code=502, detail="上游员工记录缺少 user_id")
    if include_models:
        primary_user_id = primary_upstream_user_id(upstream_user)
        # 模型权限与密钥列表相互独立，保留兼容调用方的并行加载。
        (available_models, unrestricted), keys = await asyncio.gather(
            client().available_key_models(primary_user_id),
            client().keys_for_user_ids(user_ids, refresh),
        )
    else:
        available_models, unrestricted = [], False
        keys = await client().keys_for_user_ids(user_ids, refresh)
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


# ---- 团队负责人管理成员密钥 ----


TEAM_KEY_DELETABLE_STATUSES = {"已禁用", "已过期"}


async def team_member_accounts(
    app_user: dict[str, Any],
    team_ref_value: str | None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """解析当前负责人可管理的团队与其中的普通成员。

    只服务企业 SSO 负责人：本地账号（含甲方企业部门负责人）的成员令牌走企业
    令牌管理页，与这里的上游个人密钥不是同一套数据。
    """

    if app_user.get("id"):
        raise HTTPException(status_code=403, detail="当前账号还没有团队负责人权限")
    scope = await team_scope_for_user(app_user, False)
    if not scope.get("isTeamLeader"):
        raise HTTPException(status_code=403, detail="当前账号还没有团队负责人权限")
    team = select_authorized_team(scope, (team_ref_value or "").strip() or None)
    store = usage_store()
    directory_loader = getattr(store, "team_member_directory", None) if store is not None else None
    if not callable(directory_loader):
        raise HTTPException(status_code=503, detail="团队成员快照尚未就绪，请等待后台同步完成")
    await store.connect()
    try:
        rows = await directory_loader(team_scope_items(team))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("failed to load team member directory")
        raise HTTPException(status_code=503, detail="团队成员名单暂时不可用，请稍后重试") from exc
    leader_email = str(app_user.get("email") or "").strip().lower()
    members = [
        row
        for row in rows
        if str(row.get("teamRole") or "user").lower() != "admin"
        and str(row.get("employeeEmail") or "").strip().lower() != leader_email
    ]
    return scope, team, members


async def team_member_keys(members: list[dict[str, Any]], refresh: bool) -> list[dict[str, Any]]:
    """按成员账号拉取上游密钥，并回填成员身份信息。"""

    if not members:
        return []
    member_by_account = {str(item["accountId"]): item for item in members}
    keys = await client().keys_for_user_ids(list(member_by_account.keys()), refresh)
    enriched: list[dict[str, Any]] = []
    for key in keys:
        account_id = f"{key.get('_backendId') or 'primary'}:{key.get('_userId') or ''}"
        member = member_by_account.get(account_id)
        if member is None:
            # 上游返回了不在名册里的账号，宁可漏掉也不越权展示。
            continue
        enriched.append(
            {
                **public_key(key, False),
                "memberName": member.get("employeeName") or member.get("employeeEmail") or "",
                "memberEmail": member.get("employeeEmail") or "",
                "_accountId": account_id,
            }
        )
    return enriched


async def locate_team_member_key(
    app_user: dict[str, Any],
    team_ref_value: str | None,
    key_id: str,
) -> tuple[dict[str, Any], str]:
    """重新在服务端推导密钥归属账号，浏览器只需提供团队标识与密钥 id。"""

    _scope, _team, members = await team_member_accounts(app_user, team_ref_value)
    keys = await team_member_keys(members, refresh=True)
    owned = next((item for item in keys if str(item.get("id") or "") == key_id), None)
    if owned is None:
        raise HTTPException(status_code=403, detail="无权管理该密钥")
    return owned, str(owned.pop("_accountId", ""))


@app.get("/api/team/keys")
async def team_keys(
    request: Request,
    team_ref: str | None = None,
    search: str = Query(""),
    status: str = Query("all"),
    refresh: bool = Query(False),
) -> dict[str, Any]:
    app_user = require_user(request)
    await require_non_inactive_demo_identity(app_user)
    scope, team, members = await team_member_accounts(app_user, team_ref)
    keys = await team_member_keys(members, refresh)
    for key in keys:
        key.pop("_accountId", None)
    stats = {
        "total": len(keys),
        "active": sum(1 for item in keys if str(item.get("status") or "") == "正常"),
        "disabled": sum(1 for item in keys if str(item.get("status") or "") == "已禁用"),
        "expired": sum(1 for item in keys if str(item.get("status") or "") == "已过期"),
    }
    keyword = " ".join(str(search or "").split()).lower()
    if keyword:
        keys = [
            item
            for item in keys
            if keyword in str(item.get("memberName") or "").lower()
            or keyword in str(item.get("memberEmail") or "").lower()
            or keyword in str(item.get("keyType") or "").lower()
            or keyword in str(item.get("name") or "").lower()
            or keyword in str(item.get("masked") or "").lower()
        ]
    normalized_status = str(status or "all").strip() or "all"
    if normalized_status != "all":
        keys = [item for item in keys if str(item.get("status") or "") == normalized_status]
    return {
        "team": public_team(team),
        "teams": [item for item in (public_team(entry) for entry in scope.get("leaderTeams") or []) if item],
        "memberCount": len(members),
        "keys": keys,
        "stats": stats,
    }


@app.post("/api/team/keys/{key_id:path}/revoke")
async def revoke_team_key(
    key_id: str,
    request: Request,
    data: TeamKeyMutationRequest | None = None,
) -> dict[str, Any]:
    """停用团队普通成员的密钥：立即失效，但仍保留在列表里以保住用量归属。"""

    await enforce_csrf(request)
    app_user = require_user(request)
    await require_non_inactive_demo_identity(app_user)
    owned, account_id = await locate_team_member_key(app_user, (data.teamRef if data else None), key_id)
    if str(owned.get("status") or "") == "已禁用":
        raise HTTPException(status_code=409, detail="该密钥已经是停用状态")
    try:
        await client().block_key(key_id, account_id, app_user["email"])
    except HTTPException:
        write_key_audit("team_revoke", app_user["email"], key_id, request, "failed")
        raise
    write_key_audit("team_revoke", app_user["email"], key_id, request, "success")
    return {"ok": True, "keyId": key_id}


@app.post("/api/team/keys/{key_id:path}/delete")
async def delete_team_key(
    key_id: str,
    request: Request,
    data: TeamKeyMutationRequest | None = None,
) -> dict[str, Any]:
    """删除团队普通成员的密钥，必须先撤销，避免一步误删仍在使用的密钥。"""

    await enforce_csrf(request)
    app_user = require_user(request)
    await require_non_inactive_demo_identity(app_user)
    owned, account_id = await locate_team_member_key(app_user, (data.teamRef if data else None), key_id)
    if str(owned.get("status") or "") not in TEAM_KEY_DELETABLE_STATUSES:
        raise HTTPException(status_code=409, detail="请先撤销该密钥再删除")
    try:
        await client().delete_key(key_id, account_id, app_user["email"])
    except HTTPException:
        write_key_audit("team_delete", app_user["email"], key_id, request, "failed")
        raise
    backend_id, _, raw_user_id = account_id.partition(":")
    try:
        key_vault().delete(backend_id or "primary", raw_user_id, key_id)
        warning = ""
    except KeyVaultError:
        logger.exception("failed to delete team member key from vault")
        warning = "密钥已删除并立即失效，但本地加密保管记录清理失败，请联系管理员处理。"
    write_key_audit("team_delete", app_user["email"], key_id, request, "success_vault_failed" if warning else "success")
    return {"ok": True, "keyId": key_id, "warning": warning}


# ---- 充值中心 ----


async def billing_identity(request: Request) -> tuple[dict[str, Any], str]:
    """返回当前用户与其上游账号标识。

    刻意不调用 :func:`require_active_local_entitlement`：新注册用户正是要靠充值
    才能拿到权限，若在这里挡住，新用户永远无法自助开通。
    """
    app_user = require_user(request)
    if str(
        app_user.get("accountType") or app_user.get("account_type") or "personal"
    ) == "enterprise_managed":
        raise auth_http_error(
            403,
            "企业托管账号使用企业额度，不提供个人充值",
            "ORGANIZATION_BILLING_FORBIDDEN",
        )
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
    real_member = (
        await active_real_organization_membership(app_user)
        if organization_real_enabled()
        else None
    )
    if await is_demo_customer_user(app_user):
        raise auth_http_error(403, "企业演示账号不提供模型目录查询", "ORGANIZATION_MODELS_FORBIDDEN")
    if real_member is not None:
        require_real_organization_capability()
        return {"models": await client().models(None)}
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
async def index() -> HTMLResponse:
    return spa_html_response()


@app.get("/{path:path}")
async def spa_fallback(path: str) -> HTMLResponse:
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="接口不存在")
    return spa_html_response()
