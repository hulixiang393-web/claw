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

import re as _re
from dataclasses import dataclass, field
from typing import List, Optional

from .config import SourceConfig
from .errors import ContentMissingError
from .http import HttpClient
from .parser import Parser
from .selfcheck import StructureChecker
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
    m_ch = _re.search(
        r"(?:第\s*([0-9０-９零〇一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟萬億ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩIVXLCDMivxlcdm]+)\s*(?:章|话|回|集|节)|(?:Ch(?:apter)?\.?\s*([0-9０-９IVXLCDMivxlcdm]+)))",
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
    return None


def _to_int_full(s: str) -> int:
    """全角数字（０-９）与半角混合转 int。"""
    trans = str.maketrans("０１２３４５６７８９", "0123456789")
    return int(s.translate(trans))


# 章节编号前缀：只取「第X章/话/回/集/节」或「Vol.X」「Ch.X」，不含标题文字
_CHAPTER_LABEL = _re.compile(
    r"第\s*[0-9０-９零〇一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟萬億ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩIVXLCDMivxlcdm]+\s*(?:章|话|回|集|节)"
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


class Content:
    def __init__(
        self,
        http: HttpClient,
        parser: Parser,
        checker: StructureChecker,
        decrypter: Optional["Decrypter"] = None,
    ):
        self._http = http
        self._parser = parser
        self._checker = checker
        self._decrypter = decrypter

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
        abs_url = self._abs_url(source, url)
        return self._http.get_text(
            abs_url,
            headers=self._headers(source),
            timeout=self._timeout(source),
            retries=self._retries(source),
            interval_ms=self._interval_ms(source),
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
            return self._fetch_detail_api(source, url, detail_api)

        self._checker.check(source, self._abs_url(source, url))
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
        detail.chapters = self._fetch_chapters(source, doc, book_title=detail.title)
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
    def _fetch_chapters(self, source: SourceConfig, doc, book_title: str = "") -> List[Chapter]:
        """从详情页提取章节列表。按 content_type 读 content 配置。

        支持源配置：
        - list.chapter_order: "desc" → HTML 倒序，反转成正序（asc）
        - list.title_clean: true    → 从章节标题移除书名
        """
        content_cfg = source.raw.get("endpoints", {}).get("content") or {}
        if source.content_type == "novel":
            block = content_cfg.get("chapter") or {}
        elif source.content_type == "comic":
            block = content_cfg.get("page") or {}
        else:
            block = content_cfg.get("episode") or {}
        list_cfg = block.get("list") or {}
        root_sel = list_cfg.get("root_selector")
        fields = list_cfg.get("fields") or {}
        if not root_sel:
            return []

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
        self._checker.check(source, self._abs_url(source, url))
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
        """去 URL 的 _<数字>.html 分页后缀，返回基路径。

        https://x/1/73976498_1.html → https://x/1/73976498.html
        https://x/1/73976498.html   → https://x/1/73976498.html
        """
        import re as _re2
        return _re2.sub(r"_(\d+)\.html$", ".html", url)

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
                    timeout_ms=rc.get("timeout_ms", 30000),
                    extra_delay_ms=rc.get("extra_delay_ms", 2500),
                    click_selector=rc.get("click_selector"),
                    scroll_to_bottom=rc.get("scroll_to_bottom", False),
                    extract_mode=rc.get("extract_mode", "canvas"),
                    proxy=source.transports().get("proxy"),
                    page_container_selector=rc.get("page_container_selector"),
                    scroll_step_px=int(rc.get("scroll_step_px", 600)),
                    scroll_stale_rounds=int(rc.get("scroll_stale_rounds", 6)),
                )
            except Exception as exc:
                raise ContentMissingError(
                    f"Playwright 渲染图片失败（{chapter_url}）：{exc}",
                    source_id=source.source_id,
                ) from exc

        self._checker.check(source, abs_url)
        html = self._get(source, chapter_url)
        doc = self._parser.parse(html)
        # 图片列表优先 body，兼容旧 list
        list_cfg = body_cfg or block.get("list") or {}
        root_sel = list_cfg.get("root_selector")
        fields = list_cfg.get("fields") or {}
        if not root_sel or not fields.get("url"):
            raise ContentMissingError(
                "源未配置漫画图片规则（content.page）",
                source_id=source.source_id,
            )

        # 图片字段可能用 data-src / data-original 懒加载属性
        url_sel = fields.get("url")
        if url_sel and isinstance(url_sel, dict):
            attr = url_sel.get("attr")
            if attr in (None, "src"):
                # 尝试 data-src / data-original 兜底
                for lazy_attr in ("data-src", "data-original", "src"):
                    sel = dict(url_sel)
                    sel["attr"] = lazy_attr
                    imgs = self._parser.extract(doc, sel, source.base_url)
                    if imgs:
                        return self._filter_ad_images(imgs, source)
        # 常规提取
        imgs = self._parser.extract(doc, url_sel, source.base_url)
        return self._filter_ad_images(imgs, source)

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
        self._checker.check(source, abs_url)
        html = self._get(source, chapter_url)
        doc = self._parser.parse(html)
        list_cfg = body_cfg or block.get("list") or {}
        root_sel = list_cfg.get("root_selector")
        fields = list_cfg.get("fields") or {}
        if not root_sel or not fields.get("url"):
            return []
        url_sel = fields.get("url")
        if url_sel and isinstance(url_sel, dict):
            attr = url_sel.get("attr")
            if attr in (None, "src"):
                for lazy_attr in ("data-src", "data-original", "src"):
                    sel = dict(url_sel)
                    sel["attr"] = lazy_attr
                    imgs = self._parser.extract(doc, sel, source.base_url)
                    if imgs:
                        return self._filter_ad_images(imgs, source)
        imgs = self._parser.extract(doc, url_sel, source.base_url)
        return self._filter_ad_images(imgs, source)

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

    def fetch_video_streams(self, source: SourceConfig, episode_url: str) -> tuple:
        """视频：抓取单集 dash 音视频双流（播放用）。

        返回 (video_url, audio_url)；非 dash/无音频时 audio_url 为 ""。
        mpv 播放 dash 需要同时喂视频轨+音频轨（B 站音视频分离）。
        """
        api = source.raw.get("api_endpoints") or {}
        episode_api = api.get("episode") or {}
        if episode_api:
            streams = self._fetch_episode_api(source, episode_url, episode_api, want_streams=True)
            if isinstance(streams, dict):
                return streams.get("video", ""), streams.get("audio", "")
            if streams:
                return streams, ""
        # HTML 兜底：单 URL
        return self.fetch_video_episode(source, episode_url), ""

    def fetch_video_episode(self, source: SourceConfig, episode_url: str) -> str:
        """视频：抓取单集播放地址（解密后返回真实地址）。

        优先 api_endpoints.episode（JSON API，可选 sign 签名）；
        否则 endpoints.content.episode HTML 解析（play_url.selector）。
        """
        # JSON API 播放地址（api_endpoints.episode）
        api = source.raw.get("api_endpoints") or {}
        episode_api = api.get("episode") or {}
        if episode_api:
            play = self._fetch_episode_api(source, episode_url, episode_api)
            if play:
                return play

        self._checker.check(source, self._abs_url(source, episode_url))
        html = self._get(source, episode_url)
        doc = self._parser.parse(html)

        content_cfg = source.raw.get("endpoints", {}).get("content") or {}
        block = content_cfg.get("episode") or {}
        play_cfg = block.get("play_url") or {}
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

    def _fetch_episode_api(self, source: SourceConfig, episode_url: str, cfg: dict, want_streams: bool = False) -> str | dict:
        """api_endpoints.episode：JSON API 取播放地址（支持 sign 签名）。

        want_streams=True 时返回 {"video": ..., "audio": ...}（dash 音视频分离）。
        """
        import copy
        from urllib.parse import urlencode, urljoin, quote

        api_url = str(cfg.get("url") or "")
        params = cfg.get("params") or {}
        filled = {}
        # 占位符：{bvid}/{cid}/{id} 从 episode_url 提取；{keyword} 不适用
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
        """剔除广告图（URL 含 ad/banner/promo 等标记）。"""
        ad_markers = ("/ad", "ad_", "banner", "promo", "advert", ".gif")
        filtered = []
        for url in images:
            low = url.lower()
            if any(m in low for m in ad_markers):
                continue
            filtered.append(url)
        return filtered
