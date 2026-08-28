from backend.main import _stability_model_attempt_metrics, _stability_model_ranking_key, _stability_ranking_attempt_fields


def test_stability_model_attempt_metrics_tracks_fallback_status_per_model() -> None:
    events = [
        {"requested_model_group": "alpha", "event_id": "a1", "attempt_id": "1", "status": "failure", "is_fallback": True},
        {"requested_model_group": "alpha", "event_id": "a2", "attempt_id": "2", "status": "success", "is_fallback": True},
        {"requested_model_group": "beta", "event_id": "b1", "attempt_id": "1", "status": "success", "is_fallback": False},
        {"requested_model_group": "gamma", "event_id": "g1", "attempt_id": "1", "status": "unknown", "is_fallback": False},
    ]

    metrics = _stability_model_attempt_metrics(events)

    assert metrics["alpha"] == {
        "attemptCount": 2,
        "attemptStatusCount": 2,
        "fallbackAttemptCount": 2,
        "fallbackRecoveredCount": 1,
        "attemptDataAvailable": True,
        "fallbackRecoveryRate": 0.5,
        "fallbackRecoveryStatus": "observed",
    }
    assert metrics["beta"]["fallbackRecoveryStatus"] == "not_triggered"
    assert metrics["gamma"]["fallbackRecoveryStatus"] == "unavailable"


def test_stability_model_attempt_metrics_deduplicates_terminal_attempts() -> None:
    events = [
        {"requested_model_group": "alpha", "event_id": "a1", "attempt_id": "1", "status": "failure", "is_fallback": True, "event_time": "2026-08-20T01:00:00Z"},
        {"requested_model_group": "alpha", "event_id": "a1", "attempt_id": "1", "status": "success", "is_fallback": True, "event_time": "2026-08-20T01:01:00Z"},
    ]

    metrics = _stability_model_attempt_metrics(events)

    assert metrics["alpha"]["attemptCount"] == 1
    assert metrics["alpha"]["fallbackRecoveredCount"] == 1


def test_stability_model_attempt_metrics_keeps_same_attempt_from_each_backend() -> None:
    events = [
        {"backend_id": "primary", "requested_model_group": "alpha", "request_id": "req-1", "attempt_id": "1", "status": "success", "is_fallback": True},
        {"backend_id": "her", "requested_model_group": "alpha", "request_id": "req-1", "attempt_id": "1", "status": "failure", "is_fallback": True},
    ]

    metrics = _stability_model_attempt_metrics(events)

    assert metrics["alpha"]["attemptCount"] == 2
    assert metrics["alpha"]["fallbackAttemptCount"] == 2


def test_stability_model_ranking_sorts_statuses_and_metrics() -> None:
    rows = [
        {"model": "repair", "state": "需治理", "finalRequestFailureRate": 0.01, "fallbackRecoveryRate": 1, "ttftP95Ms": 200},
        {"model": "observe", "state": "观察", "finalRequestFailureRate": 0.001, "fallbackRecoveryRate": 1, "ttftP95Ms": 200},
        {"model": "stable-slow", "state": "稳定", "finalRequestFailureRate": 0.001, "fallbackRecoveryRate": 0.8, "ttftP95Ms": 900},
        {"model": "stable-best-fallback", "state": "稳定", "finalRequestFailureRate": 0.001, "fallbackRecoveryRate": 0.9, "ttftP95Ms": 1200},
        {"model": "stable-low-failure", "state": "稳定", "finalRequestFailureRate": 0.0005, "fallbackRecoveryRate": 0.1, "ttftP95Ms": 1000},
        {"model": "unknown", "state": "暂无数据", "finalRequestFailureRate": None, "fallbackRecoveryRate": None, "ttftP95Ms": None},
    ]

    assert [row["model"] for row in sorted(rows, key=_stability_model_ranking_key)] == [
        "stable-low-failure",
        "stable-best-fallback",
        "stable-slow",
        "observe",
        "repair",
        "unknown",
    ]


def test_stability_model_ranking_places_missing_metrics_after_complete_values() -> None:
    rows = [
        {"model": "complete", "state": "稳定", "finalRequestFailureRate": 0.001, "fallbackRecoveryRate": 0.9, "ttftP95Ms": 800},
        {"model": "missing-failure", "state": "稳定", "finalRequestFailureRate": None, "fallbackRecoveryRate": 1, "ttftP95Ms": 100},
        {"model": "missing-fallback", "state": "稳定", "finalRequestFailureRate": 0.001, "fallbackRecoveryRate": None, "ttftP95Ms": 100},
        {"model": "missing-ttft", "state": "稳定", "finalRequestFailureRate": 0.001, "fallbackRecoveryRate": 0.9, "ttftP95Ms": None},
    ]

    assert [row["model"] for row in sorted(rows, key=_stability_model_ranking_key)] == [
        "complete",
        "missing-ttft",
        "missing-fallback",
        "missing-failure",
    ]


def test_stability_model_ranking_merges_attempt_fields_without_overriding_failure_rate() -> None:
    row = {
        "model": "alpha",
        "state": "稳定",
        "finalRequestFailureRate": 0.01,
        "fallbackRecoveryRate": None,
        "ttftP95Ms": 900,
    }
    attempt = {
        "attempt_count": 3,
        "attempt_status_count": 3,
        "fallback_count": 2,
        "fallback_recovered_count": 1,
    }

    merged = {**row, **_stability_ranking_attempt_fields(attempt)}

    assert merged["finalRequestFailureRate"] == 0.01
    assert merged["fallbackAttemptCount"] == 2
    assert merged["fallbackRecoveryCount"] == 1
    assert merged["fallbackRecoveryRate"] == 0.5
    assert merged["fallbackRecoveryStatus"] == "observed"
