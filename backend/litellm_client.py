import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import HTTPException


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
    for key in ("data", "results", "items", "logs", "keys", "models", "users"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


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
        self.base_url = base_url
        self.admin_key = admin_key
        self.timeout = httpx.Timeout(20.0, connect=8.0)

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.admin_key}"
        headers.setdefault("Accept", "application/json")
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise HTTPException(status_code=504, detail="上游服务响应超时，请稍后重试") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"无法连接上游服务：{exc}") from exc

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

    async def resolve_user(self, email: str) -> dict[str, Any]:
        email_lower = email.lower()
        email_prefix = email_lower.split("@", 1)[0]
        matched_users: list[dict[str, Any]] = []
        matched_user_ids: list[str] = []
        matched_sources: dict[str, list[str]] = {}

        def add_user_id(user_id: Any, source: str) -> None:
            text = str(user_id or "").strip()
            if not text:
                return
            if text not in matched_user_ids:
                matched_user_ids.append(text)
            matched_sources.setdefault(text, [])
            if source not in matched_sources[text]:
                matched_sources[text].append(source)

        for page in range(1, 51):
            payload = await self.request("GET", "/user/list", params={"page": page, "page_size": 100})
            for user in _records(payload):
                user_id = user.get("user_id")
                email_candidates = [user.get("user_email"), user.get("sso_user_id")]
                legacy_candidates = [user.get("user_id"), user.get("user_alias")]
                if any(str(candidate or "").lower() == email_lower for candidate in email_candidates):
                    matched_users.append(user)
                    add_user_id(user_id, "user_email")
                elif any(str(candidate or "").lower() == email_prefix for candidate in legacy_candidates):
                    matched_users.append(user)
                    add_user_id(user_id, "legacy_user")
            total_pages = _as_int(payload.get("total_pages")) if isinstance(payload, dict) else 0
            if total_pages and page >= total_pages:
                break

        for user_id in await self.user_ids_from_key_alias(email_prefix):
            add_user_id(user_id, "key_alias")

        if matched_user_ids:
            primary = matched_users[0].copy() if matched_users else {}
            primary.setdefault("user_id", matched_user_ids[0])
            primary["matched_user_ids"] = sorted(matched_user_ids)
            primary["matched_sources"] = matched_sources
            primary["user_email"] = email_lower
            primary.setdefault("user_alias", email_prefix)
            primary["matched_by"] = "email_and_legacy"
            return primary

        raise HTTPException(status_code=404, detail="未找到当前员工对应的用量账号")

    async def user_ids_from_key_alias(self, email_prefix: str) -> list[str]:
        user_ids: list[str] = []
        seen: set[str] = set()
        for alias in (f"cursor-{email_prefix}", f"claude-code-{email_prefix}", email_prefix):
            payload = await self.request(
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
        try:
            rows = await self._usage_from_key_daily_activity(user_id, start_date, end_date, source)
        except HTTPException:
            rows = []
        if rows:
            return rows

        try:
            rows = await self._usage_from_logs(user_id, start_date, end_date, source)
        except HTTPException:
            rows = []
        if rows:
            return rows
        return await self._usage_from_daily_activity(user_id, start_date, end_date, source)

    async def usage_rows_for_user_ids(self, user_ids: list[str], start_date: str, end_date: str, source: str | None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for user_id in user_ids:
            rows.extend(await self.usage_rows(user_id, start_date, end_date, source))
        return sorted(rows, key=lambda item: (item["date"], item["source"], item["model"]))

    async def _usage_from_key_daily_activity(self, user_id: str, start_date: str, end_date: str, source: str | None) -> list[dict[str, Any]]:
        keys = await self.keys_for_user(user_id)
        rows: list[dict[str, Any]] = []
        for key in keys[:25]:
            key_source = detect_source_from_key(key)
            if source and source != "all" and key_source != source:
                continue
            rows.extend(
                await self._usage_from_daily_activity(
                    user_id=user_id,
                    start_date=start_date,
                    end_date=end_date,
                    source="all",
                    api_key=key["id"],
                    source_override=key_source,
                )
            )
        return rows

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

    async def _usage_from_logs(self, user_id: str, start_date: str, end_date: str, source: str | None) -> list[dict[str, Any]]:
        max_pages = max(1, int(os.getenv("USAGE_LOG_MAX_PAGES", "20")))
        utc_start, utc_end = _local_date_window_as_utc_text(start_date, end_date)
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for page in range(1, max_pages + 1):
            payload = await self.request(
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
                detected_source = detect_source(log)
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
        source_override: str | None = None,
    ) -> list[dict[str, Any]]:
        if source and source != "all":
            return []
        params = {"user_id": user_id, "start_date": start_date, "end_date": end_date, "page": 1, "page_size": 1000}
        if api_key:
            params["api_key"] = api_key
        try:
            payload = await self.request("GET", "/user/daily/activity/aggregated", params=params)
        except HTTPException:
            payload = await self.request("GET", "/user/daily/activity", params=params)
        rows = []
        for item in _records(payload):
            rows.append(self._row_from_daily_activity_item(item, source_override or "其他"))
        return rows

    async def keys_for_user(self, user_id: str) -> list[dict[str, Any]]:
        payload = await self.request(
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
        return keys

    async def keys_for_user_ids(self, user_ids: list[str]) -> list[dict[str, Any]]:
        keys: list[dict[str, Any]] = []
        seen: set[str] = set()
        for user_id in user_ids:
            for key in await self.keys_for_user(user_id):
                key_id = key.get("id")
                if key_id and key_id not in seen:
                    seen.add(key_id)
                    keys.append(key)
        return keys

    async def regenerate_key(self, key_id: str, user_id: str, changed_by: str) -> str:
        owned_keys = await self.keys_for_user(user_id)
        if not any(key["id"] == key_id for key in owned_keys):
            raise HTTPException(status_code=403, detail="不能更新不属于自己的访问密钥")
        payload = await self.request(
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

    async def users(self) -> list[dict[str, Any]]:
        users: list[dict[str, Any]] = []
        for page in range(1, 101):
            payload = await self.request("GET", "/user/list", params={"page": page, "page_size": 100})
            users.extend(_records(payload))
            total_pages = _as_int(payload.get("total_pages")) if isinstance(payload, dict) else 0
            if total_pages and page >= total_pages:
                break
        return users

    async def admin_daily_activity_rows(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        payload = await self.request(
            "GET",
            "/user/daily/activity/aggregated",
            params={
                "start_date": start_date,
                "end_date": end_date,
                "timezone": usage_timezone_offset_minutes(),
            },
        )
        rows = [self._row_from_daily_activity_item(item, "其他", "全量") for item in _records(payload)]
        return sorted(rows, key=lambda item: (item["date"], item["model"]))

    async def admin_usage_rows(self, start_date: str, end_date: str, source: str | None, employee: str | None = None) -> dict[str, Any]:
        users = await self.users()
        user_map = self._admin_user_map(users)
        employee_filter = (employee or "").strip().lower()
        grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        employees: dict[str, dict[str, Any]] = {}
        max_pages = max(1, int(os.getenv("ADMIN_USAGE_LOG_MAX_PAGES", "30")))
        page_size = max(1, min(100, int(os.getenv("ADMIN_USAGE_PAGE_SIZE", "100"))))
        utc_start, utc_end = _local_date_window_as_utc_text(start_date, end_date)
        pages_read = 0
        total_pages = 0
        total_records = 0

        for page in range(1, max_pages + 1):
            payload = await self.request(
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
                raw_user = str(_first(log, "user", "user_id", "end_user", default="未绑定账号") or "未绑定账号")
                employee_info = self._admin_employee_info(raw_user, user_map)
                if employee_filter and not self._admin_employee_matches(employee_info, employee_filter):
                    continue
                detected_source = detect_source(log)
                if source and source != "all" and detected_source != source:
                    continue

                employee_key = employee_info["id"]
                employees.setdefault(employee_key, employee_info)
                model = str(_first(log, "model", "model_group", "model_id", default="未知模型"))
                day = _date_text_in_usage_timezone(_first(log, "startTime", "start_time", "created_at", "date"))
                key = (day, employee_key, detected_source, model)
                row = grouped.setdefault(key, self._admin_empty_row(day, employee_info, detected_source, model))
                self._add_log_to_row(row, log)

            if total_pages and page >= total_pages:
                break

        rows = sorted(grouped.values(), key=lambda item: (item["date"], item["employeeName"], item["source"], item["model"]))
        summary_rows: list[dict[str, Any]] = []
        if (not employee_filter) and (not source or source == "all"):
            try:
                summary_rows = await self.admin_daily_activity_rows(start_date, end_date)
            except HTTPException:
                summary_rows = []
        truncated = bool(total_pages and pages_read < total_pages)
        return {
            "rows": rows,
            "summaryRows": summary_rows or rows,
            "employees": self._admin_employee_summaries(rows, employees),
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
        return sorted(grouped.values(), key=lambda item: item["totalTokens"], reverse=True)

    async def models(self) -> list[dict[str, Any]]:
        payload = await self.request("GET", "/models")
        raw_models = _records(payload)
        if not raw_models and isinstance(payload, dict):
            values = payload.get("data") or payload.get("models") or []
            if isinstance(values, list):
                raw_models = [{"id": str(value), "model_name": str(value)} if isinstance(value, str) else value for value in values]
        models = []
        for index, item in enumerate(raw_models):
            model_name = str(_first(item, "model_name", "model", "id", "litellm_model_name", default=f"model-{index + 1}"))
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
        return models


def default_date_range(days: int = 30) -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()
