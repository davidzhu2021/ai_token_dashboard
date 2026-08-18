"""覆盖 LiteLLM attempt 事件推送方的事件构建、字段白名单与脱敏。"""

import importlib
import sys
import types

import pytest

# 本地无 litellm 依赖（dashboard 不安装它），注入最小 CustomLogger 基类以便导入。
_litellm = types.ModuleType("litellm")
_integrations = types.ModuleType("litellm.integrations")
_custom_logger = types.ModuleType("litellm.integrations.custom_logger")


class _CustomLogger:
    def __init__(self, **kwargs):
        pass


_custom_logger.CustomLogger = _CustomLogger
_integrations.custom_logger = _custom_logger
_litellm.integrations = _integrations
sys.modules.setdefault("litellm", _litellm)
sys.modules.setdefault("litellm.integrations", _integrations)
sys.modules.setdefault("litellm.integrations.custom_logger", _custom_logger)

from integrations.litellm.observability_attempt_pusher import (  # noqa: E402
    ObservabilityAttemptPusher,
    _redact_message,
)


@pytest.fixture(autouse=True)
def _pusher_env(monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_INGEST_HMAC_SECRET", "test-secret")
    monkeypatch.setenv("OBSERVABILITY_BACKEND_ID", "primary")
    monkeypatch.setenv("OBSERVABILITY_INGEST_URL", "https://example.test/events")
    yield


def _pusher() -> ObservabilityAttemptPusher:
    pusher = ObservabilityAttemptPusher()
    assert pusher._enabled
    return pusher


def _kwargs(**overrides) -> dict:
    base = {
        "model": "gpt-4o",
        "litellm_call_id": "call-1",
        "litellm_params": {
            "model": "gpt-4o-2024-08",
            "custom_llm_provider": "openai",
            "metadata": {
                "model_group": "gpt-4o",
                "attempted_retries": 2,
                "max_retries": 3,
                "trace_id": "trace-1",
            },
        },
    }
    base.update(overrides)
    return base


def test_build_success_event_maps_attempt_fields() -> None:
    pusher = _pusher()
    event = pusher._build_attempt(
        _kwargs(),
        None,
        "2026-08-10T03:04:00Z",
        "2026-08-10T03:04:02Z",
        status="success",
    )
    assert event["eventId"] == "primary:call-1:2"
    assert event["backendId"] == "primary"
    assert event["requestId"] == "call-1"
    assert event["traceId"] == "trace-1"
    assert event["requestedModelGroup"] == "gpt-4o"
    assert event["actualModel"] == "gpt-4o-2024-08"
    assert event["provider"] == "openai"
    assert event["attemptIndex"] == 2
    assert event["retryIndex"] == 2
    assert event["isRetry"] is True
    assert event["isFallback"] is False
    assert event["durationMs"] == 2000.0


def test_build_failure_event_redacts_and_never_leaks_sensitive_fields() -> None:
    pusher = _pusher()
    exception = RuntimeError("sk-abcdef1234567890 secret-key leak https://evil.test/path")
    event = pusher._build_attempt(
        _kwargs(),
        None,
        "2026-08-10T03:04:00Z",
        "2026-08-10T03:04:02Z",
        status="failure",
        exception=exception,
    )
    assert event["status"] == "failure"
    assert event["errorClass"] == "RuntimeError"
    # 脱敏：密钥与 URL 不应出现在发送字段里
    assert "sk-abcdef1234567890" not in event["errorMessage"]
    assert "https://evil.test" not in event["errorMessage"]
    # 字段必须全部落在看板白名单内（camelCase 集合）
    from backend.main import _OBSERVABILITY_EVENT_FIELDS

    for key in event:
        assert key in _OBSERVABILITY_EVENT_FIELDS, key


def test_build_attempt_model_group_falls_back_to_litellm_params() -> None:
    """生产 proxy kwargs：litellm_params 无 model 键但有 model_group + metadata.actual_model。"""
    pusher = _pusher()
    kwargs = _kwargs()
    kwargs["litellm_params"] = {
        "model_group": "gpt-4o",
        "custom_llm_provider": "openai",
        "metadata": {"actual_model": "gpt-4o-2024-08", "attempted_retries": 0},
    }
    kwargs.pop("model", None)
    event = pusher._build_attempt(
        kwargs, None, "2026-08-10T03:04:00Z", "2026-08-10T03:04:02Z", status="success"
    )
    assert event["requestedModelGroup"] == "gpt-4o"
    assert event["actualModel"] == "gpt-4o-2024-08"
    assert event["routeName"] == "gpt-4o"


def test_build_success_event_reads_fallback_header() -> None:
    pusher = _pusher()
    response = types.SimpleNamespace(
        _hidden_params={"additional_headers": {"x-litellm-attempted-fallbacks": 1}}
    )
    event = pusher._build_attempt(
        _kwargs(),
        response,
        "2026-08-10T03:04:00Z",
        "2026-08-10T03:04:02Z",
        status="success",
    )
    assert event["isFallback"] is True
    assert event["fallbackFrom"] == "gpt-4o"
    assert event["fallbackTo"] == "gpt-4o-2024-08"


def test_pusher_disabled_without_secret(monkeypatch) -> None:
    monkeypatch.delenv("OBSERVABILITY_INGEST_HMAC_SECRET", raising=False)
    pusher = ObservabilityAttemptPusher()
    assert pusher._enabled is False


def test_redact_message_strips_credentials() -> None:
    cleaned = _redact_message("auth failed api_key=sk-abc1234567890xxx for user a@b.com")
    assert "sk-abc1234567890xxx" not in cleaned
    assert "a@b.com" not in cleaned
    assert len(cleaned) <= 300
