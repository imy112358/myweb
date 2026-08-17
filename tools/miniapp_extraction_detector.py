#!/usr/bin/env python3
"""
微信小程序“文案提取/视频转文字”实现方式黑盒检测脚本。

用途：
  - 分析你有权测试的小程序网络日志（HAR）或公开分享链接；
  - 判断它大概率使用了哪些链路：平台原文解析、视频/音频下载、ASR 语音识别、
    OCR 画面识别、大模型整理、字幕导出等。

边界：
  - 不反编译小程序包；
  - 不绕过登录、风控、证书校验或平台权限；
  - 不提供抓取私密内容或规避平台限制的能力。

示例：
  python3 tools/miniapp_extraction_detector.py --har traffic.har
  python3 tools/miniapp_extraction_detector.py --flows miniapp_flows.jsonl
  python3 tools/miniapp_extraction_detector.py --url "https://v.douyin.com/xxxx/"
  python3 tools/miniapp_extraction_detector.py --har traffic.har --url "https://www.bilibili.com/video/BV..."
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


SIGNALS: dict[str, dict[str, Any]] = {
    "platform_metadata": {
        "label": "平台原始文案/元数据解析",
        "weight": 2,
        "patterns": [
            r"aweme_id",
            r"sec_uid",
            r"note_id",
            r"xsec_token",
            r"bvid",
            r"aid=",
            r"cid=",
            r"desc(ription)?",
            r"title",
            r"author",
            r"nickname",
            r"share_info",
            r"iteminfo",
            r"web/api",
        ],
    },
    "media_download": {
        "label": "视频/音频资源获取或下载",
        "weight": 3,
        "patterns": [
            r"play_addr",
            r"download_addr",
            r"video_url",
            r"media_url",
            r"audio_url",
            r"\.mp4(\?|$)",
            r"\.m4a(\?|$)",
            r"\.mp3(\?|$)",
            r"\.wav(\?|$)",
            r"ffmpeg",
            r"extract_audio",
            r"download(video|media|audio)?",
            r"transcode",
        ],
    },
    "asr": {
        "label": "ASR 语音识别/转写",
        "weight": 4,
        "patterns": [
            r"\basr\b",
            r"speech[-_]?to[-_]?text",
            r"transcri(be|pt|ption)",
            r"recognition",
            r"recognize",
            r"subtitle",
            r"\bsrt\b",
            r"\bvtt\b",
            r"whisper",
            r"faster[-_]?whisper",
            r"iflytek|xfyun|iat-api",
            r"tencent(cloud)?|asr\.cloud\.tencent",
            r"aliyun|nls-gateway|tingwu",
            r"volcengine|huoshan|openspeech",
            r"baidu.*speech|speech.*baidu",
            r"openai.*audio|audio.*transcriptions",
        ],
    },
    "ocr": {
        "label": "OCR 画面文字识别",
        "weight": 3,
        "patterns": [
            r"\bocr\b",
            r"image[-_]?to[-_]?text",
            r"paddleocr",
            r"tesseract",
            r"vision",
            r"frame",
            r"screenshot",
            r"extract_frame",
            r"img_recognize",
            r"baidu.*ocr|ocr.*baidu",
            r"tencent.*ocr|ocr.*tencent",
            r"aliyun.*ocr|ocr.*aliyun",
        ],
    },
    "llm_rewrite": {
        "label": "大模型整理/摘要/改写",
        "weight": 3,
        "patterns": [
            r"summary|summari[sz]e",
            r"rewrite",
            r"polish",
            r"clean_text",
            r"copywriting",
            r"prompt",
            r"chatgpt",
            r"openai",
            r"gpt-",
            r"deepseek",
            r"qwen|dashscope",
            r"doubao|ark\.cn|volcengine.*ark",
            r"glm|zhipu",
            r"moonshot|kimi",
        ],
    },
    "task_queue": {
        "label": "异步任务/队列处理",
        "weight": 2,
        "patterns": [
            r"task_id",
            r"job_id",
            r"queue",
            r"status",
            r"progress",
            r"processing",
            r"completed",
            r"failed",
            r"poll",
            r"callback",
        ],
    },
    "export": {
        "label": "导出 TXT/SRT/VTT/DOCX/Markdown",
        "weight": 1,
        "patterns": [
            r"\.txt(\?|$)",
            r"\.srt(\?|$)",
            r"\.vtt(\?|$)",
            r"\.docx(\?|$)",
            r"\.md(\?|$)",
            r"export",
            r"download_result",
            r"file_url",
        ],
    },
}


PLATFORMS = {
    "douyin": [r"douyin\.com", r"iesdouyin\.com", r"amemv\.com", r"aweme", r"snssdk"],
    "xiaohongshu": [r"xiaohongshu\.com", r"xhslink\.com", r"xhscdn\.com", r"redcdn"],
    "bilibili": [r"bilibili\.com", r"b23\.tv", r"biliapi", r"bvid", r"bvc"],
    "wechat": [r"weixin\.qq\.com", r"qq\.com", r"servicewechat\.com"],
}


@dataclass
class Evidence:
    category: str
    source: str
    snippet: str
    score: int


@dataclass
class Analysis:
    scores: Counter = field(default_factory=Counter)
    evidence: list[Evidence] = field(default_factory=list)
    domains: Counter = field(default_factory=Counter)
    methods: Counter = field(default_factory=Counter)
    status_codes: Counter = field(default_factory=Counter)
    platform_hits: Counter = field(default_factory=Counter)
    urls: list[str] = field(default_factory=list)


def normalize_text(value: Any, limit: int = 200_000) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False)
        except TypeError:
            text = str(value)
    else:
        text = str(value)
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def snippet_around(text: str, match: re.Match[str], width: int = 90) -> str:
    start = max(0, match.start() - width // 2)
    end = min(len(text), match.end() + width // 2)
    return text[start:end].replace("\n", " ")


def add_signals(analysis: Analysis, text: str, source: str) -> None:
    if not text:
        return
    searchable = text[:200_000]

    for platform, patterns in PLATFORMS.items():
        for pattern in patterns:
            if re.search(pattern, searchable, re.I):
                analysis.platform_hits[platform] += 1
                break

    for category, config in SIGNALS.items():
        hits = 0
        for pattern in config["patterns"]:
            match = re.search(pattern, searchable, re.I)
            if not match:
                continue
            hits += 1
            if len([e for e in analysis.evidence if e.category == category]) < 8:
                analysis.evidence.append(
                    Evidence(
                        category=category,
                        source=source,
                        snippet=snippet_around(searchable, match),
                        score=config["weight"],
                    )
                )
        if hits:
            # 多个关键词加分，但避免单一长响应把分数刷爆。
            analysis.scores[category] += min(hits, 5) * int(config["weight"])


def iter_har_entries(har: dict[str, Any]) -> Iterable[dict[str, Any]]:
    log = har.get("log", {})
    entries = log.get("entries", [])
    if not isinstance(entries, list):
        return []
    return entries


def parse_har(path: Path) -> Analysis:
    analysis = Analysis()
    try:
        har = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        har = json.loads(path.read_text(encoding="utf-8-sig"))

    for idx, entry in enumerate(iter_har_entries(har), start=1):
        request = entry.get("request", {}) or {}
        response = entry.get("response", {}) or {}
        method = request.get("method", "")
        url = request.get("url", "")
        status = response.get("status", "")

        if method:
            analysis.methods[method] += 1
        if status != "":
            analysis.status_codes[str(status)] += 1
        if url:
            analysis.urls.append(url)
            domain = urllib.parse.urlparse(url).netloc.lower()
            if domain:
                analysis.domains[domain] += 1
            add_signals(analysis, url, f"HAR request #{idx} URL")

        add_signals(analysis, normalize_text(request.get("headers")), f"HAR request #{idx} headers")
        add_signals(analysis, normalize_text(request.get("queryString")), f"HAR request #{idx} query")
        add_signals(analysis, normalize_text(request.get("postData")), f"HAR request #{idx} body")
        add_signals(analysis, normalize_text(response.get("headers")), f"HAR response #{idx} headers")

        content = (response.get("content") or {})
        mime = str(content.get("mimeType", ""))
        text = content.get("text", "")
        # HAR 里视频/图片二进制常常很大且无意义，只分析文本类响应。
        if any(x in mime.lower() for x in ["json", "text", "javascript", "xml", "html"]):
            add_signals(analysis, normalize_text(text), f"HAR response #{idx} content")
        else:
            add_signals(analysis, mime, f"HAR response #{idx} mime")

    return analysis


def parse_probe_flows(path: Path) -> Analysis:
    """Parse JSONL records emitted by miniapp_probe_mitm_addon.py."""
    analysis = Analysis()
    with path.open("r", encoding="utf-8") as fh:
        for idx, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                analysis.evidence.append(
                    Evidence("parse_error", f"flows line #{idx}", line[:180], 0)
                )
                continue

            request = record.get("request", {}) or {}
            response = record.get("response", {}) or {}
            method = request.get("method", "")
            url = request.get("url", "")
            status = response.get("status_code", "")

            if method:
                analysis.methods[method] += 1
            if status != "":
                analysis.status_codes[str(status)] += 1
            if url:
                analysis.urls.append(url)
                domain = urllib.parse.urlparse(url).netloc.lower()
                if domain:
                    analysis.domains[domain] += 1
                add_signals(analysis, url, f"flow #{idx} URL")

            add_signals(analysis, normalize_text(request.get("headers")), f"flow #{idx} request headers")
            add_signals(analysis, normalize_text(request.get("query")), f"flow #{idx} query")
            add_signals(analysis, normalize_text(request.get("body_text")), f"flow #{idx} request body")
            add_signals(analysis, normalize_text(response.get("headers")), f"flow #{idx} response headers")
            add_signals(analysis, normalize_text(response.get("body_text")), f"flow #{idx} response body")

    return analysis


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_url(url: str, timeout: int = 12, max_body: int = 200_000) -> Analysis:
    analysis = Analysis()
    current_url = url
    opener = urllib.request.build_opener(NoRedirectHandler)

    for hop in range(1, 8):
        req = urllib.request.Request(current_url, headers={"User-Agent": USER_AGENT})
        analysis.urls.append(current_url)
        add_signals(analysis, current_url, f"URL hop #{hop}")
        domain = urllib.parse.urlparse(current_url).netloc.lower()
        if domain:
            analysis.domains[domain] += 1

        try:
            with opener.open(req, timeout=timeout) as resp:
                status = getattr(resp, "status", "")
                analysis.status_codes[str(status)] += 1
                headers = dict(resp.headers.items())
                add_signals(analysis, normalize_text(headers), f"URL hop #{hop} headers")
                body = resp.read(max_body)
                content_type = headers.get("Content-Type", "")
                if any(x in content_type.lower() for x in ["text", "html", "json", "javascript", "xml"]):
                    add_signals(analysis, body.decode("utf-8", errors="ignore"), f"URL hop #{hop} body")
                break
        except urllib.error.HTTPError as exc:
            analysis.status_codes[str(exc.code)] += 1
            headers = dict(exc.headers.items())
            add_signals(analysis, normalize_text(headers), f"URL hop #{hop} redirect/error headers")
            if exc.code in {301, 302, 303, 307, 308}:
                location = exc.headers.get("Location")
                if not location:
                    break
                current_url = urllib.parse.urljoin(current_url, location)
                continue
            try:
                body = exc.read(max_body)
                add_signals(analysis, body.decode("utf-8", errors="ignore"), f"URL hop #{hop} error body")
            except Exception:
                pass
            break
        except Exception as exc:
            analysis.evidence.append(
                Evidence("fetch_error", f"URL hop #{hop}", repr(exc), 0)
            )
            break
    return analysis


def merge_analysis(items: Iterable[Analysis]) -> Analysis:
    merged = Analysis()
    for item in items:
        merged.scores.update(item.scores)
        merged.evidence.extend(item.evidence)
        merged.domains.update(item.domains)
        merged.methods.update(item.methods)
        merged.status_codes.update(item.status_codes)
        merged.platform_hits.update(item.platform_hits)
        merged.urls.extend(item.urls)
    return merged


def confidence(score: int) -> str:
    if score >= 14:
        return "高"
    if score >= 7:
        return "中"
    if score > 0:
        return "低"
    return "未发现"


def infer_pipeline(analysis: Analysis) -> list[str]:
    s = analysis.scores
    steps = []
    if s["platform_metadata"] > 0:
        steps.append("先解析平台标题、作者、描述、笔记正文等原始元数据")
    if s["media_download"] > 0:
        steps.append("获取视频/音频资源，可能在服务端下载或转码")
    if s["asr"] > 0:
        steps.append("调用 ASR 对音频转写，生成逐字稿或字幕")
    if s["ocr"] > 0:
        steps.append("抽帧后做 OCR，识别画面/硬字幕/PPT文字")
    if s["llm_rewrite"] > 0:
        steps.append("用大模型或规则做摘要、去口癖、改写和文案整理")
    if s["task_queue"] > 0:
        steps.append("通过任务 ID/轮询进度异步处理长视频")
    if s["export"] > 0:
        steps.append("生成 TXT/SRT/VTT/DOCX/Markdown 等导出文件")
    return steps


def print_report(analysis: Analysis) -> None:
    print("\n=== 小程序文案提取实现方式检测报告 ===\n")

    if analysis.platform_hits:
        print("疑似涉及平台：")
        for name, count in analysis.platform_hits.most_common():
            print(f"  - {name}: {count} 条线索")
        print()

    print("能力/链路判断：")
    for key, config in SIGNALS.items():
        score = int(analysis.scores.get(key, 0))
        print(f"  - {config['label']}: {confidence(score)}（score={score}）")
    print()

    steps = infer_pipeline(analysis)
    if steps:
        print("推测处理流程：")
        for idx, step in enumerate(steps, start=1):
            print(f"  {idx}. {step}")
    else:
        print("推测处理流程：线索不足。建议输入一次完整提取流程的 HAR 日志再分析。")
    print()

    if analysis.domains:
        print("高频域名 Top 15：")
        for domain, count in analysis.domains.most_common(15):
            print(f"  - {domain}: {count}")
        print()

    if analysis.methods:
        print("HTTP 方法：", ", ".join(f"{k}={v}" for k, v in analysis.methods.most_common()))
    if analysis.status_codes:
        print("HTTP 状态码：", ", ".join(f"{k}={v}" for k, v in analysis.status_codes.most_common()))
    if analysis.methods or analysis.status_codes:
        print()

    grouped: dict[str, list[Evidence]] = defaultdict(list)
    for ev in analysis.evidence:
        grouped[ev.category].append(ev)

    print("关键证据摘录：")
    any_evidence = False
    for key, config in SIGNALS.items():
        rows = grouped.get(key, [])
        if not rows:
            continue
        any_evidence = True
        print(f"\n  [{config['label']}]")
        for ev in rows[:5]:
            print(f"  - 来源：{ev.source}")
            print(f"    片段：{ev.snippet[:180]}")
    if not any_evidence:
        print("  暂无明显证据。")

    print(
        textwrap.dedent(
            """

            结论使用建议：
              - score 是启发式判断，不是证明；真正架构仍需你自己的服务端日志、授权接口文档或开发者后台确认。
              - 如果只有分享链接，通常只能看出平台/跳转线索；想判断 ASR/OCR/大模型，需要分析一次完整提取过程的 HAR。
              - 请只检测你拥有或已获授权测试的小程序与内容。
            """
        ).rstrip()
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="检测文案提取类微信小程序可能的实现链路（HAR/URL 黑盒分析）。"
    )
    parser.add_argument("--har", type=Path, help="从调试工具导出的 HAR 文件路径")
    parser.add_argument("--flows", type=Path, help="miniapp_probe_mitm_addon.py 记录的 JSONL 流量文件")
    parser.add_argument("--url", help="公开分享链接或平台视频链接，只做普通跳转/页面线索检测")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON 摘要")
    args = parser.parse_args(argv)

    analyses: list[Analysis] = []
    if args.har:
        if not args.har.exists():
            print(f"找不到 HAR 文件：{args.har}", file=sys.stderr)
            return 2
        analyses.append(parse_har(args.har))
    if args.flows:
        if not args.flows.exists():
            print(f"找不到 flows 文件：{args.flows}", file=sys.stderr)
            return 2
        analyses.append(parse_probe_flows(args.flows))
    if args.url:
        analyses.append(fetch_url(args.url))

    if not analyses:
        parser.print_help()
        return 2

    analysis = merge_analysis(analyses)
    if args.json:
        payload = {
            "scores": dict(analysis.scores),
            "confidence": {
                key: confidence(int(analysis.scores.get(key, 0))) for key in SIGNALS
            },
            "platform_hits": dict(analysis.platform_hits),
            "top_domains": analysis.domains.most_common(15),
            "inferred_pipeline": infer_pipeline(analysis),
            "evidence": [
                {
                    "category": ev.category,
                    "source": ev.source,
                    "snippet": ev.snippet,
                    "score": ev.score,
                }
                for ev in analysis.evidence[:80]
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_report(analysis)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
