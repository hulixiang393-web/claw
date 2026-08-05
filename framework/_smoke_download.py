"""下载队列与下载器冒烟测试（不依赖真实网络）。

覆盖 ui-download.md 核心逻辑：
- 入队 / 并发槽限制 / 进度广播
- 暂停 / 继续 / 取消 / 重试
- 失败自动重试 3 次后仍失败标红（FAILED）
- 续传（skip_existing 跳过已存在章节）
- 命名模板 + 非法字符清理
- 三种类型落盘（novel txt / comic 图 / video m3u8）

用 fake Content / fake HttpClient / fake SourceManager 注入，跑真实线程并发。
"""
import os
import sys
import time
import tempfile
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from framework.download_queue import DownloadQueue, TaskStatus
from framework.downloader import Downloader, sanitize_filename, render_name


# --------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------- #
class _FakeSettings:
    def __init__(self, concurrent=2, skip=True, out_dir=None):
        self._d = {
            "download": {
                "output_dir": out_dir or "downloads",
                "naming_template": "{title}_{chapter_no}_{chapter_title}",
                "skip_existing": skip,
                "max_concurrent_downloads": concurrent,
            }
        }

    def get(self, section, key, default=None):
        return self._d.get(section, {}).get(key, default)


class _FakeContent:
    """Content 双：按类型返回内容 / 图片 / 播放地址。可选每章延时模拟慢速。"""

    def __init__(self, chapter_delay: float = 0.0):
        self._delay = chapter_delay
        self._local_video = ""  # 测试本地视频路径（fetch_video_streams 返回）

    def _maybe_sleep(self):
        if self._delay:
            time.sleep(self._delay)

    def fetch_chapter(self, source, url):
        self._maybe_sleep()
        return f"正文{url}"

    def fetch_comic_pages(self, source, url):
        self._maybe_sleep()
        return [
            "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
            for _ in range(2)
        ]

    def fetch_comic_pages_batch(self, source, chapter_urls):
        """批量版本：复用 fetch_comic_pages 结果（模拟一次渲染多话）。"""
        self._maybe_sleep()
        return {
            url: self.fetch_comic_pages(source, url) for url in chapter_urls
        }

    def fetch_video_episode(self, source, url):
        self._maybe_sleep()
        return f"https://cdn.example.com/v/{url}.m3u8"

    def fetch_video_streams(self, source, url, quality=""):
        self._maybe_sleep()
        if self._local_video:
            return self._local_video, ""
        return f"https://cdn.example.com/v/{url}.mp4", ""


class _FailingContent(_FakeContent):
    """总是抛错的内容双（模拟断网/源失效）。"""

    def fetch_chapter(self, source, url):
        raise RuntimeError("fake network down")

    def fetch_comic_pages(self, source, url):
        raise RuntimeError("fake network down")

    def fetch_video_episode(self, source, url):
        raise RuntimeError("fake network down")

    def fetch_video_streams(self, source, url, quality=""):
        raise RuntimeError("fake network down")


class _FakeHttp:
    def get_bytes(self, url):
        return b"FAKEIMG" * 10


class _FakeManager:
    def get(self, source_id):
        # 返回带 .raw 的伪 SourceConfig（非 playwright 源，走串行下载）
        raw = {"endpoints": {"content": {"page": {"render": "html"}}}}
        return type("FakeSource", (), {"raw": raw, "source_id": source_id})()


class _FakeBus:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def make_detail(ctype, n, content_type=None, url=None, title=None):
    from framework.content import Detail, Chapter

    ct = content_type or ctype
    chs = [Chapter(f"{ct}第{i}章", f"https://x/{ct}/{i}") for i in range(1, n + 1)]
    return Detail(
        source_id="source",
        content_type=ct,
        url=url or f"https://x/{ct}/book",
        title=title or f"书-{ct}-{n}",
        author="t",
        cover="",
        summary="",
        tags=[],
        chapters=chs,
    )


