"""排行表列排序的前端结构与行为约定测试。"""

import json
import re
import subprocess
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from backend import main

APP_JS = Path(__file__).parents[1] / "assets" / "app.js"
INDEX_HTML = Path(__file__).parents[1] / "index.html"

RANKING_TBODY_IDS = ("adminUserTable", "teamUserTable", "departmentUserTable")
SORT_KEYS = ("requestCount", "totalTokens", "spend", "successRate")


def _ranking_thead(markup: str, tbody_id: str) -> str:
    """取出排行表 tbody 之前最近的一段 thead。"""
    tbody_index = markup.index(f'<tbody id="{tbody_id}">')
    head_start = markup.rindex("<thead>", 0, tbody_index)
    head_end = markup.index("</thead>", head_start)
    return markup[head_start:head_end]


def test_ranking_tables_expose_sortable_numeric_headers() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")

    for tbody_id in RANKING_TBODY_IDS:
        head = _ranking_thead(markup, tbody_id)
        for key in SORT_KEYS:
            assert f'data-sort-key="{key}"' in head, f"{tbody_id} 缺少 {key} 排序表头"
        # 可排序表头必须带初始 aria-sort 与可聚焦按钮，保证键盘和读屏可用。
        assert head.count('class="num sortable"') == len(SORT_KEYS)
        assert head.count('aria-sort="none"') == len(SORT_KEYS)
        assert head.count('<button class="th-sort" type="button"') == len(SORT_KEYS)
        assert '<th class="rank-head">排名</th>' in head


def test_ranking_rows_render_dynamic_badges_after_sorting() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert 'function rankingBadge(index)' in source
    assert '<td class="rank-cell">${rankingBadge(index)}</td>' in source
    assert source.count('.map((item, index) => {') >= 2


def test_non_ranking_tables_stay_unsorted() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")

    # 个人用量明细和成员用量明细是按日期的流水，不应引入列排序。
    for tbody_id in ("usageTable", "teamMemberUsageTable"):
        head = _ranking_thead(markup, tbody_id)
        assert "data-sort-key" not in head
        assert "sortable" not in head


def test_ranking_sort_indicators_and_listeners_are_wired() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert 'setupRankingSorting("adminUserTable", renderAdminUsers);' in source
    assert 'setupRankingSorting("departmentUserTable", renderDepartmentUsers);' in source
    assert 'setupRankingSorting("teamUserTable", renderTeamUsers);' in source
    # 两处渲染 + 初始化时都要刷新箭头，否则重新加载数据后表头指示会与实际排序不一致。
    assert source.count("updateRankingSortIndicators(tableId);") == 3
    assert 'th.setAttribute("aria-sort", active ? (direction === "asc" ? "ascending" : "descending") : "none");' in source


def test_sortable_headers_have_styles() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")

    assert "th.sortable .th-sort {" in markup
    assert 'th.sortable[aria-sort="ascending"] .sort-arrow,' in markup


def test_sort_hint_mentions_switchable_columns() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    markup = INDEX_HTML.read_text(encoding="utf-8")

    assert "点击请求数 / Token / 金额 / 成功率表头可切换升降序" in source
    assert "点击表头可切换升降序" in markup
    # 旧的"只能按 Token 降序"文案不应残留。
    assert "默认按 Token 从高到低排序；" not in source


def test_index_serves_sortable_ranking_markup() -> None:
    response = TestClient(main.app).get("/")

    assert response.status_code == 200
    assert 'data-sort-key="successRate"' in response.text


