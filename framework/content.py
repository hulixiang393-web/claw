"""内容模块（content.py）。

消费 endpoints.detail 与 endpoints.content：
- fetch_detail(source, url)  → 详情元数据 + 章节列表
- fetch_cover(source, url)   → 详情页封面（轻量，回填用，不抓章节）
- fetch_chapter(source, url) → 单章正文（按 content_type 分派）

章节列表自动排序（三种类型 novel/comic/video 共用）：
- 源配置 chapter_order: "desc" 仅做整体反转（兼容旧站）
- 其后按标题序号自动升序排序（代码层，无需源配置）：
  - **卷号+章号复合排序**：`第1卷 第3章` < `第2卷 第1章`（卷优先，卷内按章）。
  - 数字写法支持阿拉伯 / 汉字（含大写）/ 罗马（全角+半角）/ 全角（１２３）/ 英文（Vol.1 / Ch.5）。
  - 可恢复「先最新后顺序」「反爬乱序」的目录到正序；
  - 无数字章节（楔子/序章/番外/卷名）保持原相对顺序排末尾；
  - 原顺序本就正序则保持不变（稳定排序）。

对应 design.md §5.2 与 ui-discover.md 详情抽屉、ui-reader.md。
"""

from __future__ import annotations

import json
import logging
import re as _re
import time
from dataclasses import dataclass, field
from typing import List, Optional

log = logging.getLogger(__name__)

# 精确广告特征：路径段或明确广告词（不用宽泛的 ad_，防误杀正常 URL）
_AD_IMAGE_RE = _re.compile(
    r"(?:/ads?/|/ad-|_ad\.|/advert|/banner|/promo|/ad\.|/ad/|\.ads\.)"
)

from .config import SourceConfig
from .errors import ContentMissingError
from .http import HttpClient
from .parser import Parser
from .selfcheck import StructureChecker
from . import utils
from .chapter_sort import chapter_label, _extract_chapter_number, _sort_chapters
from .health import report_bg_check


def _strip_html_tags(text: str) -> str:
    """去 HTML 标签提取纯文本（解密后含 <p>...</p> 等标签时清理）。

    <p>段落</p> → 段落文本，段落间换行；非标签文本原样保留。
    """
    from lxml.html import fromstring as _html_fromstring
    from lxml.html import tostring as _html_tostring

    # 包一层 div 让 lxml 处理多段落
    try:
        doc = _html_fromstring(f"<div>{text}</div>")
        parts = []
        for el in doc.iter():
            if el.tag == "br":
                parts.append("\n")
            elif el.text and el.tag != "div":
                parts.append(el.text.strip())
        result = "\n".join(p for p in parts if p).strip()
        return result if result else text
    except Exception:
        # lxml 解析失败 → 去标签兜底
        clean = _re.sub(r"<[^>]+>", "\n", text)
        return _re.sub(r"\n{3,}", "\n\n", clean).strip()


@dataclass
class Chapter:
    title: str
    url: str
    source_id: str = ""  # 该集所属播放源标识（换源站：sid 值）；普通源空


@dataclass
class Detail:
    """详情页元数据 + 章节列表。"""

    source_id: str
    content_type: str
    url: str
    title: str = ""
    author: str = ""
    cover: str = ""
    status: str = ""
    summary: str = ""
    tags: List[str] = field(default_factory=list)
    chapters: List[Chapter] = field(default_factory=list)
    # 播放源列表（换源站）：[{sid, name, from_, ps, parse}]；无换源配置时为空
    source_list: List[dict] = field(default_factory=list)


