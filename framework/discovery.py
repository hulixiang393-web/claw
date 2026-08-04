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
        return source.request_headers()

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

        支持两种来源：
        - list_item.categories 静态列表（API 站无分类 HTML 时硬编码，如 B 站分区）
        - list_item.fields HTML 抓取（普通站），含 url_pattern 过滤
        """
        disc = source.get_discovery_config()
        list_item = disc.get("list_item") or {}

        # 静态分类列表（API 站）：categories = [{title, url}, ...]
        static = list_item.get("categories") or []
        if static:
            cats = []
            seen: set = set()
            for c in static:
                if not isinstance(c, dict) or not c.get("title") or not c.get("url"):
                    continue
                if c["url"] in seen:
                    continue
                seen.add(c["url"])
                cats.append(Category(title=c["title"], url=c["url"]))
            return cats

        fields = list_item.get("fields") or {}
        has_cat_fields = bool(
            fields.get("title")
            and (fields.get("url") or fields.get("data_val") or list_item.get("tag_url_template"))
        )
        if not has_cat_fields:
            return []  # 未配置分类规则 → 无分类

        url_pattern = list_item.get("url_pattern")
        import re as _re

        # 分类可能放在专属分类页而非首页：优先 list_categories_url，
        # 否则 list_url，再回退首页/站点根
        list_url = (
            disc.get("list_categories_url")
            or disc.get("list_url")
            or source.base_url
        )
        self._checker.check(source, self._abs_url(source, list_url))
        html = self._get(source, list_url)
        doc = self._parser.parse(html)

        titles = self._parser.extract(doc, fields.get("title"))
        urls = self._parser.extract(doc, fields.get("url"), source.base_url)
        # 分类项可能只有标签值（data-val）而无 href：用 tag 字段名提取
        # data_val 字段（如 data-val，供 ?tag={val} 构造 URL）
        tag_vals = self._parser.extract(doc, fields.get("data_val"))
        pass_filters = []
        for pat in (url_pattern,):
            if pat:
                pass_filters.append(pat)
        cats: List[Category] = []
        seen_links: set = set()
        for i, t in enumerate(titles):
            u = urls[i] if i < len(urls) else ""
            tv = tag_vals[i] if i < len(tag_vals) else ""
            if pass_filters:
                if u and not _re.search(pass_filters[0], u):
                    continue  # 显式 url 不匹配模式 → 跳过
            # 分类目录条：用 data_val（tag）构造专属分类页 URL
            tag_template = list_item.get("tag_url_template")
            if tv and tag_template:
                u = tag_template.replace("{tag}", tv)
            elif tv and not u:
                # 无 url → 用 list_url + ?tag={val}
                u = f"{disc.get('list_url', '/booklist')}?tag={tv}"
            if not u:
                continue
            # 跳过分隔占位（data-val=-1，非真实分类）。仅当处于
            # data_val 标签模式（tag_vals 非空）时才需要对 -1/空做过滤，
            # 普通链接分类源（无 data_val）不受影响。
            tag_mode = bool(tag_vals)
            if tag_mode and tv in ("-1", ""):
                continue
            if u in seen_links:
                continue
            seen_links.add(u)
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

        # 作品项选择器：优先 works_list_item（专属作品列表），
        # 其次 search.item，最后 list_item（分类项，通常不用于作品列表）
        disc = source.get_discovery_config()
        search = source.get_search_config()
        works_list_item = disc.get("works_list_item") or {}
        list_item = disc.get("list_item") or {}
        search_item = search.get("item") or {}
        root_sel = (
            works_list_item.get("root_selector")
            or search_item.get("root_selector")
            or list_item.get("root_selector")
        )
        fields = (
            works_list_item.get("fields")
            or search_item.get("fields")
            or list_item.get("fields")
            or {}
        )

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
        # 封面走加密渲染（works_list_item.cover_render == "playwright"）：
        # 标记源需要封面恢复，但不在这里同步执行（太慢会阻塞 QRunnable），
        # 由 GUI 层调 discover_page 起异步任务恢复封面后刷新卡片。
        if works and works_list_item.get("cover_render") == "playwright":
            works[0]._needs_cover_recovery = True  # 标记即可
        return works

    def _recover_covers(self, source: SourceConfig, booklist_url: str, works: List[Work]) -> dict:
        """批量恢复一页漫画封面 → {work_url: data_uri}。"""
        from .comic_cover_recovery import recover_booklist_covers_sync

        book_urls = [w.url for w in works if w.url]
        return recover_booklist_covers_sync(
            self._abs_url(source, booklist_url),
            book_urls,
            proxy=source.transports().get("proxy"),
        )

    def _list_works_api(self, source: SourceConfig, url: str, page: int = 1) -> List[Work]:
        """API 站（api_endpoints）JSON 解析作品列表。

        支持 fallback_url：当分类 URL 无有效关键词（如"全部"）时，
        回退到 fallback_url（全站热门接口），用 fallback_response_path /
        fallback_item_fields 解析。
        """
        import json
        import re as _re
        from urllib.parse import parse_qs, urlparse, urlencode

        api = source.raw.get("api_endpoints") or {}
        cfg = api.get("discovery") or api.get("search") or {}
        if not cfg:
            return []

        # 从传入分类 URL 解析 query 参数（如 tids=1&keyword=新番）
        url_qs = parse_qs(urlparse(url).query) if "?" in url else {}
        cat_params = {k: v[0] for k, v in url_qs.items()}

        # 判断是否回退（无有效关键词 → 全站热门接口）
        use_fallback = False
        if cfg.get("fallback_url"):
            kw = cat_params.get("keyword") or ""
            if not kw:
                use_fallback = True

        if use_fallback:
            api_url = str(cfg.get("fallback_url"))
            api_url = api_url.replace("{page}", str(page))
            api_url = _re.sub(r"\{[^}]+\}", "", api_url)
            abs_url = self._abs_url(source, api_url)
            # popular 参数：ps/pn/rid
            params = cfg.get("params") or {}
            filled = {k: str(v) for k, v in params.items()}
            filled = {k: v.replace("{page}", str(page)) for k, v in filled.items()}
            # 忽略 keyword/tids 占位（popular 不需要）
            filled = {k: _re.sub(r"\{[^}]+\}", "", v) for k, v in filled.items()}
            qs = urlencode(filled)
            abs_url = abs_url + ("&" if "?" in abs_url else "?") + qs
            rpath = cfg.get("fallback_response_path")
            item_fields = cfg.get("fallback_item_fields") or {}
        else:
            # search/type + keyword/tids
            api_url = str(cfg.get("url") or url)
            api_url = api_url.replace("{page}", str(page))
            api_url = _re.sub(r"\{[^}]+\}", "", api_url)
            abs_url = self._abs_url(source, api_url)
            params = cfg.get("params") or {}
            filled = {k: str(v) for k, v in params.items()}
            filled = {k: v.replace("{page}", str(page)) for k, v in filled.items()}
            # 用分类 URL 的 query 参数覆盖占位（tids/keyword）
            for k, v in cat_params.items():
                if f"{{{k}}}" in str(filled.get(k, "")) or k in filled:
                    filled[k] = v
            # 未填的占位符清空
            filled = {k: _re.sub(r"\{[^}]+\}", "", v) for k, v in filled.items()}
            sign_cfg = cfg.get("sign") or {}
            strategy = sign_cfg.get("strategy")
            if strategy:
                from .signers import get_signer

                filled = get_signer(strategy, self._http).sign(filled)
            qs = urlencode(filled)
            abs_url = abs_url + ("&" if "?" in abs_url else "?") + qs
            rpath = cfg.get("response_path")
            item_fields = cfg.get("item_fields") or {}

        # GET JSON
        resp_json = self._http.get_json(
            abs_url,
            headers=self._headers(source),
            timeout=self._timeout(source),
            retries=self._retries(source),
        )

        # 提取列表项
        items = resp_json
        if rpath:
            items = self._simple_getpath(resp_json, rpath)
        if not isinstance(items, list):
            return []

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
                    author=str(self._template_value(it, item_fields.get("author")) or ""),
                    update=str(self._template_value(it, item_fields.get("update")) or ""),
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
            if "." in field_spec:
                # 嵌套路径如 owner.name 逐层取值
                cur = item
                for part in field_spec.split("."):
                    if isinstance(cur, dict) and part in cur:
                        cur = cur[part]
                    else:
                        return ""
                return cur
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
