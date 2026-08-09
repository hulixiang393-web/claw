"""书架服务层（shelf_service.py）。

统一书架 API：聚合 本地已下载扫描 + 在线收藏 + 续读记忆 + 隐藏名单。
UI 层只调本服务，不再直接操作文件/存储。

v2 重写要点：
- **本地优先 + 合并去重**：同一本书本地已下载且也收藏 → 合并成一条
  （kind=local，附在线 source_id/url/cover），点开直接读本地（零网络）。
- **本地视频可播**：本地视频条目带集文件列表（episode_paths），供 UI 直接播放本地 mp4。
- **搜索 / 排序**：按关键词过滤，按 最近阅读/最近添加/书名 排序。
- 保留既有能力：epub 类型检测缓存、隐藏名单、收藏/收藏夹操作。

纯 Python 无 Qt 依赖：可在后台线程调用，可单元测试。
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Optional

# epub 类型检测函数（注入便于测试/替换）：path -> str
EpubTypeDetector = Callable[[str], str]

VIDEO_EXTS = (".mp4", ".flv", ".webm", ".mkv", ".mov", ".m4v", ".ts", ".m4a")


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
    """书架统一条目模型（本地优先，可带在线对应信息）。

    key：唯一键（本地=epub 文件路径或首个视频路径，收藏=url）。
    kind：local | favorite。kind=local 且 online=True 表示本地+收藏合并。
    """

    key: str
    kind: str
    title: str
    content_type: str
    path: str = ""
    url: str = ""
    source_id: str = ""
    source_name: str = ""
    author: str = ""
    cover: str = ""
    tags: list = field(default_factory=list)
    folder: str = ""
    episode_count: int = 0
    episode_paths: list = field(default_factory=list)  # 本地视频集文件列表
    resume_title: str = ""
    online: bool = False          # 本地条目是否有在线收藏对应
    size_bytes: int = 0
    mtime: float = 0.0            # 本地文件最近修改时间（下载时间近似）
    updated_at: str = ""          # 最近阅读/收藏时间（排序用）
    missing: bool = False

    def to_rec(self) -> dict:
        """转旧版 UI rec dict（兼容过渡期渲染层）。"""
        rec = asdict(self)
        rec.pop("missing", None)
        return rec


def _norm_title(s: str) -> str:
    """书名归一化（去空白/全半角/大小写），用于本地与收藏合并匹配。"""
    s = re.sub(r"\s+", "", s or "")
    s = s.strip()
    low = s.lower()
    full = {"　": " ", "：": ":", "（": "(", "）": ")"}
    for a, b in full.items():
        low = low.replace(a, b)
    return low


class ShelfService:
    """书架统一服务：本地扫描 + 收藏 + 续读 + 隐藏名单聚合（v2）。"""

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
        self._data_dir = (
            Path(data_dir) if data_dir is not None
            else Path(output_dir).parent / "data"
        )
        self._store = library_store
        self._progress = reading_progress
        self._detect = epub_detector
        self._hidden_file = self._data_dir / hidden_file_name
        self._meta_file = self._data_dir / meta_file_name
        self._type_cache: dict = {}
        self._lock = threading.Lock()
        self._load_meta()

    # ------------------------------------------------------------------ #
    # 顶层 API
    # ------------------------------------------------------------------ #
    def list_items(
        self,
        content_type: str = "",
        tag: str = "",
        folder: str = "全部",
        keyword: str = "",
        sort: str = "recent",
    ) -> list[ShelfItem]:
        """书架全部条目：本地 + 收藏，**本地/收藏同名合并成一条（本地优先）**。

        - content_type 空=全部；tag 空=全部标签；folder="全部"=不过滤收藏夹
        - keyword 非空 → 标题模糊匹配（书架搜索）
        - sort：recent（最近阅读/添加）/ added（最近添加）/ name（书名）
        """
        locals_ = self.scan_local()
        favs = self.favorites(folder=folder)
        merged = self._merge(locals_, favs)

        # 收藏夹筛选（合并条目已带收藏夹；本地未收藏的书在具体收藏夹下不显示）
        if folder != "全部":
            merged = [i for i in merged if (i.folder or "") == folder]
        if content_type:
            merged = [i for i in merged if i.content_type == content_type]
        if tag:
            merged = [i for i in merged if tag in i.tags]
        if keyword:
            kw = _norm_title(keyword)
            merged = [i for i in merged if kw in _norm_title(i.title)]
        return self._sort_items(merged, sort)

    def _merge(self, locals_: list, favs: list) -> list:
        """本地优先合并：同名（归一化）本地+收藏 → 一条本地条目带在线信息。"""
        local_by_norm: dict = {}
        for l in locals_:
            local_by_norm.setdefault(_norm_title(l.title), l)
        fav_by_norm: dict = {}
        for f in favs:
            fav_by_norm.setdefault(_norm_title(f.title), f)

        out: list = []
        seen: set = set()
        for l in locals_:
            key = _norm_title(l.title)
            f = fav_by_norm.get(key)
            if f is not None:
                # 合并在线信息到本地条目（本地优先：读本地）
                l.online = True
                l.url = f.url or l.url
                l.source_id = f.source_id or l.source_id
                l.folder = f.folder or l.folder
                l.author = l.author or f.author
                l.cover = l.cover or f.cover
                if not l.tags:
                    l.tags = list(f.tags)
                l.updated_at = f.updated_at or l.updated_at
            out.append(l)
            seen.add(key)
        # 纯收藏（无本地文件）
        for f in favs:
            key = _norm_title(f.title)
            if key not in seen:
                out.append(f)
        return out

    def _sort_items(self, items: list, sort: str) -> list:
        if sort == "name":
            return sorted(items, key=lambda i: (_norm_title(i.title), i.kind))
        if sort == "added":
            return sorted(
                items,
                key=lambda i: (i.mtime or 0, i.updated_at or ""),
                reverse=True,
            )
        # recent：按最近阅读/收藏/下载时间倒序
        def _recency(i: ShelfItem) -> float:
            if i.updated_at:
                try:
                    return time.mktime(time.strptime(i.updated_at, "%Y-%m-%dT%H:%M:%S"))
                except (ValueError, OSError):
                    pass
            return i.mtime or 0.0
        return sorted(items, key=_recency, reverse=True)

    # ------------------------------------------------------------------ #
    # 本地扫描
    # ------------------------------------------------------------------ #
    def scan_local(self) -> list[ShelfItem]:
        """扫描本地已下载目录（epub + 视频目录），排除隐藏名单。

        - {书名}/*.epub → 小说/漫画（类型走缓存检测），记录大小/mtime
        - {书名}/*.mp4|mkv|... → 视频（episode_count + 集文件列表）
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
                try:
                    st = Path(path).stat()
                    size, mtime = st.st_size, st.st_mtime
                except OSError:
                    size, mtime = 0, 0.0
                items.append(ShelfItem(
                    key=path,
                    kind="local",
                    title=sub.name,
                    content_type=self.epub_type(path),
                    path=path,
                    size_bytes=size,
                    mtime=mtime,
                ))
                continue
            vids = sorted(
                f for f in sub.iterdir()
                if f.is_file() and f.suffix.lower() in VIDEO_EXTS
            )
            if not vids:
                continue
            items.append(ShelfItem(
                key=str(sub),          # 视频书 key=目录路径（代表整本，续读/去重用）
                kind="local",
                title=sub.name,
                content_type="video",
                path=str(sub),         # 视频书路径=目录
                episode_count=len(vids),
                episode_paths=[str(v) for v in vids],
                size_bytes=sum((v.stat().st_size if v.is_file() else 0) for v in vids),
                mtime=max((v.stat().st_mtime if v.is_file() else 0.0) for v in vids),
            ))
        # 补续读位置（本地=epub 路径 key；视频=目录路径 key）
        if self._progress is not None:
            for it in items:
                pres = self._progress.resume(it.key)
                if pres:
                    it.resume_title = pres.get("chapter_title", "")
                    it.updated_at = pres.get("updated_at", "")
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
                updated_at=fav.get("favorited_at", ""),
            )
            if self._progress is not None:
                pres = self._progress.resume(it.key)
                if pres:
                    it.resume_title = pres.get("chapter_title", "")
                    it.updated_at = pres.get("updated_at", "") or it.updated_at
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
