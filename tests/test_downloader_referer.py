# -*- coding: utf-8 -*-
"""downloader 章节 URL Referer 透传测试（test_downloader_referer.py）。

覆盖：漫画下载（单章 _download_comic / 批量 download_comic_batch）按当前章节页
URL 透传正文图 Referer（防盗链，manben 等图床校验精确页），字节统一走
HttpClient.get_bytes；data URI 分支短路过网络。全部 mock，不联网。
"""
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from framework.downloader import Downloader


class _FakeHttp:
    def __init__(self):
        self.calls = []

    def get_bytes(self, url, headers=None, **k):
        self.calls.append((url, dict(headers or {})))
        return b"IMG-DATA"


class _FakeSettings:
    def get(self, section, key, default=None):
        return default


class _FakeContent:
    def __init__(self, pages=None):
        self.pages = pages or ["http://img/1.jpg", "http://img/2.jpg"]

    def fetch_comic_pages(self, source, url):
        return self.pages

    def fetch_comic_pages_batch(self, source, urls):
        return {u: ["http://img/a.jpg"] for u in urls}


class _Task:
    content_type = "comic"

    def __init__(self):
        self.epub_chapters = []
        self.image_progress_cb = None


def _downloader(http=None, content=None):
    return Downloader(content or _FakeContent(), http or _FakeHttp(), _FakeSettings())


def test_image_bytes_passes_chapter_referer():
    http = _FakeHttp()
    d = _downloader(http=http)
    raw = d._image_bytes("http://img/1.jpg", "http://c.example/m/1/1.html")
    assert raw == b"IMG-DATA"
    assert http.calls[0][0] == "http://img/1.jpg"
    assert http.calls[0][1]["Referer"] == "http://c.example/m/1/1.html"


def test_image_bytes_data_uri_no_network():
    import base64

    http = _FakeHttp()
    d = _downloader(http=http)
    raw = d._image_bytes("data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8fake").decode())
    assert raw == b"\xff\xd8fake"
    assert not http.calls


def test_download_comic_passes_chapter_referer():
    http = _FakeHttp()
    d = _downloader(http=http)
    task = _Task()
    chapter = SimpleNamespace(url="http://c.example/m/1/1.html", title="第1话")
    d._download_comic(None, task, chapter, 0)
    assert http.calls, "正文图应走 HttpClient.get_bytes"
    assert all(h.get("Referer") == "http://c.example/m/1/1.html" for _, h in http.calls)
    assert task.epub_chapters and task.epub_chapters[0][0] == "第1话"


def test_download_comic_batch_passes_chapter_referer():
    http = _FakeHttp()
    d = _downloader(http=http)
    task = _Task()
    chapters = [
        SimpleNamespace(url="http://c.example/m/1/1.html", title="第1话"),
        SimpleNamespace(url="http://c.example/m/1/2.html", title="第2话"),
    ]
    out = d.download_comic_batch(None, task, chapters, [0, 1])
    assert out == {0: 8, 1: 8}
    assert http.calls == [
        ("http://img/a.jpg", {"Referer": "http://c.example/m/1/1.html"}),
        ("http://img/a.jpg", {"Referer": "http://c.example/m/1/2.html"}),
    ]


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))