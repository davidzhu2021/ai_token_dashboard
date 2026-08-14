from datetime import date

import pytest

from backend.observability import (
    model_state,
    monthly_forecast,
    normalize_event,
    percentile,
    scenario,
    stability_metrics,
    verified_savings,
)
from backend.main import _cost_item_overlap_usd, _manual_cost_ledger_rows, _savings_totals


def test_normalize_event_reads_camel_and_snake_case_fields() -> None:
    event = normalize_event(
        {
            "startTime": "2026-08-12T00:00:00Z",
            "completionStartTime": "2026-08-12T00:00:01.250Z",
            "status": "success",
            "metadata": {
                "attempted_retries": 2,
                "maxRetries": 3,
                "trace_id": "trace-1",
                "error_information": {"error_code": "429", "error_class": "RateLimitError"},
            },
        }
    )
    assert event["ttftMs"] == pytest.approx(1250)
    assert event["attemptedRetries"] == 2
    assert event["maxRetries"] == 3
    assert event["traceId"] == "trace-1"
    assert event["scenario"] == "overload"


def test_normalize_event_reads_json_metadata_and_boolean_strings() -> None:
    event = normalize_event(
        {
            "status": "success",
            "user_visible_failure": "false",
            "metadata": '{"attempted_retries": 1, "error_information": {"error_code": "429"}}',
        }
    )
    assert event["attemptedRetries"] == 1
    assert event["userVisibleFailure"] is False


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"startTime": "2026-08-12T00:00:00Z"}, None),
        ({"startTime": "2026-08-12T00:00:01Z", "completionStartTime": "2026-08-12T00:00:00Z"}, None),
        ({"startTime": "2026-08-12T00:00:00Z", "completionStartTime": "2026-08-12T00:00:00Z"}, None),
        ({"stream": False, "startTime": "2026-08-12T00:00:00Z", "completionStartTime": "2026-08-12T00:00:01Z"}, None),
    ],
)
def test_ttft_invalid_values_are_missing(payload: dict, expected: None) -> None:
    assert normalize_event(payload)["ttftMs"] is expected


def test_ttft_equal_to_end_time_is_missing_for_non_streaming_logs() -> None:
    assert normalize_event(
        {
            "startTime": "2026-08-12T00:00:00Z",
            "completionStartTime": "2026-08-12T00:00:01Z",
            "endTime": "2026-08-12T00:00:01Z",
        }
    )["ttftMs"] is None


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("provider overload", "overload"),
        ("request timed out", "timeout"),
        ("stream closed with EOF", "stream_break"),
        ("tool schema invalid", "tool_shape"),
        ("HTTP 503", "http_5xx"),
        ("strange failure", "unknown"),
    ],
)
def test_scenario_normalization(message: str, expected: str) -> None:
    assert scenario({"status": "failure", "metadata": {"error_information": {"error_message": message}}}) == expected


def test_retry_recovery_and_percentile_metrics() -> None:
    metrics = stability_metrics(
        [
            {"status": "success", "attemptedRetries": 1, "userVisibleFailure": False, "ttftMs": 100},
            {"status": "failure", "attemptedRetries": 1, "userVisibleFailure": True, "ttftMs": 200},
            {"status": "success", "attemptedRetries": 0, "userVisibleFailure": False, "ttftMs": 300},
        ]
    )
    assert metrics["retryRecoveryRate"] == pytest.approx(0.5)
    assert metrics["userVisibleFailureRate"] == pytest.approx(1 / 3)
    assert metrics["ttftP95Ms"] == pytest.approx(percentile([100, 200, 300]))


def test_model_state_thresholds() -> None:
    assert model_state(0.01, 2000) == "稳定"
    assert model_state(0.03, 4000) == "观察"
    assert model_state(0.031, 1000) == "需治理"
    assert model_state(None, None) == "暂无数据"
    assert model_state(0.0, None) == "暂无数据"


