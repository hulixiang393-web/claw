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
from .health import report_bg_check
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
    def __init__(
        self,
        http: HttpClient,
        parser: Parser,
        checker: StructureChecker,
        health_reporter=None,
    ):
        self._http = http
        self._parser = parser
        self._checker = checker
        self._health_reporter = health_reporter  # 可选：update_health(source_id, state, error)
        self._ytdlp = None  # 懒加载单例

    # ------------------------------------------------------------------ #
    def _bg_check(self, source: SourceConfig, abs_url: str) -> None:
        """后台线程执行结构自检，不阻塞抓取（委托 health.report_bg_check）。"""
        # 源配置 diagnostics.selfcheck.strategy="off" → 关闭后台自检（同 content._bg_check）
        if (
            (source.raw.get("diagnostics") or {}).get("selfcheck") or {}
        ).get("strategy") == "off":
            return
        report_bg_check(self._checker, self._health_reporter, source, abs_url)

    def _apply_cover_state(
        self, works: List[Work], cfg: dict, fields: dict, html: str
    ) -> None:
        """列表页封面兜底：INITIAL_STATE 里的真实封面填回为空/占位的条目。

        cfg 配置（works_list_item.cover_state）：
            lists       INITIAL_STATE 里的作品列表路径（如 ["home.boyList","home.girlList"]）
            cover_key   列表项封面字段（如 thumbUri）
            id_key      列表项作品 ID 字段（缺省 bookId），与列表页 href 对应
        封面占位标记取 fields.cover.placeholder（与 detail 封面兜底一致）。
        解析失败/无匹配 → 保留原封面（容错），不阻塞列表返回。
        """
        import re as _re

        state = self._parse_initial_state(html)
        if not state:
            return
        cover_key = cfg.get("cover_key") or "thumbUri"
        id_key = cfg.get("id_key") or "bookId"
        # 跨多个列表路径扁平收集：{id: thumb} 映射 + 顺序封面序列（索引 zip 兜底）
        cover_by_id: dict = {}
        cover_seq: List[str] = []
        for path in cfg.get("lists") or []:
            lst = self._simple_getpath(state, path)
            if not isinstance(lst, list):
                continue
            for it in lst:
                if not isinstance(it, dict):
                    continue
                thumb = it.get(cover_key)
                if not thumb:
                    continue
                cover_seq.append(str(thumb))
                cid = it.get(id_key)
                if cid is not None:
                    cover_by_id[str(cid)] = str(thumb)
        if not cover_by_id and not cover_seq:
            return
        # 占位标记（fields.cover.placeholder）
        placeholder = ""
        fcover = fields.get("cover")
        if isinstance(fcover, dict):
            placeholder = str(fcover.get("placeholder") or "")
        idx = 0
        for w in works:
            # 已是有效封面（非占位）→ 跳过
            if w.cover and not (placeholder and placeholder in w.cover):
                continue
            # 优先按 URL 里的 bookId 匹配（去重/过滤后顺序仍对齐）
            m = _re.search(r"/page/(\d+)", w.url or "")
            cid = m.group(1) if m else ""
            new_cover = ""
            if cid and cid in cover_by_id:
                new_cover = cover_by_id[cid]
            elif idx < len(cover_seq):
                # 顺序 zip 兜底：列表页条目与 href 一一对应
                new_cover = cover_seq[idx]
                idx += 1
            if new_cover:
                w.cover = new_cover

    @staticmethod
    def _parse_initial_state(html: str):
        """从 html 中提取 window.__INITIAL_STATE__ = {...}; 并解析为 dict。

        大括号平衡扫描（跳过字符串内）切出 JSON，容错：找不到/解析失败返回 None。
        """
        if not html:
            return None
        marker = "window.__INITIAL_STATE__"
        idx = html.find(marker)
        if idx < 0:
            return None
        start = html.find("{", idx)
        if start < 0:
            return None
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(html)):
            c = html[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        import json as _json

                        return _json.loads(html[start:i + 1])
                    except Exception:  # noqa: BLE001
                        return None
        return None

    # ------------------------------------------------------------------ #
    def _headers(self, source: SourceConfig) -> dict:
        return source.request_headers()

    def _timeout(self, source: SourceConfig) -> float:
        return float(source.transports().get("timeout") or self._http.defaults.timeout)

    def _retries(self, source: SourceConfig) -> int:
        return int(source.transports().get("retries") or self._http.defaults.retries)

    def _interval_ms(self, source: SourceConfig) -> int:
        return int(source.transports().get("interval_ms") or self._http.defaults.interval_ms)

    def _abs_url(self, source: SourceConfig, url: str) -> str:
        from . import utils

        return utils.abs_url(source.base_url, url)

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
            encoding=source.transports().get("charset"),
            proxy_pool=source.proxy_pool(),
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
        # 自检移后台线程（不阻塞分类抓取，防每页多一次 GET 拖慢）
        self._bg_check(source, self._abs_url(source, list_url))
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
        # 仅 API 发现源才走 _list_works_api：显式配置了 api_endpoints.discovery，
        # 或仅有 api_endpoints.search 且无 HTML 发现规则（API 站用搜索接口兜底当列表）。
        # 有 HTML 发现规则（endpoints.discovery）的源即使配了 api_endpoints.search
        # 也走 HTML 解析（如纵横小说：发现走榜单 HTML、搜索走官方 JSON API），
        # 否则 list_works 会被 search API 劫持返回 0。
        disc_html = source.get_discovery_config()
        has_html_rules = bool(
            (disc_html.get("works_list_item") or {}).get("root_selector")
            or (disc_html.get("list_item") or {}).get("root_selector")
            or (disc_html.get("list_item") or {}).get("categories")
        )
        if api.get("discovery") or (api.get("search") and not has_html_rules):
            return self._list_works_api(source, url, page)

        fetch_url = self._build_page_url(source, url, page)
        # 自检移后台线程（不阻塞作品抓取）
        self._bg_check(source, self._abs_url(source, fetch_url))
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
        seen: set = set()  # 同页内按 URL 去重（maccms 等列表页偶发重复项）
        for it in items:
            if not it.get("title") or not it.get("url"):
                continue
            url_key = it.get("url", "")
            if url_key in seen:
                continue
            seen.add(url_key)
            cover = it.get("cover", "")
            works.append(
                Work(
                    title=it.get("title", ""),
                    url=it.get("url", ""),
                    cover=cover,
                    author=it.get("author", ""),
                    update=it.get("update", ""),
                    source_id=source.source_id,
                    source_name=source.source_name,
                )
            )
        # 封面加密站（18mh 类 AES 解密）或需 Playwright 渲染的源：
        # 不再同步解密封面（阻塞列表返回），改为标记，由 GUI 层异步恢复
        # （discover_page._start_cover_recovery），列表秒开、封面后补。
        need_cover_decrypt = bool(
            source.raw.get("decryption", {}).get("targets", {}).get("image")
        )
        if works and need_cover_decrypt:
            works[0]._needs_cover_decrypt = True  # 标记：GUI 起 AES 异步恢复
        if works and works_list_item.get("cover_render") == "playwright":
            works[0]._needs_cover_recovery = True  # 标记：GUI 起 Playwright 恢复
        # 封面兜底：works_list_item.cover_state 配置下，SSR 把全部封面渲染成共享
        # 占位图，真实封面在 window.__INITIAL_STATE__（如 home.boyList/girlList 的
        # thumbUri，与列表页条目 href 一一对应）。解析一次 state，把真实封面填回
        # 为空/占位的条目（解析失败静默保留原封面，容错）。
        if works and works_list_item.get("cover_state"):
            self._apply_cover_state(works, works_list_item["cover_state"], fields, html)
        return works

    # ------------------------------------------------------------------ #
    def decrypt_covers(self, source: SourceConfig, works: List[Work]) -> dict:
        """批量下载+AES 解密封面 → {work_url: data_uri}（18mh 类加密站）。

        与 list_works 内联同步解密分离：列表先返回（不阻塞），GUI 异步调本方法
        恢复封面后刷新卡片。单张失败跳过（保留原 URL 兜底）。

        每批用独立 HttpClient：GUI 多页并发调本方法时，若共享 self._http
        （requests.Session 非线程安全）会在并发下载时 Session 竞态，偶发
        下载失败 → 部分页封面加载不出（实测发现页奇数页封面空白）。
        """
        targets = [
            (w.url, w.cover)
            for w in works
            if w.url and w.cover and w.cover.startswith(("http://", "https://"))
        ]
        if not targets:
            return {}
        # 独立 HttpClient（复用同套 defaults），避免 Session 跨线程竞态
        worker_http = self._http.__class__(
            sleeper=getattr(self._http, "_sleeper", None),
            defaults=self._http.defaults,
        )
        from concurrent.futures import ThreadPoolExecutor

        result: dict = {}
        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                futs = {
                    pool.submit(self._decrypt_cover, source, url, worker_http): wurl
                    for wurl, url in targets
                }
                for fut in futs:
                    try:
                        data_uri = fut.result()
                        if data_uri and data_uri.startswith("data:"):
                            result[futs[fut]] = data_uri
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            try:
                worker_http.close()
            except Exception:  # noqa: BLE001
                pass
        return result

    def _decrypt_cover(self, source: SourceConfig, cover_url: str, http=None) -> str:
        """下载并解密带加密的封面 → data URI。解密失败保留原 URL（封面留空不阻塞）。

        复用 Decrypter.decrypt_bytes；与正文图片同密钥（decryption.image）。
        http：可指定独立 HttpClient（多任务并发时避免共享 Session 竞态）。
        """
        http = http or self._http
        try:
            from .decrypter import Decrypter

            raw = http.get_bytes(
                self._abs_url(source, cover_url),
                headers=self._headers(source),
                timeout=self._timeout(source),
                retries=self._retries(source),
                proxy_pool=source.proxy_pool(),
            )
            decrypter = Decrypter(self._http)
            plain = decrypter.decrypt_bytes(source, raw, target="image")
            if plain and plain is not raw:
                import base64 as _b64

                # 按魔数猜 mime，默认 jpeg
                mime = "image/jpeg"
                if plain[:4] == b"\x89PNG":
                    mime = "image/png"
                elif plain[:6] in (b"GIF89a", b"GIF87a"):
                    mime = "image/gif"
                elif plain[:4] == b"RIFF" and plain[8:12] == b"WEBP":
                    mime = "image/webp"
                return f"data:{mime};base64,{_b64.b64encode(plain).decode()}"
        except Exception:  # noqa: BLE001
            pass
        return cover_url

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

        # yt-dlp 引擎：从分类 URL 取 keyword，调 yt-dlp 搜索
        if cfg.get("engine") == "ytdlp":
            from urllib.parse import parse_qs, urlparse

            url_qs = parse_qs(urlparse(url).query) if "?" in url else {}
            kw = url_qs.get("keyword", [""])[0] or cfg.get("default_keyword") or "热门"
            return self._list_works_ytdlp(source, kw)

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
            proxy_pool=source.proxy_pool(),
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

    def _list_works_ytdlp(self, source: SourceConfig, keyword: str) -> List[Work]:
        """yt-dlp 引擎：按关键词搜索返回作品列表（发现页用）。"""
        if self._ytdlp is None:
            from .ytdlp import Ytdlp

            self._ytdlp = Ytdlp()
        limit = 15
        try:
            items = self._ytdlp.search(keyword, limit=limit)
        except Exception:
            return []
        cfg = source.raw.get("api_endpoints", {}).get("discovery") or {}
        works: List[Work] = []
        for it in items:
            if not it.get("title") or not it.get("url"):
                continue
            works.append(
                Work(
                    title=it["title"],
                    url=it["url"],
                    cover=it.get("cover") or "",
                    author=it.get("author") or "",
                    source_id=source.source_id,
                    source_name=source.source_name,
                )
            )
        return works

    @staticmethod
    def _simple_getpath(data, path: str):
        """简化的 JSONPath：data.list → data["list"]；data.list.0 → 列表首项。"""
        from . import utils

        return utils.jsonpath(data, path)

    @staticmethod
    def _template_value(item: dict, field_spec):
        """字段值：若是模板（含 {}）则填充，否则直接取值。"""
        if field_spec is None:
            return ""
        if isinstance(field_spec, str):
            if "{" in field_spec:
                # 模板如 https://bilibili.com/bangumi/media/md{media_id}
                from . import utils

                return utils.fill_template(field_spec, item)
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
            # 2) page_placeholder 正则：替换 URL 中页码位。
            #    第 1 页不插页码（多数站第 1 页即 base URL，页码从第 2 页起）。
            placeholder = paginator.get("page_placeholder")
            if placeholder:
                if page <= 1:
                    return url
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
