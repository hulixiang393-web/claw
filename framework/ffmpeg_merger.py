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


class MergeCancelled(RuntimeError):
    """合并被用户取消（点「取消」按钮时抛出）。"""


class MergePaused(RuntimeError):
    """合并被用户暂停（点「暂停」按钮时抛出）。合并已终止，可恢复后重下。"""


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
        # referer 由调用方从源配置传入（如 transports.headers.Referer）；默认空
        self._referer = referer or ""

    def merge(
        self,
        video_url: str,
        audio_url: str,
        out_path: str | Path,
        progress_cb=None,
        cancel_evt=None,
        pause_evt=None,
    ) -> Path:
        """合并 dash 双流为 mp4。out_path 为输出目标。返回实际文件 Path。

        progress_cb: 可选回调 (bytes_written: int) → None，每 0.5s 报告
                     已输出文件大小，供下载 UI 显示合并进度。
        cancel_evt:  可选 threading.Event；设置时立即终止 ffmpeg（用户点取消）。
                     返回时抛 MergeCancelled。
        pause_evt:   可选 threading.Event；设置时立即终止 ffmpeg（用户点暂停）。
                     返回时抛 MergePaused。

        视频热爱（copy 不重编）+ 音频转 aac（保持兼容）。仅视频轨可用时只存视频。
        """
        import os
        import subprocess

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        is_remote = video_url.startswith(("http://", "https://"))
        # Referer 头：有 referer 才附带（无则只带 UA）
        _h = f"Referer: {self._referer}\r\nUser-Agent: {self.UA}\r\n" if self._referer \
            else f"User-Agent: {self.UA}\r\n"
        headers = _h

        # 远程 URL 才加 -headers（本地文件路径加了会报 Option headers not found）
        cmd = [self._ffmpeg, "-y"]
        # 允许 https/http 协议：本地 m3u8（广告过滤重写）引用的段/AES-key 是远程 https，
        # ffmpeg 默认白名单 file,crypto,data 不含网络协议，会拒绝 → 显式放行。
        if not is_remote:
            cmd += ["-protocol_whitelist", "file,crypto,data,http,https,tcp,tls"]
        if is_remote:
            cmd += ["-headers", headers]
            # 多连接拉流：HLS/DASH 分段并发下载（等价于源配置 media.hls.workers 意图），
            # CDN 支持时从单连接串行拉段提速为多连接并行，下载速率显著提升。
            # 置于 -i 前作为输入选项；ffmpeg 8 http_multiple 默认 auto，显式置 1 兜底。
            cmd += ["-http_multiple", "1", "-http_persistent", "1"]
        cmd += ["-i", video_url]
        if audio_url:
            if is_remote:
                cmd += ["-headers", headers, "-http_multiple", "1", "-http_persistent", "1"]
            cmd += ["-i", audio_url]
        cmd += ["-c:v", "copy", "-c:a", "aac", "-shortest", str(out_path)]
        if not audio_url:
            # 仅视频轨：去掉音频编码参数和 -shortest
            cmd = [self._ffmpeg, "-y"]
            if not is_remote:
                cmd += ["-protocol_whitelist", "file,crypto,data,http,https,tcp,tls"]
            if is_remote:
                cmd += ["-headers", headers, "-http_multiple", "1", "-http_persistent", "1"]
            cmd += ["-i", video_url, "-c:v", "copy", "-an", str(out_path)]

        try:
            # capture_output=True 在 Windows 有管道缓冲死锁：ffmpeg stderr 持续输出
            # 超过管道缓冲（~64KB）后阻塞写，父进程 communicate() 也在等 → 死锁。
            # 改用临时文件重定向 stdout/stderr，彻底消除该问题（视频越大越安全）。
            import tempfile
            import threading

            with tempfile.NamedTemporaryFile(
                mode="w+", suffix=".log", delete=False
            ) as tf:
                log_path = tf.name

            # 用 Popen + 轮询输出文件大小，实时上报合并进度（否则 UI 全程 0）
            out_path = Path(out_path)
            stop = threading.Event()

            def _check_cancel():
                if cancel_evt is not None and cancel_evt.is_set():
                    stop.set()
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    raise MergeCancelled("合并已取消")
                if pause_evt is not None and pause_evt.is_set():
                    stop.set()
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    raise MergePaused("合并已暂停")

            def _monitor_progress(proc):
                """后台线程：每 0.3s 报告已输出文件大小 + 检查取消。"""
                try:
                    while proc.poll() is None and not stop.is_set():
                        _check_cancel()
                        if progress_cb is not None:
                            try:
                                size = (
                                    out_path.stat().st_size
                                    if out_path.exists() else 0
                                )
                                progress_cb(size)
                            except Exception:
                                pass
                        import time as _t
                        _t.sleep(0.3)
                    # 结束前补最后一口
                    if progress_cb is not None and proc.poll() is None:
                        try:
                            progress_cb(out_path.stat().st_size if out_path.exists() else 0)
                        except Exception:
                            pass
                except MergeCancelled:
                    return
                except MergePaused:
                    return
                except Exception:
                    pass

            proc = subprocess.Popen(
                cmd,
                stdout=open(log_path, "w"),
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
            monitor = threading.Thread(
                target=_monitor_progress, args=(proc,), daemon=True
            )
            monitor.start()
            try:
                # 轮询等待：取消/暂停时立即终止，不阻塞到 ffmpeg 结束
                import time as _t
                while proc.poll() is None:
                    if cancel_evt is not None and cancel_evt.is_set():
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        stop.set()
                        raise MergeCancelled("合并已取消")
                    if pause_evt is not None and pause_evt.is_set():
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        stop.set()
                        raise MergePaused("合并已暂停")
                    _t.sleep(0.2)
                # 进程已结束，等待清理
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            except MergeCancelled:
                if out_path.exists():
                    try:
                        out_path.unlink()
                    except OSError:
                        pass
                raise
            except MergePaused:
                if out_path.exists():
                    try:
                        out_path.unlink()
                    except OSError:
                        pass
                raise
            except subprocess.TimeoutExpired:
                stop.set()
                try:
                    proc.kill()
                except Exception:
                    pass
                if out_path.exists():
                    try:
                        out_path.unlink()
                    except OSError:
                        pass
                raise RuntimeError("ffmpeg 合并超时")
            finally:
                stop.set()
                monitor.join(timeout=2)

            # 读取合并日志（错误诊断用）
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    run_log = f.read()
            except OSError:
                run_log = ""
            finally:
                try:
                    import os

                    os.unlink(log_path)
                except OSError:
                    pass
        except MergeCancelled:
            raise  # 用户取消：透传给调用方（download_queue 标记 CANCELED）
        except MergePaused:
            raise  # 用户暂停：透传给调用方（download_queue 标记 PAUSED）
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
        finally:
            # 统一清理：任意路径（取消/暂停/超时/成功/异常）都不泄漏临时 log
            try:
                import os

                os.unlink(log_path)
            except OSError:
                pass

        if proc.returncode != 0:
            err = run_log[-500:]
            if out_path.exists():
                try:
                    out_path.unlink()
                except OSError:
                    pass
            raise RuntimeError(f"ffmpeg 合并失败（{proc.returncode}）：{err}")
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError("ffmpeg 合并无输出文件")
        return out_path