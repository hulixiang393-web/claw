# -*- coding: utf-8 -*-
"""搜索「响应监听 JSON 模式」测试（test_search_response_json.py）。

反爬 SPA（番茄类）：前端 JS 调 JSON 接口异步填充搜索结果，DOM 常带自定义
字体混淆/渲染失败。render_config.response_json_url 配置后，搜索改走
Playwright 监听接口响应（fetch_search_response_json），直接解析接口数据，
未登录也可搜（番茄 /api/author/search/search_book/v1 无需登录返回完整数据）。
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from framework.search import Search

RAW = {
    "$schema_version": 2,
    "$id": "resp-src",
    "$type": "novel",
    "$name": "响应源",
    "$enabled": True,
    "$weight": 1.0,
    "transports": {"base_url": "https://r.example"},
    "endpoints": {
        "search": {
            "base_url": "/search/{keyword}",
            "method": "GET",
            "render": "playwright",
            "item": {
                "root_selector": {"css": "a"},
                "render_config": {
                    "response_json_url": "/api/search_book/v1",
                    "response_items": "data.search_book_data_list",
                    "response_fields": {
                        "title": "book_name",
                        "author": "author",
                        "cover": "thumb_url",
                        "summary": "book_abstract",
                        "url_template": "{base}/book/{book_id}",
                    },
                    "filter_keyword": False,
                },
            },
        }
    },
}

SAMPLE = {
    "code": 0,
    "data": {
        "total_count": 2,
        "search_book_data_list": [
            {
                "book_name": "穿越：斗破苍穹",
                "book_id": "1001",
                "author": "辛酷",
                "thumb_url": "http://p3.fq.com/np/x?lk3s=1&x-expires=20&x-signature=ab",
                "book_abstract": "三十年河东",
                "last_chapter_title": "第126章",
            },
            {
                "book_name": "诡秘之主",
                "book_id": "2002",
                "author": "爱潜水的乌贼",
                "thumb_url": "",
                "book_abstract": "克苏鲁修仙",
            },
        ],
    },
}


def _patch(monkeypatch, raw):
    from framework import playwright_helper

    calls = {}
    def _fake(*a, **k):
        calls["kwargs"] = k
        return raw
    monkeypatch.setattr(playwright_helper, "fetch_search_response_json", _fake)
    return calls


def _search():
    s = Search.__new__(Search)
    s._cookie_manager = None
    return s


def _src(raw=None):
    from framework.config import SourceConfig

    return SourceConfig.from_dict(raw or RAW, "<mem>")


def _run(search=None, keyword="", src_raw=None):
    search = search or _search()
    src = _src(src_raw)
    return src, search._search_html_rendered(
        src,
        "https://r.example/search/x",
        src.get_search_config()["item"],
        keyword,
    )


# ---------------------------------------------------------------- #
def test_response_items_extracted(monkeypatch):
    """接口 JSON 正常 → 提取结果项（title/url/cover），URL 用 book_id 拼详情。"""
    calls = _patch(monkeypatch, json.dumps(SAMPLE, ensure_ascii=False))
    _src_obj, out = _run(keyword="无关键词也能返回", src_raw=RAW)
    assert len(out) == 2
    assert out[0].title == "穿越：斗破苍穹"
    assert out[0].url == "https://r.example/book/1001"
    assert out[0].cover == "http://p3.fq.com/np/x?lk3s=1&x-expires=20&x-signature=ab"
    assert out[1].title == "诡秘之主"
    assert out[1].url == "https://r.example/book/2002"
    assert out[1].cover == ""
    assert out[0].author == "辛酷"
    # 预热地址缺省回落到源 base_url：番茄这类站全新会话需先访问首页建立行为信号
    assert calls.get("kwargs", {}).get("warm_up_url") == "https://r.example"


def test_keyword_filter_disabled(monkeypatch):
    """filter_keyword=false：关键词不匹配标题也全量保留（站内模糊/接口直出）。"""
    _patch(monkeypatch, json.dumps(SAMPLE, ensure_ascii=False))
    _s, out = _run(keyword="神秘不存在词")
    assert len(out) == 2


def test_missing_response_returns_empty(monkeypatch):
    """接口未等到（返回空）→ 空结果，不抛错。"""
    _patch(monkeypatch, "")
    _s, out = _run(keyword="x")
    assert out == []


def test_bad_json_returns_empty(monkeypatch):
    """接口返回非 JSON → 空结果。"""
    _patch(monkeypatch, "<html>挑战页</html>")
    _s, out = _run(keyword="x")
    assert out == []


def test_empty_list_in_data(monkeypatch):
    """data.search_book_data_list=[] → 空结果。"""
    _patch(monkeypatch, json.dumps({"code": 0, "data": {"search_book_data_list": []}}))
    _s, out = _run(keyword="x")
    assert out == []


def test_items_from_response_json_field_mapping():
    """_items_from_response_json 字段映射：title/cover/author/summary/url。"""
    rc = RAW["endpoints"]["search"]["item"]["render_config"]
    items = Search._items_from_response_json(
        json.dumps(SAMPLE, ensure_ascii=False), rc, _src()
    )
    assert items and len(items) == 2
    first = items[0]
    assert first["title"] == "穿越：斗破苍穹"
    assert first["href"] == "https://r.example/book/1001"
    assert first["author"] == "辛酷"
    assert first["text"] == "三十年河东"
    assert "http://p3.fq.com/np/x" in first["src"]


def test_login_not_required_passively(monkeypatch):
    """response 模式源不配 login_required：0 结果不抛登录引导（番茄已不再需要）。"""
    _patch(monkeypatch, "")
    _s, out = _run(keyword="x")
    assert out == []


# ---------------------------------------------------------------- #
# 分页：response_pages 生效 + 多页合并
# ---------------------------------------------------------------- #
def test_response_pages_forwarded(monkeypatch):
    """response_pages 与 max_results 共同决定抓取页数（番茄 3 页 = 30 条）。"""
    raw = copy.deepcopy(RAW)
    raw["constraints"] = {"search": {"max_results": 30}}
    raw["endpoints"]["search"]["item"]["render_config"]["response_pages"] = 3
    calls = _patch(monkeypatch, json.dumps(SAMPLE, ensure_ascii=False))
    _s, out = _run(keyword="x", src_raw=raw)
    assert calls.get("kwargs", {}).get("pages") == 3
    assert len(out) == 2


def test_response_pages_capped_by_max_results():
    """max_results=20 → 最多 2 页（cap），超过 2 页的配置无效。"""
    src = _src(dict(RAW))
    src.raw["constraints"] = {"search": {"max_results": 20}}
    rc = {"response_pages": 5}
    from framework.search import _response_pages

    assert _response_pages(rc, src) == 2


def test_response_pages_default_one():
    """未配置 response_pages → 只抓 1 页。"""
    from framework.search import _response_pages

    src = _src(dict(RAW))
    assert _response_pages({}, src) == 1


def test_merge_response_pages():
    """多页接口响应按 data 列表合并成一页结构（search_book_data_list 拼接）。"""
    from framework.playwright_helper import _merge_response_pages

    b0 = {"code": 0, "data": {"total_count": 3, "search_book_data_list": SAMPLE["data"]["search_book_data_list"]}}
    b1 = {"code": 0, "data": {"total_count": 3, "search_book_data_list": SAMPLE["data"]["search_book_data_list"]}}
    out = _merge_response_pages({0: json.dumps(b0, ensure_ascii=False),
                                 1: json.dumps(b1, ensure_ascii=False)})
    merged = json.loads(out)
    assert len(merged["data"]["search_book_data_list"]) == 4
    assert merged["data"]["total_count"] == 3  # 骨架页字段保留


def test_merge_response_pages_no_body():
    from framework.playwright_helper import _merge_response_pages

    assert _merge_response_pages({}) == ""
    assert _merge_response_pages({0: "not-json"}) == ""


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))