def test_monthly_forecast_and_verified_savings() -> None:
    forecast = monthly_forecast(1200, date(2026, 8, 1), date(2026, 8, 12), 3000)
    assert forecast["dailyAverage"] == pytest.approx(100)
    assert forecast["forecast"] == pytest.approx(3100)
    assert forecast["budgetDelta"] == pytest.approx(100)
    savings = verified_savings(
        [
            {"status": "implemented", "baselineDailyCost": 100, "verifiedDailyCost": 50, "verifiedDate": "2026-08-01"},
            {"status": "verified", "baselineDailyCost": 100, "verifiedDailyCost": 80, "verifiedDate": "2026-08-10"},
            {"status": "verified", "baselineDailyCost": 50, "verifiedDailyCost": 80, "verifiedDate": "2026-08-10"},
        ],
        date(2026, 8, 12),
    )
    assert savings == pytest.approx(60)
    assert verified_savings(
        [{"status": "verified", "baselineDailyCost": 100, "verifiedDate": "2026-08-10"}],
        date(2026, 8, 12),
    ) == 0


def test_error_message_is_redacted() -> None:
    event = normalize_event({"status": "failure", "metadata": {"error_information": {"error_message": "Bearer abc sk-secret api_key=secret person@example.com 10.0.0.1 https://example.test/private prompt: confidential user content"}}})
    assert "abc" not in event["errorMessage"]
    assert "sk-secret" not in event["errorMessage"]
    assert "example.test" not in event["errorMessage"]
    assert "person@example.com" not in event["errorMessage"]
    assert "10.0.0.1" not in event["errorMessage"]
    assert "confidential user content" not in event["errorMessage"]


def test_numeric_http_status_is_normalized() -> None:
    assert normalize_event({"status": 201})["status"] == "success"
    assert normalize_event({"status": 503})["status"] == "failure"


def test_missing_status_and_retry_fields_remain_incomplete() -> None:
    event = normalize_event({})
    assert event["status"] == "unknown"
    assert event["attemptedRetries"] is None
    assert event["userVisibleFailure"] is None
    metrics = stability_metrics([event])
    assert metrics["userVisibleFailureRate"] is None
    assert metrics["retryRecoveryRate"] is None


def test_cost_item_is_prorated_across_months_and_disabled_items_are_ignored() -> None:
    item = {
        "enabled": True,
        "amount_usd": 310,
        "service_start_date": date(2026, 7, 16),
        "service_end_date": date(2026, 8, 15),
    }
    assert _cost_item_overlap_usd(item, date(2026, 8, 1), date(2026, 8, 31)) == pytest.approx(150)
    item["enabled"] = False
    assert _cost_item_overlap_usd(item, date(2026, 8, 1), date(2026, 8, 31)) == 0


def test_manual_cost_ledger_preserves_bucket_and_finance_dimensions() -> None:
    rows = _manual_cost_ledger_rows(
        {
            "id": "subscription-1",
            "enabled": True,
            "category": "订阅",
            "cost_bucket": "subscription",
            "source_type": "subscription",
            "vendor": "Vendor A",
            "provider": "Provider A",
            "account_id": "acct-1",
            "account_name": "备用账号",
            "voucher_no": "V-001",
            "invoice_no": "I-001",
            "reconciliation_status": "pending",
            "recognition_status": "actual",
            "amount": 31,
            "amount_usd": 31,
            "currency": "USD",
            "service_start_date": date(2026, 8, 1),
            "service_end_date": date(2026, 8, 31),
        },
        date(2026, 8, 10),
        date(2026, 8, 12),
    )
    assert len(rows) == 3
    assert rows[0]["costBucket"] == "account_procurement"
    assert rows[0]["sourceType"] == "subscription"
    assert rows[0]["category"] == "订阅"
    assert rows[0]["accountId"] == "acct-1"
    assert rows[0]["provider"] == "Provider A"
    assert rows[0]["reconciliationStatus"] == "pending"
    assert sum(row["amountUsd"] for row in rows) == pytest.approx(3)


def test_savings_totals_separates_realized_and_future_expected() -> None:
    totals = _savings_totals(
        [
            {
                "status": "verified",
                "baselineDailyCost": 100,
                "verifiedDailyCost": 80,
                "verifiedDate": "2026-08-01",
            },
            {
                "status": "planned",
                "baselineDailyCost": 100,
                "expectedDailyCost": 70,
                "expectedStartDate": "2026-08-20",
            },
        ],
        date(2026, 8, 12),
        date(2026, 8, 31),
    )
    assert totals["realizedSavingsToDate"] == pytest.approx(240)
    assert totals["forecastSavingsRemaining"] == pytest.approx(360)
