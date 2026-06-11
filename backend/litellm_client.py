import os
from collections import defaultdict
from datetime import date, datetime, timedelta
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


def _contains_any(value: Any, words: tuple[str, ...]) -> bool:
    text = str(value or "").lower()
    return any(word in text for word in words)


def detect_source(record: dict[str, Any]) -> str:
    metadata = _first(record, "metadata", "request_tags", "tags", default={})
    values = [
        _first(record, "source", "tool", "client", "application", default=""),
        _first(record, "key_alias", "key_name", "api_key_alias", default=""),
        metadata,
    ]
    haystack = " ".join(str(value) for value in values).lower()
    if any(word in haystack for word in ("cursor", "curosr")):
        return "Cursor"
    if any(word in haystack for word in ("claude code", "claude-code", "claudecode", "cc")):
        return "Claude Code"
    return "其他"


def detect_source_from_key(key: dict[str, Any]) -> str:
    values = [key.get("name"), key.get("purpose"), key.get("masked"), key.get("id")]
    haystack = " ".join(str(value or "") for value in values).lower()
    if "cursor" in haystack:
        return "Cursor"
    if any(word in haystack for word in ("claude code", "claude-code", "claudecode", "cc")):
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
        return f"上游接口失败，HTTP {response.status_code}"

    async def resolve_user(self, email: str) -> dict[str, Any]:
        email_lower = email.lower()
        email_prefix = email_lower.split("@", 1)[0]
        for page in range(1, 51):
            payload = await self.request("GET", "/user/list", params={"page": page, "page_size": 100})
            for user in _records(payload):
                candidates = [
                    user.get("user_email"),
                    user.get("sso_user_id"),
                    user.get("user_id"),
                    user.get("user_alias"),
                ]
                if any(str(candidate or "").lower() == email_lower for candidate in candidates):
                    return user
                if any(str(candidate or "").lower() == email_prefix for candidate in candidates):
                    return user
            if isinstance(payload, dict):
                total_pages = _as_int(payload.get("total_pages"))
                if total_pages and page >= total_pages:
                    break
        raise HTTPException(status_code=404, detail="没有在上游系统中找到该员工账号")

    async def usage_rows(self, user_id: str, start_date: str, end_date: str, source: str | None) -> list[dict[str, Any]]:
        try:
            rows = await self._usage_from_key_daily_activity(user_id, start_date, end_date, source)
        except HTTPException:
            rows = []
        if rows:
            return rows

        rows: list[dict[str, Any]] = []
        try:
            rows = await self._usage_from_logs(user_id, start_date, end_date, source)
        except HTTPException:
            rows = []
        if rows:
            return rows
        return await self._usage_from_daily_activity(user_id, start_date, end_date, source)

    async def _usage_from_key_daily_activity(self, user_id: str, start_date: str, end_date: str, source: str | None) -> list[dict[str, Any]]:
        keys = await self.keys_for_user(user_id)
        rows: list[dict[str, Any]] = []
        for key in keys[:25]:
            key_source = detect_source_from_key(key)
            if source and source != "all" and key_source != source:
                continue
            key_rows = await self._usage_from_daily_activity(
                user_id=user_id,
                start_date=start_date,
                end_date=end_date,
                source="all",
                api_key=key["id"],
                source_override=key_source,
            )
            rows.extend(key_rows)
        return rows

    async def _usage_from_logs(self, user_id: str, start_date: str, end_date: str, source: str | None) -> list[dict[str, Any]]:
        max_pages = max(1, int(os.getenv("USAGE_LOG_MAX_PAGES", "20")))
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for page in range(1, max_pages + 1):
            payload = await self.request(
                "GET",
                "/spend/logs/v2",
                params={
                    "user_id": user_id,
                    "start_date": f"{start_date} 00:00:00",
                    "end_date": f"{end_date} 23:59:59",
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
                day = _date_text(_first(log, "startTime", "start_time", "created_at", "date"))
                key = (day, detected_source, model)
                row = grouped.setdefault(
                    key,
                    {
                        "date": day,
                        "source": detected_source,
                        "model": model,
                        "promptTokens": 0,
                        "completionTokens": 0,
                        "totalTokens": 0,
                        "requestCount": 0,
                        "successCount": 0,
                        "failureCount": 0,
                        "spend": 0.0,
                    },
                )
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
            total_pages = _as_int(_first(payload, "total_pages", "totalPages", default=0)) if isinstance(payload, dict) else 0
            if total_pages and page >= total_pages:
                break
        return sorted(grouped.values(), key=lambda item: (item["date"], item["source"], item["model"]))

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
            metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else item
            breakdown = item.get("breakdown") if isinstance(item.get("breakdown"), dict) else {}
            models = breakdown.get("models") if isinstance(breakdown.get("models"), dict) else {}
            model = str(_first(item, "model", "model_group", default=None) or next(iter(models.keys()), "全部模型"))
            prompt = _as_int(_first(metrics, "prompt_tokens", "promptTokens", "total_prompt_tokens"))
            completion = _as_int(_first(metrics, "completion_tokens", "completionTokens", "total_completion_tokens"))
            total = _as_int(_first(metrics, "total_tokens", "totalTokens", default=prompt + completion))
            requests = _as_int(_first(metrics, "api_requests", "total_api_requests", "requestCount"))
            failures = _as_int(_first(metrics, "failed_requests", "total_failed_requests", "failureCount"))
            rows.append(
                {
                    "date": _date_text(_first(item, "date", "day")),
                    "source": source_override or "其他",
                    "model": model,
                    "promptTokens": prompt,
                    "completionTokens": completion,
                    "totalTokens": total,
                    "requestCount": requests,
                    "successCount": max(0, requests - failures),
                    "failureCount": failures,
                    "spend": _as_number(_first(metrics, "spend", "total_spend")),
                }
            )
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
            spend = _as_number(_first(item, "spend", "total_spend"))
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
                    "spend": spend,
                    "status": status,
                }
            )
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
