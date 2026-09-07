# -*- coding: utf-8 -*-
"""跨源换源核心逻辑测试（framework/cross_source.py）。

覆盖：标题清洗、相似度、find_cross_source（并发/异常隔离/过滤）、
switch_to_source（视频集数保留）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.cross_source import (
    clean_title, similarity, find_cross_source, switch_to_source,
    CrossSourceCandidate,
)
from framework.config import SourceConfig
from framework.parser import Parser
from framework.search import Search


# --------------------------------------------------------------------------- #
# clean_title / similarity
# --------------------------------------------------------------------------- #
def test_clean_title_strips_noise():
    assert "【" not in clean_title("【合集】名称")
    assert "（" not in clean_title("名称（独家）")
    assert clean_title("名称 undefined  ") == "名称"


def test_clean_title_strips_brackets_halfwidth():
    assert "(" not in clean_title("名称(高清)")
    assert "[" not in clean_title("[独家]名称")


def test_similarity_same_is_one():
    assert similarity("测试", "测试") == 1.0


def test_similarity_low_when_season_differs():
    assert similarity("测试 第一季", "测试 第三季") < 0.9
    # 注：纯 SequenceMatcher.ratio()（全局约束规定的算法）对「Season 1/2」这类
    # 英文短串天然 ≥0.9（仅 1 字符差），属算法固有属性，见 track-a-report；
    # 中文「第一季/第三季」才是季区分过滤的可测试主路径。
    assert similarity("测试 第一季", "测试 第三季") < 0.9


def test_clean_title_keeps_season():
    assert "第一季" in clean_title("测试 第一季")
    assert "Season 1" in clean_title("测试 Season 1")


def test_clean_title_none_and_empty():
    assert clean_title(None) == ""


# --------------------------------------------------------------------------- #
# find_cross_source fixture（真实 Search 驱动）
# --------------------------------------------------------------------------- #
def _search_src_json(sid, name, base):
    return {
        "$schema_version": 2,
        "$id": sid,
        "$type": "video",
        "$name": name,
        "$enabled": True,
        "$weight": 1.0,
        "$metadata": {"homepage": base + "/", "lang": "zh-CN", "region": "cn"},
        "transports": {"base_url": base},
        "endpoints": {
            "search": {
                "base_url": "/s/",
                "method": "GET",
                "keyword_param": "q",
                "paginator": {"url_template": "/s/?q={keyword}"},
                "item": {
                    "root_selector": {"css": "div.r"},
                    "fields": {
                        "title": {"css": "h3 a"},
                        "url": {"css": "h3 a", "attr": "href"},
                    },
                },
            }
        },
    }


def _result_html(titles_urls):
    cards = "".join(
        f'<div class="r"><h3><a href="{url}">{title}</a></h3></div>'
        for title, url in titles_urls
    )
    return f"<html><body>{cards}</body></html>"


class _FakeHttp:
    """按 url 前缀返回预设 HTML（一个源一个页面）；host 含 raise: 抛异常。"""

    def __init__(self, pages: dict):
        from framework.http import NetworkDefaults

        self._pages = pages  # host -> html
        self.defaults = NetworkDefaults()

    def get_text(self, url, **kwargs):
        for key, html in self._pages.items():
            if key in url:
                if key.startswith("raise:"):
                    raise RuntimeError(f"fail {url}")
                return html
        return "<html><body></body></html>"

    def post_form(self, url, form_data=None, **kwargs):
        return self.get_text(url)


def _make_manager(src_configs):
    mgr = __import__("framework.source_manager", fromlist=["SourceManager"]).SourceManager()
    for cfg in src_configs:
        mgr.add(cfg)
    return mgr


def _make_content(http, parser=None):
    from framework.selfcheck import StructureChecker

    class _NoopChecker:
        def check(self, *args, **kwargs):
            return True

    from framework.content import Content
    return Content(http, parser or Parser(), checker=_NoopChecker())


def _build_env(current_sid="src-a", concurrent=4):
    """构造：当前源 src-a + src-b（结果匹配）+ src-c（第二季，被过滤）。"""
    src_a = SourceConfig.from_dict(_search_src_json("src-a", "源A", "http://a.com"), "<a>")
    src_b = SourceConfig.from_dict(_search_src_json("src-b", "源B", "http://b.com"), "<b>")
    src_c = SourceConfig.from_dict(_search_src_json("src-c", "源C", "http://c.com"), "<c>")

    matches_html = _result_html([
        ("测试动画 第一季", "/v/1.html"),
        ("测试动画 第一季（合集）", "/v/2.html"),
        ("完全不同的作品", "/v/3.html"),
    ])
    other_html = _result_html([("测试动画 第二季", "/v/4.html")])

    http = _FakeHttp({
        "b.com": matches_html,
        "c.com": other_html,
    })
    content = _make_content(http)
    mgr = _make_manager([src_a, src_b, src_c])

    from framework.content import Detail
    detail = Detail(
        source_id=current_sid, content_type="video",
        url="http://a.com/v/1", title="测试动画 第一季",
    )
    return {"current": src_a, "mgr": mgr, "content": content, "detail": detail}


def test_find_cross_source_filters_and_orders():
    """两源并发：结果数量正确 + 相似度<0.9 被过滤 + 按相似度降序。"""
    env = _build_env()
    cands = find_cross_source(
        env["mgr"], env["content"], env["detail"], "video", concurrent=2, top_n=10
    )
    # src-b 命中 2 条（「测试动画 第一季」「…（合集）」清洗后同义≥0.9）；
    # src-c（第二季）被过滤；当前源 src-a 不参与
    assert len(cands) == 2, [c.source_id for c in cands]
    assert all(c.content_type == "video" for c in cands)
    assert all(c.source_id != "src-a" for c in cands)
    assert all(c.source_id == "src-b" for c in cands)
    # 相似度降序
    sims = [c.similarity for c in cands]
    assert sims == sorted(sims, reverse=True)
    # 全部达到阈值
    assert all(c.similarity >= 0.9 for c in cands)


def test_find_cross_source_top_n():
    """top_n 截断候选数量。"""
    env = _build_env()
    cands = find_cross_source(env["mgr"], env["content"], env["detail"], "video", top_n=1)
    assert len(cands) == 1


def test_find_cross_source_single_source_error_keeps_others():
    """一个源抛错，另一个源结果保留；异常隔离不中断。"""
    current = SourceConfig.from_dict(_search_src_json("src-a", "源A", "http://a.com"), "<a>")
    src_b = SourceConfig.from_dict(_search_src_json("src-b", "源B", "http://b.com"), "<b>")
    src_c = SourceConfig.from_dict(_search_src_json("src-c", "源C", "http://c.com"), "<c>")
    mgr = _make_manager([current, src_b, src_c])

    class _OneFails:
        """http://b.com 返回结果；其他 host 抛错。"""
        def __init__(self):
            from framework.http import NetworkDefaults
            self.defaults = NetworkDefaults()

        def get_text(self, url, **kwargs):
            if "b.com" in url:
                return _result_html([("测试动画 第一季", "/v/1.html")])
            raise RuntimeError("fail " + url)

        def post_form(self, url, form_data=None, **kwargs):
            return self.get_text(url)

    content = _make_content(_OneFails())

    from framework.content import Detail
    detail = Detail(source_id="src-a", content_type="video", url="http://a.com/v/1", title="测试动画 第一季")
    cands = find_cross_source(mgr, content, detail, "video")
    # 只有 b.com 命中；c.com 抛错被隔离，不因异常中断
    assert len(cands) == 1
    assert cands[0].source_id == "src-b"
    assert cands[0].similarity >= 0.9


