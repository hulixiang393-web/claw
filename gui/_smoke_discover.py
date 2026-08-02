"""发现界面离屏测试（mock HTTP 注入假数据）。"""
import os
import sys
import pathlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

from framework.config import SourceConfig
from framework.source_manager import SourceManager
from framework.http import HttpClient
from framework.parser import Parser
from framework.selfcheck import StructureChecker
from framework.discovery import Discovery
from framework.events import EventBus
from framework.theme_manager import ThemeManager
from framework.settings_manager import SettingsManager
from gui.pages.discover_page import DiscoverPage

CATEGORY_HTML = """<html><body><div class="site-header">d</div>
<div class="category-list">
  <a class="cat" href="/cat/fantasy">玄幻</a>
  <a class="cat" href="/cat/martial">武侠</a>
</div></body></html>"""

WORKS_HTML = """<html><body><div class="site-header">d</div>
<div class="book-list">
  <div class="book-item"><a class="title" href="/book/1">书一</a><img class="cover" src="/c/1.jpg"><span class="update">更新</span></div>
  <div class="book-item"><a class="title" href="/book/2">书二</a><img class="cover" src="/c/2.jpg"><span class="update">更新</span></div>
</div></body></html>"""

SRC = {
    "$schema_version": 2,
    "$id": "demo",
    "$type": "novel",
    "$name": "演示站",
    "transports": {"base_url": "http://example.com"},
    "endpoints": {
        "discovery": {
            "list_url": "/category",
            "list_item": {
                "root_selector": {"css": ".category-list a"},
                "fields": {"title": {"css": "a"}, "url": {"css": "a", "attr": "href"}},
            },
        },
        "search": {
            "base_url": "/search",
            "item": {
                "root_selector": {"css": ".book-item"},
                "fields": {
                    "title": {"css": "a.title"},
                    "url": {"css": "a.title", "attr": "href"},
                    "cover": {"css": "img.cover", "attr": "src"},
                },
            },
        },
    },
}


class MockHttp(HttpClient):
    def get_text(self, url, headers=None, proxy=None, timeout=10, retries=3, interval_ms=0):
        if "/category" in url:
            return CATEGORY_HTML
        if "/book" in url or "/search" in url:
            return WORKS_HTML
        return ""


def main():
    app = QApplication.instance() or QApplication([])
    import tempfile
    base = pathlib.Path(tempfile.mkdtemp())

    sm = SettingsManager(base / "ac.json")
    tm = ThemeManager(sm)
    bus = EventBus()

    mgr = SourceManager()
    mgr.add(SourceConfig.from_dict(SRC))

    http = MockHttp(sleeper=lambda _: None)
    parser = Parser()
    checker = StructureChecker(http, parser, default_strategy="off")
    discovery = Discovery(http, parser, checker)

    page = DiscoverPage(mgr, discovery, bus, tm)
    page.refresh()
    app.processEvents()

    # 源选择器只列 discoverable
    assert page.source_combo.count() == 1, page.source_combo.count()
    print("源选择器:", page.source_combo.currentText())

    # 分类按钮（2 分类 + 全部）
    cat_buttons = []
    for i in range(page.cat_bar.count()):
        w = page.cat_bar.itemAt(i).widget()
        if isinstance(w, type(page.bulk_btn)) and w.text() not in ("抓取全部 · 开发中",):
            cat_buttons.append(w)
    print("分类按钮:", [b.text() for b in cat_buttons])
    assert any(b.text() == "玄幻" for b in cat_buttons)

    # 加载作品
    page._reset_works()
    app.processEvents()
    cards = [page.list_layout.itemAt(i).widget() for i in range(page.list_layout.count())]
    cards = [c for c in cards if c is not None]
    print("作品卡片数:", len(cards))
    assert len(cards) == 2, len(cards)

    # 详情占位
    page._on_work_clicked(cards[0].work)
    print("详情占位提示:", page.status_label.text())

    print("\n=== 发现界面离屏测试通过 ===")


if __name__ == "__main__":
    main()
