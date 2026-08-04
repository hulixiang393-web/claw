"""ffmpeg dash 合并器（ffmpeg_merger.py）。

用系统 ffmpeg 把 dash 音视频双流（B 站分离的音视频轨）实时拉流合并成单 mp4。

- 优先系统 PATH 找 ffmpeg；找不到则回退常见安装路径
- 视频轨 + 音频轨（可选）→ 单 .mp4（视频流 copy、音频 aac）
- 支持 -t 截取秒数（测试用）；正常下载不设限
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional


def _find_ffmpeg() -> str:
    """定位 ffmpeg 可执行文件（PATH 或常见安装路径）。"""
    path = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if path:
        return path
    # WinGet 装 Gyan.FFmpeg 的常见位置
    import glob
    import os

    if os.name == "nt":
        patterns = [
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\*ffmpeg*\*\ffmpeg.exe"),
        ]
        for p in patterns:
            for c in glob.glob(p):
                if os.path.isfile(c):
                    return c
    return "ffmpeg"  # 兜底交给系统找


class FFmpegMerger:
    """封装 ffmpeg 将 dash 双流合并为 mp4。

    merge(video_url, audio_url, output) → 真实落盘的 mp4 路径。
    """

    UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    def __init__(self, ffmpeg_path: Optional[str] = None, referer: str = ""):
        self._ffmpeg = ffmpeg_path or _find_ffmpeg()
        self._referer = referer or "https://www.bilibili.com/"

    def merge(self, video_url: str, audio_url: str, out_path: str | Path) -> Path:
        """合并 dash 双流为 mp4。out_path 为输出目标。返回实际文件 Path。

        视频热爱（copy 不重编）+ 音频转 aac（保持兼容）。仅视频轨可用时只存视频。
        """
        import os
        import subprocess

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        is_remote = video_url.startswith(("http://", "https://"))
        headers = f"Referer: {self._referer}\r\nUser-Agent: {self.UA}\r\n"

        # 远程 URL 才加 -headers（本地文件路径加了会报 Option headers not found）
        cmd = [self._ffmpeg, "-y"]
        if is_remote:
            cmd += ["-headers", headers]
        cmd += ["-i", video_url]
        if audio_url:
            if is_remote:
                cmd += ["-headers", headers]
            cmd += ["-i", audio_url]
        cmd += ["-c:v", "copy", "-c:a", "aac", "-shortest", str(out_path)]
        if not audio_url:
            # 仅视频轨：去掉音频编码参数和 -shortest
            cmd = [self._ffmpeg, "-y"]
            if is_remote:
                cmd += ["-headers", headers]
            cmd += ["-i", video_url, "-c:v", "copy", "-an", str(out_path)]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=1800,  # 大视频可能久，30 分钟上限
                text=True,
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            # 超时可能已写入部分文件，清理
            if out_path.exists():
                try:
                    out_path.unlink()
                except OSError:
                    pass
            raise RuntimeError("ffmpeg 合并超时")
        except Exception as exc:
            raise RuntimeError(f"ffmpeg 启动失败：{exc}") from exc

        if proc.returncode != 0:
            err = (proc.stdout + proc.stderr)[-500:]
            if out_path.exists():
                try:
                    out_path.unlink()
                except OSError:
                    pass
            raise RuntimeError(f"ffmpeg 合并失败（{proc.returncode}）：{err}")
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError("ffmpeg 合并无输出文件")
        return out_path