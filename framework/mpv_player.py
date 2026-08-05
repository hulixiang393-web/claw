"""mpv 独立窗口播放器（mpv_player.py）。

用 subprocess 调系统 mpv.exe 播放 dash 双流（视频轨 + 音频轨），
避免依赖 libmpv dll / python-mpv 内嵌。mpv 需在 PATH 或指定路径。

用法：
    player = MpvPlayer()
    player.play(video_url, audio_url="", title="视频标题")
    player.stop()
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import List, Optional


def _find_mpv() -> str:
    """定位 mpv 可执行文件（PATH 或常见安装路径）。"""
    path = shutil.which("mpv") or shutil.which("mpv.exe")
    if path:
        return path
    candidates = [
        r"C:\Program Files\MPV Player\mpv.exe",
        r"C:\Program Files (x86)\MPV Player\mpv.exe",
    ]
    for c in candidates:
        import os

        if os.path.isfile(c):
            return c
    return "mpv"  # 兜底交给系统找


class MpvPlayer:
    """封装 mpv.exe 独立窗口播放。

    - play() 用视频轨 + 可选音频轨（B 站 dash 音视频分离）。
    - 每次 play 先停掉上一次的实例（同一视频切换分P时）。
    - 独立 mpv 进程，不阻塞主线程。
    """

    UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    def __init__(self, mpv_path: str | None = None, referer: str = ""):
        self._mpv = mpv_path or _find_mpv()
        self._referer = referer or ""
        self._proc: Optional[subprocess.Popen] = None

    # ------------------------------------------------------------------ #
    def play(self, video_url: str, audio_url: str = "", title: str = "") -> None:
        """启动 mpv 播放。video_url 必填，audio_url 可选（dash 音频轨）。"""
        if not video_url:
            return
        self.stop()  # 先停旧实例
        cmd = self._build_cmd(video_url, audio_url, title)
        try:
            # mpv 独立窗口：不捕获输出避免阻塞；用 DETACHED 风格由系统管理
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                shell=False,
            )
        except Exception:
            self._proc = None

    def _build_cmd(self, video_url: str, audio_url: str, title: str) -> List[str]:
        cmd = [self._mpv, "--no-config", "--keep-open=yes", "--force-window=yes"]
        # Referer 头：源配置传入时附带（如 B 站 CDN 需要）
        if self._referer:
            cmd.append(f"--http-header-fields=Referer: {self._referer}")
        cmd.append(f"--user-agent={self.UA}")
        if title:
            cmd.append(f"--title={title}")
        if audio_url:
            # dash 音视频分离：--audio-file 让音频轨同步播放
            cmd.append(f"--audio-file={audio_url}")
        cmd.append(video_url)
        return cmd

    # ------------------------------------------------------------------ #
    def stop(self) -> None:
        """停止当前 mpv 进程（若无则无操作）。

        terminate 后等待短暂时间，若 mpv 未退出（--keep-open 窗口可能忽略
        SIGTERM），再 kill 强制结束。避免旧 mpv 残留导致下次 play 失败。
        """
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:  # 仍在运行
                proc.terminate()
                # 等最多 ~1.5s，未退则强杀
                for _ in range(15):
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        except Exception:
            pass

    def is_playing(self) -> bool:
        return self._proc is not None and self._proc.poll() is None