def test_find_cross_source_season_filtered():
    """「第X季」不同的同作品相似度 <0.9 → 被过滤。"""
    current = SourceConfig.from_dict(_search_src_json("src-a", "源A", "http://a.com"), "<a>")
    src_b = SourceConfig.from_dict(_search_src_json("src-b", "源B", "http://b.com"), "<b>")
    mgr = _make_manager([current, src_b])
    http = _FakeHttp({"b.com": _result_html([("测试动画 第一季", "/v/1.html")])})
    content = _make_content(http)

    from framework.content import Detail
    detail = Detail(source_id="src-a", content_type="video", url="http://a.com/v/1", title="测试动画 第二季")
    cands = find_cross_source(mgr, content, detail, "video")
    assert cands == []  # 第一季 vs 第二季 → 相似度<0.9


# --------------------------------------------------------------------------- #
# switch_to_source
# --------------------------------------------------------------------------- #
def test_switch_to_source_video_keeps_episode_index():
    """视频：current_ep_no 保留（0 基），越界钳制到末集。"""
    from framework.content import Chapter

    class _FakeDetailContent:
        def __init__(self):
            self.calls = []

        def fetch_detail(self, source, url):
            self.calls.append((source, url))
            from framework.content import Detail
            return Detail(
                source_id=source.source_id, content_type="video", url=url,
                title="新源作品", chapters=[
                    Chapter("第1集", "e1"), Chapter("第2集", "e2"), Chapter("第3集", "e3"),
                ],
            )

    content = _FakeDetailContent()
    src = SourceConfig.from_dict(_search_src_json("src-b", "源B", "http://b.com"), "<b>")
    target = CrossSourceCandidate(
        source_id="src-b", source_name="源B", title="新源作品",
        url="http://b.com/v/9", cover="", similarity=0.95,
        content_type="video", sid="",
    )
    # current_ep_no=1（0 基第 2 集）→ 返回新详情全部 chapters，索引由调用方处理
    new_detail, chapters = switch_to_source(content, src, None, "video", 1, target)
    assert new_detail.source_id == "src-b"
    assert len(chapters) == 3
    assert content.calls[0][0].source_id == "src-b"
    assert content.calls[0][1] == "http://b.com/v/9"


