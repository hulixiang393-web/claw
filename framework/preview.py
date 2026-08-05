"""源编辑器预览（preview.py）。

对源配置的选择器/搜索/详情做实时验证，供源编辑器实时预览用。
对应 ui-editor.md #2/#3/#4 与 core.md「preview.py」。

用法：
    pv = Preview(http, parser)
    pv.validate_selector(source, url, css, xpath)   # -> 命中项列表
    pv.preview_search(source, keyword)              # -> [SearchResult]
    pv.preview_detail(source, url)                  # -> dict{字段: 值}
"""

from __future__ import annotations

import logging
from typing import List, Optional

from .config import SourceConfig
from .http import HttpClient
from .parser import Parser
from .search import Search

log = logging.getLogger(__name__)


class Preview:
    """源编辑器预览 API。所有方法只读，不落盘、不改配置。"""

    def __init__(self, http: HttpClient, parser: Parser):
        self._http = http
        self._parser = parser

    # ------------------------------------------------------------------ #
    def validate_selector(
        self,
        source: SourceConfig,
        url: str,
        css: str = "",
        xpath: str = "",
    ) -> List[str]:
        """验证选择器：抓 url 页面，返回选择器命中项（文本/属性列表）。

        css/xpath 至少一个；命中失败返回空列表（UI 显示"未命中"）。
        """
        if not css and not xpath:
            return []
        try:
            abs_url = self._abs(source, url)
            html = self._http.get_text(
                abs_url,
                headers=source.request_headers(),
                timeout=float(source.transports().get("timeout") or self._http.defaults.timeout),
                retries=int(source.transports().get("retries") or self._http.defaults.retries),
            )
            doc = self._parser.parse(html)
            nodes = self._parser._query(doc, css or None, xpath or None)
            out = []
            for n in nodes[:20]:  # 预览最多 20 项，避免刷屏
                if hasattr(n, "text_content"):
                    text = (n.text_content() or "").strip()
                    if text:
                        out.append(text[:80])
                elif hasattr(n, "get") and n.get("href"):
                    out.append(str(n.get("href"))[:80])
            return out
        except Exception as exc:  # noqa: BLE001
            log.warning("[preview] 选择器验证失败：%s", exc)
            return []

    # ------------------------------------------------------------------ #
    def preview_search(self, source: SourceConfig, keyword: str) -> list:
        """一键测试搜索：调 Search.search_one，返回 SearchResult 预览。"""
        if not keyword:
            return []
        try:
            searcher = Search(self._http, self._parser)
            return searcher.search_one(source, keyword)
        except Exception as exc:  # noqa: BLE001
            log.warning("[preview] 搜索预览失败：%s", exc)
            return []

    # ------------------------------------------------------------------ #
    def preview_detail(self, source: SourceConfig, url: str) -> dict:
        """一键测试详情：抓详情页，返回各字段解析结果。"""
        if not url:
            return {}
        try:
            from .content import Content

            # checker 占位：预览不做结构自检（源编辑器阶段可能配置不完整）
            class _NoopChecker:
                def check(self, *args, **kwargs):
                    return True

            c = Content(self._http, self._parser, checker=_NoopChecker())
            detail = c.fetch_detail(source, url)
            return {
                "title": detail.title,
                "author": detail.author,
                "cover": detail.cover,
                "status": detail.status,
                "summary": (detail.summary or "")[:200],
                "tags": detail.tags,
                "chapters": len(detail.chapters),
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("[preview] 详情预览失败：%s", exc)
            return {"error": str(exc)}

    # ------------------------------------------------------------------ #
    def _abs(self, source: SourceConfig, url: str) -> str:
        from urllib.parse import urljoin

        if url.startswith(("http://", "https://")):
            return url
        return urljoin(source.base_url, url)
