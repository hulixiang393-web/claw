"""书架服务层（shelf_service.py）。

统一书架 API：聚合 本地已下载扫描 + 在线收藏 + 续读记忆 + 隐藏名单。
UI 层只调本服务，不再直接操作文件/存储。对应 refactor-shelf-player.md S1-S4。

- ShelfItem：统一条目模型（本地/收藏同一结构，消除 UI 层 kind 分支散落）
- 本地扫描：epub 类型检测带缓存（mtime+size 失效，避免每次全量 read_epub）
- 隐藏名单：从书架移除的本地书统一管理
- 纯 Python 无 Qt 依赖：可在后台线程调用，可单元测试
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Optional

# epub 类型检测函数（注入便于测试/替换）：path -> str
EpubTypeDetector = Callable[[str], str]


def _default_epub_detector(path: str) -> str:
    """读 epub 判断类型：含图片的章节 → 漫画，否则小说。失败默认 epub。"""
    try:
        import ebooklib
        from ebooklib import epub

        book = epub.read_epub(path)
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            content = item.get_content()
            if not isinstance(content, bytes):
                content = str(content).encode()
            if b"<img" in content.lower():
                return "comic"
        return "novel"
    except Exception:  # noqa: BLE001
        return "epub"


@dataclass
class ShelfItem:
    """书架统一条目模型。

    key：唯一键（本地=文件路径，收藏=url）。kind：local | favorite。
    missing：收藏条目但本地文件不存在（仅收藏场景可扩展）。
    """

    key: str
    kind: str
    title: str
    content_type: str
    path: str = ""
    url: str = ""
    source_id: str = ""
    author: str = ""
    cover: str = ""
    tags: list = field(default_factory=list)
    folder: str = ""
    episode_count: int = 0
    resume_title: str = ""
    missing: bool = False

    def to_rec(self) -> dict:
        """转旧版 UI rec dict（兼容过渡期渲染层）。"""
        rec = asdict(self)
        rec.pop("missing", None)
        return rec


class ShelfService:
    """书架统一服务：本地扫描 + 收藏 + 续读 + 隐藏名单聚合。"""

    def __init__(
        self,
        output_dir: str | Path,
        library_store=None,
        reading_progress=None,
        data_dir: str | Path | None = None,
        epub_detector: EpubTypeDetector = _default_epub_detector,
        hidden_file_name: str = "hidden_local.json",
        meta_file_name: str = "shelf_meta.json",
    ):
        self._output_dir = Path(output_dir)
        # 数据目录默认与隐藏名单同位置（output_dir 父目录 / data），保持既有数据不迁移
        self._data_dir = (
            Path(data_dir) if data_dir is not None
            else Path(output_dir).parent / "data"
        )
        self._store = library_store
        self._progress = reading_progress
        self._detect = epub_detector
        self._hidden_file = self._data_dir / hidden_file_name
        self._meta_file = self._data_dir / meta_file_name
        # epub 类型缓存：{str(path): {"type": str, "mtime": float, "size": int}}
        self._type_cache: dict = {}
        self._lock = threading.Lock()  # 扫描可能来自后台线程
        self._load_meta()

    # ------------------------------------------------------------------ #
    # 顶层 API
    # ------------------------------------------------------------------ #
    def list_items(
        self,
        content_type: str = "",
        tag: str = "",
        folder: str = "全部",
    ) -> list[ShelfItem]:
        """书架全部条目：本地 + 收藏，附续读位置，按收藏时间/书名排序。

        筛选参数与旧 UI 行为一致：content_type 空=全部；tag 空=全部标签；
        folder="全部"=不过滤收藏夹（本地条目不受收藏夹影响）。
        """
        items = self.scan_local()
        items.extend(self.favorites(folder=folder))
        if content_type:
            items = [i for i in items if i.content_type == content_type]
        if tag:
            items = [i for i in items if tag in i.tags]
        return items

    # ------------------------------------------------------------------ #
    # 本地扫描
    # ------------------------------------------------------------------ #
    def scan_local(self) -> list[ShelfItem]:
        """扫描本地已下载目录（epub + 视频目录），排除隐藏名单。

        - {书名}/*.epub → 小说/漫画（类型走缓存检测）
        - {书名}/N 个 mp4 → 视频（episode_count=N）
        - 其余目录跳过。目录不存在返回空列表。
        """
        hidden = self.hidden_local()
        items: list[ShelfItem] = []
        out = self._output_dir
        if not out.is_dir():
            return items
        for sub in sorted(out.iterdir()):
            if not sub.is_dir() or sub.name in hidden:
                continue
            epubs = list(sub.glob("*.epub"))
            if epubs:
                path = str(epubs[0])
                items.append(ShelfItem(
                    key=path,
                    kind="local",
                    title=sub.name,
                    content_type=self.epub_type(path),
                    path=path,
                ))
                continue
            vids = sorted(sub.glob("*.mp4"))
            if not vids:
                continue
            path = str(vids[0])
            items.append(ShelfItem(
                key=path,
                kind="local",
                title=sub.name,
                content_type="video",
                path=path,
                episode_count=len(vids),
            ))
        # 补续读位置（按本地文件路径 key）
        if self._progress is not None:
            for it in items:
                pres = self._progress.resume(it.key)
                if pres:
                    it.resume_title = pres.get("chapter_title", "")
        return items

    def epub_type(self, path: str) -> str:
        """epub 类型检测（带 mtime+size 缓存，避免重复全量读文件）。"""
        try:
            p = Path(path)
            st = p.stat()
            cached = self._type_cache.get(str(p))
            if cached and cached.get("mtime") == st.st_mtime and cached.get("size") == st.st_size:
                return cached.get("type", "epub")
        except OSError:
            return "epub"
        ctype = self._detect(path)
        if ctype:
            with self._lock:
                self._type_cache[str(p)] = {
                    "type": ctype,
                    "mtime": st.st_mtime,
                    "size": st.st_size,
                }
                self._save_meta()
        return ctype or "epub"

    # ------------------------------------------------------------------ #
    # 收藏
    # ------------------------------------------------------------------ #
    def favorites(self, folder: str = "全部") -> list[ShelfItem]:
        """在线收藏条目（按收藏时间倒序），可选收藏夹过滤。"""
        items: list[ShelfItem] = []
        if self._store is None:
            return items
        for fav in self._store.list_all():
            if folder != "全部" and (fav.get("folder") or "") != folder:
                continue
            it = ShelfItem(
                key=fav.get("url", ""),
                kind="favorite",
                title=fav.get("title") or fav.get("url", ""),
                content_type=fav.get("content_type", ""),
                url=fav.get("url", ""),
                source_id=fav.get("source_id", ""),
                author=fav.get("author", ""),
                cover=fav.get("cover", ""),
                tags=list(fav.get("tags") or []),
                folder=fav.get("folder", ""),
            )
            if self._progress is not None:
                pres = self._progress.resume(it.key)
                if pres:
                    it.resume_title = pres.get("chapter_title", "")
            items.append(it)
        return items

    def all_tags(self) -> list[str]:
        """全部收藏条目的标签并集（去重排序）。"""
        if self._store is None:
            return []
        tags: set[str] = set()
        for fav in self._store.list_all():
            tags.update(str(t) for t in (fav.get("tags") or []))
        return sorted(tags)

    def folders(self) -> list[str]:
        """现有收藏夹名（含空夹，排序）。"""
        return self._store.list_folders() if self._store is not None else []

    def favorite_add(self, source_id: str, url: str, title: str,
                     content_type: str = "", cover: str = "",
                     author: str = "", tags: list | None = None,
                     folder: str = "") -> Optional[dict]:
        """收藏一部作品（url 作唯一 key）。无 store 返回 None。"""
        if self._store is None:
            return None
        return self._store.add(source_id, url, title, content_type,
                               cover, author, tags, folder)

    def favorite_has(self, url: str) -> bool:
        return bool(self._store and self._store.has(url))

    def favorite_remove(self, url: str) -> bool:
        return bool(self._store and self._store.remove(url))

    def favorite_move(self, url: str, folder: str) -> bool:
        return bool(self._store and self._store.set_folder(url, folder))

    def create_folder(self, name: str) -> bool:
        return bool(self._store and self._store.create_folder(name))

    def export_backup(self, path: str | Path):
        """导出书架全部数据（收藏 + 收藏夹）到 JSON，返回落盘路径。"""
        if self._store is None:
            return None
        return self._store.export_backup(path)

    # ------------------------------------------------------------------ #
    # 隐藏名单（本地书从书架移除，不删文件）
    # ------------------------------------------------------------------ #
    def hidden_local(self) -> set:
        """当前被隐藏的本地书名（从书架移除但保留文件）。"""
        try:
            if self._hidden_file.is_file():
                raw = json.loads(self._hidden_file.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    return set(str(x) for x in raw)
        except (OSError, json.JSONDecodeError):
            pass
        return set()

    def hide_local(self, title: str) -> None:
        """把本地书加入隐藏名单。"""
        hidden = set(self.hidden_local())
        if title:
            hidden.add(str(title))
        self._write_hidden(sorted(hidden))

    def show_local(self, title: str) -> None:
        """从隐藏名单移除（恢复显示）。"""
        hidden = set(self.hidden_local())
        hidden.discard(str(title))
        self._write_hidden(sorted(hidden))

    def _write_hidden(self, names: list) -> None:
        try:
            self._hidden_file.parent.mkdir(parents=True, exist_ok=True)
            self._hidden_file.write_text(
                json.dumps(names, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # 类型缓存持久化
    # ------------------------------------------------------------------ #
    def _load_meta(self) -> None:
        try:
            if self._meta_file.is_file():
                raw = json.loads(self._meta_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._type_cache = raw
        except (OSError, json.JSONDecodeError):
            self._type_cache = {}

    def _save_meta(self) -> None:
        try:
            self._meta_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._meta_file.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._type_cache, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(self._meta_file)
        except OSError:
            pass

    def clear_meta(self) -> None:
        """清空类型缓存（数据文件恢复等场景用）。"""
        with self._lock:
            self._type_cache = {}
            try:
                self._meta_file.unlink(missing_ok=True)
            except OSError:
                pass
