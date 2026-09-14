# -*- coding: utf-8 -*-
"""收藏系列分组：把「背徳の少女-第1話」「背徳の少女-第2話」等系列收藏并成一张卡。

纯逻辑、无 GUI 依赖，便于测试。规则（用户确认，2026-09-12）：
- 同名判定：标题去掉尾部「第N话/第N集/第N卷/第N季…」系列单元后，主书名 + 来源
  source_id 都相同 → 视为同一系列（「方舟一号第三季」→ 主书名「方舟一号」）。
- 合并展示：leader（首部）封面 + 「共 N 话」类角标；季号单独按区间显示。
"""
from __future__ import annotations

import re

# 尾部系列单元素：分隔符(可无) + 第 + 数字(阿拉伯/中文) + 话集卷季册期部
# （字符类只含 ASCII 与常见中文标点；花引号对书架标题分隔无意义，不收录）
_TOKEN = re.compile(
    r"(?P<sep>[-\s_·—~，,、]*)?"
    r"第\s*(?P<num>[0-9]+|[一二三四五六七八九十百千万零〇两])\s*"
    r"(?P<unit>話|话|集|卷|季|册|冊|期|部)"
    r"\s*$"
)

# 中文数字（十/百/千/万化简后）
_CN = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
    "二十": 20, "二十一": 21, "二十二": 22, "二十三": 23, "二十四": 24,
    "三十": 30, "三十二": 32, "四十": 40, "四十二": 42, "五十": 50, "五十二": 52,
    "百": 100, "千": 1000, "万": 10000,
    "零": 0, "〇": 0, "两": 2,
}


def _parse_token(token: str) -> tuple:
    """系列单元解析 → (unit, num 或 None)。报错容错。"""
    m = _TOKEN.search(token)
    if not m:
        return "", None
    num = m.group("num")
    n = _CN.get(num, num)
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = None
    return m.group("unit"), n


def split_series(title: str) -> tuple:
    """切分标题 → (主书名, 尾部系列单元或 None)。

    「背徳の少女-第1話」 → ("背徳の少女", "第1話")
    「方舟一号第三季」   → ("方舟一号", "第三季")
    「剑来」            → ("剑来", None)
    「第5章不完全」     → ("第5章不完全", None)  # 非尾部后缀不剥
    """
    t = (title or "").strip()
    if not t:
        return t, None
    m = _TOKEN.search(t)
    if not m:
        return t, None
    if m.group("sep") is not None:
        base = t[: m.start("sep")].rstrip()
        token = t[m.end("sep"):].strip()
    else:
        base = t[: m.start("num")].rstrip()
        token = m.group(0).strip()
    if not base or not token:
        return t, None  # 主书名空（纯后缀标题）→ 不参与合并
    return base, token


def series_key(rec: dict) -> tuple:
    """收藏记录 → 系列分组键 (source_id, 主书名)。无后缀标题用原名。"""
    title = (rec.get("title") or "").strip()
    base, _ = split_series(title)
    base = base or title
    return (rec.get("source_id", ""), base)


def group_favorites(books: list) -> list:
    """收藏列表 → 系列分组（每组 list[dict]，成员按原顺序）。

    同 source_id + 同主书名合并；单本自成一组。返回 list[list[dict]]。
    """
    groups: dict = {}
    order: list = []
    for b in books:
        key = series_key(b)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(b)
    return [groups[k] for k in order]


def series_member_label(rec: dict) -> str:
    """成员在合并卡里的短标签：有尾部单元用尾部单元，否则全名。

    「背徳の少女-第2話」 → 「第2話」；「方舟一号第三季」 → 「第三季」。
    """
    _, token = split_series(rec.get("title") or "")
    return token or (rec.get("title") or "").strip()


def _range_text(ns: list) -> str:
    """季号区间文本（升序去重，连续压缩）：
    [3] → 「第3季」；[1,2,3] → 「第1-3季」；[2,5] → 「第2季 · 第5季」。
    """
    ns = sorted(set(ns))
    segs = []
    lo = hi = ns[0]
    for n in ns[1:]:
        if n == hi + 1:
            hi = n
        else:
            segs.append((lo, hi))
            lo = hi = n
    segs.append((lo, hi))
    return " · ".join(
        f"第{a}-{b}季" if b > a else f"第{a}季" for a, b in segs
    )


def series_badge(members: list) -> str:
    """合并卡角标文本。规则（用户确认）：
    全为季 → 「第3季」/「第1-3季」；话/集 → 「共 N 话/集」；其它 → 「共 N 部」。
    """
    tokens = [split_series(m.get("title") or "")[1] for m in members]
    tokens = [t for t in tokens if t]
    if tokens and all(_parse_token(t)[0] == "季" for t in tokens):
        nums = [n for _, n in (_parse_token(t) for t in tokens) if n]
        if nums:
            return _range_text(nums)
    units = {_parse_token(t)[0] for t in tokens if _parse_token(t)[0]}
    if len(units) == 1:
        return f"共 {len(members)} {next(iter(units))}"
    return f"共 {len(members)} 部"