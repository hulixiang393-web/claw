"""ShelfService 单元测试（不依赖 Qt，纯文件系统）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from framework.library_store import LibraryStore
from framework.shelf_service import ShelfService, ShelfItem


@pytest.fixture()
def base(tmp_path: Path):
    return tmp_path


def make_epub(path: Path, content_type: str = "novel") -> None:
    """造一个假 epub（类型检测器用假探测器，这里仅需文件存在）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PK\x03\x04fake epub")


def test_scan_local_detects_epub_and_video(base: Path):
    make_epub(base / "dl" / "书A" / "书A.epub")
    make_epub(base / "dl" / "书B" / "书B.epub")
    (base / "dl" / "视频C").mkdir(parents=True, exist_ok=True)
    (base / "dl" / "视频C" / "视频C_001.mp4").write_bytes(b"x")
    (base / "dl" / "视频C" / "视频C_002.mp4").write_bytes(b"x")
    (base / "dl" / "无内容目录").mkdir(parents=True)

    svc = ShelfService(
        output_dir=base / "dl",
        data_dir=base / "data",
        epub_detector=lambda p: "comic" if "B" in p else "novel",
    )
    items = svc.scan_local()
    by_title = {i.title: i for i in items}
    assert set(by_title) == {"书A", "书B", "视频C"}
    assert by_title["书A"].content_type == "novel"
    assert by_title["书B"].content_type == "comic"
    assert by_title["视频C"].content_type == "video"
    assert by_title["视频C"].episode_count == 2
    assert all(i.kind == "local" for i in items)


def test_missing_output_dir_returns_empty(base: Path):
    svc = ShelfService(output_dir=base / "不存在", data_dir=base / "data")
    assert svc.scan_local() == []


def test_epub_type_cache_reuses_detector(base: Path):
    epub = base / "dl" / "书A" / "书A.epub"
    make_epub(epub)
    calls = []

    def detector(p):
        calls.append(p)
        return "novel"

    svc = ShelfService(
        output_dir=base / "dl", data_dir=base / "data", epub_detector=detector
    )
    assert svc.epub_type(str(epub)) == "novel"
    assert svc.epub_type(str(epub)) == "novel"
    assert len(calls) == 1  # 第二次命中缓存


def test_epub_type_cache_invalidated_on_mtime_change(base: Path):
    epub = base / "dl" / "书A" / "书A.epub"
    make_epub(epub)
    calls = []

    def detector(p):
        calls.append(p)
        return "novel"

    svc = ShelfService(
        output_dir=base / "dl", data_dir=base / "data", epub_detector=detector
    )
    assert svc.epub_type(str(epub)) == "novel"
    import os
    import time

    time.sleep(0.01)
    epub.write_bytes(b"PK\x03\x04changed")
    os.utime(epub, (time.time() + 1, time.time() + 1))
    assert svc.epub_type(str(epub)) == "novel"
    assert len(calls) == 2  # mtime 变化 → 重新检测


def test_hidden_local_roundtrip(base: Path):
    svc = ShelfService(output_dir=base / "dl", data_dir=base / "data")
    make_epub(base / "dl" / "书A" / "书A.epub")
    svc.hide_local("书A")
    assert "书A" in svc.hidden_local()
    assert svc.scan_local() == []  # 隐藏后不再出现
    svc.show_local("书A")
    assert svc.scan_local()[0].title == "书A"


def test_favorites_merged_with_local(base: Path):
    store = LibraryStore(base / "lib.json")
    make_epub(base / "dl" / "本地书" / "本地书.epub")
    store.add("src", "http://x/1", "在线书", content_type="novel", tags=["免费"])

    svc = ShelfService(
        output_dir=base / "dl",
        data_dir=base / "data",
        library_store=store,
        epub_detector=lambda p: "novel",
    )
    items = svc.list_items()
    kinds = {i.kind for i in items}
    assert kinds == {"local", "favorite"}
    fav = [i for i in items if i.kind == "favorite"][0]
    assert fav.key == "http://x/1"
    assert fav.tags == ["免费"]


def test_filters(base: Path):
    store = LibraryStore(base / "lib.json")
    store.add("src", "http://x/1", "小说1", content_type="novel", tags=["免费"])
    store.add("src", "http://x/2", "漫画1", content_type="comic", tags=["日更"])
    svc = ShelfService(
        output_dir=base / "dl",
        data_dir=base / "data",
        library_store=store,
    )
    assert len(svc.list_items(content_type="novel")) == 1
    assert len(svc.list_items(tag="日更")) == 1
    assert svc.all_tags() == ["免费", "日更"]
    assert svc.list_items(content_type="novel", tag="日更") == []


def test_favorite_folder_filter(base: Path):
    store = LibraryStore(base / "lib.json")
    store.add("src", "http://x/1", "夹内书", content_type="novel", folder="玄幻")
    store.add("src", "http://x/2", "未归类", content_type="novel")
    svc = ShelfService(
        output_dir=base / "dl", data_dir=base / "data", library_store=store
    )
    assert svc.folders() == ["玄幻"]
    favs = svc.favorites(folder="玄幻")
    assert len(favs) == 1 and favs[0].title == "夹内书"
    assert len(svc.favorites(folder="全部")) == 2


def test_favorite_crud_proxy(base: Path):
    store = LibraryStore(base / "lib.json")
    svc = ShelfService(
        output_dir=base / "dl", data_dir=base / "data", library_store=store
    )
    assert not svc.favorite_has("http://x/1")
    svc.favorite_add("src", "http://x/1", "书", content_type="video")
    assert svc.favorite_has("http://x/1")
    svc.favorite_move("http://x/1", "追番")
    assert svc.favorites(folder="追番")[0].title == "书"
    svc.favorite_remove("http://x/1")
    assert not svc.favorite_has("http://x/1")


def test_resume_title_merged(base: Path):
    class FakeProgress:
        def resume(self, key):
            return {"chapter_title": f"第{len(key)}章"} if key else None

    store = LibraryStore(base / "lib.json")
    store.add("src", "http://x/1", "在线书", content_type="novel")
    svc = ShelfService(
        output_dir=base / "dl",
        data_dir=base / "data",
        library_store=store,
        reading_progress=FakeProgress(),
    )
    fav = svc.favorites()[0]
    assert fav.resume_title == "第10章"


def test_meta_cache_persisted(base: Path):
    epub = base / "dl" / "书A" / "书A.epub"
    make_epub(epub)
    calls = []

    def detector(p):
        calls.append(p)
        return "novel"

    svc = ShelfService(
        output_dir=base / "dl", data_dir=base / "data", epub_detector=detector
    )
    svc.epub_type(str(epub))
    svc2 = ShelfService(
        output_dir=base / "dl", data_dir=base / "data", epub_detector=detector
    )
    assert svc2.epub_type(str(epub)) == "novel"
    assert len(calls) == 1  # 重启服务后缓存文件命中，不再检测


def test_to_rec_roundtrip():
    it = ShelfItem(key="k", kind="local", title="t", content_type="novel",
                   path="/p/a.epub", episode_count=3, resume_title="读到X")
    rec = it.to_rec()
    assert rec["key"] == "k"
    assert rec["episode_count"] == 3
    assert rec["resume_title"] == "读到X"
    assert "missing" not in rec
