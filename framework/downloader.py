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
from urllib.parse import urljoin, urlparse

from .content import chapter_label
from .ffmpeg_merger import MergeCancelled, MergePaused

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
        progress_cb = getattr(task, "image_progress_cb", None)
        cancel_evt = getattr(task, "cancel_evt", None)
        pause_evt = getattr(task, "pause_evt", None)
        # 统一分配：yt-dlp 引擎源直接用 yt-dlp 原生下载（签名/请求头/合并最稳）；
        # 其余源 FFmpegMerger 合并；取流为空或合并失败 → yt-dlp 兜底直接下载该集
        # （yt-dlp 能自动处理 m3u8/签名/防盗链，作为所有视频源的安全网）。
        quality = getattr(task, "quality", "") or ""
        if self._uses_ytdlp_engine(source):
            fmt = self._content._ytdlp_format(self._episode_api_cfg(source), quality)
            return self._download_via_ytdlp(chapter.url, path, fmt, progress_cb, cancel_evt, pause_evt)
        # dash 双流 → ffmpeg 合并成单 mp4（B 站音视频分离）
        video, audio = self._content.fetch_video_streams(
            source, chapter.url, quality=quality
        )
        if video:
            is_hls = ".m3u8" in video.lower() or "m3u8" in video.lower()
            # HLS（m3u8）优先自研逐段下载：每段带源 Referer + 重试，规避
            # ffmpeg HLS 分段请求不带 Referer（CDN 403）与单段失败即中止（截断）。
            # 失败（非取消/暂停）回退 FFmpegMerger（再失败走 yt-dlp 兜底）。
            if is_hls and not audio:
                try:
                    return self._download_hls(
                        video, self._source_headers(source), path,
                        progress_cb, cancel_evt, pause_evt,
                    )
                except (MergeCancelled, MergePaused):
                    raise
                except Exception:  # noqa: BLE001 自研 HLS 失败 → 回退 FFmpegMerger
                    pass
            # 直接文件（mp4/flv/webm，无分离音轨）→ 并行 Range 分段下载提速：
            # 单连接受 CDN 每连接限速（~400KB/s），多连接分段可成倍提升。
            if not audio and self._is_direct_file(video):
                try:
                    return self._download_direct_parallel(
                        video, self._source_headers(source), path,
                        progress_cb, cancel_evt, pause_evt,
                    )
                except (MergeCancelled, MergePaused):
                    raise
                except Exception:  # noqa: BLE001 不支持 Range/失败 → 回退 FFmpegMerger
                    pass
            try:
                return self._merge_video_ffmpeg(source, task, path, video, audio)
            except (MergeCancelled, MergePaused):
                raise  # 用户取消/暂停是意图，不透传 yt-dlp 兜底
            except Exception:  # noqa: BLE001 合并失败 → 用视频流 URL 走 yt-dlp 兜底
                # 直接下 m3u8/mp4 流（yt-dlp 泛 HLS/HTTP 提取，绕开 ffmpeg 合并失败）
                return self._download_via_ytdlp(video, path, "best", progress_cb, cancel_evt, pause_evt)
        # 取流为空 → 用章节页 URL 兜底（yt-dlp 能识别该站则下，否则报错明确）
        return self._download_via_ytdlp(chapter.url, path, "bestvideo+bestaudio/best", progress_cb, cancel_evt, pause_evt)

    @staticmethod
    def _is_direct_file(url: str) -> bool:
        """URL 是否为直接媒体文件（mp4/flv/webm 等，非 HLS/mpd）。"""
        path = urlparse(url).path.lower()
        return path.endswith((".mp4", ".flv", ".webm", ".mkv", ".mov", ".m4v", ".ts", ".m4a"))

    @staticmethod
    def _source_headers(source) -> dict:
        """取源配置合并后的全量请求头（transports.headers + cookie）。"""
        headers = {}
        _rh = getattr(source, "request_headers", None)
        if callable(_rh):
            try:
                headers = dict(_rh() or {})
            except Exception:  # noqa: BLE001
                headers = {}
        return headers

    def _download_direct_parallel(
        self, url: str, headers: dict, path,
        progress_cb=None, cancel_evt=None, pause_evt=None,
        workers: int = 8,
    ) -> int:
        """直接文件并行 Range 分段下载，提速单连接 CDN 限速。

        探测 Content-Range 大小 + Range 支持 → 切成多段并行拉（每段 ≤2MB
        内存）→ 按序合并。CDN 每连接限速时 8 连接可成倍提升吞吐。
        不支持 Range / 探测失败抛异常，由调用方回退 FFmpegMerger。
        支持进度上报（合并总进度）/ 取消 / 暂停。
        """
        from concurrent.futures import ThreadPoolExecutor
        import threading
        import time

        path = Path(path)  # 兼容 str/Path 入参（内部用 path.parent/name）
        probe = self._probe_range(url, headers)
        if not probe:
            raise RuntimeError("无法探测文件大小（可能不支持 Range）")
        total, supports_range = probe
        if not supports_range or total <= 0:
            raise RuntimeError("服务端不支持 Range 分段下载")
        # 分段：每段 ≤2MB，目标 2×workers 段（管道化），总段数太多时放宽。
        # 小段：进度按段细粒度上报（UI 条平滑）+ 取消只需等少量在飞段（响应快）；
        # 8MB 段在 CDN 每连接 ~0.6MB/s 时单段要 13s，取消要等整段下完，体验差。
        n_ideal = max(workers * 2, 4)
        chunk = max(256 * 1024, min(total // n_ideal, 2 * 1024 * 1024))
        ranges = []
        start = 0
        while start < total:
            end = min(start + chunk - 1, total - 1)
            ranges.append((start, end))
            start = end + 1
        base = path.parent
        stem = path.name
        parts = [base / f"{stem}.part{i}" for i in range(len(ranges))]

        done = [0] * len(ranges)
        lock = threading.Lock()

        def _check():
            if cancel_evt is not None and cancel_evt.is_set():
                raise MergeCancelled("下载已取消")
            if pause_evt is not None and pause_evt.is_set():
                raise MergePaused("下载已暂停")

        def _dl(idx):
            s, e = ranges[idx]
            h = dict(headers or {})
            h["Range"] = f"bytes={s}-{e}"
            data = self._http.get_bytes(url, headers=h, timeout=30, retries=2)
            if not data:
                raise RuntimeError(f"分段 {idx} 空响应")
            return idx, data

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                # 限量在飞任务（≤workers 段）：取消/暂停时只需等在跑 ≤workers 段
                # （每段 ≤2MB）即返回，避免旧实现"全部提交后取消要等整文件下完"。
                futs = {}
                next_submit = 0
                n_total = len(ranges)
                completed = 0
                while completed < n_total:
                    while len(futs) < workers and next_submit < n_total:
                        f = pool.submit(_dl, next_submit)
                        futs[f] = next_submit
                        next_submit += 1
                    _check()
                    finished = [f for f in list(futs) if f.done()]
                    if not finished:
                        time.sleep(0.05)
                        continue
                    for f in finished:
                        idx, data = f.result()  # 有异常在此抛出
                        parts[idx].write_bytes(data)
                        with lock:
                            done[idx] = len(data)
                            if progress_cb is not None:
                                progress_cb(sum(done))
                        del futs[f]
                        completed += 1
        except (MergeCancelled, MergePaused):
            self._cleanup_parts(parts)
            raise
        except Exception:
            self._cleanup_parts(parts)
            raise
        # 按序合并
        try:
            with open(path, "wb") as out:
                for p in parts:
                    out.write(p.read_bytes())
        finally:
            self._cleanup_parts(parts)
        if path.exists() and path.stat().st_size > 0:
            return path.stat().st_size
        raise RuntimeError("并行下载无输出")

    def _probe_range(self, url: str, headers: dict):
        """探测直接文件的 Content-Length + Range 支持。返回 (total, supports_range) 或 None。"""
        sess = getattr(self._http, "_session", None)
        if sess is None:
            return None
        h = dict(headers or {})
        h["Range"] = "bytes=0-0"
        try:
            resp = sess.get(url, headers=h, timeout=15)
            try:
                if resp.status_code == 206:
                    cr = resp.headers.get("Content-Range") or ""
                    if "/" in cr:
                        return int(cr.rsplit("/", 1)[1]), True
                elif resp.status_code == 200:
                    cl = resp.headers.get("Content-Length")
                    if cl:
                        return int(cl), False
            finally:
                resp.close()
        except Exception:  # noqa: BLE001
            return None
        return None

    def _cleanup_parts(self, parts) -> None:
        for p in parts:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _episode_api_cfg(source) -> dict:
        """取源配置 api_endpoints.episode 段。"""
        api = (source.raw or {}).get("api_endpoints") or {}
        return (api.get("episode") or {}) or {}

    @staticmethod
    def _uses_ytdlp_engine(source) -> bool:
        """源是否用 yt-dlp 引擎（api_endpoints.episode.engine == ytdlp）。"""
        return Downloader._episode_api_cfg(source).get("engine") == "ytdlp"

    def _download_via_ytdlp(self, url: str, path, fmt: str, progress_cb, cancel_evt, pause_evt) -> int:
        """yt-dlp 直接下载该集（含合并成 mp4），支持进度/取消/暂停。"""
        from .ytdlp import Ytdlp

        Ytdlp().download_file(
            url, str(path), fmt=fmt,
            progress_cb=progress_cb, cancel_evt=cancel_evt, pause_evt=pause_evt,
        )
        return path.stat().st_size if path.exists() else 0

    def _merge_video_ffmpeg(self, source, task, path, video: str, audio: str) -> int:
        """FFmpegMerger 合并（含 m3u8 广告段过滤 + 通用请求头）。"""
        from .ffmpeg_merger import FFmpegMerger

        # 通用请求头：取源配置合并后的全量头（transports.headers + cookie → Referer/UA/Cookie）。
        # CDN 校验 Referer/UA 的源（如 B站、HLS 站）靠它通过；源无此接口则空。
        headers = self._source_headers(source)
        referer = headers.get("Referer", "") or ""
        merger = FFmpegMerger(headers=headers)
        # m3u8 广告段过滤：视频是 HLS 时下载播放列表 → 剔除广告段 → 本地重写，
        # 用无广告的本地 m3u8 合并（下载不落广告片段）。
        # 【鉴权保护】源带 Referer 时本地 m3u8 无法传给内部 https 段请求（ffmpeg 白名单/
        # headers 不传播），CDN 会拒绝 → 跳过本地过滤，直接用原始 URL 下载（保证能下）。
        if video.startswith(("http://", "https://")) and (
            ".m3u8" in video.lower() or "m3u8" in video.lower()
        ) and not referer:
            filtered = self._filter_m3u8_for_download(source, video, path.parent)
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
    # HLS（m3u8）逐段下载：规避 ffmpeg HLS 不带 Referer + 单段失败截断
    # ------------------------------------------------------------------ #
    def _download_hls(
        self, url, headers, path,
        progress_cb=None, cancel_evt=None, pause_evt=None,
    ) -> int:
        """HLS（m3u8）逐段下载 → ffmpeg 本地合并成 mp4。

        ffmpeg 直连 HLS 的缺陷（xuandm 实测 ~9 分钟截断）：a) -headers 不传播
        给分段请求，CDN 校验 Referer 时 403；b) 单段失败即中止，产出截断文件。
        这里用 HttpClient 逐段下载：每段带源 Referer + 最多 5 次重试 + 递增
        退避（间歇 403 重试可过），写本地 m3u8 + 本地 key，由 ffmpeg 完成
        AES-128 解密与 -c copy 合并。彻底失败的分段写空 TS 包占位，不中断整集。
        """
        import shutil
        import tempfile
        from urllib.parse import urljoin

        path = Path(path)
        hls_dir = tempfile.mkdtemp(prefix="claw_hls_")
        try:
            playlist_url = url
            text = self._http.get_text(url, headers=headers, timeout=20, retries=2)
            if not text or "#EXTM3U" not in text:
                raise RuntimeError("HLS 播放列表无效")
            # master 多画质 → 取第一个 variant 子列表
            if "#EXT-X-STREAM-INF" in text:
                variant = self._first_variant_url(text, url)
                if not variant:
                    raise RuntimeError("HLS master 播放列表无可用画质")
                playlist_url = variant
                text = self._http.get_text(variant, headers=headers, timeout=20, retries=2)
                if not text or "#EXTM3U" not in text:
                    raise RuntimeError("HLS variant 播放列表无效")

            segs, key_attrs, target_dur = self._parse_hls_segments(text, playlist_url)
            if not segs:
                raise RuntimeError("HLS 播放列表无分段")

            # AES-128 密钥（下载到本地，本地 m3u8 引用它）
            key_local = None
            if key_attrs:
                key_url = urljoin(playlist_url, key_attrs.get("URI", ""))
                key_data = self._http.get_bytes(key_url, headers=headers, timeout=20, retries=2)
                key_local = Path(hls_dir) / "key.key"
                key_local.write_bytes(key_data)

            # 逐段下载：带 Referer + 重试；彻底失败跳过（占位），不中断整集
            seg_items = []  # (extinf, 本地文件名, 字节数)
            total = 0
            for idx, (extinf, seg_url) in enumerate(segs):
                self._check_abort(cancel_evt, pause_evt)
                seg_file = Path(hls_dir) / f"seg_{idx + 1:05d}.ts"
                n = self._download_seg_retry(seg_url, headers, seg_file, cancel_evt, pause_evt)
                seg_items.append((extinf, seg_file.name, n))
                total += n
                if progress_cb is not None:
                    progress_cb(total)

            # 重写本地 m3u8（key/分段指本地文件），ffmpeg 解密 + remux
            local_m3u8 = Path(hls_dir) / "playlist.m3u8"
            self._write_hls_playlist(local_m3u8, seg_items, key_attrs, key_local, target_dur)
            self._remux_hls(local_m3u8, path, progress_cb, cancel_evt, pause_evt)

            if path.exists() and path.stat().st_size > 0:
                return path.stat().st_size
            raise RuntimeError("HLS 下载无输出文件")
        finally:
            shutil.rmtree(hls_dir, ignore_errors=True)

    @staticmethod
    def _check_abort(cancel_evt, pause_evt) -> None:
        """取消/暂停信号 → 抛 MergeCancelled/MergePaused（供下载循环检查）。"""
        if cancel_evt is not None and cancel_evt.is_set():
            raise MergeCancelled("下载已取消")
        if pause_evt is not None and pause_evt.is_set():
            raise MergePaused("下载已暂停")

    @staticmethod
    def _parse_hls_attrs(line: str) -> dict:
        """解析 #EXT-X-KEY:METHOD=AES-128,URI="...",IV=0x... 的属性串。"""
        attrs = {}
        for m in re.finditer(r'([A-Za-z0-9_-]+)=(?:"([^"]*)"|([^",\s]*))', line):
            key, quoted, bare = m.groups()
            attrs[key] = quoted if quoted is not None else (bare or "")
        return attrs

    @staticmethod
    def _first_variant_url(playlist_text: str, base_url: str):
        """master 播放列表 → 第一个 #EXT-X-STREAM-INF 后的 variant URL。"""
        lines = playlist_text.splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith("#EXT-X-STREAM-INF"):
                for j in range(i + 1, len(lines)):
                    cand = lines[j].strip()
                    if cand and not cand.startswith("#"):
                        return urljoin(base_url, cand)
        return None

    @staticmethod
    def _parse_hls_segments(playlist_text: str, base_url: str):
        """解析媒体播放列表 → ([(extinf, seg_url), ...], key_attrs|None, target_dur)。

        处理 TS 分段 + 单一 AES-128 key（xuandm 场景）。key 可在任意 EXTINF 前，
        取首个遇到的 key 生效；key rotation / fMP4（EXT-X-MAP）等复杂 HLS 不支持，
        解析异常时调用方回退 FFmpegMerger。
        """
        lines = playlist_text.splitlines()
        segs = []
        key_attrs = None
        target_dur = 0.0
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i].strip()
            if line.startswith("#EXT-X-KEY:"):
                attrs = Downloader._parse_hls_attrs(line)
                if (attrs.get("METHOD") or "NONE").upper() != "NONE":
                    key_attrs = attrs
            elif line.startswith("#EXT-X-TARGETDURATION:"):
                try:
                    target_dur = float(line.split(":", 1)[1].strip().split(",", 1)[0])
                except (ValueError, IndexError):
                    target_dur = 0.0
            elif line.startswith("#EXTINF:"):
                dur_raw = line.split(":", 1)[1].strip().split(",", 1)[0]
                try:
                    dur = float(dur_raw)
                except ValueError:
                    dur = 0.0
                target_dur = max(target_dur, dur)
                i += 1
                while i < n and (not lines[i].strip() or lines[i].strip().startswith("#")):
                    i += 1
                if i < n:
                    segs.append((dur, urljoin(base_url, lines[i].strip())))
            elif line and not line.startswith("#"):
                segs.append((0.0, urljoin(base_url, line)))
            i += 1
        return segs, key_attrs, target_dur

    def _download_seg_retry(
        self, seg_url, headers, seg_file, cancel_evt, pause_evt, retries: int = 5,
    ) -> int:
        """下载单段：最多 retries 次 + 递增退避（0.5s*attempt）。返回字节数，彻底失败 0。"""
        import time as _time

        h = dict(headers or {})
        data = None
        for attempt in range(1, retries + 1):
            self._check_abort(cancel_evt, pause_evt)
            try:
                data = self._http.get_bytes(seg_url, headers=h, timeout=30, retries=0)
                break
            except Exception:  # noqa: BLE001 间歇 403/超时 → 退避重试
                if attempt < retries:
                    _time.sleep(0.5 * attempt)
        if not data:
            return 0
        seg_file.write_bytes(data)
        return len(data)

    @staticmethod
    def _write_hls_playlist(local_m3u8: Path, seg_items, key_attrs, key_local, target_dur) -> None:
        """重写本地 m3u8：key 指本地 key + 分段指本地文件 + ENDLIST。

        彻底失败的分段（字节 0）写 188B 空 TS 包占位，避免 ffmpeg 因文件缺失中止。
        """
        lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
        lines.append(f"#EXT-X-TARGETDURATION:{int(target_dur) + 1 if target_dur else 10}")
        lines.append("#EXT-X-MEDIA-SEQUENCE:0")
        if key_local is not None and key_attrs:
            key_line = '#EXT-X-KEY:METHOD=AES-128,URI="key.key"'
            iv = key_attrs.get("IV")
            if iv:
                key_line += f",IV={iv}"
            lines.append(key_line)
        for extinf, seg_name, nbytes in seg_items:
            dur = extinf if extinf and extinf > 0 else (target_dur or 10.0)
            lines.append(f"#EXTINF:{dur:.3f},")
            if not nbytes:
                # 空 TS 包占位：文件缺失会让 ffmpeg 中止整集
                (local_m3u8.parent / seg_name).write_bytes(
                    b"\x47\x1f\xff\x10" + b"\xff" * 184
                )
            lines.append(seg_name)
        lines.append("#EXT-X-ENDLIST")
        local_m3u8.write_text("\n".join(lines), encoding="utf-8")

    def _remux_hls(self, local_m3u8: Path, path, progress_cb, cancel_evt, pause_evt) -> None:
        """ffmpeg 合并本地 m3u8 → mp4（-c copy，AES 解密由 ffmpeg 完成）。

        本地输入放行 crypto 协议；remux 阶段 cancel/pause → kill 进程。
        """
        import os
        import subprocess
        import tempfile
        import threading
        import time as _time
        from .ffmpeg_merger import _find_ffmpeg

        ffmpeg = _find_ffmpeg()
        out_path = Path(path)
        # -allowed_extensions ALL：本地 m3u8 引用的 AES key 文件是 .key 后缀，
        # 不在 ffmpeg HLS 默认 allowed_extensions 白名单（会拒读 → 解密失败）。
        # 播放列表是我们本地重写的（只引本地文件），放开无安全风险。
        cmd = [
            ffmpeg, "-y",
            "-allowed_extensions", "ALL",
            "-protocol_whitelist", "file,crypto,data,http,https,tcp,tls",
            "-i", str(local_m3u8),
            "-c", "copy", str(out_path),
        ]
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".log", delete=False) as tf:
            log_path = tf.name
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=open(log_path, "w"),
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
            stop = threading.Event()

            def _check():
                if cancel_evt is not None and cancel_evt.is_set():
                    stop.set()
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    raise MergeCancelled("合并已取消")
                if pause_evt is not None and pause_evt.is_set():
                    stop.set()
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    raise MergePaused("合并已暂停")

            def _monitor():
                try:
                    while proc.poll() is None and not stop.is_set():
                        _check()
                        if progress_cb is not None:
                            try:
                                progress_cb(out_path.stat().st_size if out_path.exists() else 0)
                            except Exception:
                                pass
                        _time.sleep(0.3)
                except (MergeCancelled, MergePaused):
                    return
                except Exception:
                    pass

            monitor = threading.Thread(target=_monitor, daemon=True)
            monitor.start()
            try:
                while proc.poll() is None:
                    _check()
                    _time.sleep(0.2)
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            except (MergeCancelled, MergePaused):
                if out_path.exists():
                    try:
                        out_path.unlink()
                    except OSError:
                        pass
                raise
            finally:
                stop.set()
                monitor.join(timeout=2)

            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                run_log = f.read()
            if proc.returncode != 0:
                if out_path.exists():
                    try:
                        out_path.unlink()
                    except OSError:
                        pass
                raise RuntimeError(
                    f"ffmpeg HLS 合并失败（{proc.returncode}）：{run_log[-500:]}"
                )
        finally:
            try:
                os.unlink(log_path)
            except OSError:
                pass

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
