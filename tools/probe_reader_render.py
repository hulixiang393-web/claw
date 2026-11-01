# -*- coding: utf-8 -*-
"""真机渲染探针：用真正的 NovelView（Qt，offscreen）渲染一章，检查上屏文字。

走的是 GUI 的真实路径：`_LoadChapterTask(load_images=True)` → `_on_chapter_loaded`
→ `_display_chapter`。显示图片开启时渲染器会走「片段渲染」（正文 QLabel 被隐藏，
文字来自 `_cached_image_urls` 里的文本片段），字体混淆源的假字就是在这里露出来的。

检查项：
  1. 上屏文字（主正文 + 片段）里还剩多少假字（谚文/私用区码点）
  2. 与站点明文接口真值的一致率（渲染是否正确，而不只是「没乱码」）

用法：
    python tools/probe_reader_render.py [章节URL]
"""
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

from framework import zh_convert  # noqa: E402
from framework.config import load_source  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.decrypter import Decrypter  # noqa: E402
from framework.discovery import Discovery  # noqa: E402
from framework.http import HttpClient, NetworkDefaults  # noqa: E402
from framework.parser import Parser  # noqa: E402
from gui.pages.reader.novel_view import NovelView  # noqa: E402
from gui.pages.reader.novel_tasks import _LoadChapterTask  # noqa: E402
from tools.verify_timo_render_truth import compare, truth_text  # noqa: E402

RANGES = ((0xAC00, 0xD7A3), (0xE000, 0xF8FF), (0x20000, 0x2FFFF))
TAG_RE = re.compile(r"<[^>]+>")


class _NC:
    def _selfcheck_url(self, source):
        return ""

    def check(self, source, url):
        return True


def fake_count(text):
    return sum(1 for ch in text
               if any(lo <= ord(ch) <= hi for lo, hi in RANGES))


def strip_html(text):
    return TAG_RE.sub("", text or "")


def main(argv):
    source = load_source("sources/timotxt.json")
    http = HttpClient(defaults=NetworkDefaults(timeout=20, retries=1, interval_ms=300))
    parser, checker = Parser(), _NC()
    content = Content(http, parser, checker, decrypter=Decrypter(http))
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
        chapter = next(
            (ch for ch in detail.chapters if ch.url == chapter_url),
            detail.chapters[0],
        )
        view._source = source
        view._detail = detail
        view._chapters = list(detail.chapters)
        view._current_idx = view._chapters.index(chapter)
        view._show_images = True
        view._mode = "scroll"
        # GUI 同款加载路径：带图片抓取（片段渲染）
        task = _LoadChapterTask(content, source, chapter, load_images=True)
        task.signals.finished.connect(view._on_chapter_loaded)
        task.run()
        app.processEvents()

        main_text = strip_html(view.text.text())
        fragment_text = "".join(strip_html(label.text())
                                for label, _raw in (view._fragment_text_labels or []))
        if fragment_text:
            main_text = ""  # 片段渲染时 self.text 被隐藏，正文只在片段里
        shown = main_text + fragment_text
        # 阅读器在正文前加了「【第1章】」编号行，真值没有 → 对齐前先去掉
        if shown.startswith("【"):
            shown = shown.split("】", 1)[-1]
        fragments = getattr(chapter, "_cached_image_urls", []) or []
        frag_kinds = {}
        for entry in fragments:
            if isinstance(entry, dict):
                frag_kinds[entry.get("type")] = frag_kinds.get(entry.get("type"), 0) + 1

        truth = zh_convert.convert(
            truth_text(http, source.request_headers(), chapter.url), "zh-cn")
        rate, ctx = compare(shown, truth)
        print("章节：%s" % chapter.url)
        print("渲染路径：%s（片段 %s）"
              % ("片段渲染" if fragment_text else "主正文", frag_kinds or "无"))
        print("上屏字数：%d（主正文 %d + 片段 %d）"
              % (len(shown), len(main_text), len(fragment_text)))
        print("上屏假字：%d" % fake_count(shown))
        print("与站点明文一致率：%.2f%%" % (rate * 100))
        if ctx:
            print("差异：%s" % ctx)
        print("上屏样本：%s" % shown[:60].replace("\n", " "))
        ok = fake_count(shown) == 0 and rate >= 0.99
        return 0 if ok else 1
    finally:
        http.close()
        view.deleteLater()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
