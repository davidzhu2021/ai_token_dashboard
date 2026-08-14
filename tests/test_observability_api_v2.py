from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date

from fastapi.testclient import TestClient

from backend import main


class V2Store:
    def __init__(self) -> None:
        self.inserted: list[dict] = []

    async def insert_stability_attempt_events(self, events: list[dict]):
        self.inserted.extend(events)
        return {"received": len(events), "inserted": len(events), "duplicates": 0}

    async def api_cost_rows(self, start_date: str, end_date: str):
        return [{"usage_date": date(2026, 8, 1), "source": "Codex", "model": "model-a", "spend": 100.0}]

    async def list_cost_items(self):
        return [
            {"id": "actual", "category": "account", "name": "actual", "amount": 31, "amount_usd": 31, "currency": "USD", "exchange_rate": 1, "service_start_date": date(2026, 8, 1), "service_end_date": date(2026, 8, 31), "recognition_status": "actual", "enabled": True},
            {"id": "planned", "category": "account", "name": "planned", "amount": 310, "amount_usd": 310, "currency": "USD", "exchange_rate": 1, "service_start_date": date(2026, 8, 1), "service_end_date": date(2026, 8, 31), "recognition_status": "planned", "plan_version_id": "plan-1", "enabled": True},
        ]

    async def list_cost_budgets(self):
        return []

    async def list_savings_actions(self):
        return []

    async def list_savings_measurements(self, **kwargs):
        return []

    async def list_cost_plan_versions(self, **kwargs):
        return [{"id": "plan-1", "year": 2026, "version": "v1", "scenario": "baseline", "status": "approved", "active": True, "coverage_complete": True, "as_of": date(2026, 8, 12)}]


def _client(monkeypatch, store: V2Store) -> TestClient:
    monkeypatch.setenv("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", "true")
    monkeypatch.setenv("OBSERVABILITY_INGEST_HMAC_SECRET", "test-secret")
    monkeypatch.setattr(main, "usage_store", lambda: store)
    monkeypatch.setattr(main, "require_platform_admin", lambda request: {"email": "admin@example.test"})
    return TestClient(main.app)


def _signed_headers(body: bytes, timestamp: str = "1786665600") -> dict[str, str]:
    # The test patches the clock skew allowance so the fixed acceptance payload is reproducible.
    digest = hashlib.sha256(body).hexdigest()
    signature = hmac.new(b"test-secret", f"{timestamp}.{digest}".encode("ascii"), hashlib.sha256).hexdigest()
    return {"content-type": "application/json", "x-observability-timestamp": timestamp, "x-observability-signature": f"sha256={signature}"}


def test_internal_ingest_verifies_hmac_whitelist_and_normalizes(monkeypatch) -> None:
    store = V2Store()
    client = _client(monkeypatch, store)
    monkeypatch.setenv("OBSERVABILITY_INGEST_MAX_SKEW_SECONDS", "999999999")
    body = json.dumps({"events": [{"eventId": "evt-1", "backendId": "primary", "traceId": "trace-1", "attemptIndex": 0, "eventType": "attempt", "status": "failure", "errorCode": "429"}]}).encode()
    response = client.post("/api/internal/observability/events", content=body, headers=_signed_headers(body))
    assert response.status_code == 200
    assert store.inserted[0]["event_id"] == "evt-1"
    assert store.inserted[0]["scenario"] == "overload"

    bad = json.dumps({"events": [{"eventId": "evt-2", "backendId": "primary", "prompt": "secret"}]}).encode()
    assert client.post("/api/internal/observability/events", content=bad, headers=_signed_headers(bad)).status_code == 400
    assert client.post("/api/internal/observability/events", content=body, headers={**_signed_headers(body), "x-observability-signature": "bad"}).status_code == 401


def test_cost_actual_excludes_planned_and_as_of_is_explicit(monkeypatch) -> None:
    store = V2Store()
    client = _client(monkeypatch, store)
    response = client.get("/api/admin/costs/overview?month=2026-08&as_of=2026-08-12")
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["asOf"] == "2026-08-12"
    assert payload["metrics"]["actual"] == 112.0
    assert [item["id"] for item in payload["costItems"]] == ["actual"]


def test_annual_official_forecast_uses_active_approved_plan_and_run_rate_is_separate(monkeypatch) -> None:
    store = V2Store()
    client = _client(monkeypatch, store)
    response = client.get("/api/admin/costs/annual?year=2026&as_of=2026-08-12")
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["asOf"] == "2026-08-12"
    assert payload["officialForecast"] is not None
    assert payload["runRateScenario"] is not None
    assert payload["metricEnvelopes"]["officialForecast"]["source"] == "actual ledger + active approved baseline plan"
