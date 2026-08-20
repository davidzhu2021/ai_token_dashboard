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

    async def stability_overview_aggregates(self, start_date: str, end_date: str, model: str = ""):
        return {
            "overall": {"request_count": 2, "status_count": 2, "failure_known_count": 2, "failure_count": 0, "ttft_sample_count": 0},
            "daily": [],
            "models": [{"dimension": "model-a", "request_count": 2, "status_count": 2, "failure_known_count": 2, "failure_count": 0, "ttft_sample_count": 0}],
            "scenarios": [],
            "attempts": {"attempt_count": 2, "attempt_status_count": 2, "retry_count": 2, "retry_recovered_count": 1},
            "modelAttempts": [{"dimension": "model-a", "attempt_count": 2, "attempt_status_count": 2, "fallback_count": 1, "fallback_recovered_count": 1}],
        }

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

    async def stability_scenario_samples(self, start_date: str, end_date: str, *, model: str = "", scenario: str = "", error_code: str = "", page: int = 1, page_size: int = 20):
        rows = [
            {
                "request_id": "req-1",
                "backend_id": "primary",
                "event_time": datetime(2026, 8, 12, tzinfo=timezone.utc),
                "model": "model-a",
                "model_group": "group-a",
                "scenario": "overload",
                "error_code": "429",
                "status": "failure",
                "user_visible_failure": True,
                "attempted_retries": 1,
                "ttft_ms": 800,
            },
            {
                "request_id": "req-2",
                "backend_id": "primary",
                "event_time": datetime(2026, 8, 12, tzinfo=timezone.utc),
                "model": "未知模型",
                "model_group": None,
                "scenario": "http_4xx",
                "error_code": "401",
                "status": "failure",
                "user_visible_failure": True,
                "attempted_retries": 0,
                "ttft_ms": None,
            },
        ]
        if model:
            rows = [row for row in rows if row["model"] == model or row.get("model_group") == model]
        return {
            "items": rows,
            "total": len(rows),
            "modelOptions": [
                {"name": "model-a", "count": 1},
                {"name": "group-a", "count": 1},
                {"name": "未知模型", "count": 1},
            ],
        }

    async def api_cost_rows(self, start_date: str, end_date: str):
        return [{"usage_date": date(2026, 8, 1), "source": "Codex", "model": "model-a", "model_group": "gpt 系列", "spend": 120.0}]

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
        monkeypatch.setattr(
            main,
            "require_observability_dashboard",
            lambda request: {"email": "admin@auto-link.com.cn", "isPlatformAdmin": True},
        )
    else:
        from fastapi import HTTPException

        def reject(request):
            raise HTTPException(status_code=403, detail="forbidden")

        monkeypatch.setattr(main, "require_observability_dashboard", reject)
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
    ranking = payload["data"]["modelRankings"][0]
    assert ranking["fallbackRecoveryStatus"] == "observed"
    assert ranking["fallbackRecoveryRate"] == 1.0
    detail = client.get("/api/admin/stability/requests/req-1")
    assert detail.status_code == 200
    assert "messages" not in detail.json()["data"]
    assert "response" not in detail.json()["data"]

    ordinary = _client(monkeypatch, platform_admin=False)
    assert ordinary.get("/api/admin/stability/overview").status_code == 403


def test_stability_scenarios_return_model_options_and_filter_by_model_or_group(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_EMAILS", "admin@auto-link.com.cn")
    client = _client(monkeypatch)
    payload = client.get("/api/admin/stability/scenarios?start_date=2026-08-06&end_date=2026-08-12").json()["data"]
    assert payload["total"] == 2
    assert [item["name"] for item in payload["modelOptions"]] == ["model-a", "group-a", "未知模型"]
    assert payload["items"][0]["requestId"] == "req-1"
    assert payload["items"][1]["model"] == "未知模型"

    by_model = client.get("/api/admin/stability/scenarios?start_date=2026-08-06&end_date=2026-08-12&model=model-a").json()["data"]
    assert by_model["total"] == 1
    assert by_model["items"][0]["model"] == "model-a"

    by_group = client.get("/api/admin/stability/scenarios?start_date=2026-08-06&end_date=2026-08-12&model=group-a").json()["data"]
    assert by_group["total"] == 1
    assert by_group["items"][0]["model"] == "model-a"


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


def test_cost_overview_accepts_explicit_date_range_and_rejects_partial_or_invalid_ranges(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_EMAILS", "admin@auto-link.com.cn")
    monkeypatch.setattr(main, "require_observability_dashboard", lambda request: {"email": "admin@auto-link.com.cn", "isPlatformAdmin": True})
    client = _client(monkeypatch)
    response = client.get("/api/admin/costs/overview?start_date=2026-07-30&end_date=2026-08-02&as_of=2026-08-02")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["startDate"] == "2026-07-30"
    assert data["endDate"] == "2026-08-02"
    assert client.get("/api/admin/costs/overview?start_date=2026-08-01").status_code == 400
    assert client.get("/api/admin/costs/overview?start_date=bad&end_date=2026-08-01").status_code == 400
    assert client.get("/api/admin/costs/overview?start_date=2026-08-02&end_date=2026-08-01").status_code == 400


def test_cost_overview_prorates_cross_month_budget_and_anchors_annual_data_to_end_date(monkeypatch) -> None:
    class CrossMonthStore(FakeObservabilityStore):
        async def list_cost_budgets(self):
            return [
                {"month": "2026-07-01", "budget_usd": 3100, "daily_target_usd": 100},
                {"month": "2026-08-01", "budget_usd": 6200, "daily_target_usd": 200},
            ]

    monkeypatch.setattr(main, "usage_store", lambda: CrossMonthStore())
    monkeypatch.setattr(main, "require_observability_dashboard", lambda request: {"email": "admin@auto-link.com.cn", "isPlatformAdmin": True})
    monkeypatch.setenv("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", "true")
    payload = TestClient(main.app).get(
        "/api/admin/costs/overview?start_date=2026-07-31&end_date=2026-08-02&as_of=2026-08-02"
    ).json()["data"]
    assert payload["metrics"]["intervalBudget"] == 500
    assert [item["budget"] for item in payload["trend"]] == [100, 200, 200]
    assert payload["annual"]["year"] == 2026


def test_cost_overview_returns_model_cost_share_and_daily_zero_fill(monkeypatch) -> None:
    monkeypatch.setattr(main, "usage_store", lambda: FakeModelCostShareStore())
    monkeypatch.setattr(main, "require_observability_dashboard", lambda request: {"email": "admin@auto-link.com.cn", "isPlatformAdmin": True})
    monkeypatch.setenv("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", "true")
    payload = TestClient(main.app).get("/api/admin/costs/overview?month=2026-08&as_of=2026-08-03").json()["data"]
    assert payload["modelCostShare"] == [
        {
                "model": "gpt 系列",
                "spend": 150.0,
                "share": 0.75,
                "optimizationSpace": 0.0,
                "medianDaily": None,
                "rawModels": ["gpt-4o", "gpt-4o-mini"],
            "rawModelGroups": ["gpt 系列"],
            "daily": [
                    {"date": "2026-08-01", "spend": 120.0, "opportunity": 0.0},
                    {"date": "2026-08-02", "spend": 0.0, "opportunity": 0.0},
                    {"date": "2026-08-03", "spend": 30.0, "opportunity": 0.0},
            ],
        },
        {
                "model": "claude-3",
                "spend": 50.0,
                "share": 0.25,
                "optimizationSpace": 0.0,
                "medianDaily": None,
                "rawModels": ["claude-3"],
            "rawModelGroups": [],
            "daily": [
                    {"date": "2026-08-01", "spend": 0.0, "opportunity": 0.0},
                    {"date": "2026-08-02", "spend": 50.0, "opportunity": 0.0},
                    {"date": "2026-08-03", "spend": 0.0, "opportunity": 0.0},
            ],
        },
    ]


class FakeModelCostShareStore(FakeObservabilityStore):
    async def api_cost_rows(self, start_date: str, end_date: str):
        return [
            {"usage_date": date(2026, 8, 1), "source": "Codex", "model": "gpt-4o", "model_group": "gpt 系列", "spend": 120.0},
            {"usage_date": date(2026, 8, 3), "source": "Codex", "model": "gpt-4o-mini", "model_group": "gpt 系列", "spend": 30.0},
            {"usage_date": date(2026, 8, 2), "source": "Claude Code", "model": "claude-3", "model_group": "", "spend": 50.0},
        ]


class FakeCanonicalModelCostStore(FakeObservabilityStore):
    async def api_cost_rows(self, start_date: str, end_date: str):
        return [
            {"usage_date": date(2026, 8, 1), "source": "Codex", "model": "anthropic.claude-opus-4-8", "spend": 40.0},
            {"usage_date": date(2026, 8, 1), "source": "Codex", "model": "bedrock/anthropic.claude-opus-4-8", "spend": 30.0},
            {"usage_date": date(2026, 8, 2), "source": "Codex", "model": "openrouter/anthropic/claude-opus-4-8", "spend": 20.0},
            {"usage_date": date(2026, 8, 2), "source": "Codex", "model": "chatgpt-acct-84-gpt-5.6-sol", "spend": 10.0},
            {"usage_date": date(2026, 8, 2), "source": "Codex", "model": "gpt-4o", "model_group": "企业 GPT-4o", "spend": 5.0},
            {"usage_date": date(2026, 8, 2), "source": "Codex", "model": "custom-model", "spend": 1.0},
        ]


def test_cost_model_share_normalizes_equivalent_model_names_and_drills_into_all_rows(monkeypatch) -> None:
    monkeypatch.setattr(main, "usage_store", lambda: FakeCanonicalModelCostStore())
    monkeypatch.setattr(main, "require_observability_dashboard", lambda request: {"email": "admin@auto-link.com.cn", "isPlatformAdmin": True})
    monkeypatch.setenv("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", "true")
    client = TestClient(main.app)

    overview = client.get("/api/admin/costs/overview?month=2026-08&as_of=2026-08-02").json()["data"]
    by_model = {item["model"]: item for item in overview["modelCostShare"]}
    assert by_model["claude-opus-4-8"]["spend"] == 90.0
    assert by_model["claude-opus-4-8"]["rawModels"] == [
        "anthropic.claude-opus-4-8",
        "bedrock/anthropic.claude-opus-4-8",
        "openrouter/anthropic/claude-opus-4-8",
    ]
    assert by_model["gpt-5.6-sol"]["spend"] == 10.0
    assert by_model["企业 GPT-4o"]["spend"] == 5.0
    assert by_model["custom-model"]["spend"] == 1.0

    drilldown = client.get(
        "/api/admin/costs/ledger?start_date=2026-08-01&end_date=2026-08-02&as_of=2026-08-02&canonical_model=claude-opus-4-8"
    ).json()["data"]
    assert drilldown["total"] == 3
    assert {item["model"] for item in drilldown["items"]} == {
        "anthropic.claude-opus-4-8",
        "bedrock/anthropic.claude-opus-4-8",
        "openrouter/anthropic/claude-opus-4-8",
    }


def test_cost_overview_filters_api_and_manual_costs_consistently(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_EMAILS", "admin@auto-link.com.cn")
    monkeypatch.setenv("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", "true")
    monkeypatch.setattr(main, "usage_store", lambda: FakeCostFilterStore())
    monkeypatch.setattr(main, "require_observability_dashboard", lambda request: {"email": "admin@auto-link.com.cn", "isPlatformAdmin": True})
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
    monkeypatch.setattr(main, "require_observability_dashboard", lambda request: {"email": "admin@auto-link.com.cn", "isPlatformAdmin": True})
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
    monkeypatch.setattr(main, "require_observability_dashboard", lambda request: {"email": "admin@auto-link.com.cn", "isPlatformAdmin": True})
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
    monkeypatch.setattr(main, "require_observability_dashboard", lambda request: {"email": "admin@auto-link.com.cn", "isPlatformAdmin": True})
    monkeypatch.setenv("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", "true")
    response = TestClient(main.app).get("/api/admin/costs/annual?year=2026&as_of=2026-08-12")
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["year"] == 2026
    assert len(payload["months"]) == 12
    assert payload["actual"] >= 24.5
