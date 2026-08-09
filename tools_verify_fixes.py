"""临时验证脚本：
- 第 5 条：自动禁用恢复路径 vs 手动禁用不自动恢复
- 第 2 条：search._http_get/_http_post_form 传递源级 interval_ms 给 HttpClient
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from framework.config import SourceConfig
from framework.errors import RequestError
from framework.http import AntiScrapeError, HttpClient, NetworkDefaults
from framework.parser import Parser
from framework.search import Search
from framework.source_manager import SourceManager

passes = 0
fails = 0


def check(name, cond):
    global passes, fails
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if cond:
        passes += 1
    else:
        fails += 1


def make_source(sid="test", interval_ms=0):
    raw = {
        "$id": sid, "$name": f"源{sid}", "$type": "novel",
        "transports": {"base_url": "https://example.com", "interval_ms": interval_ms},
        "endpoints": {
            "search": {
                "base_url": "/search", "keyword_param": "keyword", "method": "GET",
                "item": {"root_selector": {"css": ".r"}, "fields": {"title": {"css": "a"}}},
            },
            "content": {"chapter": {"body": {"selector": {"css": ".c"}}}},
        },
        "constraints": {"search": {"max_pages": 3}},
    }
    return SourceConfig.from_dict(raw, path="<test>")


def test_interval_ms():
    print("== 第 2 条：interval_ms 传递 ==")
    http = mock.Mock(spec=HttpClient)
    http.defaults = mock.Mock(timeout=10.0, retries=3, interval_ms=0)
    src = make_source(interval_ms=500)
    s = Search(http, Parser())

    s._http_get(src, "https://example.com/search?keyword=x&page=1")
    kw = http.get_text.call_args
    check("_http_get 传 interval_ms=500", kw.kwargs.get("interval_ms") == 500)
    check("_http_get 传 retries", kw.kwargs.get("retries") == 3)

    http.reset_mock()
    s._http_post_form(src, "https://example.com/search", {"keyword": "x"})
    kw = http.post_form.call_args
    check("_http_post_form 传 interval_ms=500", kw.kwargs.get("interval_ms") == 500)


def test_manual_vs_auto_disable():
    print("== 第 5 条：手动禁用 vs 自动禁用 ==")
    sm = SourceManager()
    src = make_source("auto")
    sm.add(src)

    # 自动禁用：连续 broken 达阈值
    h = sm.get_health("auto")
    h.state = "broken"
    h.history = ["broken", "broken", "broken"]
    sm._apply_policy("auto", h)
    check("连续 broken 自动禁用", src.enabled is False and "auto" in sm._auto_disabled)

    # 恢复路径：健康转 ok → 自动重新启用
    h.state = "ok"
    h.last_error = "连续失败自动禁用"
    sm._apply_policy("auto", h)
    check("自动禁用的源健康恢复后重新启用", src.enabled is True)
    check("恢复后清除自动禁用标记", "auto" not in sm._auto_disabled)

    # 手动禁用：用户手动关 → 标记清除，健康恢复不自动打开
    src2 = make_source("manual")
    sm.add(src2)
    sm.set_enabled("manual", False)
    check("手动禁用不进入自动禁用集合", "manual" not in sm._auto_disabled)
    h2 = sm.get_health("manual")
    h2.last_error = "曾经的自检错误"
    h2.state = "ok"
    sm._apply_policy("manual", h2)
    check("手动禁用的源健康恢复后不被自动打开", src2.enabled is False)


def test_anti_scrape_passthrough():
    print("== 第 4 条：AntiScrapeError 类型透传 ==")
    http = HttpClient(defaults=NetworkDefaults(timeout=1.0, retries=0, interval_ms=0))
    # 所有方法都走 session.get/post，模拟 403 反爬响应
    http._session = mock.Mock()
    resp_403 = mock.Mock(status_code=403, headers={})
    http._session.get.return_value = resp_403
    http._session.post.return_value = resp_403
    for method, url in (
        ("get_text", "https://example.com/a"),
        ("get_bytes", "https://example.com/b"),
        ("post_json", "https://example.com/c"),
        ("post_form", "https://example.com/d"),
    ):
        try:
            getattr(http, method)(url)
            check(f"{method} 抛出 AntiScrapeError", False)
        except Exception as exc:
            check(f"{method} 抛出 AntiScrapeError", isinstance(exc, AntiScrapeError))


def test_wave_early_stop():
    print("== 第 2 条②：分波遇连续空页提前停发 ==")
    http = mock.Mock(spec=HttpClient)
    http.defaults = mock.Mock(timeout=10.0, retries=3, interval_ms=0)
    src = make_source(interval_ms=0)
    src.raw["constraints"]["search"]["max_pages"] = 10
    parser = mock.Mock(spec=Parser)
    parser.parse.return_value = mock.Mock()
    parser.parse_items.return_value = []
    s = Search(http, parser)
    results = s._search_html(src, "某关键词")
    check("搜索返回空（无结果）", results == [])
    check("仅请求第 1 波 3 页即停发（非 10 页）", http.get_text.call_count == 3)


if __name__ == "__main__":
    test_interval_ms()
    test_manual_vs_auto_disable()
    test_anti_scrape_passthrough()
    test_wave_early_stop()
    print(f"=== 结果: {passes} PASS / {fails} FAIL ===")
    sys.exit(1 if fails else 0)
