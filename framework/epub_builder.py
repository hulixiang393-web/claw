"""epub 合成模块（epub_builder.py）。

用 EbookLib（已装 0.20）把抓取到的小说/漫画组装成标准 EPUB3 文件：
- novel：每章一个 XHTML 文档（<h1>章标题</h1> + 正文段落），生成目录（toc/nav/ncx）
- comic：每话一个 XHTML 文档，内含 <img> 引用内嵌图片；图片以 EpubImage 写入
框架层纯 Python，不依赖 GUI。下载器逐章累积内容后，最终调用一次合成落盘。
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

try:
    from ebooklib import epub

    HAS_EBOOKLIB = True
except Exception:  # noqa: BLE001 —— 环境缺库时降级（不阻断下载）
    epub = None
    HAS_EBOOKLIB = False

# HTML 转义：正文纯文本 → 安全 XHTML
_ESC = str.maketrans(
    {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}
)


def _esc(text: str) -> str:
    """转义 HTML 特殊字符，防注入/破坏结构。"""
    return str(text or "").translate(_ESC)


def _para_html(text: str) -> str:
    """正文纯文本 → 每段一个 <p> 的 HTML。空行分段。"""
    blocks = [b.strip() for b in str(text or "").split("\n") if b.strip()]
    return "\n".join(f"<p>{_esc(b)}</p>" for b in blocks) if blocks else "<p></p>"


class EpubBuilder:
    """合成一本 epub（novel 或 comic）。"""

    def __init__(self, title: str = "", author: str = "", lang: str = "zh"):
        self.title = title
        self.author = author
        self.lang = lang
        self.chapters: list[dict] = []  # {"title", "file", "html"}
        self.images: dict[str, bytes] = {}  # {epub_path: bytes}
        self.cover: bytes | None = None  # 封面图字节

    # ------------------------------------------------------------------ #
    def add_novel_chapter(self, title: str, text: str, idx: int) -> None:
        """加一章小说：正文纯文本 → XHTML。"""
        file = f"ch_{idx + 1:04d}.xhtml"
        html = f"<h1>{_esc(title)}</h1>\n{_para_html(text)}"
        self.chapters.append({"title": title or f"第{idx + 1}章", "file": file, "html": html})

    @staticmethod
    def _img_ext(raw: bytes) -> str:
        """按 magic bytes 推断图片扩展名（漫画源可能是 png/webp，不能硬编码 jpg）。"""
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if raw[:4] in (b"GIF8", b"GIF8"):  # GIF87a / GIF89a
            return ".gif"
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return ".webp"
        if raw[:2] in (b"\xff\xd8", b"\xff\xd9"):
            return ".jpg"
        return ".jpg"  # 兜底

    def add_comic_chapter(self, title: str, image_bytes: list[bytes], idx: int) -> None:
        """加一话漫画：一话多张图 → 一张 XHTML 引用内嵌图。"""
        file = f"ch_{idx + 1:04d}.xhtml"
        imgs_html = []
        for i, raw in enumerate(image_bytes, 1):
            ext = self._img_ext(raw or b"")
            img_path = f"images/ch{idx + 1:04d}_{i:03d}{ext}"
            self.images[img_path] = raw
            imgs_html.append(f'<img src="{img_path}" alt="" />')
        html = f"<h1>{_esc(title)}</h1>\n" + "\n".join(imgs_html)
        self.chapters.append({"title": title or f"第{idx + 1}话", "file": file, "html": html})

    def set_cover(self, image_bytes: bytes | None) -> None:
        """封面图字节（data URI 解码后传）。"""
        self.cover = image_bytes

    # ------------------------------------------------------------------ #
    def build(self, out_path: str | Path, merge: bool = False) -> Path:
        """组装并写出 epub，返回落盘路径。无 ebooklib 时抛 RuntimeError。

        merge=True：所有章节拼进单个 XHTML（各章仍以 <h1> 分隔），
        spine/toc 单项 —— 对应「合并章节为一个文件」设置。
        """
        out_path = Path(out_path)
        if not HAS_EBOOKLIB:
            raise RuntimeError("未安装 EbookLib，无法合成 epub")
        book = epub.EpubBook()
        book.set_identifier(f"claw-{self.title}-{self.author or 'anon'}")
        book.set_title(self.title or "无题")
        if self.author:
            book.add_author(self.author)
        book.set_language(self.lang)

        # 封面
        if self.cover:
            cov = epub.EpubCover()
            cov.content = self.cover
            book.add_item(cov)

        # 章节
        toc_items = []
        if merge and self.chapters:
            # 单文档模式：所有章节拼进一个 XHTML
            parts = []
            for ch in self.chapters:
                parts.append(ch["html"])
            item = epub.EpubHtml(title=self.title or "正文", file_name="ch_all.xhtml", lang=self.lang)
            item.content = "\n".join(parts)
            book.add_item(item)
            toc_items.append(item)
        else:
            for ch in self.chapters:
                item = epub.EpubHtml(title=ch["title"], file_name=ch["file"], lang=self.lang)
                item.content = ch["html"]
                book.add_item(item)
                toc_items.append(item)

        # 图片
        for path, raw in self.images.items():
            img = epub.EpubImage()
            img.file_name = path
            img.content = raw
            book.add_item(img)

        # 目录 + 导航
        book.toc = toc_items
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())

        # spine
        book.spine = ["nav"] + toc_items

        epub.write_epub(str(out_path), book)
        return out_path


# ------------------------------------------------------------------ #
# 便捷入口
# ------------------------------------------------------------------ #
def build_novel_epub(
    title: str,
    out_path: str | Path,
    chapters: list[tuple[str, str]],
    author: str = "",
    lang: str = "zh",
    cover: bytes | None = None,
    merge: bool = False,
) -> Path:
    """合成一本小说 epub。chapters = [(章标题, 正文纯文本), ...]。

    merge=True：所有章节合并为一个文档。
    """
    b = EpubBuilder(title, author, lang)
    b.set_cover(cover)
    for idx, (t, text) in enumerate(chapters):
        b.add_novel_chapter(t, text, idx)
    return b.build(out_path, merge=merge)


def build_comic_epub(
    title: str,
    out_path: str | Path,
    chapters: list[tuple[str, list[bytes]]],
    author: str = "",
    lang: str = "zh",
    cover: bytes | None = None,
    merge: bool = False,
) -> Path:
    """合成一本漫画 epub。chapters = [(话标题, [图片字节, ...]), ...]。

    merge=True：所有话合并为一个文档（仍保留各话 <h1> 分隔）。
    """
    b = EpubBuilder(title, author, lang)
    b.set_cover(cover)
    for idx, (t, imgs) in enumerate(chapters):
        b.add_comic_chapter(t, imgs, idx)
    return b.build(out_path, merge=merge)
