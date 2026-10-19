"""本地流媒体代理（media_proxy.py）。

解决「外部播放器（VLC）无法设置 UA / 复杂防盗链头」的问题：
VLC 命令行只能传 Referer/UA 两个简单选项，且实测 --http-user-agent 在
VLC 3.0.23 无效（恒发 VLC 默认 UA，多数 CDN 拒绝）。本模块起一个
本机回环 HTTP 代理，把「目标 URL + 完整请求头（Referer/UA/Cookie…）」
打包成本地 URL 交给播放器：

    本地代理 URL ──► 代理带源 headers 请求目标 ──► 流式回传

- /s/<token>             任意媒体（mp4/mp3/ts/audio…）：流式转发，透传 Range
- 若响应是 m3u8（HLS）：重写内部分片/KEY/变体 URL 为本地代理 URL，
  让播放器拉分片时也自动带上源 headers（AES-128 key 同样代理）
- /c/<key>/<segment>     HLS 分片磁盘缓存路由（framework/media_cache.py）：
  命中本地文件直接 serve（支持 Range，磁盘磁盘秒开不再打源站）；未命中
  回落上游代理转发，同时 tee 落盘供下次命中
- /e/<key>/<idx>         惰性系列（全集播放列表）：VLC 点到哪一集才解析哪一集，
  解析后 302 到该集的 /s/<token>（重复请求按集 memo，不回源）
- mp4 大文件流式转发时 tee 落盘：完整写毕后后续 Range 请求直接本地 serve
- 单例 + 空闲自动回收（无请求 N 秒停掉，下次 open 重新起）。播放是长连接
  流式转发，循环内持续刷新 _last_use，暂停/拖动进度期间不会被看门狗误杀；
  stop() 前检查活动请求计数，有进行中的流式连接时不停止不清 token。

用法：
    proxy = MediaProxy.instance()
    local_url = proxy.build_url("https://cdn/xxx.m3u8", {"Referer": ..., "User-Agent": ...})
    # 把 local_url 交给外部播放器
"""

from __future__ import annotations

import atexit
import hashlib
import inspect
import ipaddress
import mimetypes
import os
import shutil
from contextlib import nullcontext
import posixpath
import re
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, unquote, quote, urlparse

import requests
from requests.adapters import HTTPAdapter

from .adblock import trusted_media_context, trusted_source_id
from urllib3.exceptions import HTTPError as UpstreamReadError

# 无任何请求 N 秒后自动关闭（避免残留进程/端口）。放大到 600s：VLC 暂停/拖动
# 进度是原地等待不再发请求，若 N 太小看门狗在播放期间误停代理 → URL 失效
# （拖动进度后 VLC 报错）。流式转发循环内持续刷新 _last_use，正常播放不会触发。
_IDLE_TIMEOUT = 600.0
_WATCH_INTERVAL = 10.0  # 看门狗轮询间隔（秒）；测试可调小以加速验证
_READ_CHUNK = 64 * 1024
# 内容嗅探读取字节数：判断响应体是不是 HLS 播放列表（URL 未含 .m3u8 但
# 内容是的，如短链/参数化 m3u8）。只需覆盖 "#EXTM3U" 魔数（7 字节）。
# **不要调大**：read(n) 在 http.client 的 BufferedReader 上要凑满 n 字节或
# EOF 才返回，预读 64KB 会把每个分片的首字节延后一个 64KB 的到达时间
# （慢 CDN 上就是肉眼可见的起播延迟）。
_SNIFF_BYTES = 16


def _read_available(raw, size: int) -> bytes:
    """有多少读多少地读上游：只调一次 read1，绝不并读。

    根因（实测）：read(n) 在 http.client 的 BufferedReader 上要凑满 n 字节或
    EOF 才返回。CDN 按 4KB/8KB 慢速推流时，头几 KB 明明已经到了却压在缓冲区
    里等剩下的 50KB → 播放器缓存被抽干 → 肉眼可见的跳帧/卡顿（VLC 表现为反复
    rebuffer）。模拟慢上游实测：read 首字节 4.50s vs read1 0.00s。

    read1() 只要底层有至少 1 字节可读就返回，最多读 size 字节。**只调一次**：
    曾试过「再并读几次凑到 32KB」，但第二次 read1 在底层无就绪数据时会阻塞
    （实测首字节延迟从 0.00s 退回 0.80s），把要修的 stall 又请回来了。宁可
    多几次小 write（实测 4KB 分包下 write 次数 25→400，总吞吐/耗时不变，
    瓶颈仍在上游发包速率），也不重新引入等待。

    没有 read1 的 mock / 老版本退回 read(size)（原行为，功能等价只是慢）。
    """
    read1 = getattr(raw, "read1", None)
    if callable(read1):
        try:
            return read1(size) or b""
        except (TypeError, ValueError):
            pass  # 签名不兼容 → 退回 read
    return raw.read(size)

_CACHE_FETCH_WAIT = 30.0
_PREFETCH_MAX_DEPTH = 64
_trusted_source_id = trusted_source_id


_PREFETCH_MAX_WORKERS = 8
_PREFETCH_MAX_PENDING = 32
_PREFETCH_TIMEOUT = 10.0
_PREFETCH_CANCEL_WAIT = 1.0
_PREFETCH_FAILURE_LIMIT = 3
_MANIFEST_CACHE_TTL = 10.0
# 媒体响应透传的头
_MEDIA_HDRS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges")

# 惰性系列（外部播放器全集播放列表）同时注册的最大支数。注册表是有序 dict，
# 超出时淘汰最旧的一支（兜底：App 反复换源不注销时的注册泄漏）。
_SERIES_MAX = 8
# 单集惰性解析的并发上限（避免多集同时点播把源站/反爬打爆）。
_SERIES_SEM = 4
# 单集惰性解析排队等待上限（秒）；超时回 503。
_SERIES_WAIT = 20.0

# 兼容性开关：True 时所有上游请求强制走系统代理。
_PROXY_ONLY = False

# 转发到 CDN 的连接池单例：VLC 经本地代理逐个拉 m3u8 分片时复用 keep-alive
# 连接，避免每个分片都重新对 CDN 握手（urllib.urlopen 无连接池，几十个分片
# 几十次 TCP/TLS 握手是播放卡顿/加载慢的常见根因）。
#
# 两个会话：
# - 直连会话（trust_env=False）：正常请求的首选出口，绕开系统代理转发延迟。
# - 系统代理会话（trust_env=True）：直连失败、失败记忆命中或强制代理时使用。
_PROXY_SESSION = None
_DIRECT_SESSION = None
_DIRECT_FAIL = {}  # {host: 直连失败时间戳}：失败后 300s 内该 host 直接走代理
_DIRECT_FAIL_TTL = 300.0  # 被墙站（如 18mh.net 直连 TLS 挂起）失败记忆拉长，
# 否则每 30s 就要重吃一次直连超时（播放加载慢的根因之一）
_DIRECT_CONNECT_TIMEOUT = 3.0  # 直连 connect 短超时：被墙主机快速回退，不拖慢播放
_PROXY_SESSION_LOCK = threading.Lock()
_DIRECT_SESSION_LOCK = threading.Lock()
_DIRECT_FAIL_LOCK = threading.Lock()
_UPSTREAM_DIAGNOSTICS_MAX = 128
_UPSTREAM_DIAGNOSTICS = []
_UPSTREAM_DIAGNOSTICS_LOCK = threading.Lock()

# HLS 分片时间戳重写（ad_block.hls_rules.remux_timestamps）。
# 部分源的片源是把 mp4 硬切成 TS：分片 PTS/DTS 各自回到 ~0 且边界帧常常不是
# I 帧，播放器跨分片接不上关键帧 → 表现为周期性"跳帧/花屏一下"。选项开关时
# 第 2 片起先按序重封装为 PTS 连续的 TS 再交给播放器；播放器的分片磁盘缓存
# 仍按原始分片落盘，命中后整理，不改变缓存键与命中语义。
_REMUX_CTX_MAX = 64
_REMUX_TIMEOUT = 60.0
_REMUX_CTX = {}
_REMUX_LOCKS = {}
_REMUX_CTX_LOCK = threading.Lock()


def _remux_ctx(key: str):
    with _REMUX_CTX_LOCK:
        ctx = _REMUX_CTX.get(key)
        if ctx is None:
            if len(_REMUX_CTX) >= _REMUX_CTX_MAX:
                _REMUX_CTX.pop(next(iter(_REMUX_CTX)))
            ctx = {"lock": threading.Lock(), "segs": {}, "inflight": {}, "done": 0}
            _REMUX_CTX[key] = ctx
        return ctx


def _remux_serial(key: str) -> "threading.RLock":
    """同一清单 key 的整理串行锁（建表也加锁，避免并发建两份上下文）。

    锁表单独存放：_REMUX_CTX 的值必须都是 dict（行为与清理逻辑一致）。
    """
    with _REMUX_CTX_LOCK:
        lock = _REMUX_LOCKS.get(key)
        if lock is None:
            if len(_REMUX_LOCKS) >= _REMUX_CTX_MAX * 4:
                _REMUX_LOCKS.pop(next(iter(_REMUX_LOCKS)))
            lock = threading.RLock()
            _REMUX_LOCKS[key] = lock
        return lock


def _remux_active(ad_block: dict | None) -> bool:
    rules = (ad_block or {}).get("hls_rules") or {}
    rule = rules.get("remux_timestamps") if isinstance(rules, dict) else None
    return isinstance(rule, dict) and bool(rule.get("enabled", True))


def _ctx_get(ctx, index: int, default=None):
    """按位置取 token/缓存上下文里的可选字段。

    token_context 的 source_id 只对 18j 与 remux 源追加；其他调用方（外部播放器
    全集中继、cast、测试里的三元组）不追加，写入位置不恒定 → 只认 str。
    """
    value = ctx[index] if len(ctx) > index else None
    return value if isinstance(value, str) else default


def _is_ts_payload(head: bytes) -> bool:
    return bool(head) and head[0] == 0x47 and (
        len(head) < 377 or head[188] == 0x47)


def _locate_ffmpeg() -> str:
    """定位 ffmpeg（打包内置优先，其次 PATH）；没有就返回空串。"""
    try:
        from .ffmpeg_merger import _find_ffmpeg

        path = _find_ffmpeg()
    except Exception:  # noqa: BLE001 —— 缺 ffmpeg 只是不重写，不影响播放
        return ""
    return path if path and (Path(path).is_file() or path == "ffmpeg") else ""


def _pes_header_offset(data, off: int, step: int):
    """PES 头起始偏移；越界（畸形包）返回 None。

    adaptation_field_length 为 0 时其后仍紧跟 1 个「适配字段长度」字节本身
    （ffmpeg 复用出的 TS 就带这种占位），必须用 1 + N 跳过，否则整个 PES 头
    被读偏 1 字节 —— 会算出假的 PTS/DTS，再平移就写坏时间戳（实测 728727 /
    0 之类的错值）。每个包的时间戳都靠这个偏移定位。
    """
    afc = (data[off + 3] >> 4) & 0x03
    p = off + 4
    if afc in (2, 3):
        p += 1 + data[p]
        if p >= off + step:
            return None
    return p


def _read_ts_timestamps(data, off: int, step: int):
    """取单个 188 字节 TS 包里 PES 头的 PTS/DTS 字段偏移列表。

    返回空列表表示本包没有可平移的时间戳。PCR 不参与：播放器按 PES 时间戳
    计时，PCR 一起平移会把对齐打乱。
    """
    values = []
    p = _pes_header_offset(data, off, step)
    if p is None or not (data[off + 1] & 0x40) or p + 9 >= off + step:
        return values
    if data[p] == 0 and data[p + 1] == 0 and data[p + 2] == 1 \
            and data[p + 3] in (0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xFF, 0xF2, 0xF8):
        return values                             # padding 等无 PTS 的 PES
    h = data[p + 7] & 0xC0
    if h == 0x80 and p + 13 < off + step:         # PTS only
        values.append(p + 9)
    elif h == 0xC0 and p + 18 < off + step:       # PTS + DTS
        values.append(p + 9)
        values.append(p + 14)
    return values


def _decode_ts5(data, p: int) -> int:
    return (((data[p] >> 1) & 0x07) << 30) | (data[p + 1] << 22) | \
           (((data[p + 2] >> 1) & 0x7F) << 15) | (data[p + 3] << 7) | (data[p + 4] >> 1)


def _encode_ts5(data, p: int, value: int) -> None:
    value &= 0x1FFFFFFFF
    data[p] = (data[p] & 0xF1) | ((value >> 29) & 0x0E)
    data[p + 1] = (value >> 22) & 0xFF
    data[p + 2] = ((value >> 14) & 0xFE) | 0x01
    data[p + 3] = (value >> 7) & 0xFF
    data[p + 4] = ((value << 1) & 0xFE) | 0x01


def _stream_key(data, off: int, offsets) -> tuple:
    """PES 所属节目流标识（PID + stream_id）。

    连续性按流分别检查：音视频各自的 DTS 序列互不干扰。
    """
    pid = ((data[off + 1] & 0x1F) << 8) | data[off + 2]
    p = _pes_header_offset(data, off, 188)
    if p is None or p + 3 >= off + 188:
        return pid, -1
    return pid, data[p + 3]


