"""临时验证脚本：第 1 条修复 —— soft 自检源抓章节页不再触发整页二次 GET + 不写 health.json。

断言：
1) 抓取章节页（url != 自检指定 URL）时 checker.check 不被调用（不再二次 GET）；
2) health.json 文件不被创建/写入；
3) 抓取自检指定 URL（发现列表入口）时后台自检仍触发（checker.check 被调用）。
"""
import os
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from framework.config import SourceConfig
from framework.content import Content
from framework.http import HttpClient
from framework.parser import Parser
from framework.selfcheck import StructureChecker
from framework.source_manager import SourceManager

HTML_DETAIL = (
    "<html><body><h1 class='title'>测试书</h1>"
    "<div class='content'><p>第一段正文内容</p><p>第二段正文内容</p></div>"
    "<div class='chapters'><a href='/n/1.html'>第1章</a></div></body></html>"
)
HTML_LIST = (
    "<html><body><div class='list-item'>"
    "<a href='/book/1'>书1</a></div></body></html>"
)


def make_source(base="https://example.com") -> SourceConfig:
    raw = {
        "$id": "test-health",
        "$name": "测试源",
        "$type": "novel",
        "transports": {"base_url": base, "interval_ms": 0},
        "diagnostics": {"selfcheck": {"strategy": "soft", "selector": {"css": ".list-item"}}},
        "endpoints": {
            "discovery": {
                "list_url": "/list.html",
                "list_item": {
                    "root_selector": {"css": ".list-item"},
                    "fields": {"title": {"css": "a"}, "url": {"css": "a", "attr": "href"}},
                },
                "works_list_item": {"root_selector": {"css": ".list-item"}},
            },
            "detail": {"fields": {"title": {"css": ".title"}, "url": {"css": ".chapters a", "attr": "href"}}},
            "content": {"chapter": {"body": {"selector": {"css": ".content"}}}},
        },
        "constraints": {},
    }
    return SourceConfig.from_dict(raw, path="<test>")


def main() -> None:
    passes = 0
    fails = 0

    def check(name: str, cond: bool) -> None:
        nonlocal passes, fails
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if cond:
            passes += 1
        else:
            fails += 1

    tmpdir = tempfile.mkdtemp(prefix="health_check_")
    health_file = os.path.join(tmpdir, "health.json")

    http = HttpClient(defaults=mock.Mock(timeout=10.0, retries=3, interval_ms=0,
                                         proxy=None, user_agent="t"))
    http._session = None  # 不真正联网
    with mock.patch.object(HttpClient, "get_text", side_effect=lambda url, **kw: HTML_LIST if "list" in url else HTML_DETAIL):
        parser = Parser()
        checker = StructureChecker(http, parser)
        sm = SourceManager(health_file=health_file)
        source = make_source()
        sm.add(source)
        content = Content(http, parser, checker, health_reporter=sm)

        # 1) 章节页抓取：checker.check 不应被调用
        orig_check = checker.check
        called = []

        def spy_check(src, url):
            called.append(url)
            return orig_check(src, url)

        checker.check = spy_check
        text, nxt = content._fetch_chapter_page(source, "/n/1.html")
        check("章节页正文抓取成功", (text or "").strip() != "")
        time.sleep(0.5)  # 给后台线程一点时间（若错误触发）
        check("章节页不触发 checker.check（无二次 GET）", not called)
        check("章节页不写 health.json", not os.path.exists(health_file))

        # 2) 详情页抓取：同样非自检指定页，不应触发
        called.clear()
        detail = content.fetch_detail(source, "/book/1")
        check("详情页解析成功", detail.title != "")
        time.sleep(0.5)
        check("详情页不触发 checker.check", not called)
        check("详情页不写 health.json", not os.path.exists(health_file))

        # 3) 自检指定页（发现列表入口 /list.html）：后台自检应触发
        from framework.discovery import Discovery

        disc = Discovery(http, parser, checker, health_reporter=sm)
        called.clear()
        cats = disc.list_categories(source)
        check("列表页分类解析成功", len(cats) >= 1)
        time.sleep(1.0)
        check("自检指定页触发 checker.check", len(called) == 1)
        # 状态未变（默认 ok → ok）→ 不写盘（「状态变化才写」生效，避免每章全量写盘）
        check("状态未变不写 health.json", not os.path.exists(health_file))

        # 4) 状态变化时才写盘：源曾为 warn，指定页自检恢复 ok → 落盘
        sm.get_health("test-health").state = "warn"
        sm.get_health("test-health").last_error = "旧错误"
        disc.list_categories(source)
        time.sleep(1.0)
        check("状态变化（warn→ok）写 health.json", os.path.exists(health_file))
        if os.path.exists(health_file):
            import json
            data = json.loads(open(health_file, encoding="utf-8").read())
            check("health.json 记录恢复为 ok", data.get("test-health", {}).get("state") == "ok")

    print(f"=== 结果: {passes} PASS / {fails} FAIL ===")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
