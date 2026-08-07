"""按类型下载模块（downloader.py）。

把 Content 抓取到的内容落盘到本地文件：
- novel → 逐章正文 → `{output_dir}/{书名}/{命名}.txt`（UTF-8）
- comic → 每话图片（base64 data URI 或 http 图片 URL）→ `{output_dir}/{书名}/{命名}/img_001.jpg…`
- video → dash 双流经 ffmpeg 合并 → `{output_dir}/{书名}/{命名}.mp4`

命名模板占位符：{title} {chapter_no} {chapter_title} {source_id}；非法文件名字符替换为 _。
续传：skip_existing 为 True 时，目标文件/目录已存在且非空 → 跳过（计入已下载）。
对应 architecture.md §4.5 下载模块。框架层纯 Python，不依赖 GUI。
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from urllib.parse import urlparse

from .content import chapter_label

# 文件名非法字符（Windows + 通用）
_ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t\x00-\x1f]')

# data URI mime → 扩展名
_MIME_EXT = {
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/bmp": ".bmp",
}
_IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def sanitize_filename(name: str) -> str:
    """把文件名中的非法字符替换为 _，去首尾空白与点。"""
    name = _ILLEGAL_FILENAME.sub("_", name or "")
    name = name.strip().strip(".").strip()
    return name or "untitled"


def render_name(template: str, **kw) -> str:
    """渲染命名模板；未提供的占位符留空，非法字符替换为 _。"""
    def _repl(match):
        return sanitize_filename(str(kw.get(match.group(1), "")))

    return re.sub(r"\{(\w+)\}", _repl, template)


class Downloader:
    """按内容类型落盘的下载执行器。

    依赖注入 Content（抓取）+ HttpClient（字节下载）+ SettingsManager（下载设置）。
    """

    def __init__(self, content, http, settings):
        self._content = content
        self._http = http
        self._settings = settings

    # ------------------------------------------------------------------ #
    def output_dir(self) -> Path:
        return Path(self._settings.get("download", "output_dir", "downloads"))

    def naming_template(self) -> str:
        return self._settings.get(
            "download", "naming_template", "{title}_{chapter_no}_{chapter_title}"
        )

    def skip_existing(self) -> bool:
        return bool(self._settings.get("download", "skip_existing", True))

    def book_dir(self, task) -> Path:
        """书名目录（打开文件夹 / 定位落盘根目录用）。"""
        return self.output_dir() / sanitize_filename(task.title or "untitled")

    # ------------------------------------------------------------------ #
    def _chapter_name(self, task, chapter, index: int) -> str:
        """按命名模板渲染章节文件名（无后缀），不足 3 位补零保证排序。"""
        nnn = f"{index + 1:03d}"
        label = sanitize_filename(chapter_label(chapter.title) or f"第{index + 1}章")
        name = render_name(
            self.naming_template(),
            title=sanitize_filename(task.title or "untitled"),
            chapter_no=nnn,
            chapter_title=label,
            source_id=task.source_id,
        )
        return name or nnn

    def _skip_file(self, path: Path) -> bool:
        """续传：文件已存在且非空 → 跳过。"""
        return self.skip_existing() and path.is_file() and path.stat().st_size > 0

    def _skip_dir(self, path: Path) -> bool:
        """续传：漫画话目录已存在且含文件 → 跳过。"""
        return self.skip_existing() and path.is_dir() and any(path.iterdir())

    # ------------------------------------------------------------------ #
    def download_chapter(self, source, task, chapter, index: int) -> int:
        """下载单章，返回写入字节数（0 = 跳过/已存在）。失败抛异常由队列层重试。"""
        ctype = task.content_type
        if ctype == "novel":
            return self._download_novel(source, task, chapter, index)
        if ctype == "comic":
            return self._download_comic(source, task, chapter, index)
        return self._download_video(source, task, chapter, index)

    def _download_novel(self, source, task, chapter, index: int) -> int:
        # 只产出 epub：抓正文累积到 task.epub_chapters，全书写完统一合成
        text = self._content.fetch_chapter(source, chapter.url)
        raw = (text or "").encode("utf-8")
        task.epub_chapters.append((chapter.title or f"第{index+1}章", text or ""))
        return len(raw)

    def _download_comic(self, source, task, chapter, index: int) -> int:
        # 只产出 epub：抓图片字节累积，全书写完统一合成
        images = self._content.fetch_comic_pages(source, chapter.url)
        # 并发下载图片（CDN 慢的站提速），逐张回调更新进度。
        # 用 index→bytes 字典占位，保持图片顺序（as_completed 完成序会打乱页序）。
        from concurrent.futures import ThreadPoolExecutor, as_completed

        img_map = {i: b"" for i in range(len(images))}
        progress_cb = getattr(task, "image_progress_cb", None)
        with ThreadPoolExecutor(max_workers=8) as pool:
            # 防盗链：以章节页 URL 作为正文图 Referer（manben 等图床校验精确章节页）
            futs = {pool.submit(self._image_bytes, img, chapter.url): i for i, img in enumerate(images)}
            for fut in as_completed(futs):
                idx = futs[fut]
                try:
                    b = fut.result()
                except Exception:
                    b = b""
                img_map[idx] = b
                if progress_cb is not None:
                    progress_cb(len(b))
        img_bytes = [img_map[i] for i in range(len(images))]
        task.epub_chapters.append((chapter.title or f"第{index+1}话", img_bytes))
        return sum(len(b) for b in img_bytes)

    def _download_video(self, source, task, chapter, index: int) -> int:
        book_dir = self.book_dir(task)
        book_dir.mkdir(parents=True, exist_ok=True)
        path = book_dir / f"{self._chapter_name(task, chapter, index)}.mp4"
        if self._skip_file(path):
            return 0
        # dash 双流 → ffmpeg 合并成单 mp4（B 站音视频分离）
        video, audio = self._content.fetch_video_streams(
            source, chapter.url, quality=getattr(task, "quality", "") or ""
        )
        if not video:
            raise RuntimeError(f"未获取到播放流：{chapter.title or chapter.url}")
        from .ffmpeg_merger import FFmpegMerger

        # referer 从源配置的请求头取（如 B站 CDN 需要它）；源无此接口则空
        referer = ""
        _rh = getattr(source, "request_headers", None)
        if callable(_rh):
            referer = (_rh() or {}).get("Referer", "") or ""
        merger = FFmpegMerger(referer=referer)
        # m3u8 广告段过滤：视频是 HLS 时下载播放列表 → 剔除广告段 → 本地重写，
        # 用无广告的本地 m3u8 合并（下载不落广告片段）。
        # 【鉴权保护】源带 Referer 时本地 m3u8 无法传给内部 https 段请求（ffmpeg 白名单/
        # headers 不传播），CDN 会拒绝 → 跳过本地过滤，直接用原始 URL 下载（保证能下）。
        if video.startswith(("http://", "https://")) and (
            ".m3u8" in video.lower() or "m3u8" in video.lower()
        ) and not referer:
            filtered = self._filter_m3u8_for_download(source, video, book_dir)
            if filtered:
                video = str(filtered)
        # 合并期间实时上报输出文件大小 → 下载页进度条动起来；
        # cancel_evt：用户点「取消」→ 立即终止 ffmpeg（而非等整集下完）
        progress_cb = getattr(task, "image_progress_cb", None)
        finalized = merger.merge(
            video, audio, path,
            progress_cb=progress_cb,
            cancel_evt=getattr(task, "cancel_evt", None),
            pause_evt=getattr(task, "pause_evt", None),
        )
        return finalized.stat().st_size if finalized.exists() else 0

    def _filter_m3u8_for_download(self, source, video_url: str, book_dir) -> Optional[Path]:
        """下载 m3u8 → adblock 剔除广告段 → 写本地临时文件。失败返回 None（用原 URL）。"""
        from .adblock import adblock_for

        ad = adblock_for(source)
        try:
            text = self._http.get_text(video_url, headers=source.request_headers(), timeout=20, retries=2)
        except Exception:  # noqa: BLE001
            return None
        if not text or not ad.enabled:
            return None
        cleaned = ad.filter_m3u8(text, video_url)
        if cleaned == text:
            return None  # 无广告，用原 URL
        # 写本地无广告 m3u8（相对段 URL 需基于原 m3u8 目录解析，ffmpeg 处理相对路径）
        tmp = book_dir / "_clean.m3u8"
        try:
            tmp.write_text(cleaned, encoding="utf-8")
            return tmp
        except OSError:
            return None

    # ------------------------------------------------------------------ #
    # 批量漫画下载（复用同一个 Chromium 实例，大幅提速）
    # ------------------------------------------------------------------ #
    def download_comic_batch(
        self, source, task, chapters, indices: list[int],
    ) -> dict[int, int]:
        """批量下载漫画：一次 Playwright 渲染多话，再逐话落盘。

        返回 {index: bytes_written}（仅跳过/失败话不含）。
        调用方（DownloadQueue）按返回值累计 task.done / bytes_written。
        """
        if not chapters or not indices:
            return {}
        # 只产出 epub：批量渲染后把图片字节累积到 task.epub_chapters

        # 批量渲染（一次浏览器启动）
        chapter_urls = [ch.url for ch in chapters]
        to_download = list(zip(indices, chapters))
        try:
            result_map = self._content.fetch_comic_pages_batch(
                source, chapter_urls
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"批量 Playwright 渲染失败: {exc}") from exc

        # 逐话累积图片字节
        out: dict[int, int] = {}
        for idx, ch in to_download:
            images = result_map.get(ch.url)
            if images is None:
                raise RuntimeError(f"渲染失败：{ch.title or ch.url}")
            # 防盗链：以章节页 URL 作为正文图 Referer（manben 等图床校验精确章节页）
            img_bytes = [self._image_bytes(img, ch.url) for img in images]
            task.epub_chapters.append((ch.title or f"第{idx+1}话", img_bytes))
            out[idx] = sum(len(b) for b in img_bytes)
        # 未处理的话标记 0
        for idx in indices:
            if idx not in out:
                out[idx] = 0
        return out

    # ------------------------------------------------------------------ #
    @staticmethod
    def _img_ext(img: str) -> str:
        """从 data URI mime 或 URL 后缀推断图片扩展名，默认 .jpg。"""
        if img.startswith("data:"):
            mime = img[5 : img.find(";") or img.find(",")]
            return _MIME_EXT.get(mime, ".jpg")
        ext = Path(urlparse(img).path).suffix.lower()
        return ext if ext in _IMG_EXTS else ".jpg"

    def _save_image(self, img: str, path: Path) -> int:
        """保存单张图：data URI 解码 / http URL 下载。返回字节数。"""
        raw = self._image_bytes(img)
        path.write_bytes(raw)
        return len(raw)

    def _image_bytes(self, img: str, referer: str = "") -> bytes:
        """单张图字节：data URI 解码 / http URL 下载。不落盘（供 epub 累积）。

        受 network.max_bytes_per_image 上限约束：超过则跳过该图并告警（不中断整本）。
        referer: 防盗链图床所需的 Referer（漫画正文图 = 当前章节页 URL，
        如 manben 的 manhua*.cdndm5.com 校验精确章节页 Referer）。
        """
        if img.startswith("data:"):
            _, b64 = img.split(",", 1)
            return base64.b64decode(b64)
        headers = {"Referer": referer} if referer else None
        raw = self._http.get_bytes(img, headers=headers)
        limit = int(self._settings.get("network", "max_bytes_per_image", 0) or 0)
        if limit > 0 and len(raw) > limit:
            import logging

            logging.getLogger(__name__).warning(
                "图片超过大小上限 %dB，跳过：%s", limit, img[:120]
            )
            return b""
        return raw

    # ------------------------------------------------------------------ #
    def finalize_epub(self, task) -> Path:
        """把 task.epub_chapters 累积的内容合成为单本 epub 落盘。

        novel: chapters = [(title, text), ...]
        comic: chapters = [(title, [img_bytes, ...]), ...]
        返回 epub 路径。epub_chapters 为空时抛 RuntimeError。
        """
        from .epub_builder import build_comic_epub, build_novel_epub

        if not task.epub_chapters:
            raise RuntimeError("无章节内容，无法合成 epub")
        book_dir = self.book_dir(task)
        book_dir.mkdir(parents=True, exist_ok=True)
        out = book_dir / f"{sanitize_filename(task.title or 'untitled')}.epub"
        if self._skip_file(out):
            return out  # 已存在（续传）
        ctype = task.content_type
        title = task.title or "untitled"
        merge = bool(self._settings.get("download", "merge_chapters_into_one_file", False))
        if ctype == "comic":
            build_comic_epub(title, out, task.epub_chapters, merge=merge)
        else:
            build_novel_epub(title, out, task.epub_chapters, merge=merge)
        return out
