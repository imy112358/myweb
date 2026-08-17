#!/usr/bin/env python3
"""
本地“视频提取文案”图形界面后端。

启动：
  cd /Users/yangmeng/myweb
  python3 tools/transcriber_web_server.py

然后打开：
  http://127.0.0.1:8765/my-site/transcriber.html

安全边界：
  - 只监听 127.0.0.1；
  - 读取本地 .xfyun_iat.env，但不会把密钥返回给前端；
  - 调用现有的 tools/xfyun_iat_stream_to_text.py 完成识别。
"""

from __future__ import annotations

import cgi
import json
import os
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
PORT = int(os.environ.get("TRANSCRIBER_PORT", "8765"))
UPLOAD_DIR = ROOT / "output" / "web_uploads"
JOBS_DIR = ROOT / "output" / "web_jobs"


@dataclass
class Job:
    id: str
    status: str = "queued"
    source_type: str = "link"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    command: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    output_dir: Path | None = None
    result_text: str = ""
    error: str = ""


JOBS: dict[str, Job] = {}
LOCK = threading.Lock()


def load_env_file() -> dict[str, str]:
    env_path = ROOT / ".xfyun_iat.env"
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        try:
            parsed = shlex.split(value)
            values[key.strip()] = parsed[0] if parsed else ""
        except Exception:
            values[key.strip()] = value.strip().strip("\"'")
    return values


def job_to_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "status": job.status,
        "sourceType": job.source_type,
        "createdAt": job.created_at,
        "updatedAt": job.updated_at,
        "logs": job.logs[-240:],
        "resultText": job.result_text,
        "error": job.error,
        "outputDir": str(job.output_dir) if job.output_dir else "",
    }


def append_log(job: Job, line: str) -> None:
    clean = line.rstrip("\n")
    if not clean:
        return
    with LOCK:
        job.logs.append(clean)
        job.updated_at = time.time()


def run_job(job_id: str, source: str, options: dict[str, Any]) -> None:
    with LOCK:
        job = JOBS[job_id]
        job.status = "running"
        job.updated_at = time.time()

    output_dir = JOBS_DIR / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    command = [
        "python3",
        "tools/xfyun_iat_stream_to_text.py",
        source,
        "--output-dir",
        str(output_dir),
    ]

    cookies_from_browser = (options.get("cookiesFromBrowser") or "").strip()
    if cookies_from_browser:
        command.extend(["--cookies-from-browser", cookies_from_browser])

    if options.get("preferAudio"):
        command.append("--prefer-audio")

    if options.get("live"):
        command.append("--live")

    with LOCK:
        job.command = command
        job.output_dir = output_dir

    env = os.environ.copy()
    env.update(load_env_file())
    env["PYTHONUNBUFFERED"] = "1"

    append_log(job, "开始执行提取任务...")
    append_log(job, "提示：首次下载或识别可能会稍慢。")

    try:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        assert process.stdout is not None
        for line in process.stdout:
            append_log(job, line)

        return_code = process.wait()
        result_file = output_dir / "result_plain.txt"
        if result_file.exists():
            result_text = result_file.read_text(encoding="utf-8").strip()
        else:
            result_text = ""

        with LOCK:
            job.result_text = result_text
            job.updated_at = time.time()
            if return_code == 0 and result_text:
                job.status = "completed"
            else:
                job.status = "failed"
                job.error = f"任务失败，退出码：{return_code}"
    except Exception as exc:
        with LOCK:
            job.status = "failed"
            job.error = str(exc)
            job.updated_at = time.time()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/health":
            env_values = load_env_file()
            self.send_json(
                {
                    "ok": True,
                    "hasXfyunConfig": all(
                        env_values.get(key)
                        for key in ("XFYUN_APP_ID", "XFYUN_API_KEY", "XFYUN_API_SECRET")
                    ),
                }
            )
            return

        if path.startswith("/api/jobs/"):
            job_id = unquote(path.rsplit("/", 1)[-1])
            with LOCK:
                job = JOBS.get(job_id)
                payload = job_to_dict(job) if job else None
            if not payload:
                self.send_json({"error": "job not found"}, 404)
                return
            self.send_json(payload)
            return

        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/jobs":
            self.send_json({"error": "not found"}, 404)
            return

        content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", "0") or "0")

        source = ""
        source_type = "link"
        options: dict[str, Any] = {}

        if content_type.startswith("multipart/form-data"):
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                    "CONTENT_LENGTH": str(length),
                },
            )
            source = (form.getfirst("source") or "").strip()
            source_type = form.getfirst("sourceType") or "link"
            options = {
                "cookiesFromBrowser": form.getfirst("cookiesFromBrowser") or "",
                "preferAudio": form.getfirst("preferAudio") == "true",
                "live": form.getfirst("live") == "true",
            }

            upload = form["file"] if "file" in form else None
            if upload is not None and getattr(upload, "filename", ""):
                UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                suffix = Path(upload.filename).suffix or ".mp4"
                upload_path = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
                with upload_path.open("wb") as fh:
                    while True:
                        chunk = upload.file.read(1024 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
                source = str(upload_path)
                source_type = "file"
        else:
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                self.send_json({"error": "invalid json"}, 400)
                return
            source = (payload.get("source") or "").strip()
            source_type = payload.get("sourceType") or "link"
            options = {
                "cookiesFromBrowser": payload.get("cookiesFromBrowser") or "",
                "preferAudio": bool(payload.get("preferAudio")),
                "live": bool(payload.get("live")),
            }

        if not source:
            self.send_json({"error": "请输入抖音链接或上传视频文件"}, 400)
            return

        job_id = uuid.uuid4().hex[:12]
        job = Job(id=job_id, source_type=source_type)
        with LOCK:
            JOBS[job_id] = job

        thread = threading.Thread(target=run_job, args=(job_id, source, options), daemon=True)
        thread.start()
        self.send_json(job_to_dict(job), 201)


def main() -> int:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"视频文案提取界面已启动： http://{HOST}:{PORT}/my-site/transcriber.html")
    print("按 Ctrl+C 停止服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
