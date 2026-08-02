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
        from urllib.parse import quote

        abs_url = self._abs_url(source, url)
        # 中文参数等需 URL 编码（保留结构字符）
        abs_url = quote(abs_url, safe=":/?=&%#")
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

        优先 api_endpoints（JSON API 站），否则 HTML selector。
        """
        api = source.raw.get("api_endpoints") or {}
        if api.get("discovery") or api.get("search"):
            return self._list_works_api(source, url, page)

        fetch_url = self._build_page_url(source, url, page)
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

    def _list_works_api(self, source: SourceConfig, url: str, page: int = 1) -> List[Work]:
        """API 站（api_endpoints）JSON 解析作品列表。"""
        import json

        api = source.raw.get("api_endpoints") or {}
        cfg = api.get("discovery") or api.get("search") or {}
        if not cfg:
            return []

        # 构造请求 URL（用传入 url 或 api 配置）
        api_url = cfg.get("url") or url
        api_url = str(api_url).replace("{page}", str(page))
        abs_url = self._abs_url(source, api_url)

        # GET JSON
        resp_json = self._http.get_json(
            abs_url,
            headers=self._headers(source),
            timeout=self._timeout(source),
            retries=self._retries(source),
        )

        # 提取列表项
        items = resp_json
        rpath = cfg.get("response_path")
        if rpath:
            items = self._simple_getpath(resp_json, rpath)
        if not isinstance(items, list):
            return []

        item_fields = cfg.get("item_fields") or {}
        works: List[Work] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            title = self._template_value(it, item_fields.get("title"))
            url_v = self._template_value(it, item_fields.get("url"))
            if not title or not url_v:
                continue
            url_v = self._resolve_tpl(source.base_url, url_v)
            works.append(
                Work(
                    title=str(title),
                    url=str(url_v),
                    cover=str(self._template_value(it, item_fields.get("cover")) or ""),
                    source_id=source.source_id,
                    source_name=source.source_name,
                )
            )
        return works

    @staticmethod
    def _simple_getpath(data, path: str):
        """简化的 JSONPath：data.list → data["list"]；data.list.0 → 列表首项。"""
        import re as _re

        cur = data
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return None
        return cur

    @staticmethod
    def _template_value(item: dict, field_spec):
        """字段值：若是模板（含 {}）则填充，否则直接取值。"""
        if field_spec is None:
            return ""
        if isinstance(field_spec, str):
            if "{" in field_spec:
                # 模板如 https://bilibili.com/bangumi/media/md{media_id}
                result = field_spec
                import re as _re

                for m in _re.finditer(r"\{(\w+)\}", field_spec):
                    key = m.group(1)
                    val = item.get(key, "")
                    result = result.replace("{" + key + "}", str(val))
                return result
            return item.get(field_spec, "")
        return ""

    @staticmethod
    def _resolve_tpl(base, url):
        from urllib.parse import urljoin

        if url.startswith("http"):
            return url
        return urljoin(base, url)

    def _build_page_url(self, source: SourceConfig, url: str, page: int) -> str:
        """按源配置 paginator 构造第 page 页的 URL。"""
        import re as _re

        disc = source.get_discovery_config()
        paginator = disc.get("list_paginator") or {}
        ptype = paginator.get("type") or "increment"

        if ptype == "increment":
            # 1) {page} 占位替换
            if "{page}" in url:
                return url.replace("{page}", str(page))
            # 2) page_placeholder 正则：替换 URL 中页码位
            placeholder = paginator.get("page_placeholder")
            if placeholder:
                return _re.sub(placeholder, str(page), url, count=1)
            # 3) 默认：?param=N
            param = paginator.get("param") or "page"
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}{param}={page}"

        if ptype == "cursor":
            # ?offset=N
            param = paginator.get("param") or "offset"
            start = int(paginator.get("start") or 0)
            step = int(paginator.get("step") or 20)
            offset = start + (page - 1) * step
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}{param}={offset}"

        # next_link / 默认：页码参数
        return f"{url}?page={page}"
