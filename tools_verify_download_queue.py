"""下载队列修复验证脚本（离屏，不真实下载）。

覆盖修复清单第 1/2/3 条：
- A: 视频章下载中被暂停中断（MergePaused）→ resume 后被中断章重下，done==total，不丢章
- B: 多章下载中暂停 → resume（worker 存活）继续，done 不翻倍、章节不重复
- C: 暂停后 worker 已结束（dispatched=False）→ resume 重派发，依赖 done_chapters
     跳过已完成章，计数补正、不翻倍
- D: dispatched=True（worker 已派发未开始，QRunnable 启动窗口）时 resume → 直接恢复
     而非重新派发（无双 worker 并发）

用 Fake Downloader 注入（不发起真实网络/ffmpeg）。
"""
import sys
import time
import pathlib
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from framework.download_queue import DownloadQueue, TaskStatus
from framework.ffmpeg_merger import MergePaused
from framework.content import Detail, Chapter


# --------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------- #
class _FakeSettings:
    def __init__(self, concurrent=1, out_dir=None):
        self._d = {
            "download": {
                "output_dir": out_dir or "downloads",
                "naming_template": "{title}_{chapter_no}_{chapter_title}",
                "skip_existing": True,
                "max_concurrent_downloads": concurrent,
            }
        }

    def get(self, section, key, default=None):
        return self._d.get(section, {}).get(key, default)


class _FakeHttp:
    def get_bytes(self, url):
        return b"x" * 16

    def get_text(self, url, headers=None, timeout=10, retries=1):
        raise RuntimeError("no text in verify env")


class _FakeManager:
    def get(self, source_id):
        raw = {"endpoints": {"content": {"page": {"render": "html"}}}}
        return type("FakeSource", (), {"raw": raw, "source_id": source_id})()


class _FakeContent:
    def fetch_video_streams(self, source, url, quality=""):
        return f"https://cdn.example.com/v/{url}.m3u8", ""


class _FakeDownloader:
    """可编程下载器：记录调用；可注入中断/延时。"""

    def __init__(self, out_dir, delay=0.0, interrupt_at=None, interrupt_seen=None):
        self._out = pathlib.Path(out_dir)
        self.calls = []                      # 每次 download_chapter 的 index
        self.delay = delay                   # 每章延时（制造暂停窗口）
        self.interrupt_at = interrupt_at     # 首次下载该 index 时抛 MergePaused
        self.interrupt_seen = interrupt_seen  # Event：进入中断点（供主线程 pause）

    def book_dir(self, task):
        return self._out / "book"

    def download_chapter(self, source, task, chapter, index):
        self.calls.append(index)
        if self.delay:
            time.sleep(self.delay)
        if self.interrupt_at is not None and index == self.interrupt_at \
                and self.calls.count(index) == 1:
            if self.interrupt_seen is not None:
                self.interrupt_seen.set()
            # 等主线程 pause（pause_evt 置位）→ 模拟 ffmpeg 合并被暂停中断
            if task.pause_evt.wait(5):
                raise MergePaused("合并已暂停")
            return 100
        return 100


def make_video_detail(n, title=None):
    chs = [Chapter(f"视频第{i}章", f"https://x/v/{i}") for i in range(1, n + 1)]
    return Detail(
        source_id="source", content_type="video", url="https://x/v/book",
        title=title or f"书-video-{n}", author="t", cover="", summary="",
        tags=[], chapters=chs,
    )


def make_novel_detail(n, title=None):
    chs = [Chapter(f"小说第{i}章", f"https://x/n/{i}") for i in range(1, n + 1)]
    return Detail(
        source_id="source", content_type="novel", url="https://x/n/book",
        title=title or f"书-novel-{n}", author="t", cover="", summary="",
        tags=[], chapters=chs,
    )


