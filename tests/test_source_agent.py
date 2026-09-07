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

    # ------------------------------------------------------------------ #
    # $id 路径穿越防护（Critical 1）
    # ------------------------------------------------------------------ #
    def test_config_rejects_invalid_source_id(self):
        """SourceConfig.from_dict 拒绝非法 $id（防路径穿越的第一道闸）。"""
        from framework.config import SourceConfig
        from framework.errors import ConfigError

        draft = _make_valid_draft()
        draft["$id"] = "../data/llm_keys"
        with pytest.raises(ConfigError):
            SourceConfig.from_dict(draft)
        draft["$id"] = "safe_id"
        assert SourceConfig.from_dict(draft).source_id == "safe_id"

    def test_invalid_source_id_not_written(self, tmp_path):
        """LLM 提示注入 $id=../data/llm_keys → 拒绝保存，不产生越界文件。"""
        draft = _make_valid_draft("../data/llm_keys", "novel")
        llm = _FakeLlm([json.dumps(draft, ensure_ascii=False)])
        http = _FakeHttp("<html><body>test</body></html>")
        preview = _FakePreview(search_results=[MagicMock(url="/v/1.html", title="Test")])

        agent = SourceAgent(llm=llm, http=http, preview=preview, max_attempts=3)
        sources_dir = tmp_path / "sources"
        drafts_dir = tmp_path / "drafts"
        agent._sources_dir = lambda: sources_dir
        agent._drafts_dir = lambda: drafts_dir

        result = agent.make_source("https://example.com", "novel")

        assert result.ok is False
        assert result.draft is not None
        assert result.draft["$id"] == "../data/llm_keys"
        # 拒绝发生在落盘之前：sources/drafts 目录都不应被创建
        assert not sources_dir.exists()
        assert not drafts_dir.exists()
        # 产物严格限制在 agent 配置的 sources/drafts 目录内，不得越界写入
        for p in tmp_path.rglob("*"):
            rel = p.relative_to(tmp_path).as_posix()
            assert rel.startswith(("sources/", "drafts/")), f"越界产物: {rel}"

    def test_save_source_rejects_invalid_id(self, tmp_path):
        """_save_source 拒绝非法 $id：抛 ValueError，不落任何文件。"""
        agent = SourceAgent(llm=MagicMock(), http=MagicMock(), max_attempts=1)
        sources_dir = tmp_path / "sources"
        agent._sources_dir = lambda: sources_dir
        draft = _make_valid_draft("../data/x", "novel")
        with pytest.raises(ValueError):
            agent._save_source(draft)
        assert not sources_dir.exists()

    def test_save_draft_invalid_id_sanitized(self, tmp_path):
        """草稿保存用非法 $id → 哈希兜底文件名，绝不逃逸 drafts 目录。"""
        agent = SourceAgent(llm=MagicMock(), http=MagicMock(), max_attempts=1)
        drafts_dir = tmp_path / "drafts"
        agent._drafts_dir = lambda: drafts_dir
        draft = _make_valid_draft("../data/x", "novel")
        path = Path(agent._save_draft(draft))
        assert path.parent == drafts_dir
        assert path.resolve().parent == drafts_dir.resolve()
        assert path.name.startswith("draft_")
        assert path.exists()
        assert not (tmp_path / "data").exists()
        assert not (tmp_path / "x.json").exists()

    # ------------------------------------------------------------------ #
    # 保存失败降级（Critical 4：不让写盘异常逃逸挂死 AgentDialog worker）
    # ------------------------------------------------------------------ #
    def test_save_failure_degrades_to_failure_result(self, tmp_path):
        """Phase 4 源保存抛错（磁盘/权限）→ 降级草稿 + 返回失败结果，不逃逸。"""
        draft = _make_valid_draft("writable_src", "novel")
        llm = _FakeLlm([json.dumps(draft, ensure_ascii=False)])
        http = _FakeHttp("<html><body>test</body></html>")
        preview = _FakePreview(search_results=[MagicMock(url="/v/1.html", title="Test")])

        agent = SourceAgent(llm=llm, http=http, preview=preview, max_attempts=1)
        saved_drafts = []

        def _boom(d):
            raise OSError("disk full")

        agent._save_source = _boom
        agent._save_draft = lambda d: (saved_drafts.append(d) or str(tmp_path / "draft.json"))

        result = agent.make_source("https://example.com", "novel")

        # 流程完成（未挂起），返回失败结果 + 内存草稿
        assert result.ok is False
        assert result.draft is not None
        assert any("保存失败" in m for m in result.logs)
        assert saved_drafts, "源保存失败后应降级尝试保存草稿"
        assert any("草稿" in s for s in result.suggestions)

    def test_draft_save_failure_still_returns_result(self, tmp_path):
        """Phase 3 草稿保存抛错 → 仍返回失败结果与内存草稿。"""
        bad_draft = {"$id": "fail_draft_src", "$type": "novel", "$name": "Test",
                     "transports": {"base_url": "https://example.com"}}
        llm = _FakeLlm([
            json.dumps(bad_draft, ensure_ascii=False),
            json.dumps(bad_draft, ensure_ascii=False),
        ])
        http = _FakeHttp("<html><body>test</body></html>")
        preview = _FakePreview(search_results=[])

        agent = SourceAgent(llm=llm, http=http, preview=preview, max_attempts=2)
        agent._save_source = lambda d: str(tmp_path / "x.json")
        agent._save_draft = MagicMock(side_effect=OSError("disk full"))

        result = agent.make_source("https://example.com", "novel")

        assert result.ok is False
        assert result.attempts == 2
        assert result.draft is not None
        assert any("草稿保存失败" in m for m in result.logs)

    # ------------------------------------------------------------------ #
    # 空搜索结果 = 验证失败（Critical 5）
    # ------------------------------------------------------------------ #
    def test_validation_fails_on_empty_search(self, tmp_path):
        """搜索空结果 → 每轮验证失败并回喂 LLM 修正（修复前空结果不算失败）。"""
        draft = _make_valid_draft("empty_src", "novel")
        llm = _FakeLlm([
            json.dumps(draft, ensure_ascii=False),
            json.dumps(draft, ensure_ascii=False),
        ])
        http = _FakeHttp("<html><body>test</body></html>")
        preview = _FakePreview(search_results=[])  # 搜索无结果

        agent = SourceAgent(llm=llm, http=http, preview=preview, max_attempts=2)
        agent._save_source = lambda d: str(tmp_path / "x.json")
        agent._save_draft = lambda d: str(tmp_path / "drafts/x.json")

        result = agent.make_source("https://example.com", "novel")

        assert result.ok is False
        assert result.attempts == 2          # 两轮都因空搜索失败
        assert len(llm._calls) == 2          # 修正调用确实发生（失败回喂 LLM）
        assert any("搜索" in s for s in result.suggestions)

    def test_validate_searches_once_per_round(self):
        """每轮验证只调一次 preview_search（结果复用给结果数与详情 URL 校验）。"""
        draft = _make_valid_draft("once_src", "novel")
        llm = _FakeLlm([json.dumps(draft, ensure_ascii=False)])
        http = _FakeHttp("<html><body>test</body></html>")
        preview = _FakePreview(search_results=[MagicMock(url="/v/1.html", title="Test")])

        agent = SourceAgent(llm=llm, http=http, preview=preview, max_attempts=1)
        agent._save_source = lambda d: "saved.json"
        agent._save_draft = lambda d: "draft.json"

        result = agent.make_source("https://example.com", "novel")

        assert result.ok is True
        assert len(preview._search_calls) == 1  # 一轮只搜一次（修复前同轮搜两次）
        assert len(preview._detail_calls) == 1


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
