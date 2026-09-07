"""LLM 客户端适配（llm.py）。

统一云端（OpenAI 兼容接口）与本地（Ollama）LLM 访问。
- LlmClient: OpenAI Chat Completions 兼容，使用 HttpClient.post_json
- OllamaManager: Ollama 一键启动/健康探测/模型列表
- LlmKeyStore: data/llm_keys.json 密钥读写（不碰 app_config.json）

密钥安全：API Key 仅存 data/llm_keys.json（该目录被 .gitignore 排除，绝不入 git）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from .http import HttpClient

log = logging.getLogger(__name__)


# ====================================================================== #
# 异常
# ====================================================================== #

class LlmError(Exception):
    """LLM 调用/配置错误。"""


# ====================================================================== #
# LlmKeyStore — 密钥存储
# ====================================================================== #

_DEFAULT_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)


class LlmKeyStore:
    """读写 data/llm_keys.json，隔离 API Key 等敏感配置。

    结构：
    {
        "cloud": {"api_key": "", "base_url": "", "model": ""},
        "local": {"base_url": "http://127.0.0.1:11434", "model": ""}
    }
    """

    def __init__(self, path: str | Path | None = None):
        if path is None:
            path = os.path.join(_DEFAULT_DATA_DIR, "llm_keys.json")
        self._path = Path(path)

    # ------------------------------------------------------------------ #
    def load(self) -> dict:
        """读取密钥文件；不存在/损坏返回空 dict（不抛异常）。"""
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("llm_keys.json 读取失败：%s", exc)
            return {}

    def save(self, data: dict) -> None:
        """写入密钥文件；自动创建 data/ 目录。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------ #
    def cloud(self) -> dict:
        """云端配置（api_key / base_url / model）。"""
        raw = self.load()
        return dict(raw.get("cloud") or {})

    def local(self) -> dict:
        """本地 Ollama 配置（base_url / model）。"""
        raw = self.load()
        default_local = {"base_url": "http://127.0.0.1:11434", "model": ""}
        return {**default_local, **(raw.get("local") or {})}


# ====================================================================== #
# LlmClient — OpenAI 兼容 Chat
# ====================================================================== #

class LlmClient:
    """OpenAI Chat Completions 兼容客户端。

    使用项目现有的 HttpClient.post_json 发起请求，不新增第三方依赖。
    base_url 归一：不以 /v1 结尾时追加 /v1；OllamaManager 用未追加的原始地址。
    """

    def __init__(self, base_url: str, api_key: str = "", model: str = ""):
        self._raw_base = base_url.rstrip("/")
        # 归一化：chat completions 接口统一加 /v1
        if self._raw_base.endswith("/v1"):
            self._base = self._raw_base
        else:
            self._base = self._raw_base + "/v1"
        self._api_key = api_key
        self._model = model

    @property
    def base_url(self) -> str:
        return self._base

    @property
    def raw_base(self) -> str:
        """未归一化的原始地址（供 OllamaManager 用）。"""
        return self._raw_base

    @property
    def model(self) -> str:
        return self._model

    # ------------------------------------------------------------------ #
    def chat(
        self,
        system: str,
        user: str,
        json_mode: bool = False,
        timeout: float = 60.0,
    ) -> str:
        """发送 chat 请求，返回助手回复文本。

        json_mode=True 时带 response_format（部分端点支持）。
        失败抛 LlmError。
        """
        http = HttpClient()
        try:
            url = f"{self._base}/chat/completions"
            headers: dict[str, str] = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"

            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": user})

            body: dict[str, Any] = {
                "model": self._model,
                "messages": messages,
            }
            if json_mode:
                body["response_format"] = {"type": "json_object"}

            try:
                resp = http.post_json(
                    url,
                    json_body=body,
                    headers=headers,
                    timeout=timeout,
                    retries=1,
                )
            except Exception as exc:
                raise LlmError(f"LLM 请求失败：{exc}") from exc

            # 解析 OpenAI 兼容响应
            if not isinstance(resp, dict):
                raise LlmError("LLM 响应格式异常：非 JSON 对象")

            # 检查 API 错误
            error = resp.get("error")
            if error:
                msg = error.get("message") if isinstance(error, dict) else str(error)
                raise LlmError(f"LLM API 错误：{msg}")

            choices = resp.get("choices")
            if not choices or not isinstance(choices, list):
                raise LlmError("LLM 响应无 choices 字段")

            first = choices[0]
            message = first.get("message") or {}
            content = message.get("content")
            if content is None:
                raise LlmError("LLM 响应无 content 字段")
            return content
        finally:
            http.close()


# ====================================================================== #
# OllamaManager — 本地 Ollama 管理
# ====================================================================== #

class OllamaManager:
    """Ollama 一键启动 / 健康探测 / 模型列表。

    base_url 为用户填写的 Ollama 地址（如 http://127.0.0.1:11434）。
    注意：Ollama 原生 API 在 /api/* 路径，不追加 /v1。
    """

    def __init__(self, base_url: str = "http://127.0.0.1:11434"):
        self._base = base_url.rstrip("/")

    @property
    def base_url(self) -> str:
        return self._base

    # ------------------------------------------------------------------ #
    def running(self) -> bool:
        """健康探测：GET /api/tags。"""
        http = HttpClient()
        try:
            resp = http.get_json(
                f"{self._base}/api/tags",
                timeout=5.0,
                retries=0,
            )
            # 服务连通且返回了 JSON（{} 或含 models）即视为运行中
            return isinstance(resp, dict)
        except Exception:
            return False
        finally:
            http.close()

    # ------------------------------------------------------------------ #
    def models(self) -> list[str]:
        """获取已安装模型名列表。"""
        http = HttpClient()
        try:
            resp = http.get_json(
                f"{self._base}/api/tags",
                timeout=10.0,
                retries=1,
            )
            if not isinstance(resp, dict):
                return []
            models = resp.get("models") or []
            return [m.get("name", "") for m in models if isinstance(m, dict)]
        except Exception as exc:
            log.warning("Ollama 模型列表获取失败：%s", exc)
            return []
        finally:
            http.close()

    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        """探测端口 → 未响应则启动 ollama serve。

        找不到 ollama 可执行文件 → 返回 False（调用方提示安装）。
        """
        if self.running():
            return True

        # 找不到 ollama 可执行文件 → 返回 False（调用方提示安装）
        if shutil.which("ollama") is None:
            log.info("未找到 ollama 可执行文件")
            return False

        # 尝试启动 ollama serve
        try:
            subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError:
            log.info("未找到 ollama 可执行文件")
            return False
        except OSError as exc:
            log.warning("ollama serve 启动失败：%s", exc)
            return False

        # 等待最多 8 秒
        for _ in range(16):
            time.sleep(0.5)
            if self.running():
                return True
        return False
