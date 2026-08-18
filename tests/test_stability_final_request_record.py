"""覆盖 spend-log 事件 → stability_attempt_events（final_request）转换逻辑。"""

from datetime import datetime, timezone

from backend.usage_store import UsageStore


def _event(**overrides) -> dict:
    base = {
        "request_id": "req-123",
        "event_time": "2026-08-10T03:04:05Z",
        "status": "failure",
        "error_code": "429",
        "error_class": "RateLimitError",
        "scenario": "overload",
        "scenario_version": "2026-08-14.v1",
        "provider": "openai",
        "model_group": "gpt-4o",
        "model": "gpt-4o",
        "trace_id": "trace-abc",
        "attempted_retries": 2,
        "max_retries": 3,
        "start_time": "2026-08-10T03:04:04Z",
        "end_time": "2026-08-10T03:04:06Z",
        "ttft_ms": 320.5,
    }
    base.update(overrides)
    return base


def test_final_request_record_maps_fields() -> None:
    received = datetime(2026, 8, 10, 3, 5, 0, tzinfo=timezone.utc)
    record = UsageStore._stability_final_request_record(_event(), received)
    assert record is not None
    (backend_id, event_id, request_id, trace_id, attempt_id, attempt_index,
     retry_index, requested_model_group, actual_model, route_name, provider,
     event_type, status, error_code, error_class, error_category, error_message,
     scenario, scenario_version, event_time, event_date, started_at, ended_at,
     ttft_ms, duration_ms, fallback_from, fallback_to, is_retry, is_fallback,
     collected_at, received_at) = record

    assert event_id == "req-123"
    assert request_id == "req-123"
    assert trace_id == "trace-abc"
    assert attempt_index == 2          # Router 语义：重试 2 次后最终尝试
    assert retry_index == 2
    assert requested_model_group == "gpt-4o"
    assert actual_model == "gpt-4o"
    assert provider == "openai"
    assert event_type == "final_request"  # 与推送的 attempt 事件区分
    assert status == "failure"
    assert error_code == "429"
    assert error_class == "RateLimitError"
    assert error_message == ""          # spend log 无错误消息字段
    assert scenario == "overload"
    assert scenario_version == "2026-08-14.v1"
    assert event_date.isoformat() == "2026-08-10"
    assert ttft_ms == 320.5
    assert duration_ms == 2000.0        # 04:04 -> 04:06
    assert fallback_from == ""
    assert fallback_to == ""
    assert is_retry is True
    assert is_fallback is False         # spend log 无 fallback 过程信息
    assert collected_at == received


def test_final_request_record_skips_missing_request_id() -> None:
    received = datetime.now(timezone.utc)
    assert UsageStore._stability_final_request_record(_event(request_id=""), received) is None
    assert UsageStore._stability_final_request_record(_event(request_id=None), received) is None


def test_final_request_record_success_no_retry() -> None:
    received = datetime.now(timezone.utc)
    record = UsageStore._stability_final_request_record(
        _event(status="success", attempted_retries=0, error_code="", error_class=""), received
    )
    assert record is not None
    assert record[12] == "success"  # status
    assert record[27] is False       # is_retry


def test_final_request_record_duration_fallback() -> None:
    received = datetime.now(timezone.utc)
    record = UsageStore._stability_final_request_record(
        _event(request_duration_ms=None, start_time="2026-08-10T03:04:00Z", end_time="2026-08-10T03:04:02.5Z"),
        received,
    )
    assert record is not None
    assert record[24] == 2500.0  # duration_ms 由 start/end 推导


def test_final_request_record_usage_date_override() -> None:
    received = datetime.now(timezone.utc)
    record = UsageStore._stability_final_request_record(
        _event(usage_date="2026-08-09", event_time="2026-08-10T00:30:00Z"), received
    )
    assert record is not None
    assert record[20].isoformat() == "2026-08-09"  # event_date 优先取 usage_date（本地日界）
