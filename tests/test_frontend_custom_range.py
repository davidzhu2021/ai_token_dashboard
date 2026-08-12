"""左上角时间筛选的自定义区间契约。

分两层：
1. 前端：下拉框有「自定义」入口、弹层元素齐全、取数与文案函数走自定义分支、静态资源版本号已更新。
2. 后端：起止日期变成用户可控入参后，格式与顺序必须在路由层就被挡住。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from backend import main


APP_JS = Path(__file__).parents[1] / "assets" / "app.js"
INDEX_HTML = Path(__file__).parents[1] / "index.html"


def test_range_select_exposes_custom_range_panel() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")

    assert 'id="customRangeOption" value="custom"' in markup
    assert 'id="customRangePanel"' in markup
    assert 'id="customRangeStart" class="input" type="date"' in markup
    assert 'id="customRangeEnd" class="input" type="date"' in markup
    assert 'id="customRangeHint"' in markup
    assert 'id="customRangeApply"' in markup
    assert 'id="customRangeCancel"' in markup
    assert 'data-range-preset="7"' in markup
    # 弹层必须绝对定位在下拉框下方，否则会把顶栏的两列筛选网格撑开。
    assert ".range-picker {" in markup
    assert ".range-panel {" in markup

    filters_start = markup.index('id="dashboardFilters"')
    filters_end = markup.index('id="sourceSelect"')
    assert markup.index('id="customRangePanel"') < filters_end
    assert filters_start < markup.index('id="rangeSelect"')


def test_custom_range_feeds_every_dashboard_through_shared_helpers() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert "const CUSTOM_RANGE_MAX_DAYS = 90;" in source
    assert "let customDateRange = null;" in source
    assert "function daysBetween(" in source
    assert "function customRangeBounds(" in source
    assert "function isCustomRangeActive(" in source
    assert "function applyCustomRange(" in source
    assert "function customRangeError(" in source

    # 所有看板共用 selectedDateRange()/rangeLabel()，自定义分支必须落在这两个入口里。
    selected_range = source[source.index("function selectedDateRange(") : source.index("function toggleTrendGrid(")]
    assert "isCustomRangeActive()" in selected_range
    assert "daysBetween(startDate, endDate)" in selected_range

    range_label = source[source.index("function rangeLabel(") : source.index("function selectedDepartmentInfo(")]
    assert "isCustomRangeActive()" in range_label
    assert "selectedDateRangeText()" in range_label


def test_custom_range_validation_and_reload_are_wired() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    apply_block = source[source.index("function customRangeError(") : source.index('el("rangeSelect").addEventListener("mousedown"')]
    assert "开始日期不能晚于结束日期。" in apply_block
    assert "结束日期不能晚于今天。" in apply_block
    assert f"查询跨度最多 ${{CUSTOM_RANGE_MAX_DAYS}} 天。" in apply_block
    assert 'setAttribute("aria-invalid", "true")' in apply_block
    assert "await reloadForFilterChange();" in apply_block

    assert 'el("customRangeApply").addEventListener("click", applyCustomRange);' in source
    assert 'el("customRangeCancel").addEventListener("click", () => closeCustomRangePanel(true));' in source
    assert "[data-range-preset]" in source
    # 切走到没有筛选区的页面时要收起弹层，避免残留在隐藏容器里。
    assert "closeCustomRangePanel();" in source


def test_static_asset_version_follows_custom_range_change() -> None:
    markup = INDEX_HTML.read_text(encoding="utf-8")

    assert "/assets/app.js?v=20260812-observability-filters" in markup
    assert "20260805-team-member-keys" not in markup


def test_resolve_usage_range_falls_back_to_default_window() -> None:
    assert main.resolve_usage_range(None, None) == main.default_date_range()
    assert main.resolve_usage_range("", "2026-08-05") == main.default_date_range()


def test_resolve_usage_range_accepts_historical_custom_windows() -> None:
    assert main.resolve_usage_range("2026-01-01", "2026-01-03") == ("2026-01-01", "2026-01-03")
    assert main.resolve_usage_range("2026-08-05", "2026-08-05") == ("2026-08-05", "2026-08-05")


@pytest.mark.parametrize(
    ("start_date", "end_date"),
    [
        ("2026-08-05", "2026-08-01"),
        ("not-a-date", "2026-08-05"),
        ("2026-08-01", "2026-13-40"),
    ],
)
def test_resolve_usage_range_rejects_unusable_windows(start_date: str, end_date: str) -> None:
    with pytest.raises(HTTPException) as excinfo:
        main.resolve_usage_range(start_date, end_date)

    assert excinfo.value.status_code == 400


def test_usage_routes_share_the_range_validator() -> None:
    source = (Path(__file__).parents[1] / "backend" / "main.py").read_text(encoding="utf-8")

    # 旧的"缺省即回填默认区间"写法全部改走校验入口，避免新增路由时漏掉一处。
    assert source.count("start_date, end_date = resolve_usage_range(start_date, end_date)") >= 19
    assert "if not start_date or not end_date:\n        start_date, end_date = default_date_range()" not in source
