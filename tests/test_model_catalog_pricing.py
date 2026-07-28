"""模型广场目录的厂商归类、内部别名过滤与计费信息聚合。"""

import asyncio
from datetime import date
from typing import Any

import pytest
from fastapi import HTTPException

from backend.cache import TTLCache
from backend.litellm_client import (
    LiteLLMBackend,
    LiteLLMClient,
    is_internal_model_alias,
    model_family,
)


class _MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def get(self, key: str) -> tuple[bool, Any, int]:
        if key in self.values:
            return True, self.values[key], 300
        return False, None, 0

    def set(self, key: str, value: Any, _ttl: int) -> None:
        self.values[key] = value


def make_client() -> LiteLLMClient:
    client = object.__new__(LiteLLMClient)
    client.backends = [
        LiteLLMBackend(id="primary", label="Primary", base_url="https://primary.test", admin_key="primary-key"),
        LiteLLMBackend(id="secondary", label="Secondary", base_url="https://secondary.test", admin_key="secondary-key", source="Secondary"),
    ]
    client._backend_map = {backend.id: backend for backend in client.backends}
    client._model_cache = _MemoryCache()
    client._model_usage_cache = TTLCache()
    return client


def _info(model_name: str, **model_info: Any) -> dict[str, Any]:
    return {"model_name": model_name, "model_info": model_info}


def _stub(
    client: LiteLLMClient,
    *,
    models: dict[str, list[dict[str, Any]]],
    infos: dict[str, list[dict[str, Any]]],
) -> None:
    async def fake_request_backend(backend: LiteLLMBackend, _method: str, path: str, **_kwargs: Any) -> Any:
        if path == "/models":
            return {"data": models.get(backend.id, [])}
        if path == "/model/info":
            return {"data": infos.get(backend.id, [])}
        if path == "/user/daily/activity/aggregated":
            return {"results": []}
        raise AssertionError(f"unexpected path: {path}")

    client.request_backend = fake_request_backend  # type: ignore[assignment]


def _catalog(client: LiteLLMClient) -> dict[str, dict[str, Any]]:
    return {item["modelName"]: item for item in asyncio.run(client.models({}))}


@pytest.mark.parametrize(
    ("model_name", "expected_key", "expected_label"),
    [
        ("gpt-5.5", "openai", "OpenAI"),
        ("gpt-5.3-codex", "openai", "OpenAI"),
        ("image-2", "openai", "OpenAI"),
        ("claude-opus-4-8", "anthropic", "Anthropic"),
        ("gemini-3.5-flash", "google", "Google"),
        ("deepseek-v4-pro", "deepseek", "DeepSeek"),
        ("minimax-m3", "minimax", "MiniMax"),
        ("bge-m3", "baai", "BAAI"),
        # 内部把第三方模型挂在 claude-*/gpt-* 兼容别名下，归类必须看真实模型词根。
        ("claude-code-glm-5.1", "zhipu", "智谱 GLM"),
        ("claude-kimi-k2.7-code", "moonshot", "月之暗面"),
        ("claude-deepseek-v4-pro", "deepseek", "DeepSeek"),
        ("wangsu-qwen3.7-plus", "qwen", "通义千问"),
        ("mystery-model", "other", "其他"),
    ],
)
def test_model_family_classifies_by_real_model_root(model_name: str, expected_key: str, expected_label: str) -> None:
    assert model_family(model_name) == (expected_key, expected_label)


@pytest.mark.parametrize(
    "model_name",
    [
        "wangsu-gpt-5.5",
        "wangsu7-gpt-5.4",
        "zerokey-codex-sol",
        "zerokey-pool-gpt-5.4",
        "zai-max-glm-5.2",
        "kuaihui-gpt-5.5",
        "chatgpt-gpt-5.5",
        "local-deepseek-v4-flash",
        "openrouter-claude-opus-4-6",
        "claude-max-liuguoxian-opus",
        "wangsu-cheliantianxia1-gpt-5.4",
        "anthropic.claude-opus-4-8",
        "BAAI/bge-m3",
        "",
    ],
)
def test_internal_aliases_are_hidden(model_name: str) -> None:
    assert is_internal_model_alias(model_name) is True


@pytest.mark.parametrize(
    "model_name",
    [
        # 小数点不是供应商前缀，gpt-5.2 这类正常主名不能被误判。
        "gpt-5.2",
        "gpt-5.6-terra",
        "gpt-5.3-codex-spark",
        "claude-opus-4-8",
        "claude-haiku-4-5-20251001",
        "claude-code-glm-5.1",
        "minimax-m2.7",
        "bge-m3",
        # `max`/`pool` 只是别名的组成部分，不能当作独立判定词元，否则
        # 这些正常主名会被误判成内部别名而整条消失。
        "gpt-5.4-max",
        "qwen3.7-max",
        "claude-opus-max",
    ],
)
def test_clean_model_names_are_kept(model_name: str) -> None:
    assert is_internal_model_alias(model_name) is False


def test_pricing_uses_highest_priced_deployment_of_the_same_model() -> None:
    client = make_client()
    _stub(
        client,
        models={"primary": [{"id": "gpt-5.5"}]},
        infos={
            "primary": [
                # 同名多部署：透传部署没有配价，取有价的那一份。
                _info("gpt-5.5", input_cost_per_token=0, output_cost_per_token=0, mode="responses"),
                _info("gpt-5.5", input_cost_per_token=2.5e-06, output_cost_per_token=1.5e-05),
                _info("gpt-5.5", input_cost_per_token=5e-06, output_cost_per_token=3e-05, cache_read_input_token_cost=5e-07),
            ]
        },
    )

    model = _catalog(client)["gpt-5.5"]

    assert model["inputPricePerMillion"] == 5.0
    assert model["outputPricePerMillion"] == 30.0
    assert model["cacheReadPricePerMillion"] == 0.5


