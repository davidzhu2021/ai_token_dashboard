from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_model_filter_isolated_by_board_scope() -> None:
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")

    assert "function updateDashboardModelFilterOptions(rows, optionNames = null, scopeKey = \"\")" in source
    assert "dashboardModelFilterScopeKey !== scopeKey" in source
    assert "dashboardModelFilterScopeKey = scopeKey" in source
    assert 'updateDashboardModelFilterOptions(payload.rows || [], payload.modelOptions, "personal")' in source
    assert 'updateDashboardModelFilterOptions(payload.summaryRows || payload.rows || [], payload.modelOptions, "admin")' in source
    assert 'updateDashboardModelFilterOptions(payload.summaryRows || payload.rows || [], payload.modelOptions, "department")' in source
    assert 'updateDashboardModelFilterOptions(payload.summaryRows || payload.rows || [], payload.modelOptions, "team")' in source
