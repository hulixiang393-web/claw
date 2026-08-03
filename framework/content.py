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
        """抓取详情页：元数据 + 章节列表。"""
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
        """抓取单章正文。按类型取正文选择器。"""
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
        if not selector:
            raise ContentMissingError("源未配置正文选择器", source_id=source.source_id)

        paragraphs = self._parser.extract(doc, selector)
        if not paragraphs:
            # 正文选择器未命中：可能是混淆/加密正文，尝试解密
            return self._decrypt_chapter(source, html, url)
        return "\n".join(paragraphs)

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

    def fetch_video_episode(self, source: SourceConfig, episode_url: str) -> str:
        """视频：抓取单集播放地址（解密后返回真实地址）。

        对应 endpoints.content.episode：
        - play_url.selector → 播放地址提取
        - decryption        → 解密（B站 wbi 签名等）
        """
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
