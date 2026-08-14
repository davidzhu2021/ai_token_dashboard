"""全员看板下钻到具体成员时必须能看出他来自哪个部门。

成员排行本身没有部门列（列已经很挤），部门归属只在下钻后的详情卡里显示。
因此三条数据路径都只在筛选了具体成员时才取部门，未筛选时不为没人看的字段
多发查询或多打一次上游团队列表。
"""

import asyncio
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from backend.litellm_client import LiteLLMBackend, LiteLLMClient
from backend.organization_store import InMemoryOrganizationStore
from backend.usage_store import UsageStore, _department_names_for


APP_JS = Path(__file__).parents[1] / "assets" / "app.js"
INDEX_HTML = Path(__file__).parents[1] / "index.html"


# ----------------------------------------------------------------------
# 本地 SQL 路径（生产主路径）
# ----------------------------------------------------------------------


class _FakePool:
    """按查询形状返回结果，并记录每条 SQL 以便断言是否发出。"""

    def __init__(self, membership_records: list[dict[str, Any]] | None = None) -> None:
        self.queries: list[str] = []
        self.membership_records = membership_records or []

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        flattened = " ".join(query.split())
        self.queries.append(flattened)
        if "FROM usage_team_membership_daily" in flattened and "SELECT DISTINCT user_id" in flattened:
            return list(self.membership_records)
        if "WITH filtered" in flattened:
            return [
                {
                    "employee_key": "alice@example.com",
                    "employee_id": "alice-primary",
                    "employee_email": "alice@example.com",
                    "employee_name": "Alice",
                    "user_ids": ["alice-her", "alice-primary"],
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "request_count": 2,
                    "success_count": 2,
                    "failure_count": 0,
                    "spend": 0.3,
                    "primary_source": "Codex",
                }
            ]
        return []

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.queries.append(" ".join(query.split()))
        return None

    def membership_queries(self) -> list[str]:
        return [q for q in self.queries if "SELECT DISTINCT user_id" in q and "usage_team_membership_daily" in q]


def _store_with(pool: _FakePool) -> UsageStore:
    store = UsageStore("postgresql://unused/unused")
    store.pool = pool

    async def covered(*_args: Any, **_kwargs: Any) -> list[str]:
        return ["primary", "her"]

    store.covered_backend_ids = covered  # type: ignore[assignment]
    return store


def test_admin_rows_returns_department_names_for_a_selected_employee() -> None:
    pool = _FakePool(
        [
            {"user_id": "alice-primary", "employee_email": "alice@example.com", "team_id": "team-a", "team_name": "研发部"},
            {"user_id": "alice-her", "employee_email": "alice@example.com", "team_id": "team-b", "team_name": "算法部"},
        ]
    )
    store = _store_with(pool)

    payload = asyncio.run(store.admin_rows("2026-07-01", "2026-07-30", "all", "alice@example.com", ["primary", "her"]))

    assert payload is not None
    assert payload["employees"][0]["departmentNames"] == ["研发部", "算法部"]


def test_admin_rows_still_resolves_departments_without_an_employee_filter() -> None:
    """排行榜每一行都有部门列，所以未筛选时也必须查部门（与逐员工明细不同）。"""

    pool = _FakePool(
        [{"user_id": "alice-primary", "employee_email": "alice@example.com", "team_id": "team-a", "team_name": "研发部"}]
    )
    store = _store_with(pool)

    payload = asyncio.run(store.admin_rows("2026-07-01", "2026-07-30", "all", None, ["primary", "her"]))

    assert payload is not None
    assert len(pool.membership_queries()) == 1, "全员排行需要且只需要一次成员-部门查询"
    assert payload["employees"][0]["departmentNames"] == ["研发部"]