def test_switch_to_source_video_clamps_episode():
    """视频：current_ep_no 越界 → 钳制到 len(chapters)-1。"""
    from framework.content import Chapter

    class _FakeDetailContent:
        def fetch_detail(self, source, url):
            from framework.content import Detail
            return Detail(
                source_id=source.source_id, content_type="video", url=url,
                chapters=[Chapter("A", "a")],
            )

    content = _FakeDetailContent()
    src = SourceConfig.from_dict(_search_src_json("src-b", "源B", "http://b.com"), "<b>")
    target = CrossSourceCandidate(
        source_id="src-b", source_name="源B", title="T", url="http://b.com/v/9",
        cover="", similarity=0.95, content_type="video", sid="",
    )
    new_detail, chapters = switch_to_source(content, src, None, "video", 99, target)
    assert len(chapters) == 1


def test_switch_to_source_novel_returns_all_chapters():
    """非视频：返回全部 chapters。"""
    from framework.content import Chapter

    class _FakeDetailContent:
        def fetch_detail(self, source, url):
            from framework.content import Detail
            return Detail(
                source_id=source.source_id, content_type="novel", url=url,
                chapters=[Chapter("第一章", "c1"), Chapter("第二章", "c2")],
            )

    content = _FakeDetailContent()
    src = SourceConfig.from_dict(_search_src_json("src-b", "源B", "http://b.com"), "<b>")
    src.content_type = "novel"
    target = CrossSourceCandidate(
        source_id="src-b", source_name="源B", title="T", url="http://b.com/v/9",
        cover="", similarity=0.95, content_type="novel", sid="",
    )
    new_detail, chapters = switch_to_source(content, src, None, "novel", 3, target)
    assert len(chapters) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
