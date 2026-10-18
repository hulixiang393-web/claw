# -*- coding: utf-8 -*-
"""包子漫画封面域名修复测试（test_baozimh_cover_rewrite.py）。

包子图源 CDN 存在多域名：页面/接口常返回 `static-tw.baozimh.com`（该域
缺防盗链 cookie 时 403），而 `static-tw.bzmgcn.com`（SSR 原始域）直接
200。源配置 url_replace 把静态封面一律重写到 bzmgcn.com 域。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from framework.parser import Parser

JSON_PATH = Path(__file__).resolve().parent.parent / "sources" / "baozimh.json"


def _sel(section: str):
    raw = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    ep = raw["endpoints"]
    if section == "works_list_item":
        return ep["discovery"]["works_list_item"]["fields"]["cover"]
    if section == "search":
        return ep["search"]["item"]["fields"]["cover"]
    return ep[section]["fields"]["cover"]


def _rewritten(sel, src, alt=False):
    tag = f'<amp-img{" alt=x" if alt else ""} src="{src}"></amp-img>'
    doc = Parser().parse(f"<html><body><div>{tag}</div></body></html>")
    return Parser().extract_first(doc, sel, base_url="https://www.baozimh.com")


# ----------------------------------------------------------------- #
def test_works_list_cover_rewritten_to_bzmgcn():
    """baozimh.com 封面 → 重写到 bzmgcn.com（含 query 剥离）。"""
    out = _rewritten(_sel("works_list_item"), "https://static-tw.baozimh.com/cover/wx.jpg?w=285&h=375&q=100")
    assert out == "https://static-tw.bzmgcn.com/cover/wx.jpg"


def test_search_cover_rewritten_to_bzmgcn():
    out = _rewritten(_sel("search"), "https://static-tw.baozimh.com/cover/xz.jpg?w=285")
    assert out == "https://static-tw.bzmgcn.com/cover/xz.jpg"


def test_detail_cover_rewritten_to_bzmgcn():
    out = _rewritten(_sel("detail"), "https://static-tw.baozimh.com/cover/wx.jpg", alt=True)
    assert out == "https://static-tw.bzmgcn.com/cover/wx.jpg"


def test_bzimg_domain_also_rewritten():
    out = _rewritten(_sel("works_list_item"), "https://static-tw.bzimg.com/cover/yx.jpg?w=285")
    assert out == "https://static-tw.bzmgcn.com/cover/yx.jpg"


def test_other_domain_left_untouched():
    """非 static-tw.baozimh/bzimg 域不重写（不误伤其它域名）。"""
    out = _rewritten(_sel("works_list_item"), "https://cdn.other.com/cover/wx.jpg")
    assert out == "https://cdn.other.com/cover/wx.jpg"


def test_bzmgcn_domain_identity():
    """已是目标域则不处理（重写幂等）。"""
    out = _rewritten(_sel("works_list_item"), "https://static-tw.bzmgcn.com/cover/wx.jpg")
    assert out == "https://static-tw.bzmgcn.com/cover/wx.jpg"


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))