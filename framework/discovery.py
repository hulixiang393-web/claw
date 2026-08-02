"""发现模块（discovery.py）。

消费 endpoints.discovery 与 endpoints.search：
- list_categories(source)  → 分类列表（读 discovery.list_item.fields）
- list_works(source, url, page) → 一页作品（读 search.item / discovery.list_item）

分页：支持 {page} 占位符注入 URL（increment 型）；next_link/cursor 后续扩展。
对应 design.md §5.1 与 ui-discover.md。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .config import SourceConfig
from .errors import ContentMissingError
from .http import HttpClient
from .parser import Parser
from .selfcheck import StructureChecker


@dataclass
class Category:
    """一个分类。"""

    title: str
    url: str


@dataclass
class Work:
    """作品列表项（与搜索结果共用字段结构）。"""

    title: str
    url: str
    cover: str = ""
    author: str = ""
    update: str = ""
    source_id: str = ""
    source_name: str = ""

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "cover": self.cover,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "url": self.url,
            "update_info": self.update,
        }


class Discovery:
    def __init__(self, http: HttpClient, parser: Parser, checker: StructureChecker):
        self._http = http
        self._parser = parser
        self._checker = checker

    # ------------------------------------------------------------------ #
    def _headers(self, source: SourceConfig) -> dict:
        return source.transports().get("headers") or {}

    def _timeout(self, source: SourceConfig) -> float:
        return float(source.transports().get("timeout") or 10)

    def _retries(self, source: SourceConfig) -> int:
        return int(source.transports().get("retries") or 3)

    def _interval_ms(self, source: SourceConfig) -> int:
        return int(source.transports().get("interval_ms") or 0)

    def _abs_url(self, source: SourceConfig, url: str) -> str:
        from urllib.parse import urljoin

        return urljoin(source.base_url, url) if not url.startswith("http") else url

    def _get(self, source: SourceConfig, url: str) -> str:
        abs_url = self._abs_url(source, url)
        return self._http.get_text(
            abs_url,
            headers=self._headers(source),
            timeout=self._timeout(source),
            retries=self._retries(source),
            interval_ms=self._interval_ms(source),
        )

    # ------------------------------------------------------------------ #
    def list_categories(self, source: SourceConfig) -> List[Category]:
        """抓取分类列表。未配置分类项 → 返回 []。

        支持 list_item.url_pattern 过滤：只保留 URL 匹配该正则的分类
        （过滤掉首页/logo/作者链接等非分类项）。
        """
        disc = source.get_discovery_config()
        list_item = disc.get("list_item") or {}
        fields = list_item.get("fields") or {}
        if not fields.get("title") or not fields.get("url"):
            return []  # 未配置分类规则 → 无分类

        url_pattern = list_item.get("url_pattern")
        import re as _re

        list_url = disc.get("list_url") or source.base_url
        self._checker.check(source, self._abs_url(source, list_url))
        html = self._get(source, list_url)
        doc = self._parser.parse(html)

        titles = self._parser.extract(doc, fields.get("title"))
        urls = self._parser.extract(doc, fields.get("url"), source.base_url)
        cats: List[Category] = []
        for i, t in enumerate(titles):
            u = urls[i] if i < len(urls) else ""
            if url_pattern and not _re.search(url_pattern, u):
                continue  # 不匹配分类 URL 模式 → 跳过（非真实分类）
            cats.append(Category(title=t, url=u))
        return cats

    # ------------------------------------------------------------------ #
    def list_works(self, source: SourceConfig, url: str, page: int = 1) -> List[Work]:
        """抓取一页作品列表（懒加载用）。

        url 为列表页/分类 URL；page 通过 {page} 占位注入。
        """
        # 分页：若 URL 含 {page} 则替换
        fetch_url = url.replace("{page}", str(page)) if "{page}" in url else url

        self._checker.check(source, self._abs_url(source, fetch_url))
        html = self._get(source, fetch_url)
        doc = self._parser.parse(html)

        # 作品项选择器：优先 search.item，其次 discovery.list_item
        disc = source.get_discovery_config()
        search = source.get_search_config()
        list_item = disc.get("list_item") or {}
        search_item = search.get("item") or {}
        root_sel = search_item.get("root_selector") or list_item.get("root_selector")
        fields = search_item.get("fields") or list_item.get("fields") or {}

        if not root_sel or not fields:
            raise ContentMissingError(
                "源未配置作品列表规则（search.item / discovery.list_item）",
                source_id=source.source_id,
            )

        items = self._parser.parse_items(doc, root_sel, fields, source.base_url)
        works: List[Work] = []
        for it in items:
            if not it.get("title") or not it.get("url"):
                continue
            works.append(
                Work(
                    title=it.get("title", ""),
                    url=it.get("url", ""),
                    cover=it.get("cover", ""),
                    author=it.get("author", ""),
                    update=it.get("update", ""),
                    source_id=source.source_id,
                    source_name=source.source_name,
                )
            )
        return works