def _ts_shift(src: Path, first_dts: float):
    """时间戳整理：让本片**首帧**落在 first_dts（秒），并修掉片内错位时间戳。

    1. 锚点：片内最早的时间戳平移到 first_dts；
    2. 连续性：每个 PES 的 DTS 必须紧跟同流上一个 DTS（帧间隔取片内中位数）；
       倒退或偏离过远的包按预期时刻重锚，后续包随之顺延。
    只改 PES 头的 PTS/DTS，帧数与字节内容都不变；否则返回 False。

    返回**本片时长（秒）**而不是 True：时长是下一片锚点的推进量，而本函数
    本来就要逐包扫一遍取 min_ts，顺手记 max 即可算出（max-min）/90000，
    实测与 ffprobe 的 format=duration 相差 <1 帧（20ms 量级），因此再起一个
    ffprobe 子进程纯属浪费（冷启动约 2s，直接卡住播放）。失败返回 None。
    """
    try:
        data = bytearray(src.read_bytes())
    except OSError:
        return None
    step = 188
    if len(data) < step * 2:
        return None
    entries = []          # [(包偏移, [(字段偏移, 原值)])]，按文件顺序
    min_ts = None
    max_ts = None
    off = 0
    while off + step <= len(data):
        if data[off] != 0x47:
            return None
        if (data[off + 1] & 0x1F) != 0x1F:
            offsets = _read_ts_timestamps(data, off, step)
            if offsets:
                values = []
                for p in reversed(offsets):     # 先取再写：DTS 偏移与 PTS 字面向量
                    values.append((p, _decode_ts5(data, p)))
                values.reverse()
                entries.append((off, values))
                for _p, v in values:
                    min_ts = v if min_ts is None else min(min_ts, v)
                    max_ts = v if max_ts is None else max(max_ts, v)
        off += step
    if min_ts is None or not entries or max_ts is None:
        return None
    duration = (max_ts - min_ts) / 90000.0
    if duration <= 0:
        return None
    bump = int(round(first_dts * 90000)) - min_ts

    # 帧间隔：同流相邻 DTS 差的中位数（90kHz）。样本太少就不做连续性修正。
    diffs = []
    last_by_stream = {}
    for off, values in entries:
        key = _stream_key(data, off, values)
        dts = values[-1][1]
        prev = last_by_stream.get(key)
        if prev is not None and 0 < dts - prev < 90000 * 3:
            diffs.append(dts - prev)
        last_by_stream[key] = dts
    gap = sorted(diffs)[len(diffs) // 2] if diffs else 0

    resid = 0
    changed = False
    last_by_stream = {}
    for off, values in entries:
        key = _stream_key(data, off, values)
        prev = last_by_stream.get(key)
        if gap and prev is not None:
            # values 是**原始**时间戳（必须先加已累计的 resid 再比较），否则
            # 每次比较都会把 resid 重复算一遍 → 后续包被越推越远。
            want = prev + gap
            dts = values[-1][1] + bump + resid
            if dts <= prev or dts - want > max(gap, 90000 * 0.5):
                resid += want - dts
        delta = bump + resid
        if delta:
            changed = True
            for p, _v in values:
                _encode_ts5(data, p, _decode_ts5(data, p) + delta)
        last_by_stream[key] = values[-1][1] + delta
    if not changed:
        return duration
    try:
        src.write_bytes(bytes(data))
    except OSError:
        return None
    return duration


def _ts_first_dts(path: Path):
    """文件里最早的 DTS（秒，按 PES 头解析）；读不到返回 None。"""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    step = 188
    best = None
    off = 0
    while off + step <= len(data):
        if data[off] != 0x47:
            return None
        if (data[off + 1] & 0x1F) == 0x1F:
            off += step
            continue
        for p in _read_ts_timestamps(data, off, step):
            v = _decode_ts5(data, p)
            best = v if best is None else min(best, v)
        off += step
    return None if best is None else best / 90000.0


def _classify_media_protocol(target: str, headers: dict) -> str:
    path = urlparse(target).path.lower()
    if path.endswith((".m3u8", ".m3u")) or "m3u8" in path:
        return "hls"
    if path.endswith(".mpd"):
        return "dash"
    if path.endswith((".ts", ".key", ".key.bin", ".m3u")) or "/key" in path:
        return "hls"
    if any(key.lower() == "range" and value for key, value in headers.items()):
        return "mp4"
    if path.endswith((".mp4", ".m4v", ".mov", ".webm")):
        return "mp4"
    return "unknown"


def _classify_upstream_request_kind(target: str, headers: dict) -> str:
    path = urlparse(target).path.lower()
    if any(key.lower() == "range" and value for key, value in headers.items()):
        return "range"
    if path.endswith((".m3u8", ".m3u")):
        return "manifest"
    if path.endswith((".key", ".key.bin", ".bin")) or "/key" in path:
        return "key"
    if path.endswith((".mp4", ".m4v", ".mov", ".webm")):
        return "media"
    if path.endswith((".ts", ".m4s", ".aac", ".mp3", ".webvtt", ".vtt")):
        return "segment"
    return "media"


def _diagnostic_failure(category: str, exc: Exception | None = None) -> str:
    if category == "direct":
        if isinstance(exc, requests.exceptions.SSLError):
            return "direct_tls"
        if isinstance(exc, requests.Timeout):
            return "direct_timeout"
        if isinstance(exc, requests.ConnectionError):
            return "direct_connection"
        return "direct_failure"
    if isinstance(exc, requests.exceptions.SSLError):
        return "proxy_tls"
    if isinstance(exc, requests.Timeout):
        return "proxy_timeout"
    if isinstance(exc, requests.ConnectionError):
        return "proxy_connection"
    return "proxy_failure"


def _record_upstream_diagnostic(target: str, headers: dict, route: str,
                                started: float, response=None,
                                failure_category: str | None = None,
                                cache_state: str | None = None,
                                wait_ms: float | None = None) -> dict:
    elapsed_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
    status_code = getattr(response, "status_code", None)
    if status_code is not None and status_code >= 400 and failure_category is None:
        failure_category = f"{route}_http"
    record = {
        "protocol": _classify_media_protocol(target, headers),
        "host": (urlparse(target).hostname or "")[:128],
        "request_kind": _classify_upstream_request_kind(target, headers),
        "route": route,
        "status": status_code,
        "status_code": status_code,
        "elapsed_ms": elapsed_ms,
        "first_byte_ms": None,
        "bytes": 0,
        "throughput_bps": None,
        "cache_state": cache_state,
        "wait_ms": wait_ms,
        "failure_category": failure_category,
    }
    with _UPSTREAM_DIAGNOSTICS_LOCK:
        _UPSTREAM_DIAGNOSTICS.append(record)
        del _UPSTREAM_DIAGNOSTICS[:-_UPSTREAM_DIAGNOSTICS_MAX]
    return record


def _record_cache_diagnostic(target: str, headers: dict, cache_state: str,
                             wait_ms: float | None = None) -> dict:
    return _record_upstream_diagnostic(
        target, headers, "cache", time.perf_counter(),
        response=type("_CacheResponse", (), {"status_code": 200})(),
        cache_state=cache_state, wait_ms=wait_ms,
    )


def _attach_upstream_diagnostic(response, record: dict, started: float) -> None:
    if response is None:
        return
    try:
        response._media_proxy_diagnostic = record
        response._media_proxy_started = started
        response._media_proxy_first_byte = None
        response._media_proxy_bytes = 0
    except Exception:
        pass


def _update_upstream_diagnostic(response, size: int, finished: bool = False,
                                failure_category: str | None = None) -> None:
    record = getattr(response, "_media_proxy_diagnostic", None)
    if not record:
        return
    now = time.perf_counter()
    with _UPSTREAM_DIAGNOSTICS_LOCK:
        if size > 0:
            first = getattr(response, "_media_proxy_first_byte", None)
            if first is None:
                first = now
                try:
                    response._media_proxy_first_byte = first
                except Exception:
                    pass
                record["first_byte_ms"] = max(
                    0.0, (first - getattr(response, "_media_proxy_started", first)) * 1000.0
                )
            total = getattr(response, "_media_proxy_bytes", 0) + size
            try:
                response._media_proxy_bytes = total
            except Exception:
                pass
            record["bytes"] = total
            elapsed = max(0.000001, now - first)
            record["throughput_bps"] = total * 8.0 / elapsed
        if failure_category is not None:
            record["failure_category"] = failure_category
        if finished:
            record["elapsed_ms"] = max(
                record["elapsed_ms"],
                (now - getattr(response, "_media_proxy_started", now)) * 1000.0,
            )


def _read_upstream_body(response) -> bytes:
    raw = getattr(response, "raw", None)
    if raw is None or not hasattr(raw, "read"):
        body = response.content
        _update_upstream_diagnostic(response, len(body))
        return body
    try:
        encoding = (response.headers.get("Content-Encoding") or "").lower()
        if encoding in ("gzip", "deflate", "br"):
            raw.decode_content = True
        chunks = []
        while True:
            chunk = raw.read(_READ_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
            _update_upstream_diagnostic(response, len(chunk))
        body = b"".join(chunks)
        if encoding == "zstd":
            # 部分站点无视 Accept-Encoding: identity 强制回 zstd，urllib3
            # 不会自动解压；zstandard 为可选依赖，缺失时原样返回（不新增依赖）。
            body = _decode_zstd(body)
        return body
    except (AttributeError, TypeError):
        body = response.content
        _update_upstream_diagnostic(response, len(body))
        return body


def _decode_zstd(data: bytes) -> bytes:
    """zstd 解压（可选依赖）：zstandard 缺失或解压失败时原样返回。"""
    try:
        import zstandard
    except ImportError:
        return data
    try:
        stream = zstandard.ZstdDecompressor().decompressobj()
        out = stream.decompress(data)
        return out + (stream.flush() or b"")
    except Exception:
        return data


def _read_upstream_route_diagnostics() -> list[dict]:

    with _UPSTREAM_DIAGNOSTICS_LOCK:
        return [dict(record) for record in _UPSTREAM_DIAGNOSTICS]


def _reset_upstream_route_diagnostics() -> None:
    with _UPSTREAM_DIAGNOSTICS_LOCK:
        _UPSTREAM_DIAGNOSTICS.clear()


def _make_session(trust_env: bool) -> requests.Session:
    s = requests.Session()
    s.trust_env = trust_env
    try:
        s.mount("http://", HTTPAdapter(pool_connections=16, pool_maxsize=64))
        s.mount("https://", HTTPAdapter(pool_connections=16, pool_maxsize=64))
    except Exception:  # noqa: BLE001
        pass
    return s


def _get_session() -> requests.Session:
    """系统代理会话（连接复用，keep-alive 提速）——回退用。"""
    global _PROXY_SESSION
    if _PROXY_SESSION is None:
        with _PROXY_SESSION_LOCK:
            if _PROXY_SESSION is None:
                _PROXY_SESSION = _make_session(True)
    return _PROXY_SESSION


def _get_direct_session() -> requests.Session:
    """直连会话（trust_env=False：不读系统代理，媒体流直连 CDN）。"""
    global _DIRECT_SESSION
    if _DIRECT_SESSION is None:
        with _DIRECT_SESSION_LOCK:
            if _DIRECT_SESSION is None:
                _DIRECT_SESSION = _make_session(False)
    return _DIRECT_SESSION


def _tuple_force_proxy(entry) -> bool:
    """兼容解包：老三元组 / 四元组 / 带 public_base 的五元组取 force_proxy。"""
    return len(entry) >= 4 and bool(entry[3])


def _strip_stale_headers(headers: dict) -> dict:
    """去掉只在单次请求里有效的头，避免写进 token 后污染后续请求。

    Range 是「某一次」请求的范围语义：m3u8 请求带的 Range 若被存进分片/
    KEY token，VLC 后续拉 KEY/分片（自身不带 Range）时会把过时 Range 转发
    上游 → 上游回 206 → VLC key demux 判失败。token 只应存与资源绑定的
    防盗链/鉴权头（Referer/UA/Cookie…）。
    """
    out = dict(headers)
    out.pop("Range", None)
    return out


def _clean_context_headers(headers: dict) -> dict:
    return _strip_stale_headers(headers)


def _context_target(target: str) -> str:
    return target


def _context_fingerprint(target: str, headers: dict | None = None) -> str:
    values = [_context_target(target)]
    values.extend(
        f"{key.lower()}={value}"
        for key, value in sorted(_clean_context_headers(headers or {}).items())
    )
    return hashlib.sha256("\n".join(values).encode("utf-8", "replace")).hexdigest()


def _manifest_strategy_fingerprint(ad_block: dict | None,
                                   force_proxy: bool,
                                   source_id: str = "") -> str:
    payload = {
        "version": 2,
        "source_id": trusted_source_id(source_id),
        "ad_block": ad_block or {},
        "force_proxy": bool(force_proxy),
    }
    raw = repr(payload).encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()


def _manifest_cache_key(cache, target: str, ad_block: dict | None,
                        force_proxy: bool, source_id=None) -> str:
    material = (
        target + "\n" + _manifest_strategy_fingerprint(ad_block, force_proxy, source_id)
        if trusted_source_id(source_id) == "18j" else target
    )
    return cache.key_of(material)


def _clean_detection_context(context: dict | None) -> dict | None:
    if not isinstance(context, dict):
        return None
    allowed = {"template_hashes", "keyword_region_score", "stable_block", "protocol_ad"}
    return {key: value for key, value in context.items() if key in allowed}


def _manifest_contains_query(text: str, base_url: str) -> bool:
    for line in text.splitlines():
        value = line.strip()
        if not value:
            continue
        if value.startswith("#"):
            match = re.search(r'URI="([^"]+)"', value)
            if match is None:
                continue
            value = match.group(1)
        if urlparse(urljoin(base_url, value)).query:
            return True
    return False


def _proxy_get(target: str, headers: dict, timeout=30, attempts=2):
    """经系统代理会话取流。重试一次短退避：代理偶发连接重置/切节点时，
    否则上层把异常当 502 抛给播放器 → VLC demux 失败 → 播放中断。
    两次都失败则抛出最后一次异常。"""
    last_exc = None
    deadline = None
    if isinstance(timeout, tuple):
        deadline = time.monotonic() + max(timeout)
    for _attempt in range(attempts):
        request_timeout = timeout
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise requests.Timeout("proxy fetch timeout")
            request_timeout = (min(timeout[0], remaining), remaining)
        try:
            return _get_session().get(
                target, headers=headers, timeout=request_timeout, stream=True
            )
        except requests.RequestException as exc:  # noqa: BLE001
            last_exc = exc
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.3, remaining))
            else:
                time.sleep(0.3)
    raise last_exc  # noqa: BLE001 —— 两次都失败，让上层 502/重试


def _fetch_upstream(target: str, headers: dict, force_proxy: bool = False,
                    cache_state: str | None = "miss",
                    wait_ms: float | None = None, timeout=None, segment=False,
                    retry_connect=True):
    """默认直连优先，失败后按 host 记忆并回退系统代理。"""
    segment = segment or _classify_upstream_request_kind(target, {}) == "segment"
    deadline = (time.monotonic() + max(timeout)
                if isinstance(timeout, tuple) else None)

    def _request_timeout():
        if deadline is None:
            return timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise requests.Timeout("upstream fetch timeout")
        return (min(timeout[0], remaining), remaining)

    if force_proxy or _PROXY_ONLY:
        started = time.perf_counter()
        try:
            resp = _proxy_get(target, headers,
                              timeout=_request_timeout() or 30,
                              attempts=2 if retry_connect else 1)
        except requests.RequestException as exc:
            _record_upstream_diagnostic(
                target, headers, "proxy", started,
                failure_category=_diagnostic_failure("proxy", exc),
                cache_state=cache_state, wait_ms=wait_ms,
            )
            raise
        record = _record_upstream_diagnostic(
            target, headers, "proxy", started, resp,
            cache_state=cache_state, wait_ms=wait_ms,
        )
        _attach_upstream_diagnostic(resp, record, started)
        return resp

    host = urlparse(target).hostname
    with _DIRECT_FAIL_LOCK:
        blocked = time.time() - _DIRECT_FAIL.get(host, 0.0) < _DIRECT_FAIL_TTL
    if not blocked:
        started = time.perf_counter()
        try:
            resp = _get_direct_session().get(
                target, headers=headers,
                timeout=(_request_timeout() if timeout is not None else
                         (_DIRECT_CONNECT_TIMEOUT, 60)), stream=True,
            )
            if resp is not None:
                if resp.status_code < 400 or (segment and resp.status_code in (403, 429)):
                    record = _record_upstream_diagnostic(
                        target, headers, "direct", started, resp,
                        cache_state=cache_state, wait_ms=wait_ms,
                    )
                    _attach_upstream_diagnostic(resp, record, started)
                    return resp
                _record_upstream_diagnostic(
                    target, headers, "direct", started, resp,
                    cache_state=cache_state, wait_ms=wait_ms,
                )
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass
        except requests.RequestException as exc:
            _record_upstream_diagnostic(
                target, headers, "direct", started,
                failure_category=_diagnostic_failure("direct", exc),
                cache_state=cache_state, wait_ms=wait_ms,
            )
        with _DIRECT_FAIL_LOCK:
            _DIRECT_FAIL[host] = time.time()
    started = time.perf_counter()
    try:
        resp = _proxy_get(target, headers,
                          timeout=_request_timeout() or 30,
                          attempts=1 if segment else 2)
    except requests.RequestException as exc:
        _record_upstream_diagnostic(
            target, headers, "proxy", started,
            failure_category=_diagnostic_failure("proxy", exc),
            cache_state=cache_state, wait_ms=wait_ms,
        )
        raise
    record = _record_upstream_diagnostic(
        target, headers, "proxy", started, resp,
        failure_category=("direct_failure_memory"
                          if blocked and resp.status_code < 400 else None),
        cache_state=cache_state, wait_ms=wait_ms,
    )
    _attach_upstream_diagnostic(resp, record, started)
    return resp


