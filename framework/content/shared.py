"""内容服务共享前导（B4 拆分自 content.py）：imports/log/常量/数据模型/工具。

所有内容子模块（base/detail/chapter/comic/video）从此模块导入所需名字，
避免相互循环导入。
"""


from __future__ import annotations

import inspect
import json
import logging
import threading
import re as _re
import time
from dataclasses import asdict, dataclass, field
from typing import List, Optional
from urllib.parse import urljoin, urlsplit

log = logging.getLogger("framework.content")

# 精确广告特征：路径段或明确广告词（不用宽泛的 ad_，防误杀正常 URL）
_AD_IMAGE_RE = _re.compile(
    r"(?:/ads?/|/ad-|_ad\.|/advert|/banner|/promo|/ad\.|/ad/|\.ads\.)"
)

from framework.config import SourceConfig
from framework.errors import ContentMissingError
from framework.http import AntiScrapeError, HttpClient
from framework.parser import Parser
from framework.selfcheck import StructureChecker
from framework import utils
from framework.chapter_sort import chapter_label, _extract_chapter_number, _sort_chapters
from framework.health import report_bg_check
from framework.stream_resolver import StreamBudget, StreamCancelledError


def _strip_html_tags(text: str) -> str:
    """去 HTML 标签提取纯文本（解密后含 <p>...</p> 等标签时清理）。

    <p>段落</p> → 段落文本，段落间换行；非标签文本原样保留。
    """
    from lxml.html import fromstring as _html_fromstring
    from lxml.html import tostring as _html_tostring

    # 包一层 div 让 lxml 处理多段落
    try:
        doc = _html_fromstring(f"<div>{text}</div>")
        parts = []
        for el in doc.iter():
            if el.tag == "br":
                parts.append("\n")
            elif el.text and el.tag != "div":
                parts.append(el.text.strip())
        result = "\n".join(p for p in parts if p).strip()
        return result if result else text
    except Exception:
        # lxml 解析失败 → 去标签兜底
        clean = _re.sub(r"<[^>]+>", "\n", text)
        return _re.sub(r"\n{3,}", "\n\n", clean).strip()


@dataclass
class Chapter:
    title: str
    url: str
    source_id: str = ""  # 该集所属播放源标识（换源站：sid 值）；普通源空
    provider: str = ""
    # 该集封面（如 hanime1 详情页侧边栏播放清單内每集缩略图）；
    # 由 content.<type>.list.fields.cover 提取，空则章节界面回退作品封面
    cover: str = ""
    depth: int = 0


@dataclass
class BookStackChapterResult:
    body_html: str
    text: str
    image_urls: List[str] = field(default_factory=list)


@dataclass
class Detail:
    """详情页元数据 + 章节列表。"""

    source_id: str
    content_type: str
    url: str
    title: str = ""
    author: str = ""
    cover: str = ""
    status: str = ""
    summary: str = ""
    actor: str = ""  # 演员（detail.fields.actor 可选，未配置为空）
    tags: List[str] = field(default_factory=list)
    chapters: List[Chapter] = field(default_factory=list)
    # 播放源列表（换源站）：[{sid, name, from_, ps, parse}]；无换源配置时为空
    source_list: List[dict] = field(default_factory=list)
    # 图文集（video 详情页内嵌截图序列等，如 xasiat 的 fancybox screenshots）；
    # 由 detail.fields.gallery 多值选择器提取，详情抽屉以缩略图形式展示
    gallery: List[str] = field(default_factory=list)
    # 站内相关推荐：详情页内嵌的相关影片（如 hanime1 #related-tabcontent 卡片、
    # pornhub relatedVideos JS 变量）。由 detail.related 配置驱动，未配置该块
    # 时保持空列表 → 列表页 UI 回退同源关键词搜索（其他源零影响）。
    # 每项 dict 只含 title/url/cover 三个字符串键，均允许空串（无封面纯文字卡片）。
    related: List[dict] = field(default_factory=list)
