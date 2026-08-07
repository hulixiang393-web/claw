"""yt-dlp 引擎封装（ytdlp.py）。

把 YouTube 等视频站的复杂提取（innertube 签名、n/sig 挑战、po_token、高清多 client
回退）委托给已安装的 yt-dlp 命令行，框架只做子进程调用与结果解析。

为什么用 yt-dlp：YouTube 高清流需 JS 签名（signatureCipher + n 挑战），纯配置/纯 API
只能拿 360p 单流。yt-dlp 全权处理签名，一行命令拿任意画质。

能力（均 subprocess 调 yt-dlp，不依赖其 Python 库解析）：
- search(keyword, limit)     → 搜索结果 [{title,url,cover,author}]
- fetch_detail(url)          → {title, author, cover, summary, chapters}
- fetch_streams(url, fmt)    → (video_url, audio_url) 高清双流（供播放器/下载）
- 下载仍走 ffmpeg_merger（双流 → mp4）

源配置以 api_endpoints.* .engine == "ytdlp" 声明启用本引擎。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Dict, List, Optional
from urllib.parse import urlencode, urljoin


class YtdlpError(RuntimeError):
    """yt-dlp 调用失败。"""


def _find_ytdlp() -> str:
    path = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if path:
        return path
    return "yt-dlp"


class Ytdlp:
    """yt-dlp 子进程封装。"""

    # 类级共享搜索缓存：search/discovery/content 各持一个 Ytdlp 实例，
    # 缓存跨实例复用（同关键词重复搜索秒回，减少 yt-dlp 子进程冷启动）
    _search_cache: dict = {}

    def __init__(self, binary: Optional[str] = None, timeout: float = 60.0):
        self._bin = binary or _find_ytdlp()
        self._timeout = timeout

    # ------------------------------------------------------------------ #
    def _run(self, args: List[str]) -> str:
        """跑 yt-dlp，返回 stdout（utf-8）。失败抛 YtdlpError。

        用临时文件重定向 stdout/stderr 而非 capture_output：
        Windows 管道缓冲满（~64KB）会让子进程写 stderr 时阻塞 → 死锁，
        视频/长列表输出大时尤其明显。文件重定向彻底规避。
        """
        cmd = [self._bin] + args
        import tempfile
        import os

        # stdout 与 stderr 分开写文件：yt-dlp 会把 Python 3.10 弃用警告等
        # 打到 stderr，若 stderr=STDOUT 合并会污染 JSON（json.loads 失败 →
        # 搜索/详情返回空）。stderr 独立文件，成功只取 stdout，失败读 stderr。
        tmp = tempfile.NamedTemporaryFile(
            mode="w+", suffix=".out", delete=False, encoding="utf-8"
        )
        tmp_err = tempfile.NamedTemporaryFile(
            mode="w+", suffix=".err", delete=False, encoding="utf-8"
        )
        log_path = tmp.name
        err_path = tmp_err.name
        tmp.close()
        tmp_err.close()
        try:
            with open(log_path, "w", encoding="utf-8", errors="replace") as f, \
                 open(err_path, "w", encoding="utf-8", errors="replace") as ef:
                proc = subprocess.run(
                    cmd,
                    stdout=f,
                    stderr=ef,
                    text=True,
                    errors="replace",
                    timeout=self._timeout,
                )
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    out = f.read()
            except OSError:
                out = ""
            try:
                with open(err_path, "r", encoding="utf-8", errors="replace") as f:
                    err_out = f.read()
            except OSError:
                err_out = ""
        except subprocess.TimeoutExpired:
            raise YtdlpError("yt-dlp 超时")
        except FileNotFoundError:
            raise YtdlpError(f"未找到 yt-dlp：{self._bin}")
        finally:
            for p in (log_path, err_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
        if proc.returncode != 0:
            err = (err_out or out or "")[-300:].strip()
            raise YtdlpError(f"yt-dlp 错误({proc.returncode})：{err}")
        return out

    # ------------------------------------------------------------------ #
    def search(self, keyword: str, limit: int = 20, prefix: str = "ytsearch",
               url_tpl: str = "https://www.youtube.com/watch?v={id}") -> List[Dict]:
        """通用搜索（yt-dlp 搜索前缀 + URL 模板）。

        不同站点 extractor 的搜索前缀不同（如 ytsearch=YouTube）。
        prefix: 搜索前缀（如 "ytsearch"），limit 附后。
        url_tpl: 结果 ID → 视频 URL 模板（默认 YouTube）。
        返回 [{title, url, cover, author}]。

        yt-dlp 子进程冷启动 + 网络签名通常 5~15s，同关键词重复搜索走缓存
        秒回（Content 复用单例 Ytdlp，缓存跨搜索有效）。
        """
        key = (prefix, keyword, limit)
        if key in Ytdlp._search_cache:
            return list(Ytdlp._search_cache[key])

        q = f"{prefix}{limit}:{keyword}"
        out = self._run([
            "--dump-single-json", "--no-warnings",
            "--flat-playlist",
            "--socket-timeout", "10",
            q,
        ])
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return []
        entries = data.get("entries") or []
        results = []
        for e in entries:
            eid = e.get("id")
            if not eid:
                continue
            url = url_tpl.format(id=eid)
            results.append({
                "title": e.get("title") or "",
                "url": url,
                "cover": (e.get("thumbnail") or "").split("?")[0],
                "author": e.get("channel") or e.get("uploader") or "",
            })
        Ytdlp._search_cache[key] = results
        return results

    # ------------------------------------------------------------------ #
    def list_formats(self, url: str) -> List[Dict]:
        """列可用画质/格式（常用功能）。返回 [{format_id, height, ext, fps, vcodec, acodec}]。"""
        out = self._run(["-F", "--no-warnings", url])
        formats = []
        for line in out.strip().splitlines():
            parts = line.split()
            if not parts or not parts[0].isdigit():
                continue
            fmt = {"format_id": parts[0]}
            # 格式行示例: 299 mp4 1920x1080 60 | avc1.64002a  153k video only
            for i, p in enumerate(parts):
                if "x" in p and p.split("x")[0].isdigit():
                    w, h = p.split("x")[:2]
                    if h.isdigit():
                        fmt["height"] = int(h)
                if p.isdigit() and 20 <= int(p) <= 120 and i < len(parts) - 1:
                    fmt["fps"] = int(p)
                if p == "video" or p == "audio":
                    fmt.setdefault("kind", p)
            formats.append(fmt)
        return formats

    # ------------------------------------------------------------------ #
    def extractor_of(self, url: str) -> str:
        """探测 URL 属于哪个站点 extractor（如 youtube/bilibili）。"""
        # 用 -J 看返回的 extractor_key（不白跑 --list-extractors 浪费子进程调用）
        try:
            d = json.loads(self._run(["--dump-single-json", "--no-warnings",
                                      "--skip-download", url]))
            return d.get("extractor_key") or d.get("extractor") or ""
        except Exception:
            return ""

    # ------------------------------------------------------------------ #
    def fetch_detail(self, url: str) -> Dict:
        """详情元数据 + 章节。一个 --dump-single-json 全拿。"""
        out = self._run([
            "--dump-single-json", "--no-warnings", "--no-download",
            "--socket-timeout", "10", "--encoding", "utf-8", url,
        ])
        try:
            d = json.loads(out)
        except json.JSONDecodeError:
            raise YtdlpError("yt-dlp 详情解析失败")
        chapters = []
        # 单视频作为"1 章"；若有多 P/章节用章节列表，否则整节当一章
        chapters = [{"title": d.get("title") or "视频", "url": url}]
        return {
            "title": d.get("title") or "",
            "author": d.get("channel") or d.get("uploader") or "",
            "cover": (d.get("thumbnail") or "").split("?")[0],
            "summary": d.get("description") or "",
            "status": f"{d.get('duration_string') or ''} · {d.get('view_count') or 0:,} 播放",
            "chapters": chapters,
            "_raw": d,
        }

    # ------------------------------------------------------------------ #
    def fetch_streams(self, url: str, fmt: str = "bestvideo+bestaudio/best") -> tuple:
        """高清双流（video_url, audio_url）。供播放器（VLC）播放 / ffmpeg 下载。"""
        # 拿 video 与 audio 两个 URL（有 audio 则分离）
        fmt_video, fmt_audio = None, None
        m = re.match(r"^(?P<v>[^+]+)\+(?P<a>.+)$", fmt)
        if m:
            fmt_video, fmt_audio = m.group("v"), m.group("a")
        else:
            fmt_video = fmt
        v = self._fetch_one(url, fmt_video)
        a = self._fetch_one(url, fmt_audio) if fmt_audio else ""
        return v, a

    def _fetch_one(self, url: str, fmt: str) -> str:
        """拿单个 format 的 URL。用 --get-url。"""
        out = self._run([
            "-f", fmt, "--get-url", "--no-warnings",
            "--no-playlist",  # 取流只要单条：URL 带 list 参数时不整表解析，省签名等待
            "--socket-timeout", "10",
            "--extractor-args", "youtube:skip=sections",
            url,
        ])
        first = out.strip().splitlines()
        return first[0] if first else ""

    # ------------------------------------------------------------------ #
    def download(self, url: str, output: str, fmt: str = "bestvideo+bestaudio/best") -> str:
        """yt-dlp 直接下载（含合并）。返回输出路径。"""
        self._run([
            "-f", fmt, "--no-warnings", "--newline",
            "-o", output, "--merge-output-format", "mp4",
            url,
        ])
        return output
