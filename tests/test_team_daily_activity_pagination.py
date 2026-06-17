import asyncio

from backend.litellm_client import LiteLLMBackend, LiteLLMClient


def _client_for_test() -> tuple[LiteLLMClient, LiteLLMBackend]:
    client = object.__new__(LiteLLMClient)
    backend = LiteLLMBackend(
        id="primary",
        label="Primary",
        base_url="https://example.test",
        admin_key="test-key",
    )
    client.backends = [backend]
    client._backend_map = {"primary": backend}
    return client, backend


def _entity_item(day: str, prompt: int, completion: int, total: int, requests: int, spend: float) -> dict:
    return {
        "date": day,
        "breakdown": {
            "entities": {
                "team-a": {
                    "metrics": {
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                        "total_tokens": total,
                        "api_requests": requests,
                        "successful_requests": requests,
                        "failed_requests": 0,
                        "spend": spend,
                    }
                }
            }
        },
    }


def test_team_daily_activity_rows_reads_all_pages_from_metadata_total_pages(monkeypatch) -> None:
    client, backend = _client_for_test()
    captured_pages: list[int] = []
    captured_team_ids: list[str | None] = []

    payloads = {
        1: {"results": [_entity_item("2026-06-17", 10, 20, 30, 2, 1.2)], "metadata": {"total_pages": 2}},
        2: {"results": [_entity_item("2026-06-16", 11, 21, 32, 3, 1.5)], "metadata": {"total_pages": 2}},
    }

    async def fake_request_backend(backend_arg, method, path, **kwargs):
        assert backend_arg == backend
        assert method == "GET"
        assert path == "/team/daily/activity"
        params = kwargs.get("params", {})
        page = int(params.get("page", 1))
        captured_pages.append(page)
        captured_team_ids.append(params.get("team_ids"))
        return payloads.get(page, {"results": []})

    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    rows = asyncio.run(
        client._team_daily_activity_rows(
            "2026-06-11",
            "2026-06-17",
            "team-a",
            {"team-a": {"id": "team-a", "name": "Team A"}},
            backend,
        )
    )

    assert captured_pages == [1, 2]
    assert captured_team_ids == ["team-a", "team-a"]
    assert [row["date"] for row in rows] == ["2026-06-17", "2026-06-16"]
    assert rows[0]["departmentName"] == "Team A"
    assert rows[1]["totalTokens"] == 32


def test_team_daily_activity_rows_reads_until_metadata_has_more_false(monkeypatch) -> None:
    client, backend = _client_for_test()
    captured_pages: list[int] = []

    payloads = {
        1: {"results": [_entity_item("2026-06-17", 8, 9, 17, 2, 0.5)], "metadata": {"has_more": True}},
        2: {"results": [_entity_item("2026-06-16", 5, 6, 11, 1, 0.3)], "metadata": {"has_more": False}},
    }

    async def fake_request_backend(_backend_arg, _method, _path, **kwargs):
        page = int(kwargs.get("params", {}).get("page", 1))
        captured_pages.append(page)
        return payloads.get(page, {"results": []})

    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    rows = asyncio.run(
        client._team_daily_activity_rows(
            "2026-06-11",
            "2026-06-17",
            None,
            {"team-a": {"id": "team-a", "name": "Team A"}},
            backend,
        )
    )

    assert captured_pages == [1, 2]
    assert len(rows) == 2
    assert rows[-1]["date"] == "2026-06-16"


def test_team_daily_activity_rows_stops_on_empty_page(monkeypatch) -> None:
    client, backend = _client_for_test()
    captured_pages: list[int] = []

    async def fake_request_backend(_backend_arg, _method, _path, **kwargs):
        page = int(kwargs.get("params", {}).get("page", 1))
        captured_pages.append(page)
        return {"results": []}

    monkeypatch.setattr(client, "request_backend", fake_request_backend)

    rows = asyncio.run(
        client._team_daily_activity_rows(
            "2026-06-11",
            "2026-06-17",
            "team-a",
            {"team-a": {"id": "team-a", "name": "Team A"}},
            backend,
        )
    )

    assert captured_pages == [1]
    assert rows == []
