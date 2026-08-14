from backend.main import _stability_model_ranking_key


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
