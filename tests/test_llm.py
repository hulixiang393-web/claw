# -*- coding: utf-8 -*-
"""LLM 客户端层测试（test_llm.py）。

测试 LlmClient / LlmKeyStore / OllamaManager，使用 fake HttpClient。
不引入 openai SDK / pytest-qt 等外部依赖。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.llm import LlmClient, LlmError, LlmKeyStore


# ====================================================================== #
# LlmClient 测试
# ====================================================================== #

class _FakeHttpResponse:
    """模拟 post_json 返回的 dict。"""
    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]


class _FakeHttpForLlm:
    """模拟 HttpClient：post_json / get_json 返回预设响应。"""

    def __init__(self, response_data: dict, error: Exception | None = None):
        self._response = response_data
        self._error = error
        self._last_url = ""
        self._last_body = None
        from framework.http import NetworkDefaults
        self.defaults = NetworkDefaults()

    def post_json(self, url, json_body=None, headers=None, **kwargs):
        self._last_url = url
        self._last_body = json_body
        if self._error:
            raise self._error
        return self._response

    def get_json(self, url, **kwargs):
        self._last_url = url
        if self._error:
            raise self._error
        return self._response

    def get_text(self, url, **kwargs):
        self._last_url = url
        if self._error:
            raise self._error
        return "<html>hello</html>"

    def close(self):
        pass


def test_llm_client_base_url_normalization():
    """base_url 不以 /v1 结尾时自动追加 /v1。"""
    c = LlmClient("http://127.0.0.1:11434", model="qwen")
    assert c.base_url == "http://127.0.0.1:11434/v1"
    assert c.raw_base == "http://127.0.0.1:11434"


def test_llm_client_base_url_already_v1():
    """base_url 已以 /v1 结尾时不重复追加。"""
    c = LlmClient("http://example.com/v1", api_key="sk-test", model="gpt-4")
    assert c.base_url == "http://example.com/v1"
    assert c.raw_base == "http://example.com/v1"


def test_llm_client_chat_success():
    """正常 chat 响应 → 返回 content 文本。"""
    fake = _FakeHttpForLlm({
        "choices": [{"message": {"role": "assistant", "content": "Hello!"}}]
    })
    with patch("framework.llm.HttpClient", return_value=fake):
        client = LlmClient("http://test.com", api_key="sk-x", model="m")
        result = client.chat("system", "user msg")
    assert result == "Hello!"
    assert fake._last_url == "http://test.com/v1/chat/completions"
    assert fake._last_body["model"] == "m"
    assert fake._last_body["messages"][0]["role"] == "system"
    assert fake._last_body["messages"][1]["content"] == "user msg"


def test_llm_client_chat_json_mode():
    """json_mode=True 时请求体包含 response_format。"""
    fake = _FakeHttpForLlm({
        "choices": [{"message": {"content": "{}"}}]
    })
    with patch("framework.llm.HttpClient", return_value=fake):
        client = LlmClient("http://test.com", model="m")
        client.chat("sys", "usr", json_mode=True)
    assert fake._last_body["response_format"] == {"type": "json_object"}


def test_llm_client_chat_api_error():
    """API 返回 error 字段 → 抛 LlmError。"""
    fake = _FakeHttpForLlm({
        "error": {"message": "rate limited"}
    })
    with patch("framework.llm.HttpClient", return_value=fake):
        client = LlmClient("http://test.com", model="m")
        with pytest.raises(LlmError, match="rate limited"):
            client.chat("sys", "usr")


def test_llm_client_chat_network_error():
    """网络异常 → 抛 LlmError。"""
    fake = _FakeHttpForLlm({}, error=ConnectionError("timeout"))
    with patch("framework.llm.HttpClient", return_value=fake):
        client = LlmClient("http://test.com", model="m")
        with pytest.raises(LlmError, match="请求失败"):
            client.chat("sys", "usr")


def test_llm_client_chat_no_choices():
    """响应无 choices 字段 → 抛 LlmError。"""
    fake = _FakeHttpForLlm({"data": "no choices"})
    with patch("framework.llm.HttpClient", return_value=fake):
        client = LlmClient("http://test.com", model="m")
        with pytest.raises(LlmError, match="无 choices"):
            client.chat("sys", "usr")


def test_llm_client_chat_empty_messages_no_system():
    """system 为空时只发 user 消息。"""
    fake = _FakeHttpForLlm({
        "choices": [{"message": {"content": "ok"}}]
    })
    with patch("framework.llm.HttpClient", return_value=fake):
        client = LlmClient("http://test.com", model="m")
        result = client.chat("", "hello")
    assert fake._last_body["messages"] == [{"role": "user", "content": "hello"}]


# ====================================================================== #
# LlmKeyStore 测试
# ====================================================================== #

class TestLlmKeyStore:
    """LlmKeyStore 读写 / 文件缺失 / 安全隔离。"""

    def test_load_missing_file(self, tmp_path):
        """文件不存在 → 返回空 dict。"""
        store = LlmKeyStore(tmp_path / "nonexistent.json")
        assert store.load() == {}

    def test_load_corrupted_file(self, tmp_path):
        """损坏 JSON → 返回空 dict（不抛异常）。"""
        p = tmp_path / "bad.json"
        p.write_text("not json {{{", encoding="utf-8")
        store = LlmKeyStore(p)
        assert store.load() == {}

    def test_save_and_load(self, tmp_path):
        """写入后读取一致。"""
        p = tmp_path / "keys.json"
        store = LlmKeyStore(p)
        data = {"cloud": {"api_key": "sk-test", "base_url": "https://api.open.com", "model": "gpt-4"}}
        store.save(data)
        loaded = store.load()
        assert loaded == data

    def test_save_creates_parent_dir(self, tmp_path):
        """save 自动创建父目录。"""
        p = tmp_path / "sub" / "deep" / "keys.json"
        store = LlmKeyStore(p)
        store.save({"cloud": {"api_key": "x"}})
        assert p.exists()

    def test_cloud_returns_empty_when_no_file(self, tmp_path):
        """cloud() 在无文件时返回空 dict。"""
        store = LlmKeyStore(tmp_path / "nope.json")
        assert store.cloud() == {}

    def test_local_returns_default_when_empty(self, tmp_path):
        """local() 无配置时返回默认 base_url。"""
        store = LlmKeyStore(tmp_path / "nope.json")
        local = store.local()
        assert local["base_url"] == "http://127.0.0.1:11434"
        assert local["model"] == ""

    def test_save_local_records_recent(self, tmp_path):
        """save_local 写入 local 段并记录 recent，recent 置顶去重。"""
        p = tmp_path / "keys.json"
        store = LlmKeyStore(p)
        store.save_local(
            base_url="http://127.0.0.1:11434", port=11434,
            server_path="C:/llm/llama-server.exe",
            model_path="D:/models/qwen.gguf", model="qwen",
        )
        store.save_local(
            base_url="http://127.0.0.1:11434", port=11434,
            server_path="C:/llm/llama-server.exe",
            model_path="D:/models/llama3.gguf", model="llama3",
        )
        local = store.local()
        assert local["model_path"] == "D:/models/llama3.gguf"
        # recent 新到置顶
        assert len(local["recent"]) == 2
        assert local["recent"][0]["model_path"] == "D:/models/llama3.gguf"
        # 再次选择旧模型 → 不重复、置顶
        store.save_local(
            base_url="http://127.0.0.1:11434", port=11434,
            server_path="C:/llm/llama-server.exe",
            model_path="D:/models/qwen.gguf", model="qwen",
        )
        local = store.local()
        assert len(local["recent"]) == 2
        assert local["recent"][0]["model_path"] == "D:/models/qwen.gguf"

    def test_save_local_preserves_cloud(self, tmp_path):
        """save_local 不破坏 cloud 段（Key 隔离）。"""
        p = tmp_path / "keys.json"
        store = LlmKeyStore(p)
        store.save({"cloud": {"api_key": "sk-secret", "base_url": "https://x/v1", "model": "gpt"}})
        store.save_local(server_path="C:/llm/llama-server.exe", model_path="D:/models/q.gguf")
        assert store.cloud()["api_key"] == "sk-secret"

    def test_save_cloud_preserves_local(self, tmp_path):
        """save_cloud 不破坏 local 段。"""
        p = tmp_path / "keys.json"
        store = LlmKeyStore(p)
        store.save_local(server_path="C:/llm/llama-server.exe", model_path="D:/models/q.gguf")
        store.save_cloud(api_key="sk-new", base_url="https://x/v1", model="gpt")
        assert store.local()["model_path"] == "D:/models/q.gguf"

    def test_cloud_and_local_merge(self, tmp_path):
        """cloud()/local() 正确提取各自段。"""
        p = tmp_path / "keys.json"
        data = {
            "cloud": {"api_key": "sk-abc", "base_url": "https://x.com/v1", "model": "gpt"},
            "local": {"base_url": "http://127.0.0.1:11434", "model": "qwen"},
        }
        LlmKeyStore(p).save(data)
        store = LlmKeyStore(p)
        assert store.cloud()["api_key"] == "sk-abc"
        assert store.local()["model"] == "qwen"

    def test_key_never_in_app_config(self, tmp_path, monkeypatch):
        """API Key 不会写入 app_config.json。

        验证：创建一个临时 app_config.json，调用 LlmKeyStore.save，
        确认 app_config.json 内容不变。
        """
        config_path = tmp_path / "app_config.json"
        config_path.write_text('{"network": {"proxy": null}}', encoding="utf-8")
        original_content = config_path.read_text(encoding="utf-8")

        # 保存 LLM keys 到 data 目录下的 keys 文件
        keys_path = tmp_path / "data" / "llm_keys.json"
        store = LlmKeyStore(keys_path)
        store.save({"cloud": {"api_key": "sk-super-secret"}})

        # app_config.json 不应被修改
        assert config_path.read_text(encoding="utf-8") == original_content
        # keys 文件应包含密钥
        assert "sk-super-secret" in keys_path.read_text(encoding="utf-8")
        # app_config.json 不应包含密钥
        assert "sk-super-secret" not in config_path.read_text(encoding="utf-8")


# ====================================================================== #
# LlmClient.test_connection — 探活 + 打招呼
# ====================================================================== #

def test_connection_success():
    """探活 + chat 都通过 → ok=True，两步都展示。"""
    fake = _FakeHttpForLlm({
        "choices": [{"message": {"content": "你好！"}}]
    })
    with patch("framework.llm.HttpClient", return_value=fake):
        client = LlmClient("http://test.com/v1", api_key="sk-x", model="m")
        result = client.test_connection()
    assert result["ok"] is True
    assert len(result["steps"]) == 2
    assert result["steps"][0]["ok"] is True
    assert result["steps"][1]["ok"] is True
    assert fake._last_url == "http://test.com/v1/chat/completions"


def test_connection_network_failure():
    """探活即失败 → ok=False，reason 为网络问题。"""
    fake = _FakeHttpForLlm({}, error=ConnectionError("timed out"))
    with patch("framework.llm.HttpClient", return_value=fake):
        client = LlmClient("http://test.com/v1", api_key="sk-x", model="m")
        result = client.test_connection()
    assert result["ok"] is False
    assert result["reason"] == "网络连接失败"
    assert result["steps"][0]["ok"] is False
    assert "超时" in result["steps"][0]["detail"]


def test_connection_chat_api_key_error():
    """探活通过但 chat 401 → 归类为 API Key 无效。"""
    fake = _FakeHttpForLlm({})
    with patch("framework.llm.HttpClient", return_value=fake):
        client = LlmClient("http://test.com/v1", api_key="sk-bad", model="m")
        # chat 内部抛 LlmError 需在 chat 调用路径模拟 —— 用 monkeypatch client.chat
        client.chat = lambda *a, **k: (_ for _ in ()).throw(
            LlmError("LLM API 错误：Incorrect API key provided (HTTP 401)")
        )
        result = client.test_connection()
    assert result["ok"] is False
    assert "API Key" in result["reason"]


def test_connection_chat_model_not_found():
    """chat 404 模型不存在 → 归类为模型类型问题。"""
    fake = _FakeHttpForLlm({})
    with patch("framework.llm.HttpClient", return_value=fake):
        client = LlmClient("http://test.com/v1", model="bad-model")
        client.chat = lambda *a, **k: (_ for _ in ()).throw(
            LlmError("LLM API 错误：model not found (HTTP 404)")
        )
        result = client.test_connection()
    assert result["ok"] is False
    assert "模型不存在" in result["reason"]


# ====================================================================== #
# 提示词模板：用户模板优先 + 恢复默认
# ====================================================================== #

def test_effective_template_builtin_when_no_user(tmp_path, monkeypatch):
    """无用户模板 → effective_template_path 指向内置。"""
    import framework.llm as llm_mod

    monkeypatch.setattr(llm_mod, "_USER_TEMPLATE", str(tmp_path / "llm" / "source_builder.txt"))
    monkeypatch.setattr(llm_mod, "_BUILTIN_TEMPLATE", str(tmp_path / "builtin.txt"))
    (tmp_path / "builtin.txt").write_text("内置提示词", encoding="utf-8")
    assert llm_mod.effective_template_path() == str(tmp_path / "builtin.txt")
    assert llm_mod.load_effective_template() == "内置提示词"


def test_effective_template_user_overrides(tmp_path, monkeypatch):
    """用户模板存在 → 生效为用户模板。"""
    import framework.llm as llm_mod

    user = tmp_path / "llm" / "source_builder.txt"
    user.parent.mkdir(parents=True)
    user.write_text("用户定制提示词", encoding="utf-8")
    monkeypatch.setattr(llm_mod, "_USER_TEMPLATE", str(user))
    monkeypatch.setattr(llm_mod, "_BUILTIN_TEMPLATE", str(tmp_path / "builtin.txt"))
    (tmp_path / "builtin.txt").write_text("内置提示词", encoding="utf-8")
    assert llm_mod.effective_template_path() == str(user)
    assert llm_mod.load_effective_template() == "用户定制提示词"


def test_reset_user_template_deletes(tmp_path, monkeypatch):
    """reset_user_template 删除用户模板并回退内置。"""
    import framework.llm as llm_mod

    user = tmp_path / "llm" / "source_builder.txt"
    user.parent.mkdir(parents=True)
    user.write_text("xxx", encoding="utf-8")
    monkeypatch.setattr(llm_mod, "_USER_TEMPLATE", str(user))
    monkeypatch.setattr(llm_mod, "_BUILTIN_TEMPLATE", str(tmp_path / "builtin.txt"))
    (tmp_path / "builtin.txt").write_text("内置提示词", encoding="utf-8")
    text = llm_mod.reset_user_template()
    assert text == "内置提示词"
    assert not user.exists()


# ====================================================================== #
# LlamaManager 测试见 tests/test_llama.py
# ======================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
