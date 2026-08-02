"""内容模块（content.py）。

消费 endpoints.detail 与 endpoints.content：
- fetch_detail(source, url)  → 详情元数据 + 章节列表
- fetch_chapter(source, url) → 单章正文（按 content_type 分派）

对应 design.md §5.2 与 ui-discover.md 详情抽屉、ui-reader.md。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .config import SourceConfig
from .errors import ContentMissingError
from .http import HttpClient
from .parser import Parser
from .selfcheck import StructureChecker
from .decrypter import Decrypter  # noqa: F401  (类型提示用)


@dataclass
class Chapter:
    title: str
    url: str


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
    tags: List[str] = field(default_factory=list)
    chapters: List[Chapter] = field(default_factory=list)


class Content:
    def __init__(
        self,
        http: HttpClient,
        parser: Parser,
        checker: StructureChecker,
        decrypter: Optional["Decrypter"] = None,
    ):
        self._http = http
        self._parser = parser
        self._checker = checker
        self._decrypter = decrypter

    # ------------------------------------------------------------------ #
    def _headers(self, source: SourceConfig) -> dict:
        return source.transports().get("headers") or {}

    def _timeout(self, source: SourceConfig) -> float:
        return float(source.transports().get("timeout") or 10)

    def _retries(self, source: SourceConfig) -> int:
        return int(source.transports().get("retries") or 3)

    def _interval_ms(self, source: SourceConfig) -> int:
        return int(source.transports().get("interval_ms") or 0)

    def _abs_url(self, source: SourceConfig, url: str) -> str:
        from urllib.parse import urljoin

        return urljoin(source.base_url, url) if not url.startswith("http") else url

    def _get(self, source: SourceConfig, url: str) -> str:
        abs_url = self._abs_url(source, url)
        return self._http.get_text(
            abs_url,
            headers=self._headers(source),
            timeout=self._timeout(source),
            retries=self._retries(source),
            interval_ms=self._interval_ms(source),
        )

    # ------------------------------------------------------------------ #
    def fetch_detail(self, source: SourceConfig, url: str) -> Detail:
        """抓取详情页：元数据 + 章节列表。"""
        self._checker.check(source, self._abs_url(source, url))
        html = self._get(source, url)
        doc = self._parser.parse(html)

        detail_cfg = source.get_detail_config()
        fields = detail_cfg.get("fields") or {}
        title = self._parser.extract_first(doc, fields.get("title"))
        # 书名修正：若 title 像是站点名（短/非书名），尝试 og:novel:book_name
        book_fields = fields.get("book_name")
        if book_fields:
            book = self._parser.extract_first(doc, book_fields)
            if book:
                title = book
        detail = Detail(
            source_id=source.source_id,
            content_type=source.content_type,
            url=url,
            title=title,
            author=self._parser.extract_first(doc, fields.get("author")),
            cover=self._parser.extract_first(
                doc, fields.get("cover"), source.base_url
            ),
            status=self._parser.extract_first(doc, fields.get("status")),
            summary=self._parser.extract_first(doc, fields.get("summary")),
        )
        # 标签（可空）
        tags = self._parser.extract(doc, fields.get("tags"))
        detail.tags = tags

        # 章节列表（按类型取 content 配置，传书名用于标题清理）
        detail.chapters = self._fetch_chapters(source, doc, book_title=detail.title)
        return detail

    # ------------------------------------------------------------------ #
    def _fetch_chapters(self, source: SourceConfig, doc, book_title: str = "") -> List[Chapter]:
        """从详情页提取章节列表。按 content_type 读 content 配置。

        支持源配置：
        - list.chapter_order: "desc" → HTML 倒序，反转成正序（asc）
        - list.title_clean: true    → 从章节标题移除书名
        """
        content_cfg = source.raw.get("endpoints", {}).get("content") or {}
        if source.content_type == "novel":
            block = content_cfg.get("chapter") or {}
        elif source.content_type == "comic":
            block = content_cfg.get("page") or {}
        else:
            block = content_cfg.get("episode") or {}
        list_cfg = block.get("list") or {}
        root_sel = list_cfg.get("root_selector")
        fields = list_cfg.get("fields") or {}
        if not root_sel:
            return []

        items = self._parser.parse_items(doc, root_sel, fields, source.base_url)
        chapters: List[Chapter] = []
        seen = set()

        def clean_title(t: str) -> str:
            """从章节标题移除书名 + 清理多余分隔符/后缀词。"""
            t = t.strip()
            if not t:
                return t
            if book_title:
                t = t.replace(book_title, "")
            import re as _re
            # 移除《》及其中的书名残留（含空《》）
            t = _re.sub(r"《[^》]*》", "", t)
            # 移除常见后缀词
            for suffix in ("最新章节", "全文阅读", "全文免费阅读", "最新", "TXT", "txt"):
                t = t.replace(suffix, "")
            # 清理孤立分隔符
            t = t.replace(" - ", " ").strip(" -：:　 ").strip()
            t = _re.sub(r"\s+", " ", t)
            return t

        for it in items:
            url = it.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            title = it.get("title", "")
            if list_cfg.get("title_clean"):
                title = clean_title(title)
            chapters.append(Chapter(title=title or f"第{len(chapters)+1}章", url=url))

        # 倒序反转（HTML 倒序 → 正序）
        order = list_cfg.get("chapter_order", "asc")
        if order == "desc":
            chapters.reverse()
        return chapters

    # ------------------------------------------------------------------ #
    def fetch_chapter(self, source: SourceConfig, url: str) -> str:
        """抓取单章正文。按类型取正文选择器。"""
        self._checker.check(source, self._abs_url(source, url))
        html = self._get(source, url)
        doc = self._parser.parse(html)

        content_cfg = source.raw.get("endpoints", {}).get("content") or {}
        if source.content_type == "novel":
            block = content_cfg.get("chapter") or {}
        elif source.content_type == "comic":
            block = content_cfg.get("page") or {}
        else:
            block = content_cfg.get("episode") or {}
        body = block.get("body") or {}
        selector = body.get("selector")
        if not selector:
            raise ContentMissingError("源未配置正文选择器", source_id=source.source_id)

        paragraphs = self._parser.extract(doc, selector)
        if not paragraphs:
            # 正文选择器未命中：可能是混淆/加密正文，尝试解密
            return self._decrypt_chapter(source, html, url)
        return "\n".join(paragraphs)

    def _decrypt_chapter(self, source: SourceConfig, html: str, url: str) -> str:
        """正文被混淆时的解密路径（源配置 decryption 驱动）。"""
        dec_cfg = source.raw.get("decryption") or {}
        if not dec_cfg:
            raise ContentMissingError(
                f"未解析到正文内容（{url}）", source_id=source.source_id
            )
        if self._decrypter is None:
            raise ContentMissingError(
                f"正文需解密但未配置解密器（{url}）", source_id=source.source_id
            )
        content_cfg = dec_cfg.get("targets", {}).get("content") or {}
        source_type = content_cfg.get("source", "obfuscated_script")
        try:
            if source_type == "obfuscated_script":
                # 从混淆脚本提取 base64 并解码（extract_base64_blocks 已解码）
                text = self._decrypter.extract_base64_blocks(html)
                if not text:
                    raise ContentMissingError(
                        f"混淆正文提取为空（{url}）", source_id=source.source_id
                    )
                return text
            # 其他 source 类型：把原始内容交给 decrypt（按 strategy 处理）
            return self._decrypter.decrypt(source, html, "content")
        except ContentMissingError:
            raise
        except Exception as exc:
            raise ContentMissingError(
                f"正文解密失败（{url}）：{exc}", source_id=source.source_id
            )

    # ------------------------------------------------------------------ #
    def fetch_comic_pages(self, source: SourceConfig, chapter_url: str) -> List[str]:
        """漫画：抓取一话的全部分页图片 URL。

        对应 endpoints.content.page：
        - list.fields.url → 图片 URL（可带 data-src/data-original attr）
        - list.paginator  → 分页（长话翻页）
        """
        self._checker.check(source, self._abs_url(source, chapter_url))
        html = self._get(source, chapter_url)
        doc = self._parser.parse(html)

        content_cfg = source.raw.get("endpoints", {}).get("content") or {}
        block = content_cfg.get("page") or {}
        # 图片列表优先 body，兼容旧 list
        list_cfg = block.get("body") or block.get("list") or {}
        root_sel = list_cfg.get("root_selector")
        fields = list_cfg.get("fields") or {}
        if not root_sel or not fields.get("url"):
            raise ContentMissingError(
                "源未配置漫画图片规则（content.page）",
                source_id=source.source_id,
            )

        # 图片字段可能用 data-src / data-original 懒加载属性
        url_sel = fields.get("url")
        if url_sel and isinstance(url_sel, dict):
            attr = url_sel.get("attr")
            if attr in (None, "src"):
                # 尝试 data-src / data-original 兜底
                for lazy_attr in ("data-src", "data-original", "src"):
                    sel = dict(url_sel)
                    sel["attr"] = lazy_attr
                    imgs = self._parser.extract(doc, sel, source.base_url)
                    if imgs:
                        return self._filter_ad_images(imgs, source)
        # 常规提取
        imgs = self._parser.extract(doc, url_sel, source.base_url)
        return self._filter_ad_images(imgs, source)

    def fetch_video_episode(self, source: SourceConfig, episode_url: str) -> str:
        """视频：抓取单集播放地址（解密后返回真实地址）。

        对应 endpoints.content.episode：
        - play_url.selector → 播放地址提取
        - decryption        → 解密（B站 wbi 签名等）
        """
        self._checker.check(source, self._abs_url(source, episode_url))
        html = self._get(source, episode_url)
        doc = self._parser.parse(html)

        content_cfg = source.raw.get("endpoints", {}).get("content") or {}
        block = content_cfg.get("episode") or {}
        play_cfg = block.get("play_url") or {}
        play_sel = play_cfg.get("selector")
        if not play_sel:
            raise ContentMissingError(
                "源未配置播放地址规则（content.episode.play_url）",
                source_id=source.source_id,
            )
        play = self._parser.extract_first(doc, play_sel, source.base_url)
        if not play:
            raise ContentMissingError(
                f"未解析到播放地址（{episode_url}）", source_id=source.source_id
            )
        # 解密（如 wbi 签名、加密地址）
        if self._decrypter is not None:
            return self._decrypter.decrypt(source, play, "video_url")
        return play

    @staticmethod
    def _filter_ad_images(images: List[str], source: SourceConfig) -> List[str]:
        """剔除广告图（URL 含 ad/banner/promo 等标记）。"""
        ad_markers = ("/ad", "ad_", "banner", "promo", "advert", ".gif")
        filtered = []
        for url in images:
            low = url.lower()
            if any(m in low for m in ad_markers):
                continue
            filtered.append(url)
        return filtered
