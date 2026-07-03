import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

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


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _normal_email(value: Any) -> str:
    text = _clean_text(value).lower()
    return text if "@" in text else ""


def _has_cjk(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)


def _date_text(value: Any) -> str:
    if not value:
        return date.today().isoformat()
    text = str(value)
    if "T" in text:
        return text.split("T", 1)[0]
    if " " in text:
        return text.split(" ", 1)[0]
    return text[:10]


def usage_timezone_offset_minutes() -> int:
    raw_value = os.getenv("USAGE_TIMEZONE_OFFSET_MINUTES", "-480")
    try:
        return int(raw_value)
    except ValueError:
        return -480


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
    metadata = _first(record, "metadata", "request_tags", "tags", default={})
    values = [
        _first(record, "source", "tool", "client", "application", default=""),
        _first(record, "user", "user_id", "end_user", default=""),
        _first(record, "key_alias", "key_name", "api_key_alias", default=""),
        metadata,
    ]
    haystack = " ".join(str(value) for value in values).lower()
    if any(word in haystack for word in ("cursor", "curosr")):
        return "Cursor"
    if any(word in haystack for word in ("claude code", "claude-code", "claudecode")):
        return "Claude Code"
    return "其他"


def detect_source_from_key(key: dict[str, Any]) -> str:
    values = [key.get("name"), key.get("purpose"), key.get("masked"), key.get("id")]
    haystack = " ".join(str(value or "") for value in values).lower()
    if "cursor" in haystack:
        return "Cursor"
    if any(word in haystack for word in ("claude code", "claude-code", "claudecode")):
        return "Claude Code"
    return "其他"


def mask_key(value: str) -> str:
    if not value:
        return "未返回"
    if len(value) <= 12:
        return value[:3] + "..." + value[-3:]
    return value[:10] + "..." + value[-4:]


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


