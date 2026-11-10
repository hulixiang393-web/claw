"""首页「最近播放」重影探针（离线，不弹窗口）。

复现路径：QListWidget + QListWidgetItem(label) + setItemWidget(_PlaybackRow)。
同一个列表渲染三种变体：

  both      item 带文本 + 行控件（真实代码路径，_refresh_history 的写法）
  row_only  item 文本置空 + 行控件（只有行控件画字）
  item_only item 带文本 + 不挂行控件（只有列表代理画字）

对比两种 QSS：
  plain    只有主题 BASE_QSS（无背景图）
  bgimage  BASE_QSS + _bg_overlay_qss()（背景图模式）

判定：
  * both vs row_only 的差异像素 > 阈值 → 行控件是透明的，
    item 自己的那份文本从底下漏出来 = 重影。
  * 把差异像素画成 ASCII 图，能直接看到两份错位的文字。

用法：
  python tools\\_probe_home_ghost.py
  python tools\\_probe_home_ghost.py --ascii      # 额外打印 ASCII 对照图
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QSize, Qt  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QListWidgetItem  # noqa: E402

from framework.theme_manager import ThemeManager  # noqa: E402
from gui.pages.home_page import (  # noqa: E402
    _PLAYBACK_ROW_HEIGHT,
    _PlaybackList,
    _PlaybackRow,
)

TITLES = [
    "苍兰诀2 影三界篇 · 第03集 · 12:34",
    "葬送的芙莉莲 · 第07集 · 05:06",
    "进击的巨人 · 第21集 · 01:02",
]


class _StubSettings:
    """只读默认值的 SettingsManager 替身（探针不碰真实配置）。"""

    def __init__(self, theme: str = "sakura"):
        self._theme = theme

    def get(self, section, key, default=None):
        if (section, key) == ("ui", "theme"):
            return self._theme
        return default

    def set(self, *a, **k):
        return None

    def save(self):
        return None


def _build_list(app, mode: str) -> _PlaybackList:
    """mode: both / row_only / item_only。"""
    view = _PlaybackList()
    view.setUniformItemSizes(True)
    view.setAttribute(Qt.WA_DontShowOnScreen, True)
    view.resize(520, _PLAYBACK_ROW_HEIGHT * len(TITLES) + 10)
    for text in TITLES:
        item = QListWidgetItem("" if mode == "row_only" else text)
        item.setSizeHint(QSize(0, _PLAYBACK_ROW_HEIGHT))
        view.addItem(item)
        if mode == "item_only":
            continue
        row = _PlaybackRow({"book_url": text, "title": text})
        row.set_label(text)
        view.setItemWidget(item, row)
    view.show()
    app.processEvents()
    view.fit_item_widgets()
    app.processEvents()
    return view


def _grab(app, qss: str, mode: str) -> QImage:
    app.setStyleSheet(qss)
    view = _build_list(app, mode)
    app.processEvents()
    img = view.grab().toImage().convertToFormat(QImage.Format_RGB32)
    view.close()
    view.deleteLater()
    app.processEvents()
    return img


def _diff_mask(a: QImage, b: QImage, threshold: int = 60) -> tuple[list[list[bool]], int]:
    """逐像素差异掩码；threshold 过滤抗锯齿噪声。"""
    h = min(a.height(), b.height())
    w = min(a.width(), b.width())
    mask = [[False] * w for _ in range(h)]
    total = 0
    for y in range(h):
        for x in range(w):
            ca = a.pixel(x, y) & 0xFFFFFF
            cb = b.pixel(x, y) & 0xFFFFFF
            d = (abs(((ca >> 16) & 255) - ((cb >> 16) & 255))
                 + abs(((ca >> 8) & 255) - ((cb >> 8) & 255))
                 + abs((ca & 255) - (cb & 255)))
            if d > threshold:
                mask[y][x] = True
                total += 1
    return mask, total


def _ascii(img: QImage, cols: int = 108) -> str:
    """把图缩成 ASCII：暗像素用 '#'，浅像素用 '.'。"""
    scale_x = max(1, img.width() // cols)
    scale_y = scale_x * 2
    lines = []
    for y0 in range(0, img.height(), scale_y):
        line = []
        for x0 in range(0, img.width(), scale_x):
            dark = 0
            n = 0
            for y in range(y0, min(y0 + scale_y, img.height())):
                for x in range(x0, min(x0 + scale_x, img.width())):
                    c = img.pixel(x, y) & 0xFFFFFF
                    lum = (((c >> 16) & 255) * 299 + ((c >> 8) & 255) * 587
                           + (c & 255) * 114) // 1000
                    n += 1
                    if lum < 140:
                        dark += 1
            line.append("#" if n and dark / n > 0.35 else ("+" if n and dark else "."))
        lines.append("".join(line))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ascii", action="store_true", help="打印 ASCII 对照图")
    args = ap.parse_args()

    app = QApplication.instance() or QApplication([])
    tm = ThemeManager(_StubSettings())
    base = tm.generate_qss(tm.current_key())

    from gui.app import MainWindow

    stub = type("StubApp", (), {"theme_manager": tm})()
    overlay = MainWindow._bg_overlay_qss(stub)

    variants = {"plain": base, "bgimage": base + "\n" + overlay}
    report = {}
    for name, qss in variants.items():
        both = _grab(app, qss, "both")
        row_only = _grab(app, qss, "row_only")
        item_only = _grab(app, qss, "item_only")
        _, leaked = _diff_mask(both, row_only)
        _, item_ink = _diff_mask(item_only, _grab(app, qss, "row_only"))
        report[name] = (leaked, item_ink, both, item_only)
        print(f"[{name}] 行控件下漏出的 item 文本像素 = {leaked}"
              f"   （item 自身墨量参考 = {item_ink}）")

    print()
    for name in ("plain", "bgimage"):
        leaked, item_ink, _, _ = report[name]
        ratio = leaked / item_ink if item_ink else 0.0
        verdict = "有重影" if leaked > 200 else "无重影"
        print(f"{name:<8} 漏出比例 {ratio:.2f}  → {verdict}")

    if args.ascii:
        for name in ("plain", "bgimage"):
            print(f"\n===== {name}：实际渲染（both）=====")
            print(_ascii(report[name][2]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
