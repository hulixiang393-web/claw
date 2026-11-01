# -*- coding: utf-8 -*-
"""真机渲染探针：用真正的 NovelView（Qt，offscreen）渲染一章，数渲染出的假字。

用户报「界面还是乱码」时，脚本层验证不够——这里直接走 GUI 的渲染方法：
NovelView._display_chapter(ch, text)，然后从 QLabel/富文本容器里把字读回来。
用法：
    python tools/probe_reader_render.py [章节URL]
不给 URL 时自动从 timotxt 发现页取一本书的第一章。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

from framework.config import load_source  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.decrypter import Decrypter  # noqa: E402
from framework.discovery import Discovery  # noqa: E402
from framework.http import HttpClient, NetworkDefaults  # noqa: E402
from framework.parser import Parser  # noqa: E402
from gui.pages.reader.novel_view import NovelView  # noqa: E402

RANGES = ((0xAC00, 0xD7A3), (0xE000, 0xF8FF))


class _NC:
    def _selfcheck_url(self, source):
        return ""

    def check(self, source, url):
        return True


def fake_count(text):
    return sum(1 for ch in text
               if any(lo <= ord(ch) <= hi for lo, hi in RANGES))


def main(argv):
    source = load_source("sources/timotxt.json")
    http = HttpClient(defaults=NetworkDefaults(timeout=20, retries=1, interval_ms=300))
    parser, checker = Parser(), _NC()
    content = Content(http, parser, checker, Decrypter(http))
    discovery = Discovery(http, parser, checker)
    app = QApplication.instance() or QApplication([])
    view = NovelView(content)
    try:
        if len(argv) > 1:
            chapter_url = argv[1]
            detail = content.fetch_detail(source, chapter_url.rsplit("/", 1)[0] + "/dir")
        else:
            works = discovery.list_works(source, source.base_url, 1)
            detail = content.fetch_detail(source, works[0].url)
            chapter_url = detail.chapters[0].url
        chapter = detail.chapters[0]
        if len(argv) > 1:
            chapter.url = chapter_url
        raw = content.fetch_chapter(source, chapter.url)
        view._source = source
        view._detail = detail
        view._chapters = list(detail.chapters)
        view._current_idx = 0
        view._display_chapter(chapter, raw)
        app.processEvents()
        rendered = view.text.text()
        rich = ""
        body = getattr(view, "_rich_body", None)
        if body is not None:
            for label in body.findChildren(type(view.text)):
                rich += label.text()
        for name in ("_fragment_text_labels",):
            for label in getattr(view, name, []) or []:
                rendered += label.text()
        total = rendered + rich
        print("章节：%s" % chapter.url)
        print("渲染出的正文长度：%d（富文本 %d）" % (len(rendered), len(rich)))
        print("渲染出的假字：%d" % fake_count(total))
        print("样本：%s" % "".join(total.split())[:60])
        return 0 if fake_count(total) == 0 else 1
    finally:
        http.close()
        view.deleteLater()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
