from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QPixmap, QTextDocument

from framework.config import load_source
from framework.content import BookStackChapterResult, Content
from framework.parser import Parser
from gui.pages.reader.novel_view import NovelView, _LoadChapterTask


ROOT = Path(__file__).resolve().parents[1]
SOURCE = load_source(ROOT / "sources" / "bookstack.json")


class _Http:
    defaults = SimpleNamespace(timeout=10, retries=0, interval_ms=0)

    def __init__(self, html):
        self.html = html
        self.calls = []

    def get_text(self, url, **kwargs):
        self.calls.append(url)
        return self.html


def _content(html):
    return Content(_Http(html), Parser(), object())


def test_bookstack_chapters_walk_nested_lists_in_dom_order():
    html = """
    <div class="wd-tab-content">
      <ul class="none-listyle">
        <li><a href="/one">一</a>
          <ul><li><a href="/one/a">一甲</a></li><li><a href="/one/b">一乙</a></li></ul>
        </li>
        <li><a href="/two">二</a>
          <ul><li><a href="/one/b">重复一乙</a></li></ul>
        </li>
      </ul>
    </div>
    """
    content = _content(html)

    chapters = content._fetch_chapters(
        SOURCE,
        content._parser.parse(html),
        detail_url="https://www.bookstack.cn/books/demo",
    )

    assert [(chapter.title, chapter.url) for chapter in chapters] == [
        ("一", "https://www.bookstack.cn/one"),
        ("一甲", "https://www.bookstack.cn/one/a"),
        ("一乙", "https://www.bookstack.cn/one/b"),
        ("二", "https://www.bookstack.cn/two"),
    ]
    assert [chapter.depth for chapter in chapters] == [0, 1, 1, 0]


def test_bookstack_chapters_load_multilevel_summary_sidebar():
    detail_html = """
    <div class="wd-tab-content"><ul class="none-listyle">
      <li><a href="/read/demo/README.md">首页</a></li>
    </ul></div>
    """
    summary_html = """
    <div class="article-menu-detail"><ul>
      <li><a href="/read/demo/SUMMARY.md">目录</a><ul></ul></li>
      <li class="collapse-node"><a href="/read/demo/part.md">第一部分</a><ul>
        <li><a href="/read/demo/part-one.md">第一节</a><ul>
          <li><a href="/read/demo/part-one-deep.md">深层小节</a><ul></ul></li>
        </ul></li>
      </ul></li>
    </ul></div>
    """

    class _SummaryHttp:
        defaults = SimpleNamespace(timeout=10, retries=0, interval_ms=0)

        def get_text(self, url, **kwargs):
            if url.endswith("/read/demo/SUMMARY.md"):
                return summary_html
            return detail_html

    source = SOURCE
    content = Content(_SummaryHttp(), Parser(), object())
    chapters = content._fetch_chapters(
        source,
        content._parser.parse(detail_html),
        detail_url="https://www.bookstack.cn/books/demo",
        html=detail_html,
    )

    assert [(chapter.title, chapter.depth) for chapter in chapters] == [
        ("目录", 0),
        ("第一部分", 0),
        ("第一节", 1),
        ("深层小节", 2),
    ]
    assert chapters[-1].url == "https://www.bookstack.cn/read/demo/part-one-deep.md"


def test_bookstack_reader_root_provides_nested_directory_before_summary_fallback():
    detail_html = '<div class="wd-tab-content"><ul class="none-listyle"><li><a href="/read/demo/top.md">顶层</a></li></ul></div>'
    reader_html = """
    <div class="article-menu-detail"><ul>
      <li><a href="/read/demo/one.md">一级</a><ul>
        <li><a href="/read/demo/two.md">二级</a><ul>
          <li><a href="/read/demo/three.md">三级</a></li>
        </ul></li>
      </ul></li>
    </ul></div>
    """
    calls = []

    class _ReaderHttp:
        defaults = SimpleNamespace(timeout=10, retries=0, interval_ms=0)

        def get_text(self, url, **kwargs):
            calls.append(url)
            return reader_html if url.endswith("/read/demo") else detail_html

    content = Content(_ReaderHttp(), Parser(), object())
    chapters = content._fetch_chapters(
        SOURCE,
        content._parser.parse(detail_html),
        detail_url="https://www.bookstack.cn/books/demo",
        html=detail_html,
    )

    assert [(chapter.title, chapter.depth) for chapter in chapters] == [
        ("一级", 0),
        ("二级", 1),
        ("三级", 2),
    ]
    assert calls == ["https://www.bookstack.cn/read/demo"]


