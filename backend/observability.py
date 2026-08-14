from __future__ import annotations

import math
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable


# This version is part of the API contract. Bump it when metric definitions
# change so cached dashboard payloads can be audited against their semantics.
STABILITY_DEFINITIONS_VERSION = "2026-08-14.v2"
SCENARIO_DEFINITIONS_VERSION = "2026-08-14.v1"


SCENARIO_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("overload", ("rate_limit", "ratelimit", "too_many_requests", "overload", "overloaded", "capacity", "busy", "429")),
    ("timeout", ("timeout", "timed out", "deadline", "request_timeout", "408", "504")),
    ("stream_break", ("stream", "unexpected eof", "broken pipe", "disconnect", "connection reset", "chunkedencoding")),
    ("tool_shape", ("tool_call", "toolcall", "tool choice", "tool schema", "function_call", "function schema", "json schema", "invalid tool", "invalidtool", "422")),
)


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


def scenario_details(record: dict[str, Any]) -> dict[str, str]:
    """Classify a failure with structured fields before controlled text rules."""

    info = error_information(record)
    metadata = metadata_dict(record)
    explicit = str(first(record, "scenario", default=metadata.get("scenario", "")) or "").strip().lower()
    if explicit and explicit not in {"unknown", "other"}:
        return {"scenario": explicit[:64], "source": "explicit", "version": SCENARIO_DEFINITIONS_VERSION}

    status = str(first(record, "http_status", "status_code", "response_status_code", "status", default="") or "").strip().lower()
    error_type = str(first(record, "error_type", "exception_type", default=metadata.get("error_type", metadata.get("exception_type", ""))) or "").strip().lower()
    rate_limit_type = str(first(record, "rate_limit_type", "rateLimitType", default=metadata.get("rate_limit_type", metadata.get("rateLimitType", ""))) or "").strip().lower()
    structured = " ".join((info["errorCode"].lower(), info["errorClass"].lower(), error_type, rate_limit_type, status))
    message = info["errorMessage"].lower()

    # A successful HTTP transport with an unusable provider payload is its own
    # actionable scenario and must not be hidden inside generic tool/schema errors.
    wrong_body_markers = (
        "wrong body", "invalid response body", "malformed response", "missing choices",
        "missing content", "unexpected response", "response validation", "decode response",
    )
    if status in {"200", "success", "ok"} and any(token in " ".join((structured, message)) for token in wrong_body_markers):
        return {"scenario": "http_200_wrong_body", "source": "structured", "version": SCENARIO_DEFINITIONS_VERSION}

    for name, tokens in SCENARIO_RULES:
        if any(token in structured for token in tokens):
            return {"scenario": name, "source": "structured", "version": SCENARIO_DEFINITIONS_VERSION}
    for name, tokens in SCENARIO_RULES:
        if any(token in message for token in tokens):
            return {"scenario": name, "source": "message", "version": SCENARIO_DEFINITIONS_VERSION}

    match = re.search(r"\b([45]\d\d)\b", " ".join((structured, message)))
    if match:
        return {"scenario": f"http_{match.group(1)[0]}xx", "source": "structured", "version": SCENARIO_DEFINITIONS_VERSION}
    return {"scenario": "unknown", "source": "unclassified", "version": SCENARIO_DEFINITIONS_VERSION}


def scenario(record: dict[str, Any]) -> str:
    return scenario_details(record)["scenario"]


