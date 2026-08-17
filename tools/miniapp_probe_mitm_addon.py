"""
mitmproxy/mitmdump 插件：记录一次微信小程序“提取文案”操作产生的 HTTP(S) 请求。

它只做被动记录，不破解、不反编译、不绕过登录/风控。

运行：
  MINIAPP_PROBE_OUT=/Users/yangmeng/myweb/miniapp_flows.jsonl \\
    mitmdump -s /Users/yangmeng/myweb/tools/miniapp_probe_mitm_addon.py

然后在手机或电脑微信所在设备上设置 HTTP 代理到运行 mitmdump 的机器。
完成一次“小程序粘贴链接 -> 开始提取 -> 等待结果”的操作后，停止 mitmdump，
再运行：
  python3 /Users/yangmeng/myweb/tools/miniapp_extraction_detector.py \\
    --flows /Users/yangmeng/myweb/miniapp_flows.jsonl
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from mitmproxy import http


OUT = Path(os.environ.get("MINIAPP_PROBE_OUT", "miniapp_flows.jsonl")).expanduser()
MAX_BODY_CHARS = int(os.environ.get("MINIAPP_PROBE_MAX_BODY_CHARS", "60000"))

SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-token",
    "token",
    "access-token",
    "x-access-token",
}

SENSITIVE_KEYS = re.compile(
    r"(?i)(token|secret|session|sid|openid|unionid|cookie|password|passwd|auth|key)"
)

TEXTUAL_TYPES = (
    "json",
    "text",
    "javascript",
    "xml",
    "html",
    "x-www-form-urlencoded",
)


def redact_value(value: str) -> str:
    if not value:
        return value
    if len(value) <= 10:
        return "[REDACTED]"
    return f"[REDACTED len={len(value)}]"


def scrub(obj: Any) -> Any:
    if isinstance(obj, dict):
        clean = {}
        for key, value in obj.items():
            if SENSITIVE_KEYS.search(str(key)):
                clean[key] = redact_value(str(value))
            else:
                clean[key] = scrub(value)
        return clean
    if isinstance(obj, list):
        return [scrub(item) for item in obj]
    if isinstance(obj, str):
        # 粗略遮盖 URL/query/body 中常见 token 字段。
        return re.sub(
            r"(?i)(token|secret|session|sid|openid|unionid|auth|key)=([^&\s]+)",
            lambda m: f"{m.group(1)}=[REDACTED len={len(m.group(2))}]",
            obj,
        )
    return obj


def headers_to_dict(headers) -> dict[str, str]:
    result = {}
    for key, value in headers.items():
        if key.lower() in SENSITIVE_HEADERS:
            result[key] = redact_value(value)
        else:
            result[key] = value
    return result


def body_to_text(headers: dict[str, str], raw: bytes | None) -> str:
    if not raw:
        return ""

    content_type = headers.get("content-type") or headers.get("Content-Type") or ""
    is_textual = any(marker in content_type.lower() for marker in TEXTUAL_TYPES)

    # 如果响应头不准确，也尝试识别 UTF-8 文本。
    sample = raw[: min(len(raw), MAX_BODY_CHARS)]
    if is_textual:
        return scrub(sample.decode("utf-8", errors="ignore"))[:MAX_BODY_CHARS]

    try:
        decoded = sample.decode("utf-8")
        if sum(ch.isprintable() or ch.isspace() for ch in decoded) / max(len(decoded), 1) > 0.85:
            return scrub(decoded)[:MAX_BODY_CHARS]
    except Exception:
        pass

    return f"[binary body omitted: {len(raw)} bytes, base64_head={base64.b64encode(sample[:48]).decode()}]"


def query_to_dict(query) -> dict[str, str]:
    result = {}
    for key, value in query.items(multi=False):
        if SENSITIVE_KEYS.search(str(key)):
            result[key] = redact_value(str(value))
        else:
            result[key] = value
    return result


def response(flow: http.HTTPFlow) -> None:
    req_headers = headers_to_dict(flow.request.headers)
    resp_headers = headers_to_dict(flow.response.headers) if flow.response else {}

    record = {
        "ts": time.time(),
        "request": {
            "method": flow.request.method,
            "scheme": flow.request.scheme,
            "host": flow.request.pretty_host,
            "path": scrub(flow.request.path),
            "url": scrub(flow.request.pretty_url),
            "headers": req_headers,
            "query": query_to_dict(flow.request.query),
            "body_text": body_to_text(req_headers, flow.request.raw_content),
        },
        "response": {
            "status_code": flow.response.status_code if flow.response else None,
            "reason": flow.response.reason if flow.response else None,
            "headers": resp_headers,
            "body_text": body_to_text(resp_headers, flow.response.raw_content if flow.response else None),
        },
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