def test_bookstack_rich_result_preserves_structure_and_image_dom_order():
    html = """
    <article class="article-body markdown-body">
      <h2>标题</h2><p>前文 <a href="/docs/link">链接</a></p>
      <img src="/images/one.png"><blockquote>引用</blockquote>
      <pre><code>print(1)</code></pre><table><tr><td>单元格</td></tr></table>
      <img data-src="images/two.png"><ul><li>列表</li></ul>
    </article>
    """
    content = _content(html)

    result = content.fetch_bookstack_chapter(
        SOURCE, "https://www.bookstack.cn/books/demo/chapter"
    )

    assert isinstance(result, BookStackChapterResult)
    assert "<h2>标题</h2>" in result.body_html
    assert "<blockquote>引用</blockquote>" in result.body_html
    assert "<pre><code>print(1)</code></pre>" in result.body_html
    assert "<table>" in result.body_html
    assert 'href="https://www.bookstack.cn/docs/link"' in result.body_html
    assert 'src="https://www.bookstack.cn/images/one.png"' in result.body_html
    assert 'src="https://www.bookstack.cn/books/demo/images/two.png"' in result.body_html
    assert result.image_urls == [
        "https://www.bookstack.cn/images/one.png",
        "https://www.bookstack.cn/books/demo/images/two.png",
    ]


def test_bookstack_rich_prefers_lazy_image_url_over_loading_placeholder():
    html = """
    <article class="article-body markdown-body">
      <p>正文</p>
      <img src="/static/images/loading.gif"
           data-original="https://static.bookstack.cn/projects/demo/real.png">
    </article>
    """
    content = _content(html)

    result = content.fetch_bookstack_chapter(
        SOURCE, "https://www.bookstack.cn/read/demo/chapter.md"
    )

    assert result.image_urls == [
        "https://static.bookstack.cn/projects/demo/real.png"
    ]
    assert 'src="https://static.bookstack.cn/projects/demo/real.png"' in result.body_html
    assert "loading.gif" not in result.body_html


def test_bookstack_rich_fetch_keeps_plain_text_api_unchanged():
    html = '<article class="article-body markdown-body"><h2>标题</h2><p>正文</p></article>'
    content = _content(html)

    body = content.fetch_chapter(SOURCE, "/books/demo/chapter")
    assert "标题" in body
    assert "正文" in body


def test_load_chapter_task_keeps_bookstack_rich_result_for_reader():
    calls = []

    class _Content:
        def fetch_bookstack_chapter(self, source, url):
            calls.append((source, url))
            return BookStackChapterResult(
                body_html="<p>正文</p>",
                text="正文",
                image_urls=["https://img.example/one.png"],
            )

        def fetch_chapter(self, source, url):
            raise AssertionError("BookStack must not fall back to plain text")

    chapter = SimpleNamespace(url="chapter")
    task = _LoadChapterTask(_Content(), SOURCE, chapter, load_images=True)
    task.run()

    assert calls == [(SOURCE, "chapter")]
    assert chapter._cached_rich_content.body_html == "<p>正文</p>"
    assert chapter._cached_text == "正文"