def normalize_event(record: dict[str, Any]) -> dict[str, Any]:
    attempted, maximum = retry_count(record)
    status = request_status(record)
    metadata = metadata_dict(record)
    explicit_final_failure = first(
        record,
        "final_request_failure",
        "finalRequestFailure",
        "user_visible_failure",
        "userVisibleFailure",
        default=first(
            metadata,
            "final_request_failure",
            "finalRequestFailure",
            "user_visible_failure",
            "userVisibleFailure",
            default=None,
        ),
    )
    user_failure = explicit_final_failure
    if user_failure is None:
        user_failure = True if status == "failure" else (False if status == "success" else None)
    elif isinstance(user_failure, str):
        user_failure = user_failure.strip().lower() in {"1", "true", "yes", "y", "on"}
    else:
        user_failure = bool(user_failure)
    final_failure_source = "explicit" if explicit_final_failure is not None else (
        "derived" if status in {"success", "failure"} else None
    )
    classified = scenario_details(record)
    return {
        "status": status,
        "errorCode": error_information(record)["errorCode"] or None,
        "errorClass": error_information(record)["errorClass"] or None,
        "errorMessage": error_information(record)["errorMessage"] or None,
        "scenario": classified["scenario"],
        "scenarioSource": classified["source"],
        "scenarioVersion": classified["version"],
        "ttftMs": ttft_ms(record),
        "attemptedRetries": attempted,
        "maxRetries": maximum,
        "traceId": str(first(record, "trace_id", "traceId", "litellm_call_id", "session_id", default=first(metadata_dict(record), "trace_id", "traceId", "litellm_call_id", "session_id", default="")) or "")[:256] or None,
        "finalRequestFailure": user_failure,
        "finalRequestFailureSource": final_failure_source,
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


def metric_envelope(
    value: Any,
    unit: str,
    *,
    period: dict[str, str] | str | None = None,
    as_of: date | str | None = None,
    status: str = "observed",
    source: str = "usage snapshot",
    coverage_rate: float | None = None,
    sample_count: int = 0,
    missing_reasons: Iterable[str] = (),
    definition_version: str = STABILITY_DEFINITIONS_VERSION,
) -> dict[str, Any]:
    return {
        "value": value,
        "unit": unit,
        "period": period,
        "asOf": as_of.isoformat() if isinstance(as_of, date) else as_of,
        "status": status,
        "source": source,
        "coverageRate": coverage_rate,
        "sampleCount": sample_count,
        "definitionVersion": definition_version,
        "missingReasons": list(dict.fromkeys(str(item) for item in missing_reasons if item)),
    }


def _event_type(item: dict[str, Any]) -> str:
    return str(first(item, "eventType", "event_type", default="") or "").strip().lower()


def _attempt_status(item: dict[str, Any]) -> str:
    return request_status(item)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _attempt_key(item: dict[str, Any]) -> tuple[str, ...]:
    trace = str(first(item, "traceId", "trace_id", "requestId", "request_id", default="") or "")
    attempt_id = str(first(item, "attemptId", "attempt_id", default="") or "")
    if attempt_id:
        return (trace, attempt_id)
    return (
        trace,
        str(first(item, "attemptIndex", "attempt_index", default="") or ""),
        str(first(item, "actualModel", "actual_model", default="") or ""),
        str(first(item, "route", "route_name", default="") or ""),
    )


def stability_metrics(
    events: list[dict[str, Any]],
    attempt_events: list[dict[str, Any]] | None = None,
    *,
    period: dict[str, str] | str | None = None,
    as_of: date | str | None = None,
) -> dict[str, Any]:
    attempt_events = attempt_events or []
    total = len(events)
    status_complete = bool(events) and all(item.get("status") in {"success", "failure"} for item in events)
    final_failure_values = [
        item.get("finalRequestFailure", item.get("userVisibleFailure"))
        for item in events
    ]
    failure_complete = bool(events) and all(value is not None for value in final_failure_values)
    retry_complete = bool(events) and all(item.get("attemptedRetries") is not None for item in events)
    failures = sum(value is True for value in final_failure_values)
    explicit_failure_values = [
        item.get("finalRequestFailure", item.get("userVisibleFailure"))
        for item in events
        if item.get("finalRequestFailureSource") == "explicit"
    ]
    explicit_failure_count = sum(value is True for value in explicit_failure_values)
    explicit_failure_sample_count = len(explicit_failure_values)
    explicit_failure_coverage = explicit_failure_sample_count / total if total else None
    selected_failure_count = explicit_failure_count if explicit_failure_sample_count else failures
    selected_failure_sample_count = explicit_failure_sample_count if explicit_failure_sample_count else sum(value is not None for value in final_failure_values)
    selected_failure_rate = selected_failure_count / selected_failure_sample_count if selected_failure_sample_count else None
    selected_failure_status = "observed" if explicit_failure_sample_count else ("derived" if selected_failure_sample_count else "unavailable")
    retries = sum(int(item.get("attemptedRetries") or 0) > 0 for item in events)
    recovered = sum(int(item.get("attemptedRetries") or 0) > 0 and item.get("status") == "success" for item in events)
    ttfts = [item.get("ttftMs") for item in events]
    ttft_sample_count = sum(value is not None for value in ttfts)
    # Spend logs contain a first-token timestamp only for streaming calls. A
    # missing sample is therefore a coverage limitation, not a zero latency.
    ttft_coverage = ttft_sample_count / total if total else None

    # Callback hooks can emit more than one record for the same attempt. Use
    # the last terminal status per attempt identity to avoid double counting.
    attempts_by_key: dict[tuple[str, ...], dict[str, Any]] = {}
    for item in attempt_events:
        key = _attempt_key(item)
        prior = attempts_by_key.get(key)
        if prior is None or str(first(item, "endedAt", "ended_at", "eventTime", "event_time", default="")) >= str(first(prior, "endedAt", "ended_at", "eventTime", "event_time", default="")):
            attempts_by_key[key] = item
    upstream_attempts = list(attempts_by_key.values())
    failed_attempts = sum(_attempt_status(item) == "failure" for item in upstream_attempts)
    attempt_status_count = sum(_attempt_status(item) in {"success", "failure"} for item in upstream_attempts)
    attempt_coverage = attempt_status_count / len(upstream_attempts) if upstream_attempts else None

    fallback_traces: dict[str, list[dict[str, Any]]] = defaultdict(list)
    retry_traces: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in attempt_events:
        trace_id = str(first(item, "traceId", "trace_id", "requestId", "request_id", default="") or "")
        kind = _event_type(item)
        is_fallback = _truthy(first(item, "isFallback", "is_fallback", default=False)) or kind.startswith("fallback_") or first(item, "fallbackFrom", "fallback_from", "fallbackTo", "fallback_to", default=None) is not None
        is_retry = _truthy(first(item, "isRetry", "is_retry", default=False)) or kind.startswith("retry_")
        if is_fallback:
            fallback_traces[trace_id].append(item)
        attempt_index = _optional_int(first(item, "attemptIndex", "attempt_index", default=None))
        if is_retry or (attempt_index is not None and attempt_index > 0 and not is_fallback):
            retry_traces[trace_id].append(item)
    fallback_triggered = len(fallback_traces)
    fallback_recovered = sum(any(_attempt_status(item) == "success" or _event_type(item) == "fallback_success" for item in items) for items in fallback_traces.values())
    retry_triggered = len(retry_traces)
    retry_recovered_attempt = sum(any(_attempt_status(item) == "success" for item in items) for items in retry_traces.values())
    quality: dict[str, Any] = {
        "definitionsVersion": STABILITY_DEFINITIONS_VERSION,
        "finalRequestFailure": {
            "status": selected_failure_status,
            "completeness": explicit_failure_coverage or 0.0,
            "sampleCount": selected_failure_sample_count,
            "explicitSampleCount": explicit_failure_sample_count,
        },
        # Spend logs do not expose every provider/fallback attempt. Never
        # reinterpret a final failure as an upstream exception.
        "upstreamException": {
            "status": "observed" if upstream_attempts and attempt_status_count else "unavailable",
            "completeness": attempt_coverage or 0.0,
            "sampleCount": attempt_status_count,
            "missingReasons": [] if upstream_attempts else ["upstream_attempt_logs_unavailable"],
        },
        "retryRecovery": {
            "status": "observed" if retry_triggered else ("derived" if retry_complete and status_complete else "unavailable"),
            "completeness": 1.0 if retry_triggered or (retry_complete and status_complete) else 0.0,
        },
        "fallbackRecovery": {
            "status": "observed" if fallback_triggered else "unavailable",
            "completeness": 1.0 if fallback_triggered else 0.0,
            "sampleCount": fallback_triggered,
            "missingReasons": [] if fallback_triggered else ["fallback_attempt_logs_unavailable"],
        },
        "ttft": {
            "status": "observed" if ttft_sample_count else "unavailable",
            "completeness": ttft_coverage or 0.0,
            "sampleCount": ttft_sample_count,
        },
    }
    missing_reasons: list[str] = []
    if not failure_complete:
        missing_reasons.append("final_request_status_missing")
    if not retry_complete:
        missing_reasons.append("retry_fields_missing")
    if not ttft_sample_count:
        missing_reasons.append("ttft_samples_missing")
    if not upstream_attempts:
        missing_reasons.append("upstream_attempt_logs_unavailable")
    if not fallback_triggered:
        missing_reasons.append("fallback_attempt_logs_unavailable")
    result = {
        "requestCount": total,
        "userVisibleFailureCount": selected_failure_count if selected_failure_sample_count else None,
        "userVisibleFailureRate": selected_failure_rate,
        "finalRequestFailureCount": selected_failure_count if selected_failure_sample_count else None,
        "finalRequestFailureRate": selected_failure_rate,
        "finalRequestFailureExplicitCoverageRate": explicit_failure_coverage,
        "finalRequestFailureSource": "explicit" if explicit_failure_sample_count else ("derived" if selected_failure_sample_count else None),
        "upstreamExceptionCount": failed_attempts if upstream_attempts else None,
        "upstreamExceptionRate": failed_attempts / attempt_status_count if attempt_status_count else None,
        "upstreamAttemptCount": len(upstream_attempts) if upstream_attempts else None,
        "retryCount": retries if retry_complete else None,
        "retryAttemptCount": retry_triggered if retry_triggered else (retries if retry_complete else None),
        "retryAttemptRate": retry_triggered / total if total and retry_triggered else (retries / total if total and retry_complete else None),
        "retryRecoveryCount": retry_recovered_attempt if retry_triggered else (recovered if retry_complete and status_complete else None),
        "retryRecoveryRate": retry_recovered_attempt / retry_triggered if retry_triggered else (recovered / retries if retries and retry_complete and status_complete else None),
        "fallbackAttemptCount": fallback_triggered if fallback_triggered else None,
        "fallbackAttemptRate": fallback_triggered / total if total and fallback_triggered else None,
        "fallbackRecoveryCount": fallback_recovered if fallback_triggered else None,
        "fallbackRecoveryRate": fallback_recovered / fallback_triggered if fallback_triggered else None,
        "ttftP95Ms": percentile(ttfts) if ttft_sample_count else None,
        "ttftSampleCount": ttft_sample_count,
        "ttftCoverageRate": ttft_coverage,
        "statusComplete": status_complete,
        "retryComplete": retry_complete,
        "failureComplete": failure_complete,
        "quality": quality,
        "missingReasons": missing_reasons,
        "definitionsVersion": STABILITY_DEFINITIONS_VERSION,
    }
    result["metricEnvelopes"] = {
        "finalRequestFailureRate": metric_envelope(
            result["finalRequestFailureRate"], "ratio", period=period, as_of=as_of,
            status=selected_failure_status, source="final request events",
            coverage_rate=explicit_failure_coverage, sample_count=selected_failure_sample_count,
            missing_reasons=[] if selected_failure_sample_count else ["final_request_status_missing"],
        ),
        "upstreamExceptionRate": metric_envelope(
            result["upstreamExceptionRate"], "ratio", period=period, as_of=as_of,
            status="observed" if upstream_attempts else "unavailable", source="upstream attempt events",
            coverage_rate=attempt_coverage, sample_count=attempt_status_count,
            missing_reasons=[] if upstream_attempts else ["upstream_attempt_logs_unavailable"],
        ),
        "fallbackRecoveryRate": metric_envelope(
            result["fallbackRecoveryRate"], "ratio", period=period, as_of=as_of,
            status="observed" if fallback_triggered else "unavailable", source="upstream attempt events",
            coverage_rate=1.0 if fallback_triggered else 0.0, sample_count=fallback_triggered,
            missing_reasons=[] if fallback_triggered else ["fallback_attempt_logs_unavailable"],
        ),
        "retryRecoveryRate": metric_envelope(
            result["retryRecoveryRate"], "ratio", period=period, as_of=as_of,
            status=quality["retryRecovery"]["status"], source="upstream attempt events" if retry_triggered else "final request events",
            coverage_rate=quality["retryRecovery"]["completeness"], sample_count=retry_triggered or retries,
        ),
        "ttftP95Ms": metric_envelope(
            result["ttftP95Ms"], "ms", period=period, as_of=as_of,
            status="observed" if ttft_sample_count else "unavailable", source="final request events",
            coverage_rate=ttft_coverage, sample_count=ttft_sample_count,
            missing_reasons=[] if ttft_sample_count else ["ttft_samples_missing"],
        ),
    }
    return result


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


# This quality-aware definition intentionally follows the compatibility
# implementation above, so module imports resolve to the v2 behavior.
def model_state(
    failure_rate: float | None,
    ttft_p95_ms: float | None,
    failure_warn: float = 0.01,
    failure_observe: float = 0.03,
    ttft_warn: float = 2000,
    ttft_observe: float = 4000,
    *,
    ttft_coverage_rate: float | None = None,
    ttft_sample_count: int | None = None,
    minimum_ttft_coverage: float = 0.8,
    minimum_ttft_samples: int = 30,
) -> str:
    if failure_rate is None or ttft_p95_ms is None:
        return "暂无数据"
    if ttft_coverage_rate is not None and ttft_coverage_rate < minimum_ttft_coverage:
        return "观察"
    if ttft_sample_count is not None and ttft_sample_count < minimum_ttft_samples:
        return "观察"
    if failure_rate <= failure_warn and ttft_p95_ms <= ttft_warn:
        return "稳定"
    if failure_rate <= failure_observe and ttft_p95_ms <= ttft_observe:
        return "观察"
    return "需治理"


def reviewed_savings_measurements(measurements: list[dict[str, Any]], as_of: date) -> dict[str, Any]:
    """Return audited savings only; unreviewed or overlapping evidence is excluded."""

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    occupied: dict[str, list[tuple[date, date]]] = defaultdict(list)
    total = 0.0
    for raw in sorted(measurements, key=lambda item: str(first(item, "measurementStartDate", "measurement_start_date", "measurementStart", "measurement_start", default=""))):
        item = dict(raw)
        evidence = str(first(item, "evidenceUrl", "evidence_url", default="") or "").strip()
        reviewer = str(first(item, "financeReviewer", "finance_reviewer", default="") or "").strip()
        reviewed_at = first(item, "reviewedAt", "reviewed_at", default=None)
        status = str(first(item, "status", "reviewStatus", "review_status", default="") or "").lower()
        try:
            start = date.fromisoformat(str(first(item, "measurementStartDate", "measurement_start_date", "measurementStart", "measurement_start", default=""))[:10])
            end = min(as_of, date.fromisoformat(str(first(item, "measurementEndDate", "measurement_end_date", "measurementEnd", "measurement_end", default=""))[:10]))
        except ValueError:
            rejected.append({**item, "exclusionReason": "invalid_measurement_window"})
            continue
        scope = str(first(item, "scopeKey", "scope_key", "scope", default="") or "") or "|".join(str(first(item, name, default="") or "") for name in ("provider", "model", "costBucket", "cost_bucket"))
        if status not in {"reviewed", "verified", "approved"} or not evidence or not reviewer or not reviewed_at:
            rejected.append({**item, "exclusionReason": "pending_evidence_or_review"})
            continue
        if end < start:
            rejected.append({**item, "exclusionReason": "measurement_after_as_of"})
            continue
        if any(start <= prior_end and end >= prior_start for prior_start, prior_end in occupied[scope]):
            rejected.append({**item, "exclusionReason": "overlapping_measurement"})
            continue
        baseline = _number(first(item, "baselineAmountUsd", "baseline_amount_usd", default=0))
        actual = _number(first(item, "actualAmountUsd", "actual_amount_usd", default=0))
        savings = round(max(0.0, baseline - actual), 2)
        occupied[scope].append((start, end))
        accepted.append({**item, "realizedSavingsUsd": savings})
        total += savings
    return {
        "realizedSavingsUsd": round(total, 2),
        "reviewedCount": len(accepted),
        "excludedCount": len(rejected),
        "accepted": accepted,
        "excluded": rejected,
    }
