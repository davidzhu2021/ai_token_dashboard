from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi.testclient import TestClient

from backend import main


class FakeObservabilityStore:
    async def stability_events(self, start_date: str, end_date: str, model: str = ""):
        rows = [
            {
                "backend_id": "primary",
                "request_id": "req-1",
                "event_time": datetime(2026, 8, 12, tzinfo=timezone.utc),
                "usage_date": date(2026, 8, 12),
                "model": "model-a",
                "status": "success",
                "scenario": "overload",
                "attempted_retries": 1,
                "ttft_ms": 800,
                "user_visible_failure": False,
                "error_code": "429",
                "collected_at": datetime(2026, 8, 12, tzinfo=timezone.utc),
            },
            {
                "backend_id": "primary",
                "request_id": "req-2",
                "event_time": datetime(2026, 8, 12, tzinfo=timezone.utc),
                "usage_date": date(2026, 8, 12),
                "model": "model-a",
                "status": "failure",
                "scenario": "timeout",
                "attempted_retries": 1,
                "ttft_ms": None,
                "user_visible_failure": True,
                "error_code": "504",
                "collected_at": datetime(2026, 8, 12, tzinfo=timezone.utc),
            },
        ]
        return [row for row in rows if not model or row["model"] == model]

    async def stability_sync_states(self):
        return [{"backend_id": "primary", "window_start": date(2026, 8, 6), "window_end": date(2026, 8, 12), "partial": False}]

    async def stability_request(self, request_id: str):
        if request_id != "req-1":
            return None
        return {
            "request_id": request_id,
            "model": "model-a",
            "status": "success",
            "error_message": "redacted",
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "messages": "must-not-leak",
            "response": "must-not-leak",
        }

    async def api_cost_rows(self, start_date: str, end_date: str):
        return [{"usage_date": date(2026, 8, 1), "source": "Codex", "model": "model-a", "spend": 120.0}]

    async def list_cost_items(self):
        return []

    async def list_cost_budgets(self):
        return []

    async def list_savings_actions(self):
        return []

class FakeCostFilterStore(FakeObservabilityStore):
    async def api_cost_rows(self, start_date: str, end_date: str):
        return [
            {"usage_date": date(2026, 8, 1), "source": "Codex", "model": "model-a", "spend": 120.0},
            {"usage_date": date(2026, 8, 1), "source": "Claude Code", "model": "model-b", "spend": 80.0},
        ]

    async def list_cost_items(self):
        return [
            {
                "id": "item-a", "category": "平台", "name": "平台成本", "vendor": "Vendor A",
                "model": "model-a", "business_scope": "", "amount": 31, "currency": "USD",
                "exchange_rate": 1, "amount_usd": 31, "service_start_date": date(2026, 8, 1),
                "service_end_date": date(2026, 8, 31), "finance_bucket": "", "notes": "", "enabled": True,
            },
            {
                "id": "item-b", "category": "基础设施", "name": "公共成本", "vendor": "Vendor B",
                "model": "", "business_scope": "", "amount": 31, "currency": "USD",
                "exchange_rate": 1, "amount_usd": 31, "service_start_date": date(2026, 8, 1),
                "service_end_date": date(2026, 8, 31), "finance_bucket": "", "notes": "", "enabled": True,
            },
        ]


class FakeCostLedgerStore(FakeObservabilityStore):
    async def api_cost_rows(self, start_date: str, end_date: str):
        return [
            {
                "usage_date": date(2026, 8, 12),
                "backend_id": "primary",
                "user_id": "acct-api",
                "organization_id": "org-1",
                "team_id": "team-1",
                "key_id": "key-1",
                "principal_id": "member-1",
                "source": "Codex",
                "model": "model-a",
                "spend": 12.5,
            }
        ]

    async def list_cost_items(self):
        return [
            {
                "id": "manual-1", "category": "订阅", "cost_bucket": "subscription",
                "source_type": "subscription", "name": "账号订阅", "vendor": "Vendor A",
                "provider": "Provider A", "account_id": "acct-1", "account_name": "备用账号",
                "model": "", "business_scope": "", "amount": 31, "currency": "USD",
                "exchange_rate": 1, "amount_usd": 31, "service_start_date": date(2026, 8, 1),
                "service_end_date": date(2026, 8, 31), "finance_bucket": "IT",
                "voucher_no": "V-1", "invoice_no": "I-1", "recognition_status": "actual",
                "reconciliation_status": "matched", "notes": "", "enabled": True,
            }
        ]

    async def list_savings_actions(self):
        return [
            {
                "id": "save-1", "name": "切换套餐", "baseline_daily_cost": 10,
                "implemented_date": date(2026, 8, 10), "verified_date": None,
                "verified_daily_cost": None, "owner": "Alice", "status": "planned",
                "expected_daily_cost": 6, "expected_start_date": date(2026, 8, 20),
                "provider": "Provider A", "model": "model-a", "cost_bucket": "subscription",
                "evidence_url": "https://example.test/evidence", "finance_reviewer": "Bob", "notes": "",
            }
        ]


def _client(monkeypatch, *, platform_admin: bool = True) -> TestClient:
    monkeypatch.setenv("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", "true")
    monkeypatch.setattr(main, "usage_store", lambda: FakeObservabilityStore())
    fake_client = type("FakeClient", (), {"backends": [type("B", (), {"id": "primary"})()]})()
    monkeypatch.setattr(main, "client", lambda: fake_client)
    client = TestClient(main.app)
    if platform_admin:
        monkeypatch.setattr(main, "require_platform_admin", lambda request: {"email": "admin@auto-link.com.cn"})
    else:
        from fastapi import HTTPException

        def reject(request):
            raise HTTPException(status_code=403, detail="forbidden")

        monkeypatch.setattr(main, "require_platform_admin", reject)
    return client


