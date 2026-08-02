"""阅读器离屏测试（小说视图，mock 源）。"""
import os, sys, pathlib
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

from framework.config import SourceConfig
from framework.source_manager import SourceManager
from framework.http import HttpClient
from framework.parser import Parser
from framework.selfcheck import StructureChecker
from framework.content import Content
from framework.events import EventBus
from framework.theme_manager import ThemeManager
from framework.settings_manager import SettingsManager
from gui.pages.reader_page import ReaderPage

DETAIL_HTML = """<html><body><div class="site-header">d</div>
<h1>测试小说</h1>
<meta property="og:novel:author" content="作者甲">
<div class="info"><span class="status">连载中</span></div>
<div class="catalog"><ul><li><a href="/book/1/1.html">第一章</a></li><li><a href="/book/1/2.html">第二章</a></li></ul></div>
</body></html>"""

CHAPTER_HTML = """<html><body><div class="site-header">d</div>
<div id="chaptercontent"><p>第一章正文开始</p><p>这是小说正文内容。</p></div>
</body></html>"""

SRC = {
    "$schema_version": 2, "$id": "demo", "$type": "novel", "$name": "演示站",
    "transports": {"base_url": "http://example.com"},
    "endpoints": {
        "detail": {
            "fields": {
                "title": {"css": "h1"},
                "author": {"css": "meta[property='og:novel:author']", "attr": "content"},
                "status": {"css": ".status"},
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


class MockHttp(HttpClient):
    def get_text(self, url, headers=None, proxy=None, timeout=10, retries=3, interval_ms=0):
        if "1.html" in url or "2.html" in url:
            return CHAPTER_HTML
        return DETAIL_HTML


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
    content = Content(http, parser, checker)

    from framework.content import Detail, Chapter
    detail = Detail(
        source_id="demo", content_type="novel", url="http://example.com/book/1",
        title="测试小说", author="作者甲", status="连载中",
        chapters=[Chapter("第一章", "http://example.com/book/1/1.html"),
                  Chapter("第二章", "http://example.com/book/1/2.html")],
    )

    reader = ReaderPage(mgr, content)
    reader.open("demo", "http://example.com/book/1", "novel")

    # 等待详情加载
    from PySide6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(3000, loop.quit)
    loop.exec()
    app.processEvents()

    print("标题:", reader.title_label.text())
    assert reader.title_label.text() == "测试小说", reader.title_label.text()

    # 等待第一章正文
    loop2 = QEventLoop()
    QTimer.singleShot(3000, loop2.quit)
    loop2.exec()
    app.processEvents()

    novel = reader.novel_view
    text = novel.text.text()
    print("正文:", text[:40])
    assert "第一章正文开始" in text, text
    print("目录章节数:", novel.toc_list.count())
    assert novel.toc_list.count() == 2

    print("\n=== 阅读器小说视图离屏测试通过 ===")


if __name__ == "__main__":
    main()