class Content:
    def __init__(
        self,
        http: HttpClient,
        parser: Parser,
        checker: StructureChecker,
        decrypter: Optional["Decrypter"] = None,
        health_reporter=None,
    ):
        self._http = http
        self._parser = parser
        self._checker = checker
        self._decrypter = decrypter
        self._health_reporter = health_reporter  # 可选：update_health(source_id, state, error)
        # yt-dlp 流 URL 缓存（同视频短时复用，避免重复签名等待）
        self._ytdlp_stream_cache: dict = {}
        self._ytdlp = None  # 懒加载单例，复用 yt-dlp 子进程
        # 视频详情页 HTML 复用缓存：single_chapter 源（missav/hanime1/18j/18mh-video
        # 等）取流 URL == 详情 URL，fetch_detail 已整页下载，取流时复用免二次下载
        # （慢站反爬延迟下详情+取流各一次，复用省约一半同步等待）。只留最近一部
        # （每次详情加载重新填充，天然不过期）。
        self._video_html_cache: dict = {}
        # 详情页 HTML 短 TTL 缓存：重复打开同一本书免二次整页下载（慢站详情慢）。
        # 只缓存最近 _detail_html_max 条，防长期会话无界增长（dict 保持插入序）。
        self._detail_html_cache: dict = {}  # {(source_id, abs_url): (expire_ts, html)}
        self._detail_html_ttl = 300.0  # 5 分钟
        self._detail_html_max = 50

    def _bg_check(self, source: SourceConfig, abs_url: str) -> None:
        """后台线程执行结构自检，不阻塞抓取（阅读/下载/播放提速）。

        checker.check() 会对同一 URL 额外发一次 GET（最长 timeout×retries），
        同步等待会让每章下载/每页漫画/每次取流慢一倍。自检仅健康监控，
        移后台 daemon 线程（与 discovery 一致）。

        状态映射（与 GUI 诊断一致，避免把活源标死）：
        - 自检通过（True）→ 绿 ok
        - 软失败（False：网络/超时/反爬/未达硬失败阈值）→ 黄 warn，不标 broken
        - 硬失败（StructureChangedError：结构确认变更）→ 红 broken
        """
        # 源配置 diagnostics.selfcheck.strategy="off" → 完全关闭后台自检。
        # 自检会对同一 URL 并发第二 GET（后台线程），fanqie 等已配 off 的源
        # 继续触发会放大反爬尖峰（实测详情慢 ~6s），此处跳过不再发请求。
        if (
            (source.raw.get("diagnostics") or {}).get("selfcheck") or {}
        ).get("strategy") == "off":
            return
        report_bg_check(self._checker, self._health_reporter, source, abs_url)

    # ------------------------------------------------------------------ #
    def _headers(self, source: SourceConfig) -> dict:
        return source.request_headers()

    def _endpoint_headers(self, source: SourceConfig, cfg: dict) -> dict:
        """api_endpoints.<endpoint> 级请求头：源级 headers 基础上做端点覆盖。

        cfg["headers"] 支持：
            "Header": "value"    覆盖/新增该头
            "Header": "" 或 null  移除该头（大小写不敏感）
        用途：B 站 detail 接口带 Referer 会固定 412，但 CDN 取流又必须带
        Referer —— 用端点级覆盖在 detail 上摘掉 Referer，源级保持不动。
        """
        headers = self._headers(source)
        overrides = cfg.get("headers") or {}
        if not overrides:
            return headers
        for k, v in overrides.items():
            if v is None or v == "":
                for hk in list(headers):
                    if hk.lower() == k.lower():
                        headers.pop(hk, None)
            else:
                for hk in list(headers):
                    if hk.lower() == k.lower():
                        headers.pop(hk, None)
                headers[k] = v
        return headers

    def _timeout(self, source: SourceConfig) -> float:
        return float(source.transports().get("timeout") or self._http.defaults.timeout)

    def _retries(self, source: SourceConfig) -> int:
        return int(source.transports().get("retries") or self._http.defaults.retries)

    def _interval_ms(self, source: SourceConfig) -> int:
        return int(source.transports().get("interval_ms") or self._http.defaults.interval_ms)

    def _abs_url(self, source: SourceConfig, url: str) -> str:
        return utils.abs_url(source.base_url, url)

    def _get(self, source: SourceConfig, url: str, http=None) -> str:
        """抓取页面 HTML。http 可传独立 HttpClient（并行翻页时避免共享
        self._http 的 requests.Session 跨线程竞态）；缺省用共享实例。"""
        client = http or self._http
        abs_url = self._abs_url(source, url)
        return client.get_text(
            abs_url,
            headers=self._headers(source),
            timeout=self._timeout(source),
            retries=self._retries(source),
            interval_ms=self._interval_ms(source),
            encoding=source.transports().get("charset"),
            proxy_pool=source.proxy_pool(),
        )

    def _get_detail_html(self, source: SourceConfig, url: str, abs_url: str) -> str:
        """详情页 HTML：短 TTL 缓存命中直接返回，否则抓取并缓存。

        重复打开同一本书（详情抽屉多次/刷新）免二次整页下载，慢站详情慢时
        省约整页等待。缓存键 (source_id, abs_url)，TTL 5 分钟，超上限丢最旧。
        """
        key = (source.source_id, abs_url)
        hit = self._detail_html_cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        html = self._get(source, url)
        self._detail_html_cache[key] = (time.time() + self._detail_html_ttl, html)
        if len(self._detail_html_cache) > self._detail_html_max:
            # dict 保持插入序：弹出最先插入的一条（近似 LRU）
            self._detail_html_cache.pop(next(iter(self._detail_html_cache)))
        return html

    def _content_block(self, source: SourceConfig) -> dict:
        """endpoints.content 配置块（可能缺失 → {}）。"""
        return source.raw.get("endpoints", {}).get("content") or {}

    def fetch_cover(self, source: SourceConfig, url: str) -> str:
        """按需抓详情页封面（cover_backfill 源：列表页纯文本无封面，详情页补回）。

        轻量：只取 endpoints.detail.fields.cover 选择器，不抓章节列表/正文。
        复用 _detail_html_cache，重复打开/回填同一本书不重复下载。
        异常/解析为空 → 返回 ""（失败静默，调用方跳过该条、不影响其它条目）。
        """
        try:
            abs_url = self._abs_url(source, url)
            html = self._get_detail_html(source, url, abs_url)
            doc = self._parser.parse(html)
            detail_cfg = source.get_detail_config()
            cover_sel = (detail_cfg.get("fields") or {}).get("cover")
            if not cover_sel:
                return ""
            return self._parser.extract_first(doc, cover_sel, source.base_url)
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------ #
    def fetch_detail(self, source: SourceConfig, url: str) -> Detail:
        """抓取详情页：元数据 + 章节列表。

        优先 api_endpoints.detail（JSON API，可选 sign 签名）；
        否则走 endpoints.detail HTML 解析。
        """
        api = source.raw.get("api_endpoints") or {}
        detail_api = api.get("detail") or {}
        if detail_api:
            if detail_api.get("engine") == "ytdlp":
                return self._fetch_detail_ytdlp(source, url, detail_api)
            return self._fetch_detail_api(source, url, detail_api)

        # 详情 URL 规范化：endpoints.detail.url_suffix 配置为 URL 尾缀补全
        # （如 MacCMS 变体列表 href 不带 .html 但详情页必须 .html，否则返回
        # 反爬落地页）。仅路径末尾补，保留 query；URL 已带后缀则跳过。
        detail_cfg0 = source.get_detail_config()
        suffix = (detail_cfg0 or {}).get("url_suffix") or ""
        if suffix and url:
            path, _, query = url.partition("?")
            if not path.endswith(suffix):
                url = path + suffix + ("?" + query if query else "")

        abs_url = self._abs_url(source, url)
        self._bg_check(source, abs_url)
        html = self._get_detail_html(source, url, abs_url)
        if source.content_type == "video":
            # 缓存视频详情页 HTML：single_chapter 源取流 URL == 详情 URL，
            # fetch_video_episode 复用，避免二次整页下载（慢站省约一半等待）。
            self._video_html_cache = {
                (source.source_id, abs_url): html
            }
        doc = self._parser.parse(html)

        detail_cfg = source.get_detail_config()
        fields = detail_cfg.get("fields") or {}
        title = self._parser.extract_first(doc, fields.get("title"))
        # 标题清理：fields.title_clean 为 [["在线观看", ""], ...] 替换对列表
        for _pat, _repl in (fields.get("title_clean") or []):
            if _pat:
                title = title.replace(_pat, _repl)
        # 书名修正：若 title 像是站点名（短/非书名），尝试 og:novel:book_name
        book_fields = fields.get("book_name")
        if book_fields:
            book = self._parser.extract_first(doc, book_fields)
            if book:
                title = book
        cover_cfg = fields.get("cover")
        cover = self._parser.extract_first(doc, cover_cfg, source.base_url)
        # 封面兜底：选择器封面为空/命中共享占位时，按 fields.cover 的 regex / state
        # 配置从 JSON-LD / window.__INITIAL_STATE__ 取真实封面（番茄等 SSR 占位图源）。
        cover = self._extract_cover_fallback(source, html, cover_cfg, cover)
        detail = Detail(
            source_id=source.source_id,
            content_type=source.content_type,
            url=url,
            title=title,
            author=self._parser.extract_first(doc, fields.get("author")),
            cover=cover,
            status=self._parser.extract_first(doc, fields.get("status")),
            summary=self._parser.extract_first(doc, fields.get("summary")),
        )
        # 标签（可空）
        tags = self._parser.extract(doc, fields.get("tags"))
        detail.tags = tags

        # 章节列表（按类型取 content 配置，传书名用于标题清理；
        # html 供目录页 id 从详情页 HTML 提取，如 dm5 COMIC_MID）
        detail.chapters = self._fetch_chapters(
            source, doc, book_title=detail.title, detail_url=url, html=html
        )
        # 视频系列聚合：内容配置 content.episode.series.enabled 且标题含系列标记
        # （如「第N話」）→ 搜同系列基底，把其他分部并入分集列表（hanime1 类站
        # 每个分部是独立视频，站内无分集列表，靠标题系列名聚合）。
        if source.content_type == "video":
            detail.chapters = self._maybe_expand_video_series(
                source, detail, detail.chapters
            )

        # 播放源列表（换源站）：解析 source_switch 配置的可用源
        detail.source_list = self._parse_source_list(source, html)

        return detail

    def _maybe_expand_video_series(self, source, detail, chapters) -> list:
        """把同系列的独立分部并入视频分集列表（配置 content.episode.series 驱动）。

        - base_from_title：提取系列基底的正则（字符串或正则列表，如
          `(.+?)第(\\d+)[話话]`、`(.+?)(?:前編|後編)`——base=捕获组1，话数=捕获组2
          数字或 part_map 文本标记映射）
        - part_map：文本分部标记 → 话数（如 {"前編":1,"後編":2}）
        - min_parts：分部数 ≥ 该值才聚合（防误把普通视频当系列）
        返回章节列表：当前集置首，其余分部按话数正序在后。
        """
        block = self._content_block(source) or {}
        s_cfg = (block.get("episode") or {}).get("series") or {}
        if not s_cfg.get("enabled"):
            return chapters
        patterns = self._series_patterns(s_cfg)
        if not patterns:
            return chapters
        match = self._series_match(detail.title or "", patterns, s_cfg.get("part_map") or {})
        if not match:
            return chapters  # 标题无系列标记 → 非系列作品
        base, _ = match
        # 搜系列基底，收集分部 {话数: (标题, url)}（不限精确基底——站点可能以
        # 中文/日文不同标题展示同系列，如 hanime1 详情日文、搜索中文）
        parts = self._search_video_series_parts(source, base, patterns, s_cfg.get("part_map") or {})
        min_parts = int(s_cfg.get("min_parts") or 2)
        if len(parts) < min_parts:
            return chapters  # 分部太少，保持单集
        # 确认同一系列：当前视频 URL 出现在搜索结果中（标题语言可能不同，
        # 用 URL 判定最可靠）。
        cur_url = detail.url
        if cur_url not in {u for _, u in parts.values()}:
            return chapters  # 搜索没命中当前集，可能是误判系列
        cur = chapters[0] if chapters else None
        merged = [cur] if cur is not None else []
        for n in sorted(parts):
            title, url = parts[n]
            if url == cur_url:
                continue  # 当前集已置首
            merged.append(Chapter(title=title, url=url))
        return merged

    @staticmethod
    def _series_patterns(s_cfg: dict) -> list:
        """规范化 series.base_from_title 为正则列表。"""
        raw = s_cfg.get("base_from_title") or ""
        if isinstance(raw, str):
            raw = [raw]
        return [p for p in raw if p]

    def _series_match(self, title: str, patterns: list, part_map: dict):
        """在标题上尝试系列模式，返回 (base, part) 或 None。"""
        import re as _re

        for pat in patterns:
            m = _re.search(pat, title or "")
            if not m:
                continue
            base = (m.group(1) or "").strip()
            if not base:
                continue
            part = None
            if m.lastindex and m.lastindex >= 2:
                g2 = (m.group(2) or "").strip()
                if g2.isdigit():
                    part = int(g2)
            if part is None:
                # 文本分部标记（前編/後編/上篇/下篇…）→ part_map
                for k, v in (part_map or {}).items():
                    if k in (title or ""):
                        part = v
                        break
            if part is not None:
                return base, part
        return None

    def _search_video_series_parts(self, source, base: str, patterns: list, part_map: dict) -> dict:
        """搜系列基底，返回分部 {话数: (标题, URL)}。失败返回 {}。

        不限精确基底匹配（同一系列在 hanime1 详情/搜索用不同语言标题），
        由调用方用「当前 URL 命中」确认系列。
        """
        try:
            from .search import Search

            results = Search(self._http, self._parser, self._checker).search_one(source, base)
        except Exception:  # noqa: BLE001
            return {}
        parts = {}
        for r in results:
            match = self._series_match(r.title or "", patterns, part_map)
            if not match:
                continue
            _, rpart = match
            parts.setdefault(rpart, (r.title or "", r.url or ""))
        return parts

    def _fetch_detail_ytdlp(self, source: SourceConfig, url: str, cfg: dict) -> Detail:
        """yt-dlp 引擎：详情元数据 + 章节列表。"""
        yt = self._get_ytdlp()
        try:
            d = yt.fetch_detail(url)
        except Exception as exc:
            raise ContentMissingError(
                f"yt-dlp 详情失败（{url}）：{exc}", source_id=source.source_id
            ) from exc
        detail = Detail(
            source_id=source.source_id,
            content_type=source.content_type,
            url=url,
            title=d.get("title") or "",
            author=d.get("author") or "",
            cover=d.get("cover") or "",
            status=d.get("status") or "",
            summary=d.get("summary") or "",
        )
        chapters = d.get("chapters") or []
        detail.chapters = [Chapter(title=c.get("title") or "", url=c.get("url") or url) for c in chapters]
        return detail

    # ------------------------------------------------------------------ #
    def _fetch_detail_api(self, source: SourceConfig, url: str, cfg: dict) -> Detail:
        """api_endpoints.detail：JSON API 取详情元数据 + 章节列表。

        支持 sign 签名。字段映射用 field_extractors（目标字段 → JSONPath）。
        章节列表用 chapters 块：
            items          JSONPath 到章节列表
            title / number 每项标题/序号字段名
            url_template   章节 URL 模板（可用 {cid} / {page} / {part} 占位）
        """
        from urllib.parse import urlencode, urljoin

        api_url = str(cfg.get("url") or "")
        params = cfg.get("params") or {}
        filled = {}
        m_bv = _re.search(r"(BV[0-9A-Za-z]+)", url)
        bvid = m_bv.group(1) if m_bv else url.split("/")[-1]
        for k, v in params.items():
            filled[k] = str(v).replace("{bvid}", bvid).replace("{id}", bvid)
        sign_cfg = cfg.get("sign") or {}
        strategy = sign_cfg.get("strategy")
        if strategy:
            from .signers import get_signer

            signer = get_signer(strategy, self._http)
            filled = signer.sign(filled)
        qs = urlencode(filled)
        abs_url = urljoin(source.base_url, api_url)
        if "?" in api_url:
            abs_url = f"{abs_url}&{qs}"
        else:
            abs_url = f"{abs_url}?{qs}"
        resp = self._http.get_json(
            abs_url,
            headers=self._endpoint_headers(source, cfg),
            timeout=self._timeout(source),
            retries=self._retries(source),
            proxy_pool=source.proxy_pool(),
        )
        if not isinstance(resp, dict):
            return Detail(source_id=source.source_id, content_type=source.content_type, url=url)
        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp

        extractors = cfg.get("field_extractors") or {}
        title = self._jsonpath(data, extractors.get("title")) or ""
        detail = Detail(
            source_id=source.source_id,
            content_type=source.content_type,
            url=url,
            title=str(title),
            author=str(self._jsonpath(data, extractors.get("author")) or ""),
            cover=str(self._jsonpath(data, extractors.get("cover")) or ""),
            status=str(self._jsonpath(data, extractors.get("status")) or ""),
            summary=str(self._jsonpath(data, extractors.get("summary")) or ""),
        )
        # 标签（可空，逗号分隔列表）
        tags = self._jsonpath(data, extractors.get("tags"))
        if isinstance(tags, list):
            detail.tags = [str(t) for t in tags]
        elif tags:
            detail.tags = [str(t) for t in str(tags).split(",")]

        # 章节列表
        chapters_cfg = cfg.get("chapters") or {}
        items = self._jsonpath(data, chapters_cfg.get("items"))
        if isinstance(items, list):
            title_key = chapters_cfg.get("title") or "title"
            num_key = chapters_cfg.get("number")
            url_tpl = chapters_cfg.get("url_template") or url
            for it in items:
                if not isinstance(it, dict):
                    continue
                c_title = str(it.get(title_key) or f"第{len(detail.chapters)+1}集")
                c_url = url_tpl
                if num_key:
                    c_url = c_url.replace("{cid}", str(it.get(num_key) or ""))
                c_url = c_url.replace("{bvid}", bvid).replace("{id}", bvid)
                # 模板里 {title}/{part} 等占位填充
                c_url = utils.fill_template(c_url, it)
                detail.chapters.append(Chapter(title=c_title, url=c_url))
        return detail

    @staticmethod
    def _jsonpath(node, path: str):
        """极简 JSONPath：data.owner.name / data.list.0 点号路径。"""
        return utils.jsonpath(node, path)

    def _extract_cover_fallback(
        self, source: SourceConfig, html: str, cover_cfg, current: str = ""
    ) -> str:
        """详情封面兜底：选择器封面为空/命中占位时，按配置从 JSON-LD / INITIAL_STATE 取真实封面。

        fields.cover 可选键（配置驱动，全部容错，失败保留原值）：
            regex       对原始 html 做 re.search 取 group(1)（如 JSON-LD image[0]）
            state       解析 window.__INITIAL_STATE__ 后按 JSONPath 取（如 page.thumbUri）
            placeholder 共享占位封面标记（子串/正则，命中视为无有效封面 → 触发兜底）
        尝试顺序：regex → state。
        """
        if current and not self._is_placeholder_cover(current, cover_cfg):
            return current
        cfg = cover_cfg or {}
        # 1) 正则兜底（JSON-LD image 等）
        pat = cfg.get("regex")
        if pat:
            m = _re.search(pat, html)
            if m:
                cover = m.group(1) if m.groups() else m.group(0)
                cover = cover.replace("\\/", "/").strip()
                if cover:
                    return utils.abs_url(source.base_url, cover)
        # 2) INITIAL_STATE 兜底（page.thumbUri 等）
        state_path = cfg.get("state")
        if state_path:
            state = self._parse_initial_state(html)
            if state:
                val = self._jsonpath(state, state_path)
                if val:
                    return str(val)
        return current

    @staticmethod
    def _is_placeholder_cover(cover: str, cover_cfg) -> bool:
        """封面是否命中配置的占位标记（命中 → 视为无有效封面，触发兜底）。"""
        cfg = cover_cfg if isinstance(cover_cfg, dict) else {}
        pat = cfg.get("placeholder")
        if not pat or not cover:
            return False
        try:
            return _re.search(str(pat), cover) is not None
        except Exception:  # noqa: BLE001  非法正则退化为子串匹配
            return str(pat) in cover

    @staticmethod
    def _parse_initial_state(html: str):
        """从 html 中提取 window.__INITIAL_STATE__ = {...}; 并解析为 dict。

        用大括号平衡扫描（跳过字符串内）切出 JSON，容错：找不到/解析失败返回 None。
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
                        return json.loads(html[start:i + 1])
                    except Exception:  # noqa: BLE001
                        return None
        return None

    # ------------------------------------------------------------------ #
    def _fetch_chapters(
        self,
        source: SourceConfig,
        doc,
        book_title: str = "",
        detail_url: str = "",
        html: str = "",
    ) -> List[Chapter]:
        """从详情页提取章节列表。按 content_type 读 content 配置。

        支持源配置：
        - list.chapter_order: "desc" → HTML 倒序，反转成正序（asc）
        - list.title_clean: true    → 从章节标题移除书名
        - content.<type>.single_chapter: true → 详情页即单章图集（无章节列表），
          直接以详情 URL 作为唯一章节（如 wnacg 每 aid 一图集）。
        """
        content_cfg = self._content_block(source)
        if source.content_type == "novel":
            block = content_cfg.get("chapter") or {}
        elif source.content_type == "comic":
            block = content_cfg.get("page") or {}
        else:
            block = content_cfg.get("episode") or {}
        # 换源站：分集从 source_switch.ep_list_selector 提取（按当前 sid）
        switch_cfg = block.get("source_switch") or {}
        if switch_cfg.get("ep_list_selector") and detail_url:
            # 当前 sid 从详情 URL 提取；URL 无 sid 时默认用第一个播放源（sid=1）
            m_sid = _re.search(r"sid=(\d+)", detail_url)
            sid = m_sid.group(1) if m_sid else switch_cfg.get("default_sid", "1")
            return self._fetch_chapters_for_source(
                source, doc, detail_url, sid
            )
        # 单章图集：详情页即唯一章节，无需解析章节列表
        if block.get("single_chapter"):
            return [
                Chapter(
                    title=book_title or "全本",
                    url=detail_url or self._abs_url(source, source.base_url),
                )
            ]
        list_cfg = block.get("list") or {}
        root_sel = list_cfg.get("root_selector")
        fields = list_cfg.get("fields") or {}
        if not root_sel and not list_cfg.get("chapters_api"):
            return []

        # 独立目录页：content.chapter.list.chapters_url 为模板（如 /other/chapters/id/{id}.html），
        # 从详情 URL 提取 book id 并二次抓取，在该页提取完整章节列表。
        # id 来源两级：详情 URL（/{id}/ 段）→ 详情页 HTML 正则（chapters_id_regex，
        # 别名 URL 站如 dm5 /manhua-xxx/ 的 JS 变量 COMIC_MID）。
        chapters_url_tpl = list_cfg.get("chapters_url") or ""
        if chapters_url_tpl and detail_url:
            m_id = _re.search(r"/(?:novel|book|comic|detail)/(\w+)", detail_url)
            book_id = m_id.group(1) if m_id else ""
            if not book_id:
                id_regex = list_cfg.get("chapters_id_regex")
                if id_regex and html:
                    m_id2 = _re.search(str(id_regex), html)
                    if m_id2:
                        book_id = m_id2.group(1) if m_id2.groups() else m_id2.group(0)
            if book_id:
                try:
                    cat_url = chapters_url_tpl.replace("{id}", book_id)
                    cat_html = self._get(source, cat_url)
                    cat_doc = self._parser.parse(cat_html)
                    doc = cat_doc  # 用目录页 doc 提取章节
                except Exception:
                    pass  # 目录页抓取失败回退详情页

        items = None
        # API 目录（SPA 目录站，如纵横小说）：详情页目录为 JS 渲染（SSR 空），
        # HTML 解析抓不到；直调官方章节 API 拿全部章节。
        # 配置（content.chapter.list.chapters_api）：
        #   url             章节 API（支持 {id} 占位）
        #   method          GET / POST（缺省 GET）
        #   body            请求体（POST，支持 {id} 占位）
        #   body_format     form（urlencoded，缺省）/ json
        #   response_path   JSONPath 定位卷/章列表（如 result.chapterList）
        #   children_path   卷内章节子数组字段（如 chapterViewList），缺省把
        #                   response_path 结果直接当章节列表
        #   item_fields     title → 章节名字段；chapter_id → 章节 ID 字段
        #   url_template    章节 URL 模板（支持 {id}/{chapter_id}/{任意字段} 占位）
        chapters_api = list_cfg.get("chapters_api") or {}
        if chapters_api and detail_url:
            try:
                api_items = self._fetch_chapters_api(source, chapters_api, detail_url)
                if api_items is not None:
                    items = api_items
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] API 目录抓取失败，回退 HTML 解析：%s", source.source_id, exc)
        if items is None:
            items = self._parser.parse_items(doc, root_sel, fields, source.base_url)
        chapters: List[Chapter] = []
        seen_norm = set()
        seen_title = set()

        def _norm_url(url: str) -> str:
            """归一化 URL：去尾 /、去 ?fragment、转小写 scheme-less。"""
            from urllib.parse import urlparse, urlunparse
            p = urlparse(url)
            return urlunparse(p._replace(fragment="")).rstrip("/").lower()

        def clean_title(t: str) -> str:
            """从章节标题移除书名 + 清理多余分隔符/后缀词。"""
            t = t.strip()
            if not t:
                return t
            if book_title:
                t = t.replace(book_title, "")
            # 移除《》及其中的书名残留（含空《》）
            t = _re.sub(r"《[^》]*》", "", t)
            # 移除常见后缀词
            for suffix in ("最新章节", "全文阅读", "全文免费阅读", "最新", "TXT", "txt"):
                t = t.replace(suffix, "")
            # 清理孤立分隔符
            t = t.replace(" - ", " ").strip(" -：:　 ").strip()
            t = _re.sub(r"\s+", " ", t)
            return t

        for it in items:
            url = it.get("url", "")
            if not url:
                continue
            # URL 归一化去重（不同 URL 可能指向同一章节，如末尾 /、参数顺序）
            nurl = _norm_url(url)
            if nurl in seen_norm:
                continue
            seen_norm.add(nurl)
            title = it.get("title", "")
            if list_cfg.get("title_clean"):
                title = clean_title(title)
            # 标题规范化去重（清理后标题相同跳过）
            title_key = _re.sub(r"\s+", " ", title.strip().lower())
            if title_key and title_key in seen_title:
                continue
            if title_key:
                seen_title.add(title_key)
            chapters.append(Chapter(title=title or f"第{len(chapters)+1}章", url=url))

        # 倒序反转（HTML 倒序 → 正序）
        order = list_cfg.get("chapter_order", "asc")
        if order == "desc":
            chapters.reverse()
        # 章节序号自动排序：处理「先最新后顺序」与「反爬乱序」。
        # 三种类型（novel/comic/video）共用此入口，无需源配置。
        # 仅当标题含序号数字（阿拉伯/汉字/罗马）时才改变顺序，
        # 原顺序本就正序则保持不变；无数字章节排末尾。
        if len(chapters) > 1 and any(
            _extract_chapter_number(ch.title) is not None for ch in chapters
        ):
            chapters = _sort_chapters(chapters)
        return chapters

    # ------------------------------------------------------------------ #
    def _fetch_chapters_api(
        self, source: SourceConfig, cfg: dict, detail_url: str
    ) -> Optional[List[dict]]:
        """API 目录：从详情 URL 提取 book id，直调章节 API，返回 [{title, url}]。

        供 SPA 目录站（详情页目录为 JS 渲染、SSR 空，如纵横小说）直接拿 JSON
        章节列表，避免渲染详情页。配置见 _fetch_chapters 的 chapters_api 注释。

        - 从 detail_url 提取 book id（/{id} 占位）：无法提取 → 返回 None（调用方
          回退 HTML 解析）
        - 网络/解析失败 → 抛异常（调用方 catch 后回退 HTML 解析）
        - 成功（含空列表）→ 返回 [{title, url}]，调用方直接用（不回退）
        """
        from urllib.parse import urlencode, urljoin

        m_id = _re.search(r"/(?:novel|book|comic|detail|bookinfo)/(\w+)", detail_url)
        if not m_id:
            return None
        book_id = m_id.group(1)
        api_url = str(cfg.get("url") or "").replace("{id}", book_id)
        abs_url = urljoin(source.base_url, api_url)
        headers = dict(self._headers(source))
        body = cfg.get("body") or {}
        filled = {k: str(v).replace("{id}", book_id) for k, v in body.items()}

        method = (cfg.get("method") or "GET").upper()
        if method == "POST":
            body_format = (cfg.get("body_format") or "form").lower()
            if body_format == "json":
                resp = self._http.post_json(
                    abs_url, json_body=filled, headers=headers,
                    timeout=self._timeout(source), retries=self._retries(source),
                    proxy_pool=source.proxy_pool(),
                )
            else:
                text = self._http.post_form(
                    abs_url, form_data=filled, headers=headers,
                    timeout=self._timeout(source), retries=self._retries(source),
                    proxy_pool=source.proxy_pool(),
                )
                import json as _json

                resp = _json.loads(text) if text else {}
        else:
            qs = urlencode(filled)
            abs_url = f"{abs_url}&{qs}" if "?" in abs_url else f"{abs_url}?{qs}"
            resp = self._http.get_json(
                abs_url, headers=headers,
                timeout=self._timeout(source), retries=self._retries(source),
                proxy_pool=source.proxy_pool(),
            )

        # response_path 定位列表
        node = resp
        rpath = cfg.get("response_path") or ""
        if rpath:
            node = self._jsonpath(resp, rpath)
        # children_path 扁平化卷内章节子数组；缺省直接把 response_path 结果当章节列表
        children_path = cfg.get("children_path") or ""
        raw_items = []
        if children_path and isinstance(node, list):
            for sub in node:
                if isinstance(sub, dict) and isinstance(sub.get(children_path), list):
                    raw_items.extend(sub[children_path])
        elif isinstance(node, list):
            raw_items = node

        item_fields = cfg.get("item_fields") or {}
        title_key = item_fields.get("title") or "title"
        cid_key = item_fields.get("chapter_id") or "chapter_id"
        url_tpl = str(cfg.get("url_template") or "")
        out: List[dict] = []
        for it in raw_items:
            if not isinstance(it, dict):
                continue
            c_title = str(it.get(title_key) or "").strip()
            c_url = url_tpl
            for ph, val in (("{id}", book_id), ("{chapter_id}", str(it.get(cid_key) or ""))):
                c_url = c_url.replace(ph, val)
            # 模板里其它 {占位符} 统一填充
            c_url = utils.fill_template(c_url, it)
            if c_title and c_url:
                out.append({"title": c_title, "url": c_url})
        return out

    # ------------------------------------------------------------------ #
    def fetch_chapter(self, source: SourceConfig, url: str) -> str:
        """抓取单章正文。按类型取正文选择器，支持章节分页拼接。

        长章节在部分站点会拆成多页（如 xxx.html / xxx_1.html / xxx_2.html）。
        若源配置 content.chapter.pagination 开启，则通过页脚导航「下一章」
        链接判定：URL 基路径（去 _<数字>.html 后缀）与当前页相同 → 同章续页，
        继续抓取并拼接；否则为真正的下一章，停止。
        """
        content_cfg = self._content_block(source)
        if source.content_type == "novel":
            block = content_cfg.get("chapter") or {}
        elif source.content_type == "comic":
            block = content_cfg.get("page") or {}
        else:
            block = content_cfg.get("episode") or {}
        # 章内分页（source-schema §9.2 body.paginator）：novel 默认开启自动探测，
        # 长章节跨多页（xxx.html / xxx_1.html）时拼接；seen+max 防死循环。
        pag_cfg = (block or {}).get("pagination") or {}
        pag_enabled = bool(pag_cfg.get("enabled", True))
        # 正文抓取（可能分页）
        pages = []
        cur = url
        seen = set()
        max_pages = int(pag_cfg.get("max_pages") or 20)
        while cur and len(pages) < max_pages:
            page_text, nxt = self._fetch_chapter_page(source, cur, pag_enabled)
            if page_text:
                pages.append(page_text)
            # 分页判定：URL 基路径（去 _<数字>.html）相同 → 同章续页继续抓
            if not nxt:
                break
            nxt_abs = self._abs_url(source, nxt)
            cur_base = self._chapter_base(cur)
            nxt_base = self._chapter_base(nxt_abs)
            if cur_base == nxt_base and nxt_abs not in seen:
                seen.add(nxt_abs)
                cur = nxt_abs
                continue
            break  # 基路径不同 → 是真正的下一章或重复，停止分页
        return "\n".join(pages)

    def _fetch_chapter_page(
        self, source: SourceConfig, url: str, pag_enabled: bool = True
    ) -> tuple:
        """抓取单页正文，返回 (plain_text, next_page_url 或 "")。

        自动探测分页链接（pag_enabled 时）：
        1. 源配置 content.chapter.pagination.next_selector 显式给出「下一页」选择器；
        2. 否则从页面导航（read-nav / .nav 等）找「下一页」文本的链接；
        3. 再兜底从页面找 _<数字>.html 的分页链接。
        返回的 nxt 是否同章续页由调用方 _chapter_base 判定。
        """
        self._bg_check(source, self._abs_url(source, url))
        html = self._get(source, url)
        doc = self._parser.parse(html)

        content_cfg = self._content_block(source)
        if source.content_type == "novel":
            block = content_cfg.get("chapter") or {}
        elif source.content_type == "comic":
            block = content_cfg.get("page") or {}
        else:
            block = content_cfg.get("episode") or {}
        body = block.get("body") or {}
        selector = body.get("selector")

        nxt = ""
        if pag_enabled:
            # 显式续页配置优先：body.paginator.next_link.selector（source-schema §9.2）
            body_pag = body.get("paginator") or {}
            next_sel = (body_pag.get("next_link") or {}).get("selector")
            if next_sel:
                nxt = self._parser.extract_first(doc, next_sel, self._abs_url(source, url))
            else:
                # 自动探测：导航区「下一页」链接
                nxt = self._detect_next_page(doc, source, url)
            if nxt:
                nxt = self._abs_url(source, nxt)

        # Playwright 渲染正文（SPA 小说站，如 18mh）
        if body.get("render") == "playwright":
            rc = body.get("render_config") or {}
            sel_css = (selector or {}).get("css") if isinstance(selector, dict) else selector
            from .playwright_helper import fetch_rendered_text_sync

            text = fetch_rendered_text_sync(
                self._abs_url(source, url),
                sel_css or "",
                wait_for=rc.get("wait_for") or "",
                wait_until=rc.get("wait_until") or "domcontentloaded",
                timeout_ms=int(rc.get("timeout_ms") or 30000),
                extra_delay_ms=int(rc.get("extra_delay_ms") or 2000),
                proxy=source.transports().get("proxy"),
            )
            if not text:
                raise ContentMissingError(
                    f"渲染正文为空（{url}）", source_id=source.source_id
                )
            if source.content_type == "novel":
                text = self._normalize_para_indent(text)
                if self._decrypter is not None:
                    text = self._decrypter.decrypt(source, text, "chapter")
            return text, nxt

        if not selector:
            raise ContentMissingError("源未配置正文选择器", source_id=source.source_id)

        paragraphs = self._parser.extract(doc, selector)
        if not paragraphs:
            # 正文选择器未命中：可能是混淆/加密正文，尝试解密
            text = self._decrypt_chapter(source, html, url)
        else:
            text = "\n".join(paragraphs)
        # 小说正文段落开头空格规范化（18mh 等站原文多个空格缩进）
        if source.content_type == "novel":
            text = self._normalize_para_indent(text)
            # 字符映射解密（番茄小说字体混淆等）：decryption.targets.chapter.strategy=translit
            if self._decrypter is not None:
                text = self._decrypter.decrypt(source, text, "chapter")
        # 正文广告行过滤（ad_block 引擎：剔除「广告/推广/点击领取」等特征行）
        try:
            from .adblock import adblock_for

            ad = adblock_for(source)
            if ad.enabled:
                text = ad.filter_text(text)
        except Exception:
            pass  # 广告过滤是增强能力，失败不影响正文
        return text, nxt

    @staticmethod
    def _normalize_para_indent(text: str) -> str:
        """段落开头空格规范化：前导空白（半角/全角）>2 的行统一为 2 个全角缩进。

        18mh 等站正文段落原文用多个半角空格缩进（实测 29 个），中文排版
        标准是「　　」两格全角。只压缩**异常多**的前导空白，正常缩进/无
        缩进/标题行保持原样，避免误伤。
        """
        out = []
        for ln in (text or "").split("\n"):
            stripped = ln.lstrip(" \t　")
            lead = len(ln) - len(stripped)
            if lead > 2 and stripped:
                out.append("　　" + stripped)
            else:
                out.append(ln)
        return "\n".join(out)

    def _detect_next_page(self, doc, source: SourceConfig, url: str) -> str:
        """自动探测章节分页的「下一页」链接。

        依次尝试：
        1. 文本为「下一页/下页/尾页」的 <a> 链接；
        2. 页脚导航（read-nav / pagenav 等）内与当前页**同基路径**的链接
           （即 _<数字>.html 分页续页）；
        3. 页面任意同基路径的 _<数字>.html 链接。
        返回原始 href（可能相对），无则空串。
        """
        base = self._chapter_base(self._abs_url(source, url))
        url_abs = self._abs_url(source, url)

        # 页脚导航内同基路径链接（read-nav / pagenav 等）。
        # 导航按「上一章 | 目录 | 下一章」排列；同章续页链接带 _<数字>.html
        # 后缀且基路径相同，取导航中**最后一个**这样的链接（"下一页"通常在末尾）。
        # 自指链接（href 指向当前页）排除。
        for cls in ("read-nav", "chapter-page-nav", "pagenav", "bottem1"):
            for nav in doc.xpath(f'//*[contains(concat(" ", normalize-space(@class), " "), " {cls} ")]'):
                match = ""
                for a in nav.xpath('.//a[@href]'):
                    href = a.get("href") or ""
                    if not href:
                        continue
                    href_abs = self._abs_url(source, href)
                    if (self._chapter_base(href_abs) == base
                            and href_abs != url_abs
                            and "_" in href):  # 同基路径 + 带 _ 后缀 → 续页
                        match = href
                if match:
                    return match
        # 兜底：页面任意同基路径的 _<数字>.html 链接
        for a in doc.xpath('//a[@href]'):
            href = a.get("href") or ""
            if (href and self._chapter_base(self._abs_url(source, href)) == base
                    and "_" in href):
                return href
        return ""

    @staticmethod
    def _chapter_base(url: str) -> str:
        """去 URL 的 _<数字> 分页后缀，返回基路径（兼容 .html/.htm/.shtml/.php/无后缀）。

        https://x/1/73976498_1.html → https://x/1/73976498.html
        https://x/1/73976498_2.htm  → https://x/1/73976498.htm
        https://x/1/73976498.html   → https://x/1/73976498.html
        """
        import re as _re2
        return _re2.sub(r"_(\d+)(?=\.\w+$|$)", "", url)

    def _decrypt_chapter(self, source: SourceConfig, html: str, url: str) -> str:
        """正文被混淆时的解密路径（源配置 decryption 驱动）。"""
        dec_cfg = source.raw.get("decryption") or {}
        if not dec_cfg:
            raise ContentMissingError(
                f"未解析到正文内容（{url}）", source_id=source.source_id
            )
        if self._decrypter is None:
            raise ContentMissingError(
                f"正文需解密但未配置解密器（{url}）", source_id=source.source_id
            )
        content_cfg = dec_cfg.get("targets", {}).get("content") or {}
        source_type = content_cfg.get("source", "obfuscated_script")
        try:
            if source_type == "obfuscated_script":
                # 从混淆脚本提取 base64 并解码（extract_base64_blocks 已解码）
                text = self._decrypter.extract_base64_blocks(html)
                if not text:
                    raise ContentMissingError(
                        f"混淆正文提取为空（{url}）", source_id=source.source_id
                    )
                # 解码后可能含 <p>...</p> 等 HTML 标签（混淆编码原样保留了标签结构），
                # 需要去标签转纯文本；无标签则直接返回
                if _re.search(r"<[a-z]", text, _re.IGNORECASE):
                    text = _strip_html_tags(text)
                return text
            # 其他 source 类型：把原始内容交给 decrypt（按 strategy 处理）
            return self._decrypter.decrypt(source, html, "content")
        except ContentMissingError:
            raise
        except Exception as exc:
            raise ContentMissingError(
                f"正文解密失败（{url}）：{exc}", source_id=source.source_id
            )

    # ------------------------------------------------------------------ #
    def fetch_comic_pages(self, source: SourceConfig, chapter_url: str, on_page=None) -> List[str]:
        """漫画：抓取一话的全部分页图片 URL。

        对应 endpoints.content.page：
        - render: playwright → 用 Playwright 渲染（分片加密站）
        - 普通源 → HTML 提取图片 URL

        on_page：可选分批回调 on_page(已就绪的前缀列表)。解密型源在
        _decrypt_image_urls 解密过程中分批回调（连续前缀），GUI 边收边渲染。
        """
        content_cfg = self._content_block(source)
        block = content_cfg.get("page") or {}
        abs_url = self._abs_url(source, chapter_url)

        # 渲染策略：render 可能配置在 page.body（comicbox 结构）
        body_cfg = block.get("body") or {}
        render_mode = block.get("render") or body_cfg.get("render")

        # Playwright 渲染策略（分片加密图片）
        if render_mode == "playwright":
            # 具体用法由源配置 render_config 提供（框架只执行指令，不写死）：
            #   wait_for          渲染目标选择器（默认 canvas）
            #   wait_until        页面就绪策略（domcontentloaded / networkidle）
            #   extra_delay_ms    渲染后额外等待
            #   timeout_ms        页面加载超时
            #   click_selector    可选：渲染后点击该元素（展开弹层）
            #   scroll_to_bottom  可选：滚动到页底触发懒加载
            #   extract_mode      提取方式：canvas(默认) / img / text
            #   proxy             是否走代理（默认自动探测系统代理）
            rc = block.get("render_config") or body_cfg.get("render_config") or {}
            try:
                from .playwright_helper import fetch_rendered_images_sync

                imgs = fetch_rendered_images_sync(
                    abs_url,
                    wait_for=rc.get("wait_for", "canvas"),
                    wait_until=rc.get("wait_until", "domcontentloaded"),
                    timeout_ms=int(rc.get("timeout_ms") or 30000),
                    extra_delay_ms=int(rc.get("extra_delay_ms") or 2500),
                    click_selector=rc.get("click_selector"),
                    scroll_to_bottom=rc.get("scroll_to_bottom", False),
                    extract_mode=rc.get("extract_mode", "canvas"),
                    proxy=source.transports().get("proxy"),
                    page_container_selector=rc.get("page_container_selector"),
                    scroll_step_px=int(rc.get("scroll_step_px", 600)),
                    scroll_stale_rounds=int(rc.get("scroll_stale_rounds", 6)),
                    wheel_scroll=bool(rc.get("wheel_scroll", False)),
                    img_selector=rc.get("img_selector"),
                    img_js_path=rc.get("img_js_path"),
                    # 边滚边分批回调：连续前缀提前给 GUI 渲染（首图秒出，后续边滚边补）
                    on_batch=(lambda prefix: on_page(list(prefix))) if on_page else None,
                )
                if on_page and imgs:
                    on_page(list(imgs))
                return imgs
            except Exception as exc:
                # Playwright 渲染失败 → 降级到普通 HTML 提取（站点改版/选择器不匹配时
                # 不整话失败，尝试 HTML 兜底；若 HTML 也提取不到，下方会抛 ContentMissingError）
                log.warning("[%s] Playwright 渲染失败，降级 HTML 提取：%s", source.source_id, exc)

        self._bg_check(source, abs_url)
        # 图片列表优先 body，兼容旧 list
        list_cfg = body_cfg or block.get("list") or {}
        urls = self._fetch_comic_page_imgs(source, list_cfg, chapter_url)
        # 图片解密源（如 18mh AES-CBC 加密图）：下载并把每张解密成 data URI，
        # 使阅读器/下载器无需改动即可显示/保存解密图。
        if urls and source.raw.get("decryption", {}).get("targets", {}).get("image"):
            return self._decrypt_image_urls(source, urls, on_page)
        if on_page and urls:
            on_page(list(urls))
        return urls

    def _decrypt_image_urls(self, source: SourceConfig, urls: List[str], on_page=None) -> List[str]:
        """把加密图片 URL 并发下载并 AES 解密成 data URI（供阅读器/下载器直接用）。

        一话常几十张图，串行下载+解密要 15-30s 卡半天；改为 8 并发，
        并发把耗时压到 2-4s。单张失败 → 保留原 URL（可能封面等非加密图混入）。

        on_page：可选分批回调 on_page(已解密的前缀列表)。每次「从 0 起的连续
        前缀」增长时回调一次（保持顺序），GUI 可边解密边渲染，不等整话。

        注意：HttpClient 非线程安全，每 worker 各建独立实例（复用默认值）。
        """
        if len(urls) <= 1:
            # 单张直接走原逻辑（无并发开销）
            result = [self._decrypt_one_image(source, u) for u in urls]
            if on_page and result:
                on_page(list(result))
            return result

        from concurrent.futures import ThreadPoolExecutor, as_completed

        result: List = [None] * len(urls)
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(self._decrypt_one_image, source, u): i for i, u in enumerate(urls)}
            last_emit = 0
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    result[i] = fut.result()
                except Exception:  # noqa: BLE001
                    result[i] = urls[i]
                # 连续前缀完成 → 分批回调（边解密边显示，保持顺序）
                done = 0
                while done < len(result) and result[done] is not None:
                    done += 1
                if on_page and done > last_emit:
                    on_page(list(result[:done]))
                    last_emit = done
        return list(result)

    def _decrypt_one_image(self, source: SourceConfig, u: str) -> str:
        """下载单张并 AES 解密 → data URI；失败保留原 URL。

        并发下载用 urllib 独立连接（复用 HttpClient 的 Session 线程不安全），
        headers 带 Referer/UA 防图床防盗链。
        """
        try:
            import urllib.request

            abs_url = self._abs_url(source, u)
            req = urllib.request.Request(abs_url, headers=self._headers(source))
            timeout = self._timeout(source)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            plain = self._decrypter.decrypt_bytes(source, raw, target="image")
            if plain and plain is not raw:
                import base64 as _b64

                mime = self._guess_image_mime(plain)
                return f"data:{mime};base64,{_b64.b64encode(plain).decode()}"
            return u
        except Exception:  # noqa: BLE001
            return u  # 解密失败保底原 URL

    @staticmethod
    def _guess_image_mime(data: bytes) -> str:
        """按魔数猜图片 mime（JPEG/PNG/GIF/WEBP/BMP），默认 image/jpeg。"""
        return utils.guess_image_mime(data)

    def _fetch_comic_page_imgs(
        self, source: SourceConfig, list_cfg: dict, chapter_url: str
    ) -> List[str]:
        """从单话 HTML 提取全部图片 URL，支持图片列表翻页（含并行翻页加速）。

        对应 endpoints.content.page：
        - list.root_selector / list.fields.url   单图项与图片 URL 提取
        - list.paginator.next_link.selector     （可选）「下一页」链接，跨页拼全
          多页图片站（如每页固定 N 张、30P 需翻 3 页）。循环抓取直到：
            下一页链接缺失 / 已访问过（URL 去重防死循环）/ 达 max_pages 上限。
        - list.paginator.parallel               （可选）并行翻页：站点分页 URL
          可预测（如 {base}-2.html、{base}-3.html…）时按 wave 并发抓取，把每页
          interval_ms 礼貌延迟从顺序累积压成每 wave 并行平摊。模板验证失败 /
          并行异常 → 回退下方顺序循环（保证正确性）。
        - 图片 URL 用 data-src / data-original 懒加载属性时框架自动兜底
        - image_api（可选）：图片 URL 来自 AJAX 文本接口 + JS Packer 解包的源
          （如 dm5 chapterfun.ashx）。配置见 _fetch_comic_image_api。命中即
          走该路径，不解析页面 HTML 图片。
        """
        image_api = list_cfg.get("image_api") or {}
        if image_api:
            return self._fetch_comic_image_api(source, image_api, chapter_url)
        root_sel = list_cfg.get("root_selector")
        fields = list_cfg.get("fields") or {}
        if not root_sel or not fields.get("url"):
            raise ContentMissingError(
                "源未配置漫画图片规则（content.page）",
                source_id=source.source_id,
            )
        paginator = list_cfg.get("paginator") or {}
        max_pages = int(paginator.get("max_pages") or 0)
        next_sel = (paginator.get("next_link") or {}).get("selector")

        url_sel = fields.get("url")
        urls: List[str] = []
        seen_url: set = set()   # 已访问的页面 URL（防死循环）
        seen_img: set = set()   # 已收集的图片 URL（跨页去重）

        # 共用小函数：抓单页 → (本页图片列表, 下一页链接)。顺序与并行翻页都复用。
        def _fetch_page(page_url: str, http=None) -> tuple:
            html = self._get(source, page_url, http=http)
            doc = self._parser.parse(html)
            # 在 root_selector 限定范围内提取图片 URL（复用 parse_items：
            # 每个 root 项内按 fields.url 取属性，自动 data-src 懒加载兜底）。
            # 无 root_selector 时回退整页提取。
            items = self._parser.parse_items(
                doc, root_sel, fields, source.base_url
            )
            page_imgs = [it.get("url") or "" for it in items]
            page_imgs = [u for u in page_imgs if u]
            nxt = ""
            if next_sel:
                nxt = self._parser.extract_first(doc, next_sel, source.base_url) or ""
            return page_imgs, nxt

        def _add_imgs(page_imgs: List[str]) -> None:
            """广告过滤 + 去重后并入最终列表（顺序/并行共用）。"""
            for u in self._filter_ad_images(page_imgs, source):
                if u not in seen_img:
                    seen_img.add(u)
                    urls.append(u)

        # 并行翻页（config-driven）：URL 可预测时按 wave 并发抓取，提速明显
        # （acgxmh 实测顺序 55 页 ≈71s → 并行 ~20s，window 过大触发站点 429
        # 限流丢页，window=6 为完整且最快档）。
        parallel = paginator.get("parallel") or {}
        if parallel and next_sel:
            try:
                # base：章节 URL 去 query 再 rsplit('.',1)[0] 去扩展名
                base = chapter_url.split("?", 1)[0].rsplit(".", 1)[0]
                tpl = str(parallel.get("url_template") or "")
                first_page = int(parallel.get("first_page") or 2)
                window = int(parallel.get("window") or 8)
                p_max = int(parallel.get("max_pages") or 100)
                if not base or not tpl or first_page < 2 or window < 1:
                    raise ValueError("parallel 配置无效")
                from concurrent.futures import ThreadPoolExecutor, as_completed

                def pred(n: int) -> str:
                    """第 n 页 URL（第 1 页即章节 URL 本身，从 first_page 起预测）。"""
                    return self._abs_url(source, tpl.format(base=base, page=n))

                # 第 1 页：抓章节 URL，收集图片 + 取 next 链接 nxt1
                p1_imgs, nxt1 = _fetch_page(chapter_url)
                # 模板验证（安全网）：nxt1 必须等于 pred(2)，否则模板错 → 回退顺序
                if not nxt1 or self._abs_url(source, nxt1) != pred(2):
                    raise ValueError("parallel URL 模板与站点分页不符")
                _add_imgs(p1_imgs)

                def _fetch_page_retry(p: int) -> tuple:
                    """抓第 p 页；独立 HttpClient（requests.Session 非线程安全，
                    每页一个实例避免并发竞态），失败/图片为空重试 1 次，仍失败记空
                    （保守：当页跳过，交给 wave 末页 next 判定是否继续）。"""
                    page_http = self._http.__class__(
                        sleeper=getattr(self._http, "_sleeper", None),
                        defaults=self._http.defaults,
                    )
                    try:
                        last = ([], "")
                        for attempt in range(2):
                            try:
                                imgs, nxt = _fetch_page(pred(p), http=page_http)
                            except Exception:  # noqa: BLE001
                                last = ([], "")
                                continue
                            last = (imgs, nxt)
                            if imgs:
                                break  # 拿到图即成功
                            # 图片为空：可能是瞬时失败（并发下偶发丢响应），再试 1 次
                        return last
                    finally:
                        try:
                            page_http.close()
                        except Exception:  # noqa: BLE001
                            pass

                n = first_page
                while n <= p_max:
                    wave = list(range(n, min(n + window, p_max + 1)))
                    # 每 wave 并发抓取（max_workers=min(window, 页数)）
                    results: dict = {}
                    with ThreadPoolExecutor(
                        max_workers=min(window, len(wave))
                    ) as pool:
                        futs = {pool.submit(_fetch_page_retry, p): p for p in wave}
                        for fut in as_completed(futs):
                            results[futs[fut]] = fut.result()
                    # 图片按页码顺序收集，与第 1 页拼接
                    for p in wave:
                        _add_imgs(results.get(p, ([], ""))[0])
                    # 停止条件：wave 最后一页的 next 自环（== 自身）/为空/
                    # ≠ pred(最后一页+1) → 章节结束停止；否则继续下一 wave。
                    last_p = wave[-1]
                    last_nxt = results.get(last_p, ([], ""))[1]
                    if (
                        not last_nxt
                        or self._abs_url(source, last_nxt) == pred(last_p)
                        or self._abs_url(source, last_nxt) != pred(last_p + 1)
                    ):
                        break
                    n = last_p + 1
                return urls
            except Exception as exc:  # noqa: BLE001
                # 模板验证失败 / 并行异常 → 清空并行阶段结果，回退顺序循环（保证正确性）
                log.warning("[%s] 并行翻页回退顺序：%s", source.source_id, exc)
                urls = []
                seen_url = set()
                seen_img = set()

        # 顺序循环（fallback，未配置 parallel / 并行回退时走这里）
        page_url = chapter_url
        for _ in range(max_pages if max_pages else 1000):
            if page_url in seen_url:
                break
            seen_url.add(page_url)
            page_imgs, nxt = _fetch_page(page_url)
            _add_imgs(page_imgs)
            # 无下一页配置 → 单页即止
            if not next_sel or not nxt:
                break
            page_url = nxt

        return urls

    # ------------------------------------------------------------------ #
    def _fetch_comic_image_api(
        self, source: SourceConfig, cfg: dict, chapter_url: str
    ) -> List[str]:
        """漫画图片来自 AJAX 文本接口的源（配置驱动，如 dm5 chapterfun.ashx）。

        站点阅读页的图片 URL 不在 HTML 里，而是前端 JS 按页调用文本接口拿
        （接口返回 JS Packer 混淆脚本，内含图片地址）。完整流程（全部规则
        外置到源配置，本方法只做通用执行）：

        1. 抓阅读页（chapter_url）HTML，按 cfg.vars 的 {名: 正则} 提取 JS 变量
           （如 dm5 的 DM5_CID / DM5_MID / DM5_VIEWSIGN / DM5_VIEWSIGN_DT /
           DM5_IMAGE_COUNT），其中 count 为总页数；
        2. 对第 1..count 页：把 cfg.url 里的 {变量}/{page} 占位替换成提取值，
           逐个 GET（间隔走 transports.interval_ms）；
        3. cfg.packer=true 时对响应做 JS Packer（Dean Edwards packer）解包，
           否则原文使用；
        4. 按 cfg.pix_regex / cfg.paths_regex / cfg.key_regex 从解包文本提取
           图片 CDN 基址 / 路径数组 / 签名 key；
        5. 按 cfg.url_template（{pix} {path} {key} 及 JS 变量占位）拼出图片 URL，
           去重合并。

        配置示例（dm5）：
        ```json
        "body": {
          "image_api": {
            "url": "/chapterfun.ashx",
            "params": {
              "cid": "{cid}", "page": "{page}", "key": "",
              "language": 1, "gtk": 6,
              "_cid": "{cid}", "_mid": "{mid}", "_dt": "{sign_dt}", "_sign": "{sign}"
            },
            "vars": {
              "cid": "DM5_CID\\\\s*=\\\\s*(\\\\d+)",
              "mid": "DM5_MID\\\\s*=\\\\s*(\\\\d+)",
              "sign": "DM5_VIEWSIGN\\\\s*=\\\\s*\"([^\"]+)\"",
              "sign_dt": "DM5_VIEWSIGN_DT\\\\s*=\\\\s*\"([^\"]+)\"",
              "count": "DM5_IMAGE_COUNT\\\\s*=\\\\s*(\\\\d+)"
            },
            "packer": true,
            "pix_regex": "pix=\\\"([^\\\"]+)\\\"",
            "paths_regex": "pvalue=\\\\[([^\\\\]]*)\\\\]",
            "key_regex": "key='([^']+)'",
            "url_template": "{pix}{path}?cid={cid}&key={key}",
            "max_pages": 500
          }
        }
        ```

        注意：接口 query 参数用 `params` 对象声明（自动 urlencode，空格→`+`）比
        手拼 URL 更稳——手拼 URL 里含空格的变量（如 `_dt` 时间戳）经 requests
        会变 `%20`，dm5 这类对签名串敏感的接口会据此生成失效 key（图片 404）。
        """
        import time as _time
        from urllib.parse import urlencode

        # 1) 阅读页 JS 变量
        html = self._get(source, chapter_url)
        v: dict = {}
        for name, pattern in (cfg.get("vars") or {}).items():
            m = _re.search(str(pattern), html)
            if m:
                v[name] = m.group(1) if m.groups() else m.group(0)
        try:
            count = int(v.get("count") or cfg.get("count") or 1)
        except Exception:  # noqa: BLE001
            count = 1
        count = min(count, int(cfg.get("max_pages") or 500))
        if count <= 0:
            return []

        params_cfg = cfg.get("params") or {}
        api_url = str(cfg.get("url") or "")
        url_tpl = str(cfg.get("url_template") or "")
        pix_re = str(cfg.get("pix_regex") or r'pix="([^"]+)"')
        paths_re = str(cfg.get("paths_regex") or r"pvalue=\[([^\]]*)\]")
        key_re = str(cfg.get("key_regex") or "")
        use_packer = bool(cfg.get("packer"))
        # 接口专用请求头（合并覆盖源头）——部分站点接口要求特定 Referer/Cookie
        api_headers = dict(self._headers(source))
        for k, val in (cfg.get("headers") or {}).items():
            api_headers[k] = val
        interval = (
            float(source.transports().get("interval_ms") or self._http.defaults.interval_ms)
            / 1000.0
        )

        def _fill(tpl: str, page: int) -> str:
            for name, val in v.items():
                tpl = tpl.replace("{" + name + "}", str(val))
            return tpl.replace("{page}", str(page))

        def _api_url(page: int) -> str:
            if params_cfg:
                filled = {}
                for k, val in params_cfg.items():
                    filled[k] = _fill(str(val), page)
                sep = "&" if "?" in api_url else "?"
                return api_url + sep + urlencode(filled)
            return _fill(api_url, page)

        def _fetch_api(page: int) -> str:
            """接口请求：走独立 headers，遇失败抛异常由调用方跳过该页。"""
            abs_url = self._abs_url(source, _api_url(page))
            return self._http.get_text(
                abs_url,
                headers=api_headers,
                timeout=self._timeout(source),
                retries=self._retries(source),
                interval_ms=self._interval_ms(source),
                encoding=source.transports().get("charset"),
                proxy_pool=source.proxy_pool(),
            )

        urls: List[str] = []
        seen: set = set()
        for page in range(1, count + 1):
            try:
                resp_text = _fetch_api(page)
            except Exception:  # noqa: BLE001
                continue  # 单页接口失败跳过（不整话失败）
            plain = self._unpack_js_packer(resp_text) if use_packer else resp_text
            m = _re.search(pix_re, plain)
            pix = m.group(1) if m else ""
            mp = _re.search(paths_re, plain)
            paths = _re.findall(r'"([^"]+)"', mp.group(1)) if mp else []
            key = ""
            if key_re:
                mk = _re.search(key_re, plain)
                if mk:
                    key = (mk.group(1) if mk.groups() else mk.group(0)).rstrip("\\")
            for p in paths:
                full = url_tpl.replace("{pix}", pix).replace("{path}", p)
                full = _fill(full, page).replace("{key}", key)
                if full and full not in seen:
                    seen.add(full)
                    urls.append(full)
            if interval > 0 and page < count:
                _time.sleep(interval)
        return urls

    @staticmethod
    def _unpack_js_packer(code: str) -> str:
        """解 JS Packer（Dean Edwards packer）混淆脚本，返回解包后的 JS 文本。

        形态：eval(function(p,a,c,k,e,d){...}('密文',N,N,'词表'.split('|'),0,{}))
        解不开（非 packer / 已变形）→ 原样返回。解包后把 JS 字符串转义
        （\\' → '、\\" → "）还原，方便下游正则直接提取值。
        """
        import string as _string

        m = _re.search(
            r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\('(.*?)',(\d+),(\d+),'(.*?)'\.split\('\|'\),0,\{\}\)\)",
            code,
            _re.S,
        )
        if not m:
            return code
        s, a, c, k = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split("|")

        def _b36(n: int) -> str:
            return str(n) if n < 10 else _string.ascii_lowercase[n - 10]

        def _enc(n: int, base: int) -> str:
            if n < base:
                x = n % base
                return chr(x + 29) if x > 35 else _b36(x)
            return _enc(n // base, base) + _enc(n % base, base)

        out = s
        for i in range(c - 1, -1, -1):
            if i < len(k) and k[i]:
                out = _re.sub(
                    r"\b" + _enc(i, a) + r"\b",
                    k[i].replace("\\", "\\\\").replace("$", "\\$"),
                    out,
                )
        return out.replace("\\'", "'").replace('\\"', '"')

    def comic_page_urls(self, source: SourceConfig, chapter_url: str) -> List[str]:
        """纯 HTTP 提取一话的图片 URL 列表（不渲染，供预加载计数）。

        与 fetch_comic_pages 的 HTML 分支同逻辑，但始终走普通解析、
        不走 Playwright。返回原始图片 URL 列表（可能为混淆地址，
        仅用于统计页数与占位，正文显示仍走 fetch_comic_pages 渲染）。
        """
        content_cfg = self._content_block(source)
        block = content_cfg.get("page") or {}
        body_cfg = block.get("body") or {}
        abs_url = self._abs_url(source, chapter_url)
        self._bg_check(source, abs_url)
        list_cfg = body_cfg or block.get("list") or {}
        root_sel = list_cfg.get("root_selector")
        fields = list_cfg.get("fields") or {}
        if not root_sel or not fields.get("url"):
            return []
        return self._fetch_comic_page_imgs(source, list_cfg, chapter_url)

    def fetch_comic_pages_batch(
        self,
        source: SourceConfig,
        chapter_urls: List[str],
        render_cfg: Optional[dict] = None,
    ) -> dict:
        """批量抓取多话漫画图片（**复用同一个 Chromium 实例**）。

        仅用于 render: playwright 的加密分片源（如 comicbox）。
        把 N 话的 Playwright 渲染收敛到 1 次浏览器启动，大幅降低下载耗时。

        返回 {chapter_url: [base64 data URI 列表]}；单话渲染失败 → 值为 None。
        render_cfg 可显式传入；缺省时从源配置 content.page 解析。
        """
        if not chapter_urls:
            return {}
        content_cfg = self._content_block(source)
        block = content_cfg.get("page") or {}
        body_cfg = block.get("body") or {}
        abs_urls = [self._abs_url(source, u) for u in chapter_urls]
        if render_cfg is None:
            rc = block.get("render_config") or body_cfg.get("render_config") or {}
            render_cfg = {
                "wait_for": rc.get("wait_for", "canvas"),
                "wait_until": rc.get("wait_until", "domcontentloaded"),
                "timeout_ms": rc.get("timeout_ms", 30000),
                "extra_delay_ms": rc.get("extra_delay_ms", 2500),
                "scroll_to_bottom": rc.get("scroll_to_bottom", False),
                "extract_mode": rc.get("extract_mode", "canvas"),
                "proxy": source.transports().get("proxy"),
                "wheel_scroll": bool(rc.get("wheel_scroll", False)),
                "img_selector": rc.get("img_selector"),
                "img_js_path": rc.get("img_js_path"),
            }
        from .playwright_helper import fetch_rendered_pages_batch_sync

        raw = fetch_rendered_pages_batch_sync(abs_urls, render_cfg)
        # 以原始 chapter_url 为 key 返回（调用方用 ch.url 直接查）
        return {u: raw.get(a) for u, a in zip(chapter_urls, abs_urls)}

    def fetch_video_streams(self, source: SourceConfig, episode_url: str, quality: str = "",
                            merged: bool = False) -> tuple:
        """视频：抓取单集 dash 音视频双流（播放/下载用）。

        返回 (video_url, audio_url)；非 dash/无音频时 audio_url 为 ""。
        quality: 画质名（"best"/"1080p"/...），空=源配置默认。经
        api_endpoints.episode 的 quality 映射为请求参数（如 B 站 qn）。
        merged: True 时 yt-dlp 引擎强制合并单流（含音视频的 `best`）——
        VLC 内嵌播放器喂双流 input-slave 对 DASH/fMP4 支持不可靠（黑屏），
        播放端应传 merged=True（单流稳定出画面+声音）；下载端留 False 走
        高清双流再 ffmpeg 合并。
        播放器（VLC）播放 dash 需要同时喂视频轨+音频轨（B 站音视频分离）。

        返回前经 adblock 广告过滤：URL 命中广告特征 → 置空（下载/播放均跳过）。
        """
        from .adblock import adblock_for

        ad = adblock_for(source)
        api = source.raw.get("api_endpoints") or {}
        episode_api = api.get("episode") or {}
        video, audio = "", ""
        if episode_api:
            # yt-dlp 引擎（YouTube 高清双流，需签名）
            if episode_api.get("engine") == "ytdlp":
                video, audio = self._fetch_streams_ytdlp(
                    source, episode_url, episode_api, quality, merged=merged
                )
            else:
                streams = self._fetch_episode_api(
                    source, episode_url, episode_api, want_streams=True, quality=quality
                )
                if isinstance(streams, dict):
                    video, audio = streams.get("video", ""), streams.get("audio", "")
                elif streams:
                    video, audio = streams, ""
        else:
            # HTML 兜底：单 URL
            video, audio = self.fetch_video_episode(source, episode_url), ""
        # 广告过滤（URL 命中 → 置空）
        if ad.enabled:
            if video and ad.is_ad_url(video):
                video = ""
            if audio and ad.is_ad_url(audio):
                audio = ""
        return video, audio

    def fetch_video_episode(self, source: SourceConfig, episode_url: str) -> str:
        """视频：抓取单集播放地址（解密后返回真实地址）。

        优先 api_endpoints.episode（JSON API，可选 sign 签名）；
        否则 endpoints.content.episode HTML 解析（play_url.selector）。
        换源站（content.episode.source_switch）：默认线路取流失败
        （空 / ContentMissingError / 网络异常 / 播放 URL 403/404/超时）时
        自动尝试其他线路，全部失败才抛错。
        """
        # JSON API 播放地址（api_endpoints.episode）
        api = source.raw.get("api_endpoints") or {}
        episode_api = api.get("episode") or {}
        if episode_api:
            # yt-dlp 引擎：单流播放地址（合并单流，含音视频，VLC 可直接播）
            if episode_api.get("engine") == "ytdlp":
                v, _ = self._fetch_streams_ytdlp(source, episode_url, episode_api, merged=True)
                return v
            play = self._fetch_episode_api(source, episode_url, episode_api)
            if play:
                return play

        # 换源站：默认线路失败 → 自动轮换其他线路
        if self._get_source_switch_cfg(source):
            return self._fetch_play_url_auto_switch(source, episode_url)

        return self._fetch_play_url_once(source, episode_url)

    def _fetch_play_url_once(self, source: SourceConfig, episode_url: str) -> str:
        """按单条分集 URL 提取播放地址（fetch_video_episode 的 HTML 解析部分）。

        独立成方法供线路自动轮换按 URL 复用：每条线路各抓一次播放页再提取。
        """
        abs_ep = self._abs_url(source, episode_url)
        self._bg_check(source, abs_ep)
        # single_chapter 视频源：取流 URL == 详情 URL，复用 fetch_detail 已取回的
        # HTML，免对慢站（如 missav 反爬延迟）二次整页下载（详情+取流省约一半等待）。
        # 非单章源（episode URL 与详情不同）key 不命中 → 正常请求。
        html = self._video_html_cache.get((source.source_id, abs_ep))
        if html is None:
            html = self._get(source, episode_url)
        doc = self._parser.parse(html)

        content_cfg = self._content_block(source)
        block = content_cfg.get("episode") or {}
        play_cfg = block.get("play_url") or {}
        switch_cfg = block.get("source_switch") or {}

        # Playwright 渲染播放页 → iframe 解析站 → video src（agedm 类嵌套 iframe 站）
        if play_cfg.get("render") == "playwright":
            rc = block.get("render_config") or {}
            from .playwright_helper import fetch_rendered_video_sync

            vurl = fetch_rendered_video_sync(
                self._abs_url(source, episode_url),
                wait_until=rc.get("wait_until") or "networkidle",
                timeout_ms=int(rc.get("timeout_ms") or 45000),
                extra_delay_ms=int(rc.get("extra_delay_ms") or 8000),
                proxy=source.transports().get("proxy"),
            )
            if not vurl:
                raise ContentMissingError(
                    f"渲染未获取到视频源（{episode_url}）", source_id=source.source_id
                )
            return vurl

        # 换源站：从 player_aaaa JS 配置提取真实播放地址（ps=0 直接用 / ps=1 走 parse 转码）。
        # 播放页无 player_aaaa（MacCMS 变体：var now 直链等）→ 回退下方 play_url.regex 规则
        if switch_cfg:
            try:
                return self._fetch_play_url_from_player(
                    html, switch_cfg, episode_url, source=source
                )
            except ContentMissingError:
                pass  # player_aaaa 缺失 → 走 play_url.regex

        # 正则提取（JS 里的转义 URL，如 _detail_.url m3u8）
        play_regex = play_cfg.get("regex")
        if play_regex:
            m = _re.search(play_regex, html)
            if m:
                play = m.group(1) if m.groups() else m.group(0)
                # unescape JS 转义（\/ → /）
                play = play.replace("\\/", "/")
                # 通用后缀：正则只提取到 CDN base 时补全（如 missav → /playlist.m3u8）
                suffix = play_cfg.get("suffix", "")
                if suffix:
                    play = play.rstrip("/") + "/" + suffix.lstrip("/")
                return play
            raise ContentMissingError(
                f"未匹配到播放地址（{episode_url}）", source_id=source.source_id
            )

        play_sel = play_cfg.get("selector")
        if not play_sel:
            raise ContentMissingError(
                "源未配置播放地址规则（content.episode.play_url）",
                source_id=source.source_id,
            )
        play = self._parser.extract_first(doc, play_sel, source.base_url)
        if not play:
            raise ContentMissingError(
                f"未解析到播放地址（{episode_url}）", source_id=source.source_id
            )
        # 解密（如 wbi 签名、加密地址）
        if self._decrypter is not None:
            return self._decrypter.decrypt(source, play, "video_url")
        return play

    # ------------------------------------------------------------------ #
    # 换源站线路自动轮换（source_switch）
    # ------------------------------------------------------------------ #
    def _fetch_play_url_auto_switch(
        self, source: SourceConfig, episode_url: str
    ) -> str:
        """换源站取流：默认线路失败时自动尝试其他线路，全部失败才抛错。

        失败判定：取流抛错 / 返回空 / 播放 URL 不可访问（HTTP>=400 / 空内容 /
        超时，见 _play_url_probe）。换线路复用 switch_source 重取详情拿新线路
        分集 URL（如 play-{sid}-x.html）后重新取流。
        max_switch_attempts：最多尝试线路数（含默认线路），缺省 3，避免太慢。
        """
        switch_cfg = self._get_source_switch_cfg(source)
        max_attempts = max(1, int(switch_cfg.get("max_switch_attempts") or 3))
        attempted: set = set()
        candidates: list = []
        current = episode_url
        last_err: Exception | None = None
        for _ in range(max_attempts):
            key = self._episode_key(current)
            if key in attempted:
                break
            attempted.add(key)
            try:
                play = self._fetch_play_url_once(source, current)
                if play and self._play_url_probe(source, play):
                    return play
                last_err = ContentMissingError(
                    f"播放地址不可访问（{current} → {str(play)[:80]}），"
                    f"已尝试 {len(attempted)} 条线路",
                    source_id=source.source_id,
                )
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                log.info("[%s] 线路取流失败 %s：%s", source.source_id, current, exc)
            if not candidates:
                try:
                    candidates = self._switch_candidate_episode_urls(source, current)
                except Exception as exc:  # noqa: BLE001
                    log.info("[%s] 构造候选线路失败：%s", source.source_id, exc)
                    candidates = []
            if not candidates:
                break
            current = candidates.pop(0)
        if last_err is None:
            last_err = ContentMissingError("未取到播放地址", source_id=source.source_id)
        raise ContentMissingError(
            f"全部线路取流失败（{source.source_id}）：{last_err}",
            source_id=source.source_id,
        )

    def _play_url_probe(self, source: SourceConfig, url: str) -> bool:
        """轻量探测播放 URL 是否可访问：HTTP<400 且响应非空即视为可用。

        403/404/空内容/超时 → False（触发换线路）。非 http(s) URL（如 data:）
        无法探测，直接视为可用（保持原行为，不误换线路）。
        """
        if not url or not url.startswith(("http://", "https://")):
            return True
        import urllib.request as _ur

        headers = dict(self._headers(source))
        timeout = max(5.0, min(float(self._timeout(source)) or 10.0, 10.0))
        try:
            req = _ur.Request(url, headers=headers)
            with _ur.urlopen(req, timeout=timeout) as resp:
                if resp.status >= 400:
                    return False
                return bool(resp.read(4096))
        except Exception:  # noqa: BLE001
            return False

    def _switch_candidate_episode_urls(
        self, source: SourceConfig, episode_url: str
    ) -> list:
        """构造同集其他线路的分集 URL 列表（供失败自动换线路）。

        依赖 source_switch 配置 detail_url_regex（+可选 detail_url_template）
        从分集 URL 推导详情页 URL，再走 switch_source 取各线路分集列表，
        按当前集的位置/集数号匹配出同集在新线路上的 URL。
        """
        switch = self._get_source_switch_cfg(source)
        detail_url = self._detail_url_from_episode(source, episode_url)
        if not detail_url:
            return []
        m_sid = _re.search(r"sid=(\d+)", episode_url)
        cur_sid = m_sid.group(1) if m_sid else str(switch.get("default_sid") or "1")
        try:
            base_detail, base_chs = self.switch_source(source, detail_url, cur_sid)
        except Exception as exc:  # noqa: BLE001
            log.info("[%s] 换源基线详情失败：%s", source.source_id, exc)
            return []
        # 当前集在新线路分集列表中的位置（精确 URL 匹配，回退 URL 末段集数号）
        idx = self._episode_index_in_chapters(episode_url, base_chs)
        out: list = []
        seen: set = set()
        for line in base_detail.source_list or []:
            cand_sid = str(line.get("sid") or "")
            if not cand_sid or cand_sid == cur_sid:
                continue
            try:
                _, chs = self.switch_source(source, detail_url, cand_sid)
            except Exception as exc:  # noqa: BLE001
                log.info("[%s] 换源 sid=%s 失败：%s", source.source_id, cand_sid, exc)
                continue
            if not chs:
                continue
            u = chs[idx].url if idx is not None and idx < len(chs) else chs[0].url
            if u and u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _detail_url_from_episode(self, source: SourceConfig, episode_url: str) -> str:
        """从分集 URL 推导详情页 URL（source_switch 配置驱动）。

        detail_url_regex：正则作用于分集 URL；detail_url_template：可选，
        用 {1}/{2} 引用捕获组重建详情 URL，缺省直接用捕获组 1。
        无法推导返回 ""。
        """
        switch = self._get_source_switch_cfg(source)
        regex = switch.get("detail_url_regex") or ""
        if not regex:
            return ""
        m = _re.search(regex, episode_url)
        if not m:
            return ""
        tpl = switch.get("detail_url_template")
        if tpl:
            out = tpl
            for i in range(len(m.groups())):
                out = out.replace("{%d}" % (i + 1), m.group(i + 1) or "")
            return out
        return m.group(1) or ""

    @staticmethod
    def _episode_key(url: str) -> str:
        """归一化分集 URL（去 fragment、去尾斜杠），用于去重/定位。"""
        return _re.sub(r"#.*$", "", url).rstrip("/")

    def _episode_index_in_chapters(self, episode_url: str, chapters: List[Chapter]):
        """在分集列表中定位当前集的位置：先精确 URL 匹配，回退按 URL 末段数字。"""
        target = self._episode_key(episode_url)
        for i, ch in enumerate(chapters):
            if self._episode_key(ch.url) == target:
                return i
        cur_n = self._episode_ordinal(episode_url)
        if cur_n is not None:
            for i, ch in enumerate(chapters):
                if self._episode_ordinal(ch.url) == cur_n:
                    return i
        return None

    @staticmethod
    def _episode_ordinal(url: str):
        """URL 路径末段数字（分集号/集索引），用于跨线路定位同一集。"""
        path = url.split("?", 1)[0].rstrip("/")
        m = _re.search(r"(\d+)$", path)
        return int(m.group(1)) if m else None

    def _fetch_streams_ytdlp(self, source: SourceConfig, episode_url: str, cfg: dict,
                             quality: str = "", merged: bool = False) -> tuple:
        """yt-dlp 引擎：拿高清双流（YouTube 需签名，委托 yt-dlp）。

        merged=True 时强制 `best` 合并单流（含音视频）：VLC input-slave
        对双流 DASH/fMP4 支持不可靠（黑屏），播放端走单流最稳；且少一次
        yt-dlp 子进程调用（bestvideo+bestaudio 是两次 --get-url，合并一次）。

        缓存：yt-dlp 取流 ~10s（签名+网络），同视频短时间复用，避免重复等待。
        """
        yt = self._get_ytdlp()

        key = (episode_url, quality, merged)
        cached = self._ytdlp_stream_cache.get(key)
        if cached is not None:
            return cached

        fmt = "best" if merged else self._ytdlp_format(cfg, quality)
        try:
            streams = yt.fetch_streams(episode_url, fmt=fmt)
        except Exception as exc:
            # 高清失败回退单流
            try:
                streams = (yt.fetch_streams(episode_url, fmt="best")[0], "")
            except Exception:
                raise ContentMissingError(
                    f"yt-dlp 取流失败（{episode_url}）：{exc}", source_id=source.source_id
                ) from exc
        self._ytdlp_stream_cache[key] = streams
        return streams

    def _get_ytdlp(self):
        """懒加载单例 Ytdlp（复用，避免每次子进程重复探测）。"""
        if self._ytdlp is None:
            from .ytdlp import Ytdlp

            self._ytdlp = Ytdlp()
        return self._ytdlp

    # ------------------------------------------------------------------ #
    # 换源支持（source_switch）
    # ------------------------------------------------------------------ #
    def _get_source_switch_cfg(self, source: SourceConfig) -> dict:
        """取源配置 content.episode.source_switch（换源配置），无则 {}。"""
        content_cfg = self._content_block(source)
        block = content_cfg.get("episode") or {}
        return block.get("source_switch") or {}

    def _parse_source_list(self, source: SourceConfig, html: str) -> List[dict]:
        """从详情页 HTML 提取所有可用播放源。

        source_switch 配置：
            list_selector  提取源 tab 的根选择器（如 .player_name[data-sid]）
            list_attr      源 ID 属性名（data-sid）
            name_selector  源名选择器（缺省用根节点的文本）
            playerconfig_url  可选：加载 playerconfig.js 拿各源 ps/parse
        返回 [{sid, name, from_, ps, parse}]；无配置返回 []。
        """
        switch = self._get_source_switch_cfg(source)
        if not switch:
            return []
        doc = self._parser.parse(html)
        list_sel = switch.get("list_selector")
        attr = switch.get("list_attr", "data-sid")
        name_sel = switch.get("name_selector") or list_sel
        if not list_sel:
            return []
        roots = self._parser._query(doc, list_sel, None)
        name_nodes = self._parser._query(doc, name_sel, None) if name_sel else roots
        out: List[dict] = []
        seen: set = set()
        for i, node in enumerate(roots):
            sid = node.get(attr) if hasattr(node, "get") else None
            # 无现成 sid 属性时：sid_regex 从属性值提取（如 li id="tab83" → sid=3）
            if sid and switch.get("sid_regex"):
                m_sid = _re.search(switch["sid_regex"], str(sid))
                if m_sid:
                    sid = m_sid.group(1) if m_sid.groups() else m_sid.group(0)
            # 属性无 sid 时：sid_regex 从节点 html 提取
            # （如 div.video_list_li 内含 /v/56673-2-1/ 的线路分集块）
            if not sid and switch.get("sid_regex"):
                try:
                    from lxml import etree as _etree

                    html_repr = _etree.tostring(node, encoding="unicode")
                    m_sid = _re.search(switch["sid_regex"], html_repr)
                    if m_sid:
                        sid = m_sid.group(1) if m_sid.groups() else m_sid.group(0)
                except Exception:
                    pass
            if not sid or sid in seen:
                continue
            seen.add(sid)
            name = ""
            if i < len(name_nodes) and hasattr(name_nodes[i], "text_content"):
                name = (name_nodes[i].text_content() or "").strip()
            if not name:
                name = node.text_content().strip() if hasattr(node, "text_content") else ""
            # data-from 可能在节点或其子元素上（如 span[data-from]）
            from_ = ""
            if hasattr(node, "get"):
                from_ = node.get("data-from") or ""
            if not from_ and hasattr(node, "iter"):
                for sub in node.iter():
                    if hasattr(sub, "get") and sub.get("data-from"):
                        from_ = sub.get("data-from")
                        break
            out.append({
                "sid": sid,
                "name": name,
                "from_": from_,
                "ps": "0",
                "parse": "",
            })
        # 加载 playerconfig.js 补全各源 ps/parse（可选；失败用默认 ps=0 直接用 url）
        pc_url = switch.get("playerconfig_url")
        if pc_url and out:
            try:
                pc_html = self._get(source, self._abs_url(source, pc_url))
                self._merge_parse_config(out, pc_html)
            except Exception:
                pass  # playerconfig 加载失败 → 所有源 ps=0 直接用 url
        return out

    @staticmethod
    def _merge_parse_config(source_list: List[dict], pc_html: str) -> None:
        """解析 playerconfig.js 的 player_list，把各 from 的 ps/parse 合入源列表。"""
        import json as _json

        m = _re.search(
            r'MacPlayerConfig\.player_list=(\{.*?\}),MacPlayerConfig', pc_html, _re.S
        )
        if not m:
            return
        try:
            raw = m.group(1)
            # JS 对象 → 尽量解析（字符串键无引号也能被 json 容错处理）
            raw = _re.sub(r"([{\s,])(\w+)\s*:", r'\1"\2":', raw)
            players = _json.loads(raw)
        except Exception:
            return
        for item in source_list:
            cfg = players.get(item.get("from_")) or {}
            item["ps"] = str(cfg.get("ps", "0") or "0")
            item["parse"] = (cfg.get("parse") or "").replace("\\/", "/")
            if not item["name"] and cfg.get("show"):
                item["name"] = cfg["show"]

    def _fetch_play_url_from_player(
        self, html: str, switch_cfg: dict, episode_url: str, source: SourceConfig = None
    ) -> str:
        """从 player_aaaa JS 配置提取真实播放地址。

        player_aaaa 结构：
            {url: 播放地址, from: 源类型, sid: 源ID, nid: 集数, ps: 0/1}
        ps=0：url 直接用；ps=1：url 需经 parse 接口转码（{parse}?url={url}）
        """
        regex = switch_cfg.get("play_regex", r'var player_aaaa=(\{.*?\})\s*</script>')
        url_field = switch_cfg.get("play_url_path", "url")
        m = _re.search(regex, html)
        if not m:
            raise ContentMissingError(
                f"未匹配到播放配置（{episode_url}）", source_id=(source.source_id if source else "")
            )
        try:
            obj = json.loads(m.group(1))
        except Exception as exc:
            raise ContentMissingError(
                f"播放配置解析失败（{episode_url}）：{exc}",
                source_id=(source.source_id if source else ""),
            ) from exc
        raw_url = obj.get(url_field) or ""
        if not raw_url:
            raise ContentMissingError(
                f"播放配置无 {url_field}（{episode_url}）",
                source_id=(source.source_id if source else ""),
            )
        raw_url = raw_url.replace("\\/", "/")
        # ps=1 → 走 parse 接口转码（iframe 外链源 → m3u8）
        ps = str(obj.get("ps", "0") or "0")
        parse_url = (obj.get("parse") or switch_cfg.get("default_parse") or "")
        parse_url = parse_url.replace("\\/", "/")
        if ps == "1" and parse_url:
            sep = "&" if "?" in parse_url else "?"
            return f"{parse_url}{sep}url={raw_url}"
        return raw_url

    def _fetch_chapters_for_source(
        self, source: SourceConfig, doc, detail_url: str, sid: str
    ) -> List[Chapter]:
        """按指定播放源 sid 从详情页 doc 提取分集列表。

        source_switch.ep_list_selector 含 {sid} 占位 → 替换为当前 sid。
        返回 Chapter 列表，每条带 source_id=sid。
        """
        switch = self._get_source_switch_cfg(source)
        ep_sel_tpl = switch.get("ep_list_selector")
        if not ep_sel_tpl:
            return []
        ep_sid_re = switch.get("ep_sid_regex")
        href_needle = switch.get("ep_href_contains", "/tv/")
        # 分集列表选择器：{sid} → 当前源
        ep_sel = ep_sel_tpl.replace("{sid}", str(sid))
        nodes = self._parser._query(doc, ep_sel, None)
        chapters: List[Chapter] = []
        seen: set = set()
        for node in nodes:
            href = node.get("href") if hasattr(node, "get") else None
            if not href or href_needle not in href:
                continue
            # 多线路同页：ep_sid_regex 从 href 提取线路号，非当前 sid 跳过
            if ep_sid_re:
                m_es = _re.search(ep_sid_re, href)
                link_sid = (
                    m_es.group(1)
                    if m_es and m_es.groups()
                    else (m_es.group(0) if m_es else "")
                )
                if str(link_sid) != str(sid):
                    continue
            if href in seen:
                continue
            seen.add(href)
            title = node.text_content().strip() if hasattr(node, "text_content") else ""
            # 确保 URL 带 sid（无则补）
            if f"sid={sid}" not in href:
                sep = "&" if "?" in href else "?"
                href = f"{href}{sep}sid={sid}"
            abs_url = self._abs_url(source, href)
            chapters.append(Chapter(title=title or f"第{len(chapters)+1}集", url=abs_url, source_id=str(sid)))
        # 去重标题（同标题不同 URL 的只留第一个）
        seen_t = set()
        deduped = []
        for ch in chapters:
            t = ch.title or ""
            if t in seen_t:
                continue
            seen_t.add(t)
            deduped.append(ch)
        return deduped

    def switch_source(
        self, source: SourceConfig, detail_url: str, new_sid: str
    ) -> tuple:
        """切换播放源：重新抓取该源详情页，返回 (更新后的 Detail, 新分集列表)。

        detail_url：当前详情 URL（含当前 sid）；new_sid：目标播放源 sid。
        返回的 Detail 保留元数据，chapters 换成新源分集，source_list 保留。
        """
        # 构造新 URL（换 sid 参数）
        new_url = _re.sub(r"sid=\d+", f"sid={new_sid}", detail_url)
        if f"sid={new_sid}" not in new_url:
            sep = "&" if "?" in new_url else "?"
            new_url = f"{new_url}{sep}sid={new_sid}"
        # 重新抓详情（fetch_detail 内部会按新 sid 用 _fetch_chapters 提取新分集）
        detail = self.fetch_detail(source, new_url)
        return detail, detail.chapters

    @staticmethod
    def _ytdlp_format(cfg: dict, quality: str = "") -> str:
        """quality → yt-dlp 格式串（下载端双流路径）。

        下载用双流（bestvideo+bestaudio，ffmpeg 合并出高清）；播放端
        由 _fetch_streams_ytdlp(merged=True) 强制 `best` 合并单流，不经本方法。
        """
        fmt = str(cfg.get("format") or "best")
        if not quality or quality == "best":
            return fmt
        # 精确画质：高度限制（bestvideo[height<=X]+bestaudio，ffmpeg 可合并）
        m = __import__("re").match(r"^(\d{3,4})p$", quality or "")
        if m:
            h = m.group(1)
            return f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"
        return fmt

    def _fetch_episode_api(self, source: SourceConfig, episode_url: str, cfg: dict, want_streams: bool = False, quality: str = "") -> str | dict:
        """api_endpoints.episode：JSON API 取播放地址（支持 sign 签名）。

        want_streams=True 时返回 {"video": ..., "audio": ...}（dash 音视频分离）。
        quality: 画质名（"best"/"1080p"/...），经 cfg["quality"]["map"] 映射为
        请求参数值（如 B 站 qn）；缺省用 quality.map 的默认值。
        """
        from urllib.parse import urlencode, urljoin

        api_url = str(cfg.get("url") or "")
        params = cfg.get("params") or {}
        # 画质映射：{quality} 占位 → 具体请求参数值（如 B 站 qn）
        quality_map = (cfg.get("quality") or {}).get("map") or {}
        q_param = (cfg.get("quality") or {}).get("param") or "quality"
        q_default = (cfg.get("quality") or {}).get("default") or "best"
        q_value = quality_map.get(quality, quality_map.get(q_default, quality or q_default))
        # {id} 从 episode_url 末段提取，去掉 .html/.htm 扩展名（avgood 分集 URL 带扩展名，
        # 直接拼接会成 xxx.html.html → 接口返回"视频不存在"）
        ep_id = episode_url.split("/")[-1]
        if ep_id.endswith((".html", ".htm")):
            ep_id = ep_id.rsplit(".", 1)[0]
        # 占位符同样替换进 URL 路径（如 avgood /play/ajax/{id}.html）。
        # 原实现只在 params 值里替换，URL 路径里的 {id} 会原样发出 → 接口返回"不存在"
        api_url = api_url.replace("{id}", ep_id)
        # 从 episode_url 提取一次 bvid/cid，路径与 params 值共用（避免每参数重复 re.search）
        bvid = ""
        m_bv = _re.search(r"(BV[0-9A-Za-z]+)", episode_url)
        if m_bv:
            bvid = m_bv.group(1)
            api_url = api_url.replace("{bvid}", bvid)
        cid = ""
        m_cid = _re.search(r"(?:cid|p)=(\d+)", episode_url)
        if m_cid:
            cid = m_cid.group(1)
            api_url = api_url.replace("{cid}", cid)
        api_url = api_url.replace("{" + q_param + "}", str(q_value))
        api_url = api_url.replace("{quality}", str(q_value))
        filled = {}
        # 占位符：{bvid}/{cid}/{id} 从 episode_url 提取；{quality} 从画质映射；{keyword} 不适用
        for k, v in params.items():
            val = str(v)
            val = val.replace("{id}", ep_id)
            # 从 episode_url 提取 bvid / cid（复用上面已提取的局部变量）
            if bvid:
                val = val.replace("{bvid}", bvid)
            if cid:
                val = val.replace("{cid}", cid)
            val = val.replace("{" + q_param + "}", str(q_value))
            # 兼容通用占位符 {quality}（不论 param 名是什么都替换）
            val = val.replace("{quality}", str(q_value))
            filled[k] = val
        sign_cfg = cfg.get("sign") or {}
        strategy = sign_cfg.get("strategy")
        if strategy:
            from .signers import get_signer

            signer = get_signer(strategy, self._http)
            filled = signer.sign(filled)
        qs = urlencode(filled)
        abs_url = urljoin(source.base_url, api_url)
        if "?" in api_url:
            abs_url = f"{abs_url}&{qs}"
        else:
            abs_url = f"{abs_url}?{qs}"
        resp = self._http.get_json(
            abs_url,
            headers=self._endpoint_headers(source, cfg),
            timeout=self._timeout(source),
            retries=self._retries(source),
            proxy_pool=source.proxy_pool(),
        )
        rpath = cfg.get("response_path") or ""
        node = resp
        if rpath:
            node = self._jsonpath(resp, rpath)
            if node is None:
                return ""
        # 取播放地址：优先 extractors（目标字段→JSONPath），否则字符串/第一值
        extractors = cfg.get("field_extractors") or {}
        url_path = extractors.get("play_url") or ""
        if url_path:
            play = self._jsonpath(resp if not url_path.startswith(".") else node, url_path) if url_path else node
        else:
            play = node
        # 若是列表，取首个非空元素；若是带 baseUrl 的对象，取 baseUrl
        if isinstance(play, list):
            play = next((it for it in play if it), "")
            if isinstance(play, dict):
                play = next((play[k] for k in ("baseUrl", "url") if play.get(k)), "")
        elif isinstance(play, dict):
            play = next((play[k] for k in ("baseUrl", "base_url", "url") if play.get(k)), "")
        play_url = str(play) if play else ""
        # 相对播放地址补全（如 avgood playlink 是 /remote_m3u8/...，缺绝对地址无法播）
        if play_url and not play_url.startswith(("http://", "https://", "data:")):
            play_url = urljoin(source.base_url, play_url)

        # dash 双流：额外取音频轨 URL（B 站音视频分离）
        if want_streams:
            audio_url = ""
            audio_path = extractors.get("audio_url") or ""
            if audio_path:
                au = self._jsonpath(resp, audio_path)
                if isinstance(au, list):
                    au = next((it for it in au if it), "")
                if isinstance(au, dict):
                    au = next((au[k] for k in ("baseUrl", "url") if au.get(k)), "")
                audio_url = str(au) if au else ""
                if audio_url and not audio_url.startswith(("http://", "https://", "data:")):
                    audio_url = urljoin(source.base_url, audio_url)
            return {"video": play_url, "audio": audio_url}
        return play_url

    @staticmethod
    def _filter_ad_images(images: List[str], source: SourceConfig) -> List[str]:
        """剔除广告图（URL 路径含广告标记）。

        优先走 ad_block 引擎的 is_ad_image_url（含域名黑名单/query 特征/
        源级配置），        再保留旧版路径段特征与 .gif 广告动图剔除作为兜底。
        """
        from urllib.parse import urlparse

        gif_marker = ".gif"
        # ad_block 引擎（含源级配置；无配置时用内置规则）
        try:
            from .adblock import adblock_for

            ad = adblock_for(source)
        except Exception:
            ad = None
        filtered = []
        for url in images:
            low = url.lower()
            path = urlparse(low).path
            # gif 广告动图（旧版规则，保留）
            if gif_marker in path:
                continue
            # ad_block 引擎判定（域名黑名单/路径/query/源级）
            if ad is not None and ad.enabled and ad.is_ad_image_url(url):
                continue
            # 旧版路径段特征（引擎未命中时兜底）
            if _AD_IMAGE_RE.search(path):
                continue
            filtered.append(url)
        return filtered
