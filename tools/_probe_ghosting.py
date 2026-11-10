# -*- coding: utf-8 -*-
"""「重影」取证探针：判断屏幕上正在播的画面是**单帧**还是**混帧**。

背景
----
用户报「最近播放都是重影」。这个词落到像素上有两种完全不同的原因，修法也
完全不同：

1. **片源/文件本身混帧**（错帧率转换、blend 去隔行）：每一帧都是相邻两帧
   的平均值。文件侧就能测出来 —— 抽帧做「时间混合测试」即可（片源的锅，
   换线路/换清晰度才管用）。
2. **显示链路混帧**（GPU 合成、hwdec、远程串流、合成器缩放）：文件干净，
   但屏幕上出现的那一帧是两帧的混合。只有把**屏幕实际像素**和文件解码帧
   对齐比对才能判定。

本探针干的是第 2 种：对着正在播放的 mpv 窗口抓屏，从当前播放位置前后
抽若干解码帧，找到最佳匹配帧，然后看抓到的画更像是「那一帧」还是「相邻
两帧的平均」。判据：

    d_self  = |截屏 - 最佳帧|
    d_blend = |截屏 - (前一帧 + 后一帧) / 2|
    d_nb    = min(|截屏 - 前一帧|, |截屏 - 后一帧|)

    运动足够大（相邻帧差 > MOTION_MIN）且 d_blend < d_nb  → 混帧（重影）
    运动足够大且 d_self 明显最小                        → 单帧（正常）
    运动太小（静止镜头）                              → 该样本无判定力

用法
----
    python tools/_probe_ghosting.py                 # 自动找 mpv 窗口与 IPC
    python tools/_probe_ghosting.py --samples 20 --interval 1.0
    python tools/_probe_ghosting.py --pipe \\\\.\\pipe\\claw-mpv-xxxx

结论只看结尾那一行 ``结论：…``。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

FRAME_W, FRAME_H = 240, 135      # 比对用的工作分辨率（够判混合，又够快）
SEARCH_BEFORE = 1.0              # 抓屏时刻之前多久开始找匹配帧（秒）
SEARCH_AFTER = 2.0               # 抓到屏之后再多找一会儿（抓屏本身有延迟）
MOTION_MIN = 1.5                 # 相邻帧差小于这个值 = 静止镜头，无判定力
MATCH_MAX = 6.0                  # 最佳匹配帧差大于这个值 = 对齐失败，作废


# --------------------------------------------------------------------------- #
# Win32 窗口定位 / 抓屏
# --------------------------------------------------------------------------- #
def _window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd),
                                                  ctypes.byref(pid))
    return int(pid.value)


def _window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def find_player_window(name: str = "mpv") -> int:
    """按进程名找播放器窗口：优先前台窗口，否则取面积最大的可见窗口。"""
    pids = set()
    for entry in os.popen("tasklist /fo csv /nh").read().splitlines():
        parts = [p.strip('"') for p in entry.split('","')]
        if parts and parts[0].lower().startswith(name.lower()):
            try:
                pids.add(int(parts[1]))
            except (IndexError, ValueError):
                continue
    if not pids:
        return 0
    foreground = ctypes.windll.user32.GetForegroundWindow()
    if foreground and _window_pid(foreground) in pids:
        left, top, right, bottom = _window_rect(foreground)
        if right > left and bottom > top:
            return int(foreground)
    hits: list[tuple[int, int]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):  # noqa: ANN001
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        if _window_pid(hwnd) not in pids:
            return True
        left, top, right, bottom = _window_rect(hwnd)
        area = max(0, right - left) * max(0, bottom - top)
        if area > 0:
            hits.append((area, hwnd))
        return True

    ctypes.windll.user32.EnumWindows(_enum, 0)
    return max(hits)[1] if hits else 0


def grab_window(hwnd: int):
    """抓窗口当前显示的内容（BitBlt 屏幕区域 = 用户真正看到的像素）。"""
    from PIL import Image
    left, top, right, bottom = _window_rect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise RuntimeError("播放器窗口尺寸异常")
    gdi32, user32 = ctypes.windll.gdi32, ctypes.windll.user32
    hdc_screen = user32.GetDC(None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbitmap = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
    gdi32.SelectObject(hdc_mem, hbitmap)
    gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_screen, left, top, 0x00CC0020)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = width
    header.biHeight = -height        # 负数 = 自上而下
    header.biPlanes = 1
    header.biBitCount = 32
    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(hdc_mem, hbitmap, 0, height, buffer,
                    ctypes.byref(header), 0)
    gdi32.DeleteObject(hbitmap)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(None, hdc_screen)
    return Image.frombuffer("RGBA", (width, height), buffer.raw,
                            "raw", "BGRA", 0, 1).convert("L")


# --------------------------------------------------------------------------- #
# mpv JSON IPC
# --------------------------------------------------------------------------- #
class Ipc:
    def __init__(self, path: str) -> None:
        self.path = path
        self._handle = open(path, "r+b", buffering=0)
        self._rid = 0

    def command(self, *args):
        self._rid += 1
        rid = self._rid
        payload = json.dumps({"command": list(args), "request_id": rid})
        self._handle.write((payload + "\n").encode("utf-8"))
        buffer = b""
        while True:
            while b"\n" not in buffer:
                buffer += self._handle.read(65536)
            line, buffer = buffer.split(b"\n", 1)
            if not line.strip():
                continue
            message = json.loads(line.decode("utf-8"))
            if message.get("request_id") == rid:
                return message.get("data")

    def close(self) -> None:
        try:
            self._handle.close()
        except OSError:
            pass


def auto_pipe() -> str:
    try:
        names = os.listdir(r"\\.\pipe")
    except OSError:
        return ""
    for name in names:
        if name.startswith("claw-mpv-"):
            return "\\\\.\\pipe\\" + name
    return ""


# --------------------------------------------------------------------------- #
# 比对
# --------------------------------------------------------------------------- #
def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    for candidate in (r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
                      r"D:\code\claw\bin\ffmpeg.exe"):
        if Path(candidate).is_file():
            return candidate
    raise RuntimeError("找不到 ffmpeg")


def decode_frames(movie: str, start: float, duration: float):
    import numpy as np
    width, height = FRAME_W, FRAME_H
    cmd = [_ffmpeg(), "-v", "error", "-ss", f"{max(0.0, start):.2f}",
           "-t", f"{duration:.2f}", "-i", movie, "-an",
           "-vf", f"scale={width}:{height}:flags=bilinear,format=gray",
           "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    count = len(raw) // (width * height)
    if count <= 0:
        return np.zeros((0, height, width), dtype="float32")
    return np.frombuffer(raw[:count * width * height],
                         dtype="uint8").reshape(count, height, width).astype(
        "float32")


def judge(capture, frames):
    """返回 (判定, 细节 dict)。判定 ∈ {clean, blend, inconclusive}。"""
    import numpy as np
    distances = np.array([float(np.mean(np.abs(capture - f))) for f in frames])
    best = int(distances.argmin())
    detail = {"d_self": float(distances[best]), "best_index": best}
    if detail["d_self"] > MATCH_MAX:
        return "inconclusive", detail
    if best == 0 or best >= len(frames) - 1:
        return "inconclusive", detail
    previous, following = frames[best - 1], frames[best + 1]
    motion = float((np.mean(np.abs(frames[best] - previous))
                    + np.mean(np.abs(frames[best] - following))) / 2.0)
    detail["motion"] = motion
    if motion < MOTION_MIN:
        return "inconclusive", detail
    detail["d_blend"] = float(np.mean(np.abs(capture - (previous + following) / 2)))
    detail["d_prev"] = float(np.mean(np.abs(capture - previous)))
    detail["d_next"] = float(np.mean(np.abs(capture - following)))
    if detail["d_blend"] < min(detail["d_prev"], detail["d_next"]):
        return "blend", detail
    return "clean", detail


def sample(ipc: Ipc, hwnd: int):
    position = ipc.command("get_property", "time-pos")
    if not isinstance(position, (int, float)):
        return None
    if ipc.command("get_property", "pause"):
        return "paused", {"position": float(position)}
    image = grab_window(hwnd)
    later = ipc.command("get_property", "time-pos")
    later = later if isinstance(later, (int, float)) else position + 0.2
    path = ipc.command("get_property", "path") or ""
    movie = path[8:] if str(path).startswith("file:///") else str(path)
    movie = movie.replace("/", os.sep) if movie[:3].replace("/", "") else movie
    if not Path(movie).is_file():
        return None
    import numpy as np
    capture = np.asarray(image.resize((FRAME_W, FRAME_H)), dtype="float32")
    frames = decode_frames(movie, position - SEARCH_BEFORE,
                           (later - position) + SEARCH_BEFORE + SEARCH_AFTER)
    if len(frames) < 3:
        return None
    verdict, detail = judge(capture, frames)
    if verdict == "inconclusive" and detail["d_self"] > MATCH_MAX:
        # 窗口抓偏了（mpv 有多个辅助窗口）：退回整屏抓，再判一次
        from PIL import ImageGrab
        screen = ImageGrab.grab().convert("L")
        capture = np.asarray(screen.resize((FRAME_W, FRAME_H)), dtype="float32")
        verdict, detail = judge(capture, frames)
    detail["position"] = float(position)
    return verdict, detail


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="mpv 画面「重影」取证探针")
    parser.add_argument("--pipe", default="", help="mpv IPC 管道（默认自动找）")
    parser.add_argument("--samples", type=int, default=12, help="采样次数")
    parser.add_argument("--interval", type=float, default=1.2, help="采样间隔秒")
    parser.add_argument("--player", default="mpv", help="播放器进程名")
    args = parser.parse_args(argv)

    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        print(f"缺少依赖：{exc}（需要 numpy 与 pillow）")
        return 2

    pipe = args.pipe or auto_pipe()
    if not pipe:
        print("没找到 mpv 的 IPC 管道：先在本程序里开播，再跑本探针")
        return 2
    hwnd = find_player_window(args.player)
    if not hwnd:
        print(f"没找到 {args.player} 窗口")
        return 2
    print(f"IPC: {pipe}\n窗口: 0x{hwnd:X}  采样: {args.samples} 次 / "
          f"{args.interval}s")

    ipc = Ipc(pipe)
    results: list[str] = []
    try:
        for index in range(args.samples):
            outcome = sample(ipc, hwnd)
            if outcome is None:
                print(f"  #{index + 1:02d} 跳过（位置/文件不可用）")
            else:
                verdict, detail = outcome
                if verdict == "paused":
                    print(f"  #{index + 1:02d} 播放器处于暂停（t="
                          f"{detail['position']:.1f}s）：先让它播起来再测")
                    break
                results.append(verdict)
                extra = " ".join(f"{k}={v:.2f}" for k, v in detail.items()
                                 if isinstance(v, float))
                print(f"  #{index + 1:02d} t={detail['position']:8.2f}s  "
                      f"{verdict:12s} {extra}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("已中断")
    finally:
        ipc.close()

    blends = results.count("blend")
    cleans = results.count("clean")
    print("-" * 60)
    if blends and blends >= max(1, cleans):
        print(f"结论：屏幕上确实是混帧（重影）—— blend {blends} / clean {cleans}；"
              "片源文件是干净的，问题在显示链路（hwdec / GPU 合成 / 远程串流）。")
        return 1
    if cleans:
        print(f"结论：画面是单帧，未复现重影（clean {cleans} / blend {blends}）。"
              "复现时再跑一次；静止镜头样本无判定力。")
        return 0
    print("结论：证据不足（采样全落在静止镜头或对齐失败）。换一段运动画面再跑。")
    return 3


if __name__ == "__main__":
    sys.exit(main())
