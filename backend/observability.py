from __future__ import annotations

import math
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable


def first(record: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in record and record[name] is not None:
            return record[name]
    return default


def metadata_dict(record: dict[str, Any]) -> dict[str, Any]:
    value = first(record, "metadata", "request_tags", "tags", default={})
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def nested_first(record: dict[str, Any], *names: str, default: Any = None) -> Any:
    value = first(record, *names, default=None)
    if value is not None:
        return value
    return first(metadata_dict(record), *names, default=default)


def _number(value: Any) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return None


def parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value).strip()
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                result = datetime.fromtimestamp(float(value), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def ttft_ms(record: dict[str, Any]) -> float | None:
    stream = nested_first(record, "stream", default=None)
    if stream is False:
        return None
    start = parse_datetime(first(record, "startTime", "start_time", "created_at"))
    completion = parse_datetime(first(record, "completionStartTime", "completion_start_time"))
    end = parse_datetime(first(record, "endTime", "end_time", "completionEndTime"))
    # LiteLLM uses an end-time completion marker for non-streaming calls; it
    # is not a first-token observation and must not enter TTFT percentiles.
    if not start or not completion or (end and completion == end):
        return None
    value = (completion - start).total_seconds() * 1000
    return value if value > 0 else None


def error_information(record: dict[str, Any]) -> dict[str, str]:
    info = first(metadata_dict(record), "error_information", "errorInformation", default={})
    if not isinstance(info, dict):
        info = {}
    return {
        "errorCode": str(first(info, "error_code", "errorCode", default=nested_first(record, "error_code", "errorCode", default="")) or "")[:120],
        "errorClass": str(first(info, "error_class", "errorClass", default=nested_first(record, "error_class", "errorClass", default="")) or "")[:160],
        "errorMessage": redact_error_message(first(info, "error_message", "errorMessage", default=nested_first(record, "error_message", "errorMessage", default=""))),
    }


def redact_error_message(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-***", text)
    text = re.sub(r"Bearer\s+\S+", "Bearer ***", text, flags=re.I)
    text = re.sub(r"(?i)\b(api[_ -]?key|token|password|passwd|secret|authorization|access[_ -]?token|refresh[_ -]?token)\s*[:=]\s*[^\s,;]+", r"\1=***", text)
    text = re.sub(r"(?i)([?&](?:api[_-]?key|token|password|secret|authorization)=)[^&\s]+", r"\1***", text)
    text = re.sub(r"(?is)([\"']?(?:prompt|messages?|response|completion|content|body|choices)[\"']?\s*[:=]\s*)(.*?)(?=,\s*[\"']?[A-Za-z_]|$)", r"\1<redacted>", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "<email>", text)
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<ip>", text)
    text = re.sub(r"https?://[^\s]+", "<url>", text)
    # Keep only a short diagnostic fragment; request/response bodies and
    # opaque credential-like blobs are intentionally not persisted.
    text = re.sub(r"\b[A-Za-z0-9_-]{32,}\b", "<redacted>", text)
    return text[:300]


def request_status(record: dict[str, Any]) -> str:
    value = first(record, "status", "response_status", "status_filter", default=None)
    if value is None:
        value = metadata_dict(record).get("status")
    if value is None or value == "":
        return "unknown"
    text = str(value).strip().lower()
    if text in {"ok", "200", "success", "succeeded", "completed", "complete"}:
        return "success"
    if text.isdigit() and 200 <= int(text) < 400:
        return "success"
    if text.isdigit() and 400 <= int(text) < 600:
        return "failure"
    if "fail" in text or "error" in text or text in {"4xx", "5xx"}:
        return "failure"
    return text[:64] or "unknown"


def retry_count(record: dict[str, Any]) -> tuple[int | None, int | None]:
    metadata = metadata_dict(record)
    attempted = _optional_int(first(record, "attempted_retries", "attemptedRetries", default=metadata.get("attempted_retries", metadata.get("attemptedRetries"))))
    maximum = _optional_int(first(record, "max_retries", "maxRetries", default=metadata.get("max_retries", metadata.get("maxRetries"))))
    return attempted, maximum


def scenario(record: dict[str, Any]) -> str:
    info = error_information(record)
    haystack = " ".join(str(value or "") for value in (info["errorCode"], info["errorClass"], info["errorMessage"], first(record, "status", default=""))).lower()
    if any(token in haystack for token in ("overload", "rate_limit", "429", "capacity", "busy")):
        return "overload"
    if any(token in haystack for token in ("timeout", "timed out", "deadline", "408", "504")):
        return "timeout"
    if any(token in haystack for token in ("stream", "eof", "broken pipe", "disconnect")):
        return "stream_break"
    if any(token in haystack for token in ("tool", "schema", "function", "400", "422")):
        return "tool_shape"
    match = re.search(r"\b([45]\d\d)\b", haystack)
    if match:
        return f"http_{match.group(1)[0]}xx"
    return "unknown"


def normalize_event(record: dict[str, Any]) -> dict[str, Any]:
    attempted, maximum = retry_count(record)
    status = request_status(record)
    metadata = metadata_dict(record)
    user_failure = first(record, "user_visible_failure", "userVisibleFailure", default=first(metadata, "user_visible_failure", "userVisibleFailure", default=None))
    if user_failure is None:
        user_failure = True if status == "failure" else (False if status == "success" else None)
    elif isinstance(user_failure, str):
        user_failure = user_failure.strip().lower() in {"1", "true", "yes", "y", "on"}
    else:
        user_failure = bool(user_failure)
    return {
        "status": status,
        "errorCode": error_information(record)["errorCode"] or None,
        "errorClass": error_information(record)["errorClass"] or None,
        "errorMessage": error_information(record)["errorMessage"] or None,
        "scenario": str(first(record, "scenario", default=scenario(record)) or "unknown")[:64],
        "ttftMs": ttft_ms(record),
        "attemptedRetries": attempted,
        "maxRetries": maximum,
        "traceId": str(first(record, "trace_id", "traceId", "litellm_call_id", "session_id", default=first(metadata_dict(record), "trace_id", "traceId", "litellm_call_id", "session_id", default="")) or "")[:256] or None,
        "userVisibleFailure": user_failure,
    }


def percentile(values: Iterable[float], percentile_value: float = 0.95) -> float | None:
    clean = sorted(float(value) for value in values if value is not None and math.isfinite(float(value)) and float(value) > 0)
    if not clean:
        return None
    index = (len(clean) - 1) * percentile_value
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return round(clean[lower], 2)
    return round(clean[lower] + (clean[upper] - clean[lower]) * (index - lower), 2)


def stability_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(events)
    status_complete = bool(events) and all(item.get("status") in {"success", "failure"} for item in events)
    failure_complete = bool(events) and all(item.get("userVisibleFailure") is not None for item in events)
    retry_complete = bool(events) and all(item.get("attemptedRetries") is not None for item in events)
    failures = sum(item.get("userVisibleFailure") is True for item in events)
    retries = sum(int(item.get("attemptedRetries") or 0) > 0 for item in events)
    recovered = sum(int(item.get("attemptedRetries") or 0) > 0 and item.get("status") == "success" for item in events)
    ttfts = [item.get("ttftMs") for item in events]
    return {
        "requestCount": total,
        "userVisibleFailureCount": failures if failure_complete else None,
        "userVisibleFailureRate": failures / total if total and failure_complete else None,
        "upstreamExceptionCount": sum(item.get("status") == "failure" for item in events) if status_complete else None,
        "retryCount": retries if retry_complete else None,
        "retryRecoveryRate": recovered / retries if retries and retry_complete and status_complete else None,
        "ttftP95Ms": percentile(ttfts),
        "statusComplete": status_complete,
        "retryComplete": retry_complete,
        "failureComplete": failure_complete,
    }


def model_state(failure_rate: float | None, ttft_p95_ms: float | None, failure_warn: float = 0.01, failure_observe: float = 0.03, ttft_warn: float = 2000, ttft_observe: float = 4000) -> str:
    if failure_rate is None or ttft_p95_ms is None:
        return "暂无数据"
    if failure_rate <= failure_warn and ttft_p95_ms <= ttft_warn:
        return "稳定"
    if failure_rate <= failure_observe and ttft_p95_ms <= ttft_observe:
        return "观察"
    return "需治理"


def monthly_forecast(actual: float, start: date, today: date, budget: float | None) -> dict[str, float | None]:
    month_start = date(start.year, start.month, 1)
    next_month = date(month_start.year + 1, 1, 1) if month_start.month == 12 else date(month_start.year, month_start.month + 1, 1)
    days_in_month = (next_month - month_start).days
    elapsed = max(0, (today - start).days + 1)
    daily = actual / elapsed if elapsed else 0.0
    forecast = actual + max(0, days_in_month - elapsed) * daily
    return {"actual": actual, "dailyAverage": daily, "forecast": forecast, "budget": budget, "budgetDelta": forecast - budget if budget is not None else None}


def verified_savings(actions: list[dict[str, Any]], today: date) -> float:
    total = 0.0
    for action in actions:
        if str(action.get("status") or "").lower() != "verified":
            continue
        verified = parse_datetime(action.get("verifiedDate"))
        verified_daily = action.get("verifiedDailyCost")
        if not verified or verified_daily is None:
            continue
        run_days = max(0, (today - verified.date()).days + 1)
        total += max(0.0, _number(action.get("baselineDailyCost")) - _number(verified_daily)) * run_days
    return round(total, 2)
