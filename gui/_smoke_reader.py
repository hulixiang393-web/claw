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

    # ---- 双向翻章测试（小说翻页模式） ----
    # 切到翻页模式
    novel._toggle_mode()
    assert novel._mode == "pager", novel._mode
    # 初始在第1章第0页 → 向上翻应触发上一章（但已是首章，不应跳）
    novel._auto_prev_loading = False
    novel._current_page = 0
    novel._pager_turn(-1)  # 已是首章，不应跳转
    assert novel._current_idx == 0, f"首章不应向上跳，当前idx={novel._current_idx}"
    # 跳到第2章第0页 → 向上翻应回第1章最后一页
    novel._load_chapter(1, scroll_to_end=False)
    app.processEvents()
    # 等待第二章加载
    from PySide6.QtCore import QEventLoop as _EL, QTimer as _T
    _lp = _EL()
    _T.singleShot(1500, _lp.quit)
    _lp.exec()
    app.processEvents()
    assert novel._current_idx == 1, f"应停在第二章，idx={novel._current_idx}"
    novel._current_page = 0
    novel._pager_turn(-1)  # 第2章第0页再向上 → 回第1章最后一页
    app.processEvents()
    _lp2 = _EL()
    _T.singleShot(1500, _lp2.quit)
    _lp2.exec()
    app.processEvents()
    assert novel._current_idx == 0, f"向上翻应回第一章，idx={novel._current_idx}"
    # 回第一章后应定位到最后一页
    assert novel._current_page == novel._page_count - 1, \
        f"应定位到第一章最后一页，page={novel._current_page}/{novel._page_count}"
    print(f"  [ok] 小说翻页模式双向跳章（回第一章第{novel._current_page+1}页）")

    # ---- 双向翻章测试（漫画） ----
    from gui.pages.reader.comic_view import ComicView
    comic = ComicView(content)
    comic.resize(600, 800)
    comic.show()
    comic._chapters = detail.chapters  # 复用两章
    comic._current_idx = 1
    comic._images = ["data:image/png;base64," + "A" * 200 for _ in range(20)]
    comic._render_images()
    # 离屏下图片不足以撑高 scrollbar → 手动设 range 模拟可滚动内容
    comic.scroll.verticalScrollBar().setRange(0, 1000)
    comic._auto_prev_loading = False
    comic._load_episode(1)  # 第2话
    assert comic._current_idx == 1
    comic._current_idx = 1
    # 边沿触发状态机：未武装停在边界不触发；离开边界才武装；武装后回边界触发
    comic._edge_armed = False
    comic._auto_prev_loading = False
    comic._maybe_auto_next(0)  # 顶部未武装 → 不触发
    assert not comic._auto_prev_loading, "未武装滚回顶部不应触发向上跳话"
    comic._maybe_auto_next(500)  # 滚回中段 → 武装
    assert comic._edge_armed, "离开边界应重新武装"
    comic._auto_prev_loading = False
    comic._maybe_auto_next(0)  # 武装后滚回顶部 → 触发上一话
    # mock 无 fetch_comic_pages → 锁设过即验证触发逻辑成立
    assert comic._auto_prev_loading, "武装后滚回顶部应触发向上跳话锁"
    assert not comic._edge_armed, "触发后应解除武装"
    # 停在顶部（边界）不再触发（未武装）
    comic._auto_prev_loading = False
    comic._maybe_auto_next(0)
    assert not comic._auto_prev_loading, "触发后停在顶部不应再次跳话"
    print(f"  [ok] 漫画向上边沿触发（未武装不跳 + 离开武装 + 回边界触发 + 触发后解除）")

    # ---- 小说滚动模式：向上滑到顶 → 上一章末尾（不重回开头） ----
    from gui.pages.reader.novel_view import NovelView

    class _LongContent:
        """返回超长正文，确保 QLabel 可滚动。"""
        def fetch_chapter(self, source, url):
            idx = int(url.rsplit("c", 1)[1])
            return f"第{idx}章正文补充内容。" * 200

    nv = NovelView(_LongContent())
    nv.resize(600, 800)
    nv.show()
    long_detail = Detail(
        source_id="demo", content_type="novel", url="http://example.com/book/long",
        title="长文测试",
        chapters=[Chapter("第一章", "http://example.com/long/c1"),
                  Chapter("第二章", "http://example.com/long/c2")],
    )
    nv.load(object(), long_detail, "")
    _lp3 = _EL()
    _T.singleShot(1000, _lp3.quit)
    _lp3.exec()
    app.processEvents()
    # 跳到第二章（顶部开始）
    nv._load_chapter(1, scroll_to_end=False)
    _lp4 = _EL()
    _T.singleShot(1000, _lp4.quit)
    _lp4.exec()
    app.processEvents()
    assert nv._current_idx == 1, f"应在第二章，idx={nv._current_idx}"
    # 真实用户行为：向上滑 → 先离开顶部（进中段，边沿武装），再滑回顶部（触发）
    vbar = nv.scroll.verticalScrollBar()
    mid = vbar.maximum() // 2
    nv._maybe_auto_next(mid)  # 离开边界区 → _edge_armed=True
    assert nv._edge_armed, "滚回中段应重新武装"
    nv._auto_prev_loading = False
    nv._maybe_auto_next(0)  # 滚回顶部 → 触发向上翻章
    _lp5 = _EL()
    _T.singleShot(1200, _lp5.quit)
    _lp5.exec()
    app.processEvents()
    assert nv._current_idx == 0, f"向上滑应回第一章，idx={nv._current_idx}"
    vbar = nv.scroll.verticalScrollBar()
    assert vbar.value() >= vbar.maximum() - 5, \
        f"应定位到第一章末尾，value={vbar.value()}/max={vbar.maximum()}"
    # 停在上一章末尾（边界）不应再触发向下（未武装）
    nv._maybe_auto_next(vbar.maximum())
    assert nv._current_idx == 0, f"停在末尾不应触发向下跳，idx={nv._current_idx}"
    print(f"  [ok] 小说滚动模式向上翻章定位上一章末尾且不反弹（value={vbar.value()}/{vbar.maximum()}）")

    print("\n=== 阅读器小说视图离屏测试通过 ===")


if __name__ == "__main__":
    main()
