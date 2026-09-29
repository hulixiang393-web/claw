"""章节排序纯函数单元测试（test_chapter_sort.py）。

覆盖 framework/chapter_sort.py：
- _extract_chapter_number  从章节标题提取排序键 (卷号, 章号)，无数字返回 None
- _sort_chapters           按 (卷号, 章号) 升序稳定排序，无数字章节保持相对顺序排末尾

运行：python tests/test_chapter_sort.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from framework.chapter_sort import (  # noqa: E402
    _extract_chapter_number,
    _sort_chapters,
)


def _ch(title):
    return SimpleNamespace(title=title)


# ---------------------------------------------------------------------- #
# _extract_chapter_number
# ---------------------------------------------------------------------- #
def test_extract_arabic():
    assert _extract_chapter_number("第123章") == (0, 123)
    assert _extract_chapter_number("第3话") == (0, 3)
    print("  [PASS] _extract_chapter_number 阿拉伯数字章号")


def test_extract_chinese_capital():
    assert _extract_chapter_number("第壹章") == (0, 1)
    assert _extract_chapter_number("第百章") == (0, 100)
    assert _extract_chapter_number("第十二话") == (0, 12)
    print("  [PASS] _extract_chapter_number 汉字（含大写）数字章号")


def test_extract_fullwidth():
    assert _extract_chapter_number("第１２話") == (0, 12)
    print("  [PASS] _extract_chapter_number 全角数字章号")


def test_extract_roman_volume():
    assert _extract_chapter_number("Vol.2") == (2, 0)
    assert _extract_chapter_number("Vol.III") == (3, 0)
    print("  [PASS] _extract_chapter_number 罗马数字卷号 Vol.X")


def test_extract_volume_chapter():
    assert _extract_chapter_number("第1卷 第3章") == (1, 3)
    assert _extract_chapter_number("第2卷 第1章") == (2, 1)
    assert _extract_chapter_number("第1卷 序章") is None  # 序章不算数字章
    print("  [PASS] _extract_chapter_number 卷章复合 / 序章排除")


def test_extract_english_chapter():
    assert _extract_chapter_number("Chapter 5") == (0, 5)
    assert _extract_chapter_number("Ch.12") == (0, 12)
    print("  [PASS] _extract_chapter_number 英文章号 Chapter/Ch.")


def test_extract_no_number():
    assert _extract_chapter_number("番外") is None
    assert _extract_chapter_number("楔子") is None
    assert _extract_chapter_number("序章") is None
    assert _extract_chapter_number("") is None
    print("  [PASS] _extract_chapter_number 番外/楔子/序章/空标题返回 None")


# ---------------------------------------------------------------------- #
# _sort_chapters
# ---------------------------------------------------------------------- #
def test_sort_numeric_order():
    chs = [_ch("第3章"), _ch("第1章"), _ch("第2章")]
    titles = [c.title for c in _sort_chapters(chs)]
    assert titles == ["第1章", "第2章", "第3章"]
    print("  [PASS] _sort_chapters 数字序排序")


def test_sort_volume_chapter():
    # 卷章复合：第2卷 第1章 排在 第1卷 第3章 之后
    chs = [_ch("第1卷 第3章"), _ch("第2卷 第1章"), _ch("第1卷 第1章")]
    titles = [c.title for c in _sort_chapters(chs)]
    assert titles == ["第1卷 第1章", "第1卷 第3章", "第2卷 第1章"]
    print("  [PASS] _sort_chapters 卷章复合排序优先")


def test_sort_unnumbered_tail():
    chs = [_ch("第3章"), _ch("番外A"), _ch("第1章"), _ch("番外B")]
    titles = [c.title for c in _sort_chapters(chs)]
    assert titles == ["第1章", "第3章", "番外A", "番外B"]
    print("  [PASS] _sort_chapters 无数字章节尾置且保持相对顺序")


def test_sort_empty_and_single():
    assert _sort_chapters([]) == []
    titles = [c.title for c in _sort_chapters([_ch("第1章")])]
    assert titles == ["第1章"]
    print("  [PASS] _sort_chapters 空列表 / 单元素")


def test_sort_no_volume_before_volume():
    # 无卷章号 → (0, 章号) 排最前；有卷 (卷>0) 排后
    chs = [_ch("第2卷 第1章"), _ch("第5章"), _ch("第3卷 第1章")]
    titles = [c.title for c in _sort_chapters(chs)]
    assert titles == ["第5章", "第2卷 第1章", "第3卷 第1章"]
    print("  [PASS] _sort_chapters 无卷章节排在有卷之前")


def main():
    tests = [
        test_extract_arabic,
        test_extract_chinese_capital,
        test_extract_fullwidth,
        test_extract_roman_volume,
        test_extract_volume_chapter,
        test_extract_english_chapter,
        test_extract_no_number,
        test_sort_numeric_order,
        test_sort_volume_chapter,
        test_sort_unnumbered_tail,
        test_sort_empty_and_single,
        test_sort_no_volume_before_volume,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback

            print(f"  [FAIL] {t.__name__} 失败：{exc}")
            traceback.print_exc()
    print(f"\n结果：{len(tests) - failed}/{len(tests)} 通过")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
