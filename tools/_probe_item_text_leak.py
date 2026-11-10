"""决定性实验：item 文本在行控件存在时是否还能被看到（分两种 QSS）。

背景：QListWidgetItem 自己带一份文本（列表代理画），_PlaybackRow 里
QLabel 又画同一份文本。若行控件/标签的背景是不透明色，后画的那份盖住前者；
一旦 QSS 把它们改成透明（背景图模式），两份文本就同时可见 → 重影。

做法：item 文本设为 40 个「█」宽串，行控件标签留空。
   行区域出现墨 → item 文本透出来了（重影源）
   行区域全空     → 被行控件盖住了

用法：python tools\\_probe_item_text_leak.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QPoint, QSize, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication, QListWidgetItem  # noqa: E402

from framework.theme_manager import ThemeManager  # noqa: E402
from gui.pages.home_page import (  # noqa: E402
    _PLAYBACK_ROW_HEIGHT,
    _PlaybackList,
    _PlaybackRow,
)

ITEM_TEXT = "█" * 40


class _Stub:
    def __init__(self, theme="sakura"):
        self._theme = theme

    def get(self, section, key, default=None):
        return self._theme if (section, key) == ("ui", "theme") else default

    def set(self, *a, **k):
        return None

    def save(self):
        return None


def render(app, qss: str, has_row: bool) -> QImage:
    app.setStyleSheet(qss)
    view = _PlaybackList()
    view.setUniformItemSizes(True)
    view.setAttribute(Qt.WA_DontShowOnScreen, True)
    view.resize(520, _PLAYBACK_ROW_HEIGHT + 10)
    item = QListWidgetItem(ITEM_TEXT)
    item.setSizeHint(QSize(0, _PLAYBACK_ROW_HEIGHT))
    view.addItem(item)
    if has_row:
        row = _PlaybackRow({"book_url": "x", "title": "x"})
        row.set_label("")
        view.setItemWidget(item, row)
    view.show()
    app.processEvents()
    view.fit_item_widgets()
    app.processEvents()
    # 关键：背景图模式下列表底色是透明，grab() 得到黑底，会把「底色」算成墨。
    # 这里先铺一张白图再 render 上去，只留下真正画出来的文字像素。
    img = QImage(view.size(), QImage.Format_RGB32)
    img.fill(QColor("#FFFFFF"))
    painter = QPainter(img)
    view.render(painter, QPoint(0, 0))
    painter.end()
    view.close()
    view.deleteLater()
    app.processEvents()
    return img


def dark_pixels(img: QImage, y0: int, y1: int, x0: int = 0, x1: int = 400) -> int:
    n = 0
    for y in range(y0, min(y1, img.height())):
        for x in range(x0, min(x1, img.width())):
            c = img.pixel(x, y) & 0xFFFFFF
            lum = (((c >> 16) & 255) * 299 + ((c >> 8) & 255) * 587 + (c & 255) * 114) // 1000
            if lum < 100:
                n += 1
    return n


def main() -> int:
    app = QApplication.instance() or QApplication([])
    tm = ThemeManager(_Stub())
    base = tm.generate_qss(tm.current_key())
    from gui.app import MainWindow

    stub = type("S", (), {"theme_manager": tm})()
    overlay = MainWindow._bg_overlay_qss(stub)

    cases = {"无背景图": base, "背景图模式": base + "\n" + overlay}
    print(f"item 文本 = 40 x '█'，行控件标签为空\n")
    for name, qss in cases.items():
        plain = render(app, qss, has_row=False)
        withrow = render(app, qss, has_row=True)
        d_plain = dark_pixels(plain, 0, _PLAYBACK_ROW_HEIGHT * 2)
        d_row = dark_pixels(withrow, 0, _PLAYBACK_ROW_HEIGHT * 2)
        print(f"[{name}] 无行控件墨量={d_plain:>6}   有行控件墨量={d_row:>6}")
        if d_plain == 0:
            print("        （无行控件时都没墨量 → 该模式下图根本不可判，跳过）")
        elif d_row > d_plain * 0.25:
            print("        ⇒ item 文本透出来了：两份文本会叠加成重影")
        elif d_row == 0:
            print("        ⇒ item 文本被行控件完全盖住：不会重影")
        else:
            print(f"        ⇒ 部分透出（{d_row / d_plain:.0%}），需看具体像素")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
