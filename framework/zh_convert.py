"""繁简中文转换（zhconv 封装，可选依赖）。

用途：繁体源（如 CZBooks）在阅读界面/正文展示时转成简体中文。
作为 decryption.targets 的一种 strategy（`zh_convert`）接入，可配置在
title/author/chapter/content 等目标上；zhconv 未安装时原样返回（不阻塞）。
"""
from __future__ import annotations

try:
    from zhconv import convert as _zhconv_convert
except Exception:  # noqa: BLE001 —— 未安装时降级为 no-op
    _zhconv_convert = None


# zhconv 的区域码：zh-cn=简体，zh-tw/zh-hk=繁体
_SIMPLE = {"zh-cn", "zh-hans", "cn", "hans", "s", "simplified", "简体", "简体中文"}
_TRAD = {"zh-tw", "zh-hk", "zh-hant", "tw", "hk", "hant", "t", "traditional", "繁體", "繁体"}


def normalize_locale(value: str) -> str:
    """把常见写法归一为 zhconv 认识的语言码（默认 zh-cn 简体）。"""
    v = (value or "").strip().lower()
    if not v:
        return "zh-cn"
    if v in _SIMPLE or v in ("zh", "中文", "chinese"):
        return "zh-cn"
    if v in _TRAD:
        return "zh-tw"
    # 含关键词兜底
    if "hans" in v or "简" in v:
        return "zh-cn"
    if "hant" in v or "繁" in v or "tw" in v or "hk" in v:
        return "zh-tw"
    return v


def to_simplified(text: str) -> str:
    """繁体/地区中文 → 简体中文。未安装 zhconv 或异常时原样返回。"""
    if not text or _zhconv_convert is None:
        return text
    try:
        return _zhconv_convert(text, "zh-cn")
    except Exception:  # noqa: BLE001
        return text


def convert(text: str, locale: str = "zh-cn") -> str:
    """按目标 locale 转换（默认简体）。zhconv 缺失时原样返回。"""
    if not text or _zhconv_convert is None:
        return text
    try:
        return _zhconv_convert(text, normalize_locale(locale))
    except Exception:  # noqa: BLE001
        return text


def available() -> bool:
    return _zhconv_convert is not None