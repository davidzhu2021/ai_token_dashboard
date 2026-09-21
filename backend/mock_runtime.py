"""Deterministic in-memory dependencies for local dashboard development."""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone
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
        account = self.runtime.entitlements.setdefault(user_id, {"budget": 0.0, "models": []})
        return {"user_id": user_id, "spend": 0.0, "max_budget": account["budget"], "models": account["models"]}

    async def set_user_budget(self, user_id: str, budget: float) -> dict[str, Any]:
        self.runtime.entitlements.setdefault(user_id, {"budget": 0.0, "models": []})["budget"] = float(budget)
        return {"user_id": user_id, "max_budget": float(budget)}

    async def grant_default_models(self, user_id: str, models: list[str]) -> list[str]:
        self.runtime.entitlements.setdefault(user_id, {"budget": 0.0, "models": []})["models"] = list(models)
        return list(models)

    async def raise_key_daily_budgets(self, user_id: str, budget: float) -> list[dict[str, Any]]:
        return []

    async def create_key(self, user_id: str, name: str, purpose: str, expires: str, models: list[str], changed_by: str) -> dict[str, Any]:
        normalized = user_id if ":" in user_id else f"primary:{user_id}"
        item = _key(f"mock-key-{len(self.runtime.keys.setdefault(normalized, [])) + 1:03d}", name)
        item["models"] = list(models or [model["displayName"] for model in MODELS])
        self.runtime.keys[normalized].append(item)
        return {**item, "key": "sk-mock-local-preview"}

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
    pool = True

    def __init__(self) -> None:
        self.accounts: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.organization_orders: dict[str, dict[str, Any]] = {}

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def get_account(self, user_id: str) -> dict[str, Any]:
        return copy.deepcopy(self.accounts.setdefault(str(user_id), {
            "userId": str(user_id), "balanceUsd": 0.0, "topupTotalUsd": 0.0, "updatedAt": "",
        }))

    async def create_order(self, trade_no: str, user_id: str, channel: str, amount_usd: float, money_cny: float, exchange_rate: float, payment_method: str = "", cursor_amount_usd: float = 0.0, claude_code_amount_usd: float = 0.0, status: str = "pending") -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        self.orders[str(trade_no)] = {
            "tradeNo": str(trade_no), "userId": str(user_id), "channel": str(channel),
            "amountUsd": float(amount_usd), "moneyCny": float(money_cny),
            "exchangeRate": float(exchange_rate), "status": str(status),
            "paymentMethod": str(payment_method), "upstreamTradeNo": "", "syncState": "",
            "syncError": "", "payerNote": "", "reviewNote": "", "reviewedBy": "",
            "submittedAt": "", "createdAt": now, "completedAt": "",
        }
        return {"tradeNo": str(trade_no), "status": str(status)}

    async def list_user_orders(self, user_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        items = [copy.deepcopy(item) for item in self.orders.values() if item["userId"] == str(user_id)]
        items.sort(key=lambda item: item["createdAt"], reverse=True)
        return {"items": items[offset:offset + limit], "total": len(items)}

    async def settle_order(self, trade_no: str, upstream_trade_no: str = "", notify_payload: str | None = None, reviewed_by: str = "", review_note: str = "") -> dict[str, Any]:
        order = self.orders.get(str(trade_no))
        if order is None:
            raise RuntimeError("充值订单不存在")
        settled = order["status"] == "pending"
        if settled:
            now = datetime.now(timezone.utc).isoformat()
            order.update({"status": "success", "completedAt": now, "upstreamTradeNo": upstream_trade_no, "syncState": "pending", "reviewedBy": reviewed_by, "reviewNote": review_note})
            account = self.accounts.setdefault(order["userId"], {"userId": order["userId"], "balanceUsd": 0.0, "topupTotalUsd": 0.0, "updatedAt": ""})
            account["balanceUsd"] += order["amountUsd"]
            account["topupTotalUsd"] += order["amountUsd"]
            account["updatedAt"] = now
        return {"settled": settled, "order": copy.deepcopy(order), "account": await self.get_account(order["userId"])}

    async def mark_sync_state(self, trade_no: str, state: str, error: str = "") -> None:
        order = self.orders.get(str(trade_no))
        if order:
            order["syncState"] = str(state)
            order["syncError"] = str(error)

    async def create_organization_order(self, trade_no, organization_id, operator_user_id, channel, amount_usd, money_cny, exchange_rate, payment_method, idempotency_key=""):
        existing = next((item for item in self.organization_orders.values() if item["organizationId"] == str(organization_id) and idempotency_key and item["idempotencyKey"] == idempotency_key), None)
        if existing:
            return copy.deepcopy(existing)
        now = datetime.now(timezone.utc).isoformat()
        item = {"tradeNo": str(trade_no), "organizationId": str(organization_id), "operatorUserId": str(operator_user_id or ""), "channel": str(channel), "amountUsd": float(amount_usd), "moneyCny": float(money_cny), "exchangeRate": float(exchange_rate), "status": "pending", "paymentMethod": str(payment_method or ""), "upstreamTradeNo": "", "payerNote": "", "reviewNote": "", "reviewedBy": "", "externalReference": "", "idempotencyKey": str(idempotency_key or ""), "syncState": "", "syncError": "", "createdAt": now, "submittedAt": "", "completedAt": ""}
        self.organization_orders[str(trade_no)] = item
        return copy.deepcopy(item)

    async def get_organization_order(self, trade_no):
        return copy.deepcopy(self.organization_orders.get(str(trade_no)))

    async def list_organization_orders(self, organization_id, limit=50, offset=0):
        items = [copy.deepcopy(item) for item in self.organization_orders.values() if item["organizationId"] == str(organization_id)]
        items.sort(key=lambda item: item["createdAt"], reverse=True)
        return {"items": items[offset:offset + limit], "total": len(items), "limit": limit, "offset": offset}

    async def list_all_organization_orders(self, keyword="", limit=50, offset=0):
        needle = str(keyword or '').casefold()
        items = [copy.deepcopy(item) for item in self.organization_orders.values() if not needle or needle in item["tradeNo"].casefold() or needle in item["organizationId"].casefold()]
        items.sort(key=lambda item: item["createdAt"], reverse=True)
        return {"items": items[offset:offset + limit], "total": len(items), "limit": limit, "offset": offset}

    async def submit_organization_payment(self, trade_no, organization_id, payer_note):
        item = self.organization_orders.get(str(trade_no))
        if not item or item["organizationId"] != str(organization_id) or item["status"] != "pending":
            raise RuntimeError("企业充值订单不存在或已处理")
        item["payerNote"] = str(payer_note or "")[:500]
        item["submittedAt"] = datetime.now(timezone.utc).isoformat()
        return copy.deepcopy(item)

    async def settle_organization_order(self, trade_no, *, reviewed_by="", review_note="", upstream_trade_no=""):
        item = self.organization_orders.get(str(trade_no))
        if not item:
            raise RuntimeError("企业充值订单不存在")
        if item["status"] != "pending":
            return {"settled": False, "order": copy.deepcopy(item)}
        item.update({"status": "success", "reviewedBy": str(reviewed_by or ""), "reviewNote": str(review_note or ""), "upstreamTradeNo": str(upstream_trade_no or ""), "completedAt": datetime.now(timezone.utc).isoformat(), "syncState": "pending"})
        return {"settled": True, "order": copy.deepcopy(item)}

    async def fail_organization_order(self, trade_no, *, reviewed_by="", review_note=""):
        item = self.organization_orders.get(str(trade_no))
        if not item or item["status"] != "pending":
            return False
        item.update({"status": "failed", "reviewedBy": str(reviewed_by or ""), "reviewNote": str(review_note or ""), "completedAt": datetime.now(timezone.utc).isoformat()})
        return True


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
        self.entitlements: dict[str, dict[str, Any]] = {}
        self.keys: dict[str, list[dict[str, Any]]] = {"primary:owner": [_key("mock-key-owner-001", "本地 Codex 密钥"), _key("mock-key-owner-002", "本地 Claude Code 密钥")], "primary:admin": [_key("mock-key-admin-001", "管理脚本密钥")]}
