# -*- coding: utf-8 -*-
"""Agent 自动制源测试（test_source_agent.py）。

测试 SourceAgent 全流程：LLM 生成 + 验证链 + 重试 + 草稿保存。
使用 fake LLM / fake HTTP / fake Preview，不引入外部依赖。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.source_agent import SourceAgent, AgentResult, _extract_json_from_llm, _detect_page_tech, _sample_html


# ====================================================================== #
# 辅助类
# ====================================================================== #

class _FakeLlm:
    """按序列返回预设回复。"""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self._calls = []

    def chat(self, system, user, json_mode=False, timeout=60.0):
        self._calls.append({"system": system, "user": user})
        if not self._responses:
            raise RuntimeError("No more LLM responses")
        return self._responses.pop(0)


class _FakeHttp:
    """按预设返回 HTML。"""

    def __init__(self, html: str = "<html><body><h1>Test</h1></body></html>"):
        self._html = html
        self._calls = []

    def get_text(self, url, **kwargs):
        self._calls.append(url)
        return self._html

    def close(self):
        pass


class _FakePreview:
    """预览验证：返回预设搜索/详情结果。"""

    def __init__(self, search_results=None, detail_result=None):
        self._search_results = search_results or []
        self._detail_result = detail_result or {"title": "Test Title", "author": "Author"}
        self._search_calls = []
        self._detail_calls = []

    def preview_search(self, source, keyword):
        self._search_calls.append(keyword)
        return self._search_results

    def preview_detail(self, source, url):
        self._detail_calls.append(url)
        return self._detail_result


def _make_valid_draft(source_id: str = "test_source", content_type: str = "novel") -> dict:
    """生成一个合法的源配置草稿。"""
    content = {}
    if content_type == "novel":
        content = {"chapter": {"root_selector": {"css": ".chapter-list"}, "fields": {"title": {"css": "a"}, "url": {"css": "a", "attr": "href"}}}}
    elif content_type == "comic":
        content = {"page": {"root_selector": {"css": ".page-list"}, "fields": {"url": {"css": "img", "attr": "src"}}}}
    elif content_type == "video":
        content = {"episode": {"root_selector": {"css": ".ep-list"}, "fields": {"title": {"css": "a"}, "url": {"css": "a", "attr": "href"}}}}

    return {
        "$id": source_id,
        "$type": content_type,
        "$name": "Test Source",
        "$enabled": True,
        "$weight": 1.0,
        "$metadata": {"homepage": "https://example.com"},
        "transports": {"base_url": "https://example.com"},
        "content": content,
    }


# ====================================================================== #
# 辅助函数测试
# ====================================================================== #

class TestExtractJson:
    def test_json_code_block(self):
        text = 'Some text\n```json\n{"key": "value"}\n```\nMore text'
        assert _extract_json_from_llm(text) == {"key": "value"}

    def test_json_code_block_no_lang(self):
        text = '```\n{"a": 1}\n```'
        assert _extract_json_from_llm(text) == {"a": 1}

    def test_bare_json(self):
        text = '{"id": "test"}'
        assert _extract_json_from_llm(text) == {"id": "test"}

    def test_no_json(self):
        assert _extract_json_from_llm("no json here") is None

    def test_invalid_json_code_block(self):
        text = '```json\n{bad json}\n```'
        assert _extract_json_from_llm(text) is None


class TestDetectPageTech:
    def test_ssr(self):
        text_content = "A" * 250
        html = f"<html><body><div><a href='/list'>{text_content}</a></div></body></html>"
        assert _detect_page_tech(html) == "ssr"

    def test_api_json(self):
        assert _detect_page_tech('{"data": []}') == "api"

    def test_spa(self):
        html = '<html><body><div id="app"></div></body></html>'
        assert _detect_page_tech(html) == "spa"

    def test_spa_short_content(self):
        html = "<html><body><p>Hi</p></body></html>"
        assert _detect_page_tech(html) == "spa"


class TestSampleHtml:
    def test_short_html_not_truncated(self):
        html = "<html><body>short</body></html>"
        assert _sample_html(html, max_len=100) == html

    def test_long_html_truncated(self):
        html = "<html><head><meta></head><body>" + "x" * 10000 + "</body></html>"
        result = _sample_html(html, max_len=500)
        assert len(result) <= 500


# ====================================================================== #
# SourceAgent 测试
# ====================================================================== #

class TestSourceAgent:
    """SourceAgent 全流程测试。"""

    def test_success_first_attempt(self, tmp_path):
        """首次验证即通过 → 成功保存源。"""
        draft = _make_valid_draft("success_src", "novel")
        llm = _FakeLlm([json.dumps(draft, ensure_ascii=False)])
        http = _FakeHttp("<html><body>content test</body></html>")
        preview = _FakePreview(
            search_results=[MagicMock(url="/v/1.html", title="Test")],
        )

        agent = SourceAgent(llm=llm, http=http, preview=preview, max_attempts=5)
        saved = {}
        def _fake_save(d):
            p = str(tmp_path / f"{d.get('$id', 'x')}.json")
            Path(p).write_text(json.dumps(d), encoding="utf-8")
            saved["path"] = p
            return p
        agent._save_source = _fake_save
        agent._save_draft = lambda d: str(tmp_path / "drafts" / f"{d.get('$id', 'x')}.json")

        result = agent.make_source("https://example.com", "novel")

        assert result.ok is True
        assert result.source_id == "success_src"
        assert result.attempts >= 1
        # 源文件应已保存
        assert Path(saved["path"]).exists()

    def test_retry_then_success(self, tmp_path):
        """首次验证失败，修正后通过。"""
        good_draft = _make_valid_draft("retry_src", "novel")
        # 第一次 LLM 返回缺少 content 的草稿
        bad_draft = {"$id": "retry_src", "$type": "novel", "$name": "Test", "transports": {"base_url": "https://example.com"}}
        llm = _FakeLlm([
            json.dumps(bad_draft, ensure_ascii=False),  # 第一次生成（缺少 chapter）
            json.dumps(good_draft, ensure_ascii=False),  # 修正后
        ])
        http = _FakeHttp("<html><body>test</body></html>")
        preview = _FakePreview(
            search_results=[MagicMock(url="/v/1.html", title="Test")],
        )

        agent = SourceAgent(llm=llm, http=http, preview=preview, max_attempts=5)
        agent._save_source = lambda d: str(tmp_path / f"{d.get('$id', 'x')}.json")
        agent._save_draft = lambda d: str(tmp_path / "drafts" / f"{d.get('$id', 'x')}.json")

        result = agent.make_source("https://example.com", "novel")

        assert result.ok is True
        assert result.source_id == "retry_src"

    def test_all_attempts_fail_saves_draft(self, tmp_path):
        """全部轮次失败 → 保存草稿 + 建议。"""
        bad_draft = {"$id": "fail_src", "$type": "novel", "$name": "Test", "transports": {"base_url": "https://example.com"}}
        llm = _FakeLlm([
            json.dumps(bad_draft, ensure_ascii=False),
            json.dumps(bad_draft, ensure_ascii=False),  # 修正仍然缺少 chapter
        ])
        http = _FakeHttp("<html><body>test</body></html>")
        preview = _FakePreview(search_results=[])

        agent = SourceAgent(llm=llm, http=http, preview=preview, max_attempts=2)
        agent._save_source = lambda d: str(tmp_path / f"{d.get('$id', 'x')}.json")
        agent._save_draft = lambda d: str(tmp_path / "drafts" / f"{d.get('$id', 'x')}.json")

        result = agent.make_source("https://example.com", "novel")

        assert result.ok is False
        assert result.draft is not None
        assert result.attempts == 2
        assert len(result.suggestions) > 0

    def test_site_fetch_failure(self):
        """站点抓取失败 → 返回建议。"""
        llm = _FakeLlm([])
        http = _FakeHttp()
        http.get_text = MagicMock(side_effect=ConnectionError("timeout"))

        agent = SourceAgent(llm=llm, http=http, max_attempts=3)
        result = agent.make_source("https://down.example.com", "video")

        assert result.ok is False
        assert any("JS 渲染" in s or "抓取失败" in str(result.logs) for s in result.suggestions)

    def test_llm_returns_invalid_json(self):
        """LLM 返回非 JSON → 失败。"""
        llm = _FakeLlm(["This is not JSON at all"])
        http = _FakeHttp("<html><body>test</body></html>")

        agent = SourceAgent(llm=llm, http=http, max_attempts=3)
        result = agent.make_source("https://example.com", "novel")

        assert result.ok is False
        assert any("无法解析" in log for log in result.logs)

    def test_cancel(self, tmp_path):
        """取消操作 → 中止流程。"""
        draft = _make_valid_draft("cancel_src", "novel")
        llm = _FakeLlm([json.dumps(draft, ensure_ascii=False)])
        http = _FakeHttp("<html><body>test</body></html>")
        preview = _FakePreview(
            search_results=[MagicMock(url="/v/1.html", title="Test")],
        )

        agent = SourceAgent(llm=llm, http=http, preview=preview, max_attempts=5)
        agent._save_source = lambda d: str(tmp_path / "x.json")
        agent._save_draft = lambda d: str(tmp_path / "drafts/x.json")

        # 取消在 make_source 内部（Phase 2 后、Phase 3 前）
        def _cancel_after_generate(*a, **kw):
            agent._cancelled = True
            return agent._llm.chat(*a, **kw)

        # 第一次调用触发取消
        original_chat = agent._llm.chat
        call_count = [0]
        def _chat_side_effect(*a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                # 第一次调用（Phase 2 生成）完成后取消
                result = original_chat(*a, **kw)
                agent._cancelled = True
                return result
            return original_chat(*a, **kw)
        agent._llm.chat = _chat_side_effect

        result = agent.make_source("https://example.com", "novel")
        assert result.ok is False

    def test_on_log_callback(self):
        """on_log 回调被调用。"""
        llm = _FakeLlm(["not json"])
        http = _FakeHttp("<html><body>test</body></html>")
        logs_received = []

        agent = SourceAgent(llm=llm, http=http, max_attempts=3)
        agent.make_source("https://example.com", "novel", on_log=lambda m: logs_received.append(m))

        assert len(logs_received) > 0
        assert any("Phase 1" in m for m in logs_received)

    def test_content_type_video(self, tmp_path):
        """video 类型验证正确。"""
        draft = _make_valid_draft("video_src", "video")
        llm = _FakeLlm([json.dumps(draft, ensure_ascii=False)])
        http = _FakeHttp("<html><body>test</body></html>")
        preview = _FakePreview(
            search_results=[MagicMock(url="/v/1.html", title="Test")],
        )

        agent = SourceAgent(llm=llm, http=http, preview=preview, max_attempts=1)
        agent._save_source = lambda d: str(tmp_path / f"{d.get('$id', 'x')}.json")
        agent._save_draft = lambda d: str(tmp_path / "drafts/x.json")

        result = agent.make_source("https://example.com", "video")
        assert result.ok is True
        assert result.source_id == "video_src"

    def test_category_passed_to_llm(self):
        """category 参数传递给 LLM。"""
        draft = _make_valid_draft()
        llm = _FakeLlm([json.dumps(draft, ensure_ascii=False)])
        http = _FakeHttp("<html><body>test</body></html>")

        agent = SourceAgent(llm=llm, http=http, max_attempts=1)
        agent._save_source = lambda d: "x.json"
        agent._save_draft = lambda d: "x.json"

        agent.make_source("https://example.com", "novel", category="玄幻")
        # LLM 被调用，user 消息包含类别信息
        assert len(llm._calls) == 1
        assert "玄幻" in llm._calls[0]["user"]


# ====================================================================== #
# AgentResult 测试
# ====================================================================== #

class TestAgentResult:
    def test_default_values(self):
        r = AgentResult(ok=True)
        assert r.source_id == ""
        assert r.draft is None
        assert r.attempts == 0
        assert r.logs == []
        assert r.suggestions == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
