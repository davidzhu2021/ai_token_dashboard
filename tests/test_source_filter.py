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
