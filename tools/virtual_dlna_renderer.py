from __future__ import annotations

import json
import shutil
import subprocess
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return


def find_ffmpeg(explicit: str | None = None) -> str:
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return str(path)
        raise FileNotFoundError(f"FFmpeg not found: {explicit}")
    path = shutil.which("ffmpeg")
    if path:
        return path
    raise FileNotFoundError("FFmpeg not found on PATH; pass --ffmpeg")


def _ffprobe_path(ffmpeg_path: str) -> str:
    candidate = Path(ffmpeg_path).with_name("ffprobe.exe" if Path(ffmpeg_path).suffix.lower() == ".exe" else "ffprobe")
    return str(candidate) if candidate.is_file() else (shutil.which("ffprobe") or "ffprobe")


def _safe_stderr(stderr: bytes | str, media_url: str | None = None) -> str:
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    if media_url:
        text = text.replace(media_url, "<media-url>")
        parsed = urlsplit(media_url)
        if parsed.query:
            text = text.replace(parsed.query, "<query>")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-3:])[:1000]


def run_ffprobe(ffmpeg_path: str, media_url: str, timeout: float = 10.0) -> dict:
    command = [
        _ffprobe_path(ffmpeg_path),
        "-v",
        "error",
        "-show_entries",
        "format=format_name,duration:stream=codec_type,codec_name",
        "-of",
        "json",
        media_url,
    ]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return {"returncode": None, "stderr_summary": _safe_stderr(exc.stderr or b"", media_url), "timed_out": True}
    result = {"returncode": completed.returncode, "stderr_summary": _safe_stderr(completed.stderr, media_url)}
    if completed.returncode != 0:
        return result
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        result["stderr_summary"] = "ffprobe returned invalid JSON"
        result["returncode"] = 1
        return result
    streams = payload.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    fmt = payload.get("format", {})
    result.update(
        {
            "container": fmt.get("format_name"),
            "duration": float(fmt["duration"]) if fmt.get("duration") else 0.0,
            "video_codec": video.get("codec_name"),
            "audio_codec": audio.get("codec_name"),
        }
    )
    return result


def decode_probe(ffmpeg_path: str, media_url: str, seconds: float = 3.0, timeout: float = 20.0) -> dict:
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-t",
        str(seconds),
        "-i",
        media_url,
        "-map",
        "0:v:0?",
        "-map",
        "0:a:0?",
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return {"decoded": False, "returncode": None, "stderr_summary": _safe_stderr(exc.stderr or b"", media_url), "timed_out": True}
    return {
        "decoded": completed.returncode == 0,
        "returncode": completed.returncode,
        "stderr_summary": _safe_stderr(completed.stderr, media_url),
    }


class VirtualMediaFetcher:
    def __init__(self, ffmpeg_path: str | None, timeout: float = 10.0):
        self.ffmpeg_path = ffmpeg_path
        self.timeout = timeout

    def fetch(self, url: str) -> dict:
        started = time.perf_counter()
        request = Request(url, headers={"User-Agent": "virtual-dlna-renderer/1"})
        with urlopen(request, timeout=self.timeout) as response:
            first = response.read(1)
            first_byte_ms = (time.perf_counter() - started) * 1000
            total = len(first)
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
            return {
                "http_status": response.status,
                "content_type": response.headers.get_content_type(),
                "first_byte_ms": first_byte_ms,
                "bytes": total,
            }

    def decode(self, url: str) -> dict:
        if not self.ffmpeg_path:
            raise FileNotFoundError("FFmpeg path is required for decode")
        http = self.fetch(url)
        probe = run_ffprobe(self.ffmpeg_path, url, self.timeout)
        decoded = decode_probe(self.ffmpeg_path, url, timeout=max(20.0, self.timeout))
        result = {**http, **probe, **decoded}
        return result


def start_media_server(root: Path) -> tuple[ThreadingHTTPServer, str]:
    directory = str(root.resolve())

    class Handler(_QuietHandler):
        def __init__(self, *args: Any, **kwargs: Any):
            super().__init__(*args, directory=directory, **kwargs)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"