def _run_sort_script(body: str) -> object:
    source = APP_JS.read_text(encoding="utf-8")
    blocks = []
    for name in (
        "rankingSort",
        "rankingSortValue",
        "rankingComparator",
        "employeeRankingName",
        "departmentRankingName",
        "sortedRankingRows",
    ):
        match = re.search(r"function " + name + r"\([\s\S]*?\n\}", source)
        assert match, f"未找到 {name}"
        blocks.append(match.group(0))
    script = f"""
const DEFAULT_RANKING_SORT = {{ key: "totalTokens", direction: "desc" }};
const rankingSortState = new Map();
{chr(10).join(blocks)}
{body}
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(script)
        script_path = handle.name
    try:
        result = subprocess.run(
            ["node", script_path], capture_output=True, text=True, encoding="utf-8", check=True
        )
    finally:
        Path(script_path).unlink(missing_ok=True)
    return json.loads(result.stdout)


ROWS_JS = """
const rows = [
  { employeeName: "A", requestCount: 10, totalTokens: 300, spend: 1, successCount: 5 },
  { employeeName: "B", requestCount: 50, totalTokens: 200, spend: 3, successCount: 50 },
  { employeeName: "C", requestCount: 30, totalTokens: 100, spend: 2, successCount: 15 },
];
"""


def test_ranking_defaults_to_token_descending() -> None:
    names = _run_sort_script(
        ROWS_JS
        + """
console.log(JSON.stringify(sortedRankingRows("adminUserTable", rows, employeeRankingName).map((r) => r.employeeName)));
"""
    )
    assert names == ["A", "B", "C"]


def test_ranking_sorts_each_numeric_column_both_directions() -> None:
    expected = {
        "requestCount": (["A", "C", "B"], ["B", "C", "A"]),
        "totalTokens": (["C", "B", "A"], ["A", "B", "C"]),
        "spend": (["A", "C", "B"], ["B", "C", "A"]),
        # 成功率：A 50%、B 100%、C 50%，同值时回落到 Token 降序（A 300 > C 100）。
        "successRate": (["A", "C", "B"], ["B", "A", "C"]),
    }
    for key, (asc, desc) in expected.items():
        result = _run_sort_script(
            ROWS_JS
            + f"""
rankingSortState.set("t", {{ key: "{key}", direction: "asc" }});
const asc = sortedRankingRows("t", rows, employeeRankingName).map((r) => r.employeeName);
rankingSortState.set("t", {{ key: "{key}", direction: "desc" }});
const desc = sortedRankingRows("t", rows, employeeRankingName).map((r) => r.employeeName);
console.log(JSON.stringify([asc, desc]));
"""
        )
        assert result == [asc, desc], f"{key} 排序结果不符"


def test_success_rate_handles_zero_request_rows() -> None:
    result = _run_sort_script(
        """
const rows = [
  { employeeName: "zero", requestCount: 0, successCount: 0, totalTokens: 0, spend: 0 },
  { employeeName: "half", requestCount: 4, successCount: 2, totalTokens: 10, spend: 0 },
];
rankingSortState.set("t", { key: "successRate", direction: "desc" });
console.log(JSON.stringify(sortedRankingRows("t", rows, employeeRankingName).map((r) => r.employeeName)));
"""
    )
    # 零请求成员成功率按 0 处理，不能出现 NaN 把排序打乱。
    assert result == ["half", "zero"]


def test_sorting_does_not_mutate_source_array() -> None:
    result = _run_sort_script(
        ROWS_JS
        + """
rankingSortState.set("t", { key: "requestCount", direction: "asc" });
sortedRankingRows("t", rows, employeeRankingName);
console.log(JSON.stringify(rows.map((r) => r.employeeName)));
"""
    )
    assert result == ["A", "B", "C"]


def test_department_rows_sort_by_same_keys() -> None:
    result = _run_sort_script(
        """
const rows = [
  { departmentName: "研发", requestCount: 5, totalTokens: 50, spend: 9, successCount: 5 },
  { departmentName: "销售", requestCount: 9, totalTokens: 10, spend: 1, successCount: 3 },
];
rankingSortState.set("t", { key: "spend", direction: "asc" });
console.log(JSON.stringify(sortedRankingRows("t", rows, departmentRankingName).map((r) => r.departmentName)));
"""
    )
    assert result == ["销售", "研发"]
