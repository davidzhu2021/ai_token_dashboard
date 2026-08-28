from backend.stability_governance import build_error_governance


def test_build_error_governance_excludes_429_and_calculates_both_shares():
    rows = [
        {"status_code": "500", "error_code": "500", "event_date": "2026-08-01", "model": "a", "request_id": "r1"},
        {"status_code": "400", "error_code": "400", "event_date": "2026-08-01", "model": "a", "request_id": "r2"},
        {"status_code": "429", "error_code": "429", "event_date": "2026-08-01", "model": "a", "request_id": "r3"},
        {"status_code": "200", "error_code": "", "event_date": "2026-08-02", "model": "a", "request_id": "r4"},
    ]

    result = build_error_governance(rows)

    assert result["overview"]["totalRequests"] == 4
    assert result["overview"]["stabilityErrorCount"] == 2
    assert result["overview"]["rateLimitCount"] == 1
    assert result["overview"]["stabilityErrorRate"] == 0.5
    assert result["errorCodes"][0]["errorCode"] == "500"
    assert result["errorCodes"][0]["errorShare"] == 0.5
    assert result["errorCodes"][0]["totalShare"] == 0.25


def test_build_error_governance_returns_daily_breakdown_and_drilldown_fields():
    rows = [
        {"status_code": "401", "error_code": "401", "error_class": "AuthError", "error_message": "bad key", "event_date": "2026-08-01", "model": "a", "provider": "p", "api_key": "hash", "request_id": "r1"},
    ]

    result = build_error_governance(rows)

    assert result["daily"][0]["errorCount"] == 1
    assert result["daily"][0]["errorCodes"]["401"] == 1
    assert result["errorCodes"][0]["samples"][0]["requestId"] == "r1"
    assert result["errorCodes"][0]["meaning"]
    assert result["errorCodes"][0]["action"]


def test_build_error_governance_provides_meaning_and_action_for_all_documented_codes():
    codes = ["NO_CODE", "400", "401", "403", "500", "200 / failure", "408", "invalid_request_error", "404", "400001", "504", "invalid_parameter_error", "invalid_argument", "502"]
    rows = [{"status_code": "500", "error_code": code, "event_date": "2026-08-01"} for code in codes]
    result = build_error_governance(rows)
    actual = {item["errorCode"]: item for item in result["errorCodes"]}
    for code in codes:
        assert actual[code]["meaning"]
        assert actual[code]["action"]
