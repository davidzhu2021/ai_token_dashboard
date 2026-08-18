"""验证稳定性 attempt 事件采集链路（签名 + 接收端）的自检工具。

用法
====
python scripts/observability_ingest_check.py --secret <HMAC密钥> --backend-id <backend_id>
    [--url https://myai.carher.net/api/internal/observability/events]
    [--dry-run]            # 只打印将发送的请求头/正文，不真正发送
    [--event-count N]      # 发送 N 条测试事件（默认 1）

签名算法与 backend/main.py 的 internal_observability_events 完全一致：
    digest    = sha256_hex(body)
    signature = hmac_sha256_hex(secret, f"{timestamp}.{digest}")
    header    = x-observability-signature: sha256=<signature>
    header    = x-observability-timestamp: <unix 秒>

注意：发送的事件会真实写入 stability_attempt_events 表（eventId 幂等，
重复发送同一 backend_id 会因 eventId 相同被 ON CONFLICT DO NOTHING 跳过）。
测试完成后可在稳定性看板「场景样本」中看到 status=test 的样本。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.request


def build_signature(secret: str, timestamp: str, body: bytes) -> str:
    digest = hashlib.sha256(body).hexdigest()
    return hmac.new(secret.encode("utf-8"), f"{timestamp}.{digest}".encode("ascii"), hashlib.sha256).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="稳定性 attempt 事件采集链路自检")
    parser.add_argument("--secret", required=True, help="与看板 OBSERVABILITY_INGEST_HMAC_SECRET 相同的密钥")
    parser.add_argument("--backend-id", required=True, help="后端标识，必须与看板 usage_backend_ids() 一致")
    parser.add_argument(
        "--url",
        default="https://myai.carher.net/api/internal/observability/events",
        help="采集接口地址",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印请求，不发送")
    parser.add_argument("--event-count", type=int, default=1, help="测试事件条数（1-500）")
    args = parser.parse_args()

    count = max(1, min(500, args.event_count))
    now = int(time.time())
    events = []
    for index in range(count):
        events.append(
            {
                "eventId": f"ingest-check:{args.backend_id}:{now}:{index}",
                "backendId": args.backend_id,
                "traceId": f"ingest-check-{now}",
                "requestId": f"ingest-check-{now}-{index}",
                "attemptId": f"ingest-check-{now}-{index}",
                "attemptIndex": 0,
                "retryIndex": 0,
                "requestedModelGroup": "ingest-check",
                "actualModel": "ingest-check",
                "provider": "ingest-check",
                "eventType": "test",
                "status": "test",
                "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                "endedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 1)),
                "durationMs": 1000.0,
                "isRetry": False,
                "isFallback": False,
                "errorCode": "ingest_check",
                "errorClass": "IngestCheck",
                "errorMessage": "采集链路自检事件（可安全删除）",
            }
        )
    body = json.dumps({"events": events}, ensure_ascii=False).encode("utf-8")
    timestamp = str(now)
    signature = build_signature(args.secret, timestamp, body)
    headers = {
        "Content-Type": "application/json",
        "x-observability-timestamp": timestamp,
        "x-observability-signature": f"sha256={signature}",
    }

    print(f"URL        : {args.url}")
    print(f"BackendId  : {args.backend_id}")
    print(f"Events     : {count} 条")
    print(f"Timestamp  : {timestamp}")
    print(f"Signature  : sha256={signature}")

    if args.dry_run:
        print("Dry-run 模式：不发送。请求头：")
        for key, value in headers.items():
            print(f"  {key}: {value}")
        print("请求体（前 500 字节）：")
        print(body[:500].decode("utf-8", errors="replace"))
        return 0

    request = urllib.request.Request(args.url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read().decode("utf-8", errors="replace")
            print(f"HTTP {response.status}")
            print(payload)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code}", file=sys.stderr)
        print(detail, file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"网络错误: {exc.reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
