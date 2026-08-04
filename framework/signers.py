"""签名器模块（signers.py）。

外置签名能力，供 api_endpoints 的 sign 策略消费。框架不内置站点特定签名算法，
只提供策略注册与通用执行，源配置通过 sign.strategy 声明用哪个签名器。

对应 source-schema-v2 §api_endpoints 扩展（sign 子块）。

当前策略：
- bilibili_wbi: B 站 wbi 签名（仿 yt-dlp），用于无登录搜索，绕过风控降级。

用法：
    签名器负责在请求发出前，把签名参数插入待请求的 params。
    返回签名后的 params（dict），调用方再拼 query string。
"""

from __future__ import annotations

import copy
import hashlib
import re
import time
from typing import Dict, Optional
from urllib.parse import urlencode

from .errors import DecryptError

# yt-dlp 的 MIXIN_KEY_ENC_TAB：B 站 wbi 签名混排索引表
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def _get_wbi_keys(http) -> tuple:
    """从 nav 接口取 wbi 的 img_key + sub_key（未登录即可）。"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0",
               "Referer": "https://www.bilibili.com/"}
    data = http.get_json("https://api.bilibili.com/x/web-interface/nav",
                         headers=headers, timeout=10.0, retries=3)
    wbi = (data.get("data") or {}).get("wbi_img") or {}
    img_url = wbi.get("img_url") or ""
    sub_url = wbi.get("sub_url") or ""
    m_img = re.search(r"wbi/([^/]+)\.png", img_url)
    m_sub = re.search(r"wbi/([^/]+)\.png", sub_url)
    if not m_img or not m_sub:
        raise DecryptError(f"B站 wbi nav 未取到 key（code={data.get('code')}）")
    return m_img.group(1), m_sub.group(1)


def _mixin_key(edk: str) -> str:
    """按混排索引表重组密钥（yt-dlp 同款）。"""
    chars = list(edk)
    return "".join(chars[i] for i in MIXIN_KEY_ENC_TAB)


class Signer:
    """签名器基类。子类实现 sign(params) -> dict。"""

    name = "signer"

    def sign(self, params: Dict[str, object]) -> Dict[str, object]:
        raise NotImplementedError


class BilibiliWbiSigner(Signer):
    """B 站 wbi 签名。依赖 HttpClient 获取动态 key。"""

    name = "bilibili_wbi"

    def __init__(self, http=None, key_cache: Optional[dict] = None):
        self._http = http
        self._cache = key_cache if key_cache is not None else {"mk": None}

    def _mixin(self) -> str:
        if self._cache.get("mixin"):
            return self._cache["mixin"]
        if self._http is None:
            raise DecryptError("wbi 签名需要 HttpClient，且当前未注入")
        img_key, sub_key = _get_wbi_keys(self._http)
        mk = _mixin_key(img_key + sub_key)
        self._cache["mixin"] = mk
        return mk

    def sign(self, params: Dict[str, object]) -> Dict[str, object]:
        import copy

        mixed = self._mixin()
        out = copy.copy(params)
        # 去空值保留 wts
        out["wts"] = int(time.time())
        filtered = {k: str(v) for k, v in out.items() if v not in ("", None)}
        sorted_params = dict(sorted(filtered.items()))
        query = urlencode(sorted_params)
        sorted_params["w_rid"] = _md5(query + mixed)
        return sorted_params


_REGISTRY = {x.name: x for x in (BilibiliWbiSigner,)}


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def get_signer(strategy: str, http=None) -> Signer:
    """按策略名取签名器实例（strategy 为空返回 None）。"""
    if not strategy:
        return None
    cls = _REGISTRY.get(strategy)
    if cls is None:
        raise DecryptError(f"未知签名策略：{strategy!r}（可用 bilibili_wbi）")
    return cls(http)