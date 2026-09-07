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
    def get_text(self, url, headers=None, proxy=None, timeout=10, retries=3, interval_ms=0, encoding=None, proxy_pool=None):
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
    src_cfg = SourceConfig.from_dict(SRC)
    mgr.add(src_cfg)

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

    # ---- 自动翻章已删除：滚到底/滚回顶不自动跳话，70% 仍预渲染下一话 ----
    from gui.pages.reader.comic_view import ComicView
    comic = ComicView(content)
    comic.resize(600, 800)
    comic.show()
    comic._chapters = detail.chapters  # 复用两章
    comic._current_idx = 1
    comic._pending_swap = False
    comic.scroll.verticalScrollBar().setRange(0, 1000)
    assert not hasattr(comic, "_maybe_auto_next"), "自动翻话方法已删除"
    # 70% → 预渲染下一话（保留）；中段 → 不触发；滚到底/顶 → 不跳话
    prefetched = []
    comic._prefetch_future = lambda *a, **k: prefetched.append(True)
    comic._on_scroll_prefetch(500)   # 中段
    assert not prefetched, "中段不应预渲染下一话"
    comic._on_scroll_prefetch(800)   # 70%+
    assert prefetched, "读到 70% 应预渲染下一话"
    comic._on_scroll_prefetch(1000)  # 滚到底
    assert comic._current_idx == 1, f"自动翻话已删除：滚到底不跳话，idx={comic._current_idx}"
    comic._on_scroll_prefetch(0)     # 滚回顶
    assert comic._current_idx == 1, f"自动翻话已删除：滚回顶不跳话，idx={comic._current_idx}"
    print("  [ok] 漫画自动翻话已删除（滚到底/顶不跳话）；70% 预渲染下一话保留")

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
    assert not hasattr(nv, "_maybe_auto_next"), "自动翻章方法已删除"
    # 滚到底 → 不自动下一章；滚回顶 → 不自动上一章
    vbar = nv.scroll.verticalScrollBar()
    vbar.setValue(vbar.maximum())  # 滚到底
    assert nv._current_idx == 1, f"自动翻章已删除：滚到底不跳章，idx={nv._current_idx}"
    vbar.setValue(0)  # 滚回顶
    assert nv._current_idx == 1, f"自动翻章已删除：滚回顶不跳章，idx={nv._current_idx}"
    print("  [ok] 小说自动翻章已删除（滚到底/滚回顶均不跳章）")

    # ---- 续读记忆：重新打开恢复到上次章节 + 章内位置信号接线 ----
    from framework.reading_progress import ReadingProgress
    rp = ReadingProgress(base / "rp.json")
    rp.save(
        "demo", "http://example.com/book/1", "novel",
        "http://example.com/book/1/2.html", "第二章", position=0.0, page=0,
    )
    reader2 = ReaderPage(mgr, content, reading_progress=rp)
    reader2.open("demo", "http://example.com/book/1", "novel")  # 不带 start_chapter_url
    _lp6 = _EL(); _T.singleShot(3000, _lp6.quit); _lp6.exec()
    app.processEvents()
    _lp7 = _EL(); _T.singleShot(3000, _lp7.quit); _lp7.exec()
    app.processEvents()
    assert reader2.novel_view._current_idx == 1, \
        f"应恢复到上次章节（第二章），idx={reader2.novel_view._current_idx}"
    # 位置信号接线：手动触发滚动存盘 → resume 应带位置
    reader2.novel_view._last_pos_save_ts = 0.0
    reader2.novel_view._emit_position()
    rec = rp.resume("http://example.com/book/1")
    assert rec["chapter_url"] == "http://example.com/book/1/2.html", rec
    assert rec["position"] == 0.0, rec  # 滚动模式：位置为 0~1 比例，page 为 None
    print("  [ok] 续读记忆：重新打开恢复第二章 + position_changed 落盘")

    # ---- 跨源换源调度：reader_page 弹选择器 → switch_to_source 切到第二源 ----
    import time
    import framework.cross_source as _cs
    import gui.components.cross_source_dialog as _csd
    from framework.cross_source import CrossSourceCandidate

    SRC_B = {
        "$schema_version": 2, "$id": "src-b", "$type": "novel", "$name": "备用站",
        "transports": {"base_url": "http://b.com"},
        "endpoints": {
            "detail": {"fields": {"title": {"css": "h1"}}},
            "content": {"chapter": {"list": {
                "root_selector": {"css": ".catalog li a"},
                "fields": {"title": {"css": "a"}, "url": {"css": "a", "attr": "href"}},
            }, "body": {"selector": {"css": "#chaptercontent p"}}}},
        },
    }
    src_b_cfg = SourceConfig.from_dict(SRC_B)
    mgr.add(src_b_cfg)

    b_candidate = CrossSourceCandidate(
        source_id="src-b", source_name="备用站", title="测试小说",
        url="http://b.com/book/1", cover="", similarity=0.95,
        content_type="novel", sid="src-b",
    )

    class _StubDialog:
        Accepted = 1

        def __init__(self, *a, **k):
            self.selected = b_candidate

        def exec(self):
            return 1

    _csd.CrossSourceDialog = _StubDialog
    _cs.find_cross_source = (
        lambda manager, content, detail, content_type, concurrent=4, top_n=10: [b_candidate]
    )

    reader._on_cross_source_triggered(reader.novel_view._detail)
    deadline = time.time() + 10
    while time.time() < deadline and reader._current_source_id != "src-b":
        app.processEvents()
        time.sleep(0.05)
    assert reader._current_source_id == "src-b", reader._current_source_id
    assert reader.novel_view._detail.source_id == "src-b", \
        reader.novel_view._detail.source_id
    assert reader.novel_view._current_idx == 0, "换源后应从第 0 章加载"
    reader.show()  # 需要窗口可见，按钮 isVisible() 才为真
    app.processEvents()
    assert reader.novel_view.source_btn.isVisible(), "换源后 ⇄ 换源按钮应可见"
    print("  [ok] 跨源换源调度：reader_page → 候选选择 → switch_to_source → 切到 src-b")

    # ---- 三视图跨源换源入口 + 列表长标题换行修复 ----
    from PySide6.QtCore import QSize
    from gui.pages.reader.video_view import VideoView
    from gui.pages.reader.epub_view import EpubView

    assert hasattr(reader.video_view, "cross_source_chosen")
    assert hasattr(reader.comic_view, "cross_source_chosen")
    assert hasattr(reader.novel_view, "cross_source_chosen")

    # 漫画：⇄ 换源按钮默认隐藏 → load(detail) 后显示，点击发出当前 detail
    comic4 = ComicView(content)
    comic4.resize(600, 800)
    comic4.show()
    assert not comic4.source_btn.isVisible(), "漫画换源按钮默认应隐藏"
    comic_detail = Detail(
        source_id="demo", content_type="comic", url="http://example.com/c/1",
        title="测试漫画",
        chapters=[Chapter("第1话一长串标题", "http://example.com/c/1/1.html"),
                  Chapter("第2话", "http://example.com/c/1/2.html")],
    )
    comic4.load(src_cfg, comic_detail, "")
    app.processEvents()
    assert comic4.source_btn.isVisible(), "load(detail) 后漫画换源按钮应显示"
    assert comic4.toc_list.wordWrap(), "漫画目录列表应长标题换行"
    got_cs = []
    comic4.cross_source_chosen.connect(got_cs.append)
    comic4.source_btn.click()
    assert got_cs and got_cs[0] is comic_detail, got_cs
    print("  [ok] 漫画：⇄ 换源按钮 隐藏→显示＋点击发当前 detail＋目录换行")

    # 小说：同规则，校验长标题换行与信号 payload
    nv4 = NovelView(content)
    nv4.resize(600, 800)
    nv4.show()
    assert not nv4.source_btn.isVisible()
    nv4.load(object(), long_detail, "")
    assert nv4.source_btn.isVisible(), "load(detail) 后小说换源按钮应显示"
    assert nv4.toc_list.wordWrap(), "小说目录列表应长标题换行"
    got_n = []
    nv4.cross_source_chosen.connect(got_n.append)
    nv4.source_btn.click()
    assert got_n and got_n[0] is long_detail, got_n
    print("  [ok] 小说：⇄ 换源按钮 隐藏→显示＋点击发当前 detail＋目录换行")

    # 视频：ep_list 图标 76×50 + 长标题换行；current_episode_no() 0 基；「⇄ 跨源换源」菜单项
    vd = Detail(
        source_id="demo", content_type="video", url="http://example.com/v/1",
        title="测试动画",
        chapters=[Chapter("第1集一长串标题", "http://example.com/v/e1"),
                  Chapter("第2集", "http://example.com/v/e2")],
    )
    video4 = VideoView(content)
    video4.resize(800, 600)
    video4.show()
    assert not video4.source_btn.isVisible(), "视频换源按钮默认应隐藏"
    video4.load(src_cfg, vd, "")
    app.processEvents()
    assert video4.source_btn.isVisible(), "load(detail) 后视频换源按钮应显示"
    assert video4.ep_list.iconSize() == QSize(76, 50), video4.ep_list.iconSize()
    assert video4.ep_list.wordWrap(), "视频分集列表应长标题换行"
    assert video4.current_episode_no() == 0
    video4._current_idx = 1
    assert video4.current_episode_no() == 1
    video4._current_idx = 99
    assert video4.current_episode_no() == 0, "越界应回退 0"
    video4._current_idx = 1
    video4._refresh_source_menu()
    texts = [a.text() for a in video4.source_menu.actions()]
    assert any("跨源换源" in t for t in texts), texts
    got_v = []
    video4.cross_source_chosen.connect(got_v.append)
    video4.cs_action.trigger()
    assert got_v and got_v[0] is vd, got_v
    print("  [ok] 视频：⇄ 换源按钮＋菜单「⇄ 跨源换源」＋ep_list 76×50 换行＋current_episode_no()")

    # epub：目录列表长标题换行（构造签名 font_scale, parent）
    ev4 = EpubView()
    assert ev4.toc_list.wordWrap(), "epub 目录列表应长标题换行"
    print("  [ok] epub：目录列表长标题换行")

    print("\n=== 阅读器离屏测试通过（含跨源换源调度 + 三视图入口 + 列表换行） ===")


if __name__ == "__main__":
    import sys as _sys
    import traceback as _tb
    try:
        main()
        _sys.stdout.flush()
        _sys.exit(0)
    except SystemExit:
        raise  # 正常退出路径：放行，保持退出码
    except BaseException:
        _sys.stderr.flush()
        _sys.stdout.flush()
        _tb.print_exc()
        _sys.stdout.flush()
        _sys.stderr.flush()
        _sys.exit(1)
