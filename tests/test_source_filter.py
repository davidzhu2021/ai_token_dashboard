from backend.main import apply_usage_source_filter, normalize_usage_sources


def test_normalize_usage_sources_supports_repeated_and_comma_values() -> None:
    assert normalize_usage_sources(["Cursor", "Claude Code,Cursor", "all"]) == {"Cursor", "Claude Code"}


def test_apply_usage_source_filter_recomputes_rows_and_summary() -> None:
    payload = {
        "rows": [
            {"source": "Cursor", "model": "m", "totalTokens": 4},
            {"source": "Her", "model": "m", "totalTokens": 9},
        ],
        "summaryRows": [
            {"source": "Cursor", "model": "m", "totalTokens": 4},
            {"source": "Her", "model": "m", "totalTokens": 9},
        ],
    }
    filtered = apply_usage_source_filter(payload, ["Cursor"])
    assert [row["source"] for row in filtered["rows"]] == ["Cursor"]
    assert filtered["summaryRows"][0]["source"] == "Cursor"


def test_normalize_usage_sources_accepts_all_source_selection() -> None:
    assert normalize_usage_sources("Cursor,Claude Code,Her,其他") == {"Cursor", "Claude Code", "Her", "其他"}


def test_model_filter_preserves_preaggregated_rankings_when_detail_rows_are_omitted() -> None:
    from backend.main import apply_usage_model_filter

    payload = {
        "rows": [],
        "summaryRows": [{"model": "gpt", "totalTokens": 10}],
        "departments": [{"departmentId": "d1", "departmentName": "研发部", "totalTokens": 10}],
        "employees": [{"employeeId": "u1", "employeeName": "张三", "totalTokens": 10}],
    }

    filtered = apply_usage_model_filter(payload, ["gpt"])

    assert filtered["departments"] == payload["departments"]
    assert filtered["employees"] == payload["employees"]


def test_model_filter_preserves_rankings_when_rows_are_summary_only() -> None:
    from backend.main import apply_usage_model_filter

    payload = {
        "rows": [{"model": "gpt", "totalTokens": 10}],
        "summaryRows": [{"model": "gpt", "totalTokens": 10}],
        "departments": [{"departmentId": "d1", "departmentName": "研发部", "totalTokens": 10}],
        "employees": [{"employeeId": "u1", "employeeName": "张三", "totalTokens": 10}],
    }

    filtered = apply_usage_model_filter(payload, ["gpt"])

    assert filtered["departments"] == payload["departments"]
    assert filtered["employees"] == payload["employees"]
