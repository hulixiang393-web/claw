# -*- coding: utf-8 -*-
"""收藏系列分组测试（test_series_group.py）。

规则（用户确认 2026-09-12）：标题去尾部「第N话/集/卷/季」后主书名 + source_id
相同 → 并入一张卡；展示「leader 封面 + 角标」；点击进合集选集。
真实样例：avgood「背徳の少女-第1話/第2話」两条独立收藏、xuandm「方舟一号第三季」。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from framework.series_group import (
    group_favorites,
    series_badge,
    series_member_label,
    split_series,
)


def _rec(title, sid="avgood", url="https://av.example"):
    return {"title": title, "source_id": sid, "url": url + title}


# ------------------------------------------------------------------------- #
def test_split_series_arabic():
    assert split_series("背徳の少女-第1話") == ("背徳の少女", "第1話")
    assert split_series("背徳の少女 - 第2话") == ("背徳の少女", "第2话")


def test_split_series_chinese_num_no_sep():
    assert split_series("方舟一号第三季") == ("方舟一号", "第三季")


def test_split_series_various_units():
    assert split_series("斗破苍穹·第12集") == ("斗破苍穹", "第12集")
    assert split_series("凡人修仙传 第3卷") == ("凡人修仙传", "第3卷")
    assert split_series("某某·第二册") == ("某某", "第二册")
    assert split_series("灌篮高手 第01期") == ("灌篮高手", "第01期")


def test_split_series_no_suffix():
    assert split_series("剑来") == ("剑来", None)
    assert split_series("") == ("", None)


def test_split_series_middle_suffix_kept():
    assert split_series("第5章不完全") == ("第5章不完全", None)


def test_split_series_pure_suffix_kept():
    assert split_series("第1话") == ("第1话", None)  # 主书名空 → 不参与合并


# ------------------------------------------------------------------------- #
def test_group_merges_same_series_same_source():
    a = _rec("背徳の少女-第1話")
    b = _rec("背徳の少女-第2話")
    out = group_favorites([a, b])
    assert len(out) == 1
    assert [r["title"] for r in out[0]] == ["背徳の少女-第1話", "背徳の少女-第2話"]


def test_group_keeps_different_series_separate():
    out = group_favorites([_rec("A-第1話"), _rec("B-第1話")])
    assert len(out) == 2


def test_group_cross_source_not_merged():
    out = group_favorites([_rec("X-第1話", "avgood"), _rec("X-第1話", "18mh")])
    assert len(out) == 2


def test_group_merges_plain_and_suffixed():
    out = group_favorites([_rec("背徳の少女-第2話"), _rec("背徳の少女")])
    assert len(out) == 1
    assert len(out[0]) == 2


def test_group_single_stays_single():
    out = group_favorites([_rec("剑来")])
    assert len(out) == 1 and len(out[0]) == 1


def test_group_season_titles_merge():
    out = group_favorites([
        _rec("方舟一号第一季", "xuandm"),
        _rec("方舟一号第三季", "xuandm"),
    ])
    assert len(out) == 1
    assert len(out[0]) == 2


# ------------------------------------------------------------------------- #
def test_member_label():
    assert series_member_label(_rec("背徳の少女-第2話")) == "第2話"
    assert series_member_label(_rec("方舟一号第三季")) == "第三季"
    assert series_member_label(_rec("剑来")) == "剑来"


def test_badge_season_range():
    assert series_badge([_rec("方舟一号第一季"), _rec("方舟一号第三季")]) == "第1季 · 第3季"
    assert series_badge([_rec("方舟一号第一季"), _rec("方舟一号第二季"), _rec("方舟一号第三季")]) == "第1-3季"
    assert series_badge([_rec("方舟一号第三季")]) == "第3季"


def test_badge_chapters_count():
    out = [
        _rec("背徳の少女-第1話"), _rec("背徳の少女-第2話"), _rec("背徳の少女-第3話"),
    ]
    assert series_badge(out) == "共 3 話"


def test_badge_episodes_ascii_unit():
    out = [_rec("斗破苍穹·第1集"), _rec("斗破苍穹·第2集")]
    assert series_badge(out) == "共 2 集"


def test_badge_mixed_fallback():
    out = [_rec("某作品-第1话"), _rec("某作品-第3季")]
    assert series_badge(out) == "共 2 部"


def test_badge_no_suffix_fallback():
    out = [_rec("剑来"), _rec("剑来2")]
    assert series_badge(out) == "共 2 部"


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))