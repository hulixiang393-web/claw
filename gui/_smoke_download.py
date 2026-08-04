"""下载页 GUI 冒烟测试（纯离线，不触发真实网络 / Playwright）。

用 fake Content / fake HttpClient / fake SourceManager 注入 DownloadQueue，
实例化独立 DownloadPage 验证 UI 渲染、空态切换、卡片、并发、筛选、控制不崩溃。
"""
import os
import sys
import time
import pathlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEventLoop, QTimer


def find_cards(parent):
    from gui.pages.download_page import _TaskCard

    found = []
    for c in parent.findChildren(_TaskCard):
        found.append(c)
    return found


def make_detail(ctype="novel", n=3):
    from framework.content import Detail, Chapter

    chs = [Chapter(f"{ctype}第{i}章", f"https://x/{ctype}/{i}") for i in range(1, n + 1)]
    return Detail(
        source_id="comicbox", content_type=ctype, url=f"https://x/{ctype}/book",
        title=f"离线测试{ctype}", author="t", cover="", status="连载",
        summary="", tags=[], chapters=chs,
    )


def main():
    app = QApplication.instance() or QApplication([])

    from framework.download_queue import DownloadQueue
    from framework.events import EventBus
    from framework.settings_manager import SettingsManager

    # ── Fakes（纯离线，不联网）──
    class _FC:
        def fetch_chapter(self, s, u): return "正文"
        def fetch_comic_pages(self, s, u): return ["data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="]
        def fetch_comic_pages_batch(self, s, urls): return {u: ["data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="] for u in urls}
        def fetch_video_episode(self, s, u): return "https://x/v.m3u8"

    class _FH:
        def get_bytes(self, u): return b"IMG"

    class _FM:
        def get(self, sid): return {"x": 1}

    settings = SettingsManager()
    bus = EventBus()
    queue = DownloadQueue(_FC(), _FH(), settings, _FM(), event_bus=bus)

    from gui.pages.download_page import DownloadPage
    page = DownloadPage(queue=queue, event_bus=bus, settings=settings)
    page.show()
    app.processEvents()

    # 1) 空态
    assert page.stack.currentIndex() == 0, f"空态 index={page.stack.currentIndex()}"
    print("  [ok] 空态显示")

    # 2) 加入任务 → 切列表
    task = queue.add_task(make_detail("novel", 5))
    app.processEvents()
    # 等 worker 完成（fake 瞬间）
    t0 = time.time()
    while time.time() - t0 < 5 and queue.get(task.task_id).status not in ("done", "failed"):
        loop = QEventLoop()
        QTimer.singleShot(50, loop.quit)
        loop.exec()
        app.processEvents()
    app.processEvents()
    assert page.stack.currentIndex() == 1, f"任务列表 index={page.stack.currentIndex()}"
    assert len(find_cards(page)) >= 1, "应有任务卡片"
    print("  [ok] 任务卡片渲染", f"终态={queue.get(task.task_id).status}")

    # 3) 并发调节（走 queue API + 设置持久化）
    orig = queue.concurrent
    queue.set_concurrency(orig + 1)
    assert queue.concurrent == orig + 1
    queue.set_concurrency(orig)
    page.conc_label.setText(str(queue.concurrent))
    print(f"  [ok] 并发 {orig}→{orig+1}→{orig}")

    # 4) 筛选
    for idx in range(2):
        page.status_combo.setCurrentIndex(idx)
        app.processEvents()
    page.status_combo.setCurrentIndex(0)
    page.type_combo.setCurrentIndex(1)  # 小说
    page._full_rebuild()
    app.processEvents()
    print("  [ok] 筛选切换不崩溃")

    # 5) 控制 API
    queue.pause_task(task.task_id)
    queue.resume_task(task.task_id)
    app.processEvents()
    print("  [ok] 暂停/继续")

    print("\n=== 下载页 GUI 冒烟测试通过 ===")


if __name__ == "__main__":
    main()
