"""长区间看板不为没人看的明细付传输代价。

每员工×每天×每模型的明细只在筛选了具体员工/部门后才渲染。未筛选时前端画的
是聚合趋势（summaryRows）与排行榜（employees），明细纯属白传：近 30 天有
14000 多行，SQL 本身只占 172ms，其余一秒多全花在传输与构造 dict 上。

这些断言锁定契约：未筛选时不发明细查询，筛选时照发。
"""

import asyncio
from typing import Any

from backend.usage_store import UsageStore


class _FakePool:
    """记录每条 SQL，并按查询形状返回空结果集。"""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        self.queries.append(" ".join(query.split()))
        return []

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.queries.append(" ".join(query.split()))
        return None

    def detail_queries(self) -> list[str]:
        # 明细查询的判别特征：按 user_id 分组且不是 employees 的 CTE 汇总。
        return [
            q
            for q in self.queries
            if "GROUP BY" in q and "user_id" in q.split("GROUP BY", 1)[1] and "WITH filtered" not in q
        ]


def _store_with(pool: _FakePool) -> UsageStore:
    store = UsageStore("postgresql://unused/unused")
    store.pool = pool

    async def covered(*_args: Any, **_kwargs: Any) -> list[str]:
        return ["primary"]

    store.covered_backend_ids = covered  # type: ignore[assignment]
    return store


def test_admin_rows_skips_detail_query_without_employee_filter() -> None:
    pool = _FakePool()
    store = _store_with(pool)

    asyncio.run(store.admin_rows("2026-07-01", "2026-07-30", "all", None, ["primary"]))

    assert pool.detail_queries() == [], "未筛选员工时仍查询了逐员工明细"


def test_admin_rows_still_queries_detail_for_a_selected_employee() -> None:
    pool = _FakePool()
    store = _store_with(pool)

    asyncio.run(store.admin_rows("2026-07-01", "2026-07-30", "all", "alice@example.com", ["primary"]))

    assert pool.detail_queries(), "筛选员工后必须返回该员工的明细"


def test_department_rows_skips_detail_query_without_department_filter() -> None:
    pool = _FakePool()
    store = _store_with(pool)

    asyncio.run(store.department_rows("2026-07-01", "2026-07-30", "all", None, ["primary"]))

    assert pool.detail_queries() == [], "未选中部门时仍查询了逐员工明细"


def test_department_rows_still_queries_detail_for_a_selected_department() -> None:
    pool = _FakePool()
    store = _store_with(pool)

    asyncio.run(store.department_rows("2026-07-01", "2026-07-30", "all", "研发部", ["primary"]))

    assert pool.detail_queries(), "选中部门后必须返回该部门的员工明细"


def test_department_rows_uses_event_team_id_without_membership_join() -> None:
    pool = _FakePool()
    store = _store_with(pool)

    asyncio.run(store.department_rows("2026-07-01", "2026-07-30", "all", "dept-a", ["primary"]))

    department_queries = [q for q in pool.queries if "usage_daily" in q]
    assert department_queries
    assert all("snapshot_date = u.usage_date AND m.user_id = u.user_id" not in q for q in department_queries)
    assert any("u.team_id <> ''" in q and "LEFT JOIN" in q for q in department_queries)


def test_total_records_falls_back_to_summary_size() -> None:
    """未查明细时统计规模退回聚合行数，避免看板把范围显示成空。"""
    pool = _FakePool()
    store = _store_with(pool)

    payload = asyncio.run(store.admin_rows("2026-07-01", "2026-07-30", "all", None, ["primary"]))

    assert payload is not None
    assert payload["rows"] == []
    # 假连接池返回空集，所以两者都是 0；关键是字段存在且不为 None。
    assert payload["totalRecords"] == len(payload["summaryRows"])