def test_admin_rows_keeps_skipping_the_per_employee_detail_without_a_filter() -> None:
    """加部门列不能顺手把"未筛选时不查明细"那条优化弄没了。"""

    pool = _FakePool()
    store = _store_with(pool)

    asyncio.run(store.admin_rows("2026-07-01", "2026-07-30", "all", None, ["primary", "her"]))

    detail = [
        q
        for q in pool.queries
        if "GROUP BY" in q and "user_id" in q.split("GROUP BY", 1)[1] and "WITH filtered" not in q
    ]
    assert detail == [], "未筛选成员时仍查询了逐员工明细"


def test_admin_rows_reports_no_department_when_the_member_has_no_team() -> None:
    pool = _FakePool([])
    store = _store_with(pool)

    payload = asyncio.run(store.admin_rows("2026-07-01", "2026-07-30", "all", "alice@example.com", ["primary", "her"]))

    assert payload is not None
    assert payload["employees"][0]["departmentNames"] == []


def test_department_names_prefer_email_then_fall_back_to_user_ids() -> None:
    index = {"alice@example.com": ["研发部"], "bob-primary": ["市场部"]}

    assert _department_names_for(index, "alice@example.com", ["alice-primary"]) == ["研发部"]
    assert _department_names_for(index, "", ["bob-primary"]) == ["市场部"]
    assert _department_names_for(index, "nobody@example.com", ["ghost"]) == []


# ----------------------------------------------------------------------
# 上游 LiteLLM 回退路径
# ----------------------------------------------------------------------


def _upstream_client() -> LiteLLMClient:
    client = object.__new__(LiteLLMClient)
    backend = LiteLLMBackend(id="primary", label="Primary", base_url="https://primary.test", admin_key="primary-key")
    client.backends = [backend]
    client._backend_map = {backend.id: backend}
    return client


