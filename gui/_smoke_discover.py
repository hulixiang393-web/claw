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
from framework.content import Content
from framework.bulk_fetch import BulkFetch
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
            "works_list_url": "/search",
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
        "detail": {
            "fields": {
                "title": {"css": "h1"},
                "author": {"css": "meta[property='og:novel:author']", "attr": "content"},
                "status": {"css": ".status"},
                "summary": {"css": ".summary"},
            }
        },
        "content": {
            "chapter": {
                "list": {
                    "root_selector": {"css": ".catalog li a"},
                    "fields": {"title": {"css": "a"}, "url": {"css": "a", "attr": "href"}},
                },
                "body": {"selector": {"css": "#chaptercontent p"}},
            }
        },
    },
}


DETAIL_HTML = """<html><body><div class="site-header">d</div>
<h1>书一</h1>
<meta property="og:novel:author" content="作者甲">
<div class="info"><span class="status">连载中</span></div>
<p class="summary">这是一本书的简介</p>
<div class="catalog"><ul><li><a href="/book/1/1.html">第一章</a></li><li><a href="/book/1/2.html">第二章</a></li></ul></div>
</body></html>"""


class MockHttp(HttpClient):
    def get_text(self, url, headers=None, proxy=None, timeout=10, retries=3, interval_ms=0):
        if "/category" in url:
            return CATEGORY_HTML
        if "/cat/" in url:
            return WORKS_HTML
        if ".html" in url or "/book/1" in url:
            return DETAIL_HTML
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
    content = Content(http, parser, checker)
    bulk = BulkFetch(discovery, event_bus=bus, index_dir=base)

    page = DiscoverPage(mgr, discovery, content, bulk, bus, tm)
    page.refresh()
    app.processEvents()

    # 源选择器只列 discoverable
    assert page.source_combo.count() == 1, page.source_combo.count()
    print("源选择器:", page.source_combo.currentText())

    # 分类按钮（2 分类 + 全部）
    cat_buttons = [btn for btn, _ in page._cat_buttons]
    print("分类按钮:", [b.text() for b in cat_buttons])
    assert any(b.text() == "玄幻" for b in cat_buttons)
    # 折叠状态：默认只显示前 COLLAPSED_COUNT+1 个
    visible = [page.cat_bar.itemAt(i).widget() for i in range(page.cat_bar.count())]
    visible = [w for w in visible if isinstance(w, type(page.bulk_btn))]
    assert len(visible) == min(len(cat_buttons), 7), len(visible)
    print("折叠可见按钮:", [w.text() for w in visible])

    # 展开分类
    page._toggle_categories()
    app.processEvents()
    expanded = [page.cat_bar.itemAt(i).widget() for i in range(page.cat_bar.count())]
    expanded = [w for w in expanded if isinstance(w, type(page.bulk_btn))]
    assert len(expanded) == len(cat_buttons), (len(expanded), len(cat_buttons))
    print("展开后按钮数:", len(expanded))

    # 加载作品
    page._reset_works()
    app.processEvents()
    cards = [page.grid_layout.itemAt(i).widget() for i in range(page.grid_layout.count())]
    cards = [c for c in cards if c is not None]
    print("作品卡片数:", len(cards))
    assert len(cards) == 2, len(cards)

    # 详情抽屉：点作品 → 拉详情 → 抽屉显示
    page._on_work_clicked(cards[0].work)
    app.processEvents()
    assert page.detail_drawer.is_open(), "抽屉应打开"
    print("详情抽屉标题:", page.detail_drawer.title.text())

    # 全量抓取（真实 bulk_fetch 逻辑，MockHttp 返回作品）
    stats = bulk.fetch_all(page._current_source)
    assert stats["works"] > 0, stats
    print("全量抓取:", stats["categories"], "分类", stats["works"], "作品")
    # 索引文件已保存
    idx_file = base / f"works_index_{page._current_source.source_id}.json"
    assert idx_file.exists(), "索引文件应存在"

    print("\n=== 发现界面离屏测试通过 ===")


if __name__ == "__main__":
    main()