def test_stability_overview_and_request_metadata_are_admin_only(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_EMAILS", "admin@auto-link.com.cn")
    client = _client(monkeypatch)
    response = client.get("/api/admin/stability/overview?start_date=2026-08-06&end_date=2026-08-12")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    payload = response.json()
    assert payload["data"]["overview"]["retryRecoveryRate"] == 0.5
    assert payload["coverage"]["partial"] is False
    detail = client.get("/api/admin/stability/requests/req-1")
    assert detail.status_code == 200
    assert "messages" not in detail.json()["data"]
    assert "response" not in detail.json()["data"]

    ordinary = _client(monkeypatch, platform_admin=False)
    assert ordinary.get("/api/admin/stability/overview").status_code == 403


def test_cost_overview_uses_configured_defaults(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_EMAILS", "admin@auto-link.com.cn")
    monkeypatch.setenv("COST_DEFAULT_MONTHLY_BUDGET_USD", "3000")
    monkeypatch.setenv("COST_DEFAULT_DAILY_TARGET_USD", "100")
    client = _client(monkeypatch)
    response = client.get("/api/admin/costs/overview?month=2026-08")
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["metrics"]["budget"] == 3000
    assert payload["metrics"]["dailyTarget"] == 100
    assert payload["modelSplit"] == [{"model": "model-a", "spend": 120.0}]
    assert response.json()["coverage"]["incomplete"] is True


def test_cost_overview_filters_api_and_manual_costs_consistently(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_EMAILS", "admin@auto-link.com.cn")
    monkeypatch.setenv("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", "true")
    monkeypatch.setattr(main, "usage_store", lambda: FakeCostFilterStore())
    monkeypatch.setattr(main, "require_platform_admin", lambda request: {"email": "admin@auto-link.com.cn"})
    client = TestClient(main.app)

    by_model = client.get("/api/admin/costs/overview?month=2026-08&model=model-a&as_of=2026-08-12").json()["data"]
    assert by_model["metrics"]["actual"] == 132.0
    assert by_model["modelSplit"] == [{"model": "model-a", "spend": 120.0}]
    assert [item["id"] for item in by_model["costItems"]] == ["item-a"]

    api_only = client.get("/api/admin/costs/overview?month=2026-08&category=API%20Token&as_of=2026-08-12").json()["data"]
    assert api_only["metrics"]["actual"] == 200.0
    assert api_only["costItems"] == []

    by_vendor = client.get("/api/admin/costs/overview?month=2026-08&vendor=Vendor%20A&as_of=2026-08-12").json()["data"]
    assert by_vendor["metrics"]["actual"] == 12.0
    assert by_vendor["modelSplit"] == []


def test_cost_overview_adds_full_bucket_and_savings_metrics(monkeypatch) -> None:
    monkeypatch.setattr(main, "usage_store", lambda: FakeCostLedgerStore())
    monkeypatch.setattr(main, "require_platform_admin", lambda request: {"email": "admin@auto-link.com.cn"})
    monkeypatch.setenv("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", "true")
    response = TestClient(main.app).get("/api/admin/costs/overview?month=2026-08&as_of=2026-08-12")
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["metrics"]["actual"] == 24.5
    assert payload["metrics"]["verifiedSavings"] == 0
    assert payload["metrics"]["forecastSavingsRemaining"] >= 0
    assert {item["costBucket"] for item in payload["bucketSplit"]} == {"api_usage", "account_procurement"}
    assert {item["key"] for item in payload["composition"]} == {"api_usage", "account_procurement"}
    assert payload["summary"]["accountSplit"][0]["accountId"] in {"acct-1", "acct-api"}
    assert payload["summary"]["reconciliationSummary"]
    assert payload["ledger"]["total"] == 13
    assert payload["costItems"][0]["voucherNo"] == "V-1"


def test_cost_ledger_is_paginated_and_filters_reconciliation(monkeypatch) -> None:
    monkeypatch.setattr(main, "usage_store", lambda: FakeCostLedgerStore())
    monkeypatch.setattr(main, "require_platform_admin", lambda request: {"email": "admin@auto-link.com.cn"})
    monkeypatch.setenv("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", "true")
    client = TestClient(main.app)
    response = client.get(
        "/api/admin/costs/ledger?start_date=2026-08-01&end_date=2026-08-12&page=1&page_size=2"
    )
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["total"] == 13
    assert payload["pageSize"] == 2
    assert len(payload["items"]) == 2
    matched = client.get(
        "/api/admin/costs/ledger?start_date=2026-08-01&end_date=2026-08-12&reconciliation_status=matched"
    ).json()["data"]
    assert matched["total"] == 12
    assert all(item["reconciliationStatus"] == "matched" for item in matched["items"])


def test_cost_annual_returns_twelve_months(monkeypatch) -> None:
    monkeypatch.setattr(main, "usage_store", lambda: FakeCostLedgerStore())
    monkeypatch.setattr(main, "require_platform_admin", lambda request: {"email": "admin@auto-link.com.cn"})
    monkeypatch.setenv("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", "true")
    response = TestClient(main.app).get("/api/admin/costs/annual?year=2026&as_of=2026-08-12")
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["year"] == 2026
    assert len(payload["months"]) == 12
    assert payload["actual"] >= 24.5
