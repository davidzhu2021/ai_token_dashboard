"""账号清单分页必须稳定。

上游 ``/user/list`` 默认按 ``created_at desc`` 排序，批量建号时该字段大量相同，
offset 分页会同时返回重复行并漏掉账号，姓名/邮箱补齐结果随之每轮抖动。
"""

import asyncio
from typing import Any

from backend.litellm_client import LiteLLMBackend, LiteLLMClient


def make_client() -> LiteLLMClient:
    client = object.__new__(LiteLLMClient)
    primary = LiteLLMBackend(id="primary", label="Primary", base_url="https://primary.test", admin_key="k")
    client.backends = [primary]
    client._backend_map = {primary.id: primary}
    return client


def test_user_list_is_paged_by_stable_sort_key() -> None:
    client = make_client()
    calls: list[dict[str, Any]] = []

    async def fake_request_backend(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        assert path == "/user/list"
        params = dict(kwargs["params"])
        calls.append(params)
        page = params["page"]
        return {"users": [{"user_id": f"cursor-{page}"}], "total_pages": 2}

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    users = asyncio.run(client.users())

    assert [user["user_id"] for user in users] == ["cursor-1", "cursor-2"]
    assert all(call["sort_by"] == "user_id" and call["sort_order"] == "asc" for call in calls)


def test_duplicate_rows_across_pages_are_dropped() -> None:
    client = make_client()

    async def fake_request_backend(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        page = kwargs["params"]["page"]
        if page == 1:
            return {"users": [{"user_id": "cursor-a"}, {"user_id": "cursor-b"}], "total_pages": 2}
        return {"users": [{"user_id": "cursor-b"}, {"user_id": "cursor-c"}], "total_pages": 2}

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    users = asyncio.run(client.users())

    assert [user["user_id"] for user in users] == ["cursor-a", "cursor-b", "cursor-c"]


def test_sorting_falls_back_when_upstream_rejects_the_parameter() -> None:
    """老版本上游不认排序参数时不能整份清单拉空。"""

    client = make_client()
    calls: list[dict[str, Any]] = []

    async def fake_request_backend(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        params = dict(kwargs["params"])
        calls.append(params)
        if "sort_by" in params:
            raise RuntimeError("Invalid sort column")
        return {"users": [{"user_id": f"cursor-{params['page']}"}], "total_pages": 2}

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    users = asyncio.run(client.users())

    assert [user["user_id"] for user in users] == ["cursor-1", "cursor-2"]
    assert "sort_by" in calls[0]
    assert all("sort_by" not in call for call in calls[1:])
