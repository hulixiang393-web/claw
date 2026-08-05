"""广告过滤引擎（adblock.py）。

通用广告过滤，供下载与播放统一接入。按源配置 ad_block 驱动：
- enabled:        开关（默认 true）
- block_url_regex: 追加 URL 广告特征正则（源级补充）
- block_domains:   追加广告域名黑名单

内置通用启发式（对所有源生效）：
- URL 特征：视频/图片 URL 路径含 ad/banner/promo/gg/advert 等 → 判为广告
- m3u8 广告段剔除：解析 HLS 播放列表，识别广告段
  （#EXT-X-DISCONTINUITY 分隔 + 段 URL 广告特征/异常短时长），
  重写为无广告段的 m3u8 供播放/下载。

对应 core.md「广告过滤规则体系」与 source-schema-v2 §7.5 ad_block。
"""

from __future__ import annotations

import re
from typing import List, Optional
from urllib.parse import urlparse

# 内置通用广告 URL 特征（路径段或明确广告词，防误伤正常 URL）
_DEFAULT_URL_AD_RE = re.compile(
    r"(?:/ads?/|/ad-|_ad\.|/advert|/banner|/promo|/ad\.|/ad/|\.ads\.|/gg/|/guanggao/)",
    re.IGNORECASE,
)
# 内置广告域名黑名单（子串匹配，支持 *.xxx.com 简写）
_DEFAULT_AD_DOMAINS = (
    "doubleclick.net",
    "googlesyndication.com",
    "adsystem.com",
    "adservice.google",
    "advertising.com",
    "adnxs.com",
)


class AdblockEngine:
    """广告过滤引擎。每个源一个实例（读该源的 ad_block 配置）。"""

    def __init__(self, source=None):
        self._block_re = _DEFAULT_URL_AD_RE
        self._block_domains = list(_DEFAULT_AD_DOMAINS)
        self._enabled = True
        self._extra_regexes: List[re.Pattern] = []
        self._extra_domains: List[str] = []
        if source is not None:
            self.configure(source)

    # ------------------------------------------------------------------ #
    def configure(self, source) -> None:
        """从源配置读 ad_block，构建过滤规则。"""
        # 重置补充规则：同一实例被 configure 多次时不累积旧规则
        self._extra_regexes = []
        self._extra_domains = []
        raw = getattr(source, "raw", None) or {}
        ad = raw.get("ad_block") or {}
        self._enabled = bool(ad.get("enabled", True))
        if not self._enabled:
            return
        # 源级补充 URL 正则
        for pat in ad.get("block_url_regex") or []:
            try:
                self._extra_regexes.append(re.compile(pat, re.IGNORECASE))
            except re.error:
                continue
        # 源级补充域名黑名单
        for dom in ad.get("block_domains") or []:
            d = str(dom).strip().lstrip("*.")
            if d:
                self._extra_domains.append(d)

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------ #
    def is_ad_url(self, url: str) -> bool:
        """判断单个 URL 是否广告（URL 特征 + 域名黑名单 + 源级补充）。"""
        if not self._enabled or not url:
            return False
        low = url.lower()
        host = (urlparse(url).hostname or "").lower()
        # 域名黑名单（精确后缀匹配：adservice.google 命中 adservice.google.com，但不误伤 myadsystem.com）
        for dom in self._block_domains + self._extra_domains:
            d = dom.lower()
            if host == d or host.endswith("." + d):
                return True
        # 内置 URL 特征
        if self._block_re.search(urlparse(low).path):
            return True
        # 源级补充正则
        for p in self._extra_regexes:
            if p.search(low):
                return True
        return False

    def filter_urls(self, urls: List[str]) -> List[str]:
        """过滤一批 URL，剔除广告。"""
        if not self._enabled:
            return urls
        return [u for u in urls if not self.is_ad_url(u)]

    # ------------------------------------------------------------------ #
    def filter_m3u8(self, m3u8_text: str, base_url: str = "") -> str:
        """剔除 m3u8 里的广告段，返回重写后的播放列表。

        通用启发式：
        1. 段 URL 命中广告特征（is_ad_url）→ 剔除该段
        2. 广告段通常紧跟在 #EXT-X-DISCONTINUITY 之后且时长异常短（< 3s）→ 剔除
        剔除时连同其前后的 DISCONTINUITY 标记一并清理，保持列表合法。
        """
        if not self._enabled or not m3u8_text:
            return m3u8_text
        from urllib.parse import urljoin

        lines = m3u8_text.splitlines()
        out: List[str] = []
        i = 0
        n = len(lines)
        # 记录上一非空行是否为 DISCONTINUITY（广告段通常紧跟它）
        ad_marker = False
        while i < n:
            line = lines[i]
            # #EXTINF 时长（秒）
            dur = None
            m = re.match(r"#EXTINF:\s*([\d.]+)", line)
            if m:
                dur = float(m.group(1))
            # 段 URL（非 # 开头）
            if line and not line.startswith("#"):
                # 广告段判定：1) 段 URL 命中广告特征；2) 仅在紧跟 DISCONTINUITY
                # 且异常短时才判广告（避免误删 1-2s 的正常段）
                joined = urljoin(base_url, line) if base_url else line
                is_ad = self.is_ad_url(joined)
                if not is_ad and ad_marker and dur is not None and dur < 3.0:
                    is_ad = True
                if is_ad:
                    # 回退：删掉刚写入的 EXTINF / KEY / MAP / DISCONTINUITY
                    # （避免残留引用已删段的加密 key / 初始化段）
                    while out and (
                        out[-1].startswith("#EXTINF")
                        or out[-1].startswith("#EXT-X-DISCONTINUITY")
                        or out[-1].startswith("#EXT-X-KEY")
                        or out[-1].startswith("#EXT-X-MAP")
                        or out[-1].startswith("#EXT-X-BYTERANGE")
                    ):
                        out.pop()
                    # 跳过本段及后续紧邻的 DISCONTINUITY
                    i += 1
                    ad_marker = False
                    while i < n and lines[i].startswith("#EXT-X-DISCONTINUITY"):
                        i += 1
                    continue
            # 更新上一行是否为 DISCONTINUITY 标记
            ad_marker = line.startswith("#EXT-X-DISCONTINUITY")
            out.append(line)
            i += 1
        return "\n".join(out)


# ------------------------------------------------------------------ #
# 便捷单例（无源配置时用内置规则）
# ------------------------------------------------------------------ #
_default_engine: Optional[AdblockEngine] = None


def adblock_for(source=None) -> AdblockEngine:
    """取某源的广告引擎（读源 ad_block 配置）。缺省用内置规则。"""
    if source is not None:
        return AdblockEngine(source)
    global _default_engine
    if _default_engine is None:
        _default_engine = AdblockEngine()
    return _default_engine
