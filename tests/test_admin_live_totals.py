from __future__ import annotations

import asyncio

from backend.litellm_client import LiteLLMBackend, LiteLLMClient


BACKEND = LiteLLMBackend(
    id="primary", label="Tongqu API", base_url="http://upstream", admin_key="key"
)


class SpendClient(LiteLLMClient):
    def __init__(self, payloads):
        self.backends = [BACKEND]
        self._backend_map = {BACKEND.id: BACKEND}
        self.payloads = payloads

    async def request_backend(self, _backend, _method, _path, *, params):
        return self.payloads[int(params["page"]) - 1]


def test_global_activity_totals_requires_explicit_spend_metadata() -> None:
    class ActivityClient(LiteLLMClient):
        def __init__(self):
            self.backends = [BACKEND]

        async def request_backend(self, *_args, **_kwargs):
            return {"daily_data": [], "metadata": {"total_tokens": 3}}

    result = asyncio.run(ActivityClient().global_activity_totals("2026-09-30", "2026-09-30"))

    assert result["complete"] is True
    assert result["spendAvailable"] is False


def test_global_spend_totals_rejects_upstream_ten_thousand_row_cap() -> None:
    client = SpendClient(
        [
            {
                "data": [{"request_id": "r-1", "total_tokens": 10, "spend": 1}],
                "total": 10000,
                "total_pages": 100,
            }
        ]
    )

    result = asyncio.run(client.global_spend_totals("2026-09-30", "2026-09-30"))

    backend = result["perBackend"]["primary"]
    assert result["complete"] is False
    assert result["missingBackends"] == ["primary"]
    assert backend["errorCode"] == "UpstreamResultLimit"
    assert backend["recordsRead"] == 0


def test_global_spend_totals_accepts_a_real_short_final_page() -> None:
    client = SpendClient(
        [
            {
                "data": [{"request_id": "r-1", "total_tokens": 10, "spend": 1}],
                "total": 2,
                "total_pages": 2,
            },
            {
                "data": [{"request_id": "r-2", "total_tokens": 20, "spend": 2}],
                "total": 2,
                "total_pages": 2,
            },
        ]
    )

    result = asyncio.run(client.global_spend_totals("2026-09-30", "2026-09-30"))

    assert result["complete"] is True
    assert result["totals"]["totalTokens"] == 30
    assert result["totals"]["spend"] == 3
