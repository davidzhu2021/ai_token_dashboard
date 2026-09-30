from pathlib import Path


def test_admin_all_source_scope_uses_live_totals_with_all_sources_selected() -> None:
    source = Path("assets/app.js").read_text(encoding="utf-8")
    assert "selectedDashboardSources.size === dashboardSourceOptions.length" in source
    assert "allDashboardSourcesSelected" in source
