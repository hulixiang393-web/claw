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
        strategy = cfg.get("strategy") or ""
        # 未配置解密策略 → 原样返回（与 decrypt_bytes 一致，避免误 base64 解码）
        if not strategy:
            return content
        if strategy == "base64_decode":
            return self._base64_decode(content)
        if strategy == "custom_endpoint":
            return self._call_endpoint(cfg, content)
        if strategy == "translit":
            return self._translit(content, cfg)
        # 默认原样返回
        return content

    # ------------------------------------------------------------------ #
    def decrypt_bytes(self, source: SourceConfig, data: bytes, target: str = "image") -> bytes:
        """按源配置解密二进制（图片等）。

        :param source: 源配置
        :param data: 待解密字节（如加密图片）
        :param target: 解密目标（image/video_url）
        返回解密后的字节；源未配置解密或解密失败时原样返回。
        """
        dec_cfg = source.raw.get("decryption") or {}
        targets = dec_cfg.get("targets") or {}
        cfg = targets.get(target) or {}
        strategy = cfg.get("strategy") or ""
        if not strategy:
            return data

        if strategy == "aes_cbc":
            return self._aes_cbc_bytes(cfg, data)
        if strategy == "aes_ecb":
            return self._aes_ecb_bytes(cfg, data)
        if strategy == "xor":
            return self._xor_bytes(cfg, data)
        # 未知策略原样返回
        return data

    # ------------------------------------------------------------------ #
    def _aes_cbc_bytes(self, cfg: dict, data: bytes) -> bytes:
        """AES-CBC 字节解密。key/iv 为 UTF-8 字符串（JS CryptoJS.enc.Utf8.parse 等价）。

        padding: "none"（JS NoPadding，要求密文长度是 16 的倍数）/
                 "pkcs7"（默认）。
        密钥/IV 可为 base64（CryptoJS.enc.Base64.parse）或明文 UTF-8。
        """
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError:
            raise DecryptError("需要安装 cryptography：pip install cryptography") from None

        key = self._parse_key(cfg.get("key"), 16)
        iv = self._parse_key(cfg.get("iv"), 16)
        if len(data) % 16 != 0:
            raise DecryptError(f"AES-CBC 密文长度 {len(data)} 不是 16 的倍数")

        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        dec = cipher.decryptor()
        plain = dec.update(data) + dec.finalize()
        # 去除 PKCS7 填充（默认）；NoPadding 时不动
        padding = (cfg.get("padding") or "pkcs7").lower()
        if padding in ("pkcs7", "pkcs5"):
            pad_len = plain[-1] if plain else 0
            if 1 <= pad_len <= 16 and plain[-pad_len:] == bytes([pad_len]) * pad_len:
                plain = plain[:-pad_len]
        return plain

    def _aes_ecb_bytes(self, cfg: dict, data: bytes) -> bytes:
        """AES-ECB 字节解密（无 IV）。"""
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError:
            raise DecryptError("需要安装 cryptography：pip install cryptography") from None

        key = self._parse_key(cfg.get("key"), 16)
        if len(data) % 16 != 0:
            raise DecryptError(f"AES-ECB 密文长度 {len(data)} 不是 16 的倍数")
        cipher = Cipher(algorithms.AES(key), modes.ECB())
        dec = cipher.decryptor()
        plain = dec.update(data) + dec.finalize()
        padding = (cfg.get("padding") or "pkcs7").lower()
        if padding in ("pkcs7", "pkcs5"):
            pad_len = plain[-1] if plain else 0
            if 1 <= pad_len <= 16 and plain[-pad_len:] == bytes([pad_len]) * pad_len:
                plain = plain[:-pad_len]
        return plain

    def _xor_bytes(self, cfg: dict, data: bytes) -> bytes:
        """逐字节 XOR 解密（key 为字节序列）。"""
        key = (cfg.get("key") or "").encode("utf-8")
        if not key:
            return data
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))

    @staticmethod
    def _parse_key(value, length: int) -> bytes:
        """解析密钥/IV：先尝试 base64，再按 UTF-8 明文；不足补零、超出截断。"""
        if not value:
            raise DecryptError(f"缺少 AES 密钥/IV：{value!r}")
        s = str(value)
        # 尝试 base64（CryptoJS.enc.Base64.parse 的 key 形如 22 字节 base64）
        try:
            if re.fullmatch(r"[A-Za-z0-9+/=]+", s) and len(s) % 4 == 0:
                raw = base64.b64decode(s)
                if len(raw) in (16, 24, 32):
                    return raw
        except Exception:
            pass
        raw = s.encode("utf-8")
        if len(raw) == length:
            return raw
        if len(raw) > length:
            return raw[:length]
        return raw + b"\x00" * (length - len(raw))

    # ------------------------------------------------------------------ #
    def _base64_decode(self, content: str) -> str:
        text = content.strip()
        try:
            pad = text + "=" * (-len(text) % 4)
            raw = base64.b64decode(pad)
            return raw.decode("utf-8", errors="replace")
        except Exception as exc:
            raise DecryptError(f"base64 解码失败：{exc}") from exc

    def _translit(self, content: str, cfg: dict) -> str:
        """字符映射解密（如番茄小说字体混淆：PUA 码位 → 汉字）。

        映射表由 cfg["map_module"] 指定（默认 framework.data.fanqie_glyph_map），
        用 str.translate 批量替换。加载失败/异常时原样返回（不阻塞正文）。
        """
        if not content:
            return content
        try:
            mod_name = cfg.get("map_module") or "framework.data.fanqie_glyph_map"
            import importlib

            table = getattr(importlib.import_module(mod_name), "FANQIE_GLYPH_MAP", None)
            if isinstance(table, dict) and table:
                # str.translate 只接受 ord(int) 作 key，字符 key 不生效（实测静默失败）
                return content.translate({ord(k): v for k, v in table.items()})
        except Exception:  # noqa: BLE001
            pass
        return content

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
