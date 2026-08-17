#!/usr/bin/env python3
"""
讯飞实时语音转写大模型：本地视频/音频或抖音链接边流式识别边输出文案。

需要先配置环境变量：
  export XFYUN_APP_ID="你的 appId"
  export XFYUN_ACCESS_KEY_ID="你的 accessKeyId"
  export XFYUN_ACCESS_KEY_SECRET="你的 accessKeySecret"

示例：
  python3 tools/xfyun_stream_to_text.py /path/to/video.mp4
  python3 tools/xfyun_stream_to_text.py "https://v.douyin.com/xxxx/" --cookies-from-browser chrome

输出：
  output/<任务>/result_plain.txt
  output/<任务>/result_timestamped.txt

说明：
  - 讯飞实时转写要求 16k、16bit、单声道 PCM；
  - 本脚本用 ffmpeg 把媒体实时转成 PCM，再按 40ms/1280 bytes 发送给讯飞；
  - 对抖音链接，仍需要先通过 yt-dlp 获取可读取的媒体资源；若抖音要求 Cookie，
    请使用 --cookies-from-browser chrome/safari。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, quote_plus

import websocket

from douyin_to_text import (
    download_with_ytdlp,
    extract_first_url,
    ffmpeg_command,
    is_url,
    prepare_local_media,
    safe_name,
)


XFYUN_HOST = "rtasr.xfyun.cn"
XFYUN_PATH = "/v1/ws"
XFYUN_URL = f"wss://{XFYUN_HOST}{XFYUN_PATH}"
CHUNK_SIZE = 1280
CHUNK_SECONDS = 0.04


@dataclass
class ResultItem:
    start: float | None
    end: float | None
    text: str


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量：{name}")
    return value


def build_xfyun_url(
    app_id: str,
    access_key_id: str,
    access_key_secret: str,
    *,
    language: str,
    accent: str,
    domain: str,
    role_type: int,
    language_type: int,
) -> str:
    # 讯飞实时语音转写大模型官方鉴权：
    # 对除 signa 外所有请求参数按 key 排序拼接，使用 accessKeySecret 做 HMAC-SHA1，
    # 再 base64 后作为 signa。
    params: dict[str, str | int] = {
        "appId": app_id,
        "accessKeyId": access_key_id,
        "utc": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "uuid": str(uuid.uuid4()),
        "lang": language,
        "languageType": language_type,
        "accent": accent,
        "domain": domain,
        "audio_encode": "pcm_s16le",
        "samplerate": 16000,
        "roleType": role_type,
    }
    base = urlencode(sorted(params.items()), quote_via=quote_plus)
    digest = hmac.new(
        access_key_secret.encode("utf-8"),
        base.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    params["signa"] = base64.b64encode(digest).decode("utf-8")
    return f"{XFYUN_URL}?{urlencode(params, quote_via=quote_plus)}"


def start_ffmpeg_pcm(media_path: Path) -> subprocess.Popen:
    ffmpeg = ffmpeg_command()
    return subprocess.Popen(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(media_path),
            "-vn",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def parse_result_message(message: str) -> tuple[str, float | None, float | None] | None:
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        return None

    if payload.get("code") not in (None, 0, "0"):
        print(f"\n讯飞返回错误：{payload}", file=sys.stderr)
        return None

    data = payload.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            pass

    text_parts: list[str] = []
    start = None
    end = None

    def walk(value):
        nonlocal start, end
        if isinstance(value, dict):
            for k in ("bg", "start", "start_time"):
                if k in value and start is None:
                    try:
                        start = float(value[k]) / (1000 if float(value[k]) > 1000 else 1)
                    except Exception:
                        pass
            for k in ("ed", "end", "end_time"):
                if k in value and end is None:
                    try:
                        end = float(value[k]) / (1000 if float(value[k]) > 1000 else 1)
                    except Exception:
                        pass
            # 常见结果结构里 cw/w/word/text 字段可能承载文本。
            for key in ("w", "word", "text"):
                val = value.get(key)
                if isinstance(val, str) and val.strip():
                    text_parts.append(val.strip())
            for val in value.values():
                walk(val)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data if data is not None else payload)
    text = "".join(text_parts).strip()
    if not text:
        return None
    return text, start, end


def receive_loop(ws: websocket.WebSocket, results: list[ResultItem], stop_event: threading.Event) -> None:
    seen: set[str] = set()
    while not stop_event.is_set():
        try:
            message = ws.recv()
        except Exception:
            break
        if not message:
            continue
        parsed = parse_result_message(message)
        if not parsed:
            continue
        text, start, end = parsed
        # 避免中间结果重复刷屏；不同接口版本字段略有差异，做保守去重。
        key = f"{start}-{end}-{text}"
        if key in seen:
            continue
        seen.add(key)
        item = ResultItem(start=start, end=end, text=text)
        results.append(item)
        if start is not None and end is not None:
            print(f"[{start:.2f} - {end:.2f}] {text}", flush=True)
        else:
            print(text, flush=True)


def stream_to_xfyun(media_path: Path, args: argparse.Namespace) -> list[ResultItem]:
    app_id = require_env("XFYUN_APP_ID")
    access_key_id = require_env("XFYUN_ACCESS_KEY_ID")
    access_key_secret = require_env("XFYUN_ACCESS_KEY_SECRET")

    url = build_xfyun_url(
        app_id,
        access_key_id,
        access_key_secret,
        language=args.language,
        accent=args.accent,
        domain=args.domain,
        role_type=args.role_type,
        language_type=args.language_type,
    )

    print("正在连接讯飞实时语音转写大模型...")
    ws = websocket.create_connection(url, timeout=20)
    results: list[ResultItem] = []
    stop_event = threading.Event()
    receiver = threading.Thread(target=receive_loop, args=(ws, results, stop_event), daemon=True)
    receiver.start()

    proc = start_ffmpeg_pcm(media_path)
    assert proc.stdout is not None

    print("开始流式发送音频并接收识别结果...")
    sent_chunks = 0
    try:
        while True:
            chunk = proc.stdout.read(CHUNK_SIZE)
            if not chunk:
                break
            ws.send_binary(chunk)
            sent_chunks += 1
            time.sleep(CHUNK_SECONDS)
        ws.send(json.dumps({"end": True}))
        time.sleep(args.final_wait)
    finally:
        stop_event.set()
        try:
            ws.close()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass

    print(f"已发送音频块：{sent_chunks}")
    return results


def write_outputs(results: list[ResultItem], output_dir: Path) -> None:
    plain = output_dir / "result_plain.txt"
    timestamped = output_dir / "result_timestamped.txt"

    merged_text = "\n".join(item.text for item in results if item.text.strip())
    plain.write_text(merged_text + ("\n" if merged_text else ""), encoding="utf-8")

    lines = []
    for item in results:
        if item.start is not None and item.end is not None:
            lines.append(f"[{item.start:.2f} - {item.end:.2f}] {item.text}")
        else:
            lines.append(item.text)
    timestamped.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    print("\n已生成：")
    print(f"  - 纯文案：{plain}")
    print(f"  - 带时间戳：{timestamped}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用讯飞实时语音转写大模型边流式边识别。")
    parser.add_argument("source", help="抖音分享链接/分享口令，或本地视频/音频文件路径")
    parser.add_argument("--output-dir", default=None, help="输出目录；默认 output/xfyun_<输入摘要>")
    parser.add_argument("--cookies-from-browser", default=None, help="例如 chrome、safari、firefox、edge")
    parser.add_argument("--cookies", default=None, help="Netscape cookies.txt 文件")
    parser.add_argument("--prefer-audio", action="store_true", help="抖音链接下载时优先抽取音频")
    parser.add_argument("--language", default="cn", help="语言，默认 cn")
    parser.add_argument("--accent", default="mandarin", help="方言/口音，默认 mandarin")
    parser.add_argument("--domain", default="slm", help="识别领域，实时转写大模型默认 slm")
    parser.add_argument("--language-type", type=int, default=1, help="语言类型，默认 1")
    parser.add_argument("--role-type", type=int, default=0, help="是否角色分离，默认 0")
    parser.add_argument("--final-wait", type=float, default=3.0, help="发送结束后等待最终结果秒数")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    source = args.source.strip()
    if not source:
        print("输入不能为空。", file=sys.stderr)
        return 2

    extracted_url = extract_first_url(source)
    if extracted_url and not is_url(source):
        print(f"已从分享文本中提取链接：{extracted_url}")
        source = extracted_url

    output_dir = (
        Path(args.output_dir).expanduser()
        if args.output_dir
        else Path("output") / f"xfyun_{safe_name(source)}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        if is_url(source):
            print("输入类型：链接")
            media_path = download_with_ytdlp(
                source,
                output_dir,
                args.prefer_audio,
                args.cookies_from_browser,
                args.cookies,
            )
        else:
            print("输入类型：本地媒体文件")
            media_path = prepare_local_media(source, output_dir)

        print(f"媒体文件：{media_path}")
        results = stream_to_xfyun(media_path, args)
        if not results:
            raise RuntimeError("没有收到有效识别结果。请检查讯飞服务权限、参数和音频内容。")
        write_outputs(results, output_dir)
    except subprocess.CalledProcessError as exc:
        print(f"\n命令执行失败，退出码：{exc.returncode}", file=sys.stderr)
        return exc.returncode or 1
    except RuntimeError as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
