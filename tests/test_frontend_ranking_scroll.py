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
    source = (ROOT / "assets" / "app.js").read_text(encoding="utf-8")

    assert ".ranking-table-panel > .table-wrap thead th" in markup
    assert "position: sticky" in markup
    assert ".ranking-list-panel {" in markup
    assert "height: 420px" in markup
    assert ".observability-ranking-panel { height:360px; min-height:360px;" in markup
    assert ".metrics {" in markup and "align-items: stretch;" in markup
    model_group_start = markup.index(".model-rank-group {")
    model_group_rule = markup[model_group_start : markup.index("}", model_group_start)]
    assert "min-height: 0" in model_group_rule
    assert "height: 100%" in model_group_rule
    assert "grid-auto-rows: 404px" in markup
    assert ".bars.is-compact" in markup
    assert "div.observability-ranking.is-compact" in markup
    assert "div.observability-model-list.is-compact" in markup
    assert markup.index("div.observability-ranking.is-compact") > markup.index(".observability-stability-grid .observability-ranking {")
    assert markup.index("div.observability-model-list.is-compact") > markup.index(".observability-ranking-panel > .observability-model-list {")
    assert 'container.classList.toggle("is-compact", rows.length > 0 && rows.length <= 4);' in source
    assert 'function rankingBadge(index)' in source
    assert '.map((row, index) => `<div class="bar-row">${rankingBadge(index)}' in source
    assert 'function renderBarsSkeleton(containerId, showRank = false)' in source
    assert 'renderBarsSkeleton("adminModelBars", true)' in source
    assert "grid-template-columns: 24px minmax(72px, 0.82fr)" in markup
    assert ".model-rank-group > div.bars.is-compact" in markup
    assert "justify-content: center" in markup
    assert ".model-rank-group-team .bars {" in markup
    team_bars_start = markup.index(".model-rank-group-team .bars {")
    team_bars_rule = markup[team_bars_start : markup.index("}", team_bars_start)]
    assert "width: min(100%, 390px)" in team_bars_rule
    assert "align-self: center" in team_bars_rule
    assert "align-content: safe center" in team_bars_rule
    assert 'container.classList.toggle("is-compact", sorted.length > 0 && sorted.length <= 4);' in source
    assert 'const stabilityRanking = el("stabilityRanking");' in source
    assert 'stabilityRanking.classList.toggle("is-compact", stabilityRankings.length > 0 && stabilityRankings.length <= 4);' in source
    assert 'el("costModelSplit").classList.toggle("is-compact", split.length > 0 && split.length <= 4);' in source
    assert '.filter((row) => row.value > 0)' in source
    model_renderer_start = source.index("function renderModelBarsTo")
    model_renderer_end = source.index("function renderDepartmentBarsTo", model_renderer_start)
    model_renderer = source[model_renderer_start:model_renderer_end]
    department_renderer_end = source.index("function uniqueSorted", model_renderer_end)
    department_renderer = source[model_renderer_end:department_renderer_end]
    assert ".slice(0, 5)" not in model_renderer
    assert ".slice(0, 10)" not in department_renderer
    assert "前 10 个部门" not in markup
    assert ".ranking-list-panel { height:360px; min-height:360px; }" in markup
    assert ".ranking-table-panel > .table-wrap { max-height:360px; }" in markup
