"""全量抓取（bulk_fetch.py）。

方案 B（仅元数据索引）：遍历源全部分类/列表页，只存作品元数据（title/url/cover）
到本地 JSON 索引，不拉正文/图片。用户点开某本再按需拉。

软上限：每分类最多抓 N 页（默认 20），防爆。
进度经 EventBus 广播（BULK_FETCH_PROGRESS / BULK_FETCH_COMPLETED）。

对应 ui-discover.md「一键全量抓取」。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from .config import SourceConfig
from .discovery import Discovery, Work
from .errors import SourceError
from .events import Event, EventBus, EVENT_BULK_FETCH_PROGRESS, EVENT_BULK_FETCH_COMPLETED

DEFAULT_MAX_PAGES = 20


class BulkFetch:
    def __init__(
        self,
        discovery: Discovery,
        event_bus: Optional[EventBus] = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        index_dir: str | Path = "data",
    ):
        self._discovery = discovery
        self._bus = event_bus
        self._max_pages = max_pages
        self._index_dir = Path(index_dir)

    # ------------------------------------------------------------------ #
    def fetch_all(self, source: SourceConfig) -> dict:
        """全量抓取一个源的作品元数据索引。返回汇总统计。"""
        cats = self._discovery.list_categories(source)
        stats = {"categories": len(cats), "works": 0, "source_id": source.source_id}

        all_works: List[Work] = []
        seen = set()

        # 若没分类，直接抓 discovery.list_url 全站
        urls = [c.url for c in cats] if cats else [self._works_entry(source)]

        total = len(urls)
        for i, url in enumerate(urls, start=1):
            page_works = self._fetch_category(source, url)
            for w in page_works:
                if w.url not in seen:
                    seen.add(w.url)
                    all_works.append(w)
            stats["works"] = len(all_works)
            self._emit_progress(source, i, total, stats["works"])

        self._save_index(source, all_works)
        stats["index_file"] = str(self._index_path(source))
        if self._bus:
            self._bus.emit(
                Event(EVENT_BULK_FETCH_COMPLETED, {**stats, "works": all_works})
            )
        return stats

    # ------------------------------------------------------------------ #
    def _works_entry(self, source: SourceConfig) -> str:
        """无分类时的作品入口 URL。优先 works_list_url（真正的作品列表），
        再回退 list_url / 站点根。"""
        disc = source.get_discovery_config()
        return disc.get("works_list_url") or disc.get("list_url") or source.base_url

    def _fetch_category(self, source: SourceConfig, url: str) -> List[Work]:
        """抓取一个分类的所有页（软上限 max_pages）。

        单页网络失败不中断整个分类：跳过该页继续；连续失败 N 页才停（防死循环）。
        """
        works: List[Work] = []
        consecutive_fail = 0
        for page in range(1, self._max_pages + 1):
            try:
                page_works = self._discovery.list_works(source, url, page)
            except SourceError:
                consecutive_fail += 1
                if consecutive_fail >= 3:
                    break  # 连续 3 页失败，网络/反爬问题，停止该分类
                continue  # 跳过单页失败，继续下一页
            consecutive_fail = 0
            if not page_works:
                break  # 无更多页
            works.extend(page_works)
        return works

    # ------------------------------------------------------------------ #
    def _index_path(self, source: SourceConfig) -> Path:
        return self._index_dir / f"works_index_{source.source_id}.json"

    def _save_index(self, source: SourceConfig, works: List[Work]) -> None:
        self._index_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "source_id": source.source_id,
            "source_name": source.source_name,
            "content_type": source.content_type,
            "works": [w.as_dict() for w in works],
        }
        self._index_path(source).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load_index(self, source: SourceConfig) -> List[Work]:
        """读取已保存的索引（书架/搜索用）。"""
        path = self._index_path(source)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            works = []
            for w in data.get("works", []):
                works.append(
                    Work(
                        title=w.get("title", ""),
                        url=w.get("url", ""),
                        cover=w.get("cover", ""),
                        author=w.get("author", ""),
                        update=w.get("update_info", ""),
                        source_id=w.get("source_id", source.source_id),
                        source_name=w.get("source_name", source.source_name),
                    )
                )
            return works
        except (json.JSONDecodeError, OSError):
            return []

    # ------------------------------------------------------------------ #
    def check_updates(self, source: SourceConfig, pages: int = 2) -> dict:
        """批量更新检测：对比本地索引，找各源「最近更新」里的新作品。

        只抓 discovery 列表前 pages 页（软上限），与本地索引 URL 集合对比，
        返回索引里没有的新作品列表。需先跑过 fetch_all 生成索引，
        否则返回 {error: "未建立索引"}。

        返回：
            {"source_id", "source_name", "new_works": [Work...], "checked": N}
        """
        known = self.load_index(source)
        if not known:
            return {
                "source_id": source.source_id,
                "source_name": source.source_name,
                "new_works": [],
                "error": "未建立索引，请先「抓取全部」",
            }
        known_urls = {w.url for w in known}

        # 抓最近更新列表（发现入口第 1~pages 页）
        entry = self._works_entry(source)
        fresh: List[Work] = []
        for page in range(1, pages + 1):
            try:
                page_works = self._discovery.list_works(source, entry, page)
            except SourceError:
                continue
            if not page_works:
                break
            fresh.extend(page_works)

        new_works = [w for w in fresh if w.url and w.url not in known_urls]
        # 去重（按 url）
        seen: set = set()
        deduped = []
        for w in new_works:
            if w.url in seen:
                continue
            seen.add(w.url)
            deduped.append(w)
        return {
            "source_id": source.source_id,
            "source_name": source.source_name,
            "new_works": deduped,
            "checked": len(fresh),
        }

    # ------------------------------------------------------------------ #
    def _emit_progress(self, source: SourceConfig, done: int, total: int, works: int) -> None:
        if self._bus:
            self._bus.emit(
                Event(
                    EVENT_BULK_FETCH_PROGRESS,
                    {
                        "source_id": source.source_id,
                        "done": done,
                        "total": total,
                        "works": works,
                    },
                    source_id=source.source_id,
                )
            )
