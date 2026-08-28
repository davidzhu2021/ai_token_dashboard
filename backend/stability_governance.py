from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


ERROR_MEANINGS = {
    "NO_CODE": ("未标准化的内部、超时或资源异常", "补充合成错误码并按消息聚类，恢复资源池或修复阻断 Key"),
    "400": ("模型、Prompt 或参数不合法", "请求前校验模型、参数和上下文，统一模型别名"),
    "401": ("Key 缺失、失效或未注册", "校验认证格式并轮换失效 Key，审计异常客户端"),
    "403": ("模型权限或策略拒绝", "对齐模型权限与发布前探测，收敛访问策略"),
    "500": ("上游内部异常或连接失败", "短重试并切换备用来源，隔离异常来源后复盘"),
}


def _code(row: dict[str, Any]) -> str:
    value = row.get("error_code")
    if not value:
        status = str(row.get("status_code") or row.get("status") or "").strip()
        if status == "429":
            value = "429"
        elif status.isdigit() and int(status) >= 400:
            value = "NO_CODE"
        else:
            value = ""
    text = str(value).strip()
    return text if text and text.lower() not in {"none", "null"} else "NO_CODE"


def _is_429(row: dict[str, Any]) -> bool:
    return _code(row) == "429" or str(row.get("status_code") or "") == "429"


def _account_ref(value: Any) -> str:
    text = str(value or "")
    return f"...{text[-6:]}" if len(text) > 6 else (text or "未知账号")


def build_error_governance(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    records = [dict(row) for row in rows]
    errors = []
    for row in records:
        if _is_429(row):
            continue
        status = str(row.get("status_code") or row.get("status") or "").lower()
        code = _code(row)
        explicit_code = str(row.get("error_code") or "").strip()
        if code not in {"", "NO_CODE"} or explicit_code == "NO_CODE" or status in {"failure", "error", "408", "500", "502", "503", "504"} or (status.isdigit() and int(status) >= 400):
            errors.append(row)
    rate_limits = [row for row in records if _is_429(row)]
    by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in errors:
        by_code[_code(row)].append(row)
    denominator = len(errors)
    total = len(records)
    rankings = []
    def rank_key(pair: tuple[str, list[dict[str, Any]]]) -> tuple[int, int, str]:
        code = pair[0]
        numeric = int(code) if code.isdigit() else -1
        return (-len(pair[1]), -numeric, code)

    for code, items in sorted(by_code.items(), key=rank_key):
        meaning, action = ERROR_MEANINGS.get(code, ("上游或客户端异常", "按错误分类、消息、模型和请求 ID 下钻定位"))
        rankings.append({
            "errorCode": code,
            "count": len(items),
            "errorShare": len(items) / denominator if denominator else None,
            "totalShare": len(items) / total if total else None,
            "meaning": meaning,
            "action": action,
            "samples": [{"requestId": item.get("request_id"), "eventDate": str(item.get("event_date") or ""), "model": item.get("model"), "provider": item.get("provider"), "errorClass": item.get("error_class"), "message": item.get("error_message"), "accountRef": _account_ref(item.get("api_key"))} for item in items[:20]],
        })
    daily_codes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in errors:
        daily_codes[str(row.get("event_date") or row.get("startTime") or "")[:10]][_code(row)] += 1
    daily = [{"date": day, "errorCount": sum(codes.values()), "errorCodes": dict(codes)} for day, codes in sorted(daily_codes.items())]
    top5 = sum(item["count"] for item in rankings[:5])
    return {"overview": {"totalRequests": total, "stabilityErrorCount": denominator, "stabilityErrorRate": denominator / total if total else None, "rateLimitCount": len(rate_limits), "rateLimitRate": len(rate_limits) / total if total else None, "top5Concentration": top5 / denominator if denominator else None}, "errorCodes": rankings, "daily": daily}