def test_novel_view_renders_bookstack_rich_contract_without_dropping_text(_qapp, monkeypatch):
    view = NovelView(object())
    view._source = SOURCE
    view._load_generation = 1
    view._show_images = True
    loaded = []
    monkeypatch.setattr(view, "_load_image", lambda url, image_generation, load_generation: loaded.append(url))
    chapter = SimpleNamespace(
        title="第一章",
        url="chapter",
        _cached_text="标题\n正文",
        _cached_rich_content=BookStackChapterResult(
            body_html="<h2>标题</h2><p>正文</p>",
            text="标题\n正文",
            image_urls=["https://img.example/one.png"],
        ),
    )
    view._chapters = [chapter]
    view._current_idx = 0

    view._display_chapter(chapter, chapter._cached_text)
    _qapp.processEvents()

    assert "标题" in view._rich_body.toHtml()
    assert "正文" in view._rich_body.toHtml()
    assert loaded == ["https://img.example/one.png"]


def test_novel_view_bookstack_uses_reading_card_typography_and_dark_text(_qapp):
    view = NovelView(object())
    view._source = SimpleNamespace(source_id="bookstack", source_name="书栈网")
    view._detail = SimpleNamespace(title="Istio v1.30 中文文档")
    view._base_font = 18

    html = view._bookstack_html("平台安装", "<p>正文</p><img src=\"https://img.example/one.png\">")
    css = view._bookstack_css()

    assert 'class="bookstack-title"' in html
    assert "来源：书栈网" in html
    assert "color: #222222" in css
    assert "font-size: 18px" in css
    assert "text-align: justify" in css
    assert "max-width: 100%" in css
    assert "border-radius: 4px" in css
    view.deleteLater()


def test_novel_view_bookstack_reading_style_keeps_card_light_and_updates_outer_background(_qapp):
    view = NovelView(object())
    view._source = SOURCE
    view._rich_body.setVisible(True)

    view.set_reading_style("#202124", 21, fg="#eeeeee")

    assert "#202124" in view.scroll_body.styleSheet()
    assert "#ffffff" in view._rich_body.styleSheet()
    assert "#222222" in view._bookstack_css()
    assert "font-size: 21px" in view._bookstack_css()
    view.deleteLater()


def test_novel_view_bookstack_image_resource_scales_to_card_width(_qapp):
    view = NovelView(object())
    view._source = SOURCE
    view._show_images = True
    view._image_generation = 1
    view._load_generation = 1
    view._rich_body.setHtml('<p>正文<img src="https://img.example/one.png"></p>')
    view.resize(620, 600)
    view.show()
    _qapp.processEvents()
    pixmap = QPixmap(1200, 600)
    pixmap.fill()

    assert view._on_image_loaded(
        "https://img.example/one.png", 1, 1, pixmap
    ) is True
    resource = view._rich_body.document().resource(
        QTextDocument.ResourceType.ImageResource,
        QUrl("https://img.example/one.png"),
    )
    assert isinstance(resource, QPixmap)
    assert resource.width() <= view._bookstack_image_width()
    view.deleteLater()


def test_novel_view_bookstack_image_resource_uses_qtextdocument_enum(_qapp):
    view = NovelView(object())
    view._source = SOURCE
    view._show_images = True
    view._image_generation = 1
    view._load_generation = 1
    view._rich_body.setHtml('<p>正文<img src="https://img.example/one.png"></p>')
    view.resize(800, 600)
    view.show()
    _qapp.processEvents()
    pixmap = QPixmap(2, 2)

    assert view._on_image_loaded(
        "https://img.example/one.png", 1, 1, pixmap
    ) is True
    resource = view._rich_body.document().resource(
        QTextDocument.ResourceType.ImageResource,
        QUrl("https://img.example/one.png"),
    )
    assert isinstance(resource, QPixmap)
    view.deleteLater()


