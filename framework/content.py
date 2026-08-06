"""内容模块（content.py）。

消费 endpoints.detail 与 endpoints.content：
- fetch_detail(source, url)  → 详情元数据 + 章节列表
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
from dataclasses import dataclass, field
from typing import List, Optional

log = logging.getLogger(__name__)

from .config import SourceConfig
from .errors import ContentMissingError
from .http import HttpClient
from .parser import Parser
from .selfcheck import StructureChecker
from .source_manager import HEALTH_OK
from .decrypter import Decrypter  # noqa: F401  (类型提示用)

# ---- 章节标题数字解析 ------------------------------------------------- #
# 汉字数字（含大写：壹贰叁…）
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5,
    "陆": 6, "柒": 7, "捌": 8, "玖": 9,
}
_CN_UNITS = {
    "十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000,
    "万": 10000, "萬": 10000, "亿": 100000000, "億": 100000000,
}
_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def _cn_to_int(s: str) -> int | None:
    """汉字数字串转整数。'十二'→12，'三百零五'→305。非数字串→None。"""
    total = section = number = 0
    for ch in s:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit >= 10000:
                section = (section + number) * unit
                total += section
                section = number = 0
            else:
                section += (number or 1) * unit
                number = 0
        else:
            return None
    return total + section + number


def _roman_to_int(s: str) -> int | None:
    """罗马数字转整数。'XII'→12。非法→None。"""
    total = prev = 0
    for ch in reversed(s.upper()):
        v = _ROMAN.get(ch)
        if v is None:
            return None
        if v < prev:
            total -= v
        else:
            total += v
        prev = v
    return total if total > 0 else None


# 半角罗马数字匹配：前后若不是 ASCII 字母/数字即为边界。
# （汉字「第X话」里 X 前后是中文字符，算边界；LOVE/VALUE 里才是字母，不误判）
_ROMA_HALF = _re.compile(r"(?<![A-Za-z0-9])([IVXLCDM]{1,6})(?![A-Za-z0-9])")
_ROMA_FULL = _re.compile(r"[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+")


def _extract_chapter_number(title: str) -> tuple | None:
    """从章节标题提取排序键（卷号, 章号）。

    排序键设计（用户确认：卷号+章号复合排序）：
        第1卷 第3章  → (1, 3)；第2卷 第1章 → (2, 1)；第2卷 第1章 排在第1卷 第3章 之后。
    优先级：
    1. 同时有「第X卷/Vol.X」和「第Y章/话/回/集」→ (卷号, 章号)。
    2. 只有章号无卷号 → (0, 章号)（无卷排最前）。
    3. 只有卷号无章号 → (卷号, 0)。
    4. 纯数字/数字开头标题（"12"、"第十二"）→ (0, 12)。
    5. 仍提取不到 → None（排末尾）。

    支持的数字写法：阿拉伯 / 汉字（含大写）/ 罗马（全角+半角）/ 全角阿拉伯（０-９）/ 英文（Vol.1 / Ch.5）。
    """
    if not title:
        return None
    # 特殊章节词（楔子/序章/番外/卷首/尾声等）不算数字章节
    if _re.search(r"楔|序章|番外|卷首|前言|后记|尾声|番外篇", title):
        return None

    # 找出所有「第X卷」与「第X章/话/回/集/节」
    volume = None
    chapter = None
    # 卷号：中文「第X卷」或英文「Vol.X」「VOL.X」
    m_vol = _re.search(
        r"(?:第\s*([0-9０-９零〇一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟萬億ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩIVXLCDMivxlcdm]+)\s*卷|Vol\.?\s*([0-9０-９IVXLCDMivxlcdm]+))",
        title,
        _re.IGNORECASE,
    )
    if m_vol:
        token = m_vol.group(1) or m_vol.group(2)
        v = _parse_num_token(token)
        if v is not None:
            volume = v
    # 章号：中文「第X章/话/回/集/节」或英文「Ch.X」「CHAPTER X」
    # （话/話 兼容简繁；回/囘 变体；集/節）
    m_ch = _re.search(
        r"(?:第\s*([0-9０-９零〇一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟萬億ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩIVXLCDMivxlcdm]+)\s*(?:章|话|話|回|囘|集|節|节)|(?:Ch(?:apter)?\.?\s*([0-9０-９IVXLCDMivxlcdm]+)))",
        title,
        _re.IGNORECASE,
    )
    if m_ch:
        token = m_ch.group(1) or m_ch.group(2)
        v = _parse_num_token(token)
        if v is not None:
            chapter = v
    # 有卷有章
    if volume is not None and chapter is not None:
        return (volume, chapter)
    # 有卷无章（如 "第1卷 序章" → 已排除；"Vol.1" 只有卷）→ (卷, 0)
    if volume is not None:
        return (volume, 0)
    # 有章无卷 → (0, 章)
    if chapter is not None:
        return (0, chapter)

    # 退化：标题以数字开头（纯数字/汉字数字/罗马），视为无卷的章号
    m = _re.match(r"\s*([０-９\d]{1,6})\b", title)
    if m:
        return (0, _to_int_full(m.group(1)))
    m = _re.match(r"\s*[零〇一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟萬億]+", title)
    if m:
        v = _cn_to_int(m.group(0))
        if v is not None:
            return (0, v)
    m = _re.match(r"\s*[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+", title)
    if m:
        v = _roman_to_int(m.group(0))
        if v is not None and v <= 9999:
            return (0, v)
    m = _ROMA_HALF.match(title)
    if m:
        v = _roman_to_int(m.group(1))
        if v is not None and v <= 9999:
            return (0, v)
    # 退化：标题结尾带数字（如「书名 2」「名3」），常见于短篇集/番外连载，
    # 数字是章节序号（无「第X话」前缀）。仅在标题非纯数字开头时兜底提取。
    # 收紧：排除「楔/序/番外/完结/篇/卷」等特殊词，避免「完结篇2」「第0话 序章」
    # 误判为章号；数字可紧贴标题（如「名3」）或带分隔符（如「书名 2」）。
    if _re.search(r"楔|序章|番外|完结|篇|前言|后记|尾声|卷首", title):
        return None
    m = _re.match(r".+[ \t#・\-—\.]?([０-９\d]{1,5})\s*$", title)
    if m and not _re.match(r"\s*[０-９\d]", title):
        v = _to_int_full(m.group(1))
        return (0, v)
    return None


def _to_int_full(s: str) -> int:
    """全角数字（０-９）与半角混合转 int。"""
    trans = str.maketrans("０１２３４５６７８９", "0123456789")
    return int(s.translate(trans))


# 章节编号前缀：只取「第X章/话/回/集/节」或「Vol.X」「Ch.X」，不含标题文字
# （话/話 简繁；回/囘；集/節 兼容）
_CHAPTER_LABEL = _re.compile(
    r"第\s*[0-9０-９零〇一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟萬億ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩIVXLCDMivxlcdm]+\s*(?:章|话|話|回|囘|集|節|节)"
    r"|Vol\.?\s*[0-9０-９IVXLCDMivxlcdm]+"
    r"|Ch(?:apter)?\.?\s*[0-9０-９IVXLCDMivxlcdm]+",
    _re.IGNORECASE,
)


def chapter_label(title: str) -> str:
    """从章节标题提取编号前缀（如「第12话」），不含标题文字/标签。

    - 匹配「第X章/话/回/集/节」「Vol.3」「Ch.5」等编号部分并原样返回
    - 匹配不到则返回原标题（兜底，避免空标题）
    """
    if not title:
        return ""
    m = _CHAPTER_LABEL.search(title)
    if m:
        return m.group(0).strip()
    return title.strip()


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


def _parse_num_token(token: str) -> int | None:
    """解析「第X」中 X 的数字：阿拉伯/汉字/罗马/全角统一转 int。"""
    if not token:
        return None
    if _re.fullmatch(r"[０-９\d]{1,6}", token):
        return _to_int_full(token)
    if _re.fullmatch(r"[零〇一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟萬億]+", token):
        return _cn_to_int(token)
    if _re.fullmatch(r"[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+", token):
        v = _roman_to_int(token)
        return v if v is not None and v <= 9999 else None
    if _re.fullmatch(r"[IVXLCDM]{1,6}", token.upper()):
        v = _roman_to_int(token)
        return v if v is not None and v <= 9999 else None
    return None


def _sort_chapters(chapters: List["Chapter"]) -> List["Chapter"]:
    """按标题序号升序排序章节（稳定）。

    - 有数字的章节按序号排序（倒序/先最新后顺序/反爬乱序都能恢复正序）
    - 无数字章节（楔子/序章/番外/卷名）保持原相对顺序，排在有数字之后
    """
    numbered = [(n, idx, ch) for idx, ch in enumerate(chapters)
                if (n := _extract_chapter_number(ch.title)) is not None]
    unnumbered = [(idx, ch) for idx, ch in enumerate(chapters)
                  if _extract_chapter_number(ch.title) is None]
    numbered.sort(key=lambda t: (t[0], t[1]))
    return [ch for _, _, ch in numbered] + [ch for _, ch in unnumbered]


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

    def _report_health(self, source: SourceConfig, state: str, error: str = "") -> None:
        """上报源健康状态（若注入 health_reporter）。"""
        if self._health_reporter is not None:
            try:
                self._health_reporter.update_health(source.source_id, state, error)
            except Exception:  # noqa: BLE001
                pass

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
        from urllib.parse import urljoin, urlsplit

        # 协议判断（大小写不敏感 + 协议相对 //），避免 "HTTP://" 被误当相对路径
        scheme = (urlsplit(url).scheme or "").lower()
        if scheme in ("http", "https") or url.startswith("//"):
            return url
        return urljoin(source.base_url, url)

    def _get(self, source: SourceConfig, url: str) -> str:
        abs_url = self._abs_url(source, url)
        return self._http.get_text(
            abs_url,
            headers=self._headers(source),
            timeout=self._timeout(source),
            retries=self._retries(source),
            interval_ms=self._interval_ms(source),
            encoding=source.transports().get("charset"),
        )

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

        ok = self._checker.check(source, self._abs_url(source, url))
        self._report_health(source, HEALTH_OK if ok else "broken", "" if ok else "结构自检失败")
        html = self._get(source, url)
        doc = self._parser.parse(html)

        detail_cfg = source.get_detail_config()
        fields = detail_cfg.get("fields") or {}
        title = self._parser.extract_first(doc, fields.get("title"))
        # 书名修正：若 title 像是站点名（短/非书名），尝试 og:novel:book_name
        book_fields = fields.get("book_name")
        if book_fields:
            book = self._parser.extract_first(doc, book_fields)
            if book:
                title = book
        detail = Detail(
            source_id=source.source_id,
            content_type=source.content_type,
            url=url,
            title=title,
            author=self._parser.extract_first(doc, fields.get("author")),
            cover=self._parser.extract_first(
                doc, fields.get("cover"), source.base_url
            ),
            status=self._parser.extract_first(doc, fields.get("status")),
            summary=self._parser.extract_first(doc, fields.get("summary")),
        )
        # 标签（可空）
        tags = self._parser.extract(doc, fields.get("tags"))
        detail.tags = tags

        # 章节列表（按类型取 content 配置，传书名用于标题清理）
        detail.chapters = self._fetch_chapters(
            source, doc, book_title=detail.title, detail_url=url
        )

        # 播放源列表（换源站）：解析 source_switch 配置的可用源
        detail.source_list = self._parse_source_list(source, html)

        return detail

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
        from urllib.parse import urlencode, urljoin, quote

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
            headers=self._headers(source),
            timeout=self._timeout(source),
            retries=self._retries(source),
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
                for m in _re.finditer(r"\{(\w+)\}", c_url):
                    c_url = c_url.replace("{" + m.group(1) + "}", str(it.get(m.group(1), "")))
                detail.chapters.append(Chapter(title=c_title, url=c_url))
        return detail

    @staticmethod
    def _jsonpath(node, path: str):
        """极简 JSONPath：data.owner.name / data.list.0 点号路径。"""
        if not path:
            return None
        cur = node
        for part in str(path).split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, list) and part.lstrip("-").isdigit():
                idx = int(part)
                cur = cur[idx] if -len(cur) <= idx < len(cur) else None
            else:
                return None
        return cur

    # ------------------------------------------------------------------ #
    def _fetch_chapters(
        self,
        source: SourceConfig,
        doc,
        book_title: str = "",
        detail_url: str = "",
    ) -> List[Chapter]:
        """从详情页提取章节列表。按 content_type 读 content 配置。

        支持源配置：
        - list.chapter_order: "desc" → HTML 倒序，反转成正序（asc）
        - list.title_clean: true    → 从章节标题移除书名
        - content.<type>.single_chapter: true → 详情页即单章图集（无章节列表），
          直接以详情 URL 作为唯一章节（如 wnacg 每 aid 一图集）。
        """
        content_cfg = source.raw.get("endpoints", {}).get("content") or {}
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
        if not root_sel:
            return []

        # 独立目录页：content.chapter.list.chapters_url 为模板（如 /other/chapters/id/{id}.html），
        # 从详情 URL 提取 book id 并二次抓取，在该页提取完整章节列表
        chapters_url_tpl = list_cfg.get("chapters_url") or ""
        if chapters_url_tpl and detail_url:
            m_id = _re.search(r"/(?:novel|book|comic|detail)/(\w+)", detail_url)
            if m_id:
                try:
                    cat_url = chapters_url_tpl.replace("{id}", m_id.group(1))
                    cat_html = self._get(source, cat_url)
                    cat_doc = self._parser.parse(cat_html)
                    doc = cat_doc  # 用目录页 doc 提取章节
                except Exception:
                    pass  # 目录页抓取失败回退详情页

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
    def fetch_chapter(self, source: SourceConfig, url: str) -> str:
        """抓取单章正文。按类型取正文选择器，支持章节分页拼接。

        长章节在部分站点会拆成多页（如 xxx.html / xxx_1.html / xxx_2.html）。
        若源配置 content.chapter.pagination 开启，则通过页脚导航「下一章」
        链接判定：URL 基路径（去 _<数字>.html 后缀）与当前页相同 → 同章续页，
        继续抓取并拼接；否则为真正的下一章，停止。
        """
        content_cfg = source.raw.get("endpoints", {}).get("content") or {}
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
        ok = self._checker.check(source, self._abs_url(source, url))
        self._report_health(source, HEALTH_OK if ok else "broken", "" if ok else "结构自检失败")
        html = self._get(source, url)
        doc = self._parser.parse(html)

        content_cfg = source.raw.get("endpoints", {}).get("content") or {}
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
            return text, nxt

        if not selector:
            raise ContentMissingError("源未配置正文选择器", source_id=source.source_id)

        paragraphs = self._parser.extract(doc, selector)
        if not paragraphs:
            # 正文选择器未命中：可能是混淆/加密正文，尝试解密
            text = self._decrypt_chapter(source, html, url)
        else:
            text = "\n".join(paragraphs)
        return text, nxt

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
    def fetch_comic_pages(self, source: SourceConfig, chapter_url: str) -> List[str]:
        """漫画：抓取一话的全部分页图片 URL。

        对应 endpoints.content.page：
        - render: playwright → 用 Playwright 渲染（分片加密站）
        - 普通源 → HTML 提取图片 URL
        """
        content_cfg = source.raw.get("endpoints", {}).get("content") or {}
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

                return fetch_rendered_images_sync(
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
                )
            except Exception as exc:
                # Playwright 渲染失败 → 降级到普通 HTML 提取（站点改版/选择器不匹配时
                # 不整话失败，尝试 HTML 兜底；若 HTML 也提取不到，下方会抛 ContentMissingError）
                log.warning("[%s] Playwright 渲染失败，降级 HTML 提取：%s", source.source_id, exc)

        ok = self._checker.check(source, abs_url)
        self._report_health(source, HEALTH_OK if ok else "broken", "" if ok else "结构自检失败")
        # 图片列表优先 body，兼容旧 list
        list_cfg = body_cfg or block.get("list") or {}
        urls = self._fetch_comic_page_imgs(source, list_cfg, chapter_url)
        # 图片解密源（如 18mh AES-CBC 加密图）：下载并把每张解密成 data URI，
        # 使阅读器/下载器无需改动即可显示/保存解密图。
        if urls and source.raw.get("decryption", {}).get("targets", {}).get("image"):
            return self._decrypt_image_urls(source, urls)
        return urls

    def _decrypt_image_urls(self, source: SourceConfig, urls: List[str]) -> List[str]:
        """把加密图片 URL 并发下载并 AES 解密成 data URI（供阅读器/下载器直接用）。

        一话常几十张图，串行下载+解密要 15-30s 卡半天；改为 8 并发，
        并发把耗时压到 2-4s。单张失败 → 保留原 URL（可能封面等非加密图混入）。

        注意：HttpClient 非线程安全，每 worker 各建独立实例（复用默认值）。
        """
        if len(urls) <= 1:
            # 单张直接走原逻辑（无并发开销）
            return [self._decrypt_one_image(source, u) for u in urls]

        from concurrent.futures import ThreadPoolExecutor

        def _worker(u: str) -> str:
            return self._decrypt_one_image(source, u)

        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(_worker, urls))

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
        """按魔数猜图片 mime（JPEG/PNG/GIF/WEBP/BMP），默认 octet-stream。"""
        if data[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if data[:4] == b"\x89PNG":
            return "image/png"
        if data[:6] in (b"GIF89a", b"GIF87a"):
            return "image/gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        if data[:2] == b"BM":
            return "image/bmp"
        return "image/jpeg"

    def _fetch_comic_page_imgs(
        self, source: SourceConfig, list_cfg: dict, chapter_url: str
    ) -> List[str]:
        """从单话 HTML 提取全部图片 URL，支持图片列表翻页。

        对应 endpoints.content.page：
        - list.root_selector / list.fields.url   单图项与图片 URL 提取
        - list.paginator.next_link.selector     （可选）「下一页」链接，跨页拼全
          多页图片站（如每页固定 N 张、30P 需翻 3 页）。循环抓取直到：
            下一页链接缺失 / 已访问过（URL 去重防死循环）/ 达 max_pages 上限。
        - 图片 URL 用 data-src / data-original 懒加载属性时框架自动兜底
        """
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
        page_url = chapter_url

        for _ in range(max_pages if max_pages else 1000):
            if page_url in seen_url:
                break
            seen_url.add(page_url)
            html = self._get(source, page_url)
            doc = self._parser.parse(html)
            # 在 root_selector 限定范围内提取图片 URL（复用 parse_items：
            # 每个 root 项内按 fields.url 取属性，自动 data-src 懒加载兜底）。
            # 无 root_selector 时回退整页提取。
            items = self._parser.parse_items(
                doc, root_sel, fields, source.base_url
            )
            page_imgs = [it.get("url") or "" for it in items]
            page_imgs = [u for u in page_imgs if u]
            for u in self._filter_ad_images(page_imgs, source):
                if u not in seen_img:
                    seen_img.add(u)
                    urls.append(u)
            # 无下一页配置 → 单页即止
            if not next_sel:
                break
            nxt = self._parser.extract_first(doc, next_sel, source.base_url)
            if not nxt:
                break
            page_url = nxt

        return urls

    def comic_page_urls(self, source: SourceConfig, chapter_url: str) -> List[str]:
        """纯 HTTP 提取一话的图片 URL 列表（不渲染，供预加载计数）。

        与 fetch_comic_pages 的 HTML 分支同逻辑，但始终走普通解析、
        不走 Playwright。返回原始图片 URL 列表（可能为混淆地址，
        仅用于统计页数与占位，正文显示仍走 fetch_comic_pages 渲染）。
        """
        content_cfg = source.raw.get("endpoints", {}).get("content") or {}
        block = content_cfg.get("page") or {}
        body_cfg = block.get("body") or {}
        abs_url = self._abs_url(source, chapter_url)
        ok = self._checker.check(source, abs_url)
        self._report_health(source, HEALTH_OK if ok else "broken", "" if ok else "结构自检失败")
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
        content_cfg = source.raw.get("endpoints", {}).get("content") or {}
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
            }
        from .playwright_helper import fetch_rendered_pages_batch_sync

        raw = fetch_rendered_pages_batch_sync(abs_urls, render_cfg)
        # 以原始 chapter_url 为 key 返回（调用方用 ch.url 直接查）
        return {u: raw.get(a) for u, a in zip(chapter_urls, abs_urls)}

    def fetch_video_streams(self, source: SourceConfig, episode_url: str, quality: str = "") -> tuple:
        """视频：抓取单集 dash 音视频双流（播放/下载用）。

        返回 (video_url, audio_url)；非 dash/无音频时 audio_url 为 ""。
        quality: 画质名（"best"/"1080p"/...），空=源配置默认。经
        api_endpoints.episode 的 quality 映射为请求参数（如 B 站 qn）。
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
                    source, episode_url, episode_api, quality
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
        """
        # JSON API 播放地址（api_endpoints.episode）
        api = source.raw.get("api_endpoints") or {}
        episode_api = api.get("episode") or {}
        if episode_api:
            # yt-dlp 引擎：单流播放地址（拿视频轨）
            if episode_api.get("engine") == "ytdlp":
                v, _ = self._fetch_streams_ytdlp(source, episode_url, episode_api)
                return v
            play = self._fetch_episode_api(source, episode_url, episode_api)
            if play:
                return play

        ok = self._checker.check(source, self._abs_url(source, episode_url))
        self._report_health(source, HEALTH_OK if ok else "broken", "" if ok else "结构自检失败")
        html = self._get(source, episode_url)
        doc = self._parser.parse(html)

        content_cfg = source.raw.get("endpoints", {}).get("content") or {}
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

        # 换源站：从 player_aaaa JS 配置提取真实播放地址（ps=0 直接用 / ps=1 走 parse 转码）
        if switch_cfg:
            return self._fetch_play_url_from_player(
                html, switch_cfg, episode_url, source=source
            )

        # 正则提取（JS 里的转义 URL，如 _detail_.url m3u8）
        play_regex = play_cfg.get("regex")
        if play_regex:
            m = _re.search(play_regex, html)
            if m:
                play = m.group(1) if m.groups() else m.group(0)
                # unescape JS 转义（\/ → /）
                play = play.replace("\\/", "/")
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

    def _fetch_streams_ytdlp(self, source: SourceConfig, episode_url: str, cfg: dict, quality: str = "") -> tuple:
        """yt-dlp 引擎：拿高清双流（YouTube 需签名，委托 yt-dlp）。

        缓存：yt-dlp 取流 ~10s（签名+网络），同视频短时间复用，避免重复等待。
        """
        yt = self._get_ytdlp()

        key = (episode_url, quality)
        cached = self._ytdlp_stream_cache.get(key)
        if cached is not None:
            return cached

        fmt = self._ytdlp_format(cfg, quality)
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
    @staticmethod
    def _get_source_switch_cfg(source: SourceConfig) -> dict:
        """取源配置 content.episode.source_switch（换源配置），无则 {}。"""
        content_cfg = source.raw.get("endpoints", {}).get("content") or {}
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
        # 分集列表选择器：{sid} → 当前源
        ep_sel = ep_sel_tpl.replace("{sid}", str(sid))
        nodes = self._parser._query(doc, ep_sel, None)
        chapters: List[Chapter] = []
        seen: set = set()
        for node in nodes:
            href = node.get("href") if hasattr(node, "get") else None
            if not href or "/tv/" not in href:
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
        """quality → yt-dlp 格式串。统一返回合并单流（含音视频）。

        分离双流（bestvideo+bestaudio）VLC 的 input-slave 对 DASH/fMP4
        支持不可靠（YouTube googlevideo / B站 m4s → 黑屏）。改用 `best`/
        高度限制的合并流，VLC 单流直播，稳定出画面+声音。
        """
        fmt = str(cfg.get("format") or "best")
        if not quality or quality == "best":
            return fmt
        # 精确画质：高度限制的合并流（best[height<=X]，VLC 可单流播放）
        m = __import__("re").match(r"^(\d{3,4})p$", quality or "")
        if m:
            return f"best[height<={m.group(1)}]"
        return fmt

    def _fetch_episode_api(self, source: SourceConfig, episode_url: str, cfg: dict, want_streams: bool = False, quality: str = "") -> str | dict:
        """api_endpoints.episode：JSON API 取播放地址（支持 sign 签名）。

        want_streams=True 时返回 {"video": ..., "audio": ...}（dash 音视频分离）。
        quality: 画质名（"best"/"1080p"/...），经 cfg["quality"]["map"] 映射为
        请求参数值（如 B 站 qn）；缺省用 quality.map 的默认值。
        """
        import copy
        from urllib.parse import urlencode, urljoin, quote

        api_url = str(cfg.get("url") or "")
        params = cfg.get("params") or {}
        # 画质映射：{quality} 占位 → 具体请求参数值（如 B 站 qn）
        quality_map = (cfg.get("quality") or {}).get("map") or {}
        q_param = (cfg.get("quality") or {}).get("param") or "quality"
        q_default = (cfg.get("quality") or {}).get("default") or "best"
        q_value = quality_map.get(quality, quality_map.get(q_default, quality or q_default))
        filled = {}
        # 占位符：{bvid}/{cid}/{id} 从 episode_url 提取；{quality} 从画质映射；{keyword} 不适用
        for k, v in params.items():
            val = str(v)
            val = val.replace("{id}", episode_url.split("/")[-1])
            # 从 episode_url 尝试提取 bvid / cid
            m_bv = _re.search(r"(BV[0-9A-Za-z]+)", episode_url)
            if m_bv:
                val = val.replace("{bvid}", m_bv.group(1))
            m_cid = _re.search(r"(?:cid|p)=(\d+)", episode_url)
            if m_cid:
                val = val.replace("{cid}", m_cid.group(1))
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
            headers=self._headers(source),
            timeout=self._timeout(source),
            retries=self._retries(source),
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
            return {"video": play_url, "audio": audio_url}
        return play_url

    @staticmethod
    def _filter_ad_images(images: List[str], source: SourceConfig) -> List[str]:
        """剔除广告图（URL 路径含广告标记）。

        仅检查 URL 路径部分（? 前），且用路径段边界匹配，避免
        auth_key/upload_01 等正常参数被 ad_ 子串误伤。
        """
        import re as _re
        from urllib.parse import urlparse

        # 精确广告特征：路径段或明确广告词（不用宽泛的 ad_，防误杀正常 URL）
        ad_re = _re.compile(
            r"(?:/ads?/|/ad-|_ad\.|/advert|/banner|/promo|/ad\.|/ad/|\.ads\.)"
        )
        gif_marker = ".gif"
        filtered = []
        for url in images:
            low = url.lower()
            path = urlparse(low).path
            # gif 仍按路径判断（gif 常是广告动图）
            if gif_marker in path:
                continue
            if ad_re.search(path):
                continue
            filtered.append(url)
        return filtered
