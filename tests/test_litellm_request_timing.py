import asyncio
import logging

import httpx

from backend.litellm_client import LiteLLMBackend, LiteLLMClient


def test_slow_request_log_separates_queue_and_upstream_time(
    monkeypatch, caplog
) -> None:
    client = object.__new__(LiteLLMClient)
    backend = LiteLLMBackend(
        id="primary",
        label="Primary",
        base_url="https://primary.test",
        admin_key="primary-key",
    )
    client._semaphore = asyncio.Semaphore(1)

    class FakeHttpClient:
        async def request(self, method, url, **_kwargs):
            assert method == "GET"
            assert url == "https://primary.test/user/daily/activity/aggregated"
            await asyncio.sleep(0)
            return httpx.Response(
                200,
                json={"results": []},
                request=httpx.Request(method, url),
            )

    client.http_client = FakeHttpClient()
    monkeypatch.setenv("LITELLM_SLOW_REQUEST_MS", "0")

    with caplog.at_level(logging.INFO, logger="ai-token-dashboard.litellm"):
        payload = asyncio.run(
            client.request_backend(
                backend, "GET", "/user/daily/activity/aggregated"
            )
        )

    assert payload == {"results": []}
    message = next(
        record.getMessage()
        for record in caplog.records
        if "endpoint=/user/daily/activity/aggregated" in record.getMessage()
    )
    assert "queue_ms=" in message
    assert "upstream_ms=" in message
    assert "total_ms=" in message