def _install_upstream_fakes(client: LiteLLMClient, team_map_calls: list[str]) -> None:
    async def fake_users(backend: LiteLLMBackend | None = None) -> list[dict[str, Any]]:
        return [{"user_id": "alice-primary", "user_email": "alice@example.com", "user_alias": "Alice"}]

    async def fake_team_map(backend: LiteLLMBackend | None = None) -> dict[str, dict[str, str]]:
        team_map_calls.append(backend.id if backend else "default")
        return {"team-a": {"id": "team-a", "name": "研发部"}}

    async def fake_request_backend(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        if path == "/spend/logs/v2":
            page = (kwargs.get("params") or {}).get("page")
            if page and page > 1:
                return {"logs": [], "total_pages": 1, "total": 1}
            return {
                "logs": [
                    {
                        "user": "alice-primary",
                        "team_id": "team-a",
                        "startTime": "2026-07-15T10:00:00Z",
                        "model": "gpt-4o",
                        "prompt_tokens": 120,
                        "completion_tokens": 30,
                        "total_tokens": 150,
                        "spend": 0.12,
                        "status": "success",
                    }
                ],
                "total_pages": 1,
                "total": 1,
            }
        if path == "/user/daily/activity/aggregated":
            raise HTTPException(status_code=404, detail="no summary")
        raise AssertionError(f"unexpected call {backend.id} {method} {path}")

    client.users = fake_users  # type: ignore[assignment]
    client.team_map = fake_team_map  # type: ignore[assignment]
    client.request_backend = fake_request_backend  # type: ignore[assignment]


def test_upstream_admin_usage_rows_attach_department_names_when_drilling_down() -> None:
    client = _upstream_client()
    calls: list[str] = []
    _install_upstream_fakes(client, calls)

    payload = asyncio.run(client.admin_usage_rows("2026-07-01", "2026-07-30", "all", "alice@example.com"))

    assert payload["employees"][0]["departmentNames"] == ["研发部"]
    assert calls == ["primary"]


def test_upstream_admin_usage_rows_resolve_departments_without_a_filter() -> None:
    """全员排行也要部门列，且每个后端只取一次团队列表。"""

    client = _upstream_client()
    calls: list[str] = []
    _install_upstream_fakes(client, calls)

    payload = asyncio.run(client.admin_usage_rows("2026-07-01", "2026-07-30", "all", None))

    assert calls == ["primary"]
    assert payload["employees"][0]["departmentNames"] == ["研发部"]


def test_upstream_department_names_survive_a_failing_team_list() -> None:
    """团队列表拿不到时详情卡退回"未绑定部门"，不能让整个看板 500。"""

    client = _upstream_client()
    calls: list[str] = []
    _install_upstream_fakes(client, calls)

    async def failing_team_map(backend: LiteLLMBackend | None = None) -> dict[str, dict[str, str]]:
        raise HTTPException(status_code=500, detail="upstream down")

    client.team_map = failing_team_map  # type: ignore[assignment]

    payload = asyncio.run(client.admin_usage_rows("2026-07-01", "2026-07-30", "all", "alice@example.com"))

    # team_map 失败时仍能从日志的 team_id 推出部门标识。
    assert payload["employees"][0]["departmentNames"] == ["team-a"]


# ----------------------------------------------------------------------
# Mock 客户企业路径
# ----------------------------------------------------------------------


def test_mock_organization_usage_exposes_each_member_department() -> None:
    store = InMemoryOrganizationStore()
    payload = store.usage_payload("org-demo", "2026-07-01", "2026-07-03")

    assert payload["employees"], "Mock 企业应有成员用量"
    for employee in payload["employees"]:
        assert employee["departmentNames"], f"{employee['employeeName']} 缺少部门归属"


# ----------------------------------------------------------------------
# 前端契约
# ----------------------------------------------------------------------


def test_admin_detail_card_renders_the_member_department() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert "function employeeDepartmentText(employee)" in source
    assert '`${identity} · 部门：${employeeDepartmentText(employee)}`' in source
    assert 'names.length ? names.join("、") : "未绑定部门"' in source


def _column_count(thead_markup: str) -> int:
    """数 <th> 单元格，注意别把 <thead> 本身算进去。"""

    return thead_markup.count("<th>") + thead_markup.count('<th class=')


def test_only_the_admin_ranking_gets_a_department_column() -> None:
    """部门看板整表同属一个部门，团队看板那一列留给团队角色，都不加部门列。"""

    source = APP_JS.read_text(encoding="utf-8")

    assert 'const showDepartment = tableId === "adminUserTable"' in source
    assert '${showDepartment ? `<td>${escapeHtml(employeeDepartmentText(item))}</td>` : ""}' in source
    # 空表提示要跨满新的列数，否则会短一格。
    assert 'colspan="${isTeamTable || showDepartment ? 10 : 9}"' in source


def test_admin_ranking_header_and_skeleton_match_the_new_column_count() -> None:
    """表头、渲染、骨架屏三处列数必须一致，否则加载态会错位。"""

    markup = INDEX_HTML.read_text(encoding="utf-8")
    source = APP_JS.read_text(encoding="utf-8")

    header_start = markup.index('<tbody id="adminUserTable">')
    header = markup[markup.rindex("<thead>", 0, header_start) : header_start]
    assert _column_count(header) == 10, "全员排行表头列数应为 10"
    assert '<th class="rank-head">排名</th>' in header
    assert header.index("<th>部门</th>") > header.index("<th>邮箱</th>"), "部门列应紧随邮箱列"

    assert 'renderTableSkeleton("adminUserTable", "adminUserCount", 10)' in source

    # 另两张表没加列，表头保持原样。
    for table_id, expected in (("departmentUserTable", 9), ("teamUserTable", 10)):
        start = markup.index(f'<tbody id="{table_id}">')
        block = markup[markup.rindex("<thead>", 0, start) : start]
        assert _column_count(block) == expected, f"{table_id} 表头列数被意外改动"
        assert "<th>部门</th>" not in block, f"{table_id} 不应有部门列"


def test_department_column_escapes_upstream_department_names() -> None:
    """部门名来自上游团队别名，渲染进 innerHTML 前必须转义。"""

    source = APP_JS.read_text(encoding="utf-8")
    row_start = source.index("function renderEmployeeRanking(")
    row_block = source[row_start : source.index("function renderDepartmentRanking(", row_start)]

    assert "escapeHtml(employeeDepartmentText(item))" in row_block