def test_novel_view_bookstack_does_not_add_external_image_placeholders(_qapp, monkeypatch):
    view = NovelView(object())
    view._source = SOURCE
    view._load_generation = 1
    view._show_images = True
    monkeypatch.setattr(
        "gui.components.cover_loader.CoverLoader.instance",
        lambda: SimpleNamespace(load=lambda *args, **kwargs: None),
    )
    chapter = SimpleNamespace(
        title="第一章",
        url="chapter",
        _cached_text="正文",
        _cached_rich_content=BookStackChapterResult(
            body_html='<p>正文<img src="https://img.example/one.png"></p>',
            text="正文",
            image_urls=["https://img.example/one.png"],
        ),
    )
    view._chapters = [chapter]
    view._current_idx = 0
    view.resize(800, 600)
    view.show()
    _qapp.processEvents()

    view._display_chapter(chapter, chapter._cached_text)
    _qapp.processEvents()

    placeholders = [
        view.scroll_layout.itemAt(i).widget()
        for i in range(view.scroll_layout.count())
        if view.scroll_layout.itemAt(i).widget() is not None
        and view.scroll_layout.itemAt(i).widget().property("image_url")
    ]
    assert placeholders == []
    assert view._rich_body.isVisible() is True
    view.deleteLater()


def test_novel_view_bookstack_image_toggle_preserves_rich_body(_qapp, monkeypatch):
    view = NovelView(object())
    view._source = SOURCE
    chapter = SimpleNamespace(
        title="第一章",
        url="chapter",
        _cached_rich_content=BookStackChapterResult(
            body_html="<p>正文</p>",
            text="正文",
            image_urls=["https://img.example/one.png"],
        ),
        _cached_image_urls=[{"type": "image", "url": "https://img.example/one.png"}],
    )
    view._chapters = [chapter]
    view._current_idx = 0
    view.resize(800, 600)
    view.show()
    _qapp.processEvents()
    view._rich_body.setVisible(True)
    view.text.setVisible(False)
    loaded = []
    monkeypatch.setattr(view, "_load_image", lambda *args: loaded.append(args[0]))

    view.set_show_images(False)
    assert view._rich_body.isVisible() is True
    assert view.text.isVisible() is False
    view.set_show_images(True)
    assert loaded == ["https://img.example/one.png"]
    view.deleteLater()


def test_load_chapter_task_keeps_bookstack_rich_result_when_images_disabled():
    calls = []

    class _Content:
        def fetch_bookstack_chapter(self, source, url):
            calls.append((source, url))
            return BookStackChapterResult(body_html="<p>正文</p>", text="正文")

        def fetch_chapter(self, source, url):
            raise AssertionError("BookStack must keep rich rendering when images are disabled")

    chapter = SimpleNamespace(url="chapter")
    task = _LoadChapterTask(_Content(), SOURCE, chapter, load_images=False)
    task.run()

    assert calls == [(SOURCE, "chapter")]
    assert chapter._cached_rich_content.body_html == "<p>正文</p>"


def test_novel_view_bookstack_ignores_old_image_generation(_qapp):
    view = NovelView(object())
    view._source = SOURCE
    view._show_images = True
    view._image_generation = 2
    view._load_generation = 2
    view._rich_body.setHtml('<p>正文<img src="https://img.example/one.png"></p>')
    view._rich_body.setVisible(True)
    pixmap = QPixmap(2, 2)

    assert view._on_image_loaded(
        "https://img.example/one.png", 1, 2, pixmap
    ) is False
    resource = view._rich_body.document().resource(
        QTextDocument.ResourceType.ImageResource,
        QUrl("https://img.example/one.png"),
    )
    assert resource is None
    view.deleteLater()


def test_novel_view_bookstack_disables_automatic_rich_link_navigation(_qapp):
    view = NovelView(object())
    assert view._rich_body.openExternalLinks() is False
    assert view._rich_body.openLinks() is False
    view.deleteLater()


def test_novel_view_bookstack_internal_anchor_loads_matching_chapter(_qapp):
    view = NovelView(object())
    view._source = SOURCE
    view._chapters = [
        SimpleNamespace(title="第一章", url="https://www.bookstack.cn/one"),
        SimpleNamespace(title="第二章", url="https://www.bookstack.cn/two/"),
    ]
    loaded = []
    view._load_chapter = loaded.append

    view._on_rich_anchor_clicked(QUrl("https://www.bookstack.cn/two#part"))

    assert loaded == [1]


