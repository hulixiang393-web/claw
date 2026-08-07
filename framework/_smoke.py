"""内核基础模块冒烟测试（离线，不依赖 GUI）。"""
import os
import sys
import tempfile
import pathlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.errors import *
from framework.events import EventBus, Event, EVENT_SEARCH_STARTED
from framework.config import SourceConfig
from framework.source_manager import SourceManager
from framework.settings_manager import SettingsManager
from framework.search_history import SearchHistory
from framework.theme_manager import ThemeManager


def test_eventbus():
    bus = EventBus()
    got = []
    bus.subscribe(lambda e: got.append(e.type))
    bus.emit(Event(EVENT_SEARCH_STARTED, {"source_id": "x"}))
    assert got == [EVENT_SEARCH_STARTED], got
    print("EventBus OK")


def test_themes():
    p = pathlib.Path(tempfile.mkdtemp()) / "app_config.json"
    sm = SettingsManager(p)
    tm = ThemeManager(sm)
    assert tm.current_key() == "sakura"
    assert sm.get("ui", "theme") == "sakura"
    tm.switch_to("midnight")
    assert sm.get("ui", "theme") == "midnight"
    # 非法主题回退 sakura
    tm.switch_to("bad_key")
    assert tm.current_key() == "sakura"
    # QSS 生成
    qss = tm.generate_qss("midnight")
    assert "{bg}" not in qss  # 模板已替换
    assert "#1E1B26" in qss  # midnight bg
    print("Theme OK")


def test_search_history():
    sh = SearchHistory(pathlib.Path(tempfile.mkdtemp()) / "h.json")
    for i in range(25):
        sh.push(f"word{i}")
    assert len(sh.recent()) == 20, len(sh.recent())
    assert sh.recent()[0] == "word24"
    sh.push("word5")  # 去重移前
    assert sh.recent()[0] == "word5"
    print("SearchHistory OK (20条约束+去重)")


def test_source_manager():
    cfg = SourceConfig.from_dict(
        {
            "$schema_version": 2,
            "$id": "demo_a",
            "$type": "novel",
            "$name": "站点A",
            "transports": {"base_url": "http://x"},
            "$enabled": True,
            "$weight": 1.0,
        }
    )
    assert cfg.source_id == "demo_a"
    assert cfg.content_type == "novel"

    mgr = SourceManager()
    mgr.add(cfg)
    n = SourceConfig.from_dict(
        {
            "$schema_version": 2,
            "$id": "demo_b",
            "$type": "video",
            "$name": "站点B",
            "transports": {"base_url": "http://y"},
            # 缺省 $enabled 默认 True
        }
    )
    mgr.add(n)
    assert mgr.count_by_type() == {"novel": 1, "comic": 0, "video": 1}
    assert mgr.count_enabled() == 2
    mgr.set_enabled("demo_a", False)
    assert mgr.count_enabled() == 1
    mgr.update_health("demo_a", "broken", "结构变更")
    assert mgr.count_broken() == 1
    h = mgr.get_health("demo_a")
    assert h.state == "broken"
    assert h.history[-1] == "broken"
    print("SourceManager OK (统计+健康)")


def test_config_validation():
    # 缺 $id 报错
    try:
        SourceConfig.from_dict({"$type": "novel", "$name": "x"})
        assert False, "应报错"
    except ConfigError as e:
        assert "$id" in e.message
    # 非法 $type
    try:
        SourceConfig.from_dict({"$id": "a", "$type": "music", "$name": "x"})
        assert False, "应报错"
    except ConfigError:
        pass
    print("Config 校验 OK")


