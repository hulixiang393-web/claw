"""搜索界面离屏测试（mock HTTP）。"""
import os, sys, time, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEventLoop, QTimer

from framework.config import SourceConfig
from framework.source_manager import SourceManager
from framework.http import HttpClient
from framework.parser import Parser
from framework.search import Search
from gui.pages.search_page import SearchPage

SRC = {
    "$schema_version": 2,
    "$id": "site_a",
    "$type": "novel",
    "$name": "站点A",
    "transports": {"base_url": "http://x.com"},
    "endpoints": {
        "search": {
            "base_url": "/search",
            "method": "GET",
            "item": {
                "root_selector": {"css": ".item"},
                "fields": {"title": {"css": ".t"}, "url": {"css": ".t", "attr": "href"}},
            },
        }
    },
}

HTML = """<html><body><div class="item"><a class="t" href="/book/1">测试书1</a></div>
<div class="item"><a class="t" href="/book/2">测试书2</a></div></body></html>"""


class MockHttp(HttpClient):
    def get_text(self, url, headers=None, proxy=None, timeout=10, retries=3, interval_ms=0, encoding=None, proxy_pool=None):
        return HTML


def main():
    app = QApplication.instance() or QApplication([])
    mgr = SourceManager()
    mgr.add(SourceConfig.from_dict(SRC))
    mgr.add(SourceConfig.from_dict({**SRC, "$id": "site_b", "$name": "站点B"}))
    http = MockHttp(sleeper=lambda _: None)
    parser = Parser()
    search = Search(http, parser)
    page = SearchPage(mgr, search)
    page.keyword_input.setText("测试")
    page._on_search()

    # 等待搜索完成
    loop = QEventLoop()
    QTimer.singleShot(4000, loop.quit)
    loop.exec()
    app.processEvents()

    print("status:", page.status_label.text())
    cards = [page.grid_layout.itemAt(i).widget() for i in range(page.grid_layout.count())]
    cards = [c for c in cards if c is not None]
    print("结果卡片:", len(cards))
    assert len(cards) == 4, len(cards)  # 2 源 × 2 条

    print("\n=== 搜索界面离屏测试通过 ===")


if __name__ == "__main__":
    main()
