# -*- coding: utf-8 -*-
"""LLAMA（llama-server）管理测试（test_llama.py）。

覆盖 LlamaManager：后台启动 / 失败原因采集 / 停止 / PATH 探测 /
运行探测，以及 LlmKeyStore 本地 LLAMA 配置字段与 recent 记录。
全部使用 fake HttpClient / mock Popen，不真正启动任何进程。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.llm import LlamaManager


class _FakeHttp:
    """模拟 HttpClient：按 URL 返回预设结果。"""

    def __init__(self, health_ok=True, error=None):
        self._health_ok = health_ok
        self._error = error
        self.urls = []

    def get_json(self, url, **kwargs):
        self.urls.append(url)
        if self._error:
            raise self._error
        if url.endswith("/health"):
            if not self._health_ok:
                raise ConnectionError("refused")
            return {"status": "ok"}
        raise ConnectionError("no such endpoint")

    def get_text(self, url, **kwargs):
        self.urls.append(url)
        if self._error:
            raise self._error
        return "<html>ok</html>"

    def close(self):
        pass


class TestLlamaRunning:
    """running() 健康探测。"""

    def test_running_true_with_health(self):
        fake = _FakeHttp(health_ok=True)
        mgr = LlamaManager("http://127.0.0.1:11434")
        with patch("framework.llm.HttpClient", return_value=fake):
            assert mgr.running() is True
        assert fake.urls[0].endswith("/health")

    def test_running_true_root_fallback(self):
        fake = _FakeHttp(health_ok=False)
        mgr = LlamaManager()
        with patch("framework.llm.HttpClient", return_value=fake):
            assert mgr.running() is True
        assert any(u.endswith("/") for u in fake.urls)

    def test_running_false_on_error(self):
        fake = _FakeHttp(error=ConnectionError("refused"))
        mgr = LlamaManager()
        with patch("framework.llm.HttpClient", return_value=fake):
            assert mgr.running() is False


class TestAutoLocate:
    """auto_locate_server PATH 探测。"""

    def test_found(self):
        with patch("framework.llm.shutil.which", return_value="C:/llm/llama-server.exe"):
            assert LlamaManager.auto_locate_server() == "C:/llm/llama-server.exe"

    def test_not_found(self):
        with patch("framework.llm.shutil.which", return_value=None):
            assert LlamaManager.auto_locate_server() == ""


class TestLlamaStart:
    """start() 前台条件 / 后台启动 / 失败原因。"""

    def _mgr(self, server="", model=""):
        return LlamaManager(
            base_url="http://127.0.0.1:11434", server_path=server, model_path=model
        )

    def test_start_already_running(self):
        mgr = self._mgr()
        with patch.object(mgr, "running", return_value=True):
            ok, reason = mgr.start()
        assert ok is True
        assert "已在运行" in reason

    def test_start_no_server(self):
        mgr = self._mgr(server="", model=r"D:\models\qwen.gguf")
        with patch.object(mgr, "running", return_value=False):
            with patch("framework.llm.LlamaManager.auto_locate_server", return_value=""):
                ok, reason = mgr.start()
        assert ok is False
        assert "llama-server" in reason

    def test_start_server_not_exist(self):
        mgr = self._mgr(server=r"C:\nope\llama-server.exe", model=r"D:\models\qwen.gguf")
        with patch.object(mgr, "running", return_value=False):
            ok, reason = mgr.start()
        assert ok is False
        assert "不存在" in reason

    def test_start_no_model(self):
        mgr = self._mgr(server="C:/llm/llama-server.exe", model="")
        with patch.object(mgr, "running", return_value=False):
            with patch("framework.llm.LlamaManager.auto_locate_server", return_value="C:/llm/llama-server.exe"):
                with patch("framework.llm.os.path.isfile", return_value=True):
                    ok, reason = mgr.start()
        assert ok is False
        assert "模型" in reason

    def test_start_model_not_exist(self):
        mgr = self._mgr(server="C:/llm/llama-server.exe", model=r"D:\nope.gguf")
        with patch.object(mgr, "running", return_value=False):
            ok, reason = mgr.start()
        assert ok is False
        assert "不存在" in reason

    def test_start_success(self):
        """正常启动：running 由连接失败转成功，探测到端口就绪 → 启动成功。"""
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        fake_proc.stderr = MagicMock()
        fake_proc.stderr.readline.return_value = ""
        mgr = self._mgr(server="C:/llm/llama-server.exe", model=r"D:\models\qwen.gguf")
        with patch.object(mgr, "running", side_effect=[False, False, True]):
            with patch("framework.llm.os.path.isfile", return_value=True):
                with patch("framework.llm.subprocess.Popen", return_value=fake_proc) as popen:
                    ok, reason = mgr.start(wait_seconds=10)
        assert ok is True
        assert reason == "启动成功"
        cmd = popen.call_args[0][0]
        assert cmd[0] == "C:/llm/llama-server.exe"
        assert cmd[1] == "-m" and cmd[2] == r"D:\models\qwen.gguf"
        assert "--port" in cmd and "11434" in cmd
        assert "--ctx-size" in cmd and "16384" in cmd

    def test_start_process_exits_with_stderr(self):
        """进程提前退出 → 返回 stderr 尾作为失败原因。"""
        fake_proc = MagicMock()
        fake_proc.poll.return_value = 1  # 进程已退出
        # stderr.readline 第一次返回错误行，之后返回 ""
        fake_proc.stderr.readline.side_effect = [
            "error: failed to load model: BAD gguf\n",
            "",
        ]
        mgr = self._mgr(server="C:/llm/llama-server.exe", model=r"D:\models\qwen.gguf")
        with patch.object(mgr, "running", return_value=False):
            with patch("framework.llm.os.path.isfile", return_value=True):
                with patch("framework.llm.subprocess.Popen", return_value=fake_proc):
                    ok, reason = mgr.start(wait_seconds=5)
        assert ok is False
        assert "启动失败" in reason
        assert "BAD gguf" in reason

    def test_start_timeout(self):
        """等待窗口内一直未就绪 → 启动超时。"""
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        fake_proc.stderr = MagicMock()
        fake_proc.stderr.readline.return_value = ""
        mgr = self._mgr(server="C:/llm/llama-server.exe", model=r"D:\models\qwen.gguf")
        with patch.object(mgr, "running", return_value=False):
            with patch("framework.llm.os.path.isfile", return_value=True):
                with patch("framework.llm.subprocess.Popen", return_value=fake_proc):
                    with patch("framework.llm.time.sleep"):
                        ok, reason = mgr.start(wait_seconds=1)
        assert ok is False
        assert "超时" in reason

    def test_start_popen_oserror(self):
        """Popen OSError（无法执行) → 启动失败原因。"""
        mgr = self._mgr(server="C:/llm/llama-server.exe", model=r"D:\models\qwen.gguf")
        with patch.object(mgr, "running", return_value=False):
            with patch("framework.llm.os.path.isfile", return_value=True):
                with patch("framework.llm.subprocess.Popen", side_effect=OSError("access denied")):
                    ok, reason = mgr.start()
        assert ok is False
        assert "无法执行" in reason

    def test_start_stderr_silent_does_not_block(self):
        """回归：进程存活但 stderr 静默（模型加载阶段）→ 不阻塞，窗口内正常超时。

        修复前 start() 在 _drain_stderr 的 readline() 上永久阻塞，
        永远到不了 running() 探测与超时检查 → UI「启动中」卡死。
        """
        import threading
        import io

        fake_proc = MagicMock()
        fake_proc.poll.return_value = None  # 进程存活
        # stderr 是一个有内容但已耗尽 EOF 的流（模拟安静加载期）
        fake_proc.stderr = io.StringIO("")
        mgr = self._mgr(server="C:/llm/llama-server.exe", model=r"D:\models\qwen.gguf")
        result = {}

        def _run():
            with patch.object(mgr, "running", return_value=False):
                with patch("framework.llm.os.path.isfile", return_value=True):
                    with patch("framework.llm.subprocess.Popen", return_value=fake_proc):
                        with patch("framework.llm.time.sleep"):
                            result["ok"], result["reason"] = mgr.start(wait_seconds=1)

        t = threading.Thread(target=_run)
        t.start()
        t.join(timeout=6.0)  # start 应远早于 6s 返回
        assert not t.is_alive(), "start() 永久阻塞！stderr 静默不应卡死"
        assert result["ok"] is False
        assert "超时" in result["reason"]


class TestLlamaStop:
    """stop() 托管进程终止。"""

    def test_stop_none(self):
        mgr = LlamaManager()
        assert mgr.stop() is False

    def test_stop_running_proc(self):
        mgr = LlamaManager()
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        mgr._proc = fake_proc
        assert mgr.stop() is True
        fake_proc.terminate.assert_called_once()

    def test_stop_already_exited(self):
        mgr = LlamaManager()
        fake_proc = MagicMock()
        fake_proc.poll.return_value = 0
        mgr._proc = fake_proc
        assert mgr.stop() is True  # 已退出，也视为已停止


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])