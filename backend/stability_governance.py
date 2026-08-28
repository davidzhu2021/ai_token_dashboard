from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


ERROR_MEANINGS = {
    "NO_CODE": ("Key blocked、无 deployment、超时或内部异常未标准编码", "补 synthetic code；按 message 聚类；恢复资源池或修复 blocked Key"),
    "400": ("模型名、Prompt 长度或参数不合法", "模型/参数/上下文前置校验；统一 alias；压缩或截断上下文"),
    "401": ("Key 缺失、格式错误、过期或未注册", "校验 Bearer 与 sk- 格式；轮换 Key；审计异常客户端"),
    "403": ("Key 无模型访问权限或策略拒绝", "对齐 alias 与 allowed_models；增加发布前权限探测"),
    "500": ("上游内部异常、空流或连接失败", "短重试 + fallback；隔离异常 provider/account；按 request_id 复盘"),
    "200 / failure": ("业务错误被记录成 HTTP 200", "修正状态码归一化；把上下文超限映射为标准 4xx"),
    "408": ("连接或响应超时", "分离 connect/read timeout；备用路由；监控 TTFT/P99"),
    "invalid_request_error": ("消息结构不完整", "请求前执行消息 schema 校验；拒绝空 assistant message"),
    "404": ("Endpoint 或资源不存在", "校验 responses/chat 路由及 provider adapter"),
    "400001": ("模型参数限制", "建立按模型的参数兼容层"),
    "504": ("上游 idle timeout", "provider fallback；慢供应商降权或摘除"),
    "invalid_parameter_error": ("Tool schema 缺必填字段", "工具注册时执行 JSON Schema 校验"),
    "invalid_argument": ("工具类型或参数类型不合法", "统一 tool type=function；增加契约测试"),
    "502": ("上游 provider unavailable", "熔断故障 provider，切备用供应商并做恢复探测"),
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