class LiteLLMClient:
    def __init__(self) -> None:
        base_url = os.getenv("LITELLM_BASE_URL", "").strip().rstrip("/")
        admin_key = os.getenv("LITELLM_ADMIN_KEY", "").strip()
        if not base_url or not admin_key:
            raise RuntimeError("请先在 .env 中配置 LITELLM_BASE_URL 和 LITELLM_ADMIN_KEY")
        self.backends = [
            LiteLLMBackend(id="primary", label="AI 用量中心", base_url=base_url, admin_key=admin_key),
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
        self._model_cache = TTLCache()
        self._account_index_cache = TTLCache()
        self._users_cache = TTLCache()
        self._teams_cache = TTLCache()
        self._team_map_cache = TTLCache()
        self._spend_log_scan_cache = TTLCache()

    def _cache(self, attribute: str) -> TTLCache:
        cache = getattr(self, attribute, None)
        if cache is None:
            cache = TTLCache()
            setattr(self, attribute, cache)
        return cache

    async def close(self) -> None:
        await self.http_client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        return await self.request_backend(self.backends[0], method, path, **kwargs)

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

    def _is_backend_usage_account(self, backend: LiteLLMBackend, user_id: Any) -> bool:
        text = _clean_text(user_id).lower()
        if backend.source == "Her":
            return text.startswith("carher-")
        return bool(text)

    def _empty_account_index(self) -> dict[str, Any]:
        return {
            "emails": defaultdict(dict),
            "names": defaultdict(dict),
            "profiles": {},
        }

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
                names = [
                    user.get("user_alias"),
                    metadata.get("display_name"),
                    metadata.get("owner_name"),
                ]
                for used_by in metadata.get("used_by") or []:
                    if isinstance(used_by, dict):
                        names.append(used_by.get("name"))
                if self._is_backend_usage_account(backend, user_id):
                    user_id_text = _clean_text(user_id)
                    alias_name = _clean_text(user.get("user_alias") or metadata.get("display_name") or metadata.get("owner_name"))
                    index["profiles"][user_id_text] = {"email": email, "name": alias_name}
                    self._add_account_index_entry(index, user_id, "her_user_email" if email else "her_user_alias", email, names)
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
                names = [
                    metadata.get("display_name"),
                    metadata.get("owner_name"),
                    key.get("user_alias"),
                    key.get("key_alias"),
                ]
                for used_by in metadata.get("used_by") or []:
                    if isinstance(used_by, dict):
                        names.append(used_by.get("name"))
                if self._is_backend_usage_account(backend, key.get("user_id")):
                    user_id_text = _clean_text(key.get("user_id"))
                    if user_id_text:
                        existing = index["profiles"].get(user_id_text, {})
                        profile_email = email or _normal_email(existing.get("email"))
                        profile_name = _clean_text(metadata.get("display_name") or metadata.get("owner_name") or existing.get("name"))
                        index["profiles"][user_id_text] = {"email": profile_email, "name": profile_name}
                    self._add_account_index_entry(index, key.get("user_id"), "her_key_metadata_email" if email else "her_key_metadata_name", email, names)
            total_pages = _as_int(_first(payload, "total_pages", "totalPages", default=0)) if isinstance(payload, dict) else 0
            if total_pages and page >= total_pages:
                break

        self._account_index_cache.set(cache_key, index, _env_int("HER_ACCOUNT_INDEX_CACHE_TTL_SECONDS", 1800))
        return index

    def _log_raw_user(self, log: dict[str, Any]) -> str:
        return str(_first(log, "user", "user_id", "end_user", default="") or "").strip()

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

        for user_id, entry in self._name_index_matches(index, _clean_text(name)).items():
            for source in sorted(entry.get("sources", set())) or ["her_user_alias_unique"]:
                add_user_id(backend, user_id, "her_user_alias_unique" if source.startswith("her_") else source)

    async def resolve_user(self, email: str, name: str | None = None) -> dict[str, Any]:
        email_lower = email.lower()
        email_prefix = email_lower.split("@", 1)[0]
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

        for backend in self.backends:
            for page in range(1, 51):
                payload = await self.request_backend(backend, "GET", "/user/list", params={"page": page, "page_size": 100})
                for user in _records(payload):
                    user_id = user.get("user_id")
                    email_candidates = [user.get("user_email"), user.get("sso_user_id")]
                    legacy_candidates = [user.get("user_id"), user.get("user_alias")]
                    if any(str(candidate or "").lower() == email_lower for candidate in email_candidates):
                        matched_users.append(user)
                        add_user_id(backend, user_id, "user_email")
                    elif any(str(candidate or "").lower() == email_prefix for candidate in legacy_candidates):
                        matched_users.append(user)
                        add_user_id(backend, user_id, "legacy_user")
                total_pages = _as_int(payload.get("total_pages")) if isinstance(payload, dict) else 0
                if total_pages and page >= total_pages:
                    break

            if backend.source != "Her":
                for user_id in await self.user_ids_from_key_alias(email_prefix, backend):
                    add_user_id(backend, user_id, "key_alias")

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
        for alias in (f"cursor-{email_prefix}", f"claude-code-{email_prefix}", email_prefix):
            payload = await self.request_backend(
                backend,
                "GET",
                "/key/list",
                params={"key_alias": alias, "return_full_object": "true", "page": 1, "size": 100},
            )
            for key in _records(payload):
                user_id = str(key.get("user_id") or "").strip()
                if user_id and user_id not in seen:
                    seen.add(user_id)
                    user_ids.append(user_id)
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

        batches = await asyncio.gather(
            *(
                self._usage_from_daily_activity(
                    user_id=user_id,
                    start_date=start_date,
                    end_date=end_date,
                    source="all",
                    api_key=key["id"],
                    backend=backend,
                    source_override=key_source,
                )
                for key, key_source in selected_keys
            )
        )
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
                model = str(_first(log, "model", "model_group", "model_id", default="未知模型"))
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

    def _row_from_daily_activity_item(self, item: dict[str, Any], source: str, fallback_model: str = "全部模型") -> dict[str, Any]:
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else item
        breakdown = item.get("breakdown") if isinstance(item.get("breakdown"), dict) else {}
        models = breakdown.get("models") if isinstance(breakdown.get("models"), dict) else {}
        model = str(_first(item, "model", "model_group", default=None) or next(iter(models.keys()), fallback_model))
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
        params = {"user_id": user_id, "start_date": start_date, "end_date": end_date, "page": 1, "page_size": 1000}
        if api_key:
            params["api_key"] = api_key
        try:
            payload = await self.request_backend(backend, "GET", "/user/daily/activity/aggregated", params=params)
        except HTTPException:
            payload = await self.request_backend(backend, "GET", "/user/daily/activity", params=params)
        rows = []
        for item in _records(payload):
            rows.append(self._row_from_daily_activity_item(item, source_override or "其他"))
        return rows

    async def keys_for_user(self, user_id: str, backend: LiteLLMBackend | None = None) -> list[dict[str, Any]]:
        backend = backend or self.backends[0]
        cache_key = f"keys:{backend.id}:{user_id}"
        hit, value, _ = self._key_cache.get(cache_key)
        if hit:
            return value
        payload = await self.request_backend(
            backend,
            "GET",
            "/key/list",
            params={"user_id": user_id, "return_full_object": "true", "page": 1, "size": 100},
        )
        keys = []
        for item in _records(payload):
            token = str(_first(item, "token", "key", "api_key", default=""))
            alias = str(_first(item, "key_alias", "key_name", default="个人访问密钥") or "个人访问密钥")
            metadata = _first(item, "metadata", default={})
            status = "已禁用" if _first(item, "blocked", "deleted", default=False) else "正常"
            last_used = _first(item, "last_used_at", "updated_at", "created_at", default="-")
            keys.append(
                {
                    "id": token,
                    "name": alias,
                    "purpose": str(metadata.get("purpose") if isinstance(metadata, dict) else "") or "用于个人 AI 工具访问。",
                    "masked": mask_key(token),
                    "lastUsed": _date_text(last_used) if last_used != "-" else "-",
                    "monthTokens": _as_int(_first(item, "total_tokens", "token_usage", default=0)),
                    "spend": _as_number(_first(item, "spend", "total_spend")),
                    "status": status,
                }
            )
        self._key_cache.set(cache_key, keys, _env_int("KEY_LIST_CACHE_TTL_SECONDS", 300))
        return keys

    async def keys_for_user_ids(self, user_ids: list[str]) -> list[dict[str, Any]]:
        batches = []
        for user_id in user_ids:
            backend, raw_user_id = self._decode_account_id(user_id)
            if backend.source:
                continue
            batches.append(await self.keys_for_user(raw_user_id, backend))
        keys: list[dict[str, Any]] = []
        seen: set[str] = set()
        for batch in batches:
            for key in batch:
                key_id = key.get("id")
                if key_id and key_id not in seen:
                    seen.add(key_id)
                    keys.append(key)
        return keys

    async def regenerate_key(self, key_id: str, user_id: str, changed_by: str) -> str:
        backend, raw_user_id = self._decode_account_id(user_id)
        if backend.source:
            raise HTTPException(status_code=403, detail="Her 访问密钥暂不支持在这里更新")
        owned_keys = await self.keys_for_user(raw_user_id, backend)
        if not any(key["id"] == key_id for key in owned_keys):
            raise HTTPException(status_code=403, detail="不能更新不属于自己的访问密钥")
        payload = await self.request_backend(
            backend,
            "POST",
            "/key/regenerate",
            params={"key": key_id},
            headers={"litellm-changed-by": changed_by},
            json={},
        )
        new_key = _first(payload, "key", "token", "api_key", default="")
        if not new_key:
            raise HTTPException(status_code=502, detail="上游未返回新的访问密钥")
        return str(new_key)

    async def users(self, backend: LiteLLMBackend | None = None) -> list[dict[str, Any]]:
        backend = backend or self.backends[0]
        cache_key = f"users:{backend.id}"
        hit, value, _ = self._cache("_users_cache").get(cache_key)
        if hit:
            return value
        users: list[dict[str, Any]] = []
        for page in range(1, 101):
            payload = await self.request_backend(backend, "GET", "/user/list", params={"page": page, "page_size": 100})
            users.extend(_records(payload))
            total_pages = _as_int(payload.get("total_pages")) if isinstance(payload, dict) else 0
            if total_pages and page >= total_pages:
                break
        self._cache("_users_cache").set(cache_key, users, _env_int("USERS_CACHE_TTL_SECONDS", 1800))
        return users

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

    def _spend_log_scan_cache_key(self, start_date: str, end_date: str, source: str | None) -> str:
        return f"spend-log-scan:v1:{start_date}:{end_date}:{source or 'all'}"

    async def _spend_log_scan_rows(self, start_date: str, end_date: str, source: str | None, refresh: bool = False) -> dict[str, Any]:
        cache_key = self._spend_log_scan_cache_key(start_date, end_date, source)
        if not refresh:
            hit, value, ttl_seconds = self._cache("_spend_log_scan_cache").get(cache_key)
            if hit:
                payload = dict(value)
                payload["cache"] = {"hit": True, "ttlSeconds": ttl_seconds}
                return payload

        started = time.perf_counter()
        entries: list[dict[str, Any]] = []
        max_pages = max(1, int(os.getenv("ADMIN_USAGE_LOG_MAX_PAGES", "30")))
        page_size = max(1, min(100, int(os.getenv("ADMIN_USAGE_PAGE_SIZE", "100"))))
        utc_start, utc_end = _local_date_window_as_utc_text(start_date, end_date)
        pages_read = 0
        total_pages = 0
        total_records = 0

        for backend in self.backends:
            if backend.source and _source_filter_applies(source) and source != backend.source:
                continue
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
                    detected_source = backend.source or detect_source(log)
                    if source and source != "all" and detected_source != source:
                        continue
                    entries.append({"backend": backend, "log": log, "source": detected_source})

                if backend_total_pages and page >= backend_total_pages:
                    break

            pages_read = max(pages_read, backend_pages_read)
            total_pages = max(total_pages, backend_total_pages)
            total_records += backend_total_records

        payload = {
            "entries": entries,
            "pageLimit": max_pages,
            "pageSize": page_size,
            "pagesRead": pages_read,
            "totalPages": total_pages,
            "totalRecords": total_records,
            "truncated": bool(total_pages and pages_read < total_pages),
        }
        self._cache("_spend_log_scan_cache").set(cache_key, payload, _env_int("USAGE_LOG_SCAN_CACHE_TTL_SECONDS", 60))
        payload = dict(payload)
        payload["cache"] = {"hit": False, "ttlSeconds": 0}
        duration_ms = round((time.perf_counter() - started) * 1000)
        logger.info(
            "usage log scan source=%s cache_hit=%s pages=%s/%s entries=%s took=%sms",
            source or "all",
            False,
            pages_read,
            total_pages or "?",
            len(entries),
            duration_ms,
        )
        return payload

    async def admin_usage_rows(self, start_date: str, end_date: str, source: str | None, employee: str | None = None, refresh: bool = False) -> dict[str, Any]:
        started = time.perf_counter()
        employee_filter = (employee or "").strip().lower()
        grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        employees: dict[str, dict[str, Any]] = {}
        scan = await self._spend_log_scan_rows(start_date, end_date, source, refresh)

        for backend in self.backends:
            if backend.source and _source_filter_applies(source) and source != backend.source:
                continue
            users = await self.users(backend)
            user_map = self._admin_user_map(users)
            account_index = await self.her_account_index(backend) if backend.source == "Her" else None
            for entry in scan["entries"]:
                if entry["backend"].id != backend.id:
                    continue
                log = entry["log"]
                employee_info = self._employee_info_from_log(log, user_map, backend, account_index)
                if employee_filter and not self._admin_employee_matches(employee_info, employee_filter):
                    continue
                detected_source = entry["source"]
                employee_key = employee_info["id"]
                employees.setdefault(employee_key, employee_info)
                model = str(_first(log, "model", "model_group", "model_id", default="未知模型"))
                day = _date_text_in_usage_timezone(_first(log, "startTime", "start_time", "created_at", "date"))
                key = (day, employee_key, detected_source, model)
                row = grouped.setdefault(key, self._admin_empty_row(day, employee_info, detected_source, model))
                self._add_log_to_row(row, log)

        rows = sorted(grouped.values(), key=lambda item: (item["date"], item["employeeName"], item["source"], item["model"]))
        summary_rows: list[dict[str, Any]] = []
        if (not employee_filter) and (not source or source == "all"):
            for backend in self.backends:
                try:
                    summary_rows.extend(await self.admin_daily_activity_rows(start_date, end_date, backend))
                except HTTPException:
                    continue
        result = {
            "rows": rows,
            "summaryRows": summary_rows or rows,
            "employees": self._admin_employee_summaries(rows, employees),
            "pageLimit": scan["pageLimit"],
            "pageSize": scan["pageSize"],
            "pagesRead": scan["pagesRead"],
            "totalPages": scan["totalPages"],
            "totalRecords": scan["totalRecords"],
            "truncated": scan["truncated"],
            "dataQuality": {
                "summarySource": "official_daily_activity" if summary_rows else "spend_logs",
                "rankingSource": "spend_logs",
                "timezoneOffsetMinutes": usage_timezone_offset_minutes(),
            },
        }
        duration_ms = round((time.perf_counter() - started) * 1000)
        logger.info(
            "usage board=admin cache_hit=%s pages=%s/%s rows=%s employees=%s took=%sms",
            bool(scan.get("cache", {}).get("hit")),
            scan["pagesRead"],
            scan["totalPages"] or "?",
            len(rows),
            len(result["employees"]),
            duration_ms,
        )
        return result

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
        cache_key = f"team-map:{backend.id}"
        hit, value, _ = self._cache("_team_map_cache").get(cache_key)
        if hit:
            return value
        mapping: dict[str, dict[str, str]] = {}
        for team in await self.teams(backend, include_details=False):
            team_id = str(_first(team, "team_id", "id", default="") or "").strip()
            if not team_id:
                continue
            team_alias = str(_first(team, "team_alias", "alias", "name", default="") or "").strip()
            mapping[team_id.lower()] = {"id": team_id, "name": team_alias or team_id}
        self._cache("_team_map_cache").set(cache_key, mapping, _env_int("TEAM_MAP_CACHE_TTL_SECONDS", 600))
        return mapping

    async def team_info(self, backend: LiteLLMBackend, team_id: str) -> dict[str, Any] | None:
        payload = await self.request_backend(backend, "GET", "/team/info", params={"team_id": team_id})
        if not isinstance(payload, dict):
            return None
        team_info = payload.get("team_info")
        if isinstance(team_info, dict):
            team_info.setdefault("team_id", payload.get("team_id") or team_id)
            return team_info
        if payload.get("team_id") or payload.get("members_with_roles") is not None:
            payload.setdefault("team_id", team_id)
            return payload
        return None

    async def _teams_with_details(self, backend: LiteLLMBackend, teams: list[dict[str, Any]]) -> list[dict[str, Any]]:
        detailed: list[dict[str, Any]] = []
        for team in teams:
            team_id = str(_first(team, "team_id", "id", default="") or "").strip()
            if not team_id:
                detailed.append(team)
                continue
            if self._team_members(team):
                detailed.append(team)
                continue
            try:
                full_team = await self.team_info(backend, team_id)
            except HTTPException:
                full_team = None
            detailed.append(full_team or team)
        return detailed

    async def teams(self, backend: LiteLLMBackend | None = None, include_details: bool = True) -> list[dict[str, Any]]:
        backend = backend or self.backends[0]
        cache_key = f"teams:{backend.id}:{'details' if include_details else 'list'}"
        hit, value, _ = self._cache("_teams_cache").get(cache_key)
        if hit:
            return value
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
                result = await self._teams_with_details(backend, teams) if include_details else teams
                self._cache("_teams_cache").set(cache_key, result, _env_int("TEAMS_CACHE_TTL_SECONDS", 600))
                return result
        self._cache("_teams_cache").set(cache_key, [], _env_int("TEAMS_CACHE_TTL_SECONDS", 600))
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
        seen: set[tuple[str, str]] = set()
        for backend in self.backends:
            user_ids = accounts_by_backend.get(backend.id, set())
            emails = emails_by_backend.get(backend.id, set())
            if not user_ids and not emails:
                continue
            for team in await self.teams(backend):
                team_id = str(_first(team, "team_id", "id", default="") or "").strip()
                if not team_id:
                    continue
                for member in self._team_members(team):
                    member_id = self._team_member_user_id(member).lower()
                    member_email = self._team_member_email(member)
                    if self._is_team_admin_role(member) and ((member_id and member_id in user_ids) or (member_email and member_email in emails)):
                        key = (backend.id, team_id)
                        if key not in seen:
                            seen.add(key)
                            leader_teams.append({"backend": backend, "team": team, **self._team_summary(team, backend)})
                        break

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
            return {"id": team_id, "name": known.get("name", team_alias or team_id) if known else team_alias or team_id, "bindStatus": "已绑定部门"}

        department = str(
            _first(log, "department", "department_name", "departmentName", default="")
            or metadata.get("department")
            or metadata.get("department_name")
            or metadata.get("departmentName")
            or ""
        ).strip()
        if department:
            return {"id": department, "name": department, "bindStatus": "来自部门字段"}

        org_id = str(
            _first(log, "organization_id", "org_id", "organizationId", "orgId", default="")
            or metadata.get("organization_id")
            or metadata.get("org_id")
            or metadata.get("organizationId")
            or metadata.get("orgId")
            or ""
        ).strip()
        if org_id:
            return {"id": org_id, "name": org_id, "bindStatus": "来自组织字段"}
        return {"id": "unassigned", "name": "未绑定部门", "bindStatus": "未绑定部门"}

    def _department_empty_row(self, day: str, department_info: dict[str, str], source: str, model: str, employee_info: dict[str, Any]) -> dict[str, Any]:
        row = self._admin_empty_row(day, employee_info, source, model)
        row.update(
            {
                "departmentId": department_info["id"],
                "departmentName": department_info["name"],
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
            department = departments.get(department_id, {})
            summary = grouped.setdefault(
                department_id,
                {
                    "departmentId": department_id,
                    "departmentName": department.get("name") or row.get("departmentName") or department_id,
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
            source_totals[department_id][str(row.get("source") or "其他")] += _as_int(row.get("totalTokens"))
            employee_id = str(row.get("employeeId") or row.get("employeeEmail") or "")
            if employee_id:
                employees[department_id].add(employee_id)

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

    async def admin_department_usage_rows(self, start_date: str, end_date: str, source: str | None, department: str | None = None, refresh: bool = False) -> dict[str, Any]:
        started = time.perf_counter()
        department_filter = (department or "").strip().lower()
        grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        departments: dict[str, dict[str, str]] = {}
        employees: dict[str, dict[str, Any]] = {}
        scan = await self._spend_log_scan_rows(start_date, end_date, source, refresh)
        team_maps: dict[str, dict[str, dict[str, str]]] = {}

        for backend in self.backends:
            if backend.source and _source_filter_applies(source) and source != backend.source:
                continue
            users = await self.users(backend)
            user_map = self._admin_user_map(users)
            team_map = await self.team_map(backend)
            team_maps[backend.id] = team_map
            account_index = await self.her_account_index(backend) if backend.source == "Her" else None
            for entry in scan["entries"]:
                if entry["backend"].id != backend.id:
                    continue
                log = entry["log"]
                department_info = self._department_info_from_log(log, team_map)
                if department_filter and department_filter not in {department_info["id"].lower(), department_info["name"].lower()}:
                    continue
                detected_source = entry["source"]
                employee_info = self._employee_info_from_log(log, user_map, backend, account_index)
                department_id = department_info["id"]
                departments.setdefault(department_id, department_info)
                employees.setdefault(employee_info["id"], employee_info)
                model = str(_first(log, "model", "model_group", "model_id", default="未知模型"))
                day = _date_text_in_usage_timezone(_first(log, "startTime", "start_time", "created_at", "date"))
                key = (day, department_id, employee_info["id"], detected_source, model)
                row = grouped.setdefault(key, self._department_empty_row(day, department_info, detected_source, model, employee_info))
                self._add_log_to_row(row, log)

        rows = sorted(grouped.values(), key=lambda item: (item["date"], item["departmentName"], item["employeeName"], item["source"], item["model"]))
        summary_rows: list[dict[str, Any]] = []
        if not source or source == "all":
            for backend in self.backends:
                try:
                    team_map = team_maps.get(backend.id) or await self.team_map(backend)
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

        result = {
            "rows": rows,
            "summaryRows": summary_rows or rows,
            "departments": self._department_summaries(rows, departments),
            "employees": self._admin_employee_summaries(rows, employees),
            "pageLimit": scan["pageLimit"],
            "pageSize": scan["pageSize"],
            "pagesRead": scan["pagesRead"],
            "totalPages": scan["totalPages"],
            "totalRecords": scan["totalRecords"],
            "truncated": scan["truncated"],
            "dataQuality": {
                "summarySource": "team_daily_activity" if summary_rows else "spend_logs",
                "rankingSource": "spend_logs",
                "timezoneOffsetMinutes": usage_timezone_offset_minutes(),
            },
        }
        duration_ms = round((time.perf_counter() - started) * 1000)
        logger.info(
            "usage board=department cache_hit=%s pages=%s/%s rows=%s departments=%s took=%sms",
            bool(scan.get("cache", {}).get("hit")),
            scan["pagesRead"],
            scan["totalPages"] or "?",
            len(rows),
            len(result["departments"]),
            duration_ms,
        )
        return result

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
        summaries = {item["employeeId"]: item for item in self._admin_employee_summaries(rows, employees)}
        for employee_id, employee in employees.items():
            summaries.setdefault(
                employee_id,
                {
                    "employeeId": employee_id,
                    "employeeName": employee.get("name") or employee_id,
                    "employeeEmail": employee.get("email") or "",
                    "bindStatus": employee.get("bindStatus") or "未绑定邮箱",
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
        return sorted(summaries.values(), key=self._admin_employee_sort_key)

    async def team_usage_rows(
        self,
        backend_id: str,
        team_id: str,
        start_date: str,
        end_date: str,
        source: str | None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        backend = self._backend_map.get(backend_id)
        if backend is None:
            raise HTTPException(status_code=403, detail="当前团队权限已失效，请重新登录")
        teams = await self.teams(backend)
        team = next((item for item in teams if str(_first(item, "team_id", "id", default="") or "") == team_id), None)
        if team is None:
            raise HTTPException(status_code=404, detail="未找到当前负责的团队")

        user_map = self._admin_user_map(await self.users(backend))
        account_index = await self.her_account_index(backend) if backend.source == "Her" else None
        team_info = self._team_summary(team, backend)
        employees: dict[str, dict[str, Any]] = {}
        for member in self._team_members(team):
            employee_info = self._team_member_employee_info(member, user_map)
            if employee_info:
                employee_info = dict(employee_info)
                employee_info["teamRole"] = self._team_member_role(member) or "user"
                employees.setdefault(employee_info["id"], employee_info)

        grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        scan = await self._spend_log_scan_rows(start_date, end_date, source, refresh)
        for entry in scan["entries"]:
            if entry["backend"].id != backend.id:
                continue
            log = entry["log"]
            log_team = self._department_info_from_log(log, {team_id.lower(): {"id": team_id, "name": team_info["name"]}})
            if log_team["id"] != team_id:
                continue
            detected_source = entry["source"]
            employee_info = self._employee_info_from_log(log, user_map, backend, account_index)
            employee_key = employee_info["id"]
            employees.setdefault(employee_key, employee_info)
            model = str(_first(log, "model", "model_group", "model_id", default="未知模型"))
            day = _date_text_in_usage_timezone(_first(log, "startTime", "start_time", "created_at", "date"))
            key = (day, employee_key, detected_source, model)
            row = grouped.setdefault(key, self._admin_empty_row(day, employee_info, detected_source, model))
            self._add_log_to_row(row, log)

        rows = sorted(grouped.values(), key=lambda item: (item["date"], item["employeeName"], item["source"], item["model"]))
        summary_rows: list[dict[str, Any]] = []
        if not source or source == "all":
            try:
                summary_rows = await self._team_daily_activity_rows(start_date, end_date, team_id, {team_id.lower(): {"id": team_id, "name": team_info["name"]}}, backend)
            except HTTPException:
                summary_rows = []

        result = {
            "rows": rows,
            "summaryRows": summary_rows or rows,
            "employees": self._admin_employee_summaries_with_zeroes(rows, employees),
            "team": team_info,
            "pageLimit": scan["pageLimit"],
            "pageSize": scan["pageSize"],
            "pagesRead": scan["pagesRead"],
            "totalPages": scan["totalPages"],
            "totalRecords": scan["totalRecords"],
            "truncated": scan["truncated"],
            "dataQuality": {
                "summarySource": "team_daily_activity" if summary_rows else "spend_logs",
                "rankingSource": "spend_logs",
                "timezoneOffsetMinutes": usage_timezone_offset_minutes(),
            },
        }
        duration_ms = round((time.perf_counter() - started) * 1000)
        logger.info(
            "usage board=team cache_hit=%s pages=%s/%s rows=%s employees=%s took=%sms",
            bool(scan.get("cache", {}).get("hit")),
            scan["pagesRead"],
            scan["totalPages"] or "?",
            len(rows),
            len(result["employees"]),
            duration_ms,
        )
        return result

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

    def _admin_employee_summaries(self, rows: list[dict[str, Any]], employees: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
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

    async def models(self) -> list[dict[str, Any]]:
        hit, value, _ = self._model_cache.get("models")
        if hit:
            return value
        models = []
        seen_keys: set[tuple[str, str]] = set()
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
                model_name = str(_first(item, "model_name", "model", "id", "litellm_model_name", default=f"model-{index + 1}"))
                dedupe_key = (backend.id, model_name.lower())
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
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
                    }
                )
        self._model_cache.set("models", models, _env_int("MODEL_CACHE_TTL_SECONDS", 1800))
        return models


def default_date_range(days: int = 30) -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()
