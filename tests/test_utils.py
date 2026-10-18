"""框架纯函数单元测试（test_utils.py）。

覆盖 framework/utils.py 的四个纯函数：
- jsonpath           点号路径取值（dict 键 + 列表索引含负数，越界/缺失返回 None）
- fill_template      {占位符} 模板填充（mapping 缺失替换为空串）
- guess_image_mime   按魔数猜图片 mime（未知默认 image/jpeg）
- abs_url            相对 URL 补全（带 http/https scheme 或 // 开头原样返回）

运行：python tests/test_utils.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from framework.utils import (  # noqa: E402
    abs_url,
    fill_template,
    guess_image_mime,
    jsonpath,
)


# ---------------------------------------------------------------------- #
# jsonpath
# ---------------------------------------------------------------------- #
def test_jsonpath_dict_dot_path():
    data = {"owner": {"name": "阿明"}, "age": 30}
    assert jsonpath(data, "owner.name") == "阿明"
    assert jsonpath(data, "owner.age") is None          # 缺失键
    print("  [PASS] jsonpath dict 点号路径取值 / 缺失键返回 None")


def test_jsonpath_list_index():
    data = {"data": {"list": ["x", "y", "z"],
                     "durl": [{"url": "u0"}, {"url": "u1"}]}}
    assert jsonpath(data, "data.list.0") == "x"
    assert jsonpath(data, "data.list.2") == "z"
    assert jsonpath(data, "data.durl.0.url") == "u0"
    assert jsonpath(data, "data.durl.1.url") == "u1"
    print("  [PASS] jsonpath list 索引取值（含复合路径 data.durl.0.url）")


def test_jsonpath_negative_index():
    data = {"list": [10, 20, 30]}
    assert jsonpath(data, "list.-1") == 30
    assert jsonpath(data, "list.-3") == 10
    print("  [PASS] jsonpath 负数索引")


def test_jsonpath_out_of_range_and_missing():
    data = {"list": [10, 20, 30]}
    assert jsonpath(data, "list.3") is None      # 越界（上界）
    assert jsonpath(data, "list.-4") is None     # 越界（下界）
    assert jsonpath(data, "list.0.name") is None  # 元素非 dict 继续取值
    assert jsonpath({"a": {"b": 1}}, "a.c.d") is None  # 中间键缺失
    assert jsonpath(data, "nokey") is None
    print("  [PASS] jsonpath 越界 / 缺失 / 非法路径返回 None")


def test_jsonpath_empty_path_and_bad_root():
    assert jsonpath({"a": 1}, "") is None         # 空路径
    assert jsonpath(None, "a.b") is None          # 根不是 dict/list
    assert jsonpath([1, 2], "a.b") is None        # 用键访问 list
    print("  [PASS] jsonpath 空路径 / 根对象非容器")


# ---------------------------------------------------------------------- #
# fill_template
# ---------------------------------------------------------------------- #
def test_fill_template_basic():
    out = fill_template("你好{name}，今年{age}岁",
                        {"name": "小明", "age": 18})
    assert out == "你好小明，今年18岁"
    print("  [PASS] fill_template {占位符} 替换")


def test_fill_template_missing_and_multi():
    assert fill_template("前缀{a}-{b}后缀", {"a": 1}) == "前缀1-后缀"  # b 缺失 → 空串
    assert fill_template("{a}{a}", {"a": "x"}) == "xx"               # 同一占位符多处
    print("  [PASS] fill_template 缺失占位符替换为空串 / 同占位符多处")


def test_fill_template_no_placeholder():
    assert fill_template("没有占位符", {}) == "没有占位符"
    assert fill_template("", {"a": 1}) == ""
    print("  [PASS] fill_template 无占位符原样返回")


# ---------------------------------------------------------------------- #
# guess_image_mime
# ---------------------------------------------------------------------- #
def test_guess_image_mime_magic():
    assert guess_image_mime(b"\xff\xd8\xff\xe0\x00\x10") == "image/jpeg"
    assert guess_image_mime(b"\x89PNG\r\n\x1a\n") == "image/png"
    assert guess_image_mime(b"GIF89a\x01\x00") == "image/gif"
    assert guess_image_mime(b"GIF87a\x01\x00") == "image/gif"
    assert guess_image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert guess_image_mime(b"BM\x00\x00\x00\x00") == "image/bmp"
    print("  [PASS] guess_image_mime JPEG/PNG/GIF/WEBP/BMP 魔数识别")


def test_guess_image_mime_unknown_default():
    assert guess_image_mime(b"NOTANIMAGE") == "image/jpeg"
    assert guess_image_mime(b"") == "image/jpeg"
    assert guess_image_mime(b"\xff\xd8") == "image/jpeg"   # 头太短，非完整 JPEG 魔数
    print("  [PASS] guess_image_mime 未知魔数返回默认 image/jpeg")


# ---------------------------------------------------------------------- #
# abs_url
# ---------------------------------------------------------------------- #
def test_abs_url_absolute_scheme():
    base = "http://a.com/x/"
    assert abs_url(base, "http://b.com/y") == "http://b.com/y"
    assert abs_url(base, "https://b.com/y") == "https://b.com/y"
    print("  [PASS] abs_url http/https 绝对 URL 原样返回")


def test_abs_url_protocol_relative():
    assert abs_url("http://a.com/x/",
                   "//cdn.example.com/i.jpg") == "//cdn.example.com/i.jpg"
    print("  [PASS] abs_url // 协议相对 URL 原样返回")


def test_abs_url_relative_join():
    base = "http://a.com/x/"
    assert abs_url(base, "y.jpg") == "http://a.com/x/y.jpg"
    assert abs_url(base, "../y.jpg") == "http://a.com/y.jpg"
    assert abs_url(base, "sub/z.jpg") == "http://a.com/x/sub/z.jpg"
    print("  [PASS] abs_url 相对路径 urljoin 补全")


def test_abs_url_case_insensitive_scheme():
    assert abs_url("http://a.com/x/", "HTTP://B.COM/y") == "HTTP://B.COM/y"
    assert abs_url("http://a.com/x/", "HTTPS://B.COM/y") == "HTTPS://B.COM/y"
    print("  [PASS] abs_url 大小写 scheme 仍判为绝对 URL 原样返回")


def main():
    tests = [
        test_jsonpath_dict_dot_path,
        test_jsonpath_list_index,
        test_jsonpath_negative_index,
        test_jsonpath_out_of_range_and_missing,
        test_jsonpath_empty_path_and_bad_root,
        test_fill_template_basic,
        test_fill_template_missing_and_multi,
        test_fill_template_no_placeholder,
        test_guess_image_mime_magic,
        test_guess_image_mime_unknown_default,
        test_abs_url_absolute_scheme,
        test_abs_url_protocol_relative,
        test_abs_url_relative_join,
        test_abs_url_case_insensitive_scheme,
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
