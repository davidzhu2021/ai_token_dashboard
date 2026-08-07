import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from .cache import TTLCache


logger = logging.getLogger("ai-token-dashboard.litellm")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _source_filter_applies(source: str | None) -> bool:
    return bool(source and source != "all")


@dataclass(frozen=True)
class LiteLLMBackend:
    id: str
    label: str
    base_url: str
    admin_key: str
    source: str | None = None


@dataclass(frozen=True)
class KeyModelScope:
    models: list[str]
    unrestricted: bool


class UsageLogRows(dict[str, list[dict[str, Any]]]):
    """Aggregated log rows plus a non-breaking raw-event side channel."""

    def __init__(
        self,
        *args: Any,
        events: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.events = list(events or [])


ALL_PROXY_MODELS = "all-proxy-models"
NO_DEFAULT_MODELS = "no-default-models"
DEFAULT_PERSONAL_KEY_MAX_BUDGET = 100
DEFAULT_PERSONAL_KEY_BUDGET_DURATION = "1d"
LOCAL_AUTH_UPSTREAM_ROLES = {"internal_user", "internal_user_viewer"}


def _as_number(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    return int(_as_number(value))


def _first(record: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in record and record[name] is not None:
            return record[name]
    return default


def _records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "results", "items", "logs", "keys", "models", "users", "teams"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _metadata_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _source_text_parts(value: Any, *, _depth: int = 0, _seen: set[int] | None = None) -> list[str]:
    """Flatten heterogeneous upstream tag fields without trusting their shape."""
    if value is None or _depth >= 12:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError):
            return [text]
        if decoded is value:
            return [text]
        return _source_text_parts(decoded, _depth=_depth + 1, _seen=_seen)
    if isinstance(value, (dict, list, tuple, set)):
        seen = _seen if _seen is not None else set()
        value_id = id(value)
        if value_id in seen:
            return []
        seen.add(value_id)
        parts: list[str] = []
        items = value.items() if isinstance(value, dict) else enumerate(value)
        for key, item in items:
            if isinstance(value, dict):
                parts.extend(_source_text_parts(key, _depth=_depth + 1, _seen=seen))
            parts.extend(_source_text_parts(item, _depth=_depth + 1, _seen=seen))
        return parts
    return [_clean_text(value)] if _clean_text(value) else []


def _source_text(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        parts.extend(_source_text_parts(value))
    return " ".join(parts)


def _has_claude_cli_tag(value: Any) -> bool:
    text = _source_text(value).casefold()
    return bool(re.search(r"(?<![\w-])claude-cli(?![\w-])(?:\s*/\s*[^\s,;\]\}]+)?", text))


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _normal_email(value: Any) -> str:
    text = _clean_text(value).lower()
    return text if "@" in text else ""


def normalize_team_text(value: Any) -> str:
    """Normalize team identifiers/names for safe cross-backend matching."""
    return " ".join(_clean_text(value).split()).casefold()


def team_identity_key(team_id: Any, team_name: Any) -> str:
    return f"{normalize_team_text(team_id)}::{normalize_team_text(team_name)}"


def department_key(team_id: Any, team_name: Any) -> str:
    return team_identity_key(team_id, team_name)


def _has_cjk(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)


# 这些占位名会同时出现在多个账号上，不能当成员工姓名参与匹配。
_GENERIC_ACCOUNT_NAMES = {
    "admin",
    "administrator",
    "default",
    "default_user_id",
    "guest",
    "n/a",
    "na",
    "none",
    "null",
    "root",
    "system",
    "test",
    "unknown",
    "user",
}


def _date_text(value: Any) -> str:
    if not value:
        return date.today().isoformat()
    text = str(value)
    if "T" in text:
        return text.split("T", 1)[0]
    if " " in text:
        return text.split(" ", 1)[0]
    return text[:10]


def _datetime_text(value: Any) -> str:
    """Preserve an upstream expiry timestamp instead of truncating it to a day."""

    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        normalized = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return normalized.astimezone(timezone.utc).isoformat()
    text = str(value).strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    normalized = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat()


def _metrics_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        metrics = value.get("metrics")
        if isinstance(metrics, dict):
            return metrics
        return value
    return {}


def usage_timezone_offset_minutes() -> int:
    raw_value = os.getenv("USAGE_TIMEZONE_OFFSET_MINUTES", "-480")
    try:
        return int(raw_value)
    except ValueError:
        return -480


def usage_today() -> date:
    return (datetime.now(timezone.utc) - timedelta(minutes=usage_timezone_offset_minutes())).date()


def _local_date_window_as_utc_text(start_date: str, end_date: str) -> tuple[str, str]:
    offset = timedelta(minutes=usage_timezone_offset_minutes())
    local_start = datetime.strptime(start_date, "%Y-%m-%d")
    local_end = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    utc_start = local_start + offset
    utc_end = local_end + offset
    return utc_start.strftime("%Y-%m-%d %H:%M:%S"), utc_end.strftime("%Y-%m-%d %H:%M:%S")


def _date_text_in_usage_timezone(value: Any) -> str:
    if not value:
        return date.today().isoformat()
    text = str(value).strip()
    if "T" not in text and " " not in text:
        return _date_text(text)
    try:
        normalized = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        local = parsed.astimezone(timezone.utc) - timedelta(minutes=usage_timezone_offset_minutes())
        return local.date().isoformat()
    except ValueError:
        return _date_text(text)


def detect_source(record: dict[str, Any]) -> str:
    # LiteLLM writes the caller User-Agent to request_tags. Treat that explicit
    # client signal as stronger evidence than legacy account or key names.
    request_tags = record.get("request_tags")
    if _has_claude_cli_tag(request_tags):
        return "Claude Code"

    values = [
        _first(record, "source", "tool", "client", "application", default=""),
        _first(record, "user", "user_id", "end_user", default=""),
        _first(record, "key_alias", "key_name", "api_key_alias", default=""),
        request_tags,
        record.get("tags"),
        record.get("metadata"),
    ]
    haystack = _source_text(*values).casefold()
    if _has_claude_cli_tag(record.get("tags")) or any(
        word in haystack for word in ("claude code", "claude-code", "claudecode")
    ):
        return "Claude Code"
    if any(word in haystack for word in ("cursor", "curosr")):
        return "Cursor"
    return "其他"


def detect_source_from_key(key: dict[str, Any]) -> str:
    values = [key.get("name"), key.get("purpose"), key.get("masked"), key.get("id")]
    haystack = " ".join(str(value or "") for value in values).lower()
    if "cursor" in haystack:
        return "Cursor"
    if any(word in haystack for word in ("claude code", "claude-code", "claudecode")):
        return "Claude Code"
    return "其他"


def key_display_type(key: dict[str, Any], user_id: str = "") -> str:
    """Map internal client identifiers to employee-facing product names."""

    metadata = _metadata_dict(key.get("metadata"))
    explicit_metadata = [
        metadata.get(name)
        for name in ("source", "tool", "client", "application", "key_type", "type", "created_for")
    ]
    haystack = _source_text(user_id, key.get("key_alias"), explicit_metadata).casefold()
    if any(word in haystack for word in ("claude code", "claude-code", "claudecode", "claude-cli")):
        return "Claude Code"
    if any(word in haystack for word in ("cursor", "curosr", "codex")):
        return "Codex"
    return "-"


def tool_account_aliases(email_prefix: str) -> list[str]:
    aliases = [email_prefix, f"cursor-{email_prefix}", f"claude-code-{email_prefix}"]
    return [alias for alias in aliases if alias]


# `tool_account_aliases` 的反向操作：工具账号编号里的后缀就是员工邮箱前缀，
# 同一个人的 cursor / claude-code 两个账号里往往只有一个带姓名和邮箱。
_TOOL_ACCOUNT_PREFIXES = ("claude-code-", "cursor-")


def tool_account_email_prefix(user_id: Any) -> str:
    """从工具账号编号里取出邮箱前缀，不是工具账号时返回空串。"""

    text = _clean_text(user_id).lower()
    for prefix in _TOOL_ACCOUNT_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix) :]
    return ""


def _tool_alias_matches(alias: str, candidate: Any) -> bool:
    text = _clean_text(candidate)
    return text == alias or text.startswith(f"{alias}-")


def mask_key(value: str) -> str:
    if not value:
        return "未返回"
    if not value.startswith("sk-"):
        return "sk-...----"
    suffix = value[-4:] if len(value) >= 7 else "----"
    return f"sk-...{suffix}"


def safe_key_name(value: Any) -> str:
    text = _clean_text(value)
    return text if re.fullmatch(r"sk-\.\.\..{4}", text) else "sk-...----"


def safe_key_id(value: Any) -> str:
    text = _clean_text(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text.startswith("sk-") else text


def _is_expired(value: Any) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed <= datetime.now(timezone.utc)
    except ValueError:
        return False


def provider_from_model(model_name: str) -> str:
    name = model_name.lower()
    if "claude" in name:
        return "Anthropic"
    if "gemini" in name:
        return "Google"
    if "qwen" in name:
        return "Alibaba"
    if "deepseek" in name:
        return "DeepSeek"
    if "gpt" in name or "o1" in name or "o3" in name or "o4" in name:
        return "OpenAI"
    if "auto" in name or "router" in name:
        return "内部路由"
    return "其他"


# 模型广场展示用的厂商归类。匹配顺序刻意从具体厂商词根开始：内部把
# 第三方模型挂在 claude-*/gpt-* 兼容别名下（如 claude-code-glm-5.1 实际是
# 智谱 GLM），若先匹配 claude/gpt 会把它们全部错归为 Anthropic/OpenAI。
_MODEL_FAMILY_RULES: tuple[tuple[str, str, str], ...] = (
    ("glm", "zhipu", "智谱 GLM"),
    ("kimi", "moonshot", "月之暗面"),
    ("deepseek", "deepseek", "DeepSeek"),
    ("qwen", "qwen", "通义千问"),
    ("minimax", "minimax", "MiniMax"),
    ("gemini", "google", "Google"),
    ("bge", "baai", "BAAI"),
    ("claude", "anthropic", "Anthropic"),
    ("gpt", "openai", "OpenAI"),
    ("codex", "openai", "OpenAI"),
    ("image", "openai", "OpenAI"),
)


def model_family(model_name: str) -> tuple[str, str]:
    """返回模型所属厂商的 (图标标识, 中文展示名)。"""
    name = _clean_text(model_name).lower()
    for token, key, label in _MODEL_FAMILY_RULES:
        if token in name:
            return key, label
    return "other", "其他"


_CANONICAL_VENDOR_TOKENS = frozenset(
    {
        "anthropic",
        "openai",
        "google",
        "custom_openai",
        "bedrock",
        "azure",
        "vertex_ai",
        "dashscope",
    }
)
_CANONICAL_ALIAS_TOKENS = frozenset(
    {
        "wangsu",
        "wangsu5",
        "wangsu7",
        "zerokey",
        "zai",
        "local",
        "openrouter",
        "kuaihui",
        "chatgpt",
        "liuguoxian",
        "cheliantianxia1",
        "direct",
        "secondary",
    }
)
_CANONICAL_ACCOUNT_ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-acct-\d+-", re.IGNORECASE)
_CANONICAL_ZK_ALIAS_RE = re.compile(r"^zk-\d+-", re.IGNORECASE)
_CANONICAL_MODEL_HINT_RE = re.compile(
    r"^(?:gpt|o[134]|claude|gemini|qwen|deepseek|glm|kimi|moonshot|mistral|llama|"
    r"bge|text-embedding|codex|minimax|doubao|ernie)(?:[-.]|\d)",
    re.IGNORECASE,
)
_UNKNOWN_MODEL_NAMES_LOGGED: set[str] = set()


def _canonical_model_fallback(value: Any) -> tuple[str, bool]:
    """Strip only syntax that is explicitly known to be an internal alias."""

    text = _clean_text(value)
    if not text:
        return "", False
    path_segments = [segment.strip() for segment in text.split("/") if segment.strip()]
    body = path_segments[-1] if path_segments else text
    route_segments = path_segments[:-1]
    route_tokens = {
        token.casefold()
        for segment in route_segments
        for token in re.split(r"[.-]", segment)
        if token
    }
    confirmed_alias = bool(
        route_tokens.intersection(_CANONICAL_VENDOR_TOKENS | _CANONICAL_ALIAS_TOKENS)
    )
    if route_segments and _CANONICAL_MODEL_HINT_RE.match(body):
        confirmed_alias = True
    while True:
        candidate = _CANONICAL_ACCOUNT_ALIAS_RE.sub("", body, count=1)
        candidate = _CANONICAL_ZK_ALIAS_RE.sub("", candidate, count=1)
        if candidate == body:
            break
        body = candidate
        confirmed_alias = True
    while True:
        head, dot, rest = body.partition(".")
        if not dot or not rest or (
            head.casefold() not in _CANONICAL_VENDOR_TOKENS
            and head.casefold() not in _CANONICAL_ALIAS_TOKENS
        ):
            break
        body = rest
        confirmed_alias = True
    segments = body.split("-")
    if any(segment.casefold() in _CANONICAL_ALIAS_TOKENS for segment in segments):
        body = "-".join(
            segment
            for segment in segments
            if segment and segment.casefold() not in _CANONICAL_ALIAS_TOKENS and segment.casefold() != "pool"
        )
        confirmed_alias = True
    if len(path_segments) > 1 and not confirmed_alias and not _CANONICAL_MODEL_HINT_RE.match(body):
        return text, False
    return body or text, confirmed_alias


def _log_unknown_model_once(value: Any) -> None:
    text = _clean_text(value)
    key = text.casefold()
    if text and key not in _UNKNOWN_MODEL_NAMES_LOGGED:
        _UNKNOWN_MODEL_NAMES_LOGGED.add(key)
        logger.info("unrecognized model name kept unchanged: %s", text)


def resolve_canonical_model_name(
    value: Any,
    *,
    deployment_map: dict[str, str] | None = None,
    diagnose_unknown: bool = False,
) -> str:
    """Return the conservative canonical display name used by every report."""

    text = _clean_text(value)
    if not text:
        return ""
    if deployment_map:
        mapped = deployment_map.get(text.casefold())
        if mapped:
            return resolve_canonical_model_name(mapped, diagnose_unknown=diagnose_unknown)
    candidate, confirmed_alias = _canonical_model_fallback(text)
    recognized_model = bool(_CANONICAL_MODEL_HINT_RE.match(candidate))
    if diagnose_unknown and not confirmed_alias and not recognized_model:
        _log_unknown_model_once(text)
    return candidate.casefold() if recognized_model else candidate or text


def _is_recognizable_deployment_name(value: Any) -> bool:
    candidate, confirmed_alias = _canonical_model_fallback(value)
    return bool(candidate and (confirmed_alias or _CANONICAL_MODEL_HINT_RE.match(candidate)))


def is_internal_model_alias(model_name: str) -> bool:
    """Return whether the shared resolver recognized internal routing syntax."""

    text = _clean_text(model_name)
    if not text:
        return True
    _, confirmed_alias = _canonical_model_fallback(text)
    return confirmed_alias


# Keep the historical public helpers as thin delegates to the one resolver.
def normalize_model_display_name(value: Any) -> str:
    return resolve_canonical_model_name(value)


def model_display_name(model_name: str) -> str:
    return resolve_canonical_model_name(model_name)


class LiteLLMClient:
    def __init__(self) -> None:
        base_url = os.getenv("LITELLM_BASE_URL", "").strip().rstrip("/")
        admin_key = os.getenv("LITELLM_ADMIN_KEY", "").strip()
        if not base_url or not admin_key:
            raise RuntimeError("请先在 .env 中配置 LITELLM_BASE_URL 和 LITELLM_ADMIN_KEY")
        self.backends = [
            LiteLLMBackend(id="primary", label="通衢 API", base_url=base_url, admin_key=admin_key),
        ]
        her_base_url = os.getenv("HER_LITELLM_BASE_URL", "").strip().rstrip("/")
        her_admin_key = os.getenv("HER_LITELLM_ADMIN_KEY", "").strip()
        if her_base_url and her_admin_key:
            self.backends.append(
                LiteLLMBackend(id="her", label=os.getenv("HER_SOURCE_LABEL", "Her").strip() or "Her", base_url=her_base_url, admin_key=her_admin_key, source="Her")
            )
        self._backend_map = {backend.id: backend for backend in self.backends}
        self.base_url = base_url
        self.admin_key = admin_key
        self.timeout = httpx.Timeout(20.0, connect=8.0)
        self.http_client = httpx.AsyncClient(timeout=self.timeout)
        self._semaphore = asyncio.Semaphore(max(1, _env_int("LITELLM_MAX_CONCURRENCY", 4)))
        self._key_cache = TTLCache()
        self._key_cache_versions: dict[str, int] = {}
        self._key_list_inflight: dict[tuple[str, int], asyncio.Task[list[dict[str, Any]]]] = {}
        self._model_cache = TTLCache()
        self._deployment_model_maps: dict[str, dict[str, str]] = {}
        self._model_usage_cache = TTLCache()
        self._account_index_cache = TTLCache()
        self._team_details_cache = TTLCache()
        # LiteLLM 1.92 accepts an Idempotency-Key header but does not consume
        # it on /key/generate. Serialize creates by stable alias in this
        # process; PostgreSQL's unique alias projection covers other workers.
        self._organization_key_create_locks: dict[str, asyncio.Lock] = {}
        self._organization_key_create_locks_guard = asyncio.Lock()

    async def close(self) -> None:
        # TestClient instances may create and tear down separate event loops
        # while sharing the module-level client. httpx can then hold an idle
        # connection owned by an already-closed loop. Closing that connection
        # raises RuntimeError, but the client still needs to be marked closed
        # so the next application lifespan can replace it safely.
        try:
            await self.http_client.aclose()
        except RuntimeError as exc:
            if "event loop is closed" not in str(exc).lower():
                raise

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        return await self.request_backend(self.backends[0], method, path, **kwargs)

    async def create_internal_user(
        self,
        user_id: str,
        email: str | None,
        name: str | None = None,
        *,
        metadata: dict[str, Any] | None = None,
        backend: LiteLLMBackend | None = None,
    ) -> dict[str, Any]:
        """Create a local account's primary LiteLLM user without issuing a key.

        The dashboard owns authentication and entitlements, so newly provisioned
        upstream users are deliberately restricted to ``no-default-models`` and
        receive no default model access. A zero user budget is deliberately not
        set because LiteLLM treats it as already exhausted. Keeping the stable
        local id in the request makes retries safe to reconcile by the caller.
        """
        backend = backend or self.backends[0]
        local_id = str(user_id).strip()
        normalized_email = str(email or "").strip().lower()
        if not local_id:
            raise HTTPException(status_code=400, detail="开户参数不完整")
        if normalized_email:
            if len(normalized_email) > 254 or normalized_email.count("@") != 1:
                raise HTTPException(status_code=400, detail="开户邮箱格式无效")
            local_part, domain = normalized_email.split("@", 1)
            if (
                not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", local_part)
                or len(local_part) > 64
                or local_part.startswith(".")
                or local_part.endswith(".")
                or ".." in local_part
            ):
                raise HTTPException(status_code=400, detail="开户邮箱格式无效")
            try:
                domain = domain.encode("idna").decode("ascii")
            except UnicodeError as exc:
                raise HTTPException(status_code=400, detail="开户邮箱格式无效") from exc
            if len(domain) > 253 or not all(
                label
                and len(label) <= 63
                and not label.startswith("-")
                and not label.endswith("-")
                and re.fullmatch(r"[a-z0-9-]+", label)
                for label in domain.split(".")
            ):
                raise HTTPException(status_code=400, detail="开户邮箱格式无效")
            normalized_email = f"{local_part}@{domain}"
        configured_role = os.getenv("AUTH_DEFAULT_UPSTREAM_ROLE", "internal_user_viewer").strip()
        user_role = configured_role if configured_role in LOCAL_AUTH_UPSTREAM_ROLES else "internal_user_viewer"
        if configured_role and configured_role != user_role:
            logger.warning("ignoring unsafe AUTH_DEFAULT_UPSTREAM_ROLE value")
        payload: dict[str, Any] = {
            "user_id": local_id,
            "user_alias": str(name or normalized_email or local_id).strip() or local_id,
            "user_role": user_role,
            "auto_create_key": False,
            "models": [NO_DEFAULT_MODELS],
            "metadata": {
                "created_via": "ai-token-dashboard",
                "local_user_id": local_id,
                **(metadata or {}),
            },
        }
        # LiteLLM supports stable user_id-only enterprise users. Username
        # accounts deliberately omit user_email instead of inventing an
        # unreachable mailbox that could later be mistaken for identity proof.
        if normalized_email:
            payload["user_email"] = normalized_email
        response = await self.request_backend(backend, "POST", "/user/new", json=payload)
        if isinstance(response, dict):
            return response
        fallback = {"user_id": local_id}
        if normalized_email:
            fallback["user_email"] = normalized_email
        return fallback

    async def user_info(
        self,
        user_id: str,
        *,
        backend: LiteLLMBackend | None = None,
    ) -> dict[str, Any]:
        """Return one upstream user, primarily for provisioning reconciliation."""
        backend = backend or self.backends[0]
        try:
            payload = await self.request_backend(backend, "GET", "/v2/user/info", params={"user_id": user_id})
        except HTTPException as exc:
            if exc.status_code not in {404, 405, 501}:
                raise
            payload = await self.request_backend(backend, "GET", "/user/info", params={"user_id": user_id})
        if isinstance(payload, dict):
            for key in ("user", "user_info"):
                data = payload.get(key)
                if isinstance(data, dict):
                    return data
            return payload
        return {}

    @staticmethod
    def _management_headers(changed_by: str | None = None, idempotency_key: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if changed_by:
            headers["litellm-changed-by"] = str(changed_by).strip()
        if idempotency_key:
            headers["Idempotency-Key"] = str(idempotency_key).strip()
        return {key: value for key, value in headers.items() if value}

    async def organization_capabilities(
        self,
        *,
        backend: LiteLLMBackend | None = None,
    ) -> dict[str, Any]:
        """Probe the database-backed organization and team management surface."""
        backend = backend or self.backends[0]
        checks: dict[str, bool] = {"organizations": False, "teams": False, "keys": False}
        errors: dict[str, dict[str, Any]] = {}
        probes = (
            ("organizations", (("/organization/list", {"org_alias": ""}),)),
            (
                "teams",
                (
                    ("/v2/team/list", {"page": 1, "page_size": 1}),
                    ("/team/list", {}),
                ),
            ),
            ("keys", (("/key/list", {"page": 1, "size": 1, "return_full_object": "false"}),)),
        )
        for name, candidates in probes:
            last_error: HTTPException | None = None
            for path, params in candidates:
                try:
                    await self.request_backend(backend, "GET", path, params=params)
                    checks[name] = True
                    break
                except HTTPException as exc:
                    last_error = exc
                    if exc.status_code not in {404, 405, 501}:
                        break
            if checks[name]:
                continue
            if last_error is not None:
                errors[name] = {
                    "statusCode": last_error.status_code,
                    "detail": last_error.detail,
                }
        return {
            "available": all(checks.values()),
            **checks,
            "backend": backend.id,
            "errors": errors,
        }

    async def create_organization(
        self,
        organization_alias: str,
        *,
        organization_id: str | None = None,
        models: list[str] | None = None,
        max_budget: float | None = None,
        budget_duration: str | None = None,
        blocked: bool = False,
        metadata: dict[str, Any] | None = None,
        changed_by: str | None = None,
        backend: LiteLLMBackend | None = None,
    ) -> dict[str, Any]:
        backend = backend or self.backends[0]
        body: dict[str, Any] = {
            "organization_alias": _clean_text(organization_alias),
            "models": self._clean_model_list(models or []),
        }
        # LiteLLM 1.92's organization table has no ``blocked`` column.  The
        # management endpoint accepts extra JSON at validation time, but then
        # forwards it to Prisma and fails on the unknown field.  Organization
        # suspension is enforced locally (and by revoking its keys); keep the
        # argument for callers that share the team/update interface, but do
        # not send an unsupported organization field upstream.
        for key, value in (
            ("organization_id", _clean_text(organization_id)),
            ("max_budget", max_budget),
            ("budget_duration", _clean_text(budget_duration)),
            ("metadata", metadata),
        ):
            if value not in (None, ""):
                body[key] = value
        payload = await self.request_backend(
            backend,
            "POST",
            "/organization/new",
            headers=self._management_headers(changed_by),
            json=body,
        )
        return payload if isinstance(payload, dict) else {}

    async def update_organization(
        self,
        organization_id: str,
        *,
        organization_alias: str | None = None,
        models: list[str] | None = None,
        max_budget: float | None = None,
        budget_duration: str | None = None,
        blocked: bool | None = None,
        metadata: dict[str, Any] | None = None,
        changed_by: str | None = None,
        backend: LiteLLMBackend | None = None,
    ) -> dict[str, Any]:
        backend = backend or self.backends[0]
        body: dict[str, Any] = {"organization_id": _clean_text(organization_id)}
        optional: tuple[tuple[str, Any], ...] = (
            ("organization_alias", _clean_text(organization_alias)),
            ("models", self._clean_model_list(models) if models is not None else None),
            ("max_budget", max_budget),
            ("budget_duration", _clean_text(budget_duration)),
            ("metadata", metadata),
        )
        body.update({key: value for key, value in optional if value not in (None, "")})
        payload = await self.request_backend(
            backend,
            "PATCH",
            "/organization/update",
            headers=self._management_headers(changed_by),
            json=body,
        )
        return payload if isinstance(payload, dict) else {}

    async def list_organizations(
        self,
        *,
        organization_id: str | None = None,
        organization_alias: str | None = None,
        backend: LiteLLMBackend | None = None,
    ) -> list[dict[str, Any]]:
        backend = backend or self.backends[0]
        params: dict[str, Any] = {"page": 1, "page_size": 100}
        if organization_id:
            params["org_id"] = organization_id
        if organization_alias:
            params["org_alias"] = organization_alias
        payload = await self.request_backend(backend, "GET", "/organization/list", params=params)
        return _records(payload)

    async def find_organizations_exact(
        self,
        *,
        organization_id: str | None = None,
        organization_alias: str | None = None,
        backend: LiteLLMBackend | None = None,
    ) -> list[dict[str, Any]]:
        """Return exact Organization matches from LiteLLM's partial alias API."""

        expected_id = _clean_text(organization_id)
        expected_alias = _clean_text(organization_alias).casefold()
        if not expected_id and not expected_alias:
            raise ValueError("organization id or alias is required")
        records = await self.list_organizations(
            organization_id=expected_id or None,
            organization_alias=_clean_text(organization_alias) or None,
            backend=backend,
        )
        # Some 1.90 deployments expose only alias filtering. If a configured
        # stable id was supplied, also issue the id-shaped query and let the
        # exact equality guard below decide the result.
        if expected_id and not records:
            records = await self.list_organizations(
                organization_id=expected_id,
                organization_alias=None,
                backend=backend,
            )
        matches: list[dict[str, Any]] = []
        for record in records:
            candidate_id = _clean_text(
                _first(record, "organization_id", "organizationId", "id", default="")
            )
            candidate_alias = _clean_text(
                _first(record, "organization_alias", "organizationAlias", "alias", "name", default="")
            ).casefold()
            if expected_id and candidate_id != expected_id:
                continue
            if expected_alias and candidate_alias != expected_alias:
                continue
            matches.append(record)
        return matches

    async def organization_info(
        self,
        organization_id: str,
        *,
        backend: LiteLLMBackend | None = None,
    ) -> dict[str, Any]:
        backend = backend or self.backends[0]
        payload = await self.request_backend(
            backend,
            "GET",
            "/organization/info",
            params={"organization_id": organization_id},
        )
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _organization_member_role(role: str) -> str:
        normalized = _clean_text(role).lower()
        aliases = {
            "admin": "org_admin",
            "enterprise_admin": "org_admin",
            "member": "internal_user",
            "user": "internal_user",
            "viewer": "internal_user_viewer",
        }
        value = aliases.get(normalized, normalized)
        if value not in {"org_admin", "internal_user", "internal_user_viewer"}:
            raise HTTPException(status_code=400, detail="无效的企业成员角色")
        return value

    @staticmethod
    def _member_identity(user_id: str | None, user_email: str | None) -> dict[str, str]:
        if _clean_text(user_id):
            return {"user_id": _clean_text(user_id)}
        if _clean_text(user_email):
            return {"user_email": _clean_text(user_email).lower()}
        raise HTTPException(status_code=400, detail="成员必须包含用户编号或邮箱")

    async def add_organization_member(
        self,
        organization_id: str,
        role: str,
        *,
        user_id: str | None = None,
        user_email: str | None = None,
        max_budget: float | None = None,
        changed_by: str | None = None,
        backend: LiteLLMBackend | None = None,
    ) -> dict[str, Any]:
        backend = backend or self.backends[0]
        member = {**self._member_identity(user_id, user_email), "role": self._organization_member_role(role)}
        body: dict[str, Any] = {"organization_id": organization_id, "member": member}
        if max_budget is not None:
            body["max_budget_in_organization"] = max_budget
        payload = await self.request_backend(
            backend,
            "POST",
            "/organization/member_add",
            headers=self._management_headers(changed_by),
            json=body,
        )
        return payload if isinstance(payload, dict) else {}

    async def update_organization_member(
        self,
        organization_id: str,
        *,
        user_id: str | None = None,
        user_email: str | None = None,
        role: str | None = None,
        max_budget: float | None = None,
        changed_by: str | None = None,
        backend: LiteLLMBackend | None = None,
    ) -> dict[str, Any]:
        backend = backend or self.backends[0]
        body: dict[str, Any] = {"organization_id": organization_id, **self._member_identity(user_id, user_email)}
        if role is not None:
            body["role"] = self._organization_member_role(role)
        if max_budget is not None:
            body["max_budget_in_organization"] = max_budget
        payload = await self.request_backend(
            backend,
            "PATCH",
            "/organization/member_update",
            headers=self._management_headers(changed_by),
            json=body,
        )
        return payload if isinstance(payload, dict) else {}

    async def delete_organization_member(
        self,
        organization_id: str,
        *,
        user_id: str | None = None,
        user_email: str | None = None,
        changed_by: str | None = None,
        backend: LiteLLMBackend | None = None,
    ) -> dict[str, Any]:
        backend = backend or self.backends[0]
        body = {"organization_id": organization_id, **self._member_identity(user_id, user_email)}
        payload = await self.request_backend(
            backend,
            "DELETE",
            "/organization/member_delete",
            headers=self._management_headers(changed_by),
            json=body,
        )
        return payload if isinstance(payload, dict) else {}

    async def create_team(
        self,
        team_alias: str,
        organization_id: str,
        *,
        team_id: str | None = None,
        models: list[str] | None = None,
        max_budget: float | None = None,
        budget_duration: str | None = None,
        blocked: bool = False,
        metadata: dict[str, Any] | None = None,
        changed_by: str | None = None,
        backend: LiteLLMBackend | None = None,
    ) -> dict[str, Any]:
        backend = backend or self.backends[0]
        body: dict[str, Any] = {
            "team_alias": _clean_text(team_alias),
            "organization_id": organization_id,
            "models": self._clean_model_list(models or []),
            "blocked": bool(blocked),
        }
        for key, value in (
            ("team_id", _clean_text(team_id)),
            ("max_budget", max_budget),
            ("budget_duration", _clean_text(budget_duration)),
            ("metadata", metadata),
        ):
            if value not in (None, ""):
                body[key] = value
        payload = await self.request_backend(
            backend,
            "POST",
            "/team/new",
            headers=self._management_headers(changed_by),
            json=body,
        )
        return payload if isinstance(payload, dict) else {}

    async def list_teams(
        self,
        *,
        organization_id: str | None = None,
        team_id: str | None = None,
        team_alias: str | None = None,
        backend: LiteLLMBackend | None = None,
    ) -> list[dict[str, Any]]:
        """List database-backed teams for provisioning reconciliation."""

        backend = backend or self.backends[0]
        page = 1
        records: list[dict[str, Any]] = []
        while True:
            params: dict[str, Any] = {"page": page, "page_size": 100}
            if organization_id:
                params["organization_id"] = _clean_text(organization_id)
            if team_id:
                params["team_id"] = _clean_text(team_id)
            if team_alias:
                params["team_alias"] = _clean_text(team_alias)
            try:
                payload = await self.request_backend(
                    backend, "GET", "/v2/team/list", params=params
                )
            except HTTPException as exc:
                if page != 1 or exc.status_code not in {404, 405, 501}:
                    raise
                legacy_params: dict[str, Any] = {}
                if organization_id:
                    legacy_params["organization_id"] = _clean_text(organization_id)
                payload = await self.request_backend(
                    backend, "GET", "/team/list", params=legacy_params
                )
                legacy_records = _records(payload)
                if team_id:
                    legacy_records = [
                        item
                        for item in legacy_records
                        if _clean_text(_first(item, "team_id", "teamId", "id"))
                        == _clean_text(team_id)
                    ]
                if team_alias:
                    legacy_records = [
                        item
                        for item in legacy_records
                        if _clean_text(_first(item, "team_alias", "teamAlias", "name"))
                        == _clean_text(team_alias)
                    ]
                return legacy_records
            batch = _records(payload)
            records.extend(batch)
            total_pages = _as_int(
                _first(payload, "total_pages", "totalPages", default=1)
            ) if isinstance(payload, dict) else 1
            if page >= max(1, total_pages) or not batch:
                return records
            page += 1

    async def find_teams_exact(
        self,
        *,
        organization_id: str | None = None,
        team_id: str | None = None,
        team_alias: str | None = None,
        backend: LiteLLMBackend | None = None,
    ) -> list[dict[str, Any]]:
        """Return exact Team matches and preserve upstream Organization scope."""

        expected_org = _clean_text(organization_id)
        expected_id = _clean_text(team_id)
        expected_alias = _clean_text(team_alias).casefold()
        if not expected_id and not expected_alias:
            raise ValueError("team id or alias is required")
        records = await self.list_teams(
            organization_id=expected_org or None,
            team_id=expected_id or None,
            team_alias=_clean_text(team_alias) or None,
            backend=backend,
        )
        matches: list[dict[str, Any]] = []
        for record in records:
            candidate_id = _clean_text(_first(record, "team_id", "teamId", "id", default=""))
            candidate_alias = _clean_text(
                _first(record, "team_alias", "teamAlias", "alias", "name", default="")
            ).casefold()
            candidate_org = _clean_text(
                _first(record, "organization_id", "organizationId", "org_id", default="")
            )
            if expected_id and candidate_id != expected_id:
                continue
            if expected_alias and candidate_alias != expected_alias:
                continue
            if expected_org and candidate_org != expected_org:
                continue
            matches.append(record)
        return matches

    async def update_team(
        self,
        team_id: str,
        *,
        team_alias: str | None = None,
        organization_id: str | None = None,
        models: list[str] | None = None,
        max_budget: float | None = None,
        budget_duration: str | None = None,
        blocked: bool | None = None,
        metadata: dict[str, Any] | None = None,
        changed_by: str | None = None,
        backend: LiteLLMBackend | None = None,
    ) -> dict[str, Any]:
        backend = backend or self.backends[0]
        body: dict[str, Any] = {"team_id": team_id}
        optional: tuple[tuple[str, Any], ...] = (
            ("team_alias", _clean_text(team_alias)),
            ("organization_id", _clean_text(organization_id)),
            ("models", self._clean_model_list(models) if models is not None else None),
            ("max_budget", max_budget),
            ("budget_duration", _clean_text(budget_duration)),
            ("blocked", blocked),
            ("metadata", metadata),
        )
        body.update({key: value for key, value in optional if value not in (None, "")})
        payload = await self.request_backend(
            backend,
            "POST",
            "/team/update",
            headers=self._management_headers(changed_by),
            json=body,
        )
        if hasattr(self, "_team_details_cache"):
            self._team_details_cache.delete(f"{backend.id}:{team_id}")
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _management_team_member_role(role: str) -> str:
        normalized = _clean_text(role).lower()
        aliases = {"member": "user", "enterprise_admin": "admin"}
        value = aliases.get(normalized, normalized)
        if value not in {"admin", "user"}:
            raise HTTPException(status_code=400, detail="无效的部门成员角色")
        return value

    async def add_team_member(
        self,
        team_id: str,
        role: str,
        *,
        user_id: str | None = None,
        user_email: str | None = None,
        max_budget: float | None = None,
        budget_duration: str | None = None,
        changed_by: str | None = None,
        backend: LiteLLMBackend | None = None,
    ) -> dict[str, Any]:
        backend = backend or self.backends[0]
        member = {**self._member_identity(user_id, user_email), "role": self._management_team_member_role(role)}
        body: dict[str, Any] = {"team_id": team_id, "member": member}
        if max_budget is not None:
            body["max_budget_in_team"] = max_budget
        if budget_duration:
            body["budget_duration"] = budget_duration
        payload = await self.request_backend(
            backend,
            "POST",
            "/team/member_add",
            headers=self._management_headers(changed_by),
            json=body,
        )
        self._team_details_cache.delete(f"{backend.id}:{team_id}")
        return payload if isinstance(payload, dict) else {}

    async def update_team_member(
        self,
        team_id: str,
        *,
        user_id: str | None = None,
        user_email: str | None = None,
        role: str | None = None,
        max_budget: float | None = None,
        budget_duration: str | None = None,
        changed_by: str | None = None,
        backend: LiteLLMBackend | None = None,
    ) -> dict[str, Any]:
        backend = backend or self.backends[0]
        body: dict[str, Any] = {"team_id": team_id, **self._member_identity(user_id, user_email)}
        if role is not None:
            body["role"] = self._management_team_member_role(role)
        if max_budget is not None:
            body["max_budget_in_team"] = max_budget
        if budget_duration:
            body["budget_duration"] = budget_duration
        payload = await self.request_backend(
            backend,
            "POST",
            "/team/member_update",
            headers=self._management_headers(changed_by),
            json=body,
        )
        self._team_details_cache.delete(f"{backend.id}:{team_id}")
        return payload if isinstance(payload, dict) else {}

    async def delete_team_member(
        self,
        team_id: str,
        *,
        user_id: str | None = None,
        user_email: str | None = None,
        changed_by: str | None = None,
        backend: LiteLLMBackend | None = None,
    ) -> dict[str, Any]:
        backend = backend or self.backends[0]
        body = {"team_id": team_id, **self._member_identity(user_id, user_email)}
        payload = await self.request_backend(
            backend,
            "POST",
            "/team/member_delete",
            headers=self._management_headers(changed_by),
            json=body,
        )
        self._team_details_cache.delete(f"{backend.id}:{team_id}")
        return payload if isinstance(payload, dict) else {}

    async def organization_daily_usage(
        self,
        organization_id: str,
        start_date: str,
        end_date: str,
        *,
        model: str | None = None,
        api_key: str | None = None,
        page: int = 1,
        page_size: int = 1000,
        backend: LiteLLMBackend | None = None,
    ) -> dict[str, Any]:
        """Return LiteLLM's persisted daily organization spend rows."""
        backend = backend or self.backends[0]
        params: dict[str, Any] = {
            "organization_ids": organization_id,
            "start_date": start_date,
            "end_date": end_date,
            "page": page,
            "page_size": page_size,
        }
        if model:
            params["model"] = model
        if api_key:
            params["api_key"] = api_key
        payload = await self.request_backend(backend, "GET", "/organization/daily/activity", params=params)
        return payload if isinstance(payload, dict) else {}

    async def team_daily_usage(
        self,
        team_id: str,
        start_date: str,
        end_date: str,
        *,
        model: str | None = None,
        api_key: str | None = None,
        page: int = 1,
        page_size: int = 1000,
        backend: LiteLLMBackend | None = None,
    ) -> dict[str, Any]:
        """Return LiteLLM's persisted daily team spend rows."""
        backend = backend or self.backends[0]
        params: dict[str, Any] = {
            "team_ids": team_id,
            "start_date": start_date,
            "end_date": end_date,
            "page": page,
            "page_size": page_size,
        }
        if model:
            params["model"] = model
        if api_key:
            params["api_key"] = api_key
        payload = await self.request_backend(backend, "GET", "/team/daily/activity", params=params)
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def daily_usage_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Normalize the daily activity response while retaining breakdown details."""
        return _records(payload.get("results") if isinstance(payload, dict) else payload)

    async def create_organization_key(
        self,
        organization_id: str,
        *,
        key_alias: str,
        models: list[str],
        daily_budget_usd: float | None = None,
        team_id: str | None = None,
        user_id: str | None = None,
        duration: str | None = None,
        metadata: dict[str, Any] | None = None,
        changed_by: str | None = None,
        idempotency_key: str | None = None,
        backend: LiteLLMBackend | None = None,
    ) -> dict[str, Any]:
        """Create a real upstream key scoped to an organization/team/member."""
        backend = backend or self.backends[0]
        clean_alias = _clean_text(key_alias)
        if not clean_alias:
            raise HTTPException(status_code=400, detail="企业 Token 名称不能为空")
        selected_models = self._clean_model_list(models)
        if not selected_models:
            raise HTTPException(status_code=400, detail="企业 Token 至少需要一个模型权限")
        body: dict[str, Any] = {
            "key_alias": clean_alias,
            "key_type": "llm_api",
            "organization_id": organization_id,
            "models": selected_models,
            "metadata": {"created_via": "ai-usage-center", **(metadata or {})},
        }
        if team_id:
            body["team_id"] = team_id
        if user_id:
            body["user_id"] = user_id
        if daily_budget_usd is not None:
            body["max_budget"] = daily_budget_usd
            body["budget_duration"] = "1d"
        if duration and duration != "never":
            body["duration"] = duration
        lock_key = f"{backend.id}:{organization_id}:{clean_alias}"
        async with self._organization_key_create_locks_guard:
            create_lock = self._organization_key_create_locks.setdefault(
                lock_key, asyncio.Lock()
            )
        try:
            async with create_lock:
                # Recover an earlier success before creating again. This is
                # required because LiteLLM 1.92 does not consume the
                # Idempotency-Key header on /key/generate.
                existing = await self.find_organization_key_by_alias(
                    organization_id,
                    clean_alias,
                    team_id=team_id,
                    user_id=user_id,
                    backend=backend,
                )
                if existing is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="同一企业 Token 请求已处理，请刷新列表确认结果",
                    )
                payload = await self.request_backend(
                    backend,
                    "POST",
                    "/key/generate",
                    headers=self._management_headers(changed_by, idempotency_key),
                    json=body,
                )
        finally:
            if not create_lock.locked():
                async with self._organization_key_create_locks_guard:
                    if self._organization_key_create_locks.get(lock_key) is create_lock:
                        self._organization_key_create_locks.pop(lock_key, None)
        if not isinstance(payload, dict):
            raise HTTPException(status_code=502, detail="上游未返回企业 Token")
        token = _clean_text(_first(payload, "key", "token", default=""))
        if not token.startswith("sk-"):
            raise HTTPException(status_code=502, detail="上游未返回有效企业 Token")
        token_id = _clean_text(_first(payload, "token_id", "token_hash", default=""))
        if not token_id or token_id.startswith("sk-"):
            token_id = safe_key_id(token)
        expires = _first(payload, "expires", default=None)
        return {
            "key": token,
            "id": token_id,
            "masked": mask_key(token),
            "organizationId": _clean_text(_first(payload, "organization_id", default=organization_id)) or organization_id,
            "teamId": _clean_text(_first(payload, "team_id", default=team_id)),
            "userId": _clean_text(_first(payload, "user_id", default=user_id)),
            "expiresAt": _datetime_text(expires) if expires else "永久有效",
            "upstream": payload,
        }

    async def list_organization_keys(
        self,
        organization_id: str,
        *,
        key_alias: str | None = None,
        team_id: str | None = None,
        user_id: str | None = None,
        include_full_object: bool = True,
        backend: LiteLLMBackend | None = None,
    ) -> list[dict[str, Any]]:
        backend = backend or self.backends[0]
        page = 1
        records: list[dict[str, Any]] = []
        while True:
            params: dict[str, Any] = {
                "organization_id": organization_id,
                "page": page,
                "size": 100,
                "return_full_object": str(bool(include_full_object)).lower(),
            }
            if team_id:
                params["team_id"] = team_id
            if user_id:
                params["user_id"] = user_id
            if key_alias:
                params["key_alias"] = _clean_text(key_alias)
            payload = await self.request_backend(
                backend, "GET", "/key/list", params=params
            )
            batch = _records(payload)
            records.extend(batch)
            total_pages = _as_int(
                _first(payload, "total_pages", "totalPages", default=1)
            ) if isinstance(payload, dict) else 1
            if page >= max(1, total_pages) or not batch:
                return records
            page += 1

    async def list_keys_exact(
        self,
        *,
        key_alias: str | None = None,
        key_hash: str | None = None,
        backend: LiteLLMBackend | None = None,
    ) -> list[dict[str, Any]]:
        """Read exact global key matches without changing an upstream key."""

        alias = _clean_text(key_alias)
        hashed = safe_key_id(key_hash)
        if bool(alias) == bool(hashed):
            raise ValueError("exactly one of key_alias or key_hash is required")
        backend = backend or self.backends[0]
        page = 1
        records: list[dict[str, Any]] = []
        while True:
            params: dict[str, Any] = {
                "page": page,
                "size": 100,
                "return_full_object": "true",
            }
            if alias:
                params["key_alias"] = alias
            else:
                params["key_hash"] = hashed
            payload = await self.request_backend(backend, "GET", "/key/list", params=params)
            batch = _records(payload)
            for record in batch:
                identity = self.report_only_key_identity(record)
                if alias and identity["alias"] != alias:
                    continue
                if hashed and hashed not in {identity["id"], identity["hash"]}:
                    continue
                records.append(record)
            total_pages = _as_int(
                _first(payload, "total_pages", "totalPages", default=1)
            ) if isinstance(payload, dict) else 1
            if page >= max(1, total_pages) or not batch:
                return records
            page += 1

    @staticmethod
    def report_only_key_identity(record: dict[str, Any]) -> dict[str, Any]:
        """Normalize a persisted key row without accepting cleartext credentials.

        LiteLLM 1.90/1.92 returns the SHA-256 token in ``token`` from
        ``/key/list``.  An unexpected ``sk-*`` value is rejected instead of
        being hashed locally, logged, or passed into the adoption workflow.
        """

        candidates = (
            _first(record, "key_hash", "keyHash", "token_hash", "tokenHash", default=""),
            _first(record, "token", default=""),
            _first(record, "token_id", "tokenId", "key_id", "keyId", default=""),
        )
        normalized = [_clean_text(value) for value in candidates if _clean_text(value)]
        if any(value.startswith("sk-") for value in normalized):
            raise HTTPException(status_code=502, detail="上游密钥列表返回了不安全的凭据格式")
        key_hash = next((value.lower() for value in normalized if re.fullmatch(r"[0-9a-fA-F]{64}", value)), "")
        if not key_hash:
            raise HTTPException(status_code=502, detail="上游密钥缺少稳定哈希标识")
        key_id = _clean_text(
            _first(record, "token_id", "tokenId", "key_id", "keyId", default="")
        ) or key_hash
        models = _first(record, "models", "allowed_models", "allowedModels", default=[])
        if not isinstance(models, list):
            models = []
        max_budget = _first(record, "max_budget", "maxBudget", default=None)
        spend = _first(record, "spend", "total_spend", "totalSpend", default=None)
        expires = _first(
            record,
            "expires",
            "expires_at",
            "expiresAt",
            "expiration",
            default=None,
        )
        created_at = _first(record, "created_at", "createdAt", default=None)
        return {
            "id": key_id,
            "hash": key_hash,
            "alias": _clean_text(_first(record, "key_alias", "keyAlias", "alias", default="")),
            "organizationId": _clean_text(
                _first(record, "organization_id", "organizationId", "org_id", default="")
            ),
            "teamId": _clean_text(_first(record, "team_id", "teamId", default="")),
            "userId": _clean_text(_first(record, "user_id", "userId", default="")),
            "models": [_clean_text(model) for model in models if _clean_text(model)],
            "maxBudget": max_budget,
            "budgetDuration": _clean_text(
                _first(record, "budget_duration", "budgetDuration", default="")
            ),
            "spend": spend,
            "createdAt": _clean_text(created_at),
            "expiresAt": _clean_text(expires),
            "blocked": bool(_first(record, "blocked", "disabled", default=False)),
        }

    async def find_organization_key_by_alias(
        self,
        organization_id: str,
        key_alias: str,
        *,
        team_id: str | None = None,
        user_id: str | None = None,
        backend: LiteLLMBackend | None = None,
    ) -> dict[str, Any] | None:
        """Return an exact alias match for compensating a timed-out key create.

        LiteLLM's list endpoint supports exact ``key_alias`` filtering.  Keep
        the final equality check here as a guard against older proxy versions
        that interpret the query as a substring search.
        """

        alias = _clean_text(key_alias)
        if not alias:
            return None
        records = await self.list_organization_keys(
            organization_id,
            key_alias=alias,
            team_id=team_id,
            user_id=user_id,
            include_full_object=True,
            backend=backend,
        )
        expected_organization = _clean_text(organization_id)
        expected_team = _clean_text(team_id)
        expected_user = _clean_text(user_id)
        for record in records:
            candidate_alias = _clean_text(
                _first(record, "key_alias", "keyAlias", "alias", default="")
            )
            if candidate_alias != alias:
                continue
            candidate_org = _clean_text(
                _first(record, "organization_id", "organizationId", "org_id", default="")
            )
            if candidate_org != expected_organization:
                continue
            candidate_team = _clean_text(
                _first(record, "team_id", "teamId", default="")
            )
            if candidate_team != expected_team:
                continue
            candidate_user = _clean_text(
                _first(record, "user_id", "userId", default="")
            )
            if candidate_user != expected_user:
                continue
            return record
        return None

    @staticmethod
    def organization_key_identity(record: dict[str, Any]) -> dict[str, str]:
        """Normalize an upstream key object for local compensation logic."""

        raw_token = _clean_text(_first(record, "key", "token", default=""))
        token = raw_token if raw_token.startswith("sk-") else ""
        key_id = _clean_text(
            _first(record, "token_id", "tokenId", "key_id", "keyId", "token_hash", default="")
        )
        key_hash = _clean_text(
            _first(record, "key_hash", "keyHash", "token_hash", "tokenHash", default="")
        )
        if not key_hash and raw_token and not raw_token.startswith("sk-"):
            key_hash = raw_token
        # LiteLLM's list endpoint normally exposes the persisted SHA-256 hash
        # as ``token`` and may omit token_id. The hash is the delete/update
        # identifier accepted by the proxy; do not derive a different value.
        if not key_id and key_hash:
            key_id = key_hash
        if not key_id and raw_token:
            key_id = safe_key_id(raw_token)
        return {
            "id": key_id,
            "hash": key_hash,
            "token": token,
            "alias": _clean_text(_first(record, "key_alias", "keyAlias", "alias", default="")),
            "organizationId": _clean_text(
                _first(record, "organization_id", "organizationId", "org_id", default="")
            ),
            "teamId": _clean_text(_first(record, "team_id", "teamId", default="")),
            "userId": _clean_text(_first(record, "user_id", "userId", default="")),
        }

    async def revoke_organization_key(
        self,
        key_id: str,
        *,
        changed_by: str | None = None,
        idempotency_key: str | None = None,
        backend: LiteLLMBackend | None = None,
    ) -> dict[str, Any]:
        backend = backend or self.backends[0]
        try:
            payload = await self.request_backend(
                backend,
                "POST",
                "/key/delete",
                headers=self._management_headers(changed_by, idempotency_key),
                json={"keys": [key_id]},
            )
        except HTTPException as exc:
            # A retried durable job may find that an earlier attempt already
            # removed the known upstream key. Manual deletes stay fail-closed.
            if idempotency_key and str(key_id).strip() and exc.status_code == 404:
                return {"id": str(key_id), "deleted": True, "alreadyAbsent": True}
            raise
        deleted = payload.get("deleted_keys") if isinstance(payload, dict) else None
        if not self._delete_confirmed(deleted, key_id):
            raise HTTPException(status_code=502, detail="上游未确认企业 Token 已撤销")
        return {"id": key_id, "deleted": True}

    async def update_organization_key_budget(
        self,
        key_id: str,
        daily_budget_usd: float,
        *,
        changed_by: str | None = None,
        backend: LiteLLMBackend | None = None,
    ) -> dict[str, Any]:
        backend = backend or self.backends[0]
        payload = await self.request_backend(
            backend,
            "POST",
            "/key/update",
            headers=self._management_headers(changed_by),
            json={"key": key_id, "max_budget": daily_budget_usd, "budget_duration": "1d"},
        )
        return payload if isinstance(payload, dict) else {"id": key_id, "max_budget": daily_budget_usd, "budget_duration": "1d"}

    async def request_backend(self, backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {backend.admin_key}"
        headers.setdefault("Accept", "application/json")
        url = f"{backend.base_url}{path}"
        started = time.perf_counter()
        try:
            async with self._semaphore:
                response = await self.http_client.request(method, url, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise HTTPException(status_code=504, detail="上游服务响应超时，请稍后重试") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"无法连接 {backend.label}：{exc}") from exc
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000)
            if duration_ms >= _env_int("LITELLM_SLOW_REQUEST_MS", 800):
                logger.info("litellm request %s %s %s took %sms", backend.id, method, path, duration_ms)

        if response.status_code >= 400:
            detail = self._error_detail(response)
            raise HTTPException(status_code=response.status_code, detail=detail)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise HTTPException(status_code=502, detail="上游服务返回了无法解析的数据") from exc

    def _error_detail(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
            detail = payload.get("detail") or payload.get("error") or payload.get("message")
            if isinstance(detail, dict):
                detail = detail.get("error") or detail.get("message")
            if detail:
                return f"上游接口失败：{detail}"
        except ValueError:
            pass
        if response.status_code in {401, 403}:
            return "管理员密钥无权限或已失效"
        if response.status_code == 404:
            return "上游接口不存在或资源未找到"
        return f"上游接口失败：HTTP {response.status_code}"

    def _encode_account_id(self, backend: LiteLLMBackend, user_id: str) -> str:
        return user_id if backend.id == "primary" else f"{backend.id}:{user_id}"

    def _decode_account_id(self, account_id: str) -> tuple[LiteLLMBackend, str]:
        if ":" in account_id:
            backend_id, user_id = account_id.split(":", 1)
            return self._backend_map.get(backend_id, self.backends[0]), user_id
        return self.backends[0], account_id

    def _usage_model_name(
        self,
        record: dict[str, Any],
        fallback: str = "未知模型",
        backend: LiteLLMBackend | None = None,
    ) -> str:
        backend = backend or self.backends[0]
        deployment_map = getattr(self, "_deployment_model_maps", {}).get(backend.id, {})
        model_id = _clean_text(_first(record, "model_id", default=""))
        if model_id:
            mapped = deployment_map.get(model_id.casefold())
            if mapped:
                return resolve_canonical_model_name(mapped, diagnose_unknown=True)
        for field in ("litellm_model_name", "model"):
            value = _clean_text(_first(record, field, default=""))
            if value:
                return resolve_canonical_model_name(value, diagnose_unknown=True)
        if model_id and _is_recognizable_deployment_name(model_id):
            return resolve_canonical_model_name(model_id, diagnose_unknown=True)
        model_group = _clean_text(_first(record, "model_group", default=""))
        if model_group:
            return resolve_canonical_model_name(model_group, diagnose_unknown=True)
        breakdown = record.get("breakdown") if isinstance(record.get("breakdown"), dict) else {}
        for field in ("models", "model_groups"):
            bucket = breakdown.get(field)
            if isinstance(bucket, dict):
                for name in bucket.keys():
                    value = _clean_text(name)
                    if value:
                        return resolve_canonical_model_name(value, diagnose_unknown=True)
        if model_id:
            return resolve_canonical_model_name(model_id, diagnose_unknown=True)
        return fallback

    async def _deployment_model_map(self, backend: LiteLLMBackend) -> dict[str, str]:
        if not hasattr(self, "_deployment_model_maps"):
            self._deployment_model_maps = {}
        cache_key = f"deployment-model-map:v1:{backend.id}"
        hit, value, _ = self._model_cache.get(cache_key)
        if hit:
            self._deployment_model_maps[backend.id] = value
            return value
        payload = await self.request_backend(backend, "GET", "/model/info")
        mapping: dict[str, str] = {}
        for item in _records(payload):
            info = item.get("model_info") if isinstance(item.get("model_info"), dict) else {}
            params = item.get("litellm_params") if isinstance(item.get("litellm_params"), dict) else {}
            deployment_id = _clean_text(info.get("id") or item.get("model_info_id"))
            actual_model = _clean_text(params.get("model"))
            if deployment_id and actual_model:
                mapping[deployment_id.casefold()] = actual_model
        self._deployment_model_maps[backend.id] = mapping
        self._model_cache.set(cache_key, mapping, _env_int("MODEL_CACHE_TTL_SECONDS", 1800))
        return mapping

    async def _ensure_deployment_model_map(self, backend: LiteLLMBackend) -> dict[str, str]:
        try:
            return await self._deployment_model_map(backend)
        except Exception:
            logger.warning("model deployment directory unavailable for backend %s", backend.id)
            return getattr(self, "_deployment_model_maps", {}).get(backend.id, {})

    def _is_backend_usage_account(self, backend: LiteLLMBackend, user_id: Any) -> bool:
        text = _clean_text(user_id).lower()
        if backend.source == "Her":
            return text.startswith("carher-")
        return bool(text)

    def _empty_account_index(self) -> dict[str, Any]:
        return {
            "emails": defaultdict(dict),
            # 共享账号的 used_by 姓名等次级线索，仅在 owner 匹配落空时兜底。
            "names": defaultdict(dict),
            # 账号持有人姓名 -> {owner_key: {"userIds", "emails", "sources"}}。
            "owners": defaultdict(dict),
            "identities": {},
            "profiles": {},
        }

    @staticmethod
    def _account_owner_key(user_id: Any, email: Any, metadata: dict[str, Any] | None = None) -> str:
        """把同一个人在上游的多个账号归并到一个身份键。

        上游按飞书通讯录建号，同一员工可能持有多个 ``carher-*`` 账号；
        ``lark_open_id`` 是唯一稳定的人员标识，缺失时才退回邮箱/账号 ID。
        """

        metadata = metadata or {}
        open_id = _clean_text(metadata.get("lark_open_id") or metadata.get("open_id"))
        if open_id:
            return f"lark:{open_id}"
        email_text = _normal_email(email)
        if email_text:
            return f"email:{email_text}"
        return f"uid:{_clean_text(user_id)}"

    def _add_account_owner_entry(
        self,
        index: dict[str, Any],
        user_id: Any,
        owner_key: str,
        source: str,
        email: Any = None,
        names: list[Any] | None = None,
    ) -> None:
        text_user_id = _clean_text(user_id)
        if not text_user_id or not owner_key:
            return
        index.setdefault("identities", {})[text_user_id] = owner_key
        owners = index.setdefault("owners", defaultdict(dict))
        email_text = _normal_email(email)
        for raw_name in names or []:
            name = _clean_text(raw_name)
            if not name:
                continue
            bucket = owners[name].setdefault(
                owner_key, {"userIds": set(), "emails": set(), "sources": set()}
            )
            bucket["userIds"].add(text_user_id)
            bucket["sources"].add(source)
            if email_text:
                bucket["emails"].add(email_text)

    def _add_account_index_entry(
        self,
        index: dict[str, Any],
        user_id: Any,
        source: str,
        email: Any = None,
        names: list[Any] | None = None,
    ) -> None:
        text_user_id = _clean_text(user_id)
        if not text_user_id:
            return
        email_text = _normal_email(email)
        if email_text:
            bucket = index["emails"][email_text].setdefault(text_user_id, {"emails": set(), "sources": set(), "names": set()})
            bucket["emails"].add(email_text)
            bucket["sources"].add(source)
        for raw_name in names or []:
            name = _clean_text(raw_name)
            if not name:
                continue
            bucket = index["names"][name].setdefault(text_user_id, {"emails": set(), "sources": set(), "names": set()})
            if email_text:
                bucket["emails"].add(email_text)
            bucket["sources"].add(source)
            bucket["names"].add(name)

    async def her_account_index(self, backend: LiteLLMBackend) -> dict[str, Any]:
        cache_key = f"account-index:{backend.id}"
        hit, value, _ = self._account_index_cache.get(cache_key)
        if hit:
            return value

        index = self._empty_account_index()
        for page in range(1, 101):
            payload = await self.request_backend(backend, "GET", "/user/list", params={"page": page, "page_size": 100})
            for user in _records(payload):
                metadata = _metadata_dict(user.get("metadata"))
                user_id = user.get("user_id")
                email = _normal_email(user.get("user_email") or user.get("sso_user_id") or metadata.get("email"))
                owner_names = [
                    user.get("user_alias"),
                    metadata.get("display_name"),
                    metadata.get("owner_name"),
                ]
                names = list(owner_names)
                for used_by in metadata.get("used_by") or []:
                    if isinstance(used_by, dict):
                        names.append(used_by.get("name"))
                if self._is_backend_usage_account(backend, user_id):
                    user_id_text = _clean_text(user_id)
                    alias_name = _clean_text(user.get("user_alias") or metadata.get("display_name") or metadata.get("owner_name"))
                    owner_key = self._account_owner_key(user_id_text, email, metadata)
                    index["profiles"][user_id_text] = {
                        "email": email,
                        "name": alias_name,
                        "ownerKey": owner_key,
                        "department": _clean_text(metadata.get("department") or metadata.get("team_alias")),
                        "emailSource": _clean_text(metadata.get("email_source")) or ("upstream" if email else ""),
                    }
                    source = "her_user_email" if email else "her_user_alias"
                    self._add_account_index_entry(index, user_id, source, email, names)
                    self._add_account_owner_entry(index, user_id, owner_key, source, email, owner_names)
            total_pages = _as_int(payload.get("total_pages")) if isinstance(payload, dict) else 0
            if total_pages and page >= total_pages:
                break

        max_pages = max(1, _env_int("HER_KEY_LIST_MAX_PAGES", 20))
        for page in range(1, max_pages + 1):
            payload = await self.request_backend(
                backend,
                "GET",
                "/key/list",
                params={"return_full_object": "true", "page": page, "size": 100},
            )
            keys = _records(payload)
            if not keys:
                break
            for key in keys:
                metadata = _metadata_dict(key.get("metadata"))
                email = _normal_email(metadata.get("email"))
                owner_names = [
                    metadata.get("display_name"),
                    metadata.get("owner_name"),
                    key.get("user_alias"),
                ]
                names = [*owner_names, key.get("key_alias")]
                for used_by in metadata.get("used_by") or []:
                    if isinstance(used_by, dict):
                        names.append(used_by.get("name"))
                if self._is_backend_usage_account(backend, key.get("user_id")):
                    user_id_text = _clean_text(key.get("user_id"))
                    source = "her_key_metadata_email" if email else "her_key_metadata_name"
                    owner_key = ""
                    if user_id_text:
                        existing = index["profiles"].get(user_id_text, {})
                        profile_email = email or _normal_email(existing.get("email"))
                        profile_name = _clean_text(metadata.get("display_name") or metadata.get("owner_name") or existing.get("name"))
                        # 账号本身的身份键优先，密钥元数据只在账号缺失时兜底建键。
                        owner_key = _clean_text(existing.get("ownerKey")) or self._account_owner_key(
                            user_id_text, profile_email, metadata
                        )
                        index["profiles"][user_id_text] = {
                            **existing,
                            "email": profile_email,
                            "name": profile_name,
                            "ownerKey": owner_key,
                            "department": _clean_text(
                                existing.get("department") or metadata.get("department") or metadata.get("team_alias")
                            ),
                            "emailSource": _clean_text(existing.get("emailSource"))
                            or (_clean_text(metadata.get("email_source")) if profile_email else "")
                            or ("upstream" if profile_email else ""),
                        }
                    self._add_account_index_entry(index, key.get("user_id"), source, email, names)
                    if owner_key:
                        self._add_account_owner_entry(
                            index, key.get("user_id"), owner_key, source, email, owner_names
                        )
            total_pages = _as_int(_first(payload, "total_pages", "totalPages", default=0)) if isinstance(payload, dict) else 0
            if total_pages and page >= total_pages:
                break

        self._account_index_cache.set(cache_key, index, _env_int("HER_ACCOUNT_INDEX_CACHE_TTL_SECONDS", 1800))
        return index

    def _log_raw_user(self, log: dict[str, Any]) -> str:
        metadata = _metadata_dict(_first(log, "metadata", "request_tags", "tags", default={}))
        return str(
            _first(log, "user", "user_id", "end_user", default="")
            or metadata.get("user_api_key_user_id")
            or metadata.get("user_id")
            or ""
        ).strip()

    @staticmethod
    def _log_usage_attribution(log: dict[str, Any]) -> dict[str, str]:
        """Extract non-secret tenant identifiers recorded by LiteLLM SpendLogs."""

        metadata = _metadata_dict(_first(log, "metadata", default={}))
        organization_id = _clean_text(
            _first(log, "organization_id", "org_id", "organizationId", "orgId", default="")
            or metadata.get("user_api_key_org_id")
            or metadata.get("organization_id")
            or metadata.get("org_id")
        )
        team_id = _clean_text(
            _first(log, "team_id", "teamId", default="")
            or metadata.get("user_api_key_team_id")
            or metadata.get("team_id")
        )
        # SpendLogs normally returns the hashed token. If an older deployment
        # returns a raw sk-* value, hash it before it can reach local storage.
        key_id = safe_key_id(
            _first(log, "api_key", "token_id", "key_id", default="")
            or metadata.get("user_api_key")
        )
        return {
            "organizationId": organization_id,
            "teamId": team_id,
            "keyId": key_id,
        }

    def _log_identity_candidates(self, log: dict[str, Any]) -> tuple[str, set[str], list[str]]:
        metadata = _metadata_dict(_first(log, "metadata", "request_tags", "tags", default={}))
        raw_user = self._log_raw_user(log)
        emails = {
            _normal_email(raw_user),
            _normal_email(_first(log, "user_email", "email", "sso_user_id", default="")),
            _normal_email(metadata.get("email")),
            _normal_email(metadata.get("user_email")),
            _normal_email(metadata.get("sso_user_id")),
            _normal_email(metadata.get("owner_email")),
            _normal_email(metadata.get("end_user")),
        }
        names = [
            _clean_text(_first(log, "user_alias", "name", default="")),
            _clean_text(metadata.get("display_name")),
            _clean_text(metadata.get("owner_name")),
            _clean_text(metadata.get("user_alias")),
            _clean_text(metadata.get("name")),
        ]
        return raw_user, {item for item in emails if item}, [item for item in names if item]

    def _employee_info_from_raw_user(
        self,
        raw_user: str,
        user_map: dict[str, dict[str, Any]],
        backend: LiteLLMBackend | None = None,
        account_index: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        employee = self._admin_employee_info(raw_user, user_map)
        if raw_user or not backend or backend.source != "Her" or not account_index:
            return employee

        # No raw user ID found on log; try Her profile metadata fallback.
        return {"id": "", "name": "", "email": "", "bindStatus": "未绑定邮箱"}

    def _employee_info_from_log(
        self,
        log: dict[str, Any],
        user_map: dict[str, dict[str, Any]],
        backend: LiteLLMBackend,
        account_index: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raw_user, emails, names = self._log_identity_candidates(log)
        if raw_user:
            normalized = raw_user.lower()
            if normalized in user_map:
                return user_map[normalized]
        for email in sorted(emails):
            if email in user_map:
                return user_map[email]

        if backend.source == "Her" and account_index:
            for email in sorted(emails):
                matches = account_index.get("emails", {}).get(email, {})
                if len(matches) == 1:
                    user_id = next(iter(matches.keys()))
                    if user_id and user_id.lower() in user_map:
                        return user_map[user_id.lower()]
                    profile = account_index.get("profiles", {}).get(user_id, {})
                    if user_id:
                        profile_email = _normal_email(profile.get("email"))
                        profile_name = _clean_text(profile.get("name")) or (profile_email.split("@", 1)[0] if profile_email else user_id)
                        return {"id": profile_email or user_id, "name": profile_name, "email": profile_email, "bindStatus": "已绑定邮箱" if profile_email else "未绑定邮箱"}
            for name in names:
                owner_match = self._owner_name_matches(account_index, name)
                if owner_match:
                    user_id, profile = self._owner_profile_from_index(
                        account_index, owner_match.get("userIds", set())
                    )
                    if user_id and user_id.lower() in user_map:
                        return user_map[user_id.lower()]
                    profile_email = _normal_email(profile.get("email"))
                    profile_name = _clean_text(profile.get("name")) or name or user_id
                    return {
                        "id": profile_email or user_id,
                        "name": profile_name,
                        "email": profile_email,
                        "bindStatus": "已绑定邮箱" if profile_email else "未绑定邮箱",
                    }
            for name in names:
                matches = self._name_index_matches(account_index, name)
                if len(matches) == 1:
                    user_id = next(iter(matches.keys()))
                    if user_id and user_id.lower() in user_map:
                        return user_map[user_id.lower()]
                    profile = account_index.get("profiles", {}).get(user_id, {})
                    profile_email = _normal_email(profile.get("email"))
                    profile_name = _clean_text(profile.get("name")) or name or user_id
                    return {"id": profile_email or user_id, "name": profile_name, "email": profile_email, "bindStatus": "已绑定邮箱" if profile_email else "未绑定邮箱"}

        if raw_user:
            return self._admin_employee_info(raw_user, user_map)
        return {"id": "unbound-account", "name": "未绑定账号", "email": "", "bindStatus": "未绑定邮箱"}

    def _name_index_matches(self, index: dict[str, Any], name: str) -> dict[str, dict[str, set[str]]]:
        if not name or not _has_cjk(name):
            return {}
        candidates = index["names"].get(name, {})
        if not candidates:
            return {}
        user_ids = {user_id for user_id in candidates if user_id}
        emails = {email for entry in candidates.values() for email in entry.get("emails", set()) if email}
        if len(user_ids) == 1 and len(emails) <= 1:
            return candidates
        return {}

    def _owner_name_matches(self, index: dict[str, Any], name: str) -> dict[str, Any]:
        """按"人"而不是按账号判断姓名是否唯一。

        上游同一员工可能持有多个账号，旧的"姓名只能对应一个 user_id"规则会把
        这种情况误判成重名而放弃匹配。这里先用 ``lark_open_id`` 归并到同一个人，
        只有当一个姓名确实落在两个不同的人身上时才判定歧义。
        """

        clean_name = _clean_text(name)
        if len(clean_name) < 2 or clean_name.lower() in _GENERIC_ACCOUNT_NAMES:
            return {}
        candidates = index.get("owners", {}).get(clean_name, {})
        if len(candidates) != 1:
            return {}
        return next(iter(candidates.values()))

    def _owner_profile_from_index(
        self,
        index: dict[str, Any],
        user_ids: Any,
    ) -> tuple[str, dict[str, Any]]:
        """在一个人的多个账号里挑出信息最全的一个作为展示身份。"""

        profiles = index.get("profiles", {})
        best_user_id = ""
        best_profile: dict[str, Any] = {}
        for user_id in sorted(user_ids or []):
            profile = profiles.get(user_id) or {}
            if not best_user_id:
                best_user_id, best_profile = user_id, profile
            if _normal_email(profile.get("email")) and not _normal_email(best_profile.get("email")):
                best_user_id, best_profile = user_id, profile
        return best_user_id, best_profile

    async def add_her_index_matches(
        self,
        backend: LiteLLMBackend,
        email_lower: str,
        name: str | None,
        add_user_id: Any,
    ) -> None:
        index = await self.her_account_index(backend)
        email_matches = index["emails"].get(email_lower, {})
        for user_id, entry in email_matches.items():
            for source in sorted(entry.get("sources", set())) or ["her_email"]:
                add_user_id(backend, user_id, source)

        if email_matches:
            return

        owner_match = self._owner_name_matches(index, _clean_text(name))
        if owner_match:
            for user_id in sorted(owner_match.get("userIds", set())):
                add_user_id(backend, user_id, "her_user_alias_owner")
            return

        for user_id, entry in self._name_index_matches(index, _clean_text(name)).items():
            for source in sorted(entry.get("sources", set())) or ["her_shared_account_name"]:
                add_user_id(backend, user_id, "her_shared_account_name" if source.startswith("her_") else source)

    async def _targeted_identity_users(
        self,
        backend: LiteLLMBackend,
        email: str,
        aliases: set[str],
    ) -> list[dict[str, Any]] | None:
        """Use LiteLLM's indexed user filters instead of scanning every page."""

        async def fetch(params: dict[str, Any]) -> list[dict[str, Any]] | None:
            try:
                payload = await self.request_backend(
                    backend,
                    "GET",
                    "/user/list",
                    params={"page": 1, "page_size": 100, **params},
                )
            except HTTPException as exc:
                # Older upstreams may reject newer filter fields. Preserve the
                # legacy full scan only for that compatibility case.
                if exc.status_code in {400, 404, 422}:
                    return None
                raise
            return _records(payload)

        batches = await asyncio.gather(
            fetch({"user_email": email}),
            fetch({"sso_user_ids": email}),
            fetch({"user_ids": ",".join(sorted(aliases))}),
        )
        if any(batch is None for batch in batches):
            return None

        records: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for batch in batches:
            assert batch is not None
            for user in batch:
                identity = tuple(
                    str(user.get(field) or "").strip().casefold()
                    for field in ("user_id", "user_email", "sso_user_id", "user_alias")
                )
                if identity in seen:
                    continue
                seen.add(identity)
                records.append(user)
        return records

    async def resolve_user(self, email: str, name: str | None = None) -> dict[str, Any]:
        email_lower = email.lower()
        email_prefix = email_lower.split("@", 1)[0]
        legacy_aliases = set(tool_account_aliases(email_prefix))
        matched_users: list[dict[str, Any]] = []
        matched_user_ids: list[str] = []
        matched_accounts: list[dict[str, str]] = []
        matched_account_map: dict[str, dict[str, Any]] = {}
        matched_sources: dict[str, list[str]] = {}

        def add_user_id(backend: LiteLLMBackend, user_id: Any, source: str) -> None:
            text = str(user_id or "").strip()
            if not text or not self._is_backend_usage_account(backend, text):
                return
            encoded = self._encode_account_id(backend, text)
            if encoded not in matched_user_ids:
                matched_user_ids.append(encoded)
                account = {"backend": backend.id, "source": backend.source or "其他", "user_id": text, "account_id": encoded, "matchSources": []}
                matched_accounts.append(account)
                matched_account_map[encoded] = account
            matched_sources.setdefault(encoded, [])
            if source not in matched_sources[encoded]:
                matched_sources[encoded].append(source)
            account = matched_account_map.get(encoded)
            if account is not None and source not in account["matchSources"]:
                account["matchSources"].append(source)

        def add_matching_users(backend: LiteLLMBackend, users: list[dict[str, Any]]) -> None:
            for user in users:
                user_id = user.get("user_id")
                email_candidates = [user.get("user_email"), user.get("sso_user_id")]
                legacy_candidates = [user.get("user_id"), user.get("user_alias")]
                if any(str(candidate or "").lower() == email_lower for candidate in email_candidates):
                    matched_users.append(user)
                    add_user_id(backend, user_id, "user_email")
                elif any(str(candidate or "").lower() in legacy_aliases for candidate in legacy_candidates):
                    matched_users.append(user)
                    add_user_id(backend, user_id, "tool_account_alias")

        async def scan_all_users(backend: LiteLLMBackend) -> list[dict[str, Any]]:
            users: list[dict[str, Any]] = []
            for page in range(1, 51):
                payload = await self.request_backend(
                    backend,
                    "GET",
                    "/user/list",
                    params={"page": page, "page_size": 100},
                )
                users.extend(_records(payload))
                total_pages = _as_int(payload.get("total_pages")) if isinstance(payload, dict) else 0
                if total_pages and page >= total_pages:
                    break
            return users

        for backend in self.backends:
            users = await self._targeted_identity_users(backend, email_lower, legacy_aliases)
            used_targeted_filters = users is not None
            if users is None:
                users = await scan_all_users(backend)
            add_matching_users(backend, users)

            if backend.source != "Her":
                for user_id in await self.user_ids_from_key_alias(email_prefix, backend):
                    add_user_id(backend, user_id, "key_alias")
                backend_has_match = any(account.get("backend") == backend.id for account in matched_accounts)
                if used_targeted_filters and not users and not backend_has_match:
                    # Official filters cannot search user_alias. Keep that rare
                    # legacy mapping path without penalizing normal accounts.
                    add_matching_users(backend, await scan_all_users(backend))
                if not matched_user_ids:
                    for user_id in await self.user_ids_from_recent_logs(email_prefix, backend):
                        add_user_id(backend, user_id, "recent_usage_log")

            if backend.source == "Her":
                await self.add_her_index_matches(backend, email_lower, name, add_user_id)

        if matched_user_ids:
            primary = matched_users[0].copy() if matched_users else {}
            primary.setdefault("user_id", matched_user_ids[0])
            primary["matched_user_ids"] = sorted(matched_user_ids)
            primary["matched_accounts"] = matched_accounts
            primary["matched_sources"] = matched_sources
            primary["user_email"] = email_lower
            primary.setdefault("user_alias", email_prefix)
            primary["matched_by"] = "email_and_legacy"
            return primary

        raise HTTPException(status_code=404, detail="未找到当前员工对应的用量账号")

    async def user_ids_from_key_alias(self, email_prefix: str, backend: LiteLLMBackend | None = None) -> list[str]:
        backend = backend or self.backends[0]
        user_ids: list[str] = []
        seen: set[str] = set()
        aliases = tool_account_aliases(email_prefix)

        def add_user_id(value: Any) -> None:
            user_id = str(value or "").strip()
            if user_id and user_id not in seen:
                seen.add(user_id)
                user_ids.append(user_id)

        async def fetch_alias(alias: str, substring_matching: bool = False) -> None:
            params: dict[str, Any] = {"key_alias": alias, "return_full_object": "true", "page": 1, "size": 100}
            if substring_matching:
                params["substring_matching"] = "true"
            payload = await self.request_backend(backend, "GET", "/key/list", params=params)
            for key in _records(payload):
                if substring_matching and not _tool_alias_matches(alias, key.get("key_alias")):
                    continue
                add_user_id(key.get("user_id"))

        # 并行查询所有 alias,包括精确匹配和子串匹配
        tasks = [fetch_alias(alias) for alias in aliases]
        tasks.extend([
            fetch_alias(alias, substring_matching=True)
            for alias in tool_account_aliases(email_prefix)
            if alias != email_prefix
        ])
        await asyncio.gather(*tasks)
        return user_ids

    async def user_ids_from_recent_logs(self, email_prefix: str, backend: LiteLLMBackend | None = None) -> list[str]:
        backend = backend or self.backends[0]
        if backend.source:
            return []
        aliases = set(tool_account_aliases(email_prefix))
        if not aliases:
            return []
        end_date = date.today().isoformat()
        start_date = (date.today() - timedelta(days=29)).isoformat()
        utc_start, utc_end = _local_date_window_as_utc_text(start_date, end_date)
        user_ids: list[str] = []
        seen: set[str] = set()
        max_pages = max(1, min(10, int(os.getenv("PERSONAL_ACCOUNT_DISCOVERY_LOG_PAGES", "5"))))
        page_size = max(1, min(100, int(os.getenv("PERSONAL_ACCOUNT_DISCOVERY_PAGE_SIZE", "100"))))
        for page in range(1, max_pages + 1):
            payload = await self.request_backend(
                backend,
                "GET",
                "/spend/logs/v2",
                params={
                    "start_date": utc_start,
                    "end_date": utc_end,
                    "page": page,
                    "page_size": page_size,
                    "sort_by": "startTime",
                    "sort_order": "desc",
                },
            )
            for log in _records(payload):
                raw_user = self._log_raw_user(log)
                normalized = raw_user.lower()
                if normalized in aliases and raw_user not in seen:
                    seen.add(raw_user)
                    user_ids.append(raw_user)
            total_pages = _as_int(payload.get("total_pages")) if isinstance(payload, dict) else 0
            if total_pages and page >= total_pages:
                break
        return user_ids

    async def usage_rows(self, user_id: str, start_date: str, end_date: str, source: str | None) -> list[dict[str, Any]]:
        backend, raw_user_id = self._decode_account_id(user_id)
        if backend.source:
            if _source_filter_applies(source) and source != backend.source:
                return []
            return await self._usage_from_daily_activity(raw_user_id, start_date, end_date, "all", backend=backend, source_override=backend.source)

        try:
            rows = await self._usage_from_key_daily_activity(raw_user_id, start_date, end_date, source, backend)
        except HTTPException:
            rows = []
        if rows:
            return rows

        if _env_bool("PERSONAL_USAGE_LOG_FALLBACK_ENABLED", False):
            try:
                rows = await self._usage_from_logs(raw_user_id, start_date, end_date, source, backend)
            except HTTPException:
                rows = []
            if rows:
                return rows
        return await self._usage_from_daily_activity(raw_user_id, start_date, end_date, source, backend=backend)

    async def usage_rows_for_user_ids(self, user_ids: list[str], start_date: str, end_date: str, source: str | None) -> list[dict[str, Any]]:
        batches = await asyncio.gather(*(self.usage_rows(user_id, start_date, end_date, source) for user_id in user_ids))
        rows = [row for batch in batches for row in batch]
        return sorted(rows, key=lambda item: (item["date"], item["source"], item["model"]))

    async def _usage_from_key_daily_activity(self, user_id: str, start_date: str, end_date: str, source: str | None, backend: LiteLLMBackend | None = None) -> list[dict[str, Any]]:
        backend = backend or self.backends[0]
        keys = await self.keys_for_user(user_id, backend)
        selected_keys: list[tuple[dict[str, Any], str]] = []
        for key in keys[:25]:
            key_source = detect_source_from_key(key)
            if _source_filter_applies(source) and key_source != source:
                continue
            selected_keys.append((key, key_source))

        async def load(key: dict[str, Any], key_source: str) -> list[dict[str, Any]]:
            rows = await self._usage_from_daily_activity(
                user_id=user_id,
                start_date=start_date,
                end_date=end_date,
                source="all",
                api_key=key["id"],
                backend=backend,
                source_override=key_source,
            )
            rotation = key.get("_rotation") if isinstance(key.get("_rotation"), dict) else {}
            organization_id = _clean_text(rotation.get("org_id") or rotation.get("organization_id"))
            team_id = _clean_text(rotation.get("team_id"))
            for row in rows:
                row.update({"organizationId": organization_id, "teamId": team_id, "keyId": _clean_text(key.get("id"))})
            return rows

        batches = await asyncio.gather(*(load(key, key_source) for key, key_source in selected_keys))
        return [row for batch in batches for row in batch]

    async def usage_from_daily_activity_for_debug(self, user_id: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        return await self._usage_from_daily_activity(user_id, start_date, end_date, "all")

    async def usage_from_logs_for_debug(self, user_id: str, start_date: str, end_date: str, max_pages: int = 3) -> list[dict[str, Any]]:
        original = os.getenv("USAGE_LOG_MAX_PAGES")
        os.environ["USAGE_LOG_MAX_PAGES"] = str(max(1, max_pages))
        try:
            return await self._usage_from_logs(user_id, start_date, end_date, "all")
        finally:
            if original is None:
                os.environ.pop("USAGE_LOG_MAX_PAGES", None)
            else:
                os.environ["USAGE_LOG_MAX_PAGES"] = original

    async def _usage_from_logs(self, user_id: str, start_date: str, end_date: str, source: str | None, backend: LiteLLMBackend | None = None) -> list[dict[str, Any]]:
        backend = backend or self.backends[0]
        await self._ensure_deployment_model_map(backend)
        max_pages = max(1, int(os.getenv("USAGE_LOG_MAX_PAGES", "20")))
        utc_start, utc_end = _local_date_window_as_utc_text(start_date, end_date)
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for page in range(1, max_pages + 1):
            payload = await self.request_backend(
                backend,
                "GET",
                "/spend/logs/v2",
                params={
                    "user_id": user_id,
                    "start_date": utc_start,
                    "end_date": utc_end,
                    "page": page,
                    "page_size": 100,
                    "sort_by": "startTime",
                    "sort_order": "asc",
                },
            )
            logs = _records(payload)
            if not logs:
                break
            for log in logs:
                detected_source = backend.source or detect_source(log)
                if source and source != "all" and detected_source != source:
                    continue
                model = self._usage_model_name(log, backend=backend)
                day = _date_text_in_usage_timezone(_first(log, "startTime", "start_time", "created_at", "date"))
                key = (day, detected_source, model)
                row = grouped.setdefault(key, self._empty_usage_row(day, detected_source, model))
                self._add_log_to_row(row, log)
            total_pages = _as_int(_first(payload, "total_pages", "totalPages", default=0)) if isinstance(payload, dict) else 0
            if total_pages and page >= total_pages:
                break
        return sorted(grouped.values(), key=lambda item: (item["date"], item["source"], item["model"]))

    def _empty_usage_row(self, day: str, source: str, model: str) -> dict[str, Any]:
        return {
            "date": day,
            "source": source,
            "model": model,
            "promptTokens": 0,
            "completionTokens": 0,
            "totalTokens": 0,
            "requestCount": 0,
            "successCount": 0,
            "failureCount": 0,
            "spend": 0.0,
        }

    def _add_log_to_row(self, row: dict[str, Any], log: dict[str, Any]) -> None:
        prompt = _as_int(_first(log, "prompt_tokens", "promptTokens", "input_tokens"))
        completion = _as_int(_first(log, "completion_tokens", "completionTokens", "output_tokens"))
        total = _as_int(_first(log, "total_tokens", "totalTokens", default=prompt + completion))
        status = str(_first(log, "status", "status_filter", "response_status", default="success")).lower()
        row["promptTokens"] += prompt
        row["completionTokens"] += completion
        row["totalTokens"] += total
        row["requestCount"] += 1
        row["spend"] += _as_number(_first(log, "spend", "cost", "total_spend"))
        if "fail" in status or "error" in status:
            row["failureCount"] += 1
        else:
            row["successCount"] += 1

    def _row_from_daily_activity_item(
        self,
        item: dict[str, Any],
        source: str,
        fallback_model: str = "全部模型",
        backend: LiteLLMBackend | None = None,
    ) -> dict[str, Any]:
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else item
        model = self._usage_model_name(item, fallback_model, backend)
        prompt = _as_int(_first(metrics, "prompt_tokens", "promptTokens", "total_prompt_tokens"))
        completion = _as_int(_first(metrics, "completion_tokens", "completionTokens", "total_completion_tokens"))
        total = _as_int(_first(metrics, "total_tokens", "totalTokens", default=prompt + completion))
        requests = _as_int(_first(metrics, "api_requests", "total_api_requests", "requestCount"))
        successes = _as_int(_first(metrics, "successful_requests", "total_successful_requests", "successCount"))
        failures = _as_int(_first(metrics, "failed_requests", "total_failed_requests", "failureCount"))
        if not successes and requests:
            successes = max(0, requests - failures)
        return {
            "date": _date_text(_first(item, "date", "day")),
            "source": source,
            "model": model,
            "promptTokens": prompt,
            "completionTokens": completion,
            "totalTokens": total,
            "requestCount": requests,
            "successCount": successes,
            "failureCount": failures,
            "spend": _as_number(_first(metrics, "spend", "total_spend")),
        }

    def _rows_from_daily_activity_item(
        self, item: dict[str, Any], source: str, backend: LiteLLMBackend | None = None
    ) -> list[dict[str, Any]]:
        breakdown = item.get("breakdown") if isinstance(item.get("breakdown"), dict) else {}
        models = breakdown.get("models") if isinstance(breakdown.get("models"), dict) else {}
        if models:
            day = _date_text(_first(item, "date", "day"))
            grouped: dict[str, dict[str, Any]] = {}
            for model_name, model_value in models.items():
                resolved_backend = backend or self.backends[0]
                model = resolve_canonical_model_name(
                    model_name,
                    deployment_map=getattr(self, "_deployment_model_maps", {}).get(resolved_backend.id, {}),
                    diagnose_unknown=True,
                )
                if not model:
                    continue
                metrics = _metrics_dict(model_value)
                prompt = _as_int(_first(metrics, "prompt_tokens", "promptTokens", "total_prompt_tokens"))
                completion = _as_int(_first(metrics, "completion_tokens", "completionTokens", "total_completion_tokens"))
                total = _as_int(_first(metrics, "total_tokens", "totalTokens", default=prompt + completion))
                requests = _as_int(_first(metrics, "api_requests", "total_api_requests", "requestCount"))
                successes = _as_int(_first(metrics, "successful_requests", "total_successful_requests", "successCount"))
                failures = _as_int(_first(metrics, "failed_requests", "total_failed_requests", "failureCount"))
                if not successes and requests:
                    successes = max(0, requests - failures)
                row = grouped.setdefault(model, self._empty_usage_row(day, source, model))
                row["promptTokens"] += prompt
                row["completionTokens"] += completion
                row["totalTokens"] += total
                row["requestCount"] += requests
                row["successCount"] += successes
                row["failureCount"] += failures
                row["spend"] += _as_number(_first(metrics, "spend", "total_spend"))
            if grouped:
                return list(grouped.values())
        return [self._row_from_daily_activity_item(item, source, backend=backend)]

    async def _usage_from_daily_activity(
        self,
        user_id: str,
        start_date: str,
        end_date: str,
        source: str | None,
        api_key: str | None = None,
        backend: LiteLLMBackend | None = None,
        source_override: str | None = None,
    ) -> list[dict[str, Any]]:
        if _source_filter_applies(source):
            return []
        backend = backend or self.backends[0]
        await self._ensure_deployment_model_map(backend)
        params = {"user_id": user_id, "start_date": start_date, "end_date": end_date, "page": 1, "page_size": 1000}
        if api_key:
            params["api_key"] = api_key
        try:
            payload = await self.request_backend(backend, "GET", "/user/daily/activity/aggregated", params=params)
        except HTTPException:
            payload = await self.request_backend(backend, "GET", "/user/daily/activity", params=params)
        rows = []
        for item in _records(payload):
            rows.extend(self._rows_from_daily_activity_item(item, source_override or "其他", backend))
        return rows

    async def _load_keys_for_user(
        self,
        user_id: str,
        backend: LiteLLMBackend,
        cache_key: str,
        cache_version: int,
    ) -> list[dict[str, Any]]:
        payload = await self.request_backend(
            backend,
            "GET",
            "/key/list",
            params={"user_id": user_id, "return_full_object": "true", "page": 1, "size": 100},
        )
        keys = []
        for item in _records(payload):
            token_hash = safe_key_id(_first(item, "token", "token_id", "token_hash", "id", default=""))
            if not token_hash:
                continue
            metadata = _metadata_dict(_first(item, "metadata", default={}))
            alias = _clean_text(metadata.get("display_name") or item.get("key_alias")) or "个人访问密钥"
            key_alias = _clean_text(item.get("key_alias"))
            expires = _first(item, "expires", default=None)
            if _first(item, "blocked", "deleted", default=False):
                status = "已禁用"
            elif _is_expired(expires):
                status = "已过期"
            else:
                status = "正常"
            last_used = _first(item, "last_active", "last_used_at", default=None)
            created_at = _first(item, "created_at", default=None)
            models = item.get("models") if isinstance(item.get("models"), list) else []
            rotation_fields = {
                name: item.get(name)
                for name in (
                    "max_budget",
                    "spend",
                    "budget_duration",
                    "budget_limits",
                    "budget_id",
                    "max_parallel_requests",
                    "tpm_limit",
                    "rpm_limit",
                    "allowed_cache_controls",
                    "allowed_routes",
                    "config",
                    "permissions",
                    "model_max_budget",
                    "budget_fallbacks",
                    "model_rpm_limit",
                    "model_tpm_limit",
                    "guardrails",
                    "policies",
                    "prompts",
                    "aliases",
                    "object_permission",
                    "tags",
                    "disable_global_guardrails",
                    "enforced_params",
                    "allowed_passthrough_routes",
                    "allowed_vector_store_indexes",
                    "rpm_limit_type",
                    "tpm_limit_type",
                    "router_settings",
                    "access_group_ids",
                    "team_id",
                    "agent_id",
                    "project_id",
                    "org_id",
                )
                if item.get(name) is not None
            }
            keys.append(
                {
                    "_backendId": backend.id,
                    "_userId": user_id,
                    "id": token_hash,
                    "keyType": key_display_type(item, user_id),
                    "name": alias,
                    "purpose": _clean_text(metadata.get("purpose")) or "用于个人 AI 工具访问。",
                    "masked": safe_key_name(item.get("key_name")),
                    "models": [str(model) for model in models if model],
                    "createdAt": _date_text(created_at) if created_at else "-",
                    "lastUsed": _date_text(last_used) if last_used else "-",
                    "expiresAt": _date_text(expires) if expires else "永不过期",
                    "monthTokens": _as_int(_first(item, "total_tokens", "token_usage", default=0)),
                    "spend": _as_number(_first(item, "spend", "total_spend")),
                    "status": status,
                    "_rotation": {
                        # Keep the upstream alias private so a replacement key preserves identity.
                        "key_alias": key_alias,
                        "metadata": metadata,
                        "models": [str(model) for model in models if model],
                        "expires": expires,
                        "team_id": _clean_text(item.get("team_id")),
                        "org_id": _clean_text(item.get("org_id") or item.get("organization_id")),
                        **rotation_fields,
                    },
                }
            )
        if self._key_cache_versions.get(cache_key, 0) == cache_version:
            self._key_cache.set(cache_key, keys, _env_int("KEY_LIST_CACHE_TTL_SECONDS", 300))
        return keys

    async def keys_for_user(self, user_id: str, backend: LiteLLMBackend | None = None, refresh: bool = False) -> list[dict[str, Any]]:
        backend = backend or self.backends[0]
        cache_key = f"keys:{backend.id}:{user_id}"
        if not refresh:
            hit, value, _ = self._key_cache.get(cache_key)
            if hit:
                return value

        inflight_tasks = getattr(self, "_key_list_inflight", None)
        if inflight_tasks is None:
            inflight_tasks = self._key_list_inflight = {}
        cache_versions = getattr(self, "_key_cache_versions", None)
        if cache_versions is None:
            cache_versions = self._key_cache_versions = {}
        cache_version = cache_versions.get(cache_key, 0)
        inflight_key = (cache_key, cache_version)
        task = inflight_tasks.get(inflight_key)
        if task is None:
            task = asyncio.create_task(self._load_keys_for_user(user_id, backend, cache_key, cache_version))
            inflight_tasks[inflight_key] = task
            task.add_done_callback(
                lambda finished, key=inflight_key: inflight_tasks.pop(key, None)
                if inflight_tasks.get(key) is finished
                else None
            )
        return await asyncio.shield(task)

    def invalidate_key_cache(self, user_id: str, backend: LiteLLMBackend | None = None) -> None:
        backend = backend or self.backends[0]
        cache_key = f"keys:{backend.id}:{user_id}"
        cache_versions = getattr(self, "_key_cache_versions", None)
        if cache_versions is None:
            cache_versions = self._key_cache_versions = {}
        cache_versions[cache_key] = cache_versions.get(cache_key, 0) + 1
        self._key_cache.delete(cache_key)

    async def key_user_info(self, user_id: str, backend: LiteLLMBackend | None = None) -> dict[str, Any]:
        backend = backend or self.backends[0]
        try:
            payload = await self.request_backend(backend, "GET", "/v2/user/info", params={"user_id": user_id})
            return payload if isinstance(payload, dict) else {}
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            payload = await self.request_backend(backend, "GET", "/user/info", params={"user_id": user_id})
            user_info = payload.get("user_info") if isinstance(payload, dict) else {}
            return user_info if isinstance(user_info, dict) else {}

    async def key_user_models(self, user_id: str, backend: LiteLLMBackend | None = None) -> list[str]:
        user_info = await self.key_user_info(user_id, backend)
        models = user_info.get("models") if isinstance(user_info, dict) else []
        return self._clean_model_list(models)

    @staticmethod
    def _clean_model_list(models: Any) -> list[str]:
        if not isinstance(models, list):
            return []
        return sorted({_clean_text(model) for model in models if _clean_text(model)})

    async def _proxy_model_names(self, backend: LiteLLMBackend) -> list[str]:
        cache_key = f"proxy_model_names:{backend.id}"
        hit, value, _ = self._model_cache.get(cache_key)
        if hit:
            return value
        payload = await self.request_backend(backend, "GET", "/models")
        model_names = {
            _clean_text(_first(item, "id", "model_name", "model", default=""))
            for item in _records(payload)
        }
        names = sorted(model for model in model_names if model)
        self._model_cache.set(cache_key, names, _env_int("MODEL_CACHE_TTL_SECONDS", 1800))
        return names

    async def organization_token_models(self) -> list[str]:
        """网关上真实存在的模型名，供企业令牌的可选目录使用。

        返回的是**上游原始名**：内部线路别名正是调用时唯一可用的模型名，只有展示
        才需要 ``model_display_name()`` 脱敏。单个 backend 失败不影响其余，全部失
        败时返回空列表，由调用方决定回落策略。
        """
        names: set[str] = set()
        for backend in self.backends:
            try:
                names.update(await self._proxy_model_names(backend))
            except HTTPException:
                continue
            except Exception:
                logger.warning("organization token model catalog failed for backend %s", backend.id)
                continue
        return sorted(names)

    def _team_ids_from_user_info(self, user_info: dict[str, Any]) -> list[str]:
        raw_values: list[Any] = []
        for key in ("teams", "team_ids", "team_id"):
            value = user_info.get(key)
            if isinstance(value, list):
                raw_values.extend(value)
            elif value:
                raw_values.append(value)
        team_ids: list[str] = []
        for value in raw_values:
            if isinstance(value, dict):
                team_id = _clean_text(_first(value, "team_id", "id", default=""))
            else:
                team_id = _clean_text(value)
            if team_id and team_id not in team_ids:
                team_ids.append(team_id)
        return team_ids

    async def _team_key_models(self, backend: LiteLLMBackend, user_info: dict[str, Any]) -> list[str]:
        models: set[str] = set()
        for team_id in self._team_ids_from_user_info(user_info):
            try:
                team = await self.team_info(backend, team_id)
            except HTTPException as exc:
                if exc.status_code == 404:
                    continue
                raise
            if not isinstance(team, dict):
                continue
            models.update(self._clean_model_list(team.get("models")))
        return sorted(models)

    async def key_model_scope(self, user_id: str, backend: LiteLLMBackend | None = None) -> KeyModelScope:
        backend = backend or self.backends[0]
        user_info = await self.key_user_info(user_id, backend)
        user_models = self._clean_model_list(user_info.get("models"))
        proxy_models: list[str] | None = None

        if ALL_PROXY_MODELS in user_models:
            proxy_models = await self._proxy_model_names(backend)
            return KeyModelScope(proxy_models, True)

        explicit_user_models = [model for model in user_models if model != NO_DEFAULT_MODELS]
        if explicit_user_models:
            return KeyModelScope(explicit_user_models, False)

        team_models = await self._team_key_models(backend, user_info)
        if ALL_PROXY_MODELS in team_models:
            proxy_models = await self._proxy_model_names(backend)
            return KeyModelScope(proxy_models, True)

        explicit_team_models = [model for model in team_models if model != NO_DEFAULT_MODELS]
        if explicit_team_models:
            return KeyModelScope(sorted(set(explicit_team_models)), False)

        if not user_models:
            proxy_models = await self._proxy_model_names(backend)
            return KeyModelScope(proxy_models, True)

        return KeyModelScope([], False)

    async def available_key_models(self, user_id: str, backend: LiteLLMBackend | None = None) -> tuple[list[str], bool]:
        scope = await self.key_model_scope(user_id, backend)
        return scope.models, scope.unrestricted

    async def set_user_budget(
        self,
        user_id: str,
        max_budget: float,
        backend: LiteLLMBackend | None = None,
    ) -> None:
        """把账户累计充值额度写成上游的用户级消费上限。

        上游 ``max_budget`` 与 ``spend`` 都是累计值，两者相减才是可用余额，这与
        看板既有的 spend 语义一致。因此这里写入的是"累计已充值"，不是"当前余额"。
        """
        backend = backend or self.backends[0]
        target = str(user_id).strip()
        if not target:
            raise HTTPException(status_code=400, detail="缺少账号标识，无法写入额度")
        await self.request_backend(
            backend,
            "POST",
            "/user/update",
            json={"user_id": target, "max_budget": max(0.0, float(max_budget))},
        )
        self.invalidate_key_cache(target, backend)

    async def grant_default_models(
        self,
        user_id: str,
        models: list[str],
        backend: LiteLLMBackend | None = None,
    ) -> list[str]:
        """首次充值时解除 ``no-default-models`` 限制。

        只在账号仍未获得任何真实模型权限时写入，已被管理员单独开通过模型的账号
        保持原样，避免充值把更宽的权限收窄回默认集。返回实际生效的模型列表，未
        改动时返回空列表。
        """
        backend = backend or self.backends[0]
        target = str(user_id).strip()
        desired = sorted({str(item).strip() for item in models if str(item).strip()})
        if not target or not desired:
            return []
        info = await self.key_user_info(target, backend)
        current = self._clean_model_list(info.get("models"))
        existing = [model for model in current if model != NO_DEFAULT_MODELS]
        if existing:
            return []
        await self.request_backend(
            backend,
            "POST",
            "/user/update",
            json={"user_id": target, "models": desired},
        )
        self.invalidate_key_cache(target, backend)
        return desired

    async def raise_key_daily_budgets(
        self,
        user_id: str,
        daily_budget: float,
        backend: LiteLLMBackend | None = None,
        changed_by: str = "billing-topup",
    ) -> list[str]:
        """把该账号名下访问密钥的每日额度抬高到给定值。

        只升不降：已经拥有更高日额度的密钥保持不动，避免充值反而收紧限额。返回
        实际被调整的密钥编号。
        """
        backend = backend or self.backends[0]
        target = str(user_id).strip()
        if not target:
            return []
        ceiling = max(0.0, float(daily_budget))
        if ceiling <= 0:
            return []
        keys = await self.keys_for_user(target, backend, refresh=True)
        adjusted: list[str] = []
        for item in keys:
            key_id = _clean_text(item.get("id"))
            if not key_id or item.get("status") != "正常":
                continue
            rotation = item.get("_rotation")
            current = rotation.get("max_budget") if isinstance(rotation, dict) else None
            try:
                current_value = float(current) if current is not None else 0.0
            except (TypeError, ValueError):
                current_value = 0.0
            if current_value >= ceiling:
                continue
            await self.request_backend(
                backend,
                "POST",
                "/key/update",
                headers={"litellm-changed-by": changed_by},
                json={
                    "key": key_id,
                    "max_budget": ceiling,
                    "budget_duration": DEFAULT_PERSONAL_KEY_BUDGET_DURATION,
                },
            )
            adjusted.append(key_id)
        if adjusted:
            self.invalidate_key_cache(target, backend)
        return adjusted

    async def ensure_personal_key_budget(
        self,
        backend: LiteLLMBackend,
        key_id: str,
        changed_by: str,
        user_id: str | None = None,
        max_budget: float = DEFAULT_PERSONAL_KEY_MAX_BUDGET,
        budget_duration: str = DEFAULT_PERSONAL_KEY_BUDGET_DURATION,
    ) -> None:
        if not key_id:
            raise HTTPException(status_code=502, detail="上游未返回访问密钥编号，无法确认每日额度")
        try:
            await self.request_backend(
                backend,
                "POST",
                "/key/update",
                headers={"litellm-changed-by": changed_by},
                json={
                    "key": key_id,
                    "max_budget": max_budget,
                    "budget_duration": budget_duration,
                },
            )
        except HTTPException as exc:
            status_code = 503 if exc.status_code >= 500 else 502
            raise HTTPException(status_code=status_code, detail="访问密钥已创建，但每日额度写入失败，请删除后重试") from exc
        if user_id:
            self.invalidate_key_cache(user_id, backend)

    async def create_key(
        self,
        user_id: str,
        name: str,
        purpose: str,
        duration: str,
        models: list[str],
        changed_by: str,
    ) -> dict[str, str]:
        backend, raw_user_id = self._decode_account_id(user_id)
        if backend.source:
            raise HTTPException(status_code=403, detail="该来源暂不支持在这里创建访问密钥")

        available_models, unrestricted = await self.available_key_models(raw_user_id, backend)
        selected_models = sorted({str(model).strip() for model in models if str(model).strip()})
        invalid_models = sorted(set(selected_models) - set(available_models))
        if invalid_models:
            raise HTTPException(status_code=400, detail=f"包含无权使用的模型：{', '.join(invalid_models)}")
        effective_models = selected_models or available_models
        if not effective_models:
            raise HTTPException(status_code=403, detail="当前账号没有可用于创建访问密钥的模型权限，请联系管理员开通模型权限。")

        body: dict[str, Any] = {
            "key_alias": f"ai-usage-{secrets.token_hex(8)}",
            "key_type": "llm_api",
            "user_id": raw_user_id,
            "models": effective_models,
            "max_budget": DEFAULT_PERSONAL_KEY_MAX_BUDGET,
            "budget_duration": DEFAULT_PERSONAL_KEY_BUDGET_DURATION,
            "metadata": {
                "display_name": name,
                "purpose": purpose,
                "created_via": "ai-usage-center",
            },
        }
        if duration != "never":
            body["duration"] = duration

        payload = await self.request_backend(
            backend,
            "POST",
            "/key/generate",
            headers={"litellm-changed-by": changed_by},
            json=body,
        )
        new_key = _clean_text(_first(payload, "key", default=""))
        token_id = _clean_text(_first(payload, "token_id", "token", default=""))
        if not new_key.startswith("sk-"):
            raise HTTPException(status_code=502, detail="上游未返回新的访问密钥")
        if not token_id or token_id.startswith("sk-"):
            token_id = safe_key_id(new_key)
        await self.ensure_personal_key_budget(backend, token_id, changed_by, raw_user_id)
        self.invalidate_key_cache(raw_user_id, backend)
        expires = _first(payload, "expires", default=None)
        return {
            "key": new_key,
            "id": token_id,
            "masked": mask_key(new_key),
            "expiresAt": _date_text(expires) if expires else "永不过期",
        }

    async def keys_for_user_ids(self, user_ids: list[str], refresh: bool = False) -> list[dict[str, Any]]:
        tasks = []
        for user_id in user_ids:
            backend, raw_user_id = self._decode_account_id(user_id)
            if backend.source:
                continue
            tasks.append(self.keys_for_user(raw_user_id, backend, refresh))
        batches = list(await asyncio.gather(*tasks))
        keys: list[dict[str, Any]] = []
        seen: set[str] = set()
        for batch in batches:
            for key in batch:
                key_id = key.get("id")
                if key_id and key_id not in seen:
                    seen.add(key_id)
                    keys.append(key)
        return keys

    async def regenerate_key(self, key_id: str, user_id: str, changed_by: str) -> dict[str, str]:
        backend, raw_user_id = self._decode_account_id(user_id)
        if backend.source:
            raise HTTPException(status_code=403, detail="该来源访问密钥暂不支持在这里更新")
        owned_keys = await self.keys_for_user(raw_user_id, backend, refresh=True)
        if not any(key["id"] == key_id for key in owned_keys):
            raise HTTPException(status_code=403, detail="不能更新不属于自己的访问密钥")
        try:
            payload = await self.request_backend(
                backend,
                "POST",
                f"/key/{quote(key_id, safe='')}/regenerate",
                headers={"litellm-changed-by": changed_by},
                json={"grace_period": "0s"},
            )
        except HTTPException as exc:
            detail = str(exc.detail).lower()
            if exc.status_code == 404 or "enterprise feature" in detail or "not_premium" in detail:
                raise HTTPException(status_code=501, detail="当前服务暂不支持再生成访问密钥，请联系管理员") from exc
            raise
        new_key = _first(payload, "key", "token", "api_key", default="")
        if not str(new_key).startswith("sk-"):
            raise HTTPException(status_code=502, detail="上游未返回新的访问密钥")
        new_key_id = _clean_text(_first(payload, "token_id", "token", default=""))
        if not new_key_id or new_key_id.startswith("sk-"):
            new_key_id = safe_key_id(new_key)
        self.invalidate_key_cache(raw_user_id, backend)
        return {"key": str(new_key), "id": new_key_id}

    async def supports_atomic_key_regeneration(self, user_id: str) -> bool:
        backend, _ = self._decode_account_id(user_id)
        if backend.source:
            return False
        try:
            payload = await self.request_backend(backend, "GET", "/health/license")
        except HTTPException as exc:
            if exc.status_code == 404:
                return False
            raise
        return isinstance(payload, dict) and str(payload.get("license_type") or "").lower() == "enterprise"

    @staticmethod
    def _remaining_key_duration(expires: Any) -> str | None:
        if not expires:
            return None
        try:
            parsed = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="旧密钥的过期时间无法安全继承，请新建密钥") from exc
        seconds = int((parsed - datetime.now(timezone.utc)).total_seconds())
        if seconds <= 0:
            raise HTTPException(status_code=409, detail="已过期的访问密钥不能更新，请新建密钥")
        return f"{seconds}s"

    async def create_replacement_key(self, key_id: str, user_id: str, changed_by: str) -> dict[str, str]:
        backend, raw_user_id = self._decode_account_id(user_id)
        if backend.source:
            raise HTTPException(status_code=403, detail="该来源访问密钥暂不支持在这里更新")
        owned_keys = await self.keys_for_user(raw_user_id, backend, refresh=True)
        owned = next((key for key in owned_keys if key.get("id") == key_id), None)
        if owned is None:
            raise HTTPException(status_code=403, detail="不能更新不属于自己的访问密钥")

        rotation = owned.get("_rotation") if isinstance(owned.get("_rotation"), dict) else {}
        if owned.get("status") != "正常":
            raise HTTPException(status_code=409, detail="只有正常状态的访问密钥可以更新")
        if rotation.get("budget_limits"):
            raise HTTPException(status_code=409, detail="旧密钥包含复杂预算规则，无法安全更新，请新建密钥")
        allowed_routes = rotation.get("allowed_routes") or []
        if allowed_routes and set(map(str, allowed_routes)) != {"llm_api_routes"}:
            raise HTTPException(status_code=409, detail="旧密钥包含自定义访问范围，无法安全更新，请新建密钥")
        if rotation.get("allowed_passthrough_routes"):
            raise HTTPException(status_code=409, detail="旧密钥包含自定义访问范围，无法安全更新，请新建密钥")
        available_models, unrestricted = await self.available_key_models(raw_user_id, backend)
        available_set = set(available_models)
        old_models = {str(model) for model in rotation.get("models") or [] if model}
        if unrestricted:
            effective_models = available_models if not old_models or ALL_PROXY_MODELS in old_models else sorted(old_models)
        elif not old_models or ALL_PROXY_MODELS in old_models:
            effective_models = available_models
        else:
            effective_models = sorted(old_models & available_set)
            if not effective_models:
                raise HTTPException(status_code=409, detail="旧密钥的模型权限已与当前员工权限不一致，请新建密钥")
        if not effective_models:
            raise HTTPException(status_code=403, detail="当前账号没有可用于创建访问密钥的模型权限，请联系管理员开通模型权限。")

        metadata = dict(rotation.get("metadata") or {})
        metadata["display_name"] = str(owned.get("name") or metadata.get("display_name") or "个人访问密钥")
        metadata["purpose"] = str(owned.get("purpose") or metadata.get("purpose") or "")
        metadata["created_via"] = "ai-usage-center"
        metadata["replaces_key_id"] = key_id
        body: dict[str, Any] = {
            "key_type": "llm_api",
            "user_id": raw_user_id,
            "models": effective_models,
            "metadata": metadata,
        }
        if key_alias := _clean_text(rotation.get("key_alias")):
            body["key_alias"] = key_alias
        duration = self._remaining_key_duration(rotation.get("expires"))
        if duration:
            body["duration"] = duration

        inherited_fields = (
            "max_budget",
            "spend",
            "budget_duration",
            "budget_limits",
            "budget_id",
            "max_parallel_requests",
            "tpm_limit",
            "rpm_limit",
            "allowed_cache_controls",
            "config",
            "permissions",
            "model_max_budget",
            "budget_fallbacks",
            "model_rpm_limit",
            "model_tpm_limit",
            "guardrails",
            "policies",
            "prompts",
            "aliases",
            "object_permission",
            "tags",
            "disable_global_guardrails",
            "enforced_params",
            "allowed_passthrough_routes",
            "allowed_vector_store_indexes",
            "rpm_limit_type",
            "tpm_limit_type",
            "router_settings",
            "access_group_ids",
            "team_id",
            "agent_id",
            "project_id",
        )
        for name in inherited_fields:
            if name in rotation:
                body[name] = rotation[name]
        if rotation.get("org_id"):
            body["organization_id"] = rotation["org_id"]
        body.setdefault("max_budget", DEFAULT_PERSONAL_KEY_MAX_BUDGET)
        body.setdefault("budget_duration", DEFAULT_PERSONAL_KEY_BUDGET_DURATION)

        payload = await self.request_backend(
            backend,
            "POST",
            "/key/generate",
            headers={"litellm-changed-by": changed_by},
            json=body,
        )
        new_key = _clean_text(_first(payload, "key", default=""))
        new_key_id = _clean_text(_first(payload, "token_id", "token", default=""))
        if not new_key.startswith("sk-"):
            raise HTTPException(status_code=502, detail="上游未返回新的访问密钥")
        if not new_key_id or new_key_id.startswith("sk-"):
            new_key_id = safe_key_id(new_key)
        await self.ensure_personal_key_budget(
            backend,
            new_key_id,
            changed_by,
            raw_user_id,
            max_budget=body["max_budget"],
            budget_duration=body["budget_duration"],
        )
        self.invalidate_key_cache(raw_user_id, backend)
        expires = _first(payload, "expires", default=None)
        return {
            "key": new_key,
            "id": new_key_id,
            "expiresAt": _date_text(expires) if expires else "永不过期",
        }

    async def disable_pending_old_key(
        self,
        old_key_id: str,
        replacement_key_id: str,
        user_id: str,
        changed_by: str,
    ) -> dict[str, str]:
        backend, raw_user_id = self._decode_account_id(user_id)
        owned_keys = await self.keys_for_user(raw_user_id, backend, refresh=True)
        owned_ids = {str(key.get("id") or "") for key in owned_keys}
        if replacement_key_id not in owned_ids:
            raise HTTPException(status_code=403, detail="替代密钥不属于当前员工，不能继续停用旧密钥")
        if old_key_id not in owned_ids:
            return {"id": old_key_id}
        return await self.delete_key(old_key_id, user_id, changed_by)

    async def delete_key(self, key_id: str, user_id: str, changed_by: str) -> dict[str, str]:
        backend, raw_user_id = self._decode_account_id(user_id)
        if backend.source:
            raise HTTPException(status_code=403, detail="该来源访问密钥暂不支持在这里删除")
        owned_keys = await self.keys_for_user(raw_user_id, backend, refresh=True)
        if not any(key["id"] == key_id for key in owned_keys):
            raise HTTPException(status_code=403, detail="不能删除不属于自己的访问密钥")

        payload = await self.request_backend(
            backend,
            "POST",
            "/key/delete",
            headers={"litellm-changed-by": changed_by},
            json={"keys": [key_id]},
        )
        deleted_keys = payload.get("deleted_keys") if isinstance(payload, dict) else None
        if not self._delete_confirmed(deleted_keys, key_id):
            raise HTTPException(status_code=502, detail="上游未确认访问密钥已删除")
        self.invalidate_key_cache(raw_user_id, backend)
        return {"id": key_id}

    async def block_key(self, key_id: str, user_id: str, changed_by: str) -> dict[str, str]:
        """停用指定访问密钥，密钥仍保留在上游列表中并标记为已禁用。"""
        backend, raw_user_id = self._decode_account_id(user_id)
        if backend.source:
            raise HTTPException(status_code=403, detail="该来源访问密钥暂不支持在这里停用")
        owned_keys = await self.keys_for_user(raw_user_id, backend, refresh=True)
        if not any(key["id"] == key_id for key in owned_keys):
            raise HTTPException(status_code=403, detail="不能停用不属于该成员的访问密钥")

        payload = await self.request_backend(
            backend,
            "POST",
            "/key/block",
            headers=self._management_headers(changed_by),
            json={"key": key_id},
        )
        blocked = payload.get("blocked") if isinstance(payload, dict) else None
        if blocked is False:
            raise HTTPException(status_code=502, detail="上游未确认访问密钥已停用")
        self.invalidate_key_cache(raw_user_id, backend)
        return {"id": key_id}

    @staticmethod
    def _delete_confirmed(deleted_keys: Any, key_id: str) -> bool:
        if isinstance(deleted_keys, list):
            return key_id in {str(item) for item in deleted_keys}
        if isinstance(deleted_keys, dict):
            return LiteLLMClient._delete_confirmed(deleted_keys.get("deleted_keys"), key_id)
        if isinstance(deleted_keys, int):
            return deleted_keys > 0
        return False

    async def users(self, backend: LiteLLMBackend | None = None) -> list[dict[str, Any]]:
        backend = backend or self.backends[0]
        # 上游默认按 created_at desc 排序，批量建号时这个字段大量相同，
        # offset 分页因此会同时返回重复行和漏掉账号（实测 1212 条里 78 条重复、
        # 等量账号缺失，导致姓名/邮箱补齐每轮同步都在抖动）。按主键 user_id
        # 排序分页才稳定；老版本上游不认排序参数时退回原有请求方式。
        sort_params: dict[str, Any] | None = {"sort_by": "user_id", "sort_order": "asc"}
        users: list[dict[str, Any]] = []
        seen_user_ids: set[str] = set()
        page = 1
        while page <= 100:
            params: dict[str, Any] = {"page": page, "page_size": 100, **(sort_params or {})}
            try:
                payload = await self.request_backend(backend, "GET", "/user/list", params=params)
            except Exception:
                if sort_params is None:
                    raise
                logger.warning(
                    "user list sorting unsupported on backend %s; retrying without sort", backend.id
                )
                sort_params = None
                users = []
                seen_user_ids = set()
                page = 1
                continue
            for record in _records(payload):
                user_id = _clean_text(record.get("user_id"))
                if user_id:
                    if user_id in seen_user_ids:
                        continue
                    seen_user_ids.add(user_id)
                users.append(record)
            total_pages = _as_int(payload.get("total_pages")) if isinstance(payload, dict) else 0
            if total_pages and page >= total_pages:
                break
            page += 1
        return users

    async def sync_rows_from_logs(
        self,
        start_date: str,
        end_date: str,
        backend: LiteLLMBackend | None = None,
        *,
        api_key: str | None = None,
    ) -> tuple[dict[str, list[dict[str, Any]]], bool]:
        """按北京时间日界扫描全量日志，返回 {user_id: usage rows} 与是否完整覆盖。

        上游 /user/daily/activity 的 date 是 UTC 日期（写入时直接切 startTime），
        忽略我们传入的 timezone 参数，因此北京时间 00:00-08:00 的用量会被归到前一天。
        这里改为拉原始日志、按 usage timezone 自行归日，消除 8 小时错位。

        一次全局扫描覆盖所有账号，避免按账号逐个查询导致的请求放大。
        """
        backend = backend or self.backends[0]
        await self._ensure_deployment_model_map(backend)
        utc_start, utc_end = _local_date_window_as_utc_text(start_date, end_date)
        # 上游 /spend/logs/v2 限制 page_size <= 100，单日约 800+ 页，需并发拉取。
        page_size = max(1, min(100, _env_int("USAGE_SYNC_LOG_PAGE_SIZE", 100)))
        max_pages = max(1, _env_int("USAGE_SYNC_LOG_MAX_PAGES", 5000))
        # LiteLLM's key-scoped spend-log pagination can expose a moving
        # snapshot when pages are fetched concurrently. Keep historical
        # imports deterministic; global scans retain the faster parallel path.
        concurrency = 1 if api_key else max(1, _env_int("USAGE_SYNC_LOG_CONCURRENCY", 8))

        async def fetch_page(page: int) -> tuple[list[dict[str, Any]], int]:
            params: dict[str, Any] = {
                "start_date": utc_start,
                "end_date": utc_end,
                "page": page,
                "page_size": page_size,
                "sort_by": "startTime",
                "sort_order": "asc",
            }
            if api_key:
                params["api_key"] = safe_key_id(api_key)
            payload = await self.request_backend(
                backend,
                "GET",
                "/spend/logs/v2",
                params=params,
            )
            total_pages = _as_int(_first(payload, "total_pages", "totalPages", default=0)) if isinstance(payload, dict) else 0
            return _records(payload), total_pages

        first_logs, total_pages = await fetch_page(1)
        pages_to_fetch = min(total_pages or 1, max_pages)
        truncated = bool(total_pages and total_pages > max_pages)

        grouped: dict[str, dict[tuple[str, str, str, str, str, str], dict[str, Any]]] = defaultdict(dict)
        event_rows: list[dict[str, Any]] = []

        def absorb(logs: list[dict[str, Any]]) -> None:
            for log in logs:
                user_id = self._log_raw_user(log) or "unattributed"
                day = _date_text_in_usage_timezone(_first(log, "startTime", "start_time", "created_at", "date"))
                # 并发分页取回的记录可能落在窗口外，按本地日界二次校验。
                if day < start_date or day > end_date:
                    continue
                source = backend.source or detect_source(log)
                model = self._usage_model_name(log, backend=backend)
                attribution = self._log_usage_attribution(log)
                event_time = str(
                    _first(
                        log,
                        "startTime",
                        "start_time",
                        "created_at",
                        "date",
                        default="",
                    )
                    or ""
                )
                request_id = _clean_text(
                    _first(
                        log,
                        "request_id",
                        "requestId",
                        "litellm_call_id",
                        "id",
                        default="",
                    )
                )
                if not request_id:
                    request_id = hashlib.sha256(
                        json.dumps(
                            {
                                "eventTime": event_time,
                                "userId": user_id,
                                "source": source,
                                "model": model,
                                "keyId": attribution["keyId"],
                                "spend": _first(log, "spend", "cost", "total_spend"),
                                "tokens": _first(log, "total_tokens", "totalTokens"),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        ).encode("utf-8")
                    ).hexdigest()
                event_row = self._empty_usage_row(day, source, model)
                event_row.update(attribution)
                event_row.update(
                    {
                        "requestId": request_id,
                        "eventTime": event_time,
                        "_userId": user_id,
                    }
                )
                self._add_log_to_row(event_row, log)
                event_rows.append(event_row)
                key = (
                    day,
                    source,
                    model,
                    attribution["organizationId"],
                    attribution["teamId"],
                    attribution["keyId"],
                )
                bucket = grouped[user_id]
                row = bucket.get(key)
                if row is None:
                    row = self._empty_usage_row(day, source, model)
                    row.update(attribution)
                    row["eventTime"] = event_time
                    bucket[key] = row
                else:
                    current_event_time = str(row.get("eventTime") or "")
                    if event_time and (
                        not current_event_time or event_time < current_event_time
                    ):
                        row["eventTime"] = event_time
                self._add_log_to_row(row, log)

        absorb(first_logs)

        if pages_to_fetch > 1:
            semaphore = asyncio.Semaphore(concurrency)

            async def load(page: int) -> list[dict[str, Any]]:
                async with semaphore:
                    try:
                        logs, _ = await fetch_page(page)
                        return logs
                    except HTTPException:
                        logger.exception("usage log page %s failed backend=%s", page, backend.id)
                        raise

            batches = await asyncio.gather(*(load(page) for page in range(2, pages_to_fetch + 1)))
            for batch in batches:
                absorb(batch)

        logger.info(
            "usage log scan backend=%s pages=%s/%s users=%s start=%s end=%s truncated=%s",
            backend.id,
            pages_to_fetch,
            total_pages,
            len(grouped),
            start_date,
            end_date,
            truncated,
        )
        if truncated:
            logger.warning(
                "usage log scan truncated backend=%s total_pages=%s max_pages=%s; "
                "raise USAGE_SYNC_LOG_MAX_PAGES to cover the full window",
                backend.id,
                total_pages,
                max_pages,
            )
        result = UsageLogRows({
            user_id: sorted(bucket.values(), key=lambda item: (item["date"], item["source"], item["model"]))
            for user_id, bucket in grouped.items()
        }, events=event_rows)
        return result, not truncated

    async def admin_daily_activity_rows(self, start_date: str, end_date: str, backend: LiteLLMBackend | None = None) -> list[dict[str, Any]]:
        backend = backend or self.backends[0]
        payload = await self.request_backend(
            backend,
            "GET",
            "/user/daily/activity/aggregated",
            params={
                "start_date": start_date,
                "end_date": end_date,
                "timezone": usage_timezone_offset_minutes(),
            },
        )
        rows = [self._row_from_daily_activity_item(item, backend.source or "其他", "全量") for item in _records(payload)]
        return sorted(rows, key=lambda item: (item["date"], item["model"]))

    async def admin_usage_rows(self, start_date: str, end_date: str, source: str | None, employee: str | None = None) -> dict[str, Any]:
        employee_filter = (employee or "").strip().lower()
        grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        employees: dict[str, dict[str, Any]] = {}
        # 员工排行有部门列，所以每一行都要部门归属。team_map 与 users 并行取，
        # 且 teams() 自带 TTL 缓存，不会给每次看板刷新都加一次串行往返。
        department_names: dict[str, list[str]] = {}
        max_pages = max(1, int(os.getenv("ADMIN_USAGE_LOG_MAX_PAGES", "30")))
        page_size = max(1, min(100, int(os.getenv("ADMIN_USAGE_PAGE_SIZE", "100"))))
        utc_start, utc_end = _local_date_window_as_utc_text(start_date, end_date)
        pages_read = 0
        total_pages = 0
        total_records = 0

        for backend in self.backends:
            if backend.source and _source_filter_applies(source) and source != backend.source:
                continue
            # Model directory, users, team map and account index are independent.
            if backend.source == "Her":
                users, account_index, team_map, _ = await asyncio.gather(
                    self.users(backend),
                    self.her_account_index(backend),
                    self._team_map_or_empty(backend),
                    self._ensure_deployment_model_map(backend),
                )
            else:
                users, team_map, _ = await asyncio.gather(
                    self.users(backend),
                    self._team_map_or_empty(backend),
                    self._ensure_deployment_model_map(backend),
                )
                account_index = None
            user_map = self._admin_user_map(users)
            backend_pages_read = 0
            backend_total_pages = 0
            backend_total_records = 0

            for page in range(1, max_pages + 1):
                payload = await self.request_backend(
                    backend,
                    "GET",
                    "/spend/logs/v2",
                    params={
                        "start_date": utc_start,
                        "end_date": utc_end,
                        "page": page,
                        "page_size": page_size,
                        "sort_by": "startTime",
                        "sort_order": "desc",
                    },
                )
                backend_pages_read = page
                if isinstance(payload, dict):
                    backend_total_pages = _as_int(_first(payload, "total_pages", "totalPages", default=backend_total_pages))
                    backend_total_records = _as_int(_first(payload, "total", "total_count", "count", default=backend_total_records))
                logs = _records(payload)
                if not logs:
                    break
                for log in logs:
                    employee_info = self._employee_info_from_log(log, user_map, backend, account_index)
                    if employee_filter and not self._admin_employee_matches(employee_info, employee_filter):
                        continue
                    detected_source = backend.source or detect_source(log)
                    if source and source != "all" and detected_source != source:
                        continue

                    employee_key = employee_info["id"]
                    employees.setdefault(employee_key, employee_info)
                    self._collect_department_name(department_names, employee_key, log, team_map)
                    model = self._usage_model_name(log, backend=backend)
                    day = _date_text_in_usage_timezone(_first(log, "startTime", "start_time", "created_at", "date"))
                    key = (day, employee_key, detected_source, model)
                    row = grouped.setdefault(key, self._admin_empty_row(day, employee_info, detected_source, model))
                    self._add_log_to_row(row, log)

                if backend_total_pages and page >= backend_total_pages:
                    break

            pages_read = max(pages_read, backend_pages_read)
            total_pages = max(total_pages, backend_total_pages)
            total_records += backend_total_records

        rows = sorted(grouped.values(), key=lambda item: (item["date"], item["employeeName"], item["source"], item["model"]))
        summary_rows: list[dict[str, Any]] = []
        if (not employee_filter) and (not source or source == "all"):
            for backend in self.backends:
                try:
                    summary_rows.extend(await self.admin_daily_activity_rows(start_date, end_date, backend))
                except HTTPException:
                    continue
        truncated = bool(total_pages and pages_read < total_pages)
        return {
            "rows": rows,
            "summaryRows": summary_rows or rows,
            "employees": self._admin_employee_summaries(rows, employees, department_names),
            "pageLimit": max_pages,
            "pageSize": page_size,
            "pagesRead": pages_read,
            "totalPages": total_pages,
            "totalRecords": total_records,
            "truncated": truncated,
            "dataQuality": {
                "summarySource": "official_daily_activity" if summary_rows else "spend_logs",
                "rankingSource": "spend_logs",
                "timezoneOffsetMinutes": usage_timezone_offset_minutes(),
            },
        }

    async def admin_usage_compare(self, start_date: str, end_date: str, source: str | None) -> dict[str, Any]:
        payload = await self.admin_usage_rows(start_date, end_date, source, None)
        rows = payload.get("rows", [])
        summary_rows = payload.get("summaryRows", [])
        employee_ids = {str(row.get("employeeId") or "") for row in rows}
        employee_emails = {str(row.get("employeeEmail") or "").lower() for row in rows if row.get("employeeEmail")}
        return {
            "startDate": start_date,
            "endDate": end_date,
            "source": source or "all",
            "officialDailyActivity": self._usage_totals(summary_rows),
            "spendLogs": self._usage_totals(rows),
            "truncated": payload.get("truncated", False),
            "pagesRead": payload.get("pagesRead", 0),
            "totalPages": payload.get("totalPages", 0),
            "totalRecords": payload.get("totalRecords", 0),
            "employeesAfterMerge": len(employee_ids),
            "boundEmailCount": len(employee_emails),
            "dataQuality": payload.get("dataQuality", {}),
        }

    async def team_map(self, backend: LiteLLMBackend | None = None) -> dict[str, dict[str, str]]:
        backend = backend or self.backends[0]
        mapping: dict[str, dict[str, str]] = {}
        for team in await self.teams(backend, include_details=False):
            team_id = str(_first(team, "team_id", "id", default="") or "").strip()
            if not team_id:
                continue
            team_alias = str(_first(team, "team_alias", "alias", "name", default="") or "").strip()
            mapping[team_id.lower()] = {"id": team_id, "name": team_alias or team_id}
        return mapping

    async def team_info(self, backend: LiteLLMBackend, team_id: str) -> dict[str, Any] | None:
        cache_key = f"{backend.id}:{team_id}"
        details_cache = getattr(self, "_team_details_cache", None)
        if details_cache is None:
            details_cache = self._team_details_cache = TTLCache()
        hit, cached, _ = details_cache.get(cache_key)
        if hit:
            return cached
        payload = await self.request_backend(backend, "GET", "/team/info", params={"team_id": team_id})
        if not isinstance(payload, dict):
            return None
        team_info = payload.get("team_info")
        if isinstance(team_info, dict):
            team_info.setdefault("team_id", payload.get("team_id") or team_id)
            details_cache.set(cache_key, team_info, _env_int("TEAM_DETAILS_CACHE_TTL_SECONDS", 300))
            return team_info
        if payload.get("team_id") or payload.get("members_with_roles") is not None:
            payload.setdefault("team_id", team_id)
            details_cache.set(cache_key, payload, _env_int("TEAM_DETAILS_CACHE_TTL_SECONDS", 300))
            return payload
        return None

    async def _teams_with_details(self, backend: LiteLLMBackend, teams: list[dict[str, Any]]) -> list[dict[str, Any]]:
        async def resolve(team: dict[str, Any]) -> dict[str, Any]:
            team_id = str(_first(team, "team_id", "id", default="") or "").strip()
            if not team_id:
                return team
            if self._team_members(team):
                return team
            try:
                full_team = await self.team_info(backend, team_id)
            except HTTPException:
                full_team = None
            return full_team or team

        return list(await asyncio.gather(*(resolve(team) for team in teams)))

    async def teams(self, backend: LiteLLMBackend | None = None, include_details: bool = True) -> list[dict[str, Any]]:
        backend = backend or self.backends[0]
        for path in ("/v2/team/list", "/team/list"):
            teams: list[dict[str, Any]] = []
            for page in range(1, 51):
                try:
                    payload = await self.request_backend(backend, "GET", path, params={"page": page, "page_size": 100})
                except HTTPException:
                    break
                teams.extend(_records(payload))
                total_pages = _as_int(_first(payload, "total_pages", "totalPages", default=0)) if isinstance(payload, dict) else 0
                has_more = bool(payload.get("has_more")) if isinstance(payload, dict) else False
                if total_pages and page >= total_pages:
                    break
                if not total_pages and not has_more:
                    break
            if teams:
                return await self._teams_with_details(backend, teams) if include_details else teams
        return []

    def _team_summary(self, team: dict[str, Any], backend: LiteLLMBackend) -> dict[str, Any]:
        team_id = str(_first(team, "team_id", "id", default="") or "").strip()
        team_alias = str(_first(team, "team_alias", "alias", "name", default="") or "").strip()
        members = self._team_members(team)
        return {
            "id": team_id,
            "name": team_alias or team_id,
            "memberCount": len(members),
            "backend": backend.id,
        }

    def _team_members(self, team: dict[str, Any]) -> list[dict[str, Any]]:
        members = _first(team, "members_with_roles", "membersWithRoles", default=[])
        if isinstance(members, str):
            try:
                members = json.loads(members)
            except ValueError:
                members = []
        if not isinstance(members, list):
            return []
        return [member for member in members if isinstance(member, dict)]

    def _team_member_user_id(self, member: dict[str, Any]) -> str:
        return str(_first(member, "user_id", "userId", default="") or "").strip()

    def _team_member_email(self, member: dict[str, Any]) -> str:
        return _normal_email(_first(member, "user_email", "userEmail", "email", default=""))

    def _team_member_role(self, member: dict[str, Any]) -> str:
        return str(_first(member, "role", "user_role", "team_role", default="") or "").strip().lower()

    def _is_team_admin_role(self, member: dict[str, Any]) -> bool:
        return self._team_member_role(member) == "admin"

    def _accounts_by_backend(self, upstream_user: dict[str, Any]) -> dict[str, set[str]]:
        grouped: dict[str, set[str]] = defaultdict(set)
        accounts = upstream_user.get("matched_accounts")
        if isinstance(accounts, list):
            for account in accounts:
                if not isinstance(account, dict):
                    continue
                backend_id = str(account.get("backend") or "primary")
                user_id = str(account.get("user_id") or "").strip().lower()
                if user_id:
                    grouped[backend_id].add(user_id)
        if grouped:
            return grouped
        for account_id in upstream_user.get("matched_user_ids") or []:
            backend, raw_user_id = self._decode_account_id(str(account_id))
            if raw_user_id:
                grouped[backend.id].add(raw_user_id.strip().lower())
        return grouped

    def _account_emails_by_backend(self, upstream_user: dict[str, Any]) -> dict[str, set[str]]:
        grouped: dict[str, set[str]] = defaultdict(set)
        accounts = upstream_user.get("matched_accounts")
        if isinstance(accounts, list):
            for account in accounts:
                if not isinstance(account, dict):
                    continue
                email = _normal_email(_first(account, "user_email", "email", "sso_user_id", default=""))
                if email:
                    grouped[str(account.get("backend") or "primary")].add(email)
        for email in (
            _normal_email(_first(upstream_user, "user_email", "email", "sso_user_id", default="")),
            *[_normal_email(item) for item in upstream_user.get("matched_emails") or []],
        ):
            if email:
                for backend in self.backends:
                    grouped[backend.id].add(email)
        return grouped

    async def team_leader_scope(self, upstream_user: dict[str, Any]) -> dict[str, Any]:
        accounts_by_backend = self._accounts_by_backend(upstream_user)
        emails_by_backend = self._account_emails_by_backend(upstream_user)
        leader_teams: list[dict[str, Any]] = []
        primary = self.backends[0]
        primary_user_ids = accounts_by_backend.get(primary.id, set())
        primary_emails = emails_by_backend.get(primary.id, set())
        if not primary_user_ids and not primary_emails:
            return {"isTeamLeader": False, "teamBoardStatus": "none", "team": None, "leaderTeams": []}

        all_teams = await asyncio.gather(*(self.teams(backend) for backend in self.backends))
        primary_teams = all_teams[0]
        all_backend_teams = all_teams[1:]
        for team in primary_teams:
            team_id = str(_first(team, "team_id", "id", default="") or "").strip()
            team_name = str(_first(team, "team_alias", "alias", "name", default="") or "").strip() or team_id
            if not team_id:
                continue
            is_admin = any(
                self._is_team_admin_role(member)
                and (
                    self._team_member_user_id(member).lower() in primary_user_ids
                    or self._team_member_email(member) in primary_emails
                )
                for member in self._team_members(team)
            )
            if not is_admin:
                continue
            scopes = [{"backend": primary, **self._team_summary(team, primary)}]
            identity = team_identity_key(team_id, team_name)
            for backend, backend_teams in zip(self.backends[1:], all_backend_teams):
                for candidate in backend_teams:
                    candidate_id = str(_first(candidate, "team_id", "id", default="") or "").strip()
                    candidate_name = str(_first(candidate, "team_alias", "alias", "name", default="") or "").strip() or candidate_id
                    if candidate_id and team_identity_key(candidate_id, candidate_name) == identity:
                        scopes.append({"backend": backend, **self._team_summary(candidate, backend)})
                        break
            anchor = {"backend": primary, "team": team, **self._team_summary(team, primary), "teamScopes": scopes}
            leader_teams.append(anchor)

        if not leader_teams:
            return {"isTeamLeader": False, "teamBoardStatus": "none", "team": None, "leaderTeams": []}
        if len(leader_teams) > 1:
            return {
                "isTeamLeader": True,
                "teamBoardStatus": "multiple",
                "team": None,
                "leaderTeams": [{key: value for key, value in item.items() if key != "team"} for item in leader_teams],
            }
        only = leader_teams[0]
        return {
            "isTeamLeader": True,
            "teamBoardStatus": "single",
            "team": {key: value for key, value in only.items() if key != "team"},
            "leaderTeams": [{key: value for key, value in only.items() if key != "team"}],
        }

    def _department_info_from_log(self, log: dict[str, Any], team_map: dict[str, dict[str, str]]) -> dict[str, str]:
        metadata = _metadata_dict(_first(log, "metadata", "request_tags", "tags", default={}))
        team_id = str(
            _first(log, "team_id", "teamId", default="")
            or metadata.get("team_id")
            or metadata.get("teamId")
            or ""
        ).strip()
        team_alias = str(
            _first(log, "team_alias", "team_name", "teamName", default="")
            or metadata.get("team_alias")
            or metadata.get("team_name")
            or metadata.get("teamName")
            or ""
        ).strip()
        if team_id:
            known = team_map.get(team_id.lower())
            name = known.get("name", team_alias or team_id) if known else team_alias or team_id
            return {"id": team_id, "name": name, "key": department_key(team_id, name), "bindStatus": "已绑定部门"}

        department = str(
            _first(log, "department", "department_name", "departmentName", default="")
            or metadata.get("department")
            or metadata.get("department_name")
            or metadata.get("departmentName")
            or ""
        ).strip()
        if department:
            return {"id": department, "name": department, "key": department_key(department, department), "bindStatus": "来自部门字段"}

        org_id = str(
            _first(log, "organization_id", "org_id", "organizationId", "orgId", default="")
            or metadata.get("organization_id")
            or metadata.get("org_id")
            or metadata.get("organizationId")
            or metadata.get("orgId")
            or ""
        ).strip()
        if org_id:
            return {"id": org_id, "name": org_id, "key": department_key(org_id, org_id), "bindStatus": "来自组织字段"}
        return {"id": "unassigned", "name": "未绑定部门", "key": department_key("unassigned", "未绑定部门"), "bindStatus": "未绑定部门"}

    def _department_empty_row(self, day: str, department_info: dict[str, str], source: str, model: str, employee_info: dict[str, Any]) -> dict[str, Any]:
        row = self._admin_empty_row(day, employee_info, source, model)
        row.update(
            {
                "departmentId": department_info["id"],
                "departmentName": department_info["name"],
                "departmentKey": department_info.get("key") or department_key(department_info["id"], department_info["name"]),
                "departmentBindStatus": department_info["bindStatus"],
            }
        )
        return row

    def _department_sort_key(self, department: dict[str, Any]) -> tuple[float, float, float, str]:
        name = str(department.get("departmentName") or department.get("departmentId") or "")
        return (
            -_as_number(department.get("totalTokens")),
            -_as_number(department.get("spend")),
            -_as_number(department.get("requestCount")),
            name.lower(),
        )

    def _department_summaries(self, rows: list[dict[str, Any]], departments: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        source_totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        employees: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            department_id = str(row.get("departmentId") or "unassigned")
            department_name = str(row.get("departmentName") or department_id)
            logical_key = str(row.get("departmentKey") or department_key(department_id, department_name))
            department = departments.get(logical_key, {})
            summary = grouped.setdefault(
                logical_key,
                {
                    "departmentKey": logical_key,
                    "departmentId": department_id,
                    "departmentName": department.get("name") or department_name,
                    "bindStatus": department.get("bindStatus") or row.get("departmentBindStatus") or "未绑定部门",
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "totalTokens": 0,
                    "requestCount": 0,
                    "successCount": 0,
                    "failureCount": 0,
                    "spend": 0.0,
                    "primarySource": "其他",
                    "activeEmployees": 0,
                },
            )
            summary["promptTokens"] += _as_int(row.get("promptTokens"))
            summary["completionTokens"] += _as_int(row.get("completionTokens"))
            summary["totalTokens"] += _as_int(row.get("totalTokens"))
            summary["requestCount"] += _as_int(row.get("requestCount"))
            summary["successCount"] += _as_int(row.get("successCount"))
            summary["failureCount"] += _as_int(row.get("failureCount"))
            summary["spend"] += _as_number(row.get("spend"))
            source_totals[logical_key][str(row.get("source") or "其他")] += _as_int(row.get("totalTokens"))
            employee_id = str(row.get("employeeId") or row.get("employeeEmail") or "")
            if employee_id:
                employees[logical_key].add(employee_id)

        for department_id, summary in grouped.items():
            sources = source_totals.get(department_id, {})
            if sources:
                summary["primarySource"] = max(sources.items(), key=lambda item: item[1])[0]
            summary["activeEmployees"] = len(employees.get(department_id, set()))
        return sorted(grouped.values(), key=self._department_sort_key)

    def _team_daily_activity_rows_from_items(
        self,
        items: list[dict[str, Any]],
        department: str | None,
        team_map: dict[str, dict[str, str]],
        backend: LiteLLMBackend,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in items:
            metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else item
            breakdown = item.get("breakdown") if isinstance(item.get("breakdown"), dict) else {}
            entities = breakdown.get("entities") if isinstance(breakdown.get("entities"), dict) else {}
            if entities:
                for team_id, entity in entities.items():
                    entity_metrics = entity.get("metrics") if isinstance(entity, dict) and isinstance(entity.get("metrics"), dict) else entity
                    known = team_map.get(str(team_id).lower(), {})
                    rows.append(
                        {
                            "date": _date_text(_first(item, "date", "day")),
                            "source": backend.source or "\u5176\u4ed6",
                            "model": "\u5168\u91cf",
                            "promptTokens": _as_int(_first(entity_metrics, "prompt_tokens", "promptTokens", "total_prompt_tokens")),
                            "completionTokens": _as_int(_first(entity_metrics, "completion_tokens", "completionTokens", "total_completion_tokens")),
                            "totalTokens": _as_int(_first(entity_metrics, "total_tokens", "totalTokens")),
                            "requestCount": _as_int(_first(entity_metrics, "api_requests", "total_api_requests", "requestCount")),
                            "successCount": _as_int(_first(entity_metrics, "successful_requests", "total_successful_requests", "successCount")),
                            "failureCount": _as_int(_first(entity_metrics, "failed_requests", "total_failed_requests", "failureCount")),
                            "spend": _as_number(_first(entity_metrics, "spend", "total_spend")),
                            "departmentId": str(team_id),
                            "departmentName": known.get("name") or str(team_id),
                            "departmentBindStatus": "\u5df2\u7ed1\u5b9a\u90e8\u95e8",
                        }
                    )
            else:
                row = self._row_from_daily_activity_item(item, "\u5176\u4ed6", "\u5168\u91cf")
                row["source"] = backend.source or row["source"]
                team_id = str(_first(item, "team_id", "teamId", default=department or "") or department or "all")
                known = team_map.get(team_id.lower(), {})
                row.update({"departmentId": team_id, "departmentName": known.get("name") or team_id, "departmentBindStatus": "\u5df2\u7ed1\u5b9a\u90e8\u95e8"})
                rows.append(row)
        return rows

    async def _team_daily_activity_rows(
        self,
        start_date: str,
        end_date: str,
        department: str | None,
        team_map: dict[str, dict[str, str]],
        backend: LiteLLMBackend | None = None,
    ) -> list[dict[str, Any]]:
        backend = backend or self.backends[0]
        rows: list[dict[str, Any]] = []
        max_pages = max(1, _env_int("TEAM_DAILY_ACTIVITY_MAX_PAGES", 50))
        page_size = 100

        for page in range(1, max_pages + 1):
            params: dict[str, Any] = {"start_date": start_date, "end_date": end_date, "page": page, "page_size": page_size}
            if department and department != "unassigned":
                params["team_ids"] = department
            payload = await self.request_backend(backend, "GET", "/team/daily/activity", params=params)
            items = _records(payload)
            if not items:
                break

            rows.extend(self._team_daily_activity_rows_from_items(items, department, team_map, backend))

            metadata = _metadata_dict(payload.get("metadata")) if isinstance(payload, dict) else {}
            total_pages = _as_int(_first(metadata, "total_pages", "totalPages", default=_first(payload, "total_pages", "totalPages", default=0)))
            has_more_raw = _first(metadata, "has_more", "hasMore", default=_first(payload, "has_more", "hasMore", default=None))
            has_more = bool(has_more_raw)
            if isinstance(has_more_raw, str):
                has_more = has_more_raw.strip().lower() in {"1", "true", "yes", "on"}

            if total_pages and page >= total_pages:
                break
            if not total_pages:
                if has_more_raw is not None and not has_more:
                    break
                if has_more_raw is None and len(items) < page_size:
                    break
        return rows

    async def admin_department_usage_rows(self, start_date: str, end_date: str, source: str | None, department: str | None = None) -> dict[str, Any]:
        department_filter = (department or "").strip().lower()
        grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        departments: dict[str, dict[str, str]] = {}
        employees: dict[str, dict[str, Any]] = {}
        max_pages = max(1, int(os.getenv("ADMIN_USAGE_LOG_MAX_PAGES", "30")))
        page_size = max(1, min(100, int(os.getenv("ADMIN_USAGE_PAGE_SIZE", "100"))))
        utc_start, utc_end = _local_date_window_as_utc_text(start_date, end_date)
        pages_read = 0
        total_pages = 0
        total_records = 0
        department_options: dict[str, dict[str, Any]] = {}

        for backend in self.backends:
            try:
                for team in await self.teams(backend, include_details=False):
                    team_id = str(_first(team, "team_id", "teamId", "id", default="") or "").strip()
                    blocked = _first(team, "blocked", default=False)
                    if isinstance(blocked, str):
                        blocked = blocked.strip().lower() in {"1", "true", "yes", "on"}
                    if not team_id or bool(blocked):
                        continue
                    team_name = str(_first(team, "team_alias", "teamAlias", "alias", "name", default="") or team_id).strip()
                    logical_key = department_key(team_id, team_name)
                    department_options.setdefault(logical_key, {
                        "departmentKey": logical_key,
                        "departmentId": team_id,
                        "departmentName": team_name,
                        "organizationId": str(_first(team, "organization_id", "organizationId", "org_id", "orgId", default="") or ""),
                        "status": "active",
                    })
            except Exception:
                logger.debug("failed to load department directory for backend %s", backend.id, exc_info=True)
            if backend.source and _source_filter_applies(source) and source != backend.source:
                continue
            # Load the model directory with the other independent backend metadata.
            if backend.source == "Her":
                users, team_map, account_index, _ = await asyncio.gather(
                    self.users(backend),
                    self.team_map(backend),
                    self.her_account_index(backend),
                    self._ensure_deployment_model_map(backend),
                )
            else:
                users, team_map, _ = await asyncio.gather(
                    self.users(backend),
                    self.team_map(backend),
                    self._ensure_deployment_model_map(backend),
                )
                account_index = None
            user_map = self._admin_user_map(users)
            backend_pages_read = 0
            backend_total_pages = 0
            backend_total_records = 0

            for page in range(1, max_pages + 1):
                payload = await self.request_backend(
                    backend,
                    "GET",
                    "/spend/logs/v2",
                    params={
                        "start_date": utc_start,
                        "end_date": utc_end,
                        "page": page,
                        "page_size": page_size,
                        "sort_by": "startTime",
                        "sort_order": "desc",
                    },
                )
                backend_pages_read = page
                if isinstance(payload, dict):
                    backend_total_pages = _as_int(_first(payload, "total_pages", "totalPages", default=backend_total_pages))
                    backend_total_records = _as_int(_first(payload, "total", "total_count", "count", default=backend_total_records))
                logs = _records(payload)
                if not logs:
                    break
                for log in logs:
                    department_info = self._department_info_from_log(log, team_map)
                    if department_filter and department_filter not in {department_info["key"], normalize_team_text(department_info["id"]), normalize_team_text(department_info["name"])}:
                        continue
                    detected_source = backend.source or detect_source(log)
                    if source and source != "all" and detected_source != source:
                        continue

                    employee_info = self._employee_info_from_log(log, user_map, backend, account_index)
                    department_id = department_info["id"]
                    logical_key = department_info["key"]
                    departments.setdefault(logical_key, department_info)
                    employees.setdefault(employee_info["id"], employee_info)
                    model = self._usage_model_name(log, backend=backend)
                    day = _date_text_in_usage_timezone(_first(log, "startTime", "start_time", "created_at", "date"))
                    key = (day, logical_key, employee_info["id"], detected_source, model)
                    row = grouped.setdefault(key, self._department_empty_row(day, department_info, detected_source, model, employee_info))
                    self._add_log_to_row(row, log)

                if backend_total_pages and page >= backend_total_pages:
                    break

            pages_read = max(pages_read, backend_pages_read)
            total_pages = max(total_pages, backend_total_pages)
            total_records += backend_total_records

        rows = sorted(grouped.values(), key=lambda item: (item["date"], item["departmentName"], item["employeeName"], item["source"], item["model"]))
        summary_rows: list[dict[str, Any]] = []
        if not source or source == "all":
            for backend in self.backends:
                try:
                    team_map = await self.team_map(backend)
                    backend_summary_rows = await self._team_daily_activity_rows(start_date, end_date, department, team_map, backend)
                    if department_filter:
                        backend_summary_rows = [
                            row
                            for row in backend_summary_rows
                            if department_filter in {str(row.get("departmentId", "")).lower(), str(row.get("departmentName", "")).lower()}
                        ]
                    summary_rows.extend(backend_summary_rows)
                except HTTPException:
                    continue

        truncated = bool(total_pages and pages_read < total_pages)
        department_summaries = self._department_summaries(rows, departments)
        summaries_by_id = {str(item.get("departmentId") or ""): item for item in department_summaries}
        for option in department_options.values():
            option.update(summaries_by_id.get(str(option.get("departmentId") or ""), {}))
        return {
            "rows": rows,
            "summaryRows": summary_rows or rows,
            "departments": department_summaries,
            "departmentOptions": sorted(department_options.values(), key=lambda item: (str(item["departmentName"]).casefold(), str(item["departmentId"]))),
            "employees": self._admin_employee_summaries(rows, employees),
            "pageLimit": max_pages,
            "pageSize": page_size,
            "pagesRead": pages_read,
            "totalPages": total_pages,
            "totalRecords": total_records,
            "truncated": truncated,
            "dataQuality": {
                "summarySource": "team_daily_activity" if summary_rows else "spend_logs",
                "rankingSource": "spend_logs",
                "timezoneOffsetMinutes": usage_timezone_offset_minutes(),
            },
        }

    def _team_member_employee_info(self, member: dict[str, Any], user_map: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        user_id = self._team_member_user_id(member)
        email = str(_first(member, "user_email", "userEmail", default="") or "").strip().lower()
        if user_id and user_id.lower() in user_map:
            return user_map[user_id.lower()]
        if email and email in user_map:
            return user_map[email]
        if not user_id and not email:
            return None
        name = str(_first(member, "user_alias", "userAlias", "name", default="") or "").strip()
        return {
            "id": email or user_id,
            "name": name or (email.split("@", 1)[0] if email else user_id),
            "email": email,
            "bindStatus": "已绑定邮箱" if email else "未绑定邮箱",
            "userIds": [user_id] if user_id else [],
        }

    def _admin_employee_summaries_with_zeroes(self, rows: list[dict[str, Any]], employees: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        summaries: dict[str, dict[str, Any]] = {}
        for item in self._admin_employee_summaries(rows, employees):
            email = str(item.get("employeeEmail") or "").strip().lower()
            identity = email or str(item.get("employeeId") or "").strip().lower()
            existing = summaries.get(identity)
            if existing is None:
                summaries[identity] = item
                continue
            for field in ("promptTokens", "completionTokens", "totalTokens", "requestCount", "successCount", "failureCount", "spend"):
                existing[field] += item.get(field) or 0
            for user_id in item.get("userIds") or []:
                if user_id not in existing["userIds"]:
                    existing["userIds"].append(user_id)
            for name in item.get("departmentNames") or []:
                if name not in existing["departmentNames"]:
                    existing["departmentNames"].append(name)

        for employee_id, employee in employees.items():
            email = str(employee.get("email") or "").strip().lower()
            identity = email or str(employee_id).strip().lower()
            summary = summaries.setdefault(
                identity,
                {
                    "employeeId": employee.get("id") or employee_id,
                    "employeeName": employee.get("name") or employee_id,
                    "employeeEmail": email,
                    "bindStatus": employee.get("bindStatus") or "未绑定邮箱",
                    "userIds": [],
                    "departmentNames": [],
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "totalTokens": 0,
                    "requestCount": 0,
                    "successCount": 0,
                    "failureCount": 0,
                    "spend": 0.0,
                    "primarySource": "其他",
                    "teamRole": employee.get("teamRole") or "user",
                },
            )
            for user_id in employee.get("userIds") or []:
                if user_id not in summary["userIds"]:
                    summary["userIds"].append(user_id)
            if not summary.get("employeeEmail") and email:
                summary["employeeEmail"] = email
                summary["bindStatus"] = "已绑定邮箱"
            if summary.get("teamRole") != "admin" and employee.get("teamRole") == "admin":
                summary["teamRole"] = "admin"
        return sorted(summaries.values(), key=self._admin_employee_sort_key)

    async def _team_usage_rows_single(
        self,
        backend_id: str,
        team_id: str,
        start_date: str,
        end_date: str,
        source: str | None,
    ) -> dict[str, Any]:
        backend = self._backend_map.get(backend_id)
        if backend is None:
            raise HTTPException(status_code=403, detail="当前团队权限已失效，请重新登录")
        teams = await self.teams(backend)
        team = next((item for item in teams if str(_first(item, "team_id", "id", default="") or "") == team_id), None)
        if team is None:
            raise HTTPException(status_code=404, detail="未找到当前负责的团队")

        # Load the model directory with the other independent backend metadata.
        if backend.source == "Her":
            user_map_users, account_index, _ = await asyncio.gather(
                self.users(backend),
                self.her_account_index(backend),
                self._ensure_deployment_model_map(backend),
            )
        else:
            user_map_users, _ = await asyncio.gather(
                self.users(backend),
                self._ensure_deployment_model_map(backend),
            )
            account_index = None
        user_map = self._admin_user_map(user_map_users)
        team_info = self._team_summary(team, backend)
        employees: dict[str, dict[str, Any]] = {}
        for member in self._team_members(team):
            employee_info = self._team_member_employee_info(member, user_map)
            if employee_info:
                employee_info = dict(employee_info)
                employee_info["teamRole"] = self._team_member_role(member) or "user"
                employees.setdefault(employee_info["id"], employee_info)

        grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        max_pages = max(1, int(os.getenv("ADMIN_USAGE_LOG_MAX_PAGES", "30")))
        page_size = max(1, min(100, int(os.getenv("ADMIN_USAGE_PAGE_SIZE", "100"))))
        utc_start, utc_end = _local_date_window_as_utc_text(start_date, end_date)
        pages_read = 0
        total_pages = 0
        total_records = 0

        for page in range(1, max_pages + 1):
            payload = await self.request_backend(
                backend,
                "GET",
                "/spend/logs/v2",
                params={
                    "start_date": utc_start,
                    "end_date": utc_end,
                    "page": page,
                    "page_size": page_size,
                    "sort_by": "startTime",
                    "sort_order": "desc",
                },
            )
            pages_read = page
            if isinstance(payload, dict):
                total_pages = _as_int(_first(payload, "total_pages", "totalPages", default=total_pages))
                total_records = _as_int(_first(payload, "total", "total_count", "count", default=total_records))
            logs = _records(payload)
            if not logs:
                break
            for log in logs:
                log_team = self._department_info_from_log(log, {team_id.lower(): {"id": team_id, "name": team_info["name"]}})
                if log_team["id"] != team_id:
                    continue
                detected_source = backend.source or detect_source(log)
                if source and source != "all" and detected_source != source:
                    continue
                employee_info = self._employee_info_from_log(log, user_map, backend, account_index)
                employee_key = employee_info["id"]
                employees.setdefault(employee_key, employee_info)
                model = self._usage_model_name(log, backend=backend)
                day = _date_text_in_usage_timezone(_first(log, "startTime", "start_time", "created_at", "date"))
                key = (day, employee_key, detected_source, model)
                row = grouped.setdefault(key, self._admin_empty_row(day, employee_info, detected_source, model))
                self._add_log_to_row(row, log)
            if total_pages and page >= total_pages:
                break

        rows = sorted(grouped.values(), key=lambda item: (item["date"], item["employeeName"], item["source"], item["model"]))
        summary_rows: list[dict[str, Any]] = []
        if not source or source == "all":
            try:
                summary_rows = await self._team_daily_activity_rows(start_date, end_date, team_id, {team_id.lower(): {"id": team_id, "name": team_info["name"]}}, backend)
            except HTTPException:
                summary_rows = []

        truncated = bool(total_pages and pages_read < total_pages)
        return {
            "rows": rows,
            "summaryRows": summary_rows or rows,
            "employees": self._admin_employee_summaries_with_zeroes(rows, employees),
            "team": team_info,
            "pageLimit": max_pages,
            "pageSize": page_size,
            "pagesRead": pages_read,
            "totalPages": total_pages,
            "totalRecords": total_records,
            "truncated": truncated,
            "dataQuality": {
                "summarySource": "team_daily_activity" if summary_rows else "spend_logs",
                "rankingSource": "spend_logs",
                "timezoneOffsetMinutes": usage_timezone_offset_minutes(),
            },
        }

    async def team_usage_rows(
        self,
        team_scopes: list[dict[str, Any]] | str,
        start_date: str | None = None,
        end_date: str | None = None,
        source: str | None = None,
        legacy_source: str | None = None,
    ) -> dict[str, Any]:
        """Read and merge one logical team across matching backend instances."""
        if isinstance(team_scopes, str):
            # Backward-compatible call shape: backend_id, team_id, start, end, source.
            backend_id = team_scopes
            team_id = str(start_date or "")
            start_value = str(end_date or "")
            end_value = str(source or "")
            source_value = legacy_source or "all"
            scopes = [{"backend": backend_id, "id": team_id}]
            start_date, end_date, source = start_value, end_value, source_value
        else:
            scopes = team_scopes
        if not start_date or not end_date:
            raise HTTPException(status_code=400, detail="团队用量日期范围无效")
        payloads = await asyncio.gather(
            *(self._team_usage_rows_single(str(item.get("backend")), str(item.get("id")), start_date, end_date, source or "all") for item in scopes),
            return_exceptions=True,
        )
        failures = [item for item in payloads if isinstance(item, BaseException)]
        if failures:
            first = failures[0]
            if isinstance(first, HTTPException):
                raise first
            raise HTTPException(status_code=502, detail="团队跨实例用量读取失败") from first
        valid = [item for item in payloads if isinstance(item, dict)]
        if not valid:
            for item in payloads:
                if isinstance(item, HTTPException):
                    raise item
            raise HTTPException(status_code=404, detail="未找到当前负责的团队")
        rows = [row for payload in valid for row in payload.get("rows") or []]
        summary_rows = [row for payload in valid for row in payload.get("summaryRows") or []]
        grouped_rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in summary_rows:
            key = (str(row.get("date") or ""), str(row.get("source") or ""), str(row.get("model") or ""))
            current = grouped_rows.setdefault(
                key,
                {
                    **{key_name: row.get(key_name) for key_name in ("date", "source", "model")},
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "totalTokens": 0,
                    "requestCount": 0,
                    "successCount": 0,
                    "failureCount": 0,
                    "spend": 0.0,
                },
            )
            for field in ("promptTokens", "completionTokens", "totalTokens", "requestCount", "successCount", "failureCount"):
                current[field] += _as_int(row.get(field))
            current["spend"] += _as_number(row.get("spend"))
        employee_groups: dict[str, dict[str, Any]] = {}
        merged_rows: list[dict[str, Any]] = []
        for scope, payload in zip(scopes, payloads):
            if not isinstance(payload, dict):
                continue
            backend_id = str(scope.get("backend") or "")
            for row in payload.get("rows") or []:
                public_row = dict(row)
                if not _normal_email(public_row.get("employeeEmail")) and public_row.get("employeeId"):
                    public_row["employeeId"] = f"{backend_id}:{public_row['employeeId']}"
                merged_rows.append(public_row)
            for employee in payload.get("employees") or []:
                email = _normal_email(employee.get("employeeEmail"))
                raw_employee_id = str(employee.get("employeeId") or "")
                identity = f"email:{email}" if email else f"id:{backend_id}:{raw_employee_id}"
                current = employee_groups.get(identity)
                if current is None:
                    current = dict(employee)
                    if not email:
                        current["employeeId"] = f"{backend_id}:{raw_employee_id}"
                    current["userIds"] = [
                        user_id if str(user_id).startswith(f"{backend_id}:") else f"{backend_id}:{user_id}"
                        for user_id in current.get("userIds") or []
                    ]
                    employee_groups[identity] = current
                else:
                    for field in ("promptTokens", "completionTokens", "totalTokens", "requestCount", "successCount", "failureCount", "spend"):
                        current[field] = _as_number(current.get(field)) + _as_number(employee.get(field))
                    for user_id in employee.get("userIds") or []:
                        account_id = user_id if str(user_id).startswith(f"{backend_id}:") else f"{backend_id}:{user_id}"
                        if account_id not in current["userIds"]:
                            current["userIds"].append(account_id)
                    if current.get("teamRole") != "admin" and employee.get("teamRole") == "admin":
                        current["teamRole"] = "admin"
        anchor = scopes[0] if scopes else {}
        team_name = next((payload.get("team", {}).get("name") for payload in valid if payload.get("team")), anchor.get("name") or anchor.get("id") or "团队")
        return {
            "rows": merged_rows,
            "summaryRows": sorted(grouped_rows.values(), key=lambda row: (str(row.get("date")), str(row.get("source")), str(row.get("model")))),
            "employees": sorted(employee_groups.values(), key=lambda item: (-_as_number(item.get("totalTokens")), -_as_number(item.get("spend")), str(item.get("employeeName") or "").casefold())),
            "team": {"id": anchor.get("id"), "name": team_name, "memberCount": len(employee_groups), "backend": anchor.get("backend")},
            "pageLimit": max((_as_int(payload.get("pageLimit")) for payload in valid), default=0),
            "pageSize": max((_as_int(payload.get("pageSize")) for payload in valid), default=0),
            "pagesRead": sum(_as_int(payload.get("pagesRead")) for payload in valid),
            "totalPages": sum(_as_int(payload.get("totalPages")) for payload in valid),
            "totalRecords": sum(_as_int(payload.get("totalRecords")) for payload in valid),
            "truncated": any(bool(payload.get("truncated")) for payload in valid),
            "dataQuality": {
                "summarySource": "spend_logs",
                "rankingSource": "spend_logs",
                "backends": [str(item.get("backend")) for item in scopes],
                "scopeCount": len(scopes),
                "memberIdentityMatch": "normalized_email_or_backend_user_id",
            },
        }

    def _usage_totals(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        totals = {
            "promptTokens": 0,
            "completionTokens": 0,
            "totalTokens": 0,
            "requestCount": 0,
            "successCount": 0,
            "failureCount": 0,
            "spend": 0.0,
        }
        for row in rows:
            totals["promptTokens"] += _as_int(row.get("promptTokens"))
            totals["completionTokens"] += _as_int(row.get("completionTokens"))
            totals["totalTokens"] += _as_int(row.get("totalTokens"))
            totals["requestCount"] += _as_int(row.get("requestCount"))
            totals["successCount"] += _as_int(row.get("successCount"))
            totals["failureCount"] += _as_int(row.get("failureCount"))
            totals["spend"] += _as_number(row.get("spend"))
        return totals

    def _admin_empty_row(self, day: str, employee_info: dict[str, Any], source: str, model: str) -> dict[str, Any]:
        row = self._empty_usage_row(day, source, model)
        row.update(
            {
                "employeeId": employee_info["id"],
                "employeeName": employee_info["name"],
                "employeeEmail": employee_info["email"],
                "bindStatus": employee_info["bindStatus"],
            }
        )
        return row

    def _admin_user_map(self, users: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        mapping: dict[str, dict[str, Any]] = {}
        by_email: dict[str, dict[str, Any]] = {}
        for user in users:
            user_id = str(user.get("user_id") or "").strip()
            if not user_id:
                continue
            metadata = _metadata_dict(user.get("metadata"))
            email = str(user.get("user_email") or user.get("sso_user_id") or "").strip().lower()
            alias = str(user.get("user_alias") or "").strip()
            if email and email in by_email:
                info = by_email[email]
                if not info.get("name") and alias:
                    info["name"] = alias
            else:
                info = {
                    "id": email or user_id,
                    "name": alias or email.split("@", 1)[0] or user_id,
                    "email": email,
                    "organization_id": _clean_text(user.get("organization_id") or user.get("org_id") or metadata.get("organization_id")),
                    "team_id": _clean_text(user.get("team_id") or metadata.get("team_id")),
                    "metadata": metadata,
                    "bindStatus": "已绑定邮箱" if email else "未绑定邮箱",
                }
                if email:
                    by_email[email] = info
            info.setdefault("userIds", [])
            if user_id not in info["userIds"]:
                info["userIds"].append(user_id)
            info.update(
                {
                    "email": email,
                    "bindStatus": "已绑定邮箱" if email else "未绑定邮箱",
                }
            )
            mapping[user_id.lower()] = info
            if email:
                mapping[email] = info
        return mapping

    def _admin_employee_info(self, raw_user: str, user_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
        normalized = raw_user.strip().lower()
        if normalized in user_map:
            return user_map[normalized]
        return {"id": raw_user, "name": raw_user, "email": "", "bindStatus": "未绑定邮箱"}

    def _admin_employee_matches(self, employee_info: dict[str, Any], employee_filter: str) -> bool:
        values = [employee_info.get("id"), employee_info.get("name"), employee_info.get("email")]
        return any(employee_filter in str(value or "").lower() for value in values)

    async def _team_map_or_empty(self, backend: LiteLLMBackend) -> dict[str, dict[str, str]]:
        """团队列表拿不到时退回空表，部门列显示"未绑定部门"，不牵连整个看板。"""

        try:
            return await self.team_map(backend)
        except HTTPException:
            return {}

    def _collect_department_name(
        self,
        department_names: dict[str, list[str]],
        employee_key: str,
        log: dict[str, Any],
        team_map: dict[str, dict[str, str]],
    ) -> None:
        info = self._department_info_from_log(log, team_map)
        name = str(info.get("name") or "").strip()
        if not name or info.get("id") == "unassigned":
            return
        names = department_names.setdefault(employee_key, [])
        if name not in names:
            names.append(name)

    def _admin_employee_summaries(
        self,
        rows: list[dict[str, Any]],
        employees: dict[str, dict[str, Any]],
        department_names: dict[str, list[str]] | None = None,
    ) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        source_totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for row in rows:
            employee_id = str(row["employeeId"])
            employee = employees.get(employee_id, {})
            summary = grouped.setdefault(
                employee_id,
                {
                    "employeeId": employee_id,
                    "employeeName": employee.get("name") or row.get("employeeName") or employee_id,
                    "employeeEmail": employee.get("email") or row.get("employeeEmail") or "",
                    "bindStatus": employee.get("bindStatus") or row.get("bindStatus") or "未绑定邮箱",
                    "userIds": list(employee.get("userIds") or []),
                    "departmentNames": sorted((department_names or {}).get(employee_id, [])),
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "totalTokens": 0,
                    "requestCount": 0,
                    "successCount": 0,
                    "failureCount": 0,
                    "spend": 0.0,
                    "primarySource": "其他",
                },
            )
            summary["promptTokens"] += _as_int(row.get("promptTokens"))
            summary["completionTokens"] += _as_int(row.get("completionTokens"))
            summary["totalTokens"] += _as_int(row.get("totalTokens"))
            summary["requestCount"] += _as_int(row.get("requestCount"))
            summary["successCount"] += _as_int(row.get("successCount"))
            summary["failureCount"] += _as_int(row.get("failureCount"))
            summary["spend"] += _as_number(row.get("spend"))
            source_totals[employee_id][str(row.get("source") or "其他")] += _as_int(row.get("totalTokens"))

        for employee_id, summary in grouped.items():
            sources = source_totals.get(employee_id, {})
            if sources:
                summary["primarySource"] = max(sources.items(), key=lambda item: item[1])[0]
        return sorted(grouped.values(), key=self._admin_employee_sort_key)

    def _admin_employee_sort_key(self, employee: dict[str, Any]) -> tuple[float, float, float, str]:
        name = str(employee.get("employeeName") or employee.get("employeeEmail") or employee.get("employeeId") or "")
        return (
            -_as_number(employee.get("totalTokens")),
            -_as_number(employee.get("spend")),
            -_as_number(employee.get("requestCount")),
            name.lower(),
        )

    @staticmethod
    def _normalized_model_name(value: Any) -> str:
        return _clean_text(value).casefold()

    @staticmethod
    def _model_usage_from_activity(
        payload: Any, deployment_map: dict[str, str] | None = None
    ) -> dict[str, int]:
        usage: dict[str, int] = defaultdict(int)
        for item in _records(payload):
            breakdown = item.get("breakdown") if isinstance(item.get("breakdown"), dict) else {}
            model_groups = breakdown.get("model_groups") if isinstance(breakdown.get("model_groups"), dict) else {}
            models = breakdown.get("models") if isinstance(breakdown.get("models"), dict) else {}
            buckets = models or model_groups
            for model_name, value in buckets.items():
                normalized_name = LiteLLMClient._normalized_model_name(
                    resolve_canonical_model_name(
                        model_name,
                        deployment_map=deployment_map,
                        diagnose_unknown=True,
                    )
                )
                if not normalized_name or not isinstance(value, dict):
                    continue
                metrics = value.get("metrics") if isinstance(value.get("metrics"), dict) else value
                usage[normalized_name] += _as_int(_first(metrics, "api_requests", "total_api_requests", "requestCount"))
        return dict(usage)

    async def model_usage_counts(self, start_date: str, end_date: str) -> dict[str, int]:
        model_usage_cache = getattr(self, "_model_usage_cache", None)
        if model_usage_cache is None:
            model_usage_cache = TTLCache()
            self._model_usage_cache = model_usage_cache
        cache_key = f"model-usage:v2:{start_date}:{end_date}:tz{usage_timezone_offset_minutes()}"
        hit, value, _ = model_usage_cache.get(cache_key)
        if hit:
            return value

        async def load_backend(backend: LiteLLMBackend) -> dict[str, int] | None:
            try:
                deployment_map = await self._ensure_deployment_model_map(backend)
                payload = await self.request_backend(
                    backend,
                    "GET",
                    "/user/daily/activity/aggregated",
                    params={
                        "start_date": start_date,
                        "end_date": end_date,
                        "timezone": usage_timezone_offset_minutes(),
                    },
                )
                if isinstance(payload, list):
                    return self._model_usage_from_activity(payload, deployment_map)
                if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                    logger.warning("model usage query returned an invalid response for backend %s", backend.id)
                    return None
                return self._model_usage_from_activity(payload, deployment_map)
            except HTTPException as exc:
                logger.warning("model usage query failed for backend %s: HTTP %s", backend.id, exc.status_code)
                return None
            except Exception:
                logger.warning("model usage query failed for backend %s", backend.id)
                return None

        results = await asyncio.gather(*(load_backend(backend) for backend in self.backends))
        merged: dict[str, int] = defaultdict(int)
        successful_backends = 0
        for result in results:
            if result is None:
                continue
            successful_backends += 1
            for model_name, request_count in result.items():
                merged[model_name] += request_count

        value = dict(merged)
        if successful_backends:
            model_usage_cache.set(cache_key, value, _env_int("MODEL_USAGE_CACHE_TTL_SECONDS", 300))
        return value

    @staticmethod
    def _price_per_million(value: Any) -> float | None:
        """把上游的「每 token 单价」换算成展示用的「每百万 token 单价」。"""
        if value is None:
            return None
        amount = _as_number(value)
        if amount <= 0:
            return None
        return round(amount * 1_000_000, 4)

    async def _model_pricing(self) -> dict[str, dict[str, Any]]:
        """按模型名聚合上游计费信息。

        同一模型名在不同线路/后端上会有多份部署，单价并不一致。这里沿用
        LiteLLM 自身 `_set_model_group_info`（litellm/router.py）的口径：取同名
        部署中输入单价最高的那一份，既与上游聚合一致，也不会低报成本。

        上下文窗口不跟着价格走：它是模型自身能力、不随线路变，而上游只有部分
        部署填了 `max_input_tokens`。若跟着「输入价最高」的那一份取，恰好那份
        没填时 1M 窗口会显示成「未标注」，所以按模型名单独取非零最大值。
        """
        hit, value, _ = self._model_cache.get("model_pricing:v2")
        if hit:
            return value

        pricing: dict[str, dict[str, Any]] = {}
        deployment_input_prices: dict[str, float] = {}
        context_windows: dict[str, int] = {}
        successful_backends = 0
        for backend in self.backends:
            try:
                payload = await self.request_backend(backend, "GET", "/model/info")
            except HTTPException:
                continue
            except Exception:
                logger.warning("model pricing query failed for backend %s", backend.id)
                continue
            successful_backends += 1
            deployment_map: dict[str, str] = {}
            for item in _records(payload):
                info = item.get("model_info") if isinstance(item.get("model_info"), dict) else {}
                params = item.get("litellm_params") if isinstance(item.get("litellm_params"), dict) else {}
                deployment_id = _clean_text(info.get("id") or item.get("model_info_id"))
                actual_model = _clean_text(params.get("model"))
                if deployment_id and actual_model:
                    deployment_map[deployment_id.casefold()] = actual_model
                canonical_name = resolve_canonical_model_name(
                    actual_model or _first(item, "model_name", "model", "id"),
                    deployment_map=deployment_map,
                )
                normalized_name = self._normalized_model_name(canonical_name)
                if not normalized_name:
                    continue
                # 窗口要在价格过滤之前收集：没配价的透传部署常常反而填了窗口。
                max_input_tokens = _as_int(info.get("max_input_tokens"))
                if max_input_tokens > context_windows.get(normalized_name, 0):
                    context_windows[normalized_name] = max_input_tokens
                input_price = self._price_per_million(info.get("input_cost_per_token"))
                output_price = self._price_per_million(info.get("output_cost_per_token"))
                if input_price is None and output_price is None:
                    continue
                raw_names = {
                    self._normalized_model_name(_first(item, "model_name", "model", "id")),
                    self._normalized_model_name(deployment_id),
                }
                for raw_name in raw_names:
                    if raw_name:
                        deployment_input_prices[raw_name] = input_price or 0
                current = pricing.get(normalized_name)
                if current is not None and (current.get("inputPricePerMillion") or 0) >= (input_price or 0):
                    continue
                pricing[normalized_name] = {
                    "billingType": "按次计费" if _clean_text(info.get("mode")) == "image_generation" else "按量计费",
                    "inputPricePerMillion": input_price,
                    "outputPricePerMillion": output_price,
                    "cacheReadPricePerMillion": self._price_per_million(info.get("cache_read_input_token_cost")),
                    "cacheWritePricePerMillion": self._price_per_million(info.get("cache_creation_input_token_cost")),
                    "supportsVision": bool(info.get("supports_vision")),
                    "supportsReasoning": bool(info.get("supports_reasoning")),
                    "supportsFunctionCalling": bool(info.get("supports_function_calling")),
                    "isEmbedding": _clean_text(info.get("mode")) == "embedding",
                }
            if not hasattr(self, "_deployment_model_maps"):
                self._deployment_model_maps = {}
            self._deployment_model_maps[backend.id] = deployment_map
            self._model_cache.set(
                f"deployment-model-map:v1:{backend.id}",
                deployment_map,
                _env_int("MODEL_CACHE_TTL_SECONDS", 1800),
            )

        self._deployment_input_prices = deployment_input_prices

        for normalized_name, entry in pricing.items():
            window = context_windows.get(normalized_name, 0)
            entry["contextWindow"] = window if window > 0 else None

        if successful_backends:
            self._model_cache.set("model_pricing:v2", pricing, _env_int("MODEL_CACHE_TTL_SECONDS", 1800))
        return pricing

    @staticmethod
    def _pricing_capabilities(pricing: dict[str, Any]) -> list[str]:
        capabilities: list[str] = []
        if pricing.get("isEmbedding"):
            capabilities.append("向量化")
        if pricing.get("supportsVision"):
            capabilities.append("视觉")
        if pricing.get("supportsReasoning"):
            capabilities.append("推理")
        if pricing.get("supportsFunctionCalling"):
            capabilities.append("函数调用")
        return capabilities or ["通用"]

    async def models(self, usage_counts: dict[str, int] | None = None) -> list[dict[str, Any]]:
        hit, value, _ = self._model_cache.get("models:v2")
        if hit:
            models = value
        else:
            models = []
            seen_model_names: set[str] = set()
            for backend in self.backends:
                try:
                    payload = await self.request_backend(backend, "GET", "/models")
                except HTTPException:
                    continue
                raw_models = _records(payload)
                if not raw_models and isinstance(payload, dict):
                    values = payload.get("data") or payload.get("models") or []
                    if isinstance(values, list):
                        raw_models = [{"id": str(value), "model_name": str(value)} if isinstance(value, str) else value for value in values]
                for index, item in enumerate(raw_models):
                    model_name = _clean_text(_first(item, "model_name", "model", "id", "litellm_model_name", default=f"model-{index + 1}"))
                    normalized_name = self._normalized_model_name(model_name)
                    if not normalized_name or normalized_name in seen_model_names:
                        continue
                    seen_model_names.add(normalized_name)
                    provider = str(_first(item, "provider", "litellm_provider", default=provider_from_model(model_name)))
                    capabilities = ["代码"] if any(word in model_name.lower() for word in ("code", "coder", "claude", "gpt")) else ["通用"]
                    if any(word in model_name.lower() for word in ("vision", "gemini")):
                        capabilities.append("多模态")
                    models.append(
                        {
                            "id": str(_first(item, "id", "model_info_id", default=model_name)),
                            "modelName": model_name,
                            "provider": provider,
                            "capabilities": capabilities,
                            "description": str(_first(item, "description", default="当前账号可用模型。")),
                            "contextWindow": str(_first(item, "max_input_tokens", "context_window", "contextWindow", default="未标注")),
                            "status": "可用",
                            "recommendedFor": str(_first(item, "recommended_for", default="按任务需求复制模型名称后使用")),
                            "_backendId": backend.id,
                        }
                    )
            self._model_cache.set("models:v2", models, _env_int("MODEL_CACHE_TTL_SECONDS", 1800))

        catalog = await self._priced_catalog(models)

        end_day = usage_today()
        end_date = end_day.isoformat()
        start_date = (end_day - timedelta(days=29)).isoformat()
        # The production route supplies database counts. Keep the upstream call as
        # a compatibility fallback for local deployments without the snapshot DB.
        usage = usage_counts if usage_counts is not None else await self.model_usage_counts(start_date, end_date)
        usage_by_display: dict[str, int] = defaultdict(int)
        for model_name, request_count in usage.items():
            display_key = self._normalized_model_name(resolve_canonical_model_name(model_name))
            if display_key:
                usage_by_display[display_key] += request_count
        return sorted(
            catalog,
            key=lambda item: (
                -usage_by_display.get(self._normalized_model_name(item.get("displayName")), 0),
                self._normalized_model_name(item.get("displayName")),
            ),
        )

    async def _priced_catalog(self, models: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """给模型目录补上展示名、厂商与计费信息，并按展示名去重。

        上游同一模型配了多条线路部署（wangsu-gpt-5.5、kuaihui-gpt-5.5 …），
        展示名都是 gpt-5.5。这里每个展示名只保留一条：优先留原始名就等于
        展示名的那条（员工填最短的名字即可路由），其次留输入单价最高的那条
        （与 `_model_pricing` 取最高价的口径一致，不低报成本）。

        在任何部署上都查不到有效单价的模型仍然剔除（单价缺失时展示成免费会
        误导员工）。上游计费接口整体不可用时不做价格剔除，退化为不带价格的
        目录，避免模型广场直接变空。

        上下文窗口按展示名单独取非零最大值，不跟着被选中的那条部署走：窗口是
        模型能力、不随线路变，而上游只有部分部署填了这个字段（例如 fable-5 的
        1M 窗口只写在 kuaihui 那条线路上）。
        """
        pricing = await self._model_pricing()
        context_windows = self._context_windows_by_display_name(models, pricing)
        grouped: dict[str, dict[str, Any]] = {}
        for model in models:
            model_name = _clean_text(model.get("modelName"))
            backend_id = _clean_text(model.get("_backendId"))
            display_name = resolve_canonical_model_name(
                model_name,
                deployment_map=getattr(self, "_deployment_model_maps", {}).get(backend_id, {}),
            )
            if not display_name:
                continue
            family_key, family_label = model_family(model_name)
            entry = {
                **model,
                "modelName": model_name,
                "displayName": display_name,
                "familyKey": family_key,
                "familyLabel": family_label,
                "_selectionInputPrice": getattr(self, "_deployment_input_prices", {}).get(
                    self._normalized_model_name(model_name), 0
                ),
            }
            price = pricing.get(self._normalized_model_name(display_name))
            if price is None and pricing:
                continue
            if price is not None:
                entry.update(
                    {
                        "billingType": price["billingType"],
                        "inputPricePerMillion": price["inputPricePerMillion"],
                        "outputPricePerMillion": price["outputPricePerMillion"],
                        "cacheReadPricePerMillion": price["cacheReadPricePerMillion"],
                        "cacheWritePricePerMillion": price["cacheWritePricePerMillion"],
                        "capabilities": self._pricing_capabilities(price),
                    }
                )
            key = self._normalized_model_name(display_name)
            window = context_windows.get(key, 0)
            if window > 0:
                entry["contextWindow"] = str(window)
            if key not in grouped or self._prefer_catalog_entry(entry, grouped[key]):
                grouped[key] = entry
        return [
            {
                key: value
                for key, value in entry.items()
                if key not in {"_selectionInputPrice", "_backendId"}
            }
            for entry in grouped.values()
        ]

    def _context_windows_by_display_name(
        self, models: list[dict[str, Any]], pricing: dict[str, dict[str, Any]]
    ) -> dict[str, int]:
        """按展示名汇总上下文窗口，取同组各线路部署里的非零最大值。

        `/models` 自身很少带窗口，主要来源是 `/model/info`（已由 `_model_pricing`
        按原始名整理好），所以两处都看一眼，谁给出更大的非零值就用谁。
        """
        windows: dict[str, int] = {}

        def record(display_key: str, value: Any) -> None:
            window = _as_int(value)
            if window > windows.get(display_key, 0):
                windows[display_key] = window

        for model in models:
            model_name = _clean_text(model.get("modelName"))
            backend_id = _clean_text(model.get("_backendId"))
            display_key = self._normalized_model_name(
                resolve_canonical_model_name(
                    model_name,
                    deployment_map=getattr(self, "_deployment_model_maps", {}).get(backend_id, {}),
                )
            )
            if not display_key:
                continue
            record(display_key, model.get("contextWindow"))
            price = pricing.get(display_key)
            if price:
                record(display_key, price.get("contextWindow"))
        return windows

    @staticmethod
    def _prefer_catalog_entry(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
        """同一展示名的多条部署里，判断 candidate 是否比 current 更适合展示。"""

        def rank(entry: dict[str, Any]) -> tuple[int, float]:
            is_canonical = LiteLLMClient._normalized_model_name(entry.get("modelName")) == LiteLLMClient._normalized_model_name(entry.get("displayName"))
            return (1 if is_canonical else 0, _as_number(entry.get("_selectionInputPrice")))

        return rank(candidate) > rank(current)


def default_date_range(days: int = 30) -> tuple[str, str]:
    end = usage_today()
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()