def _read_media_prefix(resp, target, headers, force_proxy, cache=None,
                       fetch_key=None, fetch_state=None, segment=False):
    segment = segment or _classify_upstream_request_kind(target, {}) == "segment"
    for attempt in range(2):
        if resp.status_code >= 400:
            return resp, b"", False
        try:
            if fetch_state is not None and cache.fetch_cancelled(fetch_state):
                raise RuntimeError("media fetch cancelled")
            whole = (resp.headers.get("Content-Encoding") or "").lower() in (
                "gzip", "deflate", "br", "zstd")
            first = _read_upstream_body(resp) if whole else resp.raw.read(_SNIFF_BYTES)
            if not whole:
                _update_upstream_diagnostic(resp, len(first))
                length = resp.headers.get("Content-Length", "")
                if length.isdigit() and len(first) < min(_SNIFF_BYTES, int(length)):
                    raise requests.ConnectionError("incomplete media prefix")
            return resp, first, whole
        except (requests.ConnectionError, requests.Timeout, UpstreamReadError):
            resp.close()
            if attempt or not segment:
                raise
            force_proxy = force_proxy or (
                getattr(resp, "_media_proxy_diagnostic", {}).get("route") == "proxy")
            time.sleep(0.1)
            if fetch_state is not None and cache.fetch_cancelled(fetch_state):
                raise RuntimeError("media fetch cancelled")
            resp = _fetch_upstream(target, headers, force_proxy=force_proxy,
                                   segment=segment, retry_connect=False)
            if fetch_state is not None:
                cache.set_fetch_response(fetch_key, fetch_state, resp)
        except Exception:
            resp.close()
            raise


def _set_response_read_timeout(response, timeout: float) -> None:
    raw = getattr(response, "raw", None)
    candidates = [raw, getattr(raw, "_sock", None)]
    fp = getattr(raw, "_fp", None)
    socket_raw = getattr(getattr(fp, "fp", None), "raw", None)
    candidates.extend((getattr(fp, "fp", None), socket_raw,
                       getattr(socket_raw, "_sock", None)))
    for candidate in candidates:
        setter = getattr(candidate, "settimeout", None)
        if setter is not None:
            try:
                setter(max(0.01, timeout))
                return
            except (OSError, AttributeError):
                pass


def _parse_range_start(rng: str | None) -> int | None:
    """解析 Range 头起始字节（bytes 0- 或 None → None，表示从头/无范围）。"""
    if not rng:
        return None
    m = re.match(r"bytes[= ](\d*)-(\d*)", rng.strip(), re.IGNORECASE)
    if m and m.group(1):
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def _parse_range(rng: str | None, size: int) -> tuple[int, int] | None:
    """解析 Range: bytes=a-b / bytes=N- / bytes=-suffix，返回 (start, end)。

    不可满足（start >= size 或非法）返回 None → 上层回 416。
    """
    if not size or not rng:
        return None
    m = re.match(r"bytes[= ](\d*)-(\d*)", rng.strip(), re.IGNORECASE)
    if not m:
        return None
    a, b = m.group(1), m.group(2)
    if not a and not b:
        return None
    if not a:  # 后缀区间 bytes=-N
        suffix = int(b)
        if suffix <= 0:
            return None
        start = max(size - suffix, 0)
        return (start, size - 1)
    start = int(a)
    if start >= size:
        return None
    end = int(b) if b else size - 1
    return (start, min(end, size - 1))


def _full_zero_offset_response(resp, client_range: str | None = None) -> bool:
    if client_range:
        return False
    if resp.status_code == 200:
        return not resp.headers.get("Content-Range")
    if resp.status_code != 206:
        return False
    content_range = resp.headers.get("Content-Range") or ""
    match = re.fullmatch(r"bytes\s+(0)-(\d+)/(\d+)", content_range.strip())
    if match is None:
        return False
    total = int(match.group(3))
    length = resp.headers.get("Content-Length", "")
    return (total > 0 and int(match.group(2)) + 1 == total
            and (not length or (length.isdigit() and int(length) == total)))


def _is_contiguous_206(resp, offset: int) -> bool:
    """206 且 Content-Range 起始字节恰为 offset → 可安全续传（不重叠/无缺口）。"""
    if resp.status_code != 206:
        return False
    cr = resp.headers.get("Content-Range") or ""
    m = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", cr.strip())
    return bool(m and int(m.group(1)) == offset)


def _content_total(resp) -> int | None:
    """从响应推断完整媒体总长（200+Content-Length / 206+Content-Range）。

    tee 落盘时需知道 total 才能判断「写满 → complete」。分片/大文件未知总长
    时返回 None（退化为不缓存，保持播放优先）。
    """
    if resp.status_code == 200:
        cl = resp.headers.get("Content-Length")
        if cl:
            try:
                return int(cl)
            except (TypeError, ValueError):
                return None
        return None
    cr = resp.headers.get("Content-Range") or ""
    m = re.search(r"/(\d+)\s*$", cr)
    if m:
        try:
            return int(m.group(1))
        except (TypeError, ValueError):
            return None
    return None


class _FileTee:
    """流式转发同时落盘：写入 `<final>.part`，完整写毕原子 rename 为正式文件。

    - 写满（written >= total）→ commit_part（rename + 置 complete），此后命中
      直接本地 serve
    - 播放器提前断开 / 长度不符（拖动越过已下载区间的 gap）→ discard .part，
      不留半截文件；不做「部分文件 serve」——未完成一律继续走上游转发
    - 缓存写盘失败不影响播放转发（异常吞掉，仅退化为不缓存）
    """

    __slots__ = ("cache", "key", "part", "final", "total", "written",
                 "fh", "is_mp4", "done", "aborted", "resume")

    def __init__(self, cache, key: str, part, final, total: int | None,
                 is_mp4: bool, resume: int = 0):
        self.cache = cache
        self.key = key
        self.part = part
        self.final = final
        self.total = total
        self.resume = int(resume or 0)
        self.written = self.resume
        self.fh = None
        self.is_mp4 = is_mp4
        self.done = False
        self.aborted = False

    def start(self) -> bool:
        """打开 .part 准备写盘；失败返回 False（本次不缓存，仍继续转发）。

        resume>0：在既有 `.part` 尾部追加（续传），不截断已下载字节。
        """
        try:
            self.part.parent.mkdir(parents=True, exist_ok=True)
            self.fh = open(self.part, "ab" if self.resume else "wb")
            self.cache.inflight_add(self.key)
            return True
        except Exception:
            self.abort()
            return False

    def write(self, chunk: bytes) -> None:
        if not chunk or self.aborted or self.done or self.fh is None:
            return
        try:
            self.fh.write(chunk)
            self.written += len(chunk)
        except OSError:
            self.abort()

    def _discard_files(self, remove_final: bool = False) -> None:
        try:
            if self.fh is not None and not self.fh.closed:
                self.fh.close()
        except Exception:
            pass
        try:
            self.cache.discard_part(self.part)
        except Exception:
            try:
                self.part.unlink(missing_ok=True)
            except Exception:
                pass
        if remove_final:
            try:
                self.final.unlink(missing_ok=True)
            except Exception:
                pass

    def finish(self) -> None:
        """上游流正常结束：写满 → commit；否则丢弃 .part。"""
        if self.done or self.aborted:
            return
        committed = False
        failed = False
        try:
            try:
                if self.fh is not None and not self.fh.closed:
                    self.fh.close()
            except Exception:
                pass
            complete = self.total is not None and self.written == self.total
            if complete and self.cache.commit_part(self.part, self.final):
                committed = True
                if self.is_mp4:
                    self.cache.mark_mp4(self.key, self.total or self.written,
                                        complete=True)
                else:
                    self.cache.mark_hls_segment(self.key, self.written)
                self.cache.touch(self.key)
            elif complete:
                failed = True
            else:
                self.aborted = True
                self._discard_files()
        except Exception:
            failed = True
        finally:
            if failed:
                self._discard_files(remove_final=committed)
                self.aborted = True
            self.done = True
            try:
                self.cache.inflight_remove(self.key)
            except Exception:
                pass

    def abort(self) -> None:
        """放弃本次缓存：关文件、清 .part、退出 inflight。"""
        if self.aborted:
            return
        self.aborted = True
        self._discard_files()
        try:
            self.cache.inflight_remove(self.key)
        except Exception:
            pass


def _send_range_error(handler, status: int, headers: dict) -> None:
    handler.send_response(status)
    for name in ("Content-Range", "Accept-Ranges"):
        value = headers.get(name)
        if value:
            handler.send_header(name, value)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _send_cached_fetch_error(handler, state) -> None:
    if state.status == 416:
        _send_range_error(handler, 416, state.headers)
    else:
        handler.send_error(state.status or 502, "upstream error")


def _send_upstream_error(handler, resp) -> None:
    if resp.status_code != 416:
        handler.send_error(resp.status_code, "upstream error")
        return
    _send_range_error(handler, 416, resp.headers)


