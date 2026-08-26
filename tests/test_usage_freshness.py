from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend import main


def _payload(monkeypatch, primary_age: int, her_age: int) -> dict:
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(main, "realtime_enabled", lambda: True)
    monkeypatch.setattr(main, "usage_today", lambda: now.date())
    monkeypatch.setattr(main, "usage_backend_ids", lambda: ["primary", "her"])
    monkeypatch.setattr(
        main,
        "_usage_realtime_read_status",
        {
            "latestEventAt": now,
            "revision": 7,
            "ready": True,
            "backfillActive": False,
            "latestEventLagSeconds": 1,
            "verifiedThrough": {
                "primary": now - timedelta(seconds=primary_age),
                "her": now - timedelta(seconds=her_age),
            },
            "settlementStatuses": {
                "primary": {"status": "settled", "error": ""},
                "her": {"status": "settled", "error": ""},
            },
        },
    )
    payload: dict = {}
    return main.attach_snapshot_freshness(
        payload,
        now,
        now.date().isoformat(),
        now.date().isoformat(),
        "7",
    )


def test_freshness_is_settled_only_when_all_backends_are_current(monkeypatch) -> None:
    freshness = _payload(monkeypatch, primary_age=30, her_age=45)["dataFreshness"]

    assert freshness["settlementState"] == "settled"
    assert freshness["verifiedThroughByBackend"]["primary"]
    assert freshness["verifiedThrough"] == freshness["verifiedThroughByBackend"]["her"]
    assert freshness["unsettledBackends"] == []


def test_freshness_stays_verifying_when_one_backend_lags(monkeypatch) -> None:
    freshness = _payload(monkeypatch, primary_age=30, her_age=900)["dataFreshness"]

    assert freshness["settlementState"] == "verifying"
    assert freshness["unsettledBackends"] == ["her"]
    assert freshness["verificationLagSeconds"] >= 900
