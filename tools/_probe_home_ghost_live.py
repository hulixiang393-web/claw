"""首页「历史记录」重影：抓运行中窗口的真实屏幕像素取证（BitBlt）。

PrintWindow 会把半透明层拍丢（背景图模式整片空白），所以这里走屏幕 BitBlt：
先把窗口提到前台，再抓 GetWindowRect 对应的屏幕区域。

输出：
  1. logs/home_window.png —— 原图，人眼可直接看
  2. 逐行墨量带 —— 一行 14px 文字应只产生 ~14px 高的一条带；
     出现 ~24px 或两条相邻带 = 上下重影
  3. 局部 ASCII —— 直接把「最近搜索 / 最近播放」区域摊开看字形是否双层
  4. 水平自相关 —— lag 3~24px 有显著峰 = 同一段字形被复制了一段距离

用法：
  python tools\\_probe_home_ghost_live.py --rows           # 只打文本带
  python tools\\_probe_home_ghost_live.py --ascii-crop 0,480:0,760
"""

from __future__ import annotations

import argparse
import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TITLE = "多源爬虫框架"
SW_RESTORE = 9


def _pid_of(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def find_window(title: str = TITLE) -> int:
    hits: list[tuple[int, int]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lp):
        u = ctypes.windll.user32
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(wintypes.HWND(hwnd))
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(wintypes.HWND(hwnd), buf, n + 1)
        if title not in buf.value:
            return True
        rect = wintypes.RECT()
        u.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
        area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        if area:
            hits.append((area, hwnd))
        return True

    ctypes.windll.user32.EnumWindows(_enum, 0)
    return max(hits)[1] if hits else 0


def capture_screen(hwnd: int):
    """返回 (w, h, BGRA bytes)。抓的是用户真正看到的像素。"""
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    try:
        user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
        user32.SetForegroundWindow(wintypes.HWND(hwnd))
    except OSError:
        pass
    import time

    time.sleep(0.35)

    rect = wintypes.RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    left, top = rect.left, rect.top
    width, height = rect.right - left, rect.bottom - top
    if width <= 0 or height <= 0:
        raise RuntimeError("窗口尺寸异常，可能已最小化")

    class BMIH(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    hdc_screen = user32.GetDC(None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbitmap = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
    gdi32.SelectObject(hdc_mem, hbitmap)
    gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_screen, left, top, 0x00CC0020)

    header = BMIH()
    header.biSize = ctypes.sizeof(BMIH)
    header.biWidth = width
    header.biHeight = -height
    header.biPlanes = 1
    header.biBitCount = 32
    buf = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(hdc_mem, hbitmap, 0, height, buf, ctypes.byref(header), 0)
    gdi32.DeleteObject(hbitmap)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(None, hdc_screen)
    return width, height, buf.raw, (left, top)


def to_lum(width: int, height: int, data: bytes) -> list[list[int]]:
    rows = []
    for y in range(height):
        base = y * width * 4
        row = []
        for x in range(width):
            i = base + x * 4
            row.append((data[i + 2] * 299 + data[i + 1] * 587 + data[i] * 114) // 1000)
        rows.append(row)
    return rows


def band_profile(lum, x0, x1, y0, y1) -> list[int]:
    """每行「偏离该行众数颜色」的像素数：对浅底深字/深底浅字都成立。"""
    prof = []
    for y in range(y0, y1):
        row = lum[y][x0:x1]
        if not row:
            prof.append(0)
            continue
        counts: dict[int, int] = {}
        for v in row:
            b = v // 8
            counts[b] = counts.get(b, 0) + 1
        mode_b = max(counts.items(), key=lambda kv: kv[1])[0]
        prof.append(sum(1 for v in row if abs(v - mode_b * 8) > 40))
    return prof


def ascii_view(lum, x0, x1, y0, y1, step_x=4, step_y=8, invert=False) -> str:
    lines = []
    for y in range(y0, y1, step_y):
        line = []
        for x in range(x0, x1, step_x):
            block = [lum[yy][xx] for yy in range(y, min(y + step_y, y1))
                     for xx in range(x, min(x + step_x, x1))]
            if not block:
                line.append(" ")
                continue
            block.sort()
            med = block[len(block) // 2]
            dark = sum(1 for v in block if abs(v - med) > 45)
            line.append("#" if dark / len(block) > 0.35 else
                        ("+" if dark / len(block) > 0.12 else "."))
        lines.append("".join(line))
    return "\n".join(lines)


def autocorr(prof: list[int], max_lag: int = 24) -> list[tuple[int, float]]:
    mean = sum(prof) / len(prof) if prof else 0
    p = [v - mean for v in prof]
    denom = sum(v * v for v in p) or 1.0
    return [(lag, sum(p[i] * p[i + lag] for i in range(len(p) - lag)) / denom)
            for lag in range(2, max_lag + 1)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default=TITLE)
    ap.add_argument("--ascii-crop", default="", help="x0,x1:y0,y1 局部 ASCII")
    ap.add_argument("--step-x", type=int, default=4)
    ap.add_argument("--step-y", type=int, default=8)
    ap.add_argument("--out", default="logs")
    args = ap.parse_args()

    hwnd = find_window(args.title)
    if not hwnd:
        print(f"没找到标题含「{args.title}」的窗口")
        return 2
    print(f"窗口 hwnd=0x{hwnd:X} pid={_pid_of(hwnd)}")
    w, h, data, origin = capture_screen(hwnd)
    print(f"屏幕抓图 {w}x{h} 左上角={origin}")
    lum = to_lum(w, h, data)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image

        (out / "home_window.png").write_bytes(b"")
        img = Image.frombytes("RGBA", (w, h), data, "raw", "BGRA")
        img.convert("RGB").save(out / "home_window.png", "PNG")
        print(f"已存 {out / 'home_window.png'}")
    except ImportError:
        print("（没装 Pillow，跳过 PNG）")

    prof = band_profile(lum, 0, w, 0, h)
    bands, run = [], 0
    for y, n in enumerate(prof):
        if n > 2:
            run += 1
        elif run:
            bands.append((y - run, run))
            run = 0
    if run:
        bands.append((h - run, run))
    print("\n文本带（y, 高）——单行文字应 ~10~18px，>24 提示两行叠加：")
    for y0_, hh in bands:
        flag = "  ← 偏厚" if hh > 24 else ""
        print(f"  y={y0_:>4}  高={hh:>3}{flag}")

    if args.ascii_crop:
        try:
            xs, ys = args.ascii_crop.split(":")
            x0, x1 = (int(v) for v in xs.split(","))
            y0, y1 = (int(v) for v in ys.split(","))
        except ValueError:
            print("--ascii-crop 格式应为 x0,x1:y0,y1")
            return 2
        print()
        print(ascii_view(lum, x0, x1, y0, y1, args.step_x, args.step_y))
        for y0_, hh in bands:
            if y0_ < y0 or y0_ + hh > y1:
                continue
            peaks = [(lag, r) for lag, r in autocorr(prof_slice(lum, x0, x1, y0_, y0_ + hh))
                     if r > 0.35]
            if peaks:
                print(f"  带 y={y0_}: 自相关峰 {[(l, round(r, 2)) for l, r in peaks[:6]]}")
    return 0


def prof_slice(lum, x0, x1, y0, y1) -> list[int]:
    """该带内每列墨量（水平剖面），用于自相关。"""
    prof = []
    for x in range(x0, x1):
        col = [lum[y][x] for y in range(y0, y1)]
        if not col:
            prof.append(0)
            continue
        med = sorted(col)[len(col) // 2]
        prof.append(sum(1 for v in col if abs(v - med) > 45))
    return prof


if __name__ == "__main__":
    raise SystemExit(main())
