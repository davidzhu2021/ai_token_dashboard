from __future__ import annotations

from datetime import date

import pytest

from backend.observability import (
    SCENARIO_DEFINITIONS_VERSION,
    normalize_event,
    reviewed_savings_measurements,
    scenario_details,
    stability_metrics,
)


def test_final_failure_prefers_explicit_samples_and_reports_coverage() -> None:
    events = [
        {"status": "failure", "finalRequestFailure": False, "finalRequestFailureSource": "explicit", "ttftMs": 100},
        {"status": "failure", "finalRequestFailure": True, "finalRequestFailureSource": "derived", "ttftMs": 200},
    ]
    metrics = stability_metrics(events, period={"startDate": "2026-08-01", "endDate": "2026-08-12"}, as_of="2026-08-12")
    assert metrics["finalRequestFailureRate"] == 0
    assert metrics["finalRequestFailureSource"] == "explicit"
    assert metrics["finalRequestFailureExplicitCoverageRate"] == pytest.approx(0.5)
    assert metrics["metricEnvelopes"]["finalRequestFailureRate"]["coverageRate"] == pytest.approx(0.5)


def test_missing_explicit_failure_uses_derived_status() -> None:
    event = normalize_event({"status": "failure"})
    assert event["finalRequestFailure"] is True
    assert event["finalRequestFailureSource"] == "derived"


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"status_code": 429, "error_type": "RateLimitError"}, "overload"),
        ({"error_code": "504", "error_class": "GatewayTimeout"}, "timeout"),
        ({"error_type": "ChunkedEncodingError"}, "stream_break"),
        ({"error_type": "InvalidToolCall"}, "tool_shape"),
        ({"status_code": 200, "error_message": "missing choices in response"}, "http_200_wrong_body"),
    ],
)
def test_scenario_dictionary_is_structured_and_versioned(record: dict, expected: str) -> None:
    classified = scenario_details(record)
    assert classified["scenario"] == expected
    assert classified["version"] == SCENARIO_DEFINITIONS_VERSION


def test_attempt_events_separate_upstream_fallback_and_retry_metrics() -> None:
    final_events = [
        {"status": "success", "finalRequestFailure": False, "finalRequestFailureSource": "explicit", "ttftMs": 100},
        {"status": "failure", "finalRequestFailure": True, "finalRequestFailureSource": "explicit", "ttftMs": None},
    ]
    attempts = [
        {"trace_id": "a", "attempt_index": 0, "event_type": "attempt", "status": "failure"},
        {"trace_id": "a", "attempt_index": 1, "event_type": "retry", "status": "success"},
        {"trace_id": "b", "attempt_index": 0, "event_type": "fallback_failure", "status": "failure", "fallback_to": "route-b"},
        {"trace_id": "b", "attempt_index": 1, "event_type": "fallback_success", "status": "success", "fallback_to": "route-c"},
    ]
    metrics = stability_metrics(final_events, attempts)
    assert metrics["upstreamExceptionCount"] == 2
    assert metrics["fallbackRecoveryRate"] == 1
    assert metrics["retryRecoveryRate"] == 1
    assert metrics["ttftCoverageRate"] == pytest.approx(0.5)


def test_reviewed_savings_requires_evidence_review_and_non_overlap() -> None:
    measurements = [
        {
            "id": "accepted", "scope": "model-a", "measurementStart": "2026-08-01", "measurementEnd": "2026-08-07",
            "baselineAmountUsd": 100, "actualAmountUsd": 60, "evidenceUrl": "https://example.test/1",
            "financeReviewer": "finance", "reviewedAt": "2026-08-08T00:00:00Z", "status": "reviewed",
        },
        {
            "id": "overlap", "scope": "model-a", "measurementStart": "2026-08-05", "measurementEnd": "2026-08-10",
            "baselineAmountUsd": 100, "actualAmountUsd": 50, "evidenceUrl": "https://example.test/2",
            "financeReviewer": "finance", "reviewedAt": "2026-08-11T00:00:00Z", "status": "reviewed",
        },
        {
            "id": "missing", "scope": "model-b", "measurementStart": "2026-08-01", "measurementEnd": "2026-08-07",
            "baselineAmountUsd": 100, "actualAmountUsd": 50, "status": "reviewed",
        },
    ]
    result = reviewed_savings_measurements(measurements, date(2026, 8, 12))
    assert result["realizedSavingsUsd"] == 40
    assert result["reviewedCount"] == 1
    assert {item["exclusionReason"] for item in result["excluded"]} == {"overlapping_measurement", "pending_evidence_or_review"}