def _send_stream_headers(handler, resp, override_len: int | None = None) -> tuple:
    """透传上游媒体响应头并保证 HTTP/1.1 响应有正确定界。

    **不能声明 Connection: close**（handler 已是 HTTP/1.1）：HLS 播放器逐段
    拉分片、mp4 拖动逐个发 Range，每次新建连接都要重新握手 + 代理新建服务
    线程，是播放卡顿/拖动迟滞的主要来源。代价是每个响应都必须有定界：
    上游给了 Content-Length 就原样透传；上游是 chunked 则由我们重新按
    chunked 分帧（保持流式）；两者都没有（连接关闭定界）才退回
    close_connection，让客户端读到 EOF 为止。

    override_len：body 已被整读（如 gzip 分支 resp.content 已解压）时传入
    实际长度——此时上游的 Content-Length 是压缩态长度，与将写出的字节数不
    符，keep-alive 下会让客户端死等。

    返回 (chunked, declared_len)：declared_len 为 None 表示长度未知。
    """
    handler.send_response(resp.status_code)
    for hk in _MEDIA_HDRS:
        if hk == "Content-Length" and override_len is not None:
            continue
        hv = resp.headers.get(hk)
        if hv:
            handler.send_header(hk, hv)
    if override_len is not None:
        raw_len = str(override_len)
        handler.send_header("Content-Length", raw_len)
    else:
        raw_len = resp.headers.get("Content-Length")
    declared = int(raw_len) if (raw_len or "").isdigit() else None
    chunked = False
    if declared is None:
        if (resp.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            handler.send_header("Transfer-Encoding", "chunked")
            chunked = True
        else:
            handler.close_connection = True
    handler.end_headers()
    return chunked, declared


def _frame(chunk: bytes, chunked: bool) -> bytes:
    """chunked 响应分帧；非 chunked 原样透传。"""
    if not chunked:
        return chunk
    return b"%x\r\n" % len(chunk) + chunk + b"\r\n"


class _ProxyHandler(BaseHTTPRequestHandler):
    """单请求处理器。

    - /s/<token> → 按 token 找目标 URL + headers 转发（媒体流 / m3u8）
    - /c/<key>/<segment> → HLS 分片磁盘缓存路由：命中本地 serve，未命中
      回落上游代理转发并 tee 落盘
    - /e/<key>/<idx> → 惰性系列：按集解析（失败 502 不缓存）→ 302 到 /s/<token>
    """

    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ #
    def do_GET(self):  # noqa: N802
        self._response_started = False
        proxy: "MediaProxy" = self.server.proxy  # type: ignore[attr-defined]
        public_base = (
            getattr(self.server, "public_base", None)
            if getattr(self.server, "is_lan", False) else None
        )
        proxy._touch()
        proxy._begin()  # 活动请求计数：长连接流式期间看门狗不停止、不移除 token
        try:
            # /c/<cache-key>/<upstream-url> keeps the upstream query string
            # visible so HLS players still see a normal `.ts?auth=...` URL.
            # Preserve it for the cache route instead of dropping it while
            # dispatching by path.
            raw_path, query_sep, raw_query = self.path.partition("?")
            path = unquote(raw_path)
            if path.startswith("/c/") and query_sep:
                path += "?" + raw_query
            if path.startswith("/c/"):
                try:
                    proxy._serve_cache(self, path, public_base)
                except Exception as exc:  # noqa: BLE001 —— 网络波动直接断流
                    try:
                        self.send_error(502, f"proxy error: {exc}")
                    except Exception:
                        pass
                return
            if path.startswith("/t/"):
                proxy._serve_transcode(self, path, public_base)
                return
            if path.startswith("/e/"):
                try:
                    proxy._serve_series(self, path, public_base)
                except Exception as exc:  # noqa: BLE001 —— 解析异常已在内部归类
                    try:
                        self.send_error(502, f"proxy error: {exc}")
                    except Exception:
                        pass
                return
            token = path.rsplit("/", 1)[-1]
            entry = proxy._tokens.get(token)
            if entry is None:
                self.send_error(404, "token not found")
                return
            target, headers, ad_block = entry[0], entry[1], entry[2]
            source_id = _ctx_get(entry, 4, "")
            force_proxy = _tuple_force_proxy(entry)
            try:
                # 拖动进度（Range 非 0 起始）若越过正在下载的缓存区间，先放弃
                # 本次 mp4 tee（不做半截文件）；本次请求仍照常走上游 206。
                if self.headers.get("Range"):
                    proxy._abort_tee_on_gap(target, self.headers.get("Range"))
                proxy._forward(self, target, headers, ad_block, force_proxy,
                               public_base, source_id)
            except Exception as exc:  # noqa: BLE001 —— 网络波动直接断流，播放器会提示
                try:
                    self.send_error(502, f"proxy error: {exc}")
                except Exception:
                    pass
        finally:
            proxy._end()

    def log_message(self, *args):  # 静音访问日志
        pass

    def end_headers(self):
        self._response_started = True
        super().end_headers()

    def send_error(self, code, message=None, explain=None):
        """错误响应：先标记连接关闭。

        流式响应体可能已写出一半，此时再发错误头会与已发字节错位，
        复用该连接的客户端会拿到乱序数据。出错即关闭连接最安全
        （代价仅是该次请求，与 keep-alive 无关）。
        """
        self.close_connection = True
        if not getattr(self, "_response_started", False):
            super().send_error(code, message, explain)


class MediaProxy:
    """本地流媒体代理（单例）。

    集成了 framework.media_cache 的磁盘视频缓存：
    - mp4 在流式转发时 tee 落盘，完整写毕后同一影片后续请求直接本地 serve
    - HLS 播放列表（已过滤广告）与分片经 /c/<key>/ 路由落盘，重播秒开
    - 看门狗空闲回收 + 活动请求计数：暂停/拖动进度不会被误停。
    """

    _instance: "MediaProxy | None" = None

    def __init__(self, cache=None, prefetch=None):
        self._tokens: dict[str, tuple] = {}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lan_server: ThreadingHTTPServer | None = None
        self._lan_thread: threading.Thread | None = None
        self._lan_base: str | None = None
        self._lan_tokens: set[str] = set()
        self._transcode_dirs: dict[str, Path] = {}
        self._lan_transcode_tokens: set[str] = set()
        self._lan_lock = threading.RLock()
        self._lan_error: Exception | None = None
        self._last_use = time.time()
        self._idle_watch: threading.Thread | None = None
        self._active = 0  # 进行中的请求数（含流式长连接）：>0 时 stop 不执行
        # 外部播放器租约：VLC 进程存活期间持有的 lease id 集合。**非空时
        # 空闲看门狗完全跳过回收** —— 否则用户暂停超 _IDLE_TIMEOUT 后
        # stop() 会 _tokens.clear()，VLC 恢复播放时全部 404。VLC 退出 /
        # 重开播放器 / App 退出时由 external_player 释放。
        self._leases: set[str] = set()
        # 惰性系列注册表：key → {resolver, on_play, count, memo, sem}
        #   memo: {(endpoint_base, idx): (video, audio, location)} 按端点缓存，
        #   避免 localhost 与 LAN Location 互相污染。
        #   sem:  该系列独占的解析并发信号量
        self._series: dict[str, dict] = {}
        self._cache = cache  # 可注入测试用缓存实例；None → 懒加载单例
        # cache_ctx key → (base_url, headers, ad_block, force_proxy, final_base)
        # （当前五元组；旧三元组/四元组读取仍兼容，endpoint base 不存入 cache_ctx）
        self._cache_ctx: dict[str, tuple] = {}
        self._manifest_cache: dict[str, tuple[float, str]] = {}
        self._mp4_tee: dict[str, _FileTee] = {}  # key → 进行中的 mp4 落盘
        # HLS 有界预取（默认关，见 _pf_cfg）
        self._pf_cfg = self._resolve_prefetch_cfg(prefetch)
        self._seg_order: dict[str, list[str]] = {}    # cache_key → 有序分片 URL
        self._seg_pos: dict[str, dict[str, int]] = {}  # cache_key → URL → 序号
        self._pf_scheduled: set[tuple[str, str]] = set()  # 已排队 (key, url) 去重
        self._pf_futures: set = set()
        self._pf_pool = None
        self._pf_pool_workers: int | None = None
        self._pf_lock = threading.Lock()
        self._pf_cancel = threading.Event()
        self._pf_cancelled_urls: dict[str, threading.Event] = {}
        self._pf_responses: dict[str, object] = {}
        self._pf_generation = 0
        self._pf_failures = 0
        self._pf_disabled = False
        self._start_idle_watch()
        atexit.register(self.stop)

    # ------------------------------------------------------------------ #
    # HLS 有界预取
    # ------------------------------------------------------------------ #
    # 实测部分 CDN **按连接限速**（ikanpp 单连接 33~190KB/s，实时播放需
    # >=136KB/s；4 并发总带宽 280.2KB/s ≈ 单连接顺序 133.2KB/s 的 2.1x）。
    # 预取按「当前消费到第 N 片」前瞻 depth 片、workers 路并发落盘，VLC 随后
    # 直接命中 /c/ 本地文件。窗口有界（不扫完整清单）、同片去重、playback
    # 停止即随 stop() 结束。
    # 默认**关闭**：开启会改变对源站的请求模式（并发+提前拉），需实测确认
    # 不触发风控后再手动打开（app_config.json → hls_prefetch.enabled）。
    @staticmethod
    def _read_prefetch_settings() -> dict:
        try:
            from .media_cache import _base_dir
            from .settings_manager import SettingsManager
            sm = SettingsManager(_base_dir() / "app_config.json")
            return sm.get_section("hls_prefetch") or {}
        except Exception:  # noqa: BLE001 —— 配置缺失/损坏按默认（关闭）
            return {}

    def _resolve_prefetch_cfg(self, prefetch) -> dict:
        sec = dict(prefetch) if prefetch is not None else self._read_prefetch_settings()
        try:
            depth = int(sec.get("depth", 4))
        except (TypeError, ValueError):
            depth = 4
        try:
            workers = int(sec.get("workers", 3))
        except (TypeError, ValueError):
            workers = 3
        return {
            "enabled": bool(sec.get("enabled", False)),
            "depth": max(1, min(_PREFETCH_MAX_DEPTH, depth)),
            "workers": max(1, min(_PREFETCH_MAX_WORKERS, workers)),
        }

    def _set_seg_order(self, cache_key: str, segs: list[str]) -> None:
        """登记某播放列表的有序分片（每次重写整体替换，避免直播刷新重复累加）。"""
        with self._pf_lock:
            self._seg_order[cache_key] = segs
            self._seg_pos[cache_key] = {u: i for i, u in enumerate(segs)}

    def _ensure_pf_pool(self):
        old_pool = None
        with self._pf_lock:
            workers = self._pf_cfg["workers"]
            if (self._pf_pool is None
                    or self._pf_pool_workers != workers):
                old_pool, self._pf_pool = self._pf_pool, None
                from concurrent.futures import ThreadPoolExecutor
                self._pf_pool = ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix="claw-pf")
                self._pf_pool_workers = workers
            pool = self._pf_pool
        if old_pool is not None:
            # 线程池不能动态修改 max_workers；取消尚未开始的旧源任务，
            # 已运行任务仍由其取消事件/超时自然收尾，不阻塞播放线程。
            try:
                old_pool.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        return pool

    def configure_prefetch(self, enabled: bool, depth: int = 16, workers: int = 3) -> None:
        # 播放会话按源隔离：上一个源的 403/429 熔断不能让新源永久失去预取。
        try:
            depth = int(depth)
        except (TypeError, ValueError):
            depth = 16
        try:
            workers = int(workers)
        except (TypeError, ValueError):
            workers = 3
        old_pool = None
        responses = []
        with self._pf_lock:
            self._pf_generation += 1
            self._pf_cfg["enabled"] = bool(enabled)
            self._pf_cfg["depth"] = max(1, min(depth, _PREFETCH_MAX_DEPTH))
            self._pf_cfg["workers"] = max(1, min(workers, _PREFETCH_MAX_WORKERS))
            self._pf_failures = 0
            self._pf_disabled = False
            # 切源/重播时取消旧窗口，防止迟到分片写进新会话；未开始的任务
            # 由 shutdown 取消，正在读取的任务通过逐 URL event 收尾。
            for event in self._pf_cancelled_urls.values():
                event.set()
            responses = list(self._pf_responses.values())
            old_pool, self._pf_pool = self._pf_pool, None
            self._pf_pool_workers = None
            self._pf_cancelled_urls.clear()
            self._pf_scheduled.clear()
            self._seg_order.clear()
            self._seg_pos.clear()
        for response in responses:
            try:
                response.close()
            except Exception:
                pass
        if old_pool is not None:
            try:
                old_pool.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

    def _prefetch_enabled(self) -> bool:
        return bool(self._pf_cfg.get("enabled")) and not self._pf_disabled

    def _prefetch_url_cancelled(self, canonical: str,
                                generation: int | None = None) -> bool:
        with self._pf_lock:
            event = self._pf_cancelled_urls.get(canonical)
            stale = (generation is not None
                     and generation != self._pf_generation)
        return stale or self._pf_cancel.is_set() or (event is not None and event.is_set())

    def _cancel_prefetch_url(self, url: str) -> None:
        cache = self._cache_obj()
        if cache is None:
            return
        canonical = cache.key_of(url)
        with self._pf_lock:
            event = self._pf_cancelled_urls.get(canonical)
            if event is not None:
                event.set()
            response = self._pf_responses.get(canonical)
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    def _note_prefetch_failure(self, status=None, exc=None) -> None:
        if status in (403, 429):
            with self._pf_lock:
                self._pf_disabled = True
            return
        connection_failure = isinstance(
            exc, (requests.ConnectionError, requests.Timeout)
        )
        if not connection_failure:
            return
        with self._pf_lock:
            self._pf_failures += 1
            if self._pf_failures >= _PREFETCH_FAILURE_LIMIT:
                self._pf_disabled = True

    def _note_prefetch_success(self) -> None:
        with self._pf_lock:
            self._pf_failures = 0

    def _maybe_prefetch(self, cache_key: str, full_url: str) -> None:
        if not self._prefetch_enabled() or self._pf_cancel.is_set():
            return
        cache = self._cache_obj()
        if cache is None or not cache.enabled:
            return
        with self._lock:
            ctx = self._cache_ctx.get(cache_key)
            pos = self._seg_pos.get(cache_key)
        with self._pf_lock:
            generation = self._pf_generation
        if ctx is None or not pos:
            return
        idx = pos.get(full_url)
        if idx is None:
            return
        order = self._seg_order.get(cache_key) or []
        window = order[idx + 1: idx + 1 + self._pf_cfg["depth"]]
        if not window:
            return
        headers = dict(ctx[1])
        headers["Accept-Encoding"] = "identity"
        force_proxy = _tuple_force_proxy(ctx)
        for u in window:
            canonical = cache.key_of(u)
            with self._pf_lock:
                if len(self._pf_futures) >= _PREFETCH_MAX_PENDING:
                    break
                if canonical in self._pf_scheduled:
                    continue
                if cache.hls_segment_final(cache_key, u).is_file():
                    continue
                self._pf_scheduled.add(canonical)
                self._pf_cancelled_urls[canonical] = threading.Event()
            try:
                fut = self._ensure_pf_pool().submit(
                    self._prefetch_one, cache_key, u, dict(headers), force_proxy,
                    generation)
            except RuntimeError:
                return
            with self._pf_lock:
                self._pf_futures.add(fut)
            fut.add_done_callback(self._prefetch_future_done)

    def _prefetch_future_done(self, future) -> None:
        with self._pf_lock:
            self._pf_futures.discard(future)

    def _prefetch_one(self, cache_key: str, url: str, headers: dict,
                      force_proxy: bool, generation: int | None = None) -> None:
        cache = self._cache_obj()
        if cache is None:
            return
        if generation is None:
            with self._pf_lock:
                generation = self._pf_generation
        canonical = cache.key_of(url)
        resp = None
        lock = cache.path_lock(url)
        acquired = False
        try:
            deadline = time.monotonic() + _PREFETCH_TIMEOUT
            acquired = lock.acquire(timeout=_PREFETCH_TIMEOUT)
            if not acquired:
                return
            final = cache.hls_segment_final(cache_key, url)
            for attempt in range(2):
                if (self._prefetch_url_cancelled(canonical, generation)
                        or self._pf_disabled
                        or final.is_file()):
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                try:
                    resp = _fetch_upstream(
                        url, headers, force_proxy=force_proxy,
                        timeout=(remaining, remaining), segment=True,
                        retry_connect=False,
                    )
                except Exception as exc:
                    self._note_prefetch_failure(exc=exc)
                    return
                force_proxy = force_proxy or (
                    getattr(resp, "_media_proxy_diagnostic", {}).get("route") == "proxy")
                with self._pf_lock:
                    self._pf_responses[canonical] = resp
                tee = None
                try:
                    if resp.status_code >= 400:
                        self._note_prefetch_failure(status=resp.status_code)
                        return
                    total = _content_total(resp)
                    if (not total or not _full_zero_offset_response(resp)
                            or (resp.headers.get("Content-Encoding") or "identity").lower() != "identity"):
                        return
                    tee = _FileTee(cache, cache_key,
                                   cache.hls_segment_part(cache_key, url), final,
                                   total, is_mp4=False)
                    if not tee.start():
                        return
                    while True:
                        if self._prefetch_url_cancelled(canonical, generation):
                            return
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise requests.Timeout("prefetch read timeout")
                        _set_response_read_timeout(resp, remaining)
                        chunk = _read_available(resp.raw, _READ_CHUNK)
                        if not chunk:
                            break
                        tee.write(chunk)
                    if tee.written != total:
                        raise requests.ConnectionError("incomplete prefetch body")
                    if self._prefetch_url_cancelled(canonical, generation):
                        return
                    tee.finish()
                    if not tee.aborted:
                        self._note_prefetch_success()
                    return
                except (requests.ConnectionError, requests.Timeout, UpstreamReadError) as exc:
                    if attempt:
                        self._note_prefetch_failure(exc=requests.ConnectionError(str(exc)))
                except Exception as exc:
                    self._note_prefetch_failure(exc=exc)
                    return
                finally:
                    if tee is not None and not tee.done:
                        tee.abort()
                    resp.close()
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._pf_cancel.wait(min(0.1, remaining))
        finally:
            if acquired:
                lock.release()
            with self._pf_lock:
                self._pf_responses.pop(canonical, None)
                self._pf_scheduled.discard(canonical)
                self._pf_cancelled_urls.pop(canonical, None)
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass

    def _prefetch_drain(self, timeout: float = 5.0) -> bool:
        """等已排队的预取任务跑完（测试用；返回是否全部完成）。"""
        from concurrent.futures import wait as _cf_wait
        with self._pf_lock:
            futs = set(self._pf_futures)
        if not futs:
            return True
        _done, pending = _cf_wait(futs, timeout=timeout)
        with self._pf_lock:
            self._pf_futures -= pending
        return not pending

    def _shutdown_prefetch(self) -> None:
        from concurrent.futures import wait as _cf_wait
        with self._pf_lock:
            self._pf_generation += 1
            self._pf_cancel.set()
            pool, self._pf_pool = self._pf_pool, None
            futures = set(self._pf_futures)
            responses = list(self._pf_responses.values())
        for response in responses:
            try:
                response.close()
            except Exception:
                pass
        if futures:
            _cf_wait(futures, timeout=_PREFETCH_CANCEL_WAIT)
        if pool is not None:
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        with self._pf_lock:
            self._pf_futures.clear()
            self._pf_responses.clear()
            self._pf_cancelled_urls.clear()
            self._seg_order.clear()
            self._seg_pos.clear()
            self._pf_scheduled.clear()

    # ------------------------------------------------------------------ #
    @classmethod
    def instance(cls) -> "MediaProxy":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------ #
    def _start_idle_watch(self) -> None:
        def _watch():
            while True:
                time.sleep(_WATCH_INTERVAL)
                if self._idle_expired():
                    self.stop()
        self._idle_watch = threading.Thread(target=_watch, daemon=True)
        self._idle_watch.start()

    def _idle_expired(self) -> bool:
        """看门狗回收判据：代理已起 + 空闲超时 + **无外部播放器租约**。"""
        with self._lock:
            if self._leases:
                return False
        return (self._server is not None
                and time.time() - self._last_use > _IDLE_TIMEOUT)

    def acquire_lease(self) -> str:
        """登记一个外部播放器租约（返回 lease id）。

        只要有租约，空闲看门狗就不会 stop() 代理、不会清 token —— 播放器
        暂停/长时间不发起请求时 URL 仍有效。成对调用 release_lease()。
        """
        lease_id = uuid.uuid4().hex
        with self._lock:
            self._leases.add(lease_id)
        return lease_id

    def release_lease(self, lease_id: str) -> None:
        """释放租约（幂等：未知 id 静默忽略）。"""
        with self._lock:
            self._leases.discard(lease_id)

    def _touch(self) -> None:
        self._last_use = time.time()

    def transport_metrics(self) -> list[dict]:
        """返回脱敏的上游传输指标快照。"""
        with _UPSTREAM_DIAGNOSTICS_LOCK:
            records = list(_UPSTREAM_DIAGNOSTICS)
        metrics = []
        for record in records:
            metric = {
                "kind": record.get("request_kind"),
                "status": record.get("status_code"),
                "first_byte_ms": record.get("first_byte_ms"),
                "bytes": record.get("bytes", 0),
                "duration_ms": record.get("elapsed_ms"),
            }
            if record.get("failure_category"):
                metric["failure_category"] = record["failure_category"]
            metrics.append(metric)
        return metrics

    def clear_transport_metrics(self) -> None:
        """清空代理进程内的传输指标队列。"""
        _reset_upstream_route_diagnostics()

    def _begin(self) -> None:
        with self._lock:
            self._active += 1

    def _end(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    def _cache_obj(self):
        """返回已注入的缓存实例或懒加载单例（缓存被禁用时返回 None 用。）。"""
        return self._cache

    def _ensure_cache(self):
        if self._cache is None:
            try:
                from .media_cache import MediaCache
                self._cache = MediaCache.instance()
            except Exception:  # noqa: BLE001 —— 缓存不可用则跳过缓存路径
                self._cache = None
        return self._cache

    def _cache_enabled(self) -> bool:
        cache = self._ensure_cache()
        return bool(cache is not None and cache.enabled)

    def _wait_for_cached_fetch(self, handler, cache, key: str, target: str,
                               headers: dict) -> bool:
        state = cache.fetch_state(key)
        if state is None:
            return False
        started = time.perf_counter()
        if not cache.wait_fetch(state, _CACHE_FETCH_WAIT):
            handler.send_error(504, "cache fetch timeout")
            return True
        wait_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
        if state.error is not None:
            handler.send_error(state.status or 502, "upstream error")
            return True
        entry = cache.entry(key)
        final = cache.mp4_final(key)
        if entry and entry.get("complete") and final.is_file():
            cache.touch(key)
            _record_cache_diagnostic(target, headers, "wait", wait_ms)
            self._serve_local_file(handler, final)
            return True
        return False

    # 缓存上下文注册：m3u8 被重写时把 key →
    # (请求 URL, headers, ad_block, force_proxy, final base) 记下；endpoint base
    # 由当前收到请求的 server 提供，避免 local/LAN 串线。
    # final base = 重写基准（resp.url 或请求 URL），_serve_cache 定位上游时用；
    # 请求 URL 单独保留，保证 disk/memory 缓存键隔离语义不变。
    def _register_cache_ctx(self, key: str, target: str, headers: dict,
                            ad_block: dict | None,
                            force_proxy: bool = False,
                            final_base: str | None = None,
                            source_id=None) -> None:
        with self._lock:
            self._cache_ctx[key] = (
                _context_target(target), _clean_context_headers(headers),
                ad_block, force_proxy,
                final_base or _context_target(target),
                source_id,
            )


    def _register_cache_ctx_if_missing(self, key: str, target: str,
                                       headers: dict, ad_block: dict | None,
                                       force_proxy: bool = False,
                                       source_id=None) -> None:
        with self._lock:
            if key in self._cache_ctx:
                return
            self._cache_ctx[key] = (
                _context_target(target), _clean_context_headers(headers),
                ad_block, force_proxy, _context_target(target), source_id,
            )

    # ------------------------------------------------------------------ #
    def _base_url(self, public_base: str | None = None) -> str:
        if public_base:
            public_base = public_base.rstrip("/")
            with self._lan_lock:
                if self._lan_error is not None:
                    raise OSError("LAN endpoint is in a failed lifecycle state") from self._lan_error
                if public_base != self._lan_base:
                    raise ValueError("public_base must be the active LAN endpoint")
            return public_base
        self._ensure_server()
        return self._server.public_base  # type: ignore[union-attr]

    def build_url(self, target_url: str, headers: dict | None = None,
                  ad_block: dict | None = None,
                  force_proxy: bool = False,
                  public_base: str | None = None,
                  source_id=None) -> str:

        """把目标媒体 URL 打包成本地代理 URL（播放器直接播这个）。

        ad_block：可选源 ad_block 配置。存在时代理转发 m3u8 会剔除广告段
        （下载路径已有过滤；播放路径此前无过滤，广告分片会照播）。
        force_proxy：True 时该 token 的所有上游转发一律走系统代理会话
        （跳过直连探测）；默认策略为直连优先、失败回退代理。
        public_base：非空时使用投屏 LAN 端点，并将 token 归入 LAN 生命周期。
        """
        if not target_url:
            return ""
        base = self._base_url(public_base)
        token = uuid.uuid4().hex
        token_context = (
            target_url, _strip_stale_headers(headers or {}), ad_block, force_proxy,
        )
        if trusted_source_id(source_id) == "18j" or _remux_active(ad_block):
            # 18j 的可信上下文、remux 开关都要随 token 一路带到 /c/ 分片，
            # 否则变体清单过滤与分片重写会丢掉策略（令牌位置不固定 → _ctx_like）。
            token_context = token_context + (source_id,)

        with self._lock:

            self._tokens[token] = token_context

            if public_base:
                self._lan_tokens.add(token)
        return f"{base}/s/{token}"

    def register_transcode_directory(self, directory: str | Path, public_base: str) -> str:
        """注册一个由当前 LAN endpoint 提供的只读 HLS 目录。"""
        base = self._base_url(public_base)
        root = Path(directory).resolve()
        if not root.is_dir():
            raise ValueError("directory must be an existing directory")
        token = uuid.uuid4().hex
        with self._lan_lock:
            if self._lan_base != base or self._lan_server is None:
                raise ValueError("public_base must be the active LAN endpoint")
            with self._lock:
                self._transcode_dirs[token] = root
            self._lan_transcode_tokens.add(token)
        return f"{base}/t/{token}"

    def unregister_transcode_directory(self, token: str) -> None:
        """注销转码目录；token 或其公开 URL 均可传入，且操作幂等。"""
        raw = str(token).rstrip("/")
        if "/t/" in raw:
            raw = raw.rsplit("/t/", 1)[1].split("/", 1)[0]
        else:
            raw = raw.rsplit("/", 1)[-1]
        with self._lan_lock:
            self._lan_transcode_tokens.discard(raw)
            with self._lock:
                self._transcode_dirs.pop(raw, None)

    def _serve_transcode(self, handler, path: str,
                         public_base: str | None = None) -> None:
        if public_base is None:
            handler.send_error(404, "LAN endpoint required")
            return
        parts = path[len("/t/"):].split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            handler.send_error(404, "bad transcode path")
            return
        token, relative = parts
        with self._lan_lock:
            active = token in self._lan_transcode_tokens and public_base == self._lan_base
            with self._lock:
                root = self._transcode_dirs.get(token)
        if root is None or not active:
            handler.send_error(404, "transcode directory not found")
            return
        if posixpath.normpath(relative) != relative or relative.startswith("../"):
            handler.send_error(404, "invalid transcode path")
            return
        candidate = (root / Path(relative)).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            handler.send_error(404, "invalid transcode path")
            return
        if not candidate.is_file():
            handler.send_error(404, "transcode file not found")
            return
        content_type = mimetypes.guess_type(candidate.name)[0]
        if candidate.suffix.lower() == ".m3u8":
            content_type = "application/vnd.apple.mpegurl"
        elif candidate.suffix.lower() == ".ts":
            content_type = "video/mp2t"
        body_length = candidate.stat().st_size
        handler.send_response(200)
        handler.send_header("Content-Type", content_type or "application/octet-stream")
        handler.send_header("Content-Length", str(body_length))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        with candidate.open("rb") as stream:
            while True:
                chunk = stream.read(_READ_CHUNK)
                if not chunk:
                    break
                handler.wfile.write(chunk)

    def start_lan_endpoint(self, bind_host: str) -> str:
        """启动 LAN server，返回 http://<bind_host>:<port>。"""
        if not isinstance(bind_host, str) or not bind_host.strip():
            raise ValueError("bind_host must be a non-empty IP address")
        bind_host = bind_host.strip()
        try:
            address = ipaddress.ip_address(bind_host)
        except ValueError as exc:
            raise ValueError("bind_host must be a literal IP address") from exc
        if address.is_unspecified:
            raise ValueError("bind_host must not be a wildcard/unspecified address")
        with self._lan_lock:
            if self._lan_server is not None:
                if self._lan_error is not None:
                    raise OSError("LAN endpoint is in a failed lifecycle state") from self._lan_error
                if self._lan_thread is not None and self._lan_thread.is_alive():
                    return self._lan_base  # type: ignore[return-value]
                stale_server = self._lan_server
                stale_thread = self._lan_thread
                try:
                    stale_server.shutdown()
                    stale_server.server_close()
                    if stale_thread is not None:
                        stale_thread.join(timeout=2.0)
                    if stale_thread is not None and stale_thread.is_alive():
                        raise RuntimeError("LAN endpoint thread did not stop")
                except Exception as exc:
                    self._lan_error = exc
                    raise OSError("LAN endpoint could not be restarted cleanly") from exc
                self._lan_server = None
                self._lan_thread = None
                self._lan_base = None
                self._lan_error = None
            server = ThreadingHTTPServer((bind_host, 0), _ProxyHandler)
            host = f"[{bind_host}]" if address.version == 6 else bind_host
            base = f"http://{host}:{server.server_address[1]}"
            server.proxy = self  # type: ignore[attr-defined]
            server.public_base = base  # type: ignore[attr-defined]
            server.is_lan = True  # type: ignore[attr-defined]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            self._lan_server = server
            self._lan_thread = thread
            self._lan_base = base
            self._lan_error = None
            try:
                thread.start()
            except Exception:
                try:
                    server.server_close()
                finally:
                    self._lan_server = None
                    self._lan_thread = None
                    self._lan_base = None
                    self._lan_error = None
                raise
            return base

    def release_token(self, url: str) -> None:
        """公开、线程安全的 token 释放接口。按 URL 从 _tokens 中移除。

        如果 URL 对应的 token 是 LAN token，同时从 _lan_tokens 中移除。
        该接口供外部（如 CastSession）调用，避免直接操作私有容器。
        """
        token = url.rsplit("/", 1)[-1]
        with self._lock:
            self._tokens.pop(token, None)
            self._lan_tokens.discard(token)

    def release_lan_tokens(self) -> None:
        """只清理投屏端点生成的 tokens。"""
        with self._lock:
            for token in tuple(self._lan_tokens):
                self._tokens.pop(token, None)
            self._lan_tokens.clear()

    def _release_lan_transcode_directories(self) -> None:
        with self._lan_lock:
            tokens = tuple(self._lan_transcode_tokens)
            self._lan_transcode_tokens.clear()
        with self._lock:
            for token in tokens:
                self._transcode_dirs.pop(token, None)

    def lan_endpoint_error(self) -> Exception | None:
        """公开只读接口：返回最近一次 LAN 端点生命周期失败的错误。

        只读、不清除——失败生命周期标记由代理自身在
        start_lan_endpoint()/stop_lan_endpoint() 成功时复位。
        调用方（如 CastSession）经此感知 stop_lan_endpoint() 的静默失败，
        不得改写该标记，否则 Task 2 的 failed-lifecycle 守卫会被解除。
        """
        with self._lan_lock:
            return self._lan_error

    def stop_lan_endpoint(self) -> None:
        """只停止 LAN server，并清理 LAN-owned tokens。

        失败时设置 _lan_error 但不抛异常（保持向后兼容）。
        调用方可通过 lan_endpoint_error() 感知失败。
        """
        with self._lan_lock:
            server = self._lan_server
            thread = self._lan_thread
            if server is None:
                self.release_lan_tokens()
                self._release_lan_transcode_directories()
                return
            self.release_lan_tokens()
            self._release_lan_transcode_directories()
            try:
                server.shutdown()
                server.server_close()
                if thread is not None:
                    thread.join(timeout=2.0)
                if thread is not None and thread.is_alive():
                    raise RuntimeError("LAN endpoint thread did not stop")
            except Exception as exc:
                self._lan_error = exc
                return
            self._lan_server = None
            self._lan_thread = None
            self._lan_base = None
            self._lan_error = None

    # ------------------------------------------------------------------ #
    # 惰性系列：外部播放器全集播放列表
    # ------------------------------------------------------------------ #
    def register_series(self, resolver, on_play=None, count: int = 0,
                        force_proxy: bool = False, source_id: str = "") -> str:

        """注册一支惰性解析系列，返回不透明 key。

        resolver(idx) -> (video, audio, headers, ad_block)；抛异常表示该集取流
        失败（/e/ 回 502 且不 memo，允许重试）。ad_block 传 None 即不过滤广告段。
        on_play(idx, video, audio) 在**每次** /e/ 请求时回调（memo 命中也回调），
        供 App 侧同步「正在播第几集」——在代理线程执行，实现方须自行跨线程。
        count 为集数上限，越界回 404。force_proxy 透传给 build_url。
        """
        self._ensure_server()
        key = uuid.uuid4().hex[:12]
        with self._lock:
            self._series[key] = {
                "resolver": resolver, "on_play": on_play, "count": int(count),
                "memo": {}, "sem": threading.Semaphore(_SERIES_SEM),
                "force_proxy": bool(force_proxy),
                "source_id": source_id,
            }
            while len(self._series) > _SERIES_MAX:   # FIFO 兜底：淘汰最旧一支
                self._series.pop(next(iter(self._series)))
        return key

    def unregister_series(self, key: str) -> None:
        """注销系列并丢弃其 memo（幂等）。"""
        with self._lock:
            self._series.pop(key, None)

    def warm_series_episode(self, key: str, idx: int, video: str,
                            audio: str = "", headers: dict | None = None,
                             ad_block: dict | None = None,
                             public_base: str | None = None,
                             source_id: str = "") -> bool:

        """把**已知的**取流结果预写入系列 memo（纯优化，无副作用）。

        点集流程 _FetchStreamTask 已用同款 merged=True 取过当前集的流，
        再让 VLC 首个 /e/ 请求同步跑 resolver 是 2.6-4.0s 的纯重复等待；
        warm 后首个 /e/ 命中 memo 直接 302，resolver 留给其它集/换集。
        与 _serve_series 同一写法：锁内校验 key/idx（注销竞态守卫同款
        cur is not None），build_url 铸 token 在锁外 try 包裹——铸失败
        返回 False 且不写 memo，VLC 请求时照常回落 resolver。
        越界 / 未知 key / 空流一律 False：调用方无需回退分支。
        """
        if not video:
            return False
        endpoint_base = self._base_url(public_base)
        lan_base = public_base if public_base == self._lan_base else None
        with self._lock:
            ser = self._series.get(key)
            if ser is None or not (0 <= int(idx) < int(ser["count"])):
                return False
            force_proxy = ser["force_proxy"]
            source_id = ser.get("source_id", "")

        try:
            entry = (video, audio,
                     self.build_url(video, headers or {},
                                    ad_block=ad_block,
                                     force_proxy=force_proxy,
                                     public_base=lan_base,
                                     source_id=source_id))

        except Exception:  # noqa: BLE001 —— 铸 token 失败：回落 resolver
            return False
        with self._lock:
            cur = self._series.get(key)   # 解析期间可能已被注销
            if cur is None:
                return False
            cur["memo"][(endpoint_base, int(idx))] = entry
        return True

    def series_episode_url(self, key: str, idx: int,
                           public_base: str | None = None) -> str:
        """第 idx 集的惰性 URL（绝对地址，不含 #标题 fragment）。"""
        base = self._base_url(public_base)
        return f"{base}/e/{key}/{int(idx)}"

    def _serve_series(self, handler, path: str,
                      public_base: str | None = None) -> None:
        """/e/<key>/<idx>：按集惰性解析 → 302 到 /s/<token>。"""
        parts = path[len("/e/"):].split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            handler.send_error(404, "bad series path")
            return
        key, raw_idx = parts[0], parts[1]
        # isdecimal 而非 isdigit：isdigit() 对上标（如 "²"）返回 True 而 int()
        # 抛 ValueError → 落到外层 502，并把客户端输入回显进 HTTP reason
        # phrase。长度上限 9 位：避免超长数字串触发 3.11+ 的 int 转换位长限制。
        if not raw_idx.isdecimal() or len(raw_idx) > 9:
            handler.send_error(404, "bad episode index")
            return
        idx = int(raw_idx)
        endpoint_base = self._base_url(public_base)
        lan_base = public_base if public_base == self._lan_base else None
        with self._lock:
            ser = self._series.get(key)
            if ser is None:
                handler.send_error(404, "series not found")
                return
            if not (0 <= idx < int(ser["count"])):
                handler.send_error(404, "episode out of range")
                return
            entry = ser["memo"].get((endpoint_base, idx))
            sem = ser["sem"]
            resolver, on_play = ser["resolver"], ser["on_play"]
            force_proxy = ser["force_proxy"]
            source_id = ser.get("source_id", "")

        if entry is None:
            if not sem.acquire(timeout=_SERIES_WAIT):
                handler.send_error(503, "too many concurrent resolves")
                return
            try:
                video, audio, headers, ad_block = resolver(idx)
            except Exception:  # noqa: BLE001 —— 该集取流失败：502 且**不**写 memo
                handler.send_error(502, "episode resolve failed")
                return
            finally:
                sem.release()
            if not video:
                handler.send_error(502, "empty stream url")
                return
            # memo 存 (video, audio, location)：命中时直接拿 token URL 重定向，
            # 既不回源也不用再扫 _tokens 反查。
            entry = (video, audio,
                     self.build_url(video, headers, ad_block=ad_block,
                                      force_proxy=force_proxy,
                                     public_base=lan_base,
                                     source_id=source_id))


            with self._lock:
                cur = self._series.get(key)      # 解析期间可能已被注销
                if cur is not None:
                    cur["memo"][(endpoint_base, idx)] = entry
        if on_play is not None:
            try:
                on_play(idx, entry[0], entry[1])
            except Exception:  # noqa: BLE001 —— 回调异常不阻断播放
                pass
        handler.send_response(302)
        handler.send_header("Location", entry[2])
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    def _ensure_server(self) -> None:
        if self._server is not None:
            return
        with self._pf_lock:
            self._pf_cancel.clear()
            self._pf_disabled = False
            self._pf_failures = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler)
        server.proxy = self  # type: ignore[attr-defined]
        server.public_base = f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]
        server.is_lan = False  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止代理并清空 token 与惰性系列注册表。

        存在进行中的活动请求（含流式转发的长连接）时不停止、不清 token：
        播放中途暂停/拖动进度时 URL 不能失效，否则 VLC 后续请求 404。

        **注册表必须与 token 一起清**：memo 里存的是 build_url 产出的 /s/<token>，
        token 被清而 memo 留下 → /e/ 一直命中一个死 token（对外 404）且永不
        重新解析。看门狗在 Task 5 租约接线之前就可能中途回收代理，所以这条
        清扫不能推迟到 App 退出。
        """
        with self._lock:
            active = self._active > 0
        if active:
            return
        cache = self._cache_obj()
        if cache is not None and hasattr(cache, "cancel_fetches"):
            cache.cancel_fetches()
        self._shutdown_prefetch()
        self.stop_lan_endpoint()
        srv, self._server = self._server, None
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
        with self._lock:
            self._series.clear()   # memo 一并作废（Task 2 审查）
            self._tokens.clear()
            self._lan_tokens.clear()

    # ------------------------------------------------------------------ #
    def _new_token(self, target: str, headers: dict, ad_block: dict | None = None,
                   force_proxy: bool = False,
                   public_base: str | None = None,
                   source_id=None) -> str:
        token = uuid.uuid4().hex
        token_context = (
            target, _strip_stale_headers(headers), ad_block, force_proxy,
        )
        if trusted_source_id(source_id) == "18j":
            token_context = token_context + (source_id,)
        with self._lock:
            self._tokens[token] = token_context
            if public_base:
                self._lan_tokens.add(token)
        return token

    def _proxy_url(self, target: str, base: str, headers: dict,
                    ad_block: dict | None = None,
                    force_proxy: bool = False,
                    public_base: str | None = None,
                    source_id=None) -> str:

        output_base = self._base_url(public_base)
        full = urljoin(base, target)
        token = self._new_token(full, headers, ad_block, force_proxy,
                                public_base, source_id)
        return f"{output_base}/s/{token}"

    # ------------------------------------------------------------------ #
    def _abort_tee_on_gap(self, target: str, rng: str) -> None:
        """拖动进度越过正在下载的缓存区间 → 放弃本次 tee（不做半截文件）。

        只有「非 0 起始 Range」才可能打乱顺序写；从头/无 Range 是续写或首播，
        不打断。放弃后本次请求仍照常走上游 206，不影响播放。
        """
        cache = self._ensure_cache()
        if cache is None or not cache.enabled:
            return
        start = _parse_range_start(rng)
        if not start:
            return
        key = cache.key_of(target)

        with self._lock:
            tee = self._mp4_tee.get(key)
        if tee is not None and not tee.done and tee.written < start:
            tee.abort()
            self._drop_mp4_tee(key, tee)

    def _drop_mp4_tee(self, key: str, tee: "_FileTee") -> None:
        """把已结束的 tee 从进行中字典移除（幂等：仅移除自身）。"""
        with self._lock:
            if self._mp4_tee.get(key) is tee:
                self._mp4_tee.pop(key, None)

    # ------------------------------------------------------------------ #
    def _forward(self, handler: "_ProxyHandler", target: str, headers: dict,
                  ad_block: dict | None = None,
                  force_proxy: bool = False,
                          public_base: str | None = None,
                          source_id: str = "") -> None:


        """转发一次请求：m3u8 重写内部 URL（含广告过滤），否则流式转发。

        ad_block：源 ad_block 配置。非空时 m3u8 重写前先剔除广告段
        （播放路径广告过滤，与下载路径 filter_m3u8 一致的判定启发式）。
        force_proxy：透传到上游取回（详见 _fetch_upstream）。
        """
        req_headers = dict(headers)
        # Range 是「单次请求」语义，绝不能从存储的 token headers 里带出来：
        # m3u8 请求常带 Range: bytes=0-，若不剥离会被写进分片/KEY token 的
        # headers，VLC 后续拉 KEY/分片（自身不带 Range）时上游收到过时 Range
        # → 回 206 Partial → VLC 的 AES key demux 判连接失败 → 永不进播放。
        # Range 只能来自当前客户端请求（下方再按需加回）。
        req_headers.pop("Range", None)
        # 媒体流一律要 identity（不压缩）：部分站点（如 18mh 的 /media/m3u8
        # 包装接口）无视 Accept-Encoding 强制回 zstd——requests/urllib3 不解压
        # zstd，下方 raw.read 拿到压缩字节被当 m3u8 文本重写 → VLC 播放失败。
        # m3u8 本身是 KB 级小文本、ts/mp4 通常本就不压缩，identity 无带宽代价。
        req_headers["Accept-Encoding"] = "identity"
        # 透传客户端 Range（拖动进度 / 分片定位）。
        # 但 m3u8 播放列表必须整读：VLC 拉 m3u8 时常带 Range（如 bytes=0-1275
        # 探测大小），若透传，CDN 返回截断的 m3u8 → 只拿到部分分片 → 播放
        # 中断。仅对媒体分片/大文件透传 Range，m3u8 URL 一律不传。
        # 注意：m3u8 也可能是带参数包装接口（如 /media/m3u8?url=），URL 不以
        # .m3u8 结尾无法靠后缀判断 → 用「路径含 m3u8 片段」判断（/media/m3u8、
        # /playlist.m3u8 等都在路径里带 m3u8）。确认为 HLS 时不给上游传 Range
        # （播放列表必须整读），且路由走 _forward_m3u8 的重写路径。
        is_hls_url = "m3u8" in target.split("?", 1)[0].lower()
        rng = handler.headers.get("Range")
        if rng and not is_hls_url:
            req_headers["Range"] = rng

        # m3u8 播放列表：重写（并缓存过滤结果/分片）。mp4 等大文件：本地缓存
        # 命中直接 serve，未命中流式转发 + tee 落盘。
        if is_hls_url:
            self._forward_m3u8(handler, target, req_headers, ad_block,
                               force_proxy, public_base, source_id)
            return
        self._forward_media(handler, target, req_headers, ad_block,
                            force_proxy, public_base, source_id)


    # ------------------------------------------------------------------ #
    def _serve_m3u8_text(self, handler: "_ProxyHandler", text: str,
                         target: str, req_headers: dict,
                         ad_block: dict | None = None,
                         force_proxy: bool = False,
                         public_base: str | None = None,
                         manifest_key: str | None = None,
                         skip_ad_filter: bool = False,
                         final_base: str | None = None,
                         source_id=None) -> None:


        """把已取回的 m3u8 文本：广告过滤 → 缓存过滤结果 → 重写内部 URL → 发送。

        由 _forward_m3u8（URL 是 .m3u8）与 _forward_media（短链/参数化 URL
        内容其实是 m3u8）共用。
        target：请求 URL（缓存键/token 语义）。final_base：urljoin 重写基准，
        为上游最终响应 URL（resp.url）。两者可能不同——包装接口 302 到 CDN 时，
        相对分片/KEY 必须拼在最终 host 上，否则会拼成包装接口的假路径。
        """
        cache = self._ensure_cache()
        cache_on = bool(cache is not None and cache.enabled)
        base = final_base or target
        # 播放路径广告过滤：HLS 一律走本地代理/过滤，剔除 m3u8 广告段再
        # 重写（不要求源配 ad_block；ad_block 显式 enabled:false 才关闭，
        # 判定与下载路径 filter_m3u8 一致）
        if not skip_ad_filter:
            text = self._filter_ad_segments(text, base, ad_block, req_headers, source_id)
        if manifest_key is not None:
            self._manifest_cache[manifest_key] = (time.monotonic(), text, base)
        key = None
        if cache_on:
            try:
                key = _manifest_cache_key(cache, target, ad_block,
                                           force_proxy, source_id)
                if not _manifest_contains_query(text, target):
                    cache.playlist_write(key, text.encode("utf-8", "replace"))

                self._register_cache_ctx(key, target, req_headers, ad_block,
                                         force_proxy, final_base=base,
                                         source_id=source_id)

            except Exception:  # noqa: BLE001 —— 缓存写失败不阻断播放
                key = None
        rewritten = self._rewrite_m3u8(text, base, req_headers, ad_block,
                                        cache_on=bool(key), cache_key=key,
                                        force_proxy=force_proxy,
                                        public_base=public_base,
                                        source_id=source_id)

        self._emit_m3u8(handler, rewritten)

    # ------------------------------------------------------------------ #
    def _emit_m3u8(self, handler: "_ProxyHandler", rewritten: str,
                   status: int = 200) -> None:
        """把重写后的 m3u8 文本发给播放器。"""
        body = rewritten.encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/vnd.apple.mpegurl")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()  # 有 Content-Length 定界 → 可 keep-alive 复用
        handler.wfile.write(body)

    # ------------------------------------------------------------------ #
    def _forward_m3u8(self, handler: "_ProxyHandler", target: str,
                       req_headers: dict, ad_block: dict | None = None,
                       force_proxy: bool = False,
                         public_base: str | None = None,
                       source_id: str = "") -> None:


        """转发 m3u8 播放列表：整读 → 重写内部 URL（含广告过滤/缓存）。"""
        # 连接池复用：requests.Session 保持到 CDN 的 keep-alive 连接，

        # HLS 分片逐个转发时不再每次重新握手（见 _get_session 注释）。
        # stream=True：只读头，body 手动流式透传（避免整段载入内存/拖慢首帧）。
        # 默认直连优先，失败后回退系统代理。
        manifest_key = ":".join((
            _context_fingerprint(target, req_headers),
            _manifest_strategy_fingerprint(ad_block, force_proxy, source_id),
        ))
        cached_manifest = self._manifest_cache.get(manifest_key)
        now = time.monotonic()
        if cached_manifest and now - cached_manifest[0] < _MANIFEST_CACHE_TTL:
            cached_base = (cached_manifest[2]
                           if len(cached_manifest) > 2 else None)
            self._serve_m3u8_text(
                handler, cached_manifest[1], target, req_headers,
                ad_block, force_proxy, public_base,
                manifest_key=manifest_key, skip_ad_filter=True,
                final_base=cached_base,
                source_id=source_id,
            )
            return
        resp = _fetch_upstream(target, req_headers, force_proxy=force_proxy)
        try:
            # 上游错误（403/404/5xx）不发 body 给播放器：原 urllib 会抛
            # HTTPError，这里等价处理（播放器收到 502 会提示换线路/重试，
            # 而不是把错误页当媒体流播放黑屏）。
            if resp.status_code >= 400:
                _send_upstream_error(handler, resp)
                return
            # m3u8 播放列表必须整读且自动解压：stream=True 时 resp.raw 返回
            # gzip 原始字节（Content-Encoding: gzip 不解压），直接重写会乱码/
            # 截断（542B gzip vs 8871B 明文）。m3u8 是小文本，用 resp.content
            # 完整读取 + 自动解压；媒体流（mp4/ts 大文件）才用 resp.raw 流式。
            body = _read_upstream_body(resp)
            _update_upstream_diagnostic(resp, 0, finished=True)
            if not body.lstrip().startswith(b"#EXTM3U") and not (
                (resp.headers.get("Content-Type") or "")).find("mpegurl") >= 0:
                # URL 是 m3u8 但内容不是（可能重定向/错误页）→ 透传原始内容
                self._send_body(handler, resp, body)
                return
            text = body.decode("utf-8", "replace")
            # 重写基准用最终响应 URL（302 后的 CDN host）：包装接口
            # /media/m3u8?url=… 的相对分片/KEY 必须拼在最终 host 上。
            final_base = getattr(resp, "url", None) or target
            self._serve_m3u8_text(
                handler, text, target, req_headers,
                ad_block, force_proxy, public_base,
                manifest_key=manifest_key, final_base=final_base,
                source_id=source_id,
            )

        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    def _forward_media(self, handler: "_ProxyHandler", target: str,
                       req_headers: dict, ad_block: dict | None = None,
                       force_proxy: bool = False,
                         public_base: str | None = None,
                       source_id: str = "") -> None:


        """转发普通媒体（mp4/mp3/ts/audio…）：优先本地缓存命中，否则流式转发。

        - mp4 已完整缓存 → 直接 serve 本地文件（支持 Range/206/416）
        - 否则直连/代理上游流式转发，同时 tee 落盘（写满置 complete；
          播放被中断 / 拖动越界 → 弃用 .part，不做半截文件）
        - 内容实际是 HLS（短链/参数化 URL 未含 .m3u8）→ 转 m3u8 重写路径
        """
        cache = self._ensure_cache()
        cache_on = bool(cache is not None and cache.enabled)
        key = None
        fetch_state = None
        fetch_owner = False

        # 本地缓存命中：直接 serve。命中条件 = 索引标记 complete + 文件存在。
        if cache_on:
            try:
                key = cache.key_of(target)
                entry = cache.entry(key)

                if entry and entry.get("complete") and entry.get("kind") == "mp4":
                    p = cache.mp4_final(key)
                    if p.is_file():
                        cache.touch(key)
                        _record_cache_diagnostic(target, req_headers, "hit")
                        self._serve_local_file(handler, p)
                        return
                if self._wait_for_cached_fetch(handler, cache, key, target, req_headers):
                    return
                if not handler.headers.get("Range"):
                    fetch_owner, fetch_state = cache.start_fetch(key)
                    if not fetch_owner:
                        if self._wait_for_cached_fetch(handler, cache, key, target,
                                                       req_headers):
                            return
                        fetch_owner, fetch_state = cache.start_fetch(key)
            except Exception:  # noqa: BLE001 —— 缓存不可用退化为直传
                cache_on = False
                fetch_owner = False
                fetch_state = None

        # 连接池复用：requests.Session 保持到 CDN 的 keep-alive 连接，
        # HLS 分片逐个转发时不再每次重新握手（见 _get_session 注释）。
        # stream=True：只读头，body 手动流式透传（避免整段载入内存/拖慢首帧）。
        # 默认直连优先，失败后回退系统代理。
        fetch_status = None
        fetch_error = None
        try:
            resp = _fetch_upstream(target, req_headers, force_proxy=force_proxy)
            if fetch_owner:
                cache.set_fetch_response(key, fetch_state, resp)
        except Exception as exc:
            fetch_error = exc
            if fetch_owner:
                cache.finish_fetch(key, fetch_state, 502, exc)
                fetch_owner = False
            raise
        try:
            if resp.status_code >= 400:
                fetch_status = resp.status_code
                fetch_error = RuntimeError("upstream error")
                _send_upstream_error(handler, resp)
                return
            # 判断内容是不是 HLS 播放列表（URL 未含 .m3u8 但内容是的，如
            # 短链/参数化 m3u8）。只嗅探 _SNIFF_BYTES 字节（够覆盖 #EXTM3U）：
            # 预读多了会把每个分片的首字节延后一整个读取量的到达时间。
            # 压缩流必须整读解压（无法边读边判），此分支保留整读。
            # 普通媒体：透传响应头 + 流式转发（已拦截 >=400，这里透传上游状态码）
            resp, first, whole = _read_media_prefix(
                resp, target, req_headers, force_proxy, cache, key, fetch_state)
            if resp.status_code >= 400:
                fetch_status = resp.status_code
                fetch_error = RuntimeError("upstream error")
                _send_upstream_error(handler, resp)
                return
            is_m3u8 = first.startswith(b"#EXTM3U") or (
                resp.headers.get("Content-Type") or "").find("mpegurl") >= 0

            if is_m3u8:
                # 读完整文本，重写内部 URL（分片/KEY/变体）为本地代理。
                # gzip 压缩时 first 已是完整解压内容（上方分支），rest 为空。
                rest = resp.raw.read()
                _update_upstream_diagnostic(resp, len(rest), finished=True)
                text = (first + rest).decode("utf-8", "replace")
                # 短链/参数化 URL 内容实为 m3u8：同样以最终响应 URL 为重写基准。
                final_base = getattr(resp, "url", None) or target
                self._serve_m3u8_text(handler, text, target, req_headers,
                                        ad_block, force_proxy, public_base,
                                        final_base=final_base, source_id=source_id)


                return

            # 普通媒体：透传响应头 + 流式转发（已拦截 >=400，这里透传上游状态码）
            chunked, declared = _send_stream_headers(
                handler, resp, override_len=len(first) if whole else None)

            # 磁盘缓存 tee：首播流（从头开始）才起写；已知总长且从头接收，
            # 写满 commit。拖动进度的非 0 起始请求走 _abort_tee_on_gap 放弃。
            tee = None
            if cache_on:
                try:
                    entry = cache.entry(key)
                    with self._lock:
                        already = self._mp4_tee.get(key)
                    rng_start = _parse_range_start(req_headers.get("Range"))
                    from_head = rng_start is None or rng_start == 0
                    if (not (entry and entry.get("complete"))
                            and already is None and from_head):
                        total = len(first) if whole else _content_total(resp)
                        if total and _full_zero_offset_response(
                                resp, req_headers.get("Range")):
                            st = _FileTee(cache, key, cache.mp4_part(key),
                                          cache.mp4_final(key), total,
                                          is_mp4=True)
                            if st.start():
                                with self._lock:
                                    self._mp4_tee[key] = st
                                tee = st

                except Exception:  # noqa: BLE001 —— 落盘失败不阻断播放
                    tee = None
            sent = 0
            try:
                if first:
                    handler.wfile.write(_frame(first, chunked))
                    sent += len(first)
                    if tee:
                        tee.write(first)
                while True:
                    if fetch_state is not None and cache.fetch_cancelled(fetch_state):
                        raise RuntimeError("media fetch cancelled")
                    chunk = _read_available(resp.raw, _READ_CHUNK)
                    if not chunk:
                        break
                    handler.wfile.write(_frame(chunk, chunked))
                    sent += len(chunk)
                    _update_upstream_diagnostic(resp, len(chunk))
                    self._touch()  # 流式期间持续刷新看门狗（暂停/拖动不误杀）
                    if tee:
                        tee.write(chunk)
                if declared is not None and sent != declared:
                    raise requests.ConnectionError("incomplete media body")
                if chunked:
                    handler.wfile.write(b"0\r\n\r\n")
                _update_upstream_diagnostic(resp, 0, finished=True)
                if tee:
                    tee.finish()
                # 结束即从进行中字典移除：finish/abort 后残留对象会令
                # 后续请求「already is None」为假 → 断流的 mp4 再也不落盘。
                if tee:
                    self._drop_mp4_tee(key, tee)
            except (BrokenPipeError, ConnectionResetError):
                fetch_error = RuntimeError("client disconnected")
                _update_upstream_diagnostic(
                    resp, 0, finished=True, failure_category="client_disconnect"
                )
                handler.close_connection = True
                if tee:
                    tee.abort()
                    self._drop_mp4_tee(key, tee)
            except Exception as exc:
                fetch_error = exc
                handler.close_connection = True
                if tee:
                    tee.abort()
                    self._drop_mp4_tee(key, tee)
                raise
        except Exception as exc:
            fetch_error = exc
            raise
        finally:
            if fetch_owner:
                cache.finish_fetch(key, fetch_state, fetch_status, fetch_error)
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    def _send_body(self, handler: "_ProxyHandler", resp, body: bytes) -> None:
        """透传上游响应状态码 + Content-Type，但用给定 body 发送。

        用于「URL 是 .m3u8 但内容不是 m3u8」（如上游动态校验/错误页）：
        原样回给播放器，不做 m3u8 重写。
        """
        handler.send_response(resp.status_code)
        ct = resp.headers.get("Content-Type")
        if ct:
            handler.send_header("Content-Type", ct)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()  # 有 Content-Length 定界 → 可 keep-alive 复用
        handler.wfile.write(body)

    def _send_bytes(self, handler: "_ProxyHandler", body: bytes,
                    status: int = 200, content_type: str = "video/mp2t") -> None:
        """发送一段已成型的字节（remux 输出）：定长 200，可 keep-alive 复用。"""
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    # ------------------------------------------------------------------ #
    # 分片时间戳重写（ad_block.hls_rules.remux_timestamps）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _store_segment_bytes(cache, cache_key: str, seg_name: str,
                             body: bytes) -> None:
        """把整流的分片写进分片缓存（原始字节；整理输出随后原地替换）。"""
        try:
            final = cache.hls_segment_final(cache_key, seg_name)
            part = cache.hls_segment_part(cache_key, seg_name)
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(body)
            if final.exists():
                final.unlink()
            os.replace(part, final)
        except OSError:
            pass

    def _serve_remux_from_file(self, handler: "_ProxyHandler", cache_key: str,
                               seg_name: str, target: str, path) -> bool:
        """缓存命中的分片：需要时按片序整理后发送；不适用返回 False。

        ctx["segs"][name] > 0 表示该片已整理且输出已直写缓存文件（下次直通）。
        首片在 ctx 里登记的是 0，它从未被整理，必须继续走读取/直通逻辑。
        """
        ctx = _remux_ctx(cache_key)
        with ctx["lock"]:
            anchor = ctx["segs"].get(seg_name)
        if anchor is not None and anchor > 0:
            return False          # 已整理且有输出（输出已直写缓存文件）
        data = self._read_whole_ts(path)
        if data is None or not _is_ts_payload(data):
            # 非明文 TS（典型：AES-128 加密分片，首字节随机而非 0x47）不整理：
            # 本路径此前漏了这个判断，命中加密片仍会起 ffmpeg 去整理密文——
            # 必然失败，每片白烧一次进程（实测 ~0.13s，且逐片串行）。未命中
            # 路径早就有同样的 _is_ts_payload 守卫，这里补齐保持一致。
            return False
        out = self._remux_segment(cache_key, seg_name, data, None)
        if out is None:
            return False
        self._send_bytes(handler, out)
        try:
            if not path.name.endswith(".remux.ts"):
                tmp = path.with_name(path.name + ".remux.ts")
                tmp.write_bytes(out)
                os.replace(tmp, path)
        except OSError:
            pass
        return True

    @staticmethod
    def _read_whole_ts(path, budget: int = 32 * 1024 * 1024):
        """读取整个 TS 分片；文件过大/读失败返回 None（不整理，直接直通）。"""
        try:
            size = path.stat().st_size
            if size <= 0 or size > budget:
                return None
            return path.read_bytes()
        except OSError:
            return None

    def _remux_segment(self, cache_key: str, seg_name: str, raw: bytes,
                       req_headers) -> bytes | None:
        """把本片整理成"接上前一片"的 TS；失败/不适用返回 None。

        第 0 片是后续所有片的时间基准，原样返回（关键帧、锚点都在里面）。
        之后每片：ffmpeg 只做 TS 重封装（-c copy，不转码）把 TS 时间戳重新
        编号，再整体平移到前一片结束点，播放器时钟连续，拼接处不再丢帧。
        分片必须按播放顺序到达；乱序/拖动引起的片段不整理（直接直通）。
        """
        # 同一个 key 上的整理串行执行：每个请求在自己的 HTTP 线程里跑，两个分片
        # 同时到达时不能在"读基准/写基准"之间穿插（会拿到过期 base）。
        sctx = _remux_serial(cache_key)
        with sctx:
            return self._remux_segment_locked(cache_key, seg_name, raw, req_headers)

    def _remux_segment_locked(self, cache_key: str, seg_name: str, raw: bytes,
                              req_headers) -> bytes | None:
        """_remux_segment 的实现体（调用方已持 _remux_serial 锁）。"""
        ctx = _remux_ctx(cache_key)
        with ctx["lock"]:
            if not ctx["segs"]:
                ctx["segs"][seg_name] = 0.0
                ctx["inflight"][seg_name] = False
                ctx["done"] = 1
                return None
            if seg_name in ctx["segs"]:
                return None
            base = max(ctx["segs"].values())
            ctx["inflight"][seg_name] = True
        ffmpeg = _locate_ffmpeg()
        result = None
        if ffmpeg:
            result = self._run_remux(ffmpeg, raw, base, req_headers)
        with ctx["lock"]:
            ctx["inflight"].pop(seg_name, None)
            if result is not None:
                ctx["segs"][seg_name] = base + result[1]
                ctx["done"] += 1
        return None if result is None else result[0]

    def _run_remux(self, ffmpeg: str, raw: bytes, base: float,
                   req_headers=None) -> tuple[bytes, float] | None:
        """ffmpeg 重封装 + 时间戳平移；返回 (输出字节, 实际时长) 或 None。

        输入是本地临时文件，不访问网络 → 不能带 -headers 之类的 http 协议选项
        （ffmpeg 会直接报 "Option headers not found" 并拒绝运行）。
        """
        tmpdir = tempfile.TemporaryDirectory(prefix="claw-remux-")
        try:
            root = Path(tmpdir.name)
            src = root / "in.ts"
            src.write_bytes(raw)
            work = root / "out.ts"
            cmd = [ffmpeg, "-v", "error", "-nostdin",
                   "-copyts", "-i", str(src), "-map", "0", "-c", "copy",
                   "-muxdelay", "0.7", "-f", "mpegts", "-y", str(work)]
            proc = subprocess.run(cmd, capture_output=True, timeout=_REMUX_TIMEOUT)
            if proc.returncode != 0 or not work.is_file():
                return None
            # 时长由 _ts_shift 的逐包扫描一并算出（max-min），不再起 ffprobe
            # 子进程：省掉每片一次进程创建/冷启动（实测冷启动 ~2s 直接卡播放）。
            # ffmpeg 的 mpegts 复用器会把时间戳重排（-copyts 对 TS→TS 无效），
            # 锚点必须按**重封装后的输出**重算并直接落到 base，不能先归零再
            # 平移——第二步会在已归零的时间轴上再次取模，负数被绕回成 33 位
            # 时间戳高位，整片被推飞。
            duration = _ts_shift(work, base)
            if duration is None or duration <= 0:
                return None
            return work.read_bytes(), duration
        except (OSError, subprocess.SubprocessError):
            return None
        finally:
            tmpdir.cleanup()


    # ------------------------------------------------------------------ #
    def _serve_cache(self, handler: "_ProxyHandler", path: str,
                     public_base: str | None = None) -> None:
        """/c/<key>/<segment> 路由：HLS 分片磁盘缓存。

        命中（本地 final 文件已完整存在）→ 直接 serve 本地文件（支持 Range）；
        未命中 → 回落上游代理转发，同时 tee 落盘（写满 commit，下次命中）。
         若内容是 m3u8（变体/嵌套播放列表）→ 转 m3u8 重写路径。
         客户端 Range miss 不加入共享 fetch state，保持每个 206 响应独立。
         """

        cache = self._ensure_cache()
        if cache is None or not cache.enabled:
            handler.send_error(403, "cache disabled")
            return
        parts = path[len("/c/"):].split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            handler.send_error(400, "bad cache path")
            return

        cache_key = parts[0]
        seg_name = unquote(parts[1])
        with self._lock:
            ctx = self._cache_ctx.get(cache_key)
        if ctx is None:
            handler.send_error(502, "cache context lost")
            return
        base_url, base_headers, ad_block = ctx[0], ctx[1], ctx[2]
        source_id = _ctx_get(ctx, 5, "")
        force_proxy = _tuple_force_proxy(ctx)

        final_base = ctx[4] if len(ctx) > 4 and ctx[4] else None
        target = urljoin(base_url, seg_name)
        # 分片级预取钩子：取到第 seg_name 片后把后续 depth 片排队落盘。
        # 放锁外：预取自身会取同名 path_lock，锁内触发会自死锁。
        if not seg_name.endswith(".m3u8"):
            self._cancel_prefetch_url(target)
            self._maybe_prefetch(cache_key, target)

        fetch_key = cache.key_of(target)
        fetch_owner = False
        fetch_state = None
        shared_fetch = not handler.headers.get("Range")
        # 分片时间戳重写只作用于"播放"（播放器顺序拉片、无 Range）；下载器用
        # Range 顺序读段，整理会打乱字节偏移，必须保持原始分片。
        remux = _remux_active(ad_block) and not handler.headers.get("Range")
        if not seg_name.endswith(".m3u8"):
            final = cache.hls_segment_final(cache_key, seg_name)
            entry = cache.entry(cache_key)
            if entry and entry.get("complete") and final.is_file():
                cache.touch(cache_key)
                _record_cache_diagnostic(target, base_headers, "hit")
                if remux and self._serve_remux_from_file(
                        handler, cache_key, seg_name, target, final):
                    return
                self._serve_local_file(handler, final)
                return
        if shared_fetch:
            fetch_owner, fetch_state = cache.start_fetch(fetch_key)
        if shared_fetch and not fetch_owner:
            if not cache.wait_fetch(fetch_state, _CACHE_FETCH_WAIT):
                handler.send_error(504, "cache fetch timeout")
                return
            if fetch_state.error is not None:
                _send_cached_fetch_error(handler, fetch_state)
                return

        # 定位到本段在缓存里的路径；已知总长才 tee（见 _content_total）。
        lock = cache.path_lock(target) if shared_fetch else nullcontext()
        with lock:
            if seg_name.endswith(".m3u8") or target.lower().split("?", 1)[0].endswith(".m3u8"):
                # 嵌套/变体播放列表：按自身 URL 的独立 key 缓存放过滤文本；
                # 命中且已缓存 → 复用过滤结果，只重写（免上游往返/免重复过滤）
                sub_key = _manifest_cache_key(cache, target, ad_block,
                                               force_proxy, source_id)
                p = cache.playlist_file(sub_key)
                if p.is_file():
                    cache.touch(sub_key)
                    cached = p.read_text(encoding="utf-8", errors="replace")
                    with self._lock:
                        child_ctx = self._cache_ctx.get(sub_key)
                    child_base = (
                        child_ctx[4]
                        if child_ctx is not None and len(child_ctx) > 4 and child_ctx[4]
                        else target
                    )
                    child_headers = child_ctx[1] if child_ctx is not None else base_headers
                    child_ad_block = child_ctx[2] if child_ctx is not None else ad_block
                    child_force_proxy = (
                        _tuple_force_proxy(child_ctx)
                        if child_ctx is not None else force_proxy
                    )
                    child_source_id = (
                        _ctx_get(child_ctx, 5, source_id)
                        if child_ctx is not None else source_id
                    )
                    rewritten = self._rewrite_m3u8(
                        cached, child_base, child_headers, child_ad_block,
                        cache_on=True, cache_key=sub_key,
                        force_proxy=child_force_proxy, public_base=public_base,
                        source_id=child_source_id)

                    self._emit_m3u8(handler, rewritten)



                    if fetch_owner:
                        cache.finish_fetch(fetch_key, fetch_state, None)
                    return
                # 未缓存 → 走下路（fetch + 嗅探 + _serve_m3u8_text 会登记 sub_key）
            else:
                final = cache.hls_segment_final(cache_key, seg_name)
                entry = cache.entry(cache_key)
                if entry and entry.get("complete") and final.is_file():
                    cache.touch(cache_key)
                    _record_cache_diagnostic(target, base_headers, "hit")
                    if remux and self._serve_remux_from_file(
                            handler, cache_key, seg_name, target, final):
                        if fetch_owner:
                            cache.finish_fetch(fetch_key, fetch_state, None)
                        return
                    self._serve_local_file(handler, final)
                    if fetch_owner:
                        cache.finish_fetch(fetch_key, fetch_state, None)
                    return

            req_headers = dict(base_headers)
            req_headers["Accept-Encoding"] = "identity"
            rng = handler.headers.get("Range")
            if rng and not seg_name.endswith(".m3u8"):
                req_headers["Range"] = rng
            # 跨重启续传：同一分片存在未完成 `.part` 且客户端未带 Range →
            # 从已下载偏移向上游续取剩余字节，不重复下载已完成部分。
            resume_offset = 0
            part_path = None
            if not seg_name.endswith(".m3u8") and not rng:
                part_path = cache.hls_segment_part(cache_key, seg_name)
                try:
                    if part_path.is_file():
                        resume_offset = part_path.stat().st_size
                except OSError:
                    resume_offset = 0
                if resume_offset > 0:
                    req_headers["Range"] = f"bytes={resume_offset}-"
            fetch_status = None
            fetch_error = None
            try:
                resp = _fetch_upstream(target, req_headers, force_proxy=force_proxy,
                                       segment=not seg_name.endswith(".m3u8"))
                if resume_offset and resp.status_code == 416:
                    # 偏移越界（.part 已完整但尚未提交）→ 弃 part，从头重取。
                    try:
                        resp.close()
                    except Exception:  # noqa: BLE001
                        pass
                    cache.discard_part(part_path)
                    resume_offset = 0
                    req_headers.pop("Range", None)
                    resp = _fetch_upstream(target, req_headers, force_proxy=force_proxy,
                                           segment=True)
            except Exception as exc:
                if fetch_owner:
                    cache.finish_fetch(fetch_key, fetch_state, 502, exc)
                fetch_owner = False
                raise
            try:
                if fetch_owner:
                    cache.set_fetch_response(fetch_key, fetch_state, resp)
                if resp.status_code >= 400:
                    fetch_status = resp.status_code
                    fetch_error = RuntimeError("upstream error")
                    _send_upstream_error(handler, resp)
                    return

                # 可能是「URL 不带 .m3u8 的嵌套播放列表」→ 先嗅探首块
                resp, first, whole = _read_media_prefix(
                    resp, target, req_headers, force_proxy, cache, fetch_key, fetch_state,
                    segment=not seg_name.endswith(".m3u8"))
                if resp.status_code >= 400:
                    fetch_status = resp.status_code
                    fetch_error = RuntimeError("upstream error")
                    _send_upstream_error(handler, resp)
                    return
                is_m3u8 = first.startswith(b"#EXTM3U") or (
                    resp.headers.get("Content-Type") or "").find("mpegurl") >= 0
                if is_m3u8:
                    rest = resp.raw.read()
                    _update_upstream_diagnostic(resp, len(rest or b""), finished=True)
                    text = (first + (rest or b"")).decode("utf-8", "replace")
                    # 嵌套/变体播放列表同样以最终响应 URL 为重写基准：
                    # /c/ name 已基于上一层 final base 解析，若本层再 302 需再对齐。
                    final_base = getattr(resp, "url", None) or target
                    self._serve_m3u8_text(handler, text, target, req_headers,
                                            ad_block, force_proxy, public_base,
                                            final_base=final_base, source_id=source_id)


                    return
                if seg_name.endswith(".m3u8"):
                    # 上游非 m3u8（动态校验失败/错误页）→ 原样回给播放器
                    self._send_body(handler, resp, first + (resp.raw.read() or b""))
                    return
                total = len(first) if whole else _content_total(resp)
                resume = bool(
                    resume_offset and not handler.headers.get("Range")
                    and total and total > resume_offset
                    and _is_contiguous_206(resp, resume_offset))
                head_bytes = b""
                if resume:
                    # 先把磁盘已完成头部读入内存（在 tee 以追加模式打开同一文件
                    # 之前关闭读句柄，避免 Windows 上的双句柄共享问题）。
                    try:
                        head_bytes = cache.hls_segment_part(cache_key, seg_name).read_bytes()
                    except OSError:
                        head_bytes = b""
                    if len(head_bytes) != resume_offset:
                        resume = False
                        resume_offset = 0
                        head_bytes = b""
                if not resume and _remux_active(ad_block) and _is_ts_payload(first):
                    # 时间戳重写源：本片整流后按序整理（原地替换缓存文件）再
                    # 交给播放器；任何一步失败都退回原始字节，绝不阻断播放。
                    body = first + (resp.raw.read() or b"") if not whole else first
                    _update_upstream_diagnostic(resp, 0, finished=True)
                    if total and _full_zero_offset_response(
                            resp, handler.headers.get("Range")):
                        self._store_segment_bytes(
                            cache, cache_key, seg_name, body)
                    out = self._remux_segment(
                        cache_key, seg_name, body, req_headers)
                    self._send_bytes(handler, out if out is not None else body)
                    if fetch_owner:
                        fetch_status = resp.status_code
                    return
                if resume:
                    # 续传：对客户端仍是完整 200——回放磁盘头部 + 续流剩余字节。
                    handler.send_response(200)
                    for hk in _MEDIA_HDRS:
                        if hk in ("Content-Length", "Content-Range"):
                            continue
                        hv = resp.headers.get(hk)
                        if hv:
                            handler.send_header(hk, hv)
                    handler.send_header("Content-Length", str(total))
                    handler.end_headers()
                    chunked, declared = False, total
                else:
                    chunked, declared = _send_stream_headers(
                        handler, resp, override_len=len(first) if whole else None)
                tee = None
                # /c/ 分片：新抓（完整 200）或续传（206 偏移）都落盘；拖动产生的
                # 非 0 Range 不落盘（不满足顺序写前提），与 mp4 tee 规则一致。
                if total and (resume or _full_zero_offset_response(
                        resp, handler.headers.get("Range"))):
                    final = cache.hls_segment_final(cache_key, seg_name)
                    st = _FileTee(cache, cache_key, cache.hls_segment_part(cache_key, seg_name),
                                  final, total, is_mp4=False,
                                  resume=resume_offset if resume else 0)
                    if st.start():
                        tee = st
                sent = 0
                try:
                    if resume and head_bytes:
                        handler.wfile.write(_frame(head_bytes, chunked))
                        sent += len(head_bytes)
                    if first:
                        handler.wfile.write(_frame(first, chunked))
                        sent += len(first)
                        if tee:
                            tee.write(first)
                    while True:
                        if fetch_state is not None and cache.fetch_cancelled(fetch_state):
                            raise RuntimeError("media fetch cancelled")
                        chunk = resp.raw.read(_READ_CHUNK)
                        if not chunk:
                            break
                        handler.wfile.write(_frame(chunk, chunked))
                        sent += len(chunk)
                        _update_upstream_diagnostic(resp, len(chunk))
                        self._touch()
                        if tee:
                            tee.write(chunk)

                    if declared is not None and sent != declared:
                        raise requests.ConnectionError("incomplete media body")
                    if chunked:
                        handler.wfile.write(b"0\r\n\r\n")
                    _update_upstream_diagnostic(resp, 0, finished=True)
                    if tee:
                        tee.finish()
                except (BrokenPipeError, ConnectionResetError):
                    fetch_error = RuntimeError("client disconnected")
                    _update_upstream_diagnostic(
                        resp, 0, finished=True, failure_category="client_disconnect"
                    )
                    handler.close_connection = True
                    if tee:
                        tee.abort()
                except Exception as exc:
                    fetch_error = exc
                    handler.close_connection = True
                    if tee:
                        tee.abort()
                    raise
            except Exception as exc:
                fetch_error = exc
                raise
            finally:
                if fetch_owner:
                    cache.finish_fetch(
                        fetch_key, fetch_state, fetch_status, fetch_error,
                        headers=(dict(resp.headers) if fetch_status == 416 else None),
                    )
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------ #
    def _serve_local_file(self, handler: "_ProxyHandler", path) -> None:
        """serve 本地缓存文件（支持 Range/206/416），Content-Type 按扩展名猜。"""
        try:
            size = path.stat().st_size
            rng = handler.headers.get("Range")
            parsed = _parse_range(rng, size)
            with open(path, "rb") as f:
                if rng and parsed is None:
                    handler.send_response(416)
                    handler.send_header("Content-Range", f"bytes */{size}")
                    handler.send_header("Content-Length", "0")
                    handler.end_headers()
                    return
                if parsed is None:
                    start, end = 0, size - 1
                    handler.send_response(200)
                else:
                    start, end = parsed
                    handler.send_response(206)
                    handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                length = end - start + 1
                ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                handler.send_header("Content-Type", ctype)
                handler.send_header("Content-Length", str(length))
                handler.send_header("Accept-Ranges", "bytes")
                handler.end_headers()  # Content-Length 定界 → 可 keep-alive 复用
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(_READ_CHUNK, remaining))
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
                    remaining -= len(chunk)
        except OSError:
            try:
                handler.send_error(404, "cache file missing")
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    def _filter_ad_segments(self, m3u8_text: str, base_url: str,
                             ad_block: dict | None = None,
                             headers: dict | None = None,
                             source_id=None) -> str:

        try:
            from .adblock import AdblockEngine

            raw = {} if ad_block is None else {"ad_block": ad_block}
            source = type("Source", (), {"raw": raw})()
            engine = AdblockEngine()
            engine.configure(source, default_on=True)
            return engine.filter_m3u8(
                m3u8_text, base_url, source_id=source_id
            )

        except Exception:
            return m3u8_text


    # ------------------------------------------------------------------ #
    def _rewrite_m3u8(self, text: str, base: str, headers: dict,
                      ad_block: dict | None = None,
                      cache_on: bool = False,
                      cache_key: str | None = None,
                      force_proxy: bool = False,
                      public_base: str | None = None,
                      source_id=None) -> str:

        """把 m3u8 内部 URL 重写为本机代理 URL（返回完整新文本）。

        - cache_on=False（无磁盘缓存路径）→ 全部走 /s/<token>
        - cache_on=True（HLS 磁盘缓存）→
             分片/变体/初始化片段（MAP）：/c/<key>/<name>，未命中回落上游 + tee；
             KEY/SESSION-KEY/PRELOAD-HINT：带签名时效参数，每请求都要现签 →
             走 /s/<token>（绝不做 /c/ 磁盘缓存）

        只重写普通行（分片 URL）与带 URI= 的标签行；其余 # 行原样保留。
        同一播放列表所有资源都挂在同一个 cache_key 下（同目录），保证
        mark_hls_segment 总在已索引的影片条目上累加字节数。
        """
        out = []
        segs: list[str] = []  # 有序分片（供预取前瞻定位消费位置）
        for line in text.splitlines():
            if not line:
                continue
            if line.startswith("#"):
                m = re.search(r'URI="([^"]+)"', line)
                if m is None:
                    out.append(line)
                    continue
                tag = line.split(":", 1)[0].strip()
                if tag in ("#EXT-X-KEY", "#EXT-X-SESSION-KEY"):
                    new_uri = self._proxy_url(m.group(1), base, headers,
                                               ad_block, force_proxy,
                                               public_base, source_id)

                    out.append(line.replace(f'URI="{m.group(1)}"', f'URI="{new_uri}"'))
                    continue
                uri = m.group(1)
                rewritten_uri = self._rewrite_cache_or_token(
                    uri, base, headers, ad_block, cache_on, cache_key,
                    force_proxy, public_base, source_id,
                )

                out.append(line.replace(f'URI="{uri}"', f'URI="{rewritten_uri}"'))
                self._note_segment(segs, uri, base, cache_on, cache_key)
                continue

            uri = line
            out.append(self._rewrite_cache_or_token(
                uri, base, headers, ad_block, cache_on, cache_key,
                force_proxy, public_base, source_id))

            self._note_segment(segs, uri, base, cache_on, cache_key)
        if cache_on and cache_key is not None:
            self._set_seg_order(cache_key, segs)
        return "\n".join(out)

    @staticmethod
    def _note_segment(segs: list[str], uri: str, base: str,
                      cache_on: bool, cache_key: str | None) -> None:
        """收集可预取的分片（排除嵌套清单本身，它们走各自的重写/缓存路径）。"""
        if not cache_on or cache_key is None:
            return
        full = urljoin(base, uri)
        if full.split("?", 1)[0].lower().endswith(".m3u8"):
            return
        segs.append(full)

    # ------------------------------------------------------------------ #
    def _rewrite_cache_or_token(self, uri: str, base: str, headers: dict,
                                ad_block: dict | None, cache_on: bool,
                                cache_key: str | None,
                                force_proxy: bool = False,
                                public_base: str | None = None,
                                source_id=None) -> str:

        """单个资源重写：cache_on 且已知缓存 key → /c/<key>/<full>，否则 /s/。

