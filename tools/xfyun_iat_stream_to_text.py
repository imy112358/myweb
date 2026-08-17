#!/usr/bin/env python3
"""
讯飞语音听写（流式版）WebSocket：抖音链接/本地视频边上传边识别。

适配你在讯飞控制台看到的三件套：
  XFYUN_APP_ID
  XFYUN_API_KEY
  XFYUN_API_SECRET

配置方式：
  export XFYUN_APP_ID="你的 APPID"
  export XFYUN_API_KEY="你的 APIKey"
  export XFYUN_API_SECRET="你的 APISecret"

运行：
  ./extract_text_iat.sh "https://v.douyin.com/xxxx/" --cookies-from-browser chrome
  ./extract_text_iat.sh "/path/to/video.mp4"

说明：
  - IAT 单次 WebSocket 会话通常限制 60 秒音频；
  - 本脚本按约 55 秒自动切分为多个会话，适合短视频/中等长度视频；
  - 如果你开通的是“实时语音转写大模型”，也可以继续使用 extract_text_xfyun.sh。
"""

from __future__ import annotations

import argparse
import base64
import email.utils
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import websocket

from douyin_to_text import (
    download_with_ytdlp,
    extract_first_url,
    ffmpeg_command,
    is_url,
    prepare_local_media,
    safe_name,
)


IAT_HOST = "iat-api.xfyun.cn"
IAT_PATH = "/v2/iat"
IAT_URL = f"wss://{IAT_HOST}{IAT_PATH}"

# 16k * 16bit * mono = 32000 bytes/sec.
# 1280 bytes ≈ 40ms，符合讯飞流式发送常见建议。
CHUNK_SIZE = 1280
CHUNK_SECONDS = 0.04
SESSION_SECONDS = 55
SESSION_BYTES = int(32000 * SESSION_SECONDS)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量：{name}")
    return value


