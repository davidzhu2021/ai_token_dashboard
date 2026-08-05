"""上游账号姓名化之后的匹配规则回归。

上游把员工账号重写了一遍，姓名普遍补齐但多数账号仍然没有邮箱，匹配主力从邮箱
变成了姓名。旧规则按"一个姓名只能对应一个账号"判唯一，遇到同一人持有多个账号、
或本人姓名出现在别人的共享账号使用人清单里，都会误判成重名而放弃匹配。这里覆盖
改成按"人"归并之后的行为。
"""

import asyncio
from typing import Any

import pytest
from fastapi import HTTPException

from backend.litellm_client import LiteLLMBackend, LiteLLMClient


def make_client() -> LiteLLMClient:
    client = object.__new__(LiteLLMClient)
    her = LiteLLMBackend(id="her", label="Her", base_url="https://her.test", admin_key="her-key", source="Her")
    client.backends = [her]
    client._backend_map = {item.id: item for item in client.backends}
    from backend.litellm_client import TTLCache

    client._account_index_cache = TTLCache()
    return client


def account(user_id: str, *, alias: str = "", email: str = "", open_id: str = "", used_by: list[str] | None = None) -> dict[str, Any]:
    metadata: dict[str, Any] = {"source": "feishu_carher_account_base"}
    if open_id:
        metadata["lark_open_id"] = open_id
    if alias:
        metadata["display_name"] = alias
    if used_by:
        metadata["used_by"] = [{"name": name} for name in used_by]
    if not email:
        metadata["email_source"] = "missing"
    return {
        "user_id": user_id,
        "user_alias": alias,
        "user_email": email,
        "metadata": metadata,
    }


def build_index(client: LiteLLMClient, accounts: list[dict[str, Any]]) -> dict[str, Any]:
    async def fake_request_backend(backend: LiteLLMBackend, method: str, path: str, **kwargs: Any) -> Any:
        if path == "/user/list":
            page = int((kwargs.get("params") or {}).get("page") or 1)
            return {"users": accounts if page == 1 else [], "total_pages": 1}
        if path == "/key/list":
            return {"keys": [], "total_pages": 1}
        raise AssertionError(f"unexpected call {method} {path}")

    client.request_backend = fake_request_backend  # type: ignore[assignment]
    return asyncio.run(client.her_account_index(client.backends[0]))


def matched_user_ids(client: LiteLLMClient, index: dict[str, Any], email: str, name: str) -> list[str]:
    collected: list[tuple[str, str]] = []

    def add_user_id(backend: LiteLLMBackend, user_id: Any, source: str) -> None:
        collected.append((str(user_id), source))

    async def fake_index(backend: LiteLLMBackend) -> dict[str, Any]:
        return index

    client.her_account_index = fake_index  # type: ignore[assignment]
    asyncio.run(client.add_her_index_matches(client.backends[0], email, name, add_user_id))
    return [user_id for user_id, _ in collected]


def matched_sources(client: LiteLLMClient, index: dict[str, Any], email: str, name: str) -> list[str]:
    collected: list[str] = []

    def add_user_id(backend: LiteLLMBackend, user_id: Any, source: str) -> None:
        collected.append(source)

    async def fake_index(backend: LiteLLMBackend) -> dict[str, Any]:
        return index

    client.her_account_index = fake_index  # type: ignore[assignment]
    asyncio.run(client.add_her_index_matches(client.backends[0], email, name, add_user_id))
    return collected


def test_one_person_with_multiple_accounts_matches_all_of_them() -> None:
    client = make_client()
    index = build_index(
        client,
        [
            account("carher-11", alias="刘国现", open_id="ou-liu"),
            account("carher-12", alias="刘国现", open_id="ou-liu"),
            account("carher-13", alias="刘国现", open_id="ou-liu"),
        ],
    )

    assert sorted(matched_user_ids(client, index, "liuguoxian@auto-link.com.cn", "刘国现")) == [
        "carher-11",
        "carher-12",
        "carher-13",
    ]