/c/ 资源名 = quote 解析后的完整上游 URL：_serve_cache 反解时无需再次

urljoin 猜测上下文（分片可能是绝对路径 /hls/x.ts 或相对 hls/x.ts），
unquote 即得上游原 URL，直接取回/落盘（文件存名由 _safe_seg_name 压平）。
"""
        full = urljoin(base, uri)
        cache = self._ensure_cache() if cache_on else None
        if cache_on and cache is not None and cache_key is not None:
            # Keep `?query` outside the escaped path. FFmpeg/VLC reject
            # `segment.ts%3Ftoken=...` as a segment URL because it no longer
            # has the `.ts` extension. The cache route preserves the query
            # from the HTTP request and reconstructs the same upstream URL.
            name = quote(full, safe="/:@?&=%")
            if full.lower().split("?", 1)[0].endswith(".m3u8"):
                child_key = _manifest_cache_key(
                    cache, full, ad_block, force_proxy, source_id
                )
                self._register_cache_ctx_if_missing(
                    child_key, full, headers, ad_block, force_proxy, source_id
                )
            return f"{self._base_url(public_base)}/c/{cache_key}/{name}"


        return self._proxy_url(full, base, headers, ad_block, force_proxy,
                                public_base, source_id)



def proxy_url_for(url: str, headers: dict | None = None,
                    ad_block: dict | None = None,
                    force_proxy: bool = False,
                    source_id=None) -> str:






    """external_player 调用的入口：把目标 URL 打包成本地代理 URL。



    headers 为空时直接返回原 URL（无防盗链头则无需代理，避免无谓起进程）。
    force_proxy：True 时该 URL 的所有上游转发一律走系统代理会话（跳过
    直连探测）；默认策略为直连优先、失败回退代理。
    """
    is_hls = "m3u8" in url.lower()
    if not headers and not is_hls:
        return url
    proxy = MediaProxy.instance()
    args = (url, headers or {}, ad_block, force_proxy)
    try:
        supports_source_id = "source_id" in inspect.signature(proxy.build_url).parameters
    except (TypeError, ValueError):
        supports_source_id = False
    if supports_source_id:
        return proxy.build_url(*args, source_id=source_id)
    return proxy.build_url(*args)
