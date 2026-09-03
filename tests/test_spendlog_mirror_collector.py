from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

from integrations.litellm.spendlog_mirror_collector import (
    build_event,
    signed_headers,
)


def test_build_event_keeps_only_stability_fields_and_hashes_principal() -> None:
    event = build_event(
        {
            "request_id": "req-1",
            "startTime": datetime(2026, 9, 3, 1, 2, tzinfo=timezone.utc),
            "endTime": datetime(2026, 9, 3, 1, 2, 1, tzinfo=timezone.utc),
            "completionStartTime": datetime(2026, 9, 3, 1, 2, 0, 250000, tzinfo=timezone.utc),
            "model": "provider/private-model",
            "model_group": "codex",
            "custom_llm_provider": "openai",
            "status": "success",
            "user": "employee@example.com",
            "team_id": "team-1",
            "organization_id": "org-1",
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "messages": [{"role": "user", "content": "secret prompt"}],
            "response": {"content": "secret response"},
            "api_key": "hashed-key-value",
            "metadata": {"error_information": {"error_code": "upstream_timeout"}},
        },
        source="litellm-198",
        principal_salt="salt-for-test",
        collected_at=datetime(2026, 9, 3, 1, 3, tzinfo=timezone.utc),
    )

    assert event == {
        "sourceRequestId": "req-1",
        "eventTime": "2026-09-03T01:02:00+00:00",
        "model": "codex",
        "actualModel": "provider/private-model",
        "modelGroup": "codex",
        "provider": "openai",
        "status": "success",
        "errorCode": "upstream_timeout",
        "teamId": "team-1",
        "organizationId": "org-1",
        "principalHash": hashlib.sha256(b"salt-for-test:employee@example.com").hexdigest(),
        "requestDurationMs": 1000.0,
        "ttftMs": 250.0,
        "promptTokens": 10,
        "completionTokens": 20,
        "totalTokens": 30,
        "collectedAt": "2026-09-03T01:03:00+00:00",
    }
    serialized = json.dumps(event)
    assert "secret prompt" not in serialized
    assert "secret response" not in serialized
    assert "hashed-key-value" not in serialized
    assert "employee@example.com" not in serialized


def test_signed_headers_match_dashboard_hmac_contract() -> None:
    body = b'{"events":[{"sourceRequestId":"req-1"}]}'
    headers = signed_headers(body, "shared-secret", timestamp=1_725_000_000)
    digest = hashlib.sha256(body).hexdigest()
    expected = hmac.new(
        b"shared-secret",
        f"1725000000.{digest}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()

    assert headers["x-observability-timestamp"] == "1725000000"
    assert headers["x-observability-signature"] == f"sha256={expected}"