def build_auth_url(api_key: str, api_secret: str) -> str:
    date = email.utils.formatdate(usegmt=True)
    signature_origin = f"host: {IAT_HOST}\ndate: {date}\nGET {IAT_PATH} HTTP/1.1"
    signature_sha = hmac.new(
        api_secret.encode("utf-8"),
        signature_origin.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    signature = base64.b64encode(signature_sha).decode("utf-8")
    authorization_origin = (
        f'api_key="{api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(authorization_origin.encode("utf-8")).decode("utf-8")
    query = urlencode({"authorization": authorization, "date": date, "host": IAT_HOST})
    return f"{IAT_URL}?{query}"


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


def send_frame(ws: websocket.WebSocket, app_id: str, audio: bytes, status: int, args: argparse.Namespace) -> None:
    frame: dict[str, object] = {
        "data": {
            "status": status,
            "format": "audio/L16;rate=16000",
            "encoding": "raw",
            "audio": base64.b64encode(audio).decode("utf-8"),
        }
    }
    if status == 0:
        frame["common"] = {"app_id": app_id}
        frame["business"] = {
            "language": args.language,
            "domain": args.domain,
            "accent": args.accent,
            "vad_eos": args.vad_eos,
            "ptt": args.ptt,
        }
        if args.dwa:
            frame["business"]["dwa"] = args.dwa
    ws.send(json.dumps(frame, ensure_ascii=False))


def extract_text_from_iat_result(payload: dict) -> str:
    try:
        ws_items = payload["data"]["result"]["ws"]
    except Exception:
        return ""
    parts: list[str] = []
    for item in ws_items:
        for cw in item.get("cw", []):
            word = cw.get("w", "")
            if word:
                parts.append(word)
    return "".join(parts)


def parse_iat_message(message: str) -> tuple[int | None, str, str | None, list[int] | None]:
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        return None, "", None, None
    code = payload.get("code", 0)
    if code != 0:
        raise RuntimeError(f"讯飞返回错误：code={code}, message={payload.get('message')}")

    result = ((payload.get("data") or {}).get("result") or {})
    sn = result.get("sn")
    try:
        sn_int = int(sn) if sn is not None else None
    except Exception:
        sn_int = None

    pgs = result.get("pgs")
    rg = result.get("rg")
    if isinstance(rg, list) and len(rg) == 2:
        try:
            rg = [int(rg[0]), int(rg[1])]
        except Exception:
            rg = None
    else:
        rg = None

    return sn_int, extract_text_from_iat_result(payload), pgs, rg


def run_iat_session(chunks: list[bytes], app_id: str, api_key: str, api_secret: str, args: argparse.Namespace) -> str:
    url = build_auth_url(api_key, api_secret)
    ws = websocket.create_connection(url, timeout=20)
    collected_by_sn: dict[int, str] = {}
    try:
        for idx, chunk in enumerate(chunks):
            status = 0 if idx == 0 else 1
            send_frame(ws, app_id, chunk, status, args)
            time.sleep(args.chunk_interval)
            # 尽量读取当前返回，避免阻塞太久。
            ws.settimeout(0.01)
            while True:
                try:
                    msg = ws.recv()
                except Exception:
                    break
                apply_iat_message(msg, collected_by_sn, live=args.live)

        send_frame(ws, app_id, b"", 2, args)
        ws.settimeout(args.final_wait)
        deadline = time.time() + args.final_wait
        while time.time() < deadline:
            try:
                msg = ws.recv()
            except Exception:
                break
            apply_iat_message(msg, collected_by_sn, live=args.live)
    finally:
        try:
            ws.close()
        except Exception:
            pass
    final_text = "".join(text for _, text in sorted(collected_by_sn.items()) if text)
    if final_text and not args.live:
        print(final_text, end="", flush=True)
    return final_text


def apply_iat_message(message: str, collected_by_sn: dict[int, str], live: bool) -> None:
    sn, text, pgs, rg = parse_iat_message(message)
    if sn is None or not text:
        return

    # 讯飞动态修正 dwa=wpgs 时，pgs=rpl 表示要替换 rg 范围内的旧片段。
    # 之前直接追加所有中间结果，就会出现用户截图里的大量重复。
    if pgs == "rpl" and rg:
        start, end = rg
        for old_sn in range(start, end + 1):
            collected_by_sn.pop(old_sn, None)

    is_new = sn not in collected_by_sn
    collected_by_sn[sn] = text
    if live and is_new:
        print(text, end="", flush=True)


def stream_media_to_iat(media_path: Path, args: argparse.Namespace) -> str:
    app_id = require_env("XFYUN_APP_ID")
    api_key = require_env("XFYUN_API_KEY")
    api_secret = require_env("XFYUN_API_SECRET")

    proc = start_ffmpeg_pcm(media_path)
    assert proc.stdout is not None

    all_text: list[str] = []
    session_chunks: list[bytes] = []
    session_bytes = 0
    session_no = 1

    print("开始转码并流式识别：")
    try:
        while True:
            chunk = proc.stdout.read(CHUNK_SIZE)
            if not chunk:
                break
            session_chunks.append(chunk)
            session_bytes += len(chunk)
            if session_bytes >= SESSION_BYTES:
                print(f"\n\n[会话 {session_no} / 约 {SESSION_SECONDS}s]\n", flush=True)
                text = run_iat_session(session_chunks, app_id, api_key, api_secret, args)
                all_text.append(text)
                session_chunks = []
                session_bytes = 0
                session_no += 1

        if session_chunks:
            print(f"\n\n[会话 {session_no}]\n", flush=True)
            text = run_iat_session(session_chunks, app_id, api_key, api_secret, args)
            all_text.append(text)
    finally:
        try:
            proc.terminate()
        except Exception:
            pass

    return "\n".join(part for part in all_text if part.strip()).strip()


def write_outputs(text: str, output_dir: Path) -> None:
    plain = output_dir / "result_plain.txt"
    plain.write_text(text + ("\n" if text else ""), encoding="utf-8")
    print(f"\n\n已生成：{plain}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="讯飞语音听写流式版：抖音链接/本地视频转文字。")
    parser.add_argument("source", help="抖音分享链接/分享口令，或本地视频/音频路径")
    parser.add_argument("--output-dir", default=None, help="输出目录；默认 output/iat_<输入摘要>")
    parser.add_argument("--cookies-from-browser", default=None, help="例如 chrome、safari、firefox、edge")
    parser.add_argument("--cookies", default=None, help="Netscape cookies.txt 文件")
    parser.add_argument("--prefer-audio", action="store_true", help="抖音链接下载时优先抽取音频")
    parser.add_argument("--language", default="zh_cn", help="语言，默认 zh_cn")
    parser.add_argument("--domain", default="iat", help="应用领域，默认 iat")
    parser.add_argument("--accent", default="mandarin", help="口音，默认 mandarin")
    parser.add_argument("--vad-eos", type=int, default=10000, help="端点检测静音毫秒，默认 10000")
    parser.add_argument("--dwa", default="", help="动态修正；默认关闭。需要时可设为 wpgs")
    parser.add_argument("--ptt", type=int, default=1, help="标点，默认 1")
    parser.add_argument("--chunk-interval", type=float, default=CHUNK_SECONDS, help="每帧发送间隔秒数")
    parser.add_argument("--final-wait", type=float, default=3.0, help="每个会话结束后等待最终结果秒数")
    parser.add_argument("--live", action="store_true", help="实时打印片段；默认每个会话结束后打印合并结果，避免重复刷屏")
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
        else Path("output") / f"iat_{safe_name(source)}"
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
        text = stream_media_to_iat(media_path, args)
        if not text:
            raise RuntimeError("没有识别到有效文字。")
        write_outputs(text, output_dir)
    except subprocess.CalledProcessError as exc:
        print(f"\n命令执行失败，退出码：{exc.returncode}", file=sys.stderr)
        return exc.returncode or 1
    except RuntimeError as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
