from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_all_ranking_surfaces_use_internal_scroll_regions() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")

    assert ".model-rank-group .bars" in markup
    assert ".ranking-list-panel > .bars" in markup
    assert ".ranking-table-panel > .table-wrap" in markup
    assert ".observability-stability-grid .observability-ranking" in markup
    assert ".observability-ranking-panel > .observability-model-list" in markup
    for selector in (
        ".model-rank-group .bars",
        ".ranking-list-panel > .bars",
        ".ranking-table-panel > .table-wrap",
        ".observability-stability-grid .observability-ranking",
        ".observability-ranking-panel > .observability-model-list",
    ):
        start = markup.index(selector)
        end = markup.index("}", start)
        rule = markup[start:end]
        assert "overflow" in rule
        assert "scrollbar-gutter" in rule
        assert "overscroll-behavior" in rule


def test_ranking_panels_are_marked_without_affecting_regular_tables() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")

    for tbody_id in ("adminUserTable", "teamUserTable", "departmentUserTable", "stabilityScenarioBody"):
        tbody_at = markup.index(f'id="{tbody_id}"')
        section_at = markup.rindex("<section", 0, tbody_at)
        section_tag = markup[section_at : markup.index(">", section_at)]
        assert "ranking-table-panel" in section_tag

    assert 'id="departmentBars" class="bars"' in markup
    department_bars_at = markup.index('id="departmentBars"')
    department_section_at = markup.rindex("<section", 0, department_bars_at)
    department_section = markup[department_section_at : markup.index(">", department_section_at)]
    assert "ranking-list-panel" in department_section

    assert 'class="panel observability-ranking-panel"' in markup
    assert 'id="costModelSplit" class="observability-model-list"' in markup
    assert 'id="usageTable"' in markup


def test_ranking_tables_keep_headers_visible_and_mobile_heights_bounded() -> None:
    markup = (ROOT / "index.html").read_text(encoding="utf-8")

    assert ".ranking-table-panel > .table-wrap thead th" in markup
    assert "position: sticky" in markup
    assert "max-height: 420px" in markup
    assert ".observability-ranking-panel { height:360px; min-height:360px;" in markup
    assert ".model-rank-group {" in markup and "height: 320px" in markup
    assert ".ranking-table-panel > .table-wrap { max-height:360px; }" in markup
