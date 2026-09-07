"""LLM 客户端适配（llm.py）。

统一云端（OpenAI 兼容接口）与本地（LLAMA llama-server）LLM 访问。
- LlmClient: OpenAI Chat Completions 兼容，使用 HttpClient.post_json
- LlmManager: LLAMA（llama-server）后台托管启动 / 健康探测 / 停止，*失败原因采集*
- LlmKeyStore: data/llm_keys.json 密钥与本地模型配置读写（不碰 app_config.json）

密钥安全：API Key 仅存 data/llm_keys.json（该目录被 .gitignore 排除，绝不入 git）。
本地 LLAMA 配置（server_path / model_path / port / recent 列表）同样存 data 目录。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
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
# 常驻数据目录
# ====================================================================== #

_DEFAULT_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)

_DATA_SUBDIR = "llm"                 # data/llm/ 存提示词模板等
_DEFAULT_PROMPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts"
)
_BUILTIN_TEMPLATE = os.path.join(_DEFAULT_PROMPTS_DIR, "source_builder.txt")
_USER_TEMPLATE = os.path.join(_DEFAULT_DATA_DIR, "llm", "source_builder.txt")


# ====================================================================== #
# LlmKeyStore — 密钥与本地模型配置
# ====================================================================== #

class LlmKeyStore:
    """读写 data/llm_keys.json，隔离 API Key 与本地 LLAMA 配置。

    结构：
    {
        "cloud": {"api_key": "", "base_url": "", "model": ""},
        "local": {
            "base_url": "http://127.0.0.1:11434",
            "port": 11434,
            "server_path": "",                 # llama-server.exe 可执行文件
            "model_path": "",                  # 当前选中 .gguf
            "model": "",                       # 当前模型名（gguf 文件名）
            "recent": [{"name", "model_path", "server_path", "port"}]  # 曾选择
        }
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

    def _write_local(self, local: dict) -> None:
        raw = self.load()
        raw["local"] = local
        self.save(raw)

    # ------------------------------------------------------------------ #
    def cloud(self) -> dict:
        """云端配置（api_key / base_url / model）。"""
        raw = self.load()
        return dict(raw.get("cloud") or {})

    def local(self) -> dict:
        """本地 LLAMA 配置（base_url / port / server_path / model_path / model / recent）。

        后端统一以 base_url 为服务入口（含端口），例如
        http://127.0.0.1:11434 → OpenAI 兼容端点为 /v1/chat/completions。
        """
        raw = self.load()
        local = dict(raw.get("local") or {})
        port = int(local.get("port") or 11434)
        host = (local.get("base_url") or f"http://127.0.0.1:{port}")
        base = host.rstrip("/")
        return {
            "base_url": base,
            "port": port,
            "server_path": local.get("server_path") or "",
            "model_path": local.get("model_path") or "",
            "model": local.get("model") or "",
            "recent": local.get("recent") or [],
        }

    def save_cloud(self, api_key: str = "", base_url: str = "", model: str = "") -> None:
        raw = self.load()
        cloud = dict(raw.get("cloud") or {})
        cloud["api_key"] = api_key
        cloud["base_url"] = base_url
        cloud["model"] = model
        raw["cloud"] = cloud
        self.save(raw)

    def save_local(
        self,
        base_url: str = "",
        port: int = 11434,
        server_path: str = "",
        model_path: str = "",
        model: str = "",
    ) -> None:
        """覆盖本地配置并记录到 recent 历史。

        recent：按 (server_path, model_path) 去重，最近使用置顶，最多记 20 条。
        """
        recent = list(self.local().get("recent") or [])
        entry = {
            "name": model or (Path(model_path).name if model_path else ""),
            "model_path": model_path,
            "server_path": server_path,
            "port": int(port or 11434),
        }
        # 去重（以 model_path 为准，避免无模型路径的默认记录累积）
        if model_path:
            recent = [
                e for e in recent if str(e.get("model_path") or "") != model_path
            ]
        recent.insert(0, entry)
        recent = recent[:20]
        raw = self.load()
        raw["local"] = {
            "base_url": (base_url or f"http://127.0.0.1:{int(port or 11434)}"),
            "port": int(port or 11434),
            "server_path": server_path,
            "model_path": model_path,
            "model": model,
            "recent": recent,
        }
        self.save(raw)


# ====================================================================== #
# 提示词模板：用户可编辑（data/llm/source_builder.txt 优先，内置兜底）
# ====================================================================== #

def effective_template_path() -> str:
    """返回实际生效的提示词模板路径：用户模板存在则用用户模板，否则内置。"""
    if os.path.exists(_USER_TEMPLATE):
        return _USER_TEMPLATE
    return _BUILTIN_TEMPLATE


def load_effective_template() -> str:
    """返回实际生效的提示词模板内容（用户模板优先，内置兜底）。"""
    path = effective_template_path()
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("读取提示词模板失败 %s：%s", path, exc)
        return Path(_BUILTIN_TEMPLATE).read_text(encoding="utf-8")


def save_user_template(text: str) -> str:
    """保存用户提示词模板到 data/llm/source_builder.txt；返回写入路径。"""
    os.makedirs(os.path.dirname(_USER_TEMPLATE), exist_ok=True)
    Path(_USER_TEMPLATE).write_text(text, encoding="utf-8")
    return _USER_TEMPLATE


def reset_user_template() -> str:
    """删除用户模板（回退到内置）；返回内置模板内容。"""
    if os.path.exists(_USER_TEMPLATE):
        try:
            os.remove(_USER_TEMPLATE)
        except OSError as exc:
            log.warning("删除用户模板失败：%s", exc)
    return Path(_BUILTIN_TEMPLATE).read_text(encoding="utf-8")


# ====================================================================== #
# LlmClient — OpenAI 兼容 Chat + 连接测试
# ====================================================================== #

class LlmClient:
    """OpenAI Chat Completions 兼容客户端。

    使用项目现有的 HttpClient.post_json 发起请求，不新增第三方依赖。
    支持云端（https://api.openai.com/v1 等）与本地 LLAMA（llama-server
    OpenAI 兼容端点 /v1/chat/completions，base_url 不含 /v1 时自动追加）。

    test_connection() 做「探活 + 打招呼」两步校验：
      - GET base_url（根地址）→ 判断网络连通
      - POST /v1/chat/completions 发一条极简消息 → 判断 API Key / 模型正确
      失败按原因分类：网络不通 / 鉴权失败(API Key) / 模型不存在 / 服务端错误。
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
        """未归一化的原始地址（供根地址探活用）。"""
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

    # ------------------------------------------------------------------ #
    def test_connection(
        self,
        timeout: float = 15.0,
        greet_word: str = "你好",
    ) -> dict:
        """两步校验：GET 根地址探活 + chat 打招呼验证。

        返回：
        {
          "ok": bool,               # 全部通过
          "steps": [                 # 逐步结果（UI 逐行展示）
             {"label": "...", "ok": bool, "detail": "..."}
          ],
          "reason": ""              # 失败时的总原因（ok=False 时非空）
        }
        失败按原因分类：网络不通 / API Key 鉴权失败 / 模型不存在 / 服务端错误。
        """
        steps: list[dict] = []
        http = HttpClient()
        try:
            # 1) 根地址探活（返回文本仅作连通证据；401/404 等服务端 HTTP 响应
            #    也算「可达」，鉴权由下一步 chat 判定）
            try:
                _text = http.get_text(self._raw_base, timeout=timeout, retries=1)
                snippet = (_text or "").strip().replace("\n", " ")[:40]
                steps.append({
                    "label": "链接可达",
                    "ok": True,
                    "detail": f"{self._raw_base} → 响应" + (f"：{snippet}" if snippet else ""),
                })
            except Exception as exc:
                steps.append({
                    "label": "链接可达",
                    "ok": False,
                    "detail": self._categorize_network_error(exc),
                })
                return self._result(steps, "网络连接失败")

            # 2) 真实 chat 打招呼
            try:
                reply = self.chat(
                    system="你是一个简洁的助手。",
                    user=greet_word,
                    timeout=timeout,
                )
                steps.append({
                    "label": "模型打招呼",
                    "ok": True,
                    "detail": f"模型已回复：{(reply or '').strip()[:60]}",
                })
            except LlmError as exc:
                kind = self._categorize_chat_error(str(exc))
                steps.append({
                    "label": "模型打招呼",
                    "ok": False,
                    "detail": str(exc),
                })
                return self._result(steps, kind)

            return self._result(steps, "")
        finally:
            http.close()

    def _result(self, steps: list[dict], reason: str) -> dict:
        ok = all(s["ok"] for s in steps)
        return {"ok": ok, "steps": steps, "reason": reason if not ok else ""}

    @staticmethod
    def _categorize_network_error(exc: Exception) -> str:
        """把探活异常归类为可读原因。"""
        msg = str(exc)
        low = msg.lower()
        if any(w in low for w in ("timed out", "timeout", "timeouterror", "connect")):
            if "timed out" in low or "connect" in low:
                return "连接超时或无法连接（请检查地址/网络）"
        if any(w in low for w in ("refused", "503", "service unavailable")):
            return "服务未启动（连接被拒绝）"
        if any(w in low for w in ("401", "403", "unauthorized", "forbidden", "apikey", "api key")):
            return "鉴权失败（API Key 无效）"
        if any(w in low for w in ("dns", "name or service not known", "getaddrinfo")):
            return "DNS 解析失败（域名错误）"
        return msg[:200]

    @staticmethod
    def _categorize_chat_error(msg: str) -> str:
        """把 chat 异常归类为可读原因。"""
        low = msg.lower()
        if any(w in low for w in ("401", "403", "unauthorized", "forbidden", "invalid api key", "auth")):
            return "API Key 无效或权限不足"
        if any(w in low for w in ("404", "not found", "model not found", "does not exist", "不存在", "not exist")):
            return "模型不存在或地址错误"
        if any(w in low for w in ("400", "invalid", "429", "quota", "limit", "overloaded")):
            return "请求被拒绝（模型名错误或配额/限流）"
        if any(w in low for w in ("500", "502", "503", "504", "server error", "internal")):
            return "服务端错误（联系模型服务方）"
        return msg[:200]


# ====================================================================== #
# LlamaManager — 本地 LLAMA（llama-server）托管启动
# ====================================================================== #

@dataclass
class LlamaProcess:
    """已启动的 llama-server 托管进程信息。"""

    pid: int
    command: str
    started_at: float
    stderr_tail: list[str] = field(default_factory=list)


class LlamaManager:
    """LLAMA（llama.cpp llama-server）后台托管启动 / 健康探测 / 停止。

    - running(): 探测 base_url 是否已响应
    - start(): 后台 Popen 启动 llama-server（不阻塞 UI），失败原因从 stderr
      采集；找不到 exe / 模型不存在 / 已在运行 分别返回明确原因
    - stop(): 结束托管进程并等待退出
    - auto_locate_server(): PATH 探测 llama-server[.exe]

    base_url 同时是 API 入口（不含 /v1），llama-server OpenAPI 兼容端点
    为 <base_url>/v1/chat/completions，健康端点为 <base_url>/health。

    用户偏好「后台启动 + 失败原因」满足：
      启动不阻塞 UI —— 立即返回「启动中」，进程探测在后台；
      启动失败 —— 读 stderr 尾部作为失败原因返回。
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        server_path: str = "",
        model_path: str = "",
    ):
        self._base = base_url.rstrip("/")
        self._server_path = server_path.strip()
        self._model_path = model_path.strip()
        self._proc: subprocess.Popen | None = None
        self._stderr_lock = threading.Lock()
        self._stderr_buf: list[str] = []

    @property
    def base_url(self) -> str:
        return self._base

    @property
    def server_path(self) -> str:
        return self._server_path

    @property
    def model_path(self) -> str:
        return self._model_path

    # ------------------------------------------------------------------ #
    @staticmethod
    def auto_locate_server() -> str:
        """PATH 探测 llama-server 可执行文件；找不到返回空串。"""
        for name in ("llama-server", "llama-server.exe"):
            found = shutil.which(name)
            if found:
                return found
        return ""

    # ------------------------------------------------------------------ #
    def running(self, timeout: float = 5.0) -> bool:
        """健康探测 base_url。llama-server 提供 /health 端点。"""
        http = HttpClient()
        try:
            resp = http.get_json(f"{self._base}/health", timeout=timeout, retries=0)
            return isinstance(resp, dict)
        except Exception:
            # 部分实现无 /health，探根地址兜底
            try:
                _ = http.get_text(f"{self._base}/", timeout=timeout, retries=0)
                return True
            except Exception:
                return False
        finally:
            http.close()

    # ------------------------------------------------------------------ #
    def start(self, wait_seconds: float = 20.0) -> tuple[bool, str]:
        """后台启动 llama-server，返回 (ok, reason)。

        ok=True: 已启动成功（或已在运行）。
        ok=False: 原因字符串：

        必须先满足启动前置条件：
        - 已有进程托管 → 返回 (True, "已启动")
        - 已在运行（端口已响应）→ (True, "已在运行")
        - 找不到 llama-server.exe（未填且 PATH 无）→ (False, "未找到 llama-server 可执行文件…")
        - 模型文件不存在 → (False, "模型文件不存在：…")
        然后 Popen 后台启动：
        - 启动后轮询 base_url 健康；期间读 stderr，若进程退出即返回其 stderr 尾作为失败原因
        - 超时未就绪 → 返回 (False, "启动超时，未响应探活", stderr 尾)
        """
        # 已在运行
        if self.running(timeout=2.0):
            return True, "已在运行"

        # 前置校验：可执行文件
        server = self._server_path or self.auto_locate_server()
        if not server:
            return False, "未找到 llama-server.exe，请在设置页选择 llama.cpp 的 llama-server 可执行文件，或将其加入 PATH"
        if not os.path.isfile(server):
            return False, f"llama-server 可执行文件不存在：{server}"
        # 前置校验：模型
        if not self._model_path:
            return False, "未选择本地模型（.gguf）文件，请在设置页浏览选择"
        if not os.path.isfile(self._model_path):
            return False, f"模型文件不存在：{self._model_path}"

        # 解析端口
        try:
            port = _port_from_url(self._base)
        except ValueError:
            port = 11434
        host = _host_from_url(self._base)

        # 拼命令行：llama-server -m <gguf> --host <host> --port <port>
        cmd = [server, "-m", self._model_path, "--host", host, "--port", str(port)]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            return False, f"启动失败（无法执行）：{exc}"

        # stderr 必须丢到后台线程消化 — readline() 是阻塞读，
        # 若在主循环里逐行读，llama-server 加载模型期间 stderr 无新行，
        # readline() 永久阻塞 → 永远探测不到端口就绪 → 「启动中」卡死。
        self._stderr_buf.clear()
        self._stop_reader = threading.Event()
        self._reader_thread = threading.Thread(
            target=self._read_stderr_loop, daemon=True, name="llama-stderr"
        )
        self._reader_thread.start()

        start_t = time.time()
        # 非阻塞轮询：启动期间探测端口就绪；若进程提前退出则报 stderr 尾部
        while time.time() - start_t < wait_seconds:
            if self.running(timeout=1.0):
                return True, "启动成功"
            if self._proc.poll() is not None:
                # 进程提前退出 → 用 stderr 定位失败原因
                tail = self._stderr_tail(-8) or "(无输出，进程即退出)"
                return False, f"启动失败：{tail}"
            time.sleep(0.3)

        tail = self._stderr_tail(-8) or ""
        return False, f"启动超时（{int(wait_seconds)}s 未响应探活）{('：' + tail) if tail else ''}"

    # ------------------------------------------------------------------ #
    def _read_stderr_loop(self) -> None:
        """后台线程持续收集 llama-server stderr（阻塞读但不在主循环）。"""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = proc.stderr.readline()
                if not line:
                    break
                line = line.rstrip("\n")
                if line:
                    with self._stderr_lock:
                        self._stderr_buf.append(line)
                        if len(self._stderr_buf) > 200:
                            del self._stderr_buf[:-200]
        except Exception:  # noqa: BLE001 —— 管道被关等情形直接退出线程
            pass

    def _stderr_tail(self, n: int = -8) -> str:
        """非阻塞读取 stderr 缓冲区尾部；进程已退出时先耗尽残留。"""
        proc = self._proc
        if proc is not None and proc.stderr is not None and proc.poll() is not None:
            # 进程已退出：尽力把残余读进来（readline 在 EOF 时立即返回）
            try:
                while True:
                    line = proc.stderr.readline()
                    if not line:
                        break
                    line = line.rstrip("\n")
                    if line:
                        with self._stderr_lock:
                            self._stderr_buf.append(line)
            except Exception:  # noqa: BLE001
                pass
        with self._stderr_lock:
            buf = list(self._stderr_buf)
        return "\n".join(buf[n:])

    # ------------------------------------------------------------------ #
    def stop(self) -> bool:
        """停止托管进程并等待退出。未托管 → 直接返回 False。"""
        proc = self._proc
        if proc is None:
            return False
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5.0)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        # 关 stderr 管道 → 后台读取线程在 readline 返回空时退出
        try:
            if proc.stderr is not None:
                proc.stderr.close()
        except Exception:  # noqa: BLE001
            pass
        reader = getattr(self, "_reader_thread", None)
        if reader is not None and reader.is_alive():
            reader.join(timeout=1.0)
        self._proc = None
        return True

    # ------------------------------------------------------------------ #
    def is_managed(self) -> bool:
        """当前 LlamaManager 是否托管了一个仍在运行的进程。"""
        return self._proc is not None and self._proc.poll() is None


def _port_from_url(url: str) -> int:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.port:
        return parts.port
    return 443 if parts.scheme == "https" else 80


def _host_from_url(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).hostname or "127.0.0.1"