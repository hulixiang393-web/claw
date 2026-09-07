# -*- coding: utf-8 -*-
"""提示词模板测试（test_prompt_template.py）。

验证 prompts/source_builder.txt：
- 文件存在且非空
- 必需小节齐全（角色设定 / 类型字段矩阵 / 选择器规范 / URL 占位符 / 反爬 / 输出强约束）
- 四个模板变量 {site_url} {content_type} {html_sample} {last_error}
- SourceAgent 填充变量后不留未替换占位符
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.source_agent import SourceAgent

TEMPLATE = Path(__file__).resolve().parents[1] / "prompts" / "source_builder.txt"

# SourceAgent 需要替换的四个模板变量（与实现约定一致）
TEMPLATE_VARS = (
    "{site_url}",
    "{content_type}",
    "{html_sample}",
    "{last_error}",
)


def _content() -> str:
    assert TEMPLATE.exists(), f"模板缺失：{TEMPLATE}"
    return TEMPLATE.read_text(encoding="utf-8")


class TestTemplateExists:
    def test_file_exists_and_nonempty(self):
        assert TEMPLATE.exists()
        text = TEMPLATE.read_text(encoding="utf-8")
        assert len(text.strip()) > 200

    def test_all_template_vars_present(self):
        text = _content()
        for var in TEMPLATE_VARS:
            assert var in text, f"模板缺少变量 {var}"


class TestTemplateSections:
    """spec §18 / brief B3 要求的必需小节。"""

    def test_role_setting(self):
        text = _content()
        assert "爬虫配置工程师" in text or "角色" in text

    def test_type_field_matrix(self):
        text = _content()
        assert "content.chapter" in text          # novel
        assert "content.page" in text             # comic
        assert "content.episode" in text          # video
        assert "media" in text                    # video 媒体
        assert "novel" in text and "comic" in text and "video" in text

    def test_selector_rules(self):
        text = _content()
        assert "attr" in text                     # 取属性
        assert "fallback" in text                 # 多候选
        assert "href" in text and "src" in text   # 链接/图片

    def test_url_placeholders(self):
        text = _content()
        for ph in ("{page}", "{cat}", "{keyword}", "{id}"):
            assert ph in text, f"缺占位符说明 {ph}"

    def test_anti_scrape_notes(self):
        text = _content()
        assert "UA" in text or "User-Agent" in text
        assert "render" in text
        assert "auth" in text

    def test_output_strict_format(self):
        text = _content()
        assert "```json" in text                  # 单个 json 代码块
        assert "$id" in text
        assert "唯一" in text                       # $id 唯一

    def test_correction_format(self):
        text = _content()
        assert "修正" in text and "完整 JSON" in text


class _FakeLlm:
    """捕获 chat 调用（返回不可解析文本即可触发 Phase 2 失败，不影响判断）。"""

    def __init__(self):
        self.calls = []

    def chat(self, system, user, json_mode=False, timeout=60.0, max_tokens=None, ctx_limit=None):
        self.calls.append({"system": system, "user": user})
        return "not a json"


class _FakeHttp:
    def __init__(self, html):
        self._html = html

    def get_text(self, url, **kwargs):
        return self._html

    def close(self):
        pass


class TestTemplateFilling:
    """SourceAgent 用模板生成 user 消息后，四个变量都被替换。"""

    def test_variables_are_replaced(self):
        llm = _FakeLlm()
        agent = SourceAgent(
            llm=llm,
            http=_FakeHttp("<html><body><h1>首页标题内容正文</h1></body></html>"),
            max_attempts=1,
        )
        agent.make_source("https://mysite.example/", "novel")

        assert len(llm.calls) == 1
        user = llm.calls[0]["user"]
        # 四个模板变量都被替换（不残留）
        for var in TEMPLATE_VARS:
            assert var not in user, f"变量 {var} 未被替换"
        # 替换值正确落入
        assert "https://mysite.example/" in user
        assert "novel" in user
        assert "<h1>" in user


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])