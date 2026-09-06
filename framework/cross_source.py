"""跨源换源核心逻辑（cross_source.py）。

当阅读界面的当前源失效（线路全挂/站点改版/反爬拦截）时，用当前作品标题去
其他**同类型**源搜索同一作品；按清洗后标题相似度 ≥0.90 返回候选，用户选定后
切换到新源继续阅读。覆盖全部内容类型（视频/漫画/小说），epub 本地书除外。

对应跨源换源设计文档部分 I 第 4 节。
"""

from __future__ import annotations

import difflib
import logging
import re as _re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List

from .search import Search

log = logging.getLogger(__name__)

# 相似度阈值：清洗后 SequenceMatcher.ratio() >= 0.90 才入候选
SIMILARITY_THRESHOLD = 0.90

# 括号及内容（全角/半角）
_BRACKET_RE = _re.compile(r"[（(][^（）()]*[）)]")
# 方括号/尖括号标签（全角/半角）（如【合集】、[独家]）
_ANGLE_BRACKET_RE = _re.compile(r"[【\[][^【】\[\]]*[】\]]")
# 噪音词（大小写不敏感）
_NOISE_RE = _re.compile(r"\b(?:undefined|null|none)\b", _re.IGNORECASE)
# 多余空白
_MULTI_WS_RE = _re.compile(r"\s+")


def clean_title(raw) -> str:
    """清洗标题噪音：去括号及内容、【】、undefined/null/none、多余空白。

    保留「第X季」/「Season X」（作品同一性的一部分，勿删）。
    """
    if not raw:
        return ""
    s = str(raw)
    s = _BRACKET_RE.sub("", s)
    s = _ANGLE_BRACKET_RE.sub("", s)
    s = _NOISE_RE.sub("", s)
    s = _MULTI_WS_RE.sub(" ", s)
    return s.strip()


def similarity(a, b) -> float:
    """清洗后标题相似度（0~1）：先清洗再 SequenceMatcher.ratio()。"""
    return difflib.SequenceMatcher(None, clean_title(a), clean_title(b)).ratio()


@dataclass
class CrossSourceCandidate:
    """跨源换源候选（来自其他同类型源、相似度 ≥ 阈值）。"""

    source_id: str
    source_name: str
    title: str        # 清洗后标题
    url: str
    cover: str
    similarity: float  # 0~1
    content_type: str
    sid: str = ""     # 多线路源用，单线路为空


def _has_search_capability(source) -> bool:
    """源是否配置了搜索接口（endpoints.search 或 api_endpoints.search）。"""
    raw = getattr(source, "raw", None) or {}
    eps = raw.get("endpoints") or {}
    api = raw.get("api_endpoints") or {}
    return bool(eps.get("search") or api.get("search"))


def find_cross_source(manager, content, detail, content_type,
                      concurrent: int = 4, top_n: int = 10) -> List[CrossSourceCandidate]:
    """跨源搜索：并发搜其他同类型、有搜索接口的源，返回相似度≥0.90 候选。

    - manager：SourceManager（取候选源列表）
    - content：Content 实例（复用其 http/parser 驱动 Search）
    - detail：当前作品 Detail（title 为搜索词，source_id 排除自身）
    - concurrent：并发搜索上限；top_n：返回候选上限（按相似度降序取前 N）
    - 单源异常隔离：某源搜索抛错仅记 warning，不影响其他源结果。
    """
    if detail is None or not getattr(detail, "title", ""):
        return []
    keyword = clean_title(detail.title)
    if not keyword:
        return []
    current_id = getattr(detail, "source_id", "") or ""
    http = getattr(content, "_http", None)
    parser = getattr(content, "_parser", None)
    search = Search(http=http, parser=parser)

    sources = [
        s for s in manager.all()
        if s.content_type == content_type
        and s.source_id != current_id
        and _has_search_capability(s)
    ]

    def _worker(source):
        try:
            results = search.search_one_cached(source, keyword, http=http, use_cache=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("[cross_source] 源 %s 搜索失败：%s", source.source_id, exc)
            return []
        out = []
        for r in results or []:
            raw_title = getattr(r, "title", "") or ""
            score = similarity(keyword, raw_title)
            if score >= SIMILARITY_THRESHOLD:
                out.append(CrossSourceCandidate(
                    source_id=source.source_id,
                    source_name=source.source_name,
                    title=clean_title(raw_title),
                    url=getattr(r, "url", "") or "",
                    cover=getattr(r, "cover", "") or "",
                    similarity=round(score, 4),
                    content_type=source.content_type,
                ))
        return out

    candidates: List[CrossSourceCandidate] = []
    with ThreadPoolExecutor(max_workers=max(1, int(concurrent or 1))) as ex:
        futures = [ex.submit(_worker, s) for s in sources]
        for f in futures:
            try:
                candidates.extend(f.result())
            except Exception as exc:  # noqa: BLE001
                log.warning("[cross_source] 候选源并发异常：%s", exc)

    candidates.sort(key=lambda c: c.similarity, reverse=True)
    return candidates[: max(1, int(top_n or 1))]


def clamp_episode_no(current_ep_no, chapters) -> int:
    """视频换源后的目标集序号（0 基）：钳制到 [0, len-1]；无分集返回 0。"""
    n = len(chapters or [])
    if n <= 0:
        return 0
    return min(max(0, int(current_ep_no or 0)), n - 1)


def switch_to_source(content, source, detail, content_type, current_ep_no: int,
                     target: CrossSourceCandidate):
    """切到目标源：抓新源详情，返回 (新 Detail, chapters 列表)。

    - content：Content 实例（fetch_detail）
    - source：目标源 SourceConfig（manager.get(target.source_id) 结果）
    - detail：当前 Detail（保留，非视频类型仍用于上下文）
    - content_type：当前内容类型（video 保留集数序号，0 基）
    - current_ep_no：当前集序号（0 基），video 用 min(max(0, n), len-1)
    - target：选中的 CrossSourceCandidate（url 作详情地址）

    视频集数序号钳制结果供调用方定位到新源对应集（clamp_episode_no）。
    """
    new_detail = content.fetch_detail(source, target.url)
    chapters = list(getattr(new_detail, "chapters", None) or [])
    if content_type == "video":
        clamp_episode_no(current_ep_no, chapters)
    return new_detail, chapters
