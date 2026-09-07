# -*- coding: utf-8 -*-
"""Ollama 管理测试（test_ollama.py）。

覆盖 OllamaManager：运行探测 / 模型列表 / 一键启动。
使用 fake HttpClient，不引入外部依赖。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.llm import OllamaManager


class _FakeHttp:
    """模拟 HttpClient：get_json 返回预设响应或抛错。"""

    def __init__(self, data=None, error=None):
        self._data = data
        self._error = error
        self.urls = []

    def get_json(self, url, **kwargs):
        self.urls.append(url)
        if self._error:
            raise self._error
        return self._data

    def close(self):
        pass


class TestOllamaManager:
    """OllamaManager 运行探测 / 模型列表 / 启动。"""

    def test_running_true_with_models(self):
        """API 返回 models → running=True。"""
        fake = _FakeHttp({"models": [{"name": "qwen"}]})
        mgr = OllamaManager("http://127.0.0.1:11434")
        with patch("framework.llm.HttpClient", return_value=fake):
            assert mgr.running() is True

    def test_running_true_empty_dict(self):
        """API 返回 {}（连通无 models 键）→ running=True。"""
        fake = _FakeHttp({})
        mgr = OllamaManager("http://127.0.0.1:11434")
        with patch("framework.llm.HttpClient", return_value=fake):
            assert mgr.running() is True

    def test_running_false_on_error(self):
        """API 异常 → running=False。"""
        fake = _FakeHttp({}, error=ConnectionError("refused"))
        mgr = OllamaManager()
        with patch("framework.llm.HttpClient", return_value=fake):
            assert mgr.running() is False

    def test_running_false_on_non_dict(self):
        """响应非 dict（如 404 文本/列表）→ running=False。"""
        fake = _FakeHttp(["not", "a", "dict"])
        mgr = OllamaManager()
        with patch("framework.llm.HttpClient", return_value=fake):
            assert mgr.running() is False

    def test_models_list(self):
        """返回模型名列表。"""
        fake = _FakeHttp({"models": [{"name": "qwen:latest"}, {"name": "llama3"}]})
        mgr = OllamaManager()
        with patch("framework.llm.HttpClient", return_value=fake):
            assert mgr.models() == ["qwen:latest", "llama3"]

    def test_models_empty_on_error(self):
        """网络异常 → 返回空列表。"""
        fake = _FakeHttp({}, error=RuntimeError("timeout"))
        mgr = OllamaManager()
        with patch("framework.llm.HttpClient", return_value=fake):
            assert mgr.models() == []

    def test_models_empty_when_no_key(self):
        """响应无 models 键 → 返回空列表。"""
        fake = _FakeHttp({})
        mgr = OllamaManager()
        with patch("framework.llm.HttpClient", return_value=fake):
            assert mgr.models() == []

    def test_start_already_running(self):
        """已在运行 → 直接返回 True，不启动。"""
        mgr = OllamaManager()
        with patch.object(mgr, "running", return_value=True):
            assert mgr.start() is True

    def test_start_not_installed(self):
        """ollama 未安装（shutil.which 返回 None）→ 返回 False。"""
        mgr = OllamaManager()
        with patch.object(mgr, "running", return_value=False):
            with patch("framework.llm.shutil.which", return_value=None):
                assert mgr.start() is False

    def test_start_search_executable_uses_base_without_v1(self):
        """探测/启动使用原始地址（不带 /v1）。"""
        fake = _FakeHttp({"models": []})
        mgr = OllamaManager("http://127.0.0.1:11434/v1")
        with patch("framework.llm.HttpClient", return_value=fake):
            assert mgr.running() is True
            assert fake.urls[0] == "http://127.0.0.1:11434/v1/api/tags"
            assert mgr.base_url == "http://127.0.0.1:11434/v1"

    def test_start_success(self):
        """启动后探测成功 → 返回 True。"""
        mgr = OllamaManager()
        with patch.object(mgr, "running", side_effect=[False, True]):
            with patch("framework.llm.shutil.which", return_value="C:/ollama/ollama.exe"):
                with patch("framework.llm.subprocess.Popen"):
                    with patch("framework.llm.time.sleep"):
                        assert mgr.start() is True

    def test_start_not_installed_popen_fallback(self):
        """兼容兜底：无 which 前置拦截时 Popen FileNotFoundError → False。"""
        mgr = OllamaManager()
        with patch.object(mgr, "running", side_effect=[False, False]):
            with patch("framework.llm.shutil.which", return_value="C:/ollama/ollama.exe"):
                with patch("framework.llm.subprocess.Popen", side_effect=FileNotFoundError):
                    assert mgr.start() is False


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])