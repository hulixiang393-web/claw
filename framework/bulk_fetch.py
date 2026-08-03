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
        """抓取一个分类的所有页（软上限 max_pages）。"""
        works: List[Work] = []
        for page in range(1, self._max_pages + 1):
            try:
                page_works = self._discovery.list_works(source, url, page)
            except SourceError:
                break
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
