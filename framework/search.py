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
        self, source: SourceConfig, keyword: str, http: Optional[HttpClient] = None
    ) -> List[SearchResult]:
        """单源搜索。优先 yt-dlp 引擎，其次 api_endpoints.search，否则 endpoints.search。

        http：可指定独立 HttpClient（并发搜索时每 worker 各用一个，requests.Session
        非线程安全）。None 用 self._http。
        """
        http = http or self._http
        api = source.raw.get("api_endpoints") or {}
        search_cfg = api.get("search") or {}
        if search_cfg.get("engine") == "ytdlp":
            return self._search_ytdlp(source, keyword, search_cfg, http=http)
        if search_cfg:
            return self._search_api(source, keyword, http=http)
        return self._search_html(source, keyword, http=http)

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
        self, source: SourceConfig, keyword: str, http: Optional[HttpClient] = None
    ) -> List[SearchResult]:
        """HTML 站搜索（endpoints.search）。"""
        http = http or self._http
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
            # 固定附加参数（source-schema §3.2 extra_params）并入 POST body
            extra = search_cfg.get("extra_params") or {}
            if isinstance(extra, dict):
                for k, v in extra.items():
                    body.setdefault(k, v)
            # 需要 http 支持 post_form
            text = self._http_post_form(source, abs_url, body, http=http)
        else:
            sep = "&" if "?" in base_url else "?"
            abs_url = urljoin(source.base_url, f"{base_url}{sep}{kw_param}={quote(keyword)}")
            text = self._http_get(source, abs_url, http=http)

        doc = self._parser.parse(text)
        item_cfg = search_cfg.get("item") or {}
        root_sel = item_cfg.get("root_selector")
        fields = item_cfg.get("fields") or {}
        if not root_sel:
            return []
        items = self._parser.parse_items(doc, root_sel, fields, source.base_url)
        results = []
        seen_urls = set()
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