def test_novel_view_bookstack_relative_anchor_loads_matching_chapter(_qapp):
    view = NovelView(object())
    view._source = SOURCE
    view._chapters = [
        SimpleNamespace(title="一", url="https://www.bookstack.cn/books/one"),
        SimpleNamespace(title="二", url="https://www.bookstack.cn/books/two"),
    ]
    loaded = []
    view._load_chapter = loaded.append

    view._on_rich_anchor_clicked(QUrl("/books/two#part"))

    assert loaded == [1]


def test_novel_view_non_matching_anchor_opens_in_browser(_qapp, monkeypatch):
    view = NovelView(object())
    view._source = SOURCE
    view._chapters = [SimpleNamespace(title="第一章", url="https://www.bookstack.cn/one")]
    opened = []
    monkeypatch.setattr("gui.pages.reader.novel_view.webbrowser.open", opened.append)

    view._on_rich_anchor_clicked(QUrl("https://www.example.com/other#part"))

    assert opened == ["https://www.example.com/other#part"]


def test_novel_view_rejects_non_http_rich_links(_qapp, monkeypatch):
    view = NovelView(object())
    view._source = SOURCE
    opened = []
    monkeypatch.setattr("gui.pages.reader.novel_view.webbrowser.open", opened.append)

    view._on_rich_anchor_clicked(QUrl("file:///C:/private.txt"))
    view._on_rich_anchor_clicked(QUrl("javascript:alert(1)"))

    assert opened == []


def test_novel_view_rich_fragment_stays_in_current_document(_qapp, monkeypatch):
    view = NovelView(object())
    view._source = SOURCE
    scrolled = []
    monkeypatch.setattr(view._rich_body, "scrollToAnchor", scrolled.append)
    monkeypatch.setattr(view, "_load_chapter", lambda index: (_ for _ in ()).throw(AssertionError()))

    view._on_rich_anchor_clicked(QUrl("#section"))

    assert scrolled == ["section"]


def test_novel_view_bookstack_toc_builds_multilevel_tree(_qapp):
    view = NovelView(object())
    view._source = SOURCE
    view._chapters = [
        SimpleNamespace(title="一级", url="one", depth=0),
        SimpleNamespace(title="二级", url="two", depth=1),
        SimpleNamespace(title="三级", url="three", depth=2),
        SimpleNamespace(title="另一一级", url="four", depth=0),
    ]
    view._populate_toc()

    assert view.toc_tree.topLevelItemCount() == 2
    first = view.toc_tree.topLevelItem(0)
    assert first.text(0) == "一级"
    assert first.data(0, Qt.UserRole) == 0
    assert first.childCount() == 1
    second = first.child(0)
    assert second.text(0) == "二级"
    assert second.data(0, Qt.UserRole) == 1
    assert second.childCount() == 1
    assert second.child(0).text(0) == "三级"
    assert second.child(0).data(0, Qt.UserRole) == 2
    assert view.toc_tree.topLevelItem(1).data(0, Qt.UserRole) == 3


def test_novel_view_bookstack_toc_click_loads_leaf_and_layout_expands(_qapp):
    view = NovelView(object())
    view._source = SOURCE
    view._chapters = [
        SimpleNamespace(title="一级", url="one", depth=0),
        SimpleNamespace(title="二级", url="two", depth=1),
    ]
    view._populate_toc()
    loaded = []
    view._load_chapter = loaded.append
    leaf = view.toc_tree.topLevelItem(0).child(0)

    view._on_toc_clicked(leaf, 0)
    view.resize(900, 600)
    view.show()
    _qapp.processEvents()

    assert loaded == [1]
    assert view.toc_tree.isVisible() is False
    view._toggle_toc()
    _qapp.processEvents()
    assert view.toc_tree.isVisible() is True
    assert view.toc_tree.width() >= 240
    assert view.toc_tree.height() > 0
    assert view.body_stack.width() > 0
    assert view.body_stack.height() > 0