def test_owner_match_wins_over_shared_account_usage_list() -> None:
    """本人姓名同时出现在别人的共享账号里时，仍然只归属到本人账号。"""

    client = make_client()
    index = build_index(
        client,
        [
            account("carher-20", alias="杨晖", open_id="ou-yang"),
            account("carher-21", alias="白羽", open_id="ou-bai", used_by=["白羽", "杨晖"]),
            account("carher-22", alias="赵凌云", open_id="ou-zhao", used_by=["赵凌云", "杨晖"]),
        ],
    )

    assert matched_user_ids(client, index, "yanghui@auto-link.com.cn", "杨晖") == ["carher-20"]


def test_shared_account_fallback_still_covers_people_without_own_account() -> None:
    """没有独立账号的共享使用人仍要能看到所在账号的用量。"""

    client = make_client()
    index = build_index(
        client,
        [account("carher-30", alias="白羽", open_id="ou-bai", used_by=["白羽", "李小明"])],
    )

    assert matched_user_ids(client, index, "lixiaoming@auto-link.com.cn", "李小明") == ["carher-30"]
    assert matched_sources(client, index, "lixiaoming@auto-link.com.cn", "李小明") == [
        "her_shared_account_name"
    ]


def test_same_name_on_two_different_people_is_not_guessed() -> None:
    client = make_client()
    index = build_index(
        client,
        [
            account("carher-40", alias="张伟", open_id="ou-zhang-a"),
            account("carher-41", alias="张伟", open_id="ou-zhang-b"),
        ],
    )

    assert matched_user_ids(client, index, "zhangwei@auto-link.com.cn", "张伟") == []


def test_non_chinese_name_can_match_when_it_maps_to_one_person() -> None:
    client = make_client()
    index = build_index(client, [account("carher-50", alias="TH Goh", open_id="ou-goh")])

    assert matched_user_ids(client, index, "thgoh@auto-link.com.cn", "TH Goh") == ["carher-50"]


def test_generic_account_names_never_match() -> None:
    client = make_client()
    index = build_index(client, [account("carher-60", alias="admin", open_id="ou-admin")])

    assert matched_user_ids(client, index, "someone@auto-link.com.cn", "admin") == []


def test_upstream_email_still_takes_priority_over_name() -> None:
    client = make_client()
    index = build_index(
        client,
        [
            account("carher-70", alias="孙倩", email="sunqian@auto-link.com.cn", open_id="ou-sun"),
            account("carher-71", alias="孙倩", open_id="ou-sun"),
        ],
    )

    assert matched_sources(client, index, "sunqian@auto-link.com.cn", "孙倩") == ["her_user_email"]


def test_index_without_owner_bucket_does_not_crash() -> None:
    """外部构造的精简索引（只有 emails/names/profiles）必须继续可用。"""

    client = make_client()
    index = {"emails": {}, "names": {}, "profiles": {}}

    assert matched_user_ids(client, index, "nobody@auto-link.com.cn", "无人") == []


def test_employee_info_from_log_uses_owner_profile() -> None:
    client = make_client()
    index = build_index(
        client,
        [
            account("carher-80", alias="陈可", email="chenke@auto-link.com.cn", open_id="ou-chen"),
            account("carher-81", alias="陈可", open_id="ou-chen"),
        ],
    )

    info = client._employee_info_from_log(
        {"user": "carher-81", "user_alias": "陈可"},
        {},
        client.backends[0],
        index,
    )

    assert info["name"] == "陈可"
    assert info["email"] == "chenke@auto-link.com.cn"


def test_resolve_user_reports_missing_account_when_nothing_matches() -> None:
    client = make_client()

    async def fake_targeted(backend: LiteLLMBackend, email: str, aliases: set[str]) -> list[dict[str, Any]]:
        return []

    async def fake_index(backend: LiteLLMBackend) -> dict[str, Any]:
        return {"emails": {}, "names": {}, "owners": {}, "identities": {}, "profiles": {}}

    client._targeted_identity_users = fake_targeted  # type: ignore[assignment]
    client.her_account_index = fake_index  # type: ignore[assignment]

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(client.resolve_user("ghost@auto-link.com.cn", "幽灵"))
    assert excinfo.value.status_code == 404
