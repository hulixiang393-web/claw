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
from urllib.parse import quote, urlencode, urljoin

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
    # 合并相似结果时：该结果的其它源版本（ui-search.md #7）
    variants: "list[SearchResult]" = field(default_factory=list)

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
        concurrent: int = 1,
    ):
        self._http = http
        self._parser = parser
        self._discovery = discovery
        self._concurrent = max(1, int(concurrent or 1))
        self._ytdlp = None  # 懒加载单例

    # ------------------------------------------------------------------ #
    def search_one(
        self,
        source: SourceConfig,
        keyword: str,
        http: Optional[HttpClient] = None,
        on_page=None,
    ) -> List[SearchResult]:
        """单源搜索。优先 yt-dlp 引擎，其次 api_endpoints.search，否则 endpoints.search。

        http：可指定独立 HttpClient（并发搜索时每 worker 各用一个，requests.Session
        非线程安全）。None 用 self._http。
        on_page：可选回调 on_page(source, page, new_results)。HTML 搜索每翻一页
        解析完成后调用（传本页新增结果），供 GUI 边抓边显示（第 1 页秒出、后续
        页后台抓到即追加），避免 max_pages 调大后全部抓完才显示导致卡死。
        """
        http = http or self._http
        api = source.raw.get("api_endpoints") or {}
        search_cfg = api.get("search") or {}
        if search_cfg.get("engine") == "ytdlp":
            return self._search_ytdlp(source, keyword, search_cfg, http=http)
        if search_cfg:
            return self._search_api(source, keyword, http=http)
        return self._search_html(source, keyword, http=http, on_page=on_page)

    def search_type(
        self, sources: List[SourceConfig], keyword: str
    ) -> List[SearchResult]:
        """跨源搜索（并发数 = concurrent），合并结果。单源失败不影响其他。

        concurrent>1 时用 ThreadPoolExecutor 并行；每 worker 独立 HttpClient
        （复用同一 NetworkDefaults），避免 requests.Session 非线程安全。
        """
        results: List[SearchResult] = []
        if self._concurrent <= 1 or len(sources) <= 1:
            for source in sources:
                try:
                    results.extend(self.search_one(source, keyword))
                except Exception as exc:
                    log.warning("[%s] 搜索失败: %s", source.source_id, exc)
            return results

        from concurrent.futures import ThreadPoolExecutor

        def _worker(source: SourceConfig) -> List[SearchResult]:
            # 每 worker 一个独立 HttpClient（共享默认值）
            worker_http = self._http.__class__(
                sleeper=getattr(self._http, "_sleeper", None),
                defaults=self._http.defaults,
            )
            try:
                return self.search_one(source, keyword, http=worker_http)
            except Exception as exc:
                log.warning("[%s] 搜索失败: %s", source.source_id, exc)
                return []
            finally:
                try:
                    worker_http.close()
                except Exception:
                    pass

        workers = min(self._concurrent, max(1, len(sources)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for part in pool.map(_worker, sources):
                results.extend(part or [])
        return results

    # ------------------------------------------------------------------ #
    def _search_html(
        self,
        source: SourceConfig,
        keyword: str,
        http: Optional[HttpClient] = None,
        on_page=None,
    ) -> List[SearchResult]:
        """HTML 站搜索（endpoints.search）。

        on_page(source, page, new_results)：每翻完一页后回调本页新增结果
        （供 GUI 边抓边显示）。None 则只返回全量。
        """
        http = http or self._http
        search_cfg = source.get_search_config()
        if not search_cfg.get("item") or not search_cfg.get("item", {}).get("fields"):
            return []
        base_url = search_cfg.get("base_url") or source.base_url
        method = search_cfg.get("method") or "GET"
        kw_param = search_cfg.get("keyword_param") or "keyword"

        # 翻页：读 constraints.search.max_pages（schema 默认 3），多页合并去重。
        # 分页 URL 模板（endpoints.search.paginator.url_template，编辑器「换页逻辑」）：
        # - 含 {keyword} → 完整 URL 模板（如 /search/{keyword}/{page}.html）
        # - 不含 {keyword} → 页码拼接片段（?page={page} 或 -{page}.html）
        # - 留空 → 默认自动 ?page=N（POST 源 body 加 page 参数）
        constraints = source.raw.get("constraints") or {}
        max_pages = int((constraints.get("search") or {}).get("max_pages") or 3)
        max_results = int((constraints.get("search") or {}).get("max_results") or 0)
        paginator_cfg = search_cfg.get("paginator") or {}
        page_param = paginator_cfg.get("param") or "page"
        url_template = paginator_cfg.get("url_template") or ""
        extra = search_cfg.get("extra_params") or {}
        if not isinstance(extra, dict):
            extra = {}

        item_cfg = search_cfg.get("item") or {}
        root_sel = item_cfg.get("root_selector")
        fields = item_cfg.get("fields") or {}
        if not root_sel:
            return []

        # render: "playwright" → 反爬 SPA 站（结果 JS 渲染），交互式只搜第一页
        if search_cfg.get("render") == "playwright":
            abs_url = self._build_page_url(
                source=source, base_url=base_url, keyword=keyword,
                kw_param=kw_param, page=1, page_param=page_param,
                url_template=url_template,
            )
            return self._search_html_rendered(source, abs_url, item_cfg, keyword)

        def _fetch(page: int):
            """抓取并解析第 page 页，返回 (page, items)。失败返回空列表。"""
            try:
                if method == "POST" and not url_template:
                    # POST：body 加 page 参数（配了 url_template 则改用 GET 模板拼 URL）
                    abs_url = urljoin(source.base_url, base_url)
                    body = {kw_param: keyword, page_param: page}
                    for k, v in extra.items():
                        body.setdefault(k, v)
                    text = self._http_post_form(source, abs_url, body, http=http)
                else:
                    # GET / 分页模板：统一由 _build_page_url 构造第 page 页 URL
                    # （用关键字参数传递，避免位置参数错位导致 `&1=page` 这类 bug）
                    abs_url = self._build_page_url(
                        source=source,
                        base_url=base_url,
                        keyword=keyword,
                        kw_param=kw_param,
                        page=page,
                        page_param=page_param,
                        url_template=url_template,
                    )
                    text = self._http_get(source, abs_url, http=http)
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] 搜索第 %d 页失败：%s", source.source_id, page, exc)
                return (page, [])
            try:
                doc = self._parser.parse(text)
                items = self._parser.parse_items(doc, root_sel, fields, source.base_url)
                return (page, items or [])
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] 搜索第 %d 页解析失败：%s", source.source_id, page, exc)
                return (page, [])

        # 受控并发翻页：每源最多 3 页在途请求（防反爬）。
        # 保留 transports.interval_ms 间隔（_http_get 内部已 sleep）；
        # 并发上限 3 平衡提速与反爬风险，不放大单源请求频率。
        from concurrent.futures import ThreadPoolExecutor, as_completed

        page_items: dict = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            futs = {pool.submit(_fetch, p): p for p in range(1, max_pages + 1)}
            for fut in as_completed(futs):
                page, items = fut.result()
                page_items[page] = items

        # 按页序合并（第 1 页先回调 on_page 秒出），URL 去重
        results = []
        seen_urls = set()
        for page in sorted(page_items):
            items = page_items[page]
            if not items:
                break  # 本页无结果 → 后续页大概率也无（沿用串行语义）
            page_start = len(results)  # 本页处理前的累计数（切分本页新增）
            for it in items:
                title = it.get("title", "")
                url = it.get("url", "")
                if not title or not url:
                    continue
                if url in seen_urls:
                    continue  # URL 去重（trtag 等站搜索页 DOM 有重复节点）
                seen_urls.add(url)
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
            # 边抓边显示：本页新增结果回调给 GUI（第 1 页秒出，后续页抓到即追加）
            if on_page:
                try:
                    on_page(source, page, results[page_start:])
                except Exception:  # noqa: BLE001
                    pass
            # 达到结果总数上限：本页完整并入后再停，而不是每页内部提前 return。
            # 否则「单页条数 >= max_results」的源第一页就返回，真实站几十页也只
            # 显示一页。max_results 是跨页总数上限，由源配置保证 >= 单页条数。
            if max_results and len(results) >= max_results:
                break
        return results

    @staticmethod
    def _build_page_url(
        *,
        source: SourceConfig,
        base_url: str,
        keyword: str,
        kw_param: str,
        page: int,
        page_param: str = "page",
        url_template: str = "",
    ) -> str:
        """构造搜索第 page 页的 URL（分页拼接规则由源配置决定）。

        url_template（endpoints.search.paginator.url_template，编辑器「换页逻辑」）：
        - 含 {keyword} → 完整 URL 模板：替换 {keyword}/{page} 后拼 base_url
          （如 /search/{keyword}/{page}.html、/search?q={keyword}&p={page}）
        - 不含 {keyword} → 页码拼接片段：?/& 开头作为 query 参数追加到
          「搜索 URL + 关键词」后，否则作为路径后缀（如 -{page}.html）
        - 留空 → 默认：GET 追加 ?{page_param}={page}；{page} 占位走占位替换

        全部参数用关键字传递（* 强制），避免位置错位生成 `&1=page` 的错误 URL。
        """
        if url_template:
            if "{keyword}" in url_template:
                tpl = url_template.replace("{keyword}", quote(keyword)).replace(
                    "{page}", str(page)
                )
                if tpl.startswith(("http://", "https://")):
                    return tpl
                return source.base_url.rstrip("/") + "/" + tpl.lstrip("/")
            seg = url_template.replace("{page}", str(page))
            if seg.startswith("?"):
                sep = "&" if "?" in base_url else "?"
                return source.base_url.rstrip("/") + "/" + (
                    f"{base_url}{sep}{kw_param}={quote(keyword)}&{seg[1:]}"
                ).lstrip("/")
            if seg.startswith("&"):
                sep = "&" if "?" in base_url else "?"
                return source.base_url.rstrip("/") + "/" + (
                    f"{base_url}{sep}{kw_param}={quote(keyword)}{seg}"
                ).lstrip("/")
            # 路径后缀：如 -{page}.html、/list/{page}.html（追加到搜索 URL 路径后）
            path = base_url.split("?", 1)[0].rstrip("/")
            return source.base_url.rstrip("/") + "/" + (path + seg).lstrip("/")
        # 默认逻辑：{page} 占位 或 ?page=N
        if "{page}" in base_url:
            tpl = base_url.replace("{page}", str(page)).replace(
                "{keyword}", quote(keyword)
            )
            return source.base_url.rstrip("/") + "/" + tpl.lstrip("/")
        sep = "&" if "?" in base_url else "?"
        return source.base_url.rstrip("/") + "/" + (
            f"{base_url}{sep}{kw_param}={quote(keyword)}&{page_param}={page}"
        ).lstrip("/")

    def _search_html_rendered(
        self,
        source: SourceConfig,
        abs_url: str,
        item_cfg: dict,
        keyword: str = "",
    ) -> List[SearchResult]:
        """Playwright 渲染搜索页，按 root_selector 提取搜索结果。

        适用于反爬 SPA 站（CSS 类名混淆 + JS 动态填充，如 fdzys）。
        渲染配置从 item.render_config 读取。

        keyword 非空时，结果按「标题/文本含关键词」过滤——反爬站常在搜索页
        塞入热门榜（无关项），过滤后只留真正命中关键词的结果，避免误导。
        """
        from .playwright_helper import fetch_rendered_items_sync, fetch_rendered_search_sync

        rc = item_cfg.get("render_config") or {}

        def _sel(spec) -> str:
            """root_selector 可能为 {"css": "..."} 或纯字符串。"""
            if isinstance(spec, dict):
                return spec.get("css") or spec.get("xpath") or ""
            return spec or ""

        if rc.get("interact"):
            # 交互式搜索：访问首页 → 填搜索框 → 提交（JS 加载真实结果）
            home = rc.get("home_url") or source.base_url
            items = fetch_rendered_search_sync(
                home,
                keyword,
                input_selector=rc.get("input_selector") or "input[name='wd'], input[name='searchword']",
                result_selector=_sel(item_cfg.get("root_selector")),
                wait_until=rc.get("wait_until") or "networkidle",
                timeout_ms=int(rc.get("timeout_ms") or 30000),
                extra_delay_ms=int(rc.get("extra_delay_ms") or 3000),
                proxy=source.transports().get("proxy"),
            )
        else:
            items = fetch_rendered_items_sync(
                abs_url,
                _sel(item_cfg.get("root_selector")),
                wait_for=rc.get("wait_for") or "",
                wait_until=rc.get("wait_until") or "networkidle",
                timeout_ms=int(rc.get("timeout_ms") or 30000),
                extra_delay_ms=int(rc.get("extra_delay_ms") or 2500),
                proxy=source.transports().get("proxy"),
            )
        kw = (keyword or "").strip()
        results = []
        seen_urls = set()
        for it in items:
            url = it.get("href") or ""
            if not url or not url.startswith("http"):
                url = urljoin(source.base_url, url)
            title = it.get("title") or it.get("text") or ""
            if not title or not url or url in seen_urls:
                continue
            # 关键词过滤：标题/文本含关键词才算真实命中（剔除热门榜无关项）
            if kw and kw not in title and kw not in it.get("text", ""):
                continue
            seen_urls.add(url)
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    source_id=source.source_id,
                    source_name=source.source_name,
                    cover=Search._clean_cover(it.get("src", "")),
                )
            )
        return results

    # ------------------------------------------------------------------ #
    def _search_ytdlp(
        self, source: SourceConfig, keyword: str, cfg: dict, http: Optional[HttpClient] = None
    ) -> List[SearchResult]:
        """yt-dlp 引擎搜索。搜索前缀/URL 模板从源配置读（通用化）。"""
        constraints = source.raw.get("constraints") or {}
        cs = constraints.get("search") or {}
        limit = int(cfg.get("max_results") or cs.get("max_results") or 20)
        prefix = str(cfg.get("search_prefix") or "ytsearch")
        url_tpl = str(cfg.get("url_template") or "https://www.youtube.com/watch?v={id}")
        if self._ytdlp is None:
            from .ytdlp import Ytdlp

            self._ytdlp = Ytdlp()
        items = self._ytdlp.search(keyword, limit=limit, prefix=prefix, url_tpl=url_tpl)
        results = []
        for it in items:
            if not it.get("title") or not it.get("url"):
                continue
            results.append(
                SearchResult(
                    title=self._clean_title(str(it["title"])),
                    url=str(it["url"]),
                    source_id=source.source_id,
                    source_name=source.source_name,
                    cover=str(it.get("cover") or ""),
                    author=str(it.get("author") or ""),
                )
            )
        return results

    def _search_api(
        self, source: SourceConfig, keyword: str, http: Optional[HttpClient] = None
    ) -> List[SearchResult]:
        """API 站搜索（api_endpoints.search）。

        支持两种 URL 构造：
        - params 对象：结构化参数，自动 URL encode，支持 sign 策略签名。
        - url 模板：URL 含 {keyword} 占位，手动拼接。
        """
        http = http or self._http
        api = source.raw.get("api_endpoints") or {}
        cfg = api.get("search") or {}
        if not cfg:
            return []
        api_url = str(cfg.get("url") or "")
        params = cfg.get("params") or {}

        # 翻页：读 constraints.search.max_pages（默认 1），多页合并去重
        constraints = source.raw.get("constraints") or {}
        max_pages = int((constraints.get("search") or {}).get("max_pages") or 1)

        results: List[SearchResult] = []
        seen_urls: set = set()
        for page in range(1, max_pages + 1):
            if params:
                filled = {}
                for k, v in params.items():
                    val = str(v).replace("{keyword}", keyword).replace("{page}", str(page))
                    filled[k] = val
                sign_cfg = cfg.get("sign") or {}
                strategy = sign_cfg.get("strategy")
                if strategy:
                    from .signers import get_signer

                    signer = get_signer(strategy, http)
                    filled = signer.sign(filled)
                qs = urlencode(filled)
                abs_url = urljoin(source.base_url, api_url)
                if "?" in api_url:
                    abs_url = f"{abs_url}&{qs}"
                else:
                    abs_url = f"{abs_url}?{qs}"
            else:
                api_url2 = api_url.replace("{keyword}", quote(keyword)).replace("{page}", str(page))
                abs_url = urljoin(source.base_url, api_url2)
            resp = http.get_json(
                abs_url,
                headers=source.request_headers(),
                timeout=float(source.transports().get("timeout") or http.defaults.timeout),
                proxy_pool=source.proxy_pool(),
            )
            items = resp
            rpath = cfg.get("response_path")
            if rpath:
                items = self._simple_getpath(resp, rpath)
            if not isinstance(items, list) or not items:
                break
            item_fields = cfg.get("item_fields") or {}
            for it in items:
                if not isinstance(it, dict):
                    continue
                title = self._tpl(it, item_fields.get("title"))
                url = self._tpl(it, item_fields.get("url"))
                if not title or not url:
                    continue
                url = urljoin(source.base_url, url)
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                cover = self._clean_cover(self._tpl(it, item_fields.get("cover")))
                results.append(
                    SearchResult(
                        title=self._clean_title(str(title)),
                        url=str(url),
                        source_id=source.source_id,
                        source_name=source.source_name,
                        cover=str(cover or ""),
                        author=str(self._tpl(it, item_fields.get("author")) or ""),
                        update=str(self._tpl(it, item_fields.get("update")) or ""),
                    )
                )
        return results

    @staticmethod
    def merge_similar(results: "List[SearchResult]") -> "List[SearchResult]":
        """合并相似搜索结果（ui-search.md #7）。

        按「书名规范化 + 作者」模糊匹配：书名去标点/空格/括号内容，作者一致则视为同书。
        合并后保留第一个结果作为代表，其余各源版本挂到 `variants` 属性。
        返回新列表（不修改原列表）。
        """
        import re as _re

        def _norm_title(t: str) -> str:
            """书名规范化：去括号内容/标点/常见状态后缀，转小写。"""
            t = _re.sub(r"[（(].*?[)）]", "", t or "")  # 去括号内容（含副标题）
            t = _re.sub(r"[\s:：,，.。、!！?？\-—_/\\|··'\"“”]+", "", t)
            # 去常见状态/连载后缀（连载中/更新中/已完结/全本/全文/TXT 等）
            t = _re.sub(r"(连载中|更新中|已完结|完结|全本|全文|正版|无删减|TXT|txt)$", "", t)
            return t.lower().strip()

        groups: "dict[str, list]" = {}
        order: "list[str]" = []  # 保持首次出现顺序
        for r in results:
            key = (_norm_title(r.title), (r.author or "").strip().lower())
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(r)

        merged = []
        for key in order:
            items = groups[key]
            if len(items) == 1:
                merged.append(items[0])
                continue
            # 多条合并：首条为代表，其余挂 variants
            head = items[0]
            # 用 dataclasses.replace 克隆，避免污染原对象
            from dataclasses import replace

            rep = replace(head)
            rep.variants = items[1:]  # type: ignore[attr-defined]
            rep.title = f"{head.title}（{len(items)} 个源）"
            merged.append(rep)
        return merged

    @staticmethod
    def _clean_title(title: str) -> str:
        """去标题中的 HTML 标签（如 B 站搜索结果 <em class='keyword'>）。"""
        import re

        return re.sub(r"<[^>]+>", "", title).strip()

    @staticmethod
    def _clean_cover(cover: str) -> str:
        """封面修复：协议相对 URL（//xxx）补 https 头。"""
        cover = (cover or "").strip()
        if cover.startswith("//"):
            return "https:" + cover
        return cover

    # ------------------------------------------------------------------ #
    def decrypt_search_covers(self, source: SourceConfig, results) -> dict:
        """批量解密搜索结果的封面 → {result.url: data_uri}。

        18mh 类加密站：搜索结果封面是加密 URL，直接加载不出图。复用 discovery
        （list_works）的 AES 解密逻辑（decrypt_covers，8 并发），与分类页封面
        解密同一套。返回 {SearchResult.url: data_uri}，解密失败的结果不出现。
        非加密源或空结果返回 {}（GUI 层据此跳过后台任务）。
        """
        need = bool(
            source.raw.get("decryption", {}).get("targets", {}).get("image")
        )
        if not need or not results:
            return {}
        from types import SimpleNamespace

        if self._discovery is None:
            # 懒创建（复用分类页同一套 Discovery：含 checker 自检器）
            from .selfcheck import StructureChecker

            self._discovery = Discovery(
                self._http, self._parser, StructureChecker(self._http, self._parser)
            )
        carriers = [
            SimpleNamespace(url=r.url, cover=r.cover)
            for r in results
            if getattr(r, "url", "") and getattr(r, "cover", "")
        ]
        if not carriers:
            return {}
        return self._discovery.decrypt_covers(source, carriers)

    # ------------------------------------------------------------------ #
    def _http_get(
        self, source: SourceConfig, url: str, http: Optional[HttpClient] = None
    ) -> str:
        http = http or self._http
        return http.get_text(
            url,
            headers=source.request_headers(),
            timeout=float(source.transports().get("timeout") or http.defaults.timeout),
            retries=int(source.transports().get("retries") or http.defaults.retries),
            encoding=source.transports().get("charset"),
            proxy_pool=source.proxy_pool(),
        )

    def _http_post_form(
        self, source: SourceConfig, url: str, data: dict, http: Optional[HttpClient] = None
    ) -> str:
        from urllib.parse import urlencode

        http = http or self._http
        return http.post_form(
            url,
            form_data=data,
            headers=source.request_headers(),
            timeout=float(source.transports().get("timeout") or http.defaults.timeout),
            encoding=source.transports().get("charset"),
            proxy_pool=source.proxy_pool(),
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