def wait_until(cond, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


# --------------------------------------------------------------------- #
# 场景 A：暂停中断当前视频章 → resume 后重下，不丢章
# --------------------------------------------------------------------- #
def verify_interrupted_chapter_redownloaded():
    out = tempfile.mkdtemp(prefix="verify_a_")
    seen = __import__("threading").Event()
    fake = _FakeDownloader(out, interrupt_at=2, interrupt_seen=seen)
    q = DownloadQueue(_FakeContent(), _FakeHttp(), _FakeSettings(out_dir=out), _FakeManager())
    q._downloader = fake
    task = q.add_task(make_video_detail(5))
    assert seen.wait(8), "worker 未到达被中断章"
    q.pause_task(task.task_id)                 # 置位 pause_evt
    assert wait_until(lambda: q.get(task.task_id).status == TaskStatus.PAUSED), "未进入暂停"
    q.resume_task(task.task_id)                # 恢复 → 被中断章应重下
    assert wait_until(lambda: q.get(task.task_id).status == TaskStatus.DONE), \
        f"任务未完成：{q.get(task.task_id).status} {q.get(task.task_id).error}"
    st = q.get(task.task_id)
    assert st.done == st.total == 5, f"done={st.done} total={st.total}（被中断章丢失/计数错）"
    assert len(st.done_chapters) == 5, st.done_chapters
    assert fake.calls.count(2) == 2, f"被中断章应重下：calls={fake.calls}"
    assert len(fake.calls) == 6, f"共 5 章 + 第2章重下：calls={fake.calls}"
    print("  [ok] A: 暂停中断章 resume 后重下，done==total，无丢失无翻倍")


# --------------------------------------------------------------------- #
# 场景 B：章边界暂停 → resume（worker 存活）继续，不翻倍
# --------------------------------------------------------------------- #
def verify_pause_resume_worker_alive():
    out = tempfile.mkdtemp(prefix="verify_b_")
    fake = _FakeDownloader(out, delay=0.05)
    q = DownloadQueue(_FakeContent(), _FakeHttp(), _FakeSettings(out_dir=out), _FakeManager())
    q._downloader = fake
    task = q.add_task(make_video_detail(6, title="书B"))
    assert wait_until(lambda: q.get(task.task_id).done >= 2), "进度未到 2 章"
    q.pause_task(task.task_id)
    assert wait_until(lambda: q.get(task.task_id).status == TaskStatus.PAUSED), "未进入暂停"
    q.resume_task(task.task_id)
    assert wait_until(lambda: q.get(task.task_id).status == TaskStatus.DONE), "任务未完成"
    st = q.get(task.task_id)
    assert st.done == st.total == 6, f"done={st.done} total={st.total}（翻倍或丢失）"
    assert len(st.done_chapters) == 6, st.done_chapters
    assert len(fake.calls) == 6, f"每章只下一次：calls={fake.calls}"
    print("  [ok] B: 章边界暂停→resume，done 不翻倍、章节不重复")


# --------------------------------------------------------------------- #
# 场景 C：暂停后 worker 已结束 → resume 重派发，done_chapters 去重
# --------------------------------------------------------------------- #
def verify_resume_redispatch_skip_done():
    out = tempfile.mkdtemp(prefix="verify_c_")
    fake = _FakeDownloader(out)
    q = DownloadQueue(_FakeContent(), _FakeHttp(), _FakeSettings(out_dir=out), _FakeManager())
    q._downloader = fake
    task = q.add_task(make_video_detail(5, title="书C"))
    assert wait_until(lambda: q.get(task.task_id).status == TaskStatus.DONE), "首次下载未完成"
    st = q.get(task.task_id)
    assert st.done == 5 and len(st.done_chapters) == 5
    n_calls_first = len(fake.calls)            # 首次 = 5 次
    # 模拟：任务已在 PAUSED 且 worker 已结束（dispatched=False），done 被清零
    with q._lock:
        st.status = TaskStatus.PAUSED
        st.dispatched = False
        st.done = 0
        st.bytes_written = 0
    q.resume_task(task.task_id)                # 应重新派发
    assert wait_until(lambda: q.get(task.task_id).status == TaskStatus.DONE), "重派发未完成"
    st2 = q.get(task.task_id)
    assert st2.done == st2.total == 5, f"done={st2.done}（重派发后计数错/翻倍）"
    assert len(st2.done_chapters) == 5, st2.done_chapters
    assert len(fake.calls) == n_calls_first, \
        f"已完成章不应重下：首次{n_calls_first} 次，现在{len(fake.calls)} 次 calls={fake.calls}"
    print("  [ok] C: worker 已结束时 resume 重派发，done_chapters 去重、计数正确")


# --------------------------------------------------------------------- #
# 场景 D：dispatched=True（QRunnable 启动窗口）时 resume 直接恢复，不重复派发
# --------------------------------------------------------------------- #
def verify_resume_dispatched_not_redispatch():
    out = tempfile.mkdtemp(prefix="verify_d_")
    fake = _FakeDownloader(out)
    q = DownloadQueue(_FakeContent(), _FakeHttp(), _FakeSettings(out_dir=out), _FakeManager())
    q._downloader = fake
    task = q.add_task(make_video_detail(4, title="书D"))
    with q._lock:
        st = q.get(task.task_id)
        st.status = TaskStatus.PAUSED     # 模拟：pause 落在派发后、worker 启动前窗口
        st.dispatched = True              # 已派发未完成
        st.worker = None                  # 启动前 is_alive()=False
    q.resume_task(task.task_id)
    assert q.get(task.task_id).status == TaskStatus.DOWNLOADING, \
        f"应直接恢复而非重新派发：{q.get(task.task_id).status}"
    assert wait_until(lambda: q.get(task.task_id).status == TaskStatus.DONE)
    st2 = q.get(task.task_id)
    assert st2.done == st2.total == 4, f"done={st2.done}（双 worker 翻倍）"
    assert len(fake.calls) == 4, f"无双 worker 并发：calls={fake.calls}"
    print("  [ok] D: dispatched=True 时 resume 直接恢复，无双 worker")


# --------------------------------------------------------------------- #
# 场景 E：novel 已完成章节 → worker 结束 + resume 重派发，epub 内容完整不翻倍
# --------------------------------------------------------------------- #
def verify_novel_resume_redispatch_complete():
    from framework.downloader import Downloader

    class _NovelContent:
        def fetch_chapter(self, source, url):
            time.sleep(0.02)
            return f"正文{url}"

        def fetch_video_streams(self, source, url, quality=""):
            return f"https://cdn.example.com/v/{url}.m3u8", ""

    out = tempfile.mkdtemp(prefix="verify_e_")
    dl = Downloader(_NovelContent(), _FakeHttp(), _FakeSettings(out_dir=out), )
    q = DownloadQueue(_NovelContent(), _FakeHttp(), _FakeSettings(out_dir=out), _FakeManager())
    q._downloader = dl
    task = q.add_task(make_novel_detail(5, title="书E"))
    assert wait_until(lambda: q.get(task.task_id).status == TaskStatus.DONE), "首次下载未完成"
    st = q.get(task.task_id)
    assert st.done == 5 and len(st.epub_chapters) == 5
    # 模拟：暂停且 worker 已结束，done 清零 → resume 重派发整本重跑
    with q._lock:
        st.status = TaskStatus.PAUSED
        st.dispatched = False
        st.done = 0
        st.bytes_written = 0
    q.resume_task(task.task_id)
    assert wait_until(lambda: q.get(task.task_id).status == TaskStatus.DONE), "重派发未完成"
    st2 = q.get(task.task_id)
    assert st2.done == st2.total == 5, f"done={st2.done}（翻倍/丢失）"
    titles = [t for t, _ in st2.epub_chapters]
    assert len(titles) == 5 and len(set(titles)) == 5, f"epub 章节翻倍：{titles}"
    assert {f"小说第{i}章" for i in range(1, 6)} == set(titles), f"章节缺失：{titles}"
    print("  [ok] E: novel resume 重派发后 epub 内容完整、无翻倍")


if __name__ == "__main__":
    verify_interrupted_chapter_redownloaded()
    verify_pause_resume_worker_alive()
    verify_resume_redispatch_skip_done()
    verify_resume_dispatched_not_redispatch()
    verify_novel_resume_redispatch_complete()
    print("\n=== 下载队列修复验证通过 ===")
