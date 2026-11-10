"""首页「最近播放」重影：真实页面栈复现（离线，不弹窗口）。

栈：QMainWindow 中央区 _CentralArea（paintEvent 画背景图）→ QTabWidget
→ HomePage → _PlaybackList（QListWidget + setItemWidget）。

两组：
  nobg   只有 BASE_QSS
  bg     BASE_QSS + _bg_overlay_qss() + 中央区背景图

对每组渲染整个中央区，裁出「最近播放」列表矩形，打印 ASCII，
并按行统计墨量、按列统计墨量分布，判两份文本是否错位叠加。

用法：
  python tools\\_probe_home_ghost.py
  python tools\\_probe_home_ghost.py --mode nobg|bg --ascii
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QRect, QSize, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow, QTabWidget, QVBoxLayout  # noqa: E402

from framework.events import EventBus  # noqa: E402
from framework.search_history import SearchHistory  # noqa: E402
from framework.source_manager import SourceManager  # noqa: E402
from framework.theme_manager import ThemeManager  # noqa: E402
from gui.app import _CentralArea  # noqa: E402
from gui.pages.home_page import HomePage  # noqa: E402

RECORDS = [
    ("苍兰诀2 影三界篇", "第03集", 754, "https://example.test/a"),
    ("葬送的芙莉莲", "第07集", 306, "https://example.test/b"),
    ("进击的巨人", "第21集", 62, "https://example.test/c"),
]


class _StubSettings:
    def __init__(self, theme: str = "sakura"):
        self._theme = theme
        self._data = {}

    def get(self, section, key, default=None):
        if (section, key) == ("ui", "theme"):
            return self._theme
        return self._data.get((section, key), default)

    def set(self, section, key, value):
        self._data[(section, key)] = value

    def save(self):
        return None


class _StubProgress:
    """只提供首页要用的两个方法，记录内容固定。"""

    def recent_videos(self, privacy=None):
        return [
            {
                "book_url": url,
                "title": title,
                "chapter_title": chapter,
                "location": {"seconds": secs},
                "updated_at": 1_700_000_000 + i,
            }
            for i, (title, chapter, secs, url) in enumerate(RECORDS)
        ]

    def all(self):
        return {}

    def remove(self, book_url):
        return False

    def clear_videos(self):
        pass


class _StubPrivacy:
    def playback_history_hidden(self, record):
        return False


def _make_bg(width: int, height: int) -> QPixmap:
    """模拟「主题 bg 色 + 用户图 + 0.6 透明」的合成结果：浅底 + 斜纹。"""
    img = QImage(width, height, QImage.Format_RGB32)
    img.fill(QColor("#FFF6F9"))
    for y in range(height):
        for x in range(0, width, 37):
            xx = (x + y // 3) % width
            img.setPixelColor(xx, y, QColor("#FFD9E6"))
    return QPixmap.fromImage(img)


def build(mode: str, app) -> tuple[QMainWindow, HomePage, QRect]:
    settings = _StubSettings()
    tm = ThemeManager(settings)
    bus = EventBus()
    manager = SourceManager()
    history = SearchHistory(Path("data/_probe_search_history.json"), defer_cleanup=True)
    history.clear()
    for kw in ("凡人修仙传", "咒术回战", "葬送的芙莉莲"):
        history.push(kw)

    page = HomePage(
        manager, bus, tm, settings, history,
        reading_progress=_StubProgress(), privacy=_StubPrivacy(),
    )

    win = QMainWindow()
    win.resize(1100, 760)
    central = _CentralArea()
    central.setObjectName("centralArea")
    win.setCentralWidget(central)
    lay = QVBoxLayout(central)
    lay.setContentsMargins(0, 0, 0, 0)
    tabs = QTabWidget()
    tabs.setDocumentMode(True)
    lay.addWidget(tabs)
    tabs.addTab(page, "首页")

    base = tm.generate_qss(tm.current_key())
    qss = base
    if mode == "bg":
        from gui.app import MainWindow

        stub = type("StubApp", (), {"theme_manager": tm})()
        qss = base + "\n" + MainWindow._bg_overlay_qss(stub)
        central.set_bg_pixmap(_make_bg(1100, 760))
    app.setStyleSheet(qss)

    win.setAttribute(Qt.WA_DontShowOnScreen, True)
    win.show()
    app.processEvents()
    page.refresh()
    app.processEvents()

    lst = page.playback_list
    top_left = lst.mapTo(central, lst.rect().topLeft())
    rect = QRect(top_left, lst.size())
    return win, page, rect


def _lum(c: int) -> int:
    return (((c >> 16) & 255) * 299 + ((c >> 8) & 255) * 587 + (c & 255) * 114) // 1000


def ascii_crop(img: QImage, rect: QRect, cols: int = 120) -> str:
    sub = img.copy(rect)
    scale_x = max(1, sub.width() // cols)
    scale_y = max(1, scale_x * 2)
    lines = []
    for y0 in range(0, sub.height(), scale_y):
        line = []
        for x0 in range(0, sub.width(), scale_x):
            dark = 0
            n = 0
            for y in range(y0, min(y0 + scale_y, sub.height())):
                for x in range(x0, min(x0 + scale_x, sub.width())):
                    n += 1
                    if _lum(sub.pixel(x, y) & 0xFFFFFF) < 120:
                        dark += 1
            line.append("#" if n and dark / n > 0.30 else ("+" if dark else "."))
        lines.append("".join(line))
    return "\n".join(lines)


def ink_rows(img: QImage, rect: QRect, threshold: int = 120) -> list[int]:
    sub = img.copy(rect)
    out = []
    for y in range(sub.height()):
        n = 0
        for x in range(sub.width()):
            if _lum(sub.pixel(x, y) & 0xFFFFFF) < threshold:
                n += 1
        out.append(n)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["nobg", "bg", "both"], default="both")
    ap.add_argument("--ascii", action="store_true")
    args = ap.parse_args()

    app = QApplication.instance() or QApplication([])
    modes = ["nobg", "bg"] if args.mode == "both" else [args.mode]
    lines_buf = []
    for mode in modes:
        win, page, rect = build(mode, app)
        app.processEvents()
        img = win.grab().toImage().convertToFormat(QImage.Format_RGB32)
        rows = ink_rows(img, rect)
        bands = []
        run = 0
        for y, n in enumerate(rows):
            if n > 0:
                run += 1
            elif run:
                bands.append((y - run, run))
                run = 0
        if run:
            bands.append((len(rows) - run, run))
        print(f"[{mode}] 列表区域 {rect.width()}x{rect.height()}  文本带（y偏移, 高度）= {bands}")
        if args.ascii:
            print(ascii_crop(img, rect))
        lines_buf.append((mode, rows))
        win.close()
        win.deleteLater()
        app.processEvents()
        Path("data/_probe_search_history.json").unlink(missing_ok=True)

    if len(lines_buf) == 2:
        a = lines_buf[0][1]
        b = lines_buf[1][1]
        diff = sum(abs(x - y) for x, y in zip(a, b))
        print(f"\n两模式行墨量绝对差合计 = {diff}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