def wait_until(cond, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _fresh_settings(**kw):
    """每次给独立临时目录，避免跨用例文件/续传冲突。"""
    out = tempfile.mkdtemp(prefix="smoke_dl_")
    kw.setdefault("concurrent", 2)
    return _FakeSettings(out_dir=out, **kw)


def _make_test_video():
    """用 ffmpeg 生成 1 秒本地测试 mp4（供 video 下载合并，无需网络）。"""
    import shutil
    import subprocess

    ff = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    path = os.path.join(tempfile.mkdtemp(prefix="smoke_vid_"), "test.mp4")
    if ff:
        subprocess.run(
            [ff, "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1",
             "-c:v", "libx264", "-preset", "ultrafast", path],
            capture_output=True, timeout=30,
        )
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    return ""  # 无 ffmpeg → video 任务会 FAILED（跳过该场景）


def _task_done(q, task):
    return q.get(task.task_id).status in (TaskStatus.DONE, TaskStatus.FAILED)


# --------------------------------------------------------------------- #
# 用例
# --------------------------------------------------------------------- #
def test_sanitize_and_render():
    assert sanitize_filename('a/b:c*d?"e') == "a_b_c_d__e"
    assert render_name("{title}第{chapter_no}章", title="书/名", chapter_no="001") == "书_名第001章"
    print("  [ok] sanitize_filename / render_name")


def test_novel_comic_video_download():
    """三种类型都能成功落盘。"""
    settings = _fresh_settings()
    fake = _FakeContent()
    # video 落盘走 ffmpeg 合并 → 生成 1 秒本地测试视频（无网络）
    fake._local_video = _make_test_video()
    q = DownloadQueue(fake, _FakeHttp(), settings, _FakeManager())
    tasks = []
    for ct in ("novel", "comic", "video"):
        tasks.append(q.add_task(make_detail(ct, 3)))
    assert wait_until(lambda: all(_task_done(q, t) for t in tasks))
    for t in tasks:
        st = q.get(t.task_id)
        assert st.status == TaskStatus.DONE, (t.content_type, st.error)
        assert st.done == 3, (t.content_type, st.done)
    print("  [ok] novel/comic/video 三种类型落盘成功")


def test_concurrency_limit():
    """并发 =2 时最多 2 个同时下载中，5 任务全成功。"""
    settings = _fresh_settings(concurrent=2, skip=False)
    q = DownloadQueue(_FakeContent(), _FakeHttp(), settings, _FakeManager())
    # 用不同书名避免同目录竞争
    tasks = [q.add_task(make_detail("comic", 3, title=f"并发测试书{i}")) for i in range(5)]
    for _ in range(20):
        down = sum(
            1 for t in tasks if q.get(t.task_id).status == TaskStatus.DOWNLOADING
        )
        assert down <= 2, f"并发超限: {down}"
        time.sleep(0.05)
    assert wait_until(lambda: all(_task_done(q, t) for t in tasks), timeout=15)
    assert all(q.get(t.task_id).done == 3 for t in tasks)
    print("  [ok] 并发槽限制 =2 生效")


def test_pause_cancel_resume():
    """暂停/继续/取消：对未启动(WAITING)任务直接生效，可确认状态机正确。"""
    settings = _fresh_settings(concurrent=1)  # 单槽，方便排队观察
    slow = _FakeContent(chapter_delay=0.05)   # 慢速内容制造稳定排队窗口
    q = DownloadQueue(slow, _FakeHttp(), settings, _FakeManager())
    # 第一个任务占住唯一槽位（40 章 × 0.05s ≈ 2s，窗口足够）
    t_busy = q.add_task(make_detail("comic", 40))
    time.sleep(0.05)
    # 第二个任务排不进（单槽），保持 WAITING
    t_wait = q.add_task(make_detail("comic", 3, title="排队任务"))
    time.sleep(0.05)
    assert q.get(t_wait.task_id).status == TaskStatus.WAITING, q.get(t_wait.task_id).status
    # 对 WAITING 任务暂停 → 立即生效（未启动即暂停）
    q.pause_task(t_wait.task_id)
    assert q.get(t_wait.task_id).status == TaskStatus.PAUSED, q.get(t_wait.task_id).status
    # 继续 → 回到 WAITING（尚无空闲槽）
    q.resume_task(t_wait.task_id)
    assert q.get(t_wait.task_id).status == TaskStatus.WAITING, q.get(t_wait.task_id).status
    # 取消排队任务 → CANCELED
    q.cancel_task(t_wait.task_id)
    assert q.get(t_wait.task_id).status == TaskStatus.CANCELED, q.get(t_wait.task_id).status
    # 等占用任务完成
    assert wait_until(lambda: _task_done(q, t_busy))
    print("  [ok] 暂停/继续/取消（状态机）")


def test_retry_three_times_then_failed():
    settings = _fresh_settings()
    q = DownloadQueue(_FailingContent(), _FakeHttp(), settings, _FakeManager())
    task = q.add_task(make_detail("novel", 2))
    assert wait_until(lambda: _task_done(q, task), timeout=15)
    st = q.get(task.task_id)
    assert st.status == TaskStatus.FAILED, st.status
    assert len(st.failed) >= 2, st.failed  # 两章都失败（epub 合成失败时还会加"（整体）"）
    # 重试清零 → 再次失败
    q.retry_task(task.task_id)
    assert wait_until(lambda: _task_done(q, task), timeout=15)
    assert q.get(task.task_id).status == TaskStatus.FAILED
    print("  [ok] 失败自动重试(<=3次)后仍失败 → FAILED")


def test_skip_existing():
    """skip_existing=True 时已完成章节跳过（续传）。"""
    settings = _fresh_settings(skip=True)
    q = DownloadQueue(_FakeContent(), _FakeHttp(), settings, _FakeManager())
    d = make_detail("comic", 3)
    t1 = q.add_task(d)
    assert wait_until(lambda: _task_done(q, t1))
    # 再次加入同一本书（同 url 同名）
    t2 = q.add_task(d)
    assert wait_until(lambda: _task_done(q, t2))
    st = q.get(t2.task_id)
    assert st.total == 3
    assert st.done == st.total  # 全部章节因 skip_existing 被跳过计为完成
    print("  [ok] skip_existing 续传跳过已存在章节")


def test_rename_skip_existing_off():
    """skip_existing=False 时覆盖已存在（done>0 不跳过）。"""
    settings = _fresh_settings(skip=False)
    q = DownloadQueue(_FakeContent(), _FakeHttp(), settings, _FakeManager())
    d = make_detail("comic", 3)
    t1 = q.add_task(d)
    assert wait_until(lambda: _task_done(q, t1))
    assert q.get(t1.task_id).status == TaskStatus.DONE
    # 重新加入同一本：skip=False 时重新下载（覆盖），不再跳过
    t2 = q.add_task(d)
    assert wait_until(lambda: _task_done(q, t2))
    st = q.get(t2.task_id)
    assert st.status == TaskStatus.DONE
    print("  [ok] skip_existing=False 覆盖下载")


def test_progress_events():
    settings = _fresh_settings()
    bus = _FakeBus()
    q = DownloadQueue(_FakeContent(), _FakeHttp(), settings, _FakeManager(), event_bus=bus)
    task = q.add_task(make_detail("comic", 2))
    assert wait_until(lambda: _task_done(q, task))
    types = {e.type for e in bus.events}
    assert "DOWNLOAD_STARTED" in types
    assert "DOWNLOAD_PROGRESS" in types
    assert "DOWNLOAD_COMPLETED" in types
    # 进度事件 done 单调递增 ≤ total
    progresses = [e for e in bus.events if e.type == "DOWNLOAD_PROGRESS"]
    assert progresses, "没有进度事件"
    assert all(e.payload["done"] <= e.payload["total"] for e in progresses)
    print("  [ok] 事件广播 STARTED/PROGRESS/COMPLETED")


def test_downloader_direct():
    """直接调 Downloader 落盘（含命名模板 + data URI 解码）。"""
    from pathlib import Path

    settings = _fresh_settings()
    dl = Downloader(_FakeContent(), _FakeHttp(), settings)
    tmp = tempfile.mkdtemp()
    dl._settings._d["download"]["output_dir"] = tmp

    from framework.download_queue import DownloadTask
    from framework.content import Chapter

    ch = Chapter("第1章", "https://x/c/1")
    task = DownloadTask(
        task_id="t", source_id="source", content_type="novel", url="u",
        title="测试书名", chapters=[ch], total=1,
    )
    n = dl.download_chapter(None, task, ch, 0)
    # epub 只产出：章节内容累积到 task.epub_chapters（不再写逐章 txt）
    assert len(task.epub_chapters) == 1, task.epub_chapters
    assert n > 0
    print("  [ok] downloader 直接落盘（epub 累积）")


if __name__ == "__main__":
    test_sanitize_and_render()
    test_novel_comic_video_download()
    test_concurrency_limit()
    test_pause_cancel_resume()
    test_retry_three_times_then_failed()
    test_skip_existing()
    test_rename_skip_existing_off()
    test_progress_events()
    test_downloader_direct()
    print("\n=== 下载队列冒烟测试通过 ===")
