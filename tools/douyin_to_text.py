#!/usr/bin/env python3
"""
抖音/短视频链接或本地视频文件转文字，自用版。

功能：
  - 输入抖音链接：先用 yt-dlp 下载视频/音频；
  - 输入本地视频/音频：直接处理；
  - 用 ffmpeg 转成 16k 单声道 wav；
  - 用 faster-whisper 本地识别中文语音；
  - 输出：
      result_timestamped.txt  带时间戳文本
      result_plain.txt        纯文本
      result.srt              字幕文件

示例：
  python3 tools/douyin_to_text.py "https://v.douyin.com/xxxx/"
  python3 tools/douyin_to_text.py /path/to/video.mp4
  python3 tools/douyin_to_text.py "抖音链接" --model medium --output-dir output/demo
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse


DEFAULT_MODEL = "small"
MEDIA_SUFFIXES = {
    ".mp4",
    ".mov",
    ".m4v",
    ".mkv",
    ".avi",
    ".webm",
    ".flv",
    ".mp3",
    ".m4a",
    ".wav",
    ".aif",
    ".aiff",
    ".aac",
    ".ogg",
}


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str


def is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def extract_first_url(value: str) -> str | None:
    match = re.search(r"https?://[^\s\"'<>，。]+", value)
    if not match:
        return None
    return match.group(0).rstrip(".,;)")


def safe_name(value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    stem = "".join(ch if ch.isalnum() else "_" for ch in value[:32]).strip("_")
    return f"{stem or 'media'}_{digest}"


def require_command(name: str, install_hint: str) -> None:
    if shutil.which(name):
        return
    raise RuntimeError(f"找不到命令：{name}\n安装建议：{install_hint}")


def ytdlp_command() -> list[str]:
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    try:
        import yt_dlp  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("缺少 yt-dlp。\n安装建议：python3 -m pip install --user -U yt-dlp") from exc
    return [sys.executable, "-m", "yt_dlp"]


def ffmpeg_command() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise RuntimeError(
            "找不到 ffmpeg，也缺少 imageio-ffmpeg。\n"
            "安装建议：brew install ffmpeg\n"
            "或：python3 -m pip install --user -U imageio-ffmpeg"
        ) from exc
    return imageio_ffmpeg.get_ffmpeg_exe()


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    printable = " ".join(cmd)
    print(f"\n$ {printable}")
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def download_with_ytdlp(
    url: str,
    output_dir: Path,
    prefer_audio: bool,
    cookies_from_browser: str | None,
    cookies_file: str | None,
) -> Path:
    # 让 yt-dlp 自己决定真实后缀，后续从目录中找到新文件。
    before = {p.resolve() for p in output_dir.glob("*") if p.is_file()}
    template = str(output_dir / "downloaded.%(ext)s")
    base_cmd = ytdlp_command()
    ffmpeg_location = ffmpeg_command()
    auth_args: list[str] = []
    if cookies_from_browser:
        auth_args.extend(["--cookies-from-browser", cookies_from_browser])
    if cookies_file:
        auth_args.extend(["--cookies", cookies_file])

    if prefer_audio:
        cmd = base_cmd + [
            "--no-playlist",
            "--ffmpeg-location",
            ffmpeg_location,
            *auth_args,
            "-x",
            "--audio-format",
            "m4a",
            "-o",
            template,
            url,
        ]
    else:
        cmd = base_cmd + [
            "--no-playlist",
            "--ffmpeg-location",
            ffmpeg_location,
            *auth_args,
            "-f",
            "bv*+ba/best",
            "-o",
            template,
            url,
        ]

    run(cmd)

    after = [p for p in output_dir.glob("*") if p.is_file() and p.resolve() not in before]
    candidates = [p for p in after if p.suffix.lower() in MEDIA_SUFFIXES]
    if not candidates:
        candidates = [p for p in output_dir.glob("downloaded.*") if p.suffix.lower() in MEDIA_SUFFIXES]
    if not candidates:
        raise RuntimeError("yt-dlp 执行完成，但没有找到下载出的媒体文件。")

    return max(candidates, key=lambda p: p.stat().st_size)


def prepare_local_media(source: str, output_dir: Path) -> Path:
    path = Path(source).expanduser()
    if not path.exists():
        raise RuntimeError(f"找不到本地文件：{path}")
    if path.suffix.lower() not in MEDIA_SUFFIXES:
        raise RuntimeError(f"不支持的媒体格式：{path.suffix}")

    target = output_dir / f"input{path.suffix.lower()}"
    if path.resolve() != target.resolve():
        shutil.copy2(path, target)
    return target


def extract_audio(media_path: Path, audio_path: Path) -> None:
    ffmpeg = ffmpeg_command()
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(media_path),
            "-vn",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(audio_path),
        ]
    )


def import_whisper_model():
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "缺少 Python 依赖：faster-whisper\n安装建议：pip3 install -U faster-whisper"
        ) from exc
    return WhisperModel


def transcribe(audio_path: Path, *, model_name: str, device: str, compute_type: str) -> list[TranscriptSegment]:
    WhisperModel = import_whisper_model()
    print(f"\n正在加载模型：{model_name}（device={device}, compute_type={compute_type}）")
    model = WhisperModel(model_name, device=device, compute_type=compute_type)

    print("\n正在识别语音...")
    segments_iter, info = model.transcribe(
        str(audio_path),
        language="zh",
        vad_filter=True,
        beam_size=5,
    )

    print(f"识别语言：{info.language}，置信度：{getattr(info, 'language_probability', 0):.2f}")
    segments: list[TranscriptSegment] = []
    for item in segments_iter:
        text = item.text.strip()
        if not text:
            continue
        segment = TranscriptSegment(start=float(item.start), end=float(item.end), text=text)
        segments.append(segment)
        print(f"[{format_time(segment.start)} - {format_time(segment.end)}] {segment.text}")

    return segments


def format_time(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def format_srt_time(seconds: float) -> str:
    return format_time(seconds).replace(".", ",")


def write_outputs(segments: Iterable[TranscriptSegment], output_dir: Path) -> None:
    segments = list(segments)
    timestamped = output_dir / "result_timestamped.txt"
    plain = output_dir / "result_plain.txt"
    srt = output_dir / "result.srt"

    timestamped.write_text(
        "\n".join(f"[{format_time(s.start)} - {format_time(s.end)}] {s.text}" for s in segments) + "\n",
        encoding="utf-8",
    )
    plain.write_text("\n".join(s.text for s in segments) + "\n", encoding="utf-8")

    srt_blocks = []
    for idx, s in enumerate(segments, start=1):
        srt_blocks.append(
            f"{idx}\n{format_srt_time(s.start)} --> {format_srt_time(s.end)}\n{s.text}\n"
        )
    srt.write_text("\n".join(srt_blocks), encoding="utf-8")

    print("\n已生成：")
    print(f"  - 带时间戳：{timestamped}")
    print(f"  - 纯文本：{plain}")
    print(f"  - 字幕：{srt}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抖音链接/本地视频转文字，自用版。")
    parser.add_argument("source", help="抖音分享链接，或本地视频/音频文件路径")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="输出目录；默认是 output/<输入摘要>",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="Whisper 模型：tiny/base/small/medium/large-v3，默认 small",
    )
    parser.add_argument(
        "--device",
        default="cpu",
        help="运行设备，默认 cpu；有 NVIDIA GPU 可用 cuda",
    )
    parser.add_argument(
        "--compute-type",
        default="int8",
        help="计算精度，CPU 默认 int8；GPU 常用 float16",
    )
    parser.add_argument(
        "--prefer-audio",
        action="store_true",
        help="链接下载时优先只下载音频，通常更省空间；若失败可去掉此参数。",
    )
    parser.add_argument(
        "--cookies-from-browser",
        default=None,
        help=(
            "从本机浏览器读取 Cookie 给 yt-dlp 使用，例如 chrome、safari、firefox、edge。"
            "抖音提示 Fresh cookies are needed 时使用。"
        ),
    )
    parser.add_argument(
        "--cookies",
        default=None,
        help="使用 Netscape cookies.txt 文件，例如从浏览器扩展导出的 cookies.txt。",
    )
    parser.add_argument(
        "--keep-media",
        action="store_true",
        help="保留下载的视频/音频源文件；默认也会保留，当前参数用于语义占位。",
    )
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
        else Path("output") / safe_name(source)
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

        print(f"\n媒体文件：{media_path}")
        audio_path = output_dir / "audio_16k_mono.wav"
        extract_audio(media_path, audio_path)
        segments = transcribe(
            audio_path,
            model_name=args.model,
            device=args.device,
            compute_type=args.compute_type,
        )
        if not segments:
            raise RuntimeError("没有识别到有效语音文本。")
        write_outputs(segments, output_dir)
    except subprocess.CalledProcessError as exc:
        print(f"\n命令执行失败，退出码：{exc.returncode}", file=sys.stderr)
        print(
            "如果看到 Fresh cookies are needed，请先在 Chrome/Safari 打开并登录抖音，"
            "然后重试：\n"
            "  ./extract_text.sh \"抖音链接\" --cookies-from-browser chrome\n"
            "或：\n"
            "  ./extract_text.sh \"抖音链接\" --cookies-from-browser safari\n"
            "如果仍失败，可以手动保存视频，再把本地视频路径传给脚本。",
            file=sys.stderr,
        )
        return exc.returncode or 1
    except RuntimeError as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
