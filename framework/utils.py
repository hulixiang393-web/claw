"""通用纯函数工具（utils.py）。

把 content / discovery / search 里重复内联的极简工具收敛为模块级纯函数：
- jsonpath          点号路径取值（dict 键 + 列表索引含负数，越界返回 None）
- fill_template     {占位符} 模板填充（mapping 缺失替换为空串）
- guess_image_mime  按魔数猜图片 mime（默认 image/jpeg）
- abs_url           相对 URL 补全（已带 scheme / // 开头原样返回）

取各模块现有实现的最强能力（如 content._jsonpath 支持负数索引），
供后续模块直接 import，不再各自内联复制。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlsplit


def jsonpath(data: Any, path: str) -> Any:
    """极简 JSONPath 取值：data.owner.name / data.list.0 点号路径。

    支持 dict 键逐层取值与 list 索引（含负数，越界返回 None）；
    任一段不匹配返回 None；路径为空返回 None。
    """
    if not path:
        return None
    cur = data
    for part in str(path).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.lstrip("-").isdigit():
            idx = int(part)
            cur = cur[idx] if -len(cur) <= idx < len(cur) else None
        else:
            return None
    return cur


def fill_template(tpl: str, mapping: dict) -> str:
    """把模板里的 {占位符}（\\w+）替换为 mapping 的值。

    mapping 缺失的占位符替换为空字符串（保持模板结构）。
    """
    import re as _re

    result = tpl
    for m in _re.finditer(r"\{(\w+)\}", tpl):
        key = m.group(1)
        result = result.replace("{" + key + "}", str(mapping.get(key, "")))
    return result


def guess_image_mime(header: bytes) -> str:
    """按魔数猜图片 mime，默认 image/jpeg。

    识别 JPEG / PNG / GIF / WEBP / BMP 文件头。
    """
    if header[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if header[:4] == b"\x89PNG":
        return "image/png"
    if header[:6] in (b"GIF89a", b"GIF87a"):
        return "image/gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header[:2] == b"BM":
        return "image/bmp"
    return "image/jpeg"


def abs_url(base: str, url: str) -> str:
    """URL 补全：已带 scheme（大小写不敏感）或以 // 开头原样返回，否则 urljoin。

    避免 "HTTP://" 被误当相对路径。
    """
    scheme = (urlsplit(url).scheme or "").lower()
    if scheme in ("http", "https") or url.startswith("//"):
        return url
    return urljoin(base, url)
