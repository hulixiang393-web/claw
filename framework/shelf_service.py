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
from dataclasses import dataclass, field, asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
    reading_status: str = "reading"

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
        repository=None,
        data_dir: str | Path | None = None,
        epub_detector: EpubTypeDetector = _default_epub_detector,
        hidden_file_name: str = "hidden_local.json",
        meta_file_name: str = "shelf_meta.json",
    ):
        # 输出目录统一解析为绝对路径：扫描产出的本地 key（epub 文件路径/视频目录）
        # 与阅读器存续读时用的绝对路径（app._open_epub 等）保持一致，否则
        # 相对目录配置下书架扫描查不到续读（本地小说/漫画/视频记忆失效）。
        self._output_dir = Path(output_dir).resolve()
        self._data_dir = (
            Path(data_dir) if data_dir is not None
            else Path(output_dir).resolve().parent / "data"
        )
        self._store = library_store
        self._progress = reading_progress
        self._repository = repository
        self._detect = epub_detector
        self._hidden_file = self._data_dir / hidden_file_name
        self._meta_file = self._data_dir / meta_file_name
        self._type_cache: dict = {}
        self._lock = threading.Lock()
        self._generation = 0
        self._item_cache: dict = {}
        self._meta_defer = 0
        self._meta_dirty = False
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
        return self.filter_snapshot(
            (self.scan_local(), self.favorites()), content_type, tag, folder, keyword, sort
        )

    def metadata_snapshot(self, cancel=None, include_progress=False):
        """Return the cheap shelf snapshot used for the first card render.

        Progress is deliberately opt-in here.  Reading it for every item made
        entering the shelf compete with discovery/network work and blocked the
        first useful frame.
        """
        with self._lock:
            self._generation += 1
            generation = self._generation
        locals_ = self.scan_local(
            metadata_only=True,
            cancel=cancel,
            generation=generation,
            include_progress=include_progress,
        )
        if locals_ is None or (cancel is not None and cancel.is_set()):
            return None
        return locals_, self.favorites(include_progress=include_progress)

    def refresh_metadata(self, snapshot):
        locals_ = []
        for item in snapshot[0]:
            summary = self._progress.summary(item.key) if self._progress is not None else None
            locals_.append(replace(item, resume_title=(summary or {}).get("chapter_title", ""),
                                   updated_at=(summary or {}).get("updated_at", "")))
        return locals_, self.favorites()

    def filter_snapshot(self, snapshot, content_type="", tag="", folder="全部",
                        keyword="", sort="recent"):
        locals_, favs = snapshot
        locals_ = [replace(i, tags=list(i.tags), episode_paths=list(i.episode_paths))
                   for i in locals_]
        favs = [replace(i, tags=list(i.tags)) for i in favs
                if folder == "全部" or (i.folder or "") == folder]
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

    @staticmethod
    def _identity(item: ShelfItem):
        source_id = item.source_id or ""
        if item.url:
            parts = urlsplit(item.url.strip())
            host = parts.hostname or ""
            if parts.port not in (None, 80, 443):
                host = f"{host}:{parts.port}"
            query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
            url = urlunsplit((parts.scheme.lower(), host.lower(), parts.path.rstrip("/") or "/", query, ""))
            return source_id, url
        if item.kind == "local":
            return source_id, "local:" + (item.path or item.key or _norm_title(item.title))
        return source_id, "title:" + _norm_title(item.title)

    def _merge(self, locals_: list, favs: list) -> list:
        """本地优先合并：按稳定身份合并，保留输入顺序中的首条记录。"""
        local_by_key: dict = {}
        for local in locals_:
            local_by_key.setdefault(self._identity(local), local)
        fav_by_key: dict = {}
        for favorite in favs:
            fav_by_key.setdefault(self._identity(favorite), favorite)

        out: list = []
        seen: set = set()
        fallback_favorites: dict = {}
        for favorite in fav_by_key.values():
            if favorite.url:
                continue
            fallback_favorites.setdefault(_norm_title(favorite.title), []).append(favorite)
        titled_favorites: dict = {}
        for favorite in favs:
            if favorite.url:
                titled_favorites.setdefault(_norm_title(favorite.title), []).append(favorite)

        for local in local_by_key.values():
            key = self._identity(local)
            favorite = fav_by_key.get(key)
            if favorite is None and not local.url:
                candidates = titled_favorites.get(_norm_title(local.title), [])
                if len(candidates) == 1:
                    favorite = candidates[0]
                    key = self._identity(favorite)
            if favorite is not None:
                local.online = True
                if local.content_type == "epub" and favorite.content_type in ("novel", "comic"):
                    local.content_type = favorite.content_type
                local.reading_status = favorite.reading_status
                local.url = favorite.url or local.url
                local.source_id = favorite.source_id or local.source_id
                local.folder = favorite.folder or local.folder
                local.author = local.author or favorite.author
                local.cover = local.cover or favorite.cover
                if not local.tags:
                    local.tags = list(favorite.tags)
                local.updated_at = favorite.updated_at or local.updated_at
                if not local.resume_title and favorite.resume_title and local.content_type == "video":
                    local.resume_title = favorite.resume_title
            out.append(local)
            seen.add(self._identity(local))
            if favorite is not None:
                seen.add(self._identity(favorite))
        for favorite in fav_by_key.values():
            if self._identity(favorite) not in seen:
                out.append(favorite)
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
        # recent：按最近阅读/收藏/下载时间倒序。
        # 用 DSU（装饰-排序-去装饰）：每个 distinct 时间戳只解析一次，避免原实现在
        # sorted 的每轮比较里都跑 time.strptime（大数据量下放大成明显卡顿，且首次
        # 调用会触发 _strptime 模块懒加载的额外一次性停顿）。解析失败回退 mtime。
        _fmt = "%Y-%m-%dT%H:%M:%S"
        parsed: dict = {}

        def _recency_key(i: ShelfItem) -> float:
            if i.updated_at:
                epoch = parsed.get(i.updated_at)
                if epoch is None:
                    try:
                        epoch = datetime.strptime(i.updated_at, _fmt).timestamp()
                    except (ValueError, OSError):
                        epoch = None
                    parsed[i.updated_at] = epoch
                if epoch is not None:
                    return epoch
            return i.mtime or 0.0

        return sorted(items, key=_recency_key, reverse=True)

    # ------------------------------------------------------------------ #
    # 本地扫描
    # ------------------------------------------------------------------ #
    def scan_local(
        self,
        metadata_only=False,
        cancel=None,
        generation=None,
        include_progress=True,
    ) -> list[ShelfItem] | None:
        """扫描本地已下载目录（epub + 视频目录），排除隐藏名单。

        - {书名}/*.epub → 小说/漫画（类型走缓存检测），记录大小/mtime
        - {书名}/*.mp4|mkv|... → 视频（episode_count + 集文件列表）
        - 其余目录跳过。目录不存在返回空列表。

        增量：以目录签名（mtime_ns）缓存上次结果，未变更目录直接复用元数据，
        仅变更/新增目录重新读取；全量扫描仍是缓存缺失时的回退路径，输出一致。
        generation：若提供，扫描结束时代际已过期（有更新的扫描发起）则返回 None，
        调用方据此丢弃迟到结果；与 cancel 一起保证并发/取消正确。
        """
        hidden = self.hidden_local()
        items: list[ShelfItem] = []
        out = self._output_dir

        def _stale() -> bool:
            return generation is not None and generation != self._generation

        if not out.is_dir():
            return None if _stale() else items
        seen: set = set()
        # 扫描期间暂缓类型缓存落盘：逐本命中缓存不再各自重写整个 meta 文件，
        # 结束时若有变更统一写盘一次（仅变更项进内存缓存）。
        self._meta_defer += 1
        try:
            for sub in sorted(out.iterdir()):
                if _stale():
                    return None
                if cancel is not None and cancel.is_set():
                    return None if generation is not None else []
                if not sub.is_dir() or sub.name in hidden:
                    continue
                seen.add(str(sub))
                try:
                    sig = sub.stat().st_mtime_ns
                except OSError:
                    sig = None
                cached = self._item_cache.get(str(sub))
                if cached is not None and sig is not None and cached[0] == sig:
                    item = self._cached_item_type(cached[1], metadata_only)
                else:
                    item = self._build_local_item(sub, metadata_only)
                    if item is not None and sig is not None:
                        self._item_cache[str(sub)] = (sig, replace(item))
                if item is not None:
                    items.append(item)
            for key in [k for k in self._item_cache if k not in seen]:
                self._item_cache.pop(key, None)
        finally:
            self._meta_defer -= 1
            if self._meta_defer == 0 and self._meta_dirty:
                self._meta_dirty = False
                self._persist_meta()
        if _stale():
            return None
        # 补续读位置（本地=epub 路径 key；视频=目录路径 key）
        if include_progress and self._progress is not None:
            for it in items:
                pres = self._progress.summary(it.key)
                if pres:
                    it.resume_title = pres.get("chapter_title", "")
                    it.updated_at = pres.get("updated_at", "")
        return items

    def _build_local_item(self, sub: Path, metadata_only: bool) -> Optional[ShelfItem]:
        """读单个本地书目录的元数据（epub 或视频），无有效内容返回 None。"""
        epubs = sorted(sub.glob("*.epub"))
        if epubs:
            # 一个文件夹可有多本 epub（同名书名目录下多本/合集）：
            # path 取第一本（默认），episode_paths 记全部，供书架「选本阅读」。
            paths = [str(p) for p in epubs]
            path = paths[0]
            try:
                st = Path(path).stat()
                size, mtime = st.st_size, st.st_mtime
            except OSError:
                size, mtime = 0, 0.0
            return ShelfItem(
                key=path,
                kind="local",
                title=sub.name,
                content_type=(self._type_cache.get(path, {}).get("type", "epub")
                              if metadata_only else self.epub_type(path)),
                path=path,
                episode_count=len(epubs),
                episode_paths=paths,
                size_bytes=size,
                mtime=mtime,
            )
        vids = sorted(
            f for f in sub.iterdir()
            if f.is_file() and f.suffix.lower() in VIDEO_EXTS
        )
        if not vids:
            return None
        return ShelfItem(
            key=str(sub),          # 视频书 key=目录路径（代表整本，续读/去重用）
            kind="local",
            title=sub.name,
            content_type="video",
            path=str(sub),         # 视频书路径=目录
            episode_count=len(vids),
            episode_paths=[str(v) for v in vids],
            size_bytes=sum((v.stat().st_size if v.is_file() else 0) for v in vids),
            mtime=max((v.stat().st_mtime if v.is_file() else 0.0) for v in vids),
        )

    def _cached_item_type(self, tpl: ShelfItem, metadata_only: bool) -> ShelfItem:
        """复用缓存条目，仅按当前类型缓存/检测刷新 content_type（不重扫目录）。"""
        if tpl.content_type == "video":
            return replace(tpl)
        path = tpl.path
        ctype = (self._type_cache.get(path, {}).get("type", tpl.content_type)
                 if metadata_only else self.epub_type(path))
        return replace(tpl, content_type=ctype)

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
            entry = {"type": ctype, "mtime": st.st_mtime, "size": st.st_size}
            with self._lock:
                if self._type_cache.get(str(p)) != entry:
                    self._type_cache[str(p)] = entry
                    self._save_meta()
        return ctype or "epub"

    # ------------------------------------------------------------------ #
    # 收藏
    # ------------------------------------------------------------------ #
    def favorites(self, folder: str = "全部", include_progress=True) -> list[ShelfItem]:
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
                reading_status=fav.get("reading_status", "reading"),
            )
            if include_progress and self._progress is not None:
                pres = self._progress.summary(it.key)
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
            result = None
        else:
            result = self._store.add(source_id, url, title, content_type,
                                     cover, author, tags, folder)
        if self._repository is not None and url:
            self._repository.upsert_book(
                url, source_id, url, content_type,
                {"title": title, "cover": cover, "author": author,
                 "tags": list(tags or []), "folder": folder},
            )
        return result

    def favorite_has(self, url: str) -> bool:
        return bool(self._store and self._store.has(url))

    def favorite_remove(self, url: str) -> bool:
        removed = bool(self._store and self._store.remove(url))
        if self._repository is not None and url:
            try:
                self._repository.clear_content_cache(url)
            except Exception:
                pass
        return removed

    def favorite_clear_folder(self, folder: str) -> int:
        """一键清空某收藏夹内的全部收藏（保留收藏夹，不删本地文件）。返回移除条数。"""
        if self._store is None:
            return 0
        urls = [rec.get("url") for rec in self._store.folder_items(folder)
                if rec.get("url")]
        n = self._store.clear_folder(folder)
        if self._repository is not None:
            for url in urls:
                try:
                    self._repository.clear_content_cache(url)
                except Exception:
                    pass
        return n

    def clear_all_favorites(self, exclude_folders: set[str] | None = None) -> int:
        """清空收藏，可排除指定收藏夹，并同步清理 SQLite 内容缓存。"""
        if self._store is None:
            return 0
        excluded = set(exclude_folders or ())
        urls = [
            rec.get("url") for rec in self._store.list_all()
            if rec.get("url") and (rec.get("folder") or "") not in excluded
        ]
        n = self._store.remove_all(exclude_folders=excluded)
        if self._repository is not None:
            for url in urls:
                try:
                    self._repository.clear_content_cache(url)
                except Exception:
                    pass
        return n

    def favorite_set_reading_status(self, url: str, status: str) -> bool:
        return bool(self._store and self._store.set_reading_status(url, status))

    def favorite_move(self, url: str, folder: str) -> bool:
        return bool(self._store and self._store.set_folder(url, folder))

    def create_folder(self, name: str) -> bool:
        return bool(self._store and self._store.create_folder(name))

    def delete_folder(self, name: str) -> bool:
        return bool(self._store and self._store.delete_folder(name))

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
        """标记类型缓存已变更并落盘；扫描期间暂缓，结束统一写一次。"""
        if self._meta_defer > 0:
            self._meta_dirty = True
            return
        self._persist_meta()

    def _persist_meta(self) -> None:
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
