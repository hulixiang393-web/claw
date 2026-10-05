from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit


class TranscodeError(RuntimeError):
    pass


class TranscodeSession:
    def __init__(
        self,
        ffmpeg_path: Optional[str] = None,
        temp_root: Optional[str | Path] = None,
        startup_timeout: float = 10.0,
    ):
        self._ffmpeg_path = ffmpeg_path or "ffmpeg"
        self._temp_root = Path(temp_root) if temp_root is not None else None
        self._startup_timeout = startup_timeout
        self._process = None
        self._directory: Optional[Path] = None
        self._playlist_path: Optional[Path] = None
        self._lock = threading.RLock()
        self._diagnostics = {
            "state": "idle",
            "startup_time": None,
            "error": None,
        }

    @property
    def playlist_path(self) -> Optional[Path]:
        with self._lock:
            return self._playlist_path

    @property
    def diagnostics(self) -> dict:
        with self._lock:
            return dict(self._diagnostics)

    def start(self, source_url: str, headers: dict, public_base: str) -> str:
        with self._lock:
            if self._process is not None:
                raise TranscodeError("transcode session is already running")
            self._reset_diagnostics()
            directory = Path(tempfile.mkdtemp(dir=self._temp_root))
            playlist_path = directory / "index.m3u8"
            segment_pattern = directory / "segment%05d.ts"
            log_path = directory / "ffmpeg.log"
            header_value = "".join(
                f"{name}: {value}\r\n" for name, value in headers.items() if value is not None
            )
            command = [
                self._ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-headers",
                header_value,
                "-i",
                source_url,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-tune",
                "zerolatency",
                "-c:a",
                "aac",
                "-f",
                "hls",
                "-hls_time",
                "2",
                "-hls_list_size",
                "0",
                "-hls_flags",
                "independent_segments",
                "-hls_segment_filename",
                str(segment_pattern),
                str(playlist_path),
            ]
            log_file = log_path.open("w", encoding="utf-8")
            process = None
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log_file,
                    cwd=str(directory),
                )
            except Exception as exc:
                log_file.close()
                shutil.rmtree(directory, ignore_errors=True)
                self._diagnostics["state"] = "failed"
                self._diagnostics["error"] = "failed to start ffmpeg"
                raise TranscodeError("failed to start ffmpeg") from exc
            finally:
                if process is not None:
                    log_file.close()
            self._process = process
            self._directory = directory
            self._playlist_path = playlist_path
            self._diagnostics["state"] = "starting"

        started_at = time.monotonic()
        try:
            while time.monotonic() - started_at < self._startup_timeout:
                with self._lock:
                    process = self._process
                    ready = playlist_path.is_file() and self._has_segment(directory)
                    if ready:
                        self._diagnostics["state"] = "running"
                        self._diagnostics["startup_time"] = round(time.monotonic() - started_at, 3)
                        return self._public_playlist_url(public_base)
                    if process is None or process.poll() is not None:
                        raise TranscodeError("ffmpeg exited before HLS output was ready")
                time.sleep(0.02)
            raise TranscodeError("ffmpeg startup timed out")
        except TranscodeError as exc:
            with self._lock:
                self._diagnostics["state"] = "failed"
                self._diagnostics["error"] = str(exc)
            self.stop()
            raise

    def stop(self) -> None:
        with self._lock:
            process = self._process
            directory = self._directory
            if process is None and directory is None:
                if self._diagnostics["state"] != "failed":
                    self._diagnostics["state"] = "stopped"
                return
            self._process = None
            self._directory = None
            self._playlist_path = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            except OSError:
                pass
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        with self._lock:
            self._diagnostics["state"] = "stopped"

    def _reset_diagnostics(self) -> None:
        self._diagnostics.clear()
        self._diagnostics.update({"state": "idle", "startup_time": None, "error": None})

    @staticmethod
    def _has_segment(directory: Path) -> bool:
        return any(directory.glob("segment*.ts"))

    @staticmethod
    def _public_playlist_url(public_base: str) -> str:
        parts = urlsplit(public_base)
        path = parts.path.rstrip("/") + "/index.m3u8"
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
