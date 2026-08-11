"""解密模块（decrypter.py）。

外置解密能力。框架不内置站点特定算法，只提供通用解码 + 调用外置解密 API。
对应 source-schema-v2 §decryption 与 design.md §5.3。

能力：
- base64_decode: 通用 base64 解码
- custom_endpoint: 调用外置解密 HTTP 接口
- js_custom: 执行源自定义 JS 解密函数（复用 Playwright evaluate）
- 源配置通过 decryption 声明用哪个策略

js_custom 约定（decryption.targets.*）：
- strategy: "js_custom"
- script: JS 函数体源码，定义一个入口函数（entry 指定，默认 decrypt）
- entry : 入口函数名，默认 "decrypt"，签名 (data, params) -> result
- params: dict（可选），JSON 中可含密钥/偏移等，网页执行时展开为参数对象
- 文本解密：data 为源文本串，result 须为字符串明文
- 字节解密（decrypt_bytes）：data 为该字节的 base64 串，result 须为 base64 串
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
        if strategy == "maccms_url":
            return self._maccms_url(content)
        if strategy == "custom_endpoint":
            return self._call_endpoint(cfg, content)
        if strategy == "translit":
            return self._translit(content, cfg)
        if strategy == "js_custom":
            return self._js_custom(cfg, content)
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
        if strategy == "js_custom":
            return self._js_custom_bytes(cfg, data)
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
    @staticmethod
    def _parse_key_enc(value, length: int) -> bytes:
        """加密用 key/iv 解析：明文 UTF-8 优先（站点 JS 的 CryptoJS key 多为
        UTF-8 明文，会被 _parse_key 的 base64 猜测误判为其它字节长度）；
        'b64:' 前缀强制按 base64（与 CryptoJS.enc.Base64.parse 等价）。"""
        s = str(value)
        if s.startswith("b64:"):
            raw = base64.b64decode(s[4:])
        else:
            raw = s.encode("utf-8")
        if len(raw) == length:
            return raw
        if len(raw) > length:
            return raw[:length]
        return raw + b"\x00" * (length - len(raw))

    @staticmethod
    def aes_cbc_encrypt_text(key: str, iv: str, text: str) -> str:
        """AES-CBC 加密文本 → base64（等价 CryptoJS.AES.encrypt(...).toString()）。

        供需要加密请求体的 API（如 MacCMS/自研站目录接口）使用；
        key/iv 为 UTF-8 明文（'b64:' 前缀表示 base64，与 CryptoJS.enc.Base64.parse 等价）。
        """
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError:
            raise DecryptError("需要安装 cryptography：pip install cryptography") from None

        raw_key = Decrypter._parse_key_enc(key, 32 if len(str(key)) > 16 else 16)
        raw_iv = Decrypter._parse_key_enc(iv, 16)
        data = text.encode("utf-8")
        pad_len = 16 - len(data) % 16
        data += bytes([pad_len]) * pad_len
        enc = Cipher(algorithms.AES(raw_key), modes.CBC(raw_iv)).encryptor()
        return base64.b64encode(enc.update(data) + enc.finalize()).decode("ascii")

    # ------------------------------------------------------------------ #
    def _base64_decode(self, content: str) -> str:
        text = content.strip()
        try:
            pad = text + "=" * (-len(text) % 4)
            raw = base64.b64decode(pad)
            return raw.decode("utf-8", errors="replace")
        except Exception as exc:
            raise DecryptError(f"base64 解码失败：{exc}") from exc

    def _maccms_url(self, content: str) -> str:
        """MacCMS 播放地址解密：urlencode(base64(url)) → unquote 后 base64 解码。

        兼容纯 base64 密文（无 % 编码）；b64decode 宽容模式会吞掉明文 URL 的
        非法字符（如 ://），故解码后做 round-trip 校验区分真密文：解码再编码
        与原文（忽略 padding）一致才算密文，否则（明文 m3u8 等）原样返回。
        """
        import urllib.parse

        text = content.strip()
        try:
            s = urllib.parse.unquote(text).strip()
            raw = base64.b64decode(s + "=" * (-len(s) % 4))
            if base64.b64encode(raw).rstrip(b"=") != s.encode().rstrip(b"="):
                return content
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return content

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
    def _js_custom(self, cfg: dict, content: str) -> str:
        """执行源自定义 JS 解密函数（文本）。data 为文本串，result 须为字符串。"""
        script = cfg.get("script") or ""
        if not script:
            raise DecryptError("decryption.js_custom 缺少 script")
        try:
            result = _evaluate_js(script, cfg.get("entry") or "decrypt",
                                  content, cfg.get("params"), is_bytes=False)
        except Exception as exc:
            raise DecryptError(f"JS 解密失败：{exc}") from exc
        return str(result) if result is not None else content

    def _js_custom_bytes(self, cfg: dict, data: bytes) -> bytes:
        """执行源自定义 JS 解密函数（字节）。data 以 base64 传入，result 须为 base64 串。"""
        script = cfg.get("script") or ""
        if not script:
            raise DecryptError("decryption.js_custom 缺少 script")
        b64 = base64.b64encode(data).decode("ascii")
        try:
            result = _evaluate_js(script, cfg.get("entry") or "decrypt",
                                  b64, cfg.get("params"), is_bytes=True)
        except Exception as exc:
            raise DecryptError(f"JS 解密失败：{exc}") from exc
        if result is None:
            return data
        try:
            return base64.b64decode(str(result))
        except Exception as exc:
            raise DecryptError(f"JS 解密结果不是合法 base64：{exc}") from exc

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


# --------------------------------------------------------------------------- #
# JS 自定义解密执行器：复用 playwright_helper 常驻浏览器，eval 一段入口函数
# --------------------------------------------------------------------------- #
def _evaluate_js(script: str, entry: str, data, params=None, is_bytes: bool = False):
    """在常驻 Playwright 页面中执行源自定义 JS 解密。

    script 为函数源码（可含自由函数/变量），末尾由本函数追加调用
    `entry(data, params)` 取回结果。文本解密 data 传字符串；
    字节解密 data 传 base64（result 也须为 base64），由调用方负责编解码。
    """
    from . import playwright_helper as ph

    # 构造：用户函数定义 + 调用入口（JSON 序列化参数避免注入）
    import json as _json

    params_js = _json.dumps(params or {}, ensure_ascii=False)
    caller = (
        f"(function() {{ const _d = {_json.dumps(data)}; "
        f"const _p = {params_js}; "
        f"{script}; return {entry}(_d, _p); }})()"
    )
    return ph.evaluate_js_sync(caller)
