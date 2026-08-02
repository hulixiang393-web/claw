"""解密模块（decrypter.py）。

外置解密能力。框架不内置站点特定算法，只提供通用解码 + 调用外置解密 API。
对应 source-schema-v2 §decryption 与 design.md §5.3。

能力：
- base64_decode: 通用 base64 解码
- custom_endpoint: 调用外置解密 HTTP 接口
- 源配置通过 decryption 声明用哪个策略
"""

from __future__ import annotations

import base64
import json
import re
from typing import Optional

from .config import SourceConfig
from .errors import DecryptError
from .http import HttpClient


class Decrypter:
    def __init__(self, http: HttpClient):
        self._http = http

    # ------------------------------------------------------------------ #
    def decrypt(self, source: SourceConfig, content: str, target: str = "content") -> str:
        """按源配置的 decryption 策略解密内容。

        :param source: 源配置
        :param content: 待解密内容（正文 base64 / 密文）
        :param target: 解密目标（content/image/video_url）
        """
        dec_cfg = source.raw.get("decryption") or {}
        targets = dec_cfg.get("targets") or {}
        cfg = targets.get(target) or {}
        strategy = cfg.get("strategy") or "base64_decode"

        if strategy == "base64_decode":
            return self._base64_decode(content)
        if strategy == "custom_endpoint":
            return self._call_endpoint(cfg, content)
        # 默认原样返回
        return content

    # ------------------------------------------------------------------ #
    def _base64_decode(self, content: str) -> str:
        text = content.strip()
        try:
            pad = text + "=" * (-len(text) % 4)
            raw = base64.b64decode(pad)
            return raw.decode("utf-8", errors="replace")
        except Exception as exc:
            raise DecryptError(f"base64 解码失败：{exc}") from exc

    def _call_endpoint(self, cfg: dict, content: str) -> str:
        endpoint = cfg.get("endpoint")
        if not endpoint:
            raise DecryptError("decryption.custom_endpoint 缺少 endpoint")
        response_field = cfg.get("response_field") or "result"
        try:
            resp = self._http.post_json(
                endpoint,
                json_body={cfg.get("request_field") or "data": content},
            )
        except Exception as exc:
            raise DecryptError(f"解密接口调用失败：{exc}") from exc
        if not isinstance(resp, dict) or response_field not in resp:
            raise DecryptError(f"解密接口响应缺少 {response_field} 字段")
        return str(resp[response_field])

    # ------------------------------------------------------------------ #
    def extract_base64_blocks(self, html: str) -> str:
        """从混淆脚本中提取 base64 块并解码拼接。

        匹配 <script>document.writeln(XX.qq('base64'))</script> 模式。
        """
        blocks = re.findall(
            r"document\.writeln\([^)]*?\(\s*'([^']+)'\s*\)\s*\)",
            html,
            re.IGNORECASE,
        )
        if not blocks:
            return ""
        parts = []
        for b in blocks:
            try:
                pad = b + "=" * (-len(b) % 4)
                parts.append(base64.b64decode(pad).decode("utf-8", errors="replace"))
            except Exception:
                continue
        return "\n".join(parts)