def test_chapter_pagination():
    """章节分页抓取：跨 _N.html 续页拼接，且不误抓下一章。"""
    import json
    from framework.http import HttpClient
    from framework.parser import Parser
    from framework.selfcheck import StructureChecker
    from framework.content import Content
    from framework.decrypter import Decrypter

    def page_html(blocks, next_link=""):
        nav = (
            '<nav class="read-nav"><a href="/b/1/c1.html">上一章</a>|'
            f'<a href="/b/1/">目录</a>|<a href="{next_link}">下一章</a></nav>'
        ) if next_link else ""
        return f'<html><body><div id="chaptercontent">{"".join(blocks)}{nav}</div></body></html>'

    # 首页 → 续页 _1（同基路径）；续页 → 真下一章 c2（不同基路径）
    # 模拟真实结构：上一章指向真上一章（不同基路径），续页页的上一章指向无后缀首页
    urls = {
        "http://x/b/1/c1.html": page_html(
            ['<p>首页段落0</p>', '<p>首页段落1</p>', '<p>首页段落2</p>'],
            "/b/1/c1_1.html"),
        "http://x/b/1/c1_1.html": page_html(
            ['<p>续页段落0</p>', '<p>续页段落1</p>', '<p>续页段落2</p>'],
            "/b/1/c2.html"),
    }

    class MockHttp(HttpClient):
        def get_text(self, url, headers=None, proxy=None, timeout=10, retries=3, interval_ms=0, encoding=None, proxy_pool=None):
            return urls.get(url) or page_html([])

    http = MockHttp(sleeper=lambda _: None)
    parser = Parser()
    checker = StructureChecker(http, parser, default_strategy="off")
    content = Content(http, parser, checker, Decrypter(http))

    src = SourceConfig.from_dict({
        "$schema_version": 2, "$id": "pg", "$type": "novel", "$name": "分页站",
        "transports": {"base_url": "http://x"},
        "endpoints": {
            "content": {"chapter": {
                "body": {"selector": {"css": "#chaptercontent p"}},
                "pagination": {"enabled": True},
            }},
        },
    })
    text = content.fetch_chapter(src, "http://x/b/1/c1.html")
    assert "首页段落0" in text and "首页段落2" in text, text
    assert "续页段落0" in text and "续页段落2" in text, text
    # 未误抓下一章 c2（该页无正文）
    assert "下一章" not in text and "c2" not in text, text
    print("章节分页 OK（跨页拼接 + 不越章）")

    # 分页关闭：只抓首页
    src2 = SourceConfig.from_dict({
        "$schema_version": 2, "$id": "pg2", "$type": "novel", "$name": "无分页",
        "transports": {"base_url": "http://x"},
        "endpoints": {
            "content": {"chapter": {
                "body": {"selector": {"css": "#chaptercontent p"}},
                "pagination": {"enabled": False},
            }},
        },
    })
    text2 = content.fetch_chapter(src2, "http://x/b/1/c1.html")
    assert "首页段落0" in text2 and "续页段落0" not in text2, text2
    print("分页关闭 OK（只抓首页）")


def test_chapter_sorting():
    """章节排序：简繁話 / 卷话复合 / 数字结尾 / 全角 / 无数字排末尾。"""
    from framework.content import (
        _sort_chapters,
        Chapter,
        _extract_chapter_number,
        chapter_label,
    )

    # 1. 繁体「話」能解析
    assert _extract_chapter_number("第1話-長不大的小飛俠") == (0, 1), \
        _extract_chapter_number("第1話-長不大的小飛俠")
    assert _extract_chapter_number("第10話 abc") == (0, 10)
    assert _extract_chapter_number("第壹話") == (0, 1)  # 大写数字繁体
    # 2. 全角数字
    assert _extract_chapter_number("第１２話 全角") == (0, 12)
    # 3. 数字结尾（书名 2/3，无「第X话」前缀）
    assert _extract_chapter_number("強制催眠噴霧 2") == (0, 2), \
        _extract_chapter_number("強制催眠噴霧 2")
    assert _extract_chapter_number("书名3") == (0, 3)
    # 4. 卷话复合
    assert _extract_chapter_number("第1卷 第3话") == (1, 3)
    assert _extract_chapter_number("Vol.2 第5话") == (2, 5)
    # 5. 无数字 → None（排末尾）
    assert _extract_chapter_number("番外篇1") is None
    assert _extract_chapter_number("強制催眠噴霧") is None

    # 6. chapter_label 提取简体/繁体
    assert chapter_label("第3話-某漫画") == "第3話"
    assert chapter_label("第1话 abc") == "第1话"

    # 7. 排序：繁体話混排正序
    titles = ["第9話-b", "第3话 a", "第10話-c", "第1話-x", "第12話-d"]
    chs = [Chapter(t, "x") for t in titles]
    got = [_extract_chapter_number(c.title)[1] for c in _sort_chapters(chs)]
    assert got == [1, 3, 9, 10, 12], got

    # 8. 无数字章节排末尾，且保持相对顺序
    mixed = [Chapter("第2话 b", "x"), Chapter("特别篇", "y"), Chapter("第1话 a", "z")]
    res = _sort_chapters(mixed)
    assert [c.title for c in res] == ["第1话 a", "第2话 b", "特别篇"], \
        [c.title for c in res]
    print("章节排序 OK（繁簡話/卷話/數字結尾/全角/無數字）")


if __name__ == "__main__":
    test_eventbus()
    test_themes()
    test_search_history()
    test_source_manager()
    test_config_validation()
    test_chapter_pagination()
    test_chapter_sorting()
    print("\n=== 内核基础模块冒烟测试全部通过 ===")
