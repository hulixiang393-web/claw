# -*- coding: utf-8 -*-
"""真机预加载探针：小说/漫画的「章节预加载」是否真的预取到了下一章（话）。

走 GUI 的真实路径（Qt，offscreen）：
  小说：NovelView + _LoadChapterTask(load_images=True) → _display_chapter → 预取队列
  漫画：ComicView + _load_episode → _finish_episode_load → _prefetch_future

用法：
    python tools/probe_reader_prefetch.py novel timotxt [ahead] [behind]
    python tools/probe_reader_prefetch.py comic dm5 [ahead] [behind]
输出：预取命中的章（话）下标、耗时、是否命中缓存（`_cached_text` / `_prefetched`）。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

from framework.config import load_source  # noqa: E402
from framework.content import Content  # noqa: E402
from framework.decrypter import Decrypter  # noqa: E402
from framework.discovery import Discovery  # noqa: E402
from framework.http import HttpClient, NetworkDefaults  # noqa: E402
from framework.parser import Parser  # noqa: E402


class NC:
    def _selfcheck_url(self, source):
        return ""

    def check(self, source, url):
        return True


def pump(app, deadline, stop=None):
    """跑事件循环直到 deadline 或 stop() 为真。"""
    while time.time() < deadline:
        app.processEvents()
        if stop is not None and stop():
            return True
        time.sleep(0.05)
    return False


def probe_novel(app, source_id, ahead, behind, timeout_s):
    from gui.pages.reader.novel_tasks import _LoadChapterTask
    from gui.pages.reader.novel_view import NovelView

    source = load_source("sources/%s.json" % source_id)
    http = HttpClient(defaults=NetworkDefaults(timeout=20, retries=1, interval_ms=300))
    content = Content(http, Parser(), NC(), decrypter=Decrypter(http))
    disc = Discovery(http, Parser(), NC())
    try:
        works = disc.list_works(source, source.base_url, 1)
        detail = content.fetch_detail(source, works[0].url)
        view = NovelView(content)
        view._source = source
        view._detail = detail
        view._chapters = list(detail.chapters)
        view._current_idx = 0
        view._show_images = True
        view._mode = "scroll"
        view.set_prefetch_config(True, ahead, behind)
        print("[novel] %s 共 %d 章，当前 0，预取配置 ahead=%d behind=%d"
              % (source_id, len(view._chapters), view._prefetch_ahead,
                 view._prefetch_behind))
        started = time.time()
        task = _LoadChapterTask(content, source, view._chapters[0], load_images=True)
        task.signals.finished.connect(view._on_chapter_loaded)
        task.run()
        targets = list(range(1, min(1 + ahead, len(view._chapters))))
        ok = pump(app, time.time() + timeout_s,
                  stop=lambda: all(getattr(view._chapters[i], "_cached_text", None)
                                   for i in targets) and view._prefetch_idx < 0)
        hit = [i for i in targets
               if getattr(view._chapters[i], "_cached_text", None)]
        print("[novel] %.1fs 完成=%s 预取命中章节 %s（队列剩余 %d，在途 %d）"
              % (time.time() - started, ok, hit, len(view._next_prefetch_queue),
                 view._prefetch_idx))
        for i in hit[:3]:
            sample = (view._chapters[i]._cached_text or "").strip().replace("\n", "")
            print("   #%d %s" % (i, sample[:36]))
        # 向前预加载：直接跳到第 6 章（index 5），前一章 index 4 应由 behind 预取
        if behind and len(view._chapters) > 6:
            jump = 5
            view._load_chapter(jump)
            prev_target = jump - 1
            done = pump(app, time.time() + timeout_s,
                        stop=lambda: bool(getattr(view._chapters[prev_target],
                                                  "_cached_text", None))
                        and view._prev_prefetch_idx < 0)
            print("[novel] 跳到 #%d 后：向前预取 #%d 命中=%s（%.1fs）"
                  % (jump, prev_target,
                     bool(getattr(view._chapters[prev_target], "_cached_text", None)),
                     time.time() - started))
            if not done:
                return 1
        view.deleteLater()
        return 0 if hit else 1
    finally:
        http.close()


def probe_comic(app, source_id, ahead, behind, timeout_s):
    from gui.pages.reader.comic_view import ComicView

    source = load_source("sources/%s.json" % source_id)
    http = HttpClient(defaults=NetworkDefaults(timeout=20, retries=1, interval_ms=300))
    content = Content(http, Parser(), NC(), decrypter=Decrypter(http))
    disc = Discovery(http, Parser(), NC())
    try:
        works = disc.list_works(source, source.base_url, 1)
        detail = content.fetch_detail(source, works[0].url)
        view = ComicView(content)
        view._source = source
        view._detail = detail
        view._chapters = list(detail.chapters)
        view._gen = 0
        view.set_prefetch_config(True, ahead, behind)
        print("[comic] %s 共 %d 话，预取配置 ahead=%d behind=%d"
              % (source_id, len(view._chapters), view._prefetch_count,
                 view._prefetch_back))
        started = time.time()
        view._load_episode(0)
        targets = [view._chapters[i].url for i in
                   range(1, min(1 + ahead, len(view._chapters)))]
        ok = pump(app, time.time() + timeout_s,
                  stop=lambda: all(url in view._prefetched for url in targets)
                  and not view._prefetch_busy)
        hit = [url for url in targets if url in view._prefetched]
        print("[comic] %.1fs 完成=%s 预取命中 %d/%d 话，队列剩余 %d"
              % (time.time() - started, ok, len(hit), len(targets),
                 len(view._prefetch_queue)))
        for url in hit[:3]:
            print("   %s（%d 张图）" % (url[-24:], view._prefetched[url]["count"]))
        view.deleteLater()
        return 0 if hit else 1
    finally:
        http.close()


def main(argv):
    kind = argv[1] if len(argv) > 1 else "novel"
    source_id = argv[2] if len(argv) > 2 else "timotxt"
    ahead = int(argv[3]) if len(argv) > 3 and argv[3].isdigit() else 3
    behind = int(argv[4]) if len(argv) > 4 and argv[4].isdigit() else 1
    timeout_s = 120 if kind == "comic" else 60
    app = QApplication.instance() or QApplication([])
    if kind == "comic":
        return probe_comic(app, source_id, ahead, behind, timeout_s)
    return probe_novel(app, source_id, ahead, behind, timeout_s)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
