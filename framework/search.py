"""多源搜索模块（search.py）。

支持：
- 单源搜索 search_one（HTML selector / api_endpoints JSON）
- 跨源并发 search_type
- 统一 SearchResult 输出

对应 ui-search.md 与 core.md「search.py」。
"""

from __future__ import annotations

import json
import logging
import re as _re
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import quote, urlencode, urljoin

from .config import SourceConfig
from .discovery import Discovery
from .errors import SourceError
from .http import HttpClient
from .parser import Parser
from .utils import fill_json, fill_template, jsonpath

log = logging.getLogger(__name__)

# 搜索页数放开：不设默认限量（有多少加载多少，翻到站底由连续空页自然停）。
# 此常量是引擎级硬保护——仅防"分页 URL 模板失效 → 每页都返回页首内容 → 永不
# 触发空页停"的死循环，绝不是搜索结果限制。正常源空页即停，远达不到此值。
# 源显式配置了 constraints.search.max_pages 则尊重源值（不覆盖）。
_SEARCH_MAX_PAGES_HARD_CAP = 2000


def _response_pages(rc: dict, source: SourceConfig) -> int:
    """计算响应监听模式的翻页数（番茄等单页仅 10 条、带分页条）。

    render_config.response_pages（期望页数）与 constraints.search.max_results
    （目标条数上限）共同约束：取较小者，每页按 10 条折算。缺省 1。
    """
    want = max(1, int(rc.get("response_pages") or 1))
    searches = (source.raw.get("constraints") or {}).get("search") or {}
    max_results = int(searches.get("max_results") or 0)
    if max_results:
        cap = max(1, -(-max_results // 10))
        want = min(want, cap)
    return want


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
        cookie_manager=None,
    ):
        self._http = http
        self._parser = parser
        self._discovery = discovery
        self._concurrent = max(1, int(concurrent or 1))
        self._cookie_manager = cookie_manager  # 渲染搜索页注入登录 cookie
        self._ytdlp = None  # 懒加载单例

    # ------------------------------------------------------------------ #
    def _search_decrypter(self, source: SourceConfig, http: Optional[HttpClient] = None):
        """源配置了 decryption.targets.title/author 解密策略时返回 Decrypter，否则 None。

        与 discovery.list_works 的列表解密同源（番茄 rank 榜/搜索/详情书名作者被
        PUA 字体混淆，translit 还原）；未配置的源返回 None，零开销、行为不变。
        """
        dec_cfg = (source.raw.get("decryption") or {}).get("targets") or {}
        if not (
            dec_cfg.get("title", {}).get("strategy")
            or dec_cfg.get("author", {}).get("strategy")
        ):
            return None
        from .decrypter import Decrypter

        return Decrypter(http or self._http)

    def _decrypt_results(
        self,
        source: SourceConfig,
        results: List[SearchResult],
        http: Optional[HttpClient] = None,
    ) -> List[SearchResult]:
        """按源 decryption 配置批量还原搜索结果 title/author（原地写回）。

        覆盖 yt-dlp / api_endpoints 引擎；HTML 引擎在 _merge_and_notify 逐条
        解密（保证 on_page 增量回调也是明文）。
        """
        dec = self._search_decrypter(source, http)
        if dec is None:
            return results
        for r in results:
            r.title = dec.decrypt(source, r.title, target="title")
            r.author = dec.decrypt(source, r.author, target="author")
        return results

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
            return self._decrypt_results(
                source,
                self._search_ytdlp(source, keyword, search_cfg, http=http),
                http,
            )
        if search_cfg:
            return self._decrypt_results(
                source, self._search_api(source, keyword, http=http), http
            )
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
        item_cfg = search_cfg.get("item") or {}
        # render=playwright（反爬 SPA 站）不依赖 item.fields：渲染引擎从 textContent
        # 提取 title/url（番茄/fdzys 等只写 root_selector + render_config）。只有
        # HTML selector 引擎需要 fields 做字段提取。
        if not item_cfg or (
            not item_cfg.get("fields") and search_cfg.get("render") != "playwright"
        ):
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
        # 搜索页数放开：不把 max_pages 当作搜索限制——有多少加载多少，翻到站点
        # 页尾由「连续空页」提前停（下方 while 内 break）。
        # - 源显式配了 max_pages/max_results 则尊重源配置（不截断/不覆盖）。
        # - 未配置 → 无限翻页（保留引擎硬保护 _SEARCH_MAX_PAGES_HARD_CAP 防
        #   分页模板失效死循环，并非搜索结果限制）；max_results=0 表示不按条数截断。
        max_pages = (constraints.get("search") or {}).get("max_pages")
        max_results = (constraints.get("search") or {}).get("max_results")
        if not max_pages:
            max_pages = _SEARCH_MAX_PAGES_HARD_CAP
        max_pages = int(max_pages)
        if not max_results:
            max_results = 0
        else:
            max_results = int(max_results)
        paginator_cfg = search_cfg.get("paginator") or {}
        page_param = paginator_cfg.get("param") or "page"
        url_template = paginator_cfg.get("url_template") or ""
        # 关键词 URL 变形（站点特殊 slug 规则）：[[old, new], ...] 按序字符串替换，
        # 在 URL 编码前应用。如 xasiat 要求空格→"-"、原连字符→"--"（/search/{kw}/）。
        keyword_replace = paginator_cfg.get("keyword_replace") or []
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
                url_template=url_template, keyword_replace=keyword_replace,
            )
            return self._decrypt_results(
                source,
                self._search_html_rendered(source, abs_url, item_cfg, keyword),
                http,
            )

        # 标题/作者字体混淆解密（番茄等）：decryption.targets.title/author → 逐条还原
        dec = self._search_decrypter(source, http)

        def _fetch(page: int):
            """抓取并解析第 page 页，返回 (page, items)。失败返回空列表。"""
            try:
                body = {}
                if method == "POST" and not url_template:
                    # POST：body 加 page 参数（配了 url_template 则改用 GET 模板拼 URL）
                    abs_url = urljoin(source.base_url, base_url)
                    body = {kw_param: keyword, page_param: page}
                    for k, v in extra.items():
                        body.setdefault(k, v)
                    # 前置请求取动态 token（如 rrssk 的 _t）：pre_fetch.{url,regex,param}
                    pre = search_cfg.get("pre_fetch") or {}
                    if pre.get("url") and pre.get("regex"):
                        pre_text = self._http_get(
                            source, urljoin(source.base_url, pre["url"]), http=http
                        )
                        m = _re.search(str(pre["regex"]), pre_text)
                        if m:
                            body.setdefault(pre.get("param") or "token", m.group(1))
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
                        keyword_replace=keyword_replace,
                    )
                    text = self._http_get(source, abs_url, http=http)
            except Exception as exc:  # noqa: BLE001
                # 404 是分页探测末页的正常信号（超过站点总页数），静默不报；
                # 其余失败才警告（网络/反爬）。
                msg = str(exc)
                is_404 = ("404 Client Error" in msg) or ("HTTP 404" in msg)
                if not is_404:
                    log.warning("[%s] 搜索第 %d 页失败：%s", source.source_id, page, exc)
                return (page, [])
            try:
                doc = self._parser.parse(text)
                items = self._parser.parse_items(doc, root_sel, fields, source.base_url)
                # 字段清洗：item.clean.{field} 为 [pattern, repl] 替换对列（同 detail fields.clean）
                for it in items:
                    for _c_key, _pairs in (item_cfg.get("clean") or {}).items():
                        if _c_key in it:
                            it[_c_key] = self._apply_clean(it[_c_key], _pairs)
                return (page, items or [])
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] 搜索第 %d 页解析失败：%s", source.source_id, page, exc)
                return (page, [])

        # 分波受控并发翻页：每波最多 3 页在途请求（防反爬），站点只有几页时
        # 遇连续空页提前停发后续波，避免 max_pages 调大后把空页也抓满
        # （30 页搜索原本会连发 30 个请求）。每波内并发 3 平衡提速与反爬风险。
        # 保留 transports.interval_ms 间隔（_http_get 内部已 sleep）。
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # 合并阶段内置于波循环：每波抓完立即去重并入 results 并回调 on_page——
        # 否则要等全部页抓完才开始回调（慢源多页搜索用户几十秒等不到第 1 页，
        # 且结果一次性整块到达，GUI 侧表现为「一个源一长条」）。波内合并后
        # 用户第 1 波即可看到结果，后续每波抓到即追加（配合 GUI 源级并发，
        # 多源结果天然交错）。
        wave_size = 3
        fetched_total = 0  # 已抓取的累计条数（未去重）：达 max_results 提前停发后续波
        results: List[SearchResult] = []
        seen_urls: set = set()
        # 已合并回调的最大页序（跨波递增，防重复并入/回调）
        last_merged = 0

        def _merge_and_notify(wave_items: dict) -> bool:
            """把新抓完的页按页序去重并入 results，并逐页回调 on_page（边抓边显）。

            空页（抓取失败/真实空页）跳过、后续页照常并入——防止中段失败页
            吞掉后面所有页（历史修复，见下方终止策略）。尾部连续空页
            触发波循环 break，不会合并进来。
            返回 True 表示已达 max_results 上限（须停发后续波）。
            """
            nonlocal last_merged
            for page in sorted(wave_items):
                if page <= last_merged:
                    continue
                last_merged = page
                items = wave_items.get(page) or []
                if not items:
                    continue  # 中段失败页跳过（不吞后续页）
                page_start = len(results)
                for it in items:
                    title = it.get("title", "")
                    url = it.get("url", "")
                    if not title or not url:
                        continue
                    if url in seen_urls:
                        continue  # URL 去重（trtag 等站搜索页 DOM 有重复节点）
                    seen_urls.add(url)
                    if dec is not None:
                        title = dec.decrypt(source, title, target="title")
                    results.append(
                        SearchResult(
                            title=title,
                            url=url,
                            source_id=source.source_id,
                            source_name=source.source_name,
                            cover=Search._clean_cover(it.get("cover", "")),
                            author=(
                                dec.decrypt(source, it.get("author", ""), target="author")
                                if dec is not None
                                else it.get("author", "")
                            ),
                            update=it.get("update", ""),
                        )
                    )
                # 边抓边显示：本页新增结果立即回调（第 1 波秒出，后续波抓到即追加），
                # 不再等全部页抓完才整块回吐
                if on_page and len(results) > page_start:
                    try:
                        on_page(source, page, results[page_start:])
                    except Exception:  # noqa: BLE001
                        pass
                # 达到结果总数上限：本页完整并入后再停（页级精确截断，
                # 与波级 fetched_total 提前停发互补）。
                if max_results and len(results) >= max_results:
                    return True
            return False

        with ThreadPoolExecutor(max_workers=wave_size) as pool:
            start = 1
            while start <= max_pages:
                end = min(start + wave_size - 1, max_pages)
                futs = {pool.submit(_fetch, p): p for p in range(start, end + 1)}
                wave_items: dict = {}
                for fut in as_completed(futs):
                    page, items = fut.result()
                    wave_items[page] = items
                wave_count = sum(len(v) for v in wave_items.values())
                fetched_total += wave_count
                # 本波全空（连续空页）→ 站点已无更多结果，提前停发后续波。
                # 单页失败返回空不会误停：只要本波内还有别的页有结果就继续。
                if end < max_pages and wave_items and not any(wave_items.values()):
                    break
                # 本波抓完立即合并+回调（边抓边显的核心：不等全部页）
                reached_cap = _merge_and_notify(wave_items)
                # 达到结果总数上限（页级精确截断）：停发后续波。
                if reached_cap:
                    break
                start = end + 1
                # 已抓够 max_results 条 → 不再发下一波。此前在合并阶段才按
                # max_results 截断，导致 wave 循环把 max_pages 全部页抓完
                # （51cg1「视频」max_pages=100，白抓 80 页约 40s）才丢弃。
                # 提前停省时；合并阶段仍按 max_results 精确截断兜底。
                if max_results and fetched_total >= max_results:
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
        keyword_replace=None,
    ) -> str:
        """构造搜索第 page 页的 URL（分页拼接规则由源配置决定）。

        url_template（endpoints.search.paginator.url_template，编辑器「换页逻辑」）：
        - 含 {keyword} → 完整 URL 模板：替换 {keyword}/{page} 后拼 base_url
          （如 /search/{keyword}/{page}.html、/search?q={keyword}&p={page}）
        - 不含 {keyword} → 页码拼接片段：?/& 开头作为 query 参数追加到
          「搜索 URL + 关键词」后，否则作为路径后缀（如 -{page}.html）
        - 留空 → 默认：GET 追加 ?{page_param}={page}；{page} 占位走占位替换

        keyword_replace（endpoints.search.paginator.keyword_replace）：
        [[old, new], ...] 按序字符串替换，在 URL 编码前应用到关键词
        （站点 slug 特殊规则，如 xasiat 空格→"-"、原连字符→"--"）。

        全部参数用关键字传递（* 强制），避免位置错位生成 `&1=page` 的错误 URL。
        """
        if keyword_replace:
            for old, new in keyword_replace:
                keyword = keyword.replace(old, new)
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
        from .playwright_helper import (
            fetch_rendered_items_sync,
            fetch_rendered_search_sync,
            fetch_search_response_json,
        )

        rc = item_cfg.get("render_config") or {}
        # 登录态 cookie 注入：GUI 登录弹窗保存的 data/cookies/<source_id>.json。
        # 需要登录的站才注入；无需登录的源（如番茄普通搜索）不引 cookie。
        cookies = None
        if self._cookie_manager is not None:
            cookies = self._cookie_manager.to_playwright_cookies(source.source_id) or None

        def _sel(spec) -> str:
            """root_selector 可能为 {"css": "..."} 或纯字符串。"""
            if isinstance(spec, dict):
                return spec.get("css") or spec.get("xpath") or ""
            return spec or ""

        if rc.get("response_json_url"):
            # 响应监听模式：结果由前端调 JSON 接口异步返回（番茄类 SPA）。
            # DOM 渲染常被自定义字体混淆/渲染异常，接口数据最可靠。
            raw = fetch_search_response_json(
                abs_url,
                rc.get("response_json_url"),
                wait_until=rc.get("wait_until") or "domcontentloaded",
                timeout_ms=int(rc.get("timeout_ms") or 30000),
                extra_delay_ms=int(rc.get("extra_delay_ms") or 2000),
                proxy=(None if source.transports().get("direct")
                       else source.transports().get("proxy")),
                cookies=cookies,
                warm_up_url=rc.get("response_warm_up_url") or source.base_url,
                pages=_response_pages(rc, source),
            )
            items = self._items_from_response_json(raw, rc, source)
        elif rc.get("interact"):
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
                proxy=(None if source.transports().get("direct")
                       else source.transports().get("proxy")),
                cookies=cookies,
            )
        else:
            fields = item_cfg.get("fields") or {}
            if fields:
                # 源已显式声明 item.fields：渲染 HTML 后按配置选择器解析
                # （与发现页 works_list_item 一致），避免通用启发式把标题
                # 取成 img[alt]（h-comic 等 SPA 搜索标题会变「h漫」）。
                from .playwright_helper import fetch_rendered_html_sync

                rendered = fetch_rendered_html_sync(
                    abs_url,
                    wait_for=rc.get("wait_for") or "",
                    wait_until=rc.get("wait_until") or "networkidle",
                    timeout_ms=int(rc.get("timeout_ms") or 30000),
                    extra_delay_ms=int(rc.get("extra_delay_ms") or 2500),
                    proxy=(None if source.transports().get("direct")
                           else source.transports().get("proxy")),
                    cookies=cookies,
                    scroll_to_bottom=bool(rc.get("scroll_to_bottom", False)),
                )
                items = []
                if rendered:
                    doc = self._parser.parse(rendered)
                    parsed = self._parser.parse_items(
                        doc, item_cfg.get("root_selector"), fields, source.base_url
                    )
                    for p_it in parsed:
                        if not p_it.get("url"):
                            continue  # 无作品链接的广告/占位卡：跳过（否则被 join 成站点根）
                        items.append({
                            "title": p_it.get("title", ""),
                            "href": p_it.get("url", ""),
                            "src": p_it.get("cover", ""),
                            "text": p_it.get("title", ""),
                            "author": p_it.get("author", ""),
                        })
            else:
                items = fetch_rendered_items_sync(
                    abs_url,
                    _sel(item_cfg.get("root_selector")),
                    wait_for=rc.get("wait_for") or "",
                    wait_until=rc.get("wait_until") or "networkidle",
                    timeout_ms=int(rc.get("timeout_ms") or 30000),
                    extra_delay_ms=int(rc.get("extra_delay_ms") or 2500),
                    proxy=(None if source.transports().get("direct")
                           else source.transports().get("proxy")),
                    cookies=cookies,
                )
        kw = (keyword or "").strip()
        # 关键词过滤开关：render_config.filter_keyword 显式 false 时跳过硬性过滤。
        # 默认 true 保持原行为（剔除热门榜无关项）；17k 等站搜索结果页本身按站内
        # 模糊匹配（搜"斗破"返回"剑破苍穹"等相似书名），root_selector 已限定结果区，
        # 硬筛会误杀合法模糊结果 → 该源设 filter_keyword=false。
        filter_keyword = bool(rc.get("filter_keyword", True))
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
            if filter_keyword and kw and kw not in title and kw not in it.get("text", ""):
                continue
            seen_urls.add(url)
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    source_id=source.source_id,
                    source_name=source.source_name,
                    cover=Search._clean_cover(it.get("src", "")),
                    author=str(it.get("author") or ""),
                )
            )
        # 需要登录的渲染源（render_config.login_required，如番茄）：未登录时
        # 搜索页服务端给空 body/登录墙 → 渲染结果恒为空。与其显示「什么都没
        # 搜到」，不如明确引导用户去登录（否则用户只会反复重试搜索）。
        if not results and kw and rc.get("login_required"):
            if (
                self._cookie_manager is None
                or not self._cookie_manager.is_logged_in(source.source_id)
            ):
                raise SourceError(
                    f"「{source.source_name}」搜索需登录，请在「源管理 → 登录」"
                    " 完成登录（保存 Cookie）后再搜索"
                )
        return results

    # ------------------------------------------------------------------ #
    @staticmethod
    def _items_from_response_json(raw: str, rc: dict, source: SourceConfig) -> list:
        """把 render_config.response_json_url 模式抓到的接口 JSON 转成结果项。

        返回与 fetch_rendered_*_sync 同构的 [{title,href,src,text,author}]。
        render_config.response_items 为列表的 jsonpath；response_fields 为
        字段映射（title/author/cover/summary 取值表达式；url_template 支持
        {base} 与 {字段名} 模板）。解析失败/空 → []。
        """
        items: list = []
        try:
            data = json.loads(raw or "{}")
        except Exception:  # noqa: BLE001
            return items
        path = str(rc.get("response_items") or "")
        lst = jsonpath(data, path) if path else data
        if isinstance(lst, dict):
            lst = [lst]
        elif lst is None:
            lst = []
        if not isinstance(lst, list):
            return items
        fields = rc.get("response_fields") or {}
        url_tpl = str(fields.get("url_template") or fields.get("url") or "")

        def _g(it: dict, expr: str) -> str:
            expr = str(expr or "")
            val = jsonpath(it, expr) if "." in expr or expr.startswith("$") else it.get(expr)
            if val is None:
                return ""
            return str(val)

        for it in lst:
            if not isinstance(it, dict):
                continue
            title = _g(it, fields.get("title") or "title")
            href = ""
            if url_tpl:
                try:
                    href = url_tpl.replace("{base}", source.base_url.rstrip("/"))
                    for k, v in it.items():
                        href = href.replace("{%s}" % k, str(v) if v is not None else "")
                except Exception:  # noqa: BLE001
                    href = ""
            cover = _g(it, fields.get("cover") or "cover")
            author = _g(it, fields.get("author") or "author")
            summary = _g(it, fields.get("summary") or "summary")
            if not href.startswith("http") and href:
                href = source.base_url.rstrip("/") + href
            if title and href:
                items.append({
                    "title": title,
                    "href": href,
                    "src": cover,
                    "text": summary,
                    "author": author,
                })
        return items

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
        method = (cfg.get("method") or "GET").upper()
        body = cfg.get("body") or {}

        # 翻页：读 constraints.search.max_pages（默认 1），多页合并去重
        constraints = source.raw.get("constraints") or {}
        max_pages = int((constraints.get("search") or {}).get("max_pages") or 1)

        results: List[SearchResult] = []
        seen_urls: set = set()
        for page in range(1, max_pages + 1):
            if method == "POST":
                # JSON API（GraphQL 等）：POST body 递归替换占位符
                body_filled = fill_json(body, keyword=keyword, page=str(page))
                for k, v in params.items():
                    body_filled.setdefault(k, fill_json(v, keyword=keyword, page=str(page)))
                sign_cfg = cfg.get("sign") or {}
                strategy = sign_cfg.get("strategy")
                if strategy:
                    from .signers import get_signer

                    signer = get_signer(strategy, http)
                    body_filled = signer.sign(body_filled)
                resp = http.post_json(
                    urljoin(source.base_url, api_url),
                    json_body=body_filled,
                    headers=source.request_headers(),
                    timeout=float(source.transports().get("timeout") or http.defaults.timeout),
                    proxy_pool=source.proxy_pool(),
                )
            elif params:
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
    def _apply_clean(value, pairs) -> str:
        """按替换对列表清洗字段：pattern 以 "re:" 前缀按正则替换（同 content._clean_field）。"""
        import re as _re2

        value = str(value or "").strip()
        for pat, repl in (pairs or []):
            if not pat:
                continue
            if pat.startswith("re:"):
                value = _re2.sub(pat[3:], str(repl or ""), value)
            else:
                value = value.replace(pat, str(repl or ""))
        return value.strip()

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
            interval_ms=int(source.transports().get("interval_ms") or http.defaults.interval_ms),
            encoding=source.transports().get("charset"),
            proxy_pool=source.proxy_pool(),
            direct=bool(source.transports().get("direct")),
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
            retries=int(source.transports().get("retries") or http.defaults.retries),
            interval_ms=int(source.transports().get("interval_ms") or http.defaults.interval_ms),
            encoding=source.transports().get("charset"),
            proxy_pool=source.proxy_pool(),
        )

    @staticmethod
    def _simple_getpath(data, path: str):
        return jsonpath(data, path)

    @staticmethod
    def _tpl(item: dict, spec):
        if spec is None:
            return ""
        if isinstance(spec, str) and "{" in spec:
            return fill_template(spec, item)
        if isinstance(spec, str):
            val = item.get(spec, "")
            # 嵌套路径（如 images.posterZhHans）：平铺键取不到时按点路径取值
            if not val and "." in spec:
                val = jsonpath(item, spec)
            return val if val is not None else ""
        return ""
