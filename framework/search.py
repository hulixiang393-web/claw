"""多源搜索模块（search.py）。

支持：
- 单源搜索 search_one（HTML selector / api_endpoints JSON）
- 跨源并发 search_type
- 统一 SearchResult 输出

对应 ui-search.md 与 core.md「search.py」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import quote, urljoin

from .config import SourceConfig
from .discovery import Discovery
from .errors import SourceError
from .http import HttpClient
from .parser import Parser

log = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """一条统一格式化的搜索结果。"""

    title: str
    url: str
    source_id: str = ""
    source_name: str = ""
    cover: str = ""
    author: str = ""
    update: str = ""

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "cover": self.cover,
            "author": self.author,
            "update": self.update,
        }


class Search:
    def __init__(
        self,
        http: HttpClient,
        parser: Parser,
        discovery: Optional[Discovery] = None,
    ):
        self._http = http
        self._parser = parser
        self._discovery = discovery

    # ------------------------------------------------------------------ #
    def search_one(self, source: SourceConfig, keyword: str) -> List[SearchResult]:
        """单源搜索。优先 api_endpoints.search，否则 endpoints.search。"""
        api = source.raw.get("api_endpoints") or {}
        if api.get("search"):
            return self._search_api(source, keyword)
        return self._search_html(source, keyword)

    def search_type(
        self, sources: List[SourceConfig], keyword: str
    ) -> List[SearchResult]:
        """跨源搜索（串行），合并结果。单源失败不影响其他。"""
        results: List[SearchResult] = []
        for source in sources:
            try:
                results.extend(self.search_one(source, keyword))
            except Exception as exc:
                log.warning("[%s] 搜索失败: %s", source.source_id, exc)
        return results

    # ------------------------------------------------------------------ #
    def _search_html(self, source: SourceConfig, keyword: str) -> List[SearchResult]:
        """HTML 站搜索（endpoints.search）。"""
        search_cfg = source.get_search_config()
        if not search_cfg.get("item") or not search_cfg.get("item", {}).get("fields"):
            return []
        base_url = search_cfg.get("base_url") or source.base_url
        method = search_cfg.get("method") or "GET"
        kw_param = search_cfg.get("keyword_param") or "keyword"

        # URL 构造（GET 拼 keyword，POST 放 body）
        if method == "POST":
            abs_url = urljoin(source.base_url, base_url)
            body = {kw_param: keyword}
            # 需要 http 支持 post_form
            text = self._http_post_form(source, abs_url, body)
        else:
            sep = "&" if "?" in base_url else "?"
            abs_url = urljoin(source.base_url, f"{base_url}{sep}{kw_param}={quote(keyword)}")
            text = self._http_get(source, abs_url)

        doc = self._parser.parse(text)
        item_cfg = search_cfg.get("item") or {}
        root_sel = item_cfg.get("root_selector")
        fields = item_cfg.get("fields") or {}
        if not root_sel:
            return []
        items = self._parser.parse_items(doc, root_sel, fields, source.base_url)
        results = []
        for it in items:
            title = it.get("title", "")
            url = it.get("url", "")
            if not title or not url:
                continue
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    source_id=source.source_id,
                    source_name=source.source_name,
                    cover=it.get("cover", ""),
                    author=it.get("author", ""),
                    update=it.get("update", ""),
                )
            )
        return results

    def _search_api(self, source: SourceConfig, keyword: str) -> List[SearchResult]:
        """API 站搜索（api_endpoints.search）。"""
        api = source.raw.get("api_endpoints") or {}
        cfg = api.get("search") or {}
        if not cfg:
            return []
        api_url = str(cfg.get("url") or "").replace("{keyword}", quote(keyword))
        abs_url = urljoin(source.base_url, api_url)
        resp = self._http.get_json(
            abs_url,
            headers=source.transports().get("headers") or {},
            timeout=float(source.transports().get("timeout") or 10),
        )
        items = resp
        rpath = cfg.get("response_path")
        if rpath:
            items = self._simple_getpath(resp, rpath)
        if not isinstance(items, list):
            return []
        item_fields = cfg.get("item_fields") or {}
        results = []
        for it in items:
            if not isinstance(it, dict):
                continue
            title = self._tpl(it, item_fields.get("title"))
            url = self._tpl(it, item_fields.get("url"))
            if not title or not url:
                continue
            url = urljoin(source.base_url, url)
            results.append(
                SearchResult(
                    title=str(title),
                    url=str(url),
                    source_id=source.source_id,
                    source_name=source.source_name,
                    cover=str(self._tpl(it, item_fields.get("cover")) or ""),
                )
            )
        return results

    # ------------------------------------------------------------------ #
    def _http_get(self, source: SourceConfig, url: str) -> str:
        return self._http.get_text(
            url,
            headers=source.transports().get("headers") or {},
            timeout=float(source.transports().get("timeout") or 10),
            retries=int(source.transports().get("retries") or 3),
        )

    def _http_post_form(self, source: SourceConfig, url: str, data: dict) -> str:
        from urllib.parse import urlencode

        return self._http.post_form(
            url,
            form_data=data,
            headers=source.transports().get("headers") or {},
            timeout=float(source.transports().get("timeout") or 10),
        )

    @staticmethod
    def _simple_getpath(data, path: str):
        cur = data
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return None
        return cur

    @staticmethod
    def _tpl(item: dict, spec):
        if spec is None:
            return ""
        if isinstance(spec, str) and "{" in spec:
            import re

            result = spec
            for m in re.finditer(r"\{(\w+)\}", spec):
                key = m.group(1)
                result = result.replace("{" + key + "}", str(item.get(key, "")))
            return result
        return item.get(spec, "") if isinstance(spec, str) else ""
