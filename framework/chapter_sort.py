"""章节排序纯函数（chapter_sort.py）。

从 content.py 的章节列表排序逻辑独立为模块级函数（函数名完全保留，
行为 100% 不变），供 content 及其它模块复用。

排序设计（卷号+章号复合排序，用户确认）：
- 第1卷 第3章 → (1, 3)；第2卷 第1章 → (2, 1)；第2卷 第1章 排在 第1卷 第3章 之后。
- 数字写法支持阿拉伯 / 汉字（含大写）/ 罗马（全角+半角）/ 全角（１２３）/
  英文（Vol.1 / Ch.5）。
- 可恢复「先最新后顺序」「反爬乱序」的目录到正序；
- 无数字章节（楔子/序章/番外/卷名）保持原相对顺序排末尾；
- 原顺序本就正序则保持不变（稳定排序）。
"""

from __future__ import annotations

import re as _re
from typing import List, Protocol


class _SortableChapter(Protocol):
    """章节对象的最小协议（仅需 title 属性，避免与 content.Chapter 强耦合）。"""

    title: str


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


def _sort_chapters(chapters: List[_SortableChapter]) -> List[_SortableChapter]:
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
