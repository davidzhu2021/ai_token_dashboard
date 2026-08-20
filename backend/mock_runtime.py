"""Deterministic in-memory dependencies for local dashboard development."""

from __future__ import annotations

import copy
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException


MODELS = [
    {"id": "gpt-5.2", "modelName": "gpt-5.2", "displayName": "gpt-5.2", "familyKey": "gpt", "familyLabel": "GPT", "provider": "OpenAI", "capabilities": ["推理", "函数调用"], "description": "适合复杂编码、分析与自动化任务。", "contextWindow": "400000", "status": "可用", "recommendedFor": "复杂编码与长上下文分析", "billingType": "按量计费", "inputPricePerMillion": 1.75, "outputPricePerMillion": 14.0, "cacheReadPricePerMillion": 0.175, "cacheWritePricePerMillion": None},
    {"id": "claude-sonnet-4-6", "modelName": "claude-sonnet-4-6", "displayName": "claude-sonnet-4-6", "familyKey": "claude", "familyLabel": "Claude", "provider": "Anthropic", "capabilities": ["代码", "推理", "函数调用"], "description": "适合日常开发、代码审查和代理任务。", "contextWindow": "1000000", "status": "可用", "recommendedFor": "Claude Code 与通用研发任务", "billingType": "按量计费", "inputPricePerMillion": 3.0, "outputPricePerMillion": 15.0, "cacheReadPricePerMillion": 0.3, "cacheWritePricePerMillion": 3.75},
    {"id": "qwen3-coder-plus", "modelName": "qwen3-coder-plus", "displayName": "qwen3-coder-plus", "familyKey": "qwen", "familyLabel": "Qwen", "provider": "Alibaba", "capabilities": ["代码", "函数调用"], "description": "适合高频代码补全和批量开发任务。", "contextWindow": "262144", "status": "可用", "recommendedFor": "代码生成与仓库级修改", "billingType": "按量计费", "inputPricePerMillion": 0.8, "outputPricePerMillion": 3.2, "cacheReadPricePerMillion": 0.08, "cacheWritePricePerMillion": None},
]


def _key(identifier: str, name: str, status: str = "正常") -> dict[str, Any]:
    return {"id": identifier, "name": name, "purpose": "本地开发", "masked": f"sk-...{identifier[-4:].upper()}", "models": ["gpt-5.2", "claude-sonnet-4-6"], "createdAt": "2026-07-01 09:30", "lastUsed": "2026-08-20 10:15", "expiresAt": "永不过期", "monthTokens": 386_420, "spend": 12.84, "status": status}


class MockClient:
    def __init__(self, runtime: "MockRuntime") -> None:
        self.runtime = runtime
        self.backends = [SimpleNamespace(id="primary", label="通衢 API", source="通衢 API")]

    async def close(self) -> None:
        return None

    async def resolve_user(self, email: str, name: str | None = None) -> dict[str, Any]:
        user_id = "owner" if email.casefold() == "owner@demo.example" else email.split("@", 1)[0]
        return {"user_id": user_id, "user_email": email, "user_alias": name or email, "matched_user_ids": [f"primary:{user_id}"], "matched_accounts": [{"backend": "primary", "user_id": user_id}]}

    async def user_info(self, user_id: str) -> dict[str, Any]:
        return {"user_id": user_id, "spend": 12.84, "max_budget": 100.0}

    async def models(self, usage_counts: dict[str, int] | None = None) -> list[dict[str, Any]]:
        return copy.deepcopy(MODELS)

    async def available_key_models(self, user_id: str) -> tuple[list[str], bool]:
        return [item["displayName"] for item in MODELS], False

    async def usage_rows_for_user_ids(self, user_ids: list[str], start_date: str, end_date: str, source: str) -> list[dict[str, Any]]:
        day = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        rows: list[dict[str, Any]] = []
        index = 0
        while day <= end:
            for model_index, model in enumerate(("gpt-5.2", "claude-sonnet-4-6")):
                call_source = "Codex" if model_index == 0 else "Claude Code"
                if source not in {"", "all", call_source}:
                    continue
                prompt = 18_000 + index * 850 + model_index * 2_400
                completion = 5_000 + index * 270 + model_index * 900
                rows.append({"date": day.isoformat(), "source": call_source, "model": model, "promptTokens": prompt, "completionTokens": completion, "totalTokens": prompt + completion, "requestCount": 12 + index + model_index, "successCount": 11 + index + model_index, "failureCount": 1, "spend": round((prompt + completion) / 1_000_000 * (4.0 + model_index), 4)})
            day += timedelta(days=1)
            index += 1
        return rows

    async def keys_for_user_ids(self, user_ids: list[str], refresh: bool = False) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for account_id in user_ids:
            normalized = account_id if ":" in account_id else f"primary:{account_id}"
            backend_id, raw_user_id = normalized.split(":", 1)
            for item in self.runtime.keys.setdefault(normalized, []):
                rows.append({**copy.deepcopy(item), "_backendId": backend_id, "_userId": raw_user_id, "_rotation": {}})
        return rows

    async def block_key(self, key_id: str, user_id: str, changed_by: str) -> dict[str, str]:
        normalized = user_id if ":" in user_id else f"primary:{user_id}"
        for item in self.runtime.keys.setdefault(normalized, []):
            if item["id"] == key_id:
                item["status"] = "已禁用"
                return {"id": key_id}
        raise HTTPException(status_code=403, detail="不能停用不属于该成员的访问密钥")

    async def delete_key(self, key_id: str, user_id: str, changed_by: str) -> dict[str, str]:
        normalized = user_id if ":" in user_id else f"primary:{user_id}"
        items = self.runtime.keys.setdefault(normalized, [])
        remaining = [item for item in items if item["id"] != key_id]
        if len(remaining) == len(items):
            raise HTTPException(status_code=403, detail="不能删除不属于该成员的访问密钥")
        self.runtime.keys[normalized] = remaining
        return {"id": key_id}

    def _decode_account_id(self, account_id: str) -> tuple[Any, str]:
        normalized = account_id if ":" in account_id else f"primary:{account_id}"
        backend_id, user_id = normalized.split(":", 1)
        return SimpleNamespace(id=backend_id), user_id


class MockUsageStore:
    def __init__(self, runtime: "MockRuntime") -> None:
        self.runtime = runtime

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def health(self) -> dict[str, Any]:
        return {"enabled": True, "connected": True, "status": "ok", "mode": "mock"}

    async def model_usage_counts(self, *args: Any, **kwargs: Any) -> dict[str, int]:
        return {"gpt-5.2": 125, "claude-sonnet-4-6": 94, "qwen3-coder-plus": 38}


class MockBillingStore:
    pool = None

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None


class MockKeyVault:
    def has(self, backend_id: str, user_id: str, key_id: str) -> bool:
        return False

    def pending_rotations(self, backend_id: str, user_id: str) -> list[dict[str, Any]]:
        return []

    def delete(self, backend_id: str, user_id: str, key_id: str) -> None:
        return None


class MockRuntime:
    def __init__(self) -> None:
        self.client = MockClient(self)
        self.usage_store = MockUsageStore(self)
        self.billing_store = MockBillingStore()
        self.key_vault = MockKeyVault()
        self.reset()

    def reset(self) -> None:
        self.keys: dict[str, list[dict[str, Any]]] = {"primary:owner": [_key("mock-key-owner-001", "本地 Codex 密钥"), _key("mock-key-owner-002", "本地 Claude Code 密钥")], "primary:admin": [_key("mock-key-admin-001", "管理脚本密钥")]}