def test_pricing_resolves_conflicts_across_backends_by_taking_the_maximum() -> None:
    client = make_client()
    _stub(
        client,
        models={"primary": [{"id": "claude-opus-4-8"}]},
        infos={
            "primary": [_info("claude-opus-4-8", input_cost_per_token=1e-05, output_cost_per_token=5e-05)],
            "secondary": [_info("claude-opus-4-8", input_cost_per_token=2e-05, output_cost_per_token=1e-04)],
        },
    )

    model = _catalog(client)["claude-opus-4-8"]

    assert model["inputPricePerMillion"] == 20.0
    assert model["outputPricePerMillion"] == 100.0


def test_models_without_any_price_are_dropped_from_the_catalog() -> None:
    client = make_client()
    _stub(
        client,
        models={"primary": [{"id": "gpt-5.5"}, {"id": "gpt-5.4-mini"}, {"id": "codex-auto-review"}]},
        infos={
            "primary": [
                _info("gpt-5.5", input_cost_per_token=5e-06, output_cost_per_token=3e-05),
                _info("gpt-5.4-mini", input_cost_per_token=0, output_cost_per_token=0, mode="responses"),
                # 完全查不到计费记录的模型同样不展示。
            ]
        },
    )

    assert sorted(_catalog(client)) == ["gpt-5.5"]


def test_internal_aliases_are_filtered_out_of_the_catalog() -> None:
    client = make_client()
    _stub(
        client,
        models={
            "primary": [{"id": "gpt-5.2"}, {"id": "wangsu-gpt-5.5"}, {"id": "anthropic.claude-opus-4-8"}],
            "secondary": [{"id": "claude-code-glm-5.1"}, {"id": "zerokey-codex-sol"}],
        },
        infos={
            "primary": [
                _info("gpt-5.2", input_cost_per_token=1.75e-06, output_cost_per_token=1.4e-05),
                _info("wangsu-gpt-5.5", input_cost_per_token=5e-06, output_cost_per_token=3e-05),
                _info("anthropic.claude-opus-4-8", input_cost_per_token=1e-05, output_cost_per_token=5e-05),
                _info("claude-code-glm-5.1", input_cost_per_token=1.05e-06, output_cost_per_token=3.5e-06),
                _info("zerokey-codex-sol", input_cost_per_token=5e-06, output_cost_per_token=3e-05),
            ]
        },
    )

    assert sorted(_catalog(client)) == ["claude-code-glm-5.1", "gpt-5.2"]


def test_billing_type_and_capabilities_come_from_upstream_mode_and_support_flags() -> None:
    client = make_client()
    _stub(
        client,
        models={"primary": [{"id": "image-2"}, {"id": "bge-m3"}, {"id": "gpt-5.6-terra"}]},
        infos={
            "primary": [
                _info("image-2", input_cost_per_token=5e-06, output_cost_per_token=3e-05, mode="image_generation"),
                _info("bge-m3", input_cost_per_token=1e-08, mode="embedding"),
                _info(
                    "gpt-5.6-terra",
                    input_cost_per_token=2.5e-06,
                    output_cost_per_token=1.5e-05,
                    mode="responses",
                    max_input_tokens=1050000,
                    supports_vision=True,
                    supports_reasoning=True,
                    supports_function_calling=True,
                ),
            ]
        },
    )

    catalog = _catalog(client)

    assert catalog["image-2"]["billingType"] == "按次计费"
    assert catalog["bge-m3"]["billingType"] == "按量计费"
    assert catalog["bge-m3"]["capabilities"] == ["向量化"]
    assert catalog["gpt-5.6-terra"]["billingType"] == "按量计费"
    assert catalog["gpt-5.6-terra"]["capabilities"] == ["视觉", "推理", "函数调用"]
    assert catalog["gpt-5.6-terra"]["contextWindow"] == "1050000"


def test_prices_are_converted_to_cost_per_million_tokens() -> None:
    client = make_client()
    _stub(
        client,
        models={"primary": [{"id": "deepseek-v4-flash"}]},
        infos={
            "primary": [
                _info(
                    "deepseek-v4-flash",
                    input_cost_per_token=1.43e-07,
                    output_cost_per_token=2.86e-07,
                    cache_read_input_token_cost=2.8e-09,
                    cache_creation_input_token_cost=0.0,
                )
            ]
        },
    )

    model = _catalog(client)["deepseek-v4-flash"]

    assert model["inputPricePerMillion"] == 0.143
    assert model["outputPricePerMillion"] == 0.286
    assert model["cacheReadPricePerMillion"] == 0.0028
    # 单价为 0 不是「免费」，而是未配置该档位，不参与展示。
    assert model["cacheWritePricePerMillion"] is None


def test_catalog_survives_when_the_pricing_endpoint_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client()
    monkeypatch.setattr("backend.litellm_client.usage_today", lambda: date(2026, 7, 30))

    async def fake_request_backend(_backend: LiteLLMBackend, _method: str, path: str, **_kwargs: Any) -> Any:
        if path == "/models":
            return {"data": [{"id": "gpt-5.5"}, {"id": "wangsu-gpt-5.5"}]}
        if path == "/model/info":
            raise HTTPException(status_code=503, detail="upstream unavailable")
        raise AssertionError(f"unexpected path: {path}")

    client.request_backend = fake_request_backend  # type: ignore[assignment]

    catalog = _catalog(client)

    # 计费接口全挂时不清空目录，但内部别名仍然不展示。
    assert sorted(catalog) == ["gpt-5.5"]
    assert catalog["gpt-5.5"]["familyLabel"] == "OpenAI"
    assert "inputPricePerMillion" not in catalog["gpt-5.5"]
