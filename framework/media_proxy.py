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
import mimetypes
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, unquote, quote, urlparse

import requests
from requests.adapters import HTTPAdapter

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


def _classify_upstream_request_kind(target: str, headers: dict) -> str:
    path = urlparse(target).path.lower()
    if headers.get("Range"):
        return "range"
    if path.endswith((".m3u8", ".m3u")):
        return "manifest"
    if path.endswith((".key", ".key.bin", ".bin")) or "/key" in path:
        return "key"
    if path.endswith((".mp4", ".m4v", ".mov", ".webm")):
        return "mp4"
    if path.endswith((".ts", ".m4s", ".aac", ".mp3", ".webvtt", ".vtt")):
        return "segment"
    return "segment"


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
                               failure_category: str | None = None) -> None:
    elapsed_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
    status_code = getattr(response, "status_code", None)
    if status_code is not None and status_code >= 400 and failure_category is None:
        failure_category = f"{route}_http"
    record = {
        "host": urlparse(target).hostname,
        "request_kind": _classify_upstream_request_kind(target, headers),
        "route": route,
        "status_code": status_code,
        "elapsed_ms": elapsed_ms,
        "first_byte_ms": elapsed_ms if response is not None else None,
        "throughput_bps": None,
        "failure_category": failure_category,
    }
    with _UPSTREAM_DIAGNOSTICS_LOCK:
        _UPSTREAM_DIAGNOSTICS.append(record)
        del _UPSTREAM_DIAGNOSTICS[:-_UPSTREAM_DIAGNOSTICS_MAX]


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
    """兼容解包：老三元组 / 新四元组 entry 里取 force_proxy（第 4 位）。"""
    return len(entry) == 4 and bool(entry[3])


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


def _proxy_get(target: str, headers: dict):
    """经系统代理会话取流。重试一次短退避：代理偶发连接重置/切节点时，
    否则上层把异常当 502 抛给播放器 → VLC demux 失败 → 播放中断。
    两次都失败则抛出最后一次异常。"""
    last_exc = None
    for _attempt in range(2):
        try:
            return _get_session().get(
                target, headers=headers, timeout=30, stream=True
            )
        except requests.RequestException as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(0.3)
    raise last_exc  # noqa: BLE001 —— 两次都失败，让上层 502/重试


def _fetch_upstream(target: str, headers: dict, force_proxy: bool = False):
    """默认直连优先，失败后按 host 记忆并回退系统代理。"""
    if force_proxy or _PROXY_ONLY:
        started = time.perf_counter()
        try:
            resp = _proxy_get(target, headers)
        except requests.RequestException as exc:
            _record_upstream_diagnostic(
                target, headers, "proxy", started,
                failure_category=_diagnostic_failure("proxy", exc),
            )
            raise
        _record_upstream_diagnostic(target, headers, "proxy", started, resp)
        return resp

    host = urlparse(target).netloc
    with _DIRECT_FAIL_LOCK:
        blocked = time.time() - _DIRECT_FAIL.get(host, 0.0) < _DIRECT_FAIL_TTL
    if not blocked:
        started = time.perf_counter()
        try:
            resp = _get_direct_session().get(
                target, headers=headers,
                timeout=(_DIRECT_CONNECT_TIMEOUT, 60), stream=True,
            )
            if resp is not None:
                if resp.status_code < 400:
                    _record_upstream_diagnostic(target, headers, "direct", started, resp)
                    return resp
                _record_upstream_diagnostic(target, headers, "direct", started, resp)
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass
        except requests.RequestException as exc:
            _record_upstream_diagnostic(
                target, headers, "direct", started,
                failure_category=_diagnostic_failure("direct", exc),
            )
        with _DIRECT_FAIL_LOCK:
            _DIRECT_FAIL[host] = time.time()
    started = time.perf_counter()
    try:
        resp = _proxy_get(target, headers)
    except requests.RequestException as exc:
        _record_upstream_diagnostic(
            target, headers, "proxy", started,
            failure_category=("direct_failure_memory" if blocked
                              else _diagnostic_failure("proxy", exc)),
        )
        raise
    _record_upstream_diagnostic(
        target, headers, "proxy", started, resp,
        failure_category="direct_failure_memory" if blocked else None,
    )
    return resp


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
                 "fh", "is_mp4", "done", "aborted")

    def __init__(self, cache, key: str, part, final, total: int | None,
                 is_mp4: bool):
        self.cache = cache
        self.key = key
        self.part = part
        self.final = final
        self.total = total
        self.written = 0
        self.fh = None
        self.is_mp4 = is_mp4
        self.done = False
        self.aborted = False

    def start(self) -> bool:
        """打开 .part 准备写盘；失败返回 False（本次不缓存，仍继续转发）。"""
        try:
            self.part.parent.mkdir(parents=True, exist_ok=True)
            self.fh = open(self.part, "wb")
            self.cache.inflight_add(self.key)
            return True
        except OSError:
            return False

    def write(self, chunk: bytes) -> None:
        if not chunk or self.aborted or self.done or self.fh is None:
            return
        try:
            self.fh.write(chunk)
            self.written += len(chunk)
        except OSError:
            self.abort()

    def finish(self) -> None:
        """上游流正常结束：写满 → commit；否则丢弃 .part。"""
        if self.done or self.aborted:
            return
        self.done = True
        try:
            if self.fh is not None:
                self.fh.close()
        except OSError:
            pass
        complete = self.total is None or self.written >= self.total
        if complete:
            if self.cache.commit_part(self.part, self.final):
                if self.is_mp4:
                    self.cache.mark_mp4(self.key, self.total or self.written,
                                        complete=True)
                else:
                    self.cache.mark_hls_segment(self.key, self.written)
                self.cache.touch(self.key)
        else:
            self.cache.discard_part(self.part)
        self.cache.inflight_remove(self.key)

    def abort(self) -> None:
        """放弃本次缓存：关文件、清 .part、退出 inflight。"""
        if self.done or self.aborted:
            return
        self.aborted = True
        try:
            if self.fh is not None:
                self.fh.close()
        except OSError:
            pass
        self.cache.discard_part(self.part)
        self.cache.inflight_remove(self.key)


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
        proxy: "MediaProxy" = self.server.proxy  # type: ignore[attr-defined]
        proxy._touch()
        proxy._begin()  # 活动请求计数：长连接流式期间看门狗不停止、不移除 token
        try:
            path = unquote(self.path.split("?", 1)[0])
            if path.startswith("/c/"):
                try:
                    proxy._serve_cache(self, path)
                except Exception as exc:  # noqa: BLE001 —— 网络波动直接断流
                    try:
                        self.send_error(502, f"proxy error: {exc}")
                    except Exception:
                        pass
                return
            if path.startswith("/e/"):
                try:
                    proxy._serve_series(self, path)
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
            force_proxy = _tuple_force_proxy(entry)
            try:
                # 拖动进度（Range 非 0 起始）若越过正在下载的缓存区间，先放弃
                # 本次 mp4 tee（不做半截文件）；本次请求仍照常走上游 206。
                if self.headers.get("Range"):
                    proxy._abort_tee_on_gap(target, self.headers.get("Range"))
                proxy._forward(self, target, headers, ad_block, force_proxy)
            except Exception as exc:  # noqa: BLE001 —— 网络波动直接断流，播放器会提示
                try:
                    self.send_error(502, f"proxy error: {exc}")
                except Exception:
                    pass
        finally:
            proxy._end()

    def log_message(self, *args):  # 静音访问日志
        pass

    def send_error(self, code, message=None, explain=None):
        """错误响应：先标记连接关闭。

        流式响应体可能已写出一半，此时再发错误头会与已发字节错位，
        复用该连接的客户端会拿到乱序数据。出错即关闭连接最安全
        （代价仅是该次请求，与 keep-alive 无关）。
        """
        self.close_connection = True
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
        self._last_use = time.time()
        self._idle_watch: threading.Thread | None = None
        self._active = 0  # 进行中的请求数（含流式长连接）：>0 时 stop 不执行
        # 外部播放器租约：VLC 进程存活期间持有的 lease id 集合。**非空时
        # 空闲看门狗完全跳过回收** —— 否则用户暂停超 _IDLE_TIMEOUT 后
        # stop() 会 _tokens.clear()，VLC 恢复播放时全部 404。VLC 退出 /
        # 重开播放器 / App 退出时由 external_player 释放。
        self._leases: set[str] = set()
        # 惰性系列注册表：key → {resolver, on_play, count, memo, sem}
        #   memo: {idx: (video, audio, location)} 按集缓存，重复请求（VLC 重试/重播）不回源
        #   sem:  该系列独占的解析并发信号量
        self._series: dict[str, dict] = {}
        self._cache = cache  # 可注入测试用缓存实例；None → 懒加载单例
        # cache_ctx key → (base_url, headers, ad_block, force_proxy)（四元组）
        self._cache_ctx: dict[str, tuple] = {}
        self._mp4_tee: dict[str, _FileTee] = {}  # key → 进行中的 mp4 落盘
        # HLS 有界预取（默认关，见 _pf_cfg）
        self._pf_cfg = self._resolve_prefetch_cfg(prefetch)
        self._seg_order: dict[str, list[str]] = {}    # cache_key → 有序分片 URL
        self._seg_pos: dict[str, dict[str, int]] = {}  # cache_key → URL → 序号
        self._pf_scheduled: set[tuple[str, str]] = set()  # 已排队 (key, url) 去重
        self._pf_futures: set = set()
        self._pf_pool = None
        self._pf_lock = threading.Lock()
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
            # 夹紧上限：防手滑把 depth 写成 1000 把源站打爆
            "depth": max(1, min(16, depth)),
            "workers": max(1, min(8, workers)),
        }

    def _prefetch_enabled(self) -> bool:
        return bool(self._pf_cfg.get("enabled"))

    def _set_seg_order(self, cache_key: str, segs: list[str]) -> None:
        """登记某播放列表的有序分片（每次重写整体替换，避免直播刷新重复累加）。"""
        with self._pf_lock:
            self._seg_order[cache_key] = segs
            self._seg_pos[cache_key] = {u: i for i, u in enumerate(segs)}

    def _ensure_pf_pool(self):
        with self._pf_lock:
            if self._pf_pool is None:
                from concurrent.futures import ThreadPoolExecutor
                self._pf_pool = ThreadPoolExecutor(
                    max_workers=self._pf_cfg["workers"],
                    thread_name_prefix="claw-pf")
            return self._pf_pool

    def _maybe_prefetch(self, cache_key: str, full_url: str) -> None:
        """播放器已取第 full_url 片 → 把后续 depth 片排队落盘。"""
        if not self._prefetch_enabled():
            return
        cache = self._cache_obj()
        if cache is None or not cache.enabled:
            return
        with self._lock:
            ctx = self._cache_ctx.get(cache_key)
            pos = self._seg_pos.get(cache_key)
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
        headers["Accept-Encoding"] = "identity"  # 落盘要原始分片字节，不能是 gzip 态
        force_proxy = _tuple_force_proxy(ctx)
        for u in window:
            with self._pf_lock:
                mark = (cache_key, u)
                if mark in self._pf_scheduled:
                    continue
                if cache.hls_segment_final(cache_key, u).is_file():
                    continue
                self._pf_scheduled.add(mark)
            try:
                fut = self._ensure_pf_pool().submit(
                    self._prefetch_one, cache_key, u, dict(headers), force_proxy)
            except RuntimeError:  # 池已关（stop 竞态）
                return
            with self._pf_lock:
                self._pf_futures.add(fut)

    def _prefetch_one(self, cache_key: str, url: str, headers: dict,
                      force_proxy: bool) -> None:
        """后台拉一个分片并按 _FileTee 落盘（best-effort，失败静默）。

        path_lock 用完整 URL 作键（与 _serve_cache 同一把）→ 播放器随后请求
        同一片时会等预取结束并直接命中 final 文件，不会重复回源。
        """
        cache = self._cache_obj()
        if cache is None:
            return
        with cache.path_lock(url):
            final = cache.hls_segment_final(cache_key, url)
            if final.is_file():
                return
            try:
                resp = _fetch_upstream(url, headers, force_proxy=force_proxy)
            except Exception:  # noqa: BLE001 —— 预取失败不影响播放
                return
            if resp.status_code >= 400:
                return
            tee = None
            try:
                tee = _FileTee(cache, cache_key,
                               cache.hls_segment_part(cache_key, url), final,
                               _content_total(resp), is_mp4=False)
                if not tee.start():
                    return
                while True:
                    chunk = resp.raw.read(_READ_CHUNK)
                    if not chunk:
                        break
                    tee.write(chunk)
                tee.finish()
            except Exception:  # noqa: BLE001
                if tee is not None:
                    tee.abort()
            finally:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
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
        with self._pf_lock:
            pool, self._pf_pool = self._pf_pool, None
        if pool is not None:
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001
                pass
        with self._pf_lock:
            self._pf_futures.clear()
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

    # 缓存上下文注册：m3u8 被重写时把 key → (base_url, headers, ad_block, force_proxy) 记下，
    # /c/<key>/ 缓存未命中时据此回落上游（分片相对 urljoin 到 base）。
    def _register_cache_ctx(self, key: str, target: str, headers: dict,
                            ad_block: dict | None,
                            force_proxy: bool = False) -> None:
        with self._lock:
            self._cache_ctx[key] = (target, _strip_stale_headers(headers),
                                    ad_block, force_proxy)

    # ------------------------------------------------------------------ #
    def build_url(self, target_url: str, headers: dict | None = None,
                  ad_block: dict | None = None,
                  force_proxy: bool = False) -> str:
        """把目标媒体 URL 打包成本地代理 URL（播放器直接播这个）。

        ad_block：可选源 ad_block 配置。存在时代理转发 m3u8 会剔除广告段
        （下载路径已有过滤；播放路径此前无过滤，广告分片会照播）。
        force_proxy：True 时该 token 的所有上游转发一律走系统代理会话
        （跳过直连探测）；默认策略为直连优先、失败回退代理。
        """
        if not target_url:
            return ""
        self._ensure_server()
        token = uuid.uuid4().hex
        with self._lock:
            self._tokens[token] = (
                target_url, _strip_stale_headers(headers or {}), ad_block, force_proxy,
            )
        return f"http://127.0.0.1:{self._server.server_address[1]}/s/{token}"

    # ------------------------------------------------------------------ #
    # 惰性系列：外部播放器全集播放列表
    # ------------------------------------------------------------------ #
    def register_series(self, resolver, on_play=None, count: int = 0,
                        force_proxy: bool = False) -> str:
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
            }
            while len(self._series) > _SERIES_MAX:   # FIFO 兜底：淘汰最旧一支
                self._series.pop(next(iter(self._series)))
        return key

    def unregister_series(self, key: str) -> None:
        """注销系列并丢弃其 memo（幂等）。"""
        with self._lock:
            self._series.pop(key, None)

    def series_episode_url(self, key: str, idx: int) -> str:
        """第 idx 集的惰性 URL（绝对地址，不含 #标题 fragment）。"""
        self._ensure_server()
        return (f"http://127.0.0.1:{self._server.server_address[1]}"
                f"/e/{key}/{int(idx)}")

    def _serve_series(self, handler, path: str) -> None:
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
        with self._lock:
            ser = self._series.get(key)
            if ser is None:
                handler.send_error(404, "series not found")
                return
            if not (0 <= idx < int(ser["count"])):
                handler.send_error(404, "episode out of range")
                return
            entry = ser["memo"].get(idx)
            sem = ser["sem"]
            resolver, on_play = ser["resolver"], ser["on_play"]
            force_proxy = ser["force_proxy"]
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
                                    force_proxy=force_proxy))
            with self._lock:
                cur = self._series.get(key)      # 解析期间可能已被注销
                if cur is not None:
                    cur["memo"][idx] = entry
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
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler)
        self._server.proxy = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
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
            if self._active > 0:
                return
        self._shutdown_prefetch()
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

    # ------------------------------------------------------------------ #
    def _new_token(self, target: str, headers: dict, ad_block: dict | None = None,
                   force_proxy: bool = False) -> str:
        token = uuid.uuid4().hex
        with self._lock:
            self._tokens[token] = (target, _strip_stale_headers(headers),
                                   ad_block, force_proxy)
        return token

    def _proxy_url(self, target: str, base: str, headers: dict,
                   ad_block: dict | None = None,
                   force_proxy: bool = False) -> str:
        full = urljoin(base, target)
        token = self._new_token(full, headers, ad_block, force_proxy)
        return f"http://127.0.0.1:{self._server.server_address[1]}/s/{token}"

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
                 force_proxy: bool = False) -> None:
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
            self._forward_m3u8(handler, target, req_headers, ad_block, force_proxy)
            return
        self._forward_media(handler, target, req_headers, ad_block, force_proxy)

    # ------------------------------------------------------------------ #
    def _serve_m3u8_text(self, handler: "_ProxyHandler", text: str,
                         target: str, req_headers: dict,
                         ad_block: dict | None = None,
                         force_proxy: bool = False) -> None:
        """把已取回的 m3u8 文本：广告过滤 → 缓存过滤结果 → 重写内部 URL → 发送。

        由 _forward_m3u8（URL 是 .m3u8）与 _forward_media（短链/参数化 URL
        内容其实是 m3u8）共用。
        """
        cache = self._ensure_cache()
        cache_on = bool(cache is not None and cache.enabled)
        # 播放路径广告过滤：HLS 一律走本地代理/过滤，剔除 m3u8 广告段再
        # 重写（不要求源配 ad_block；ad_block 显式 enabled:false 才关闭，
        # 判定与下载路径 filter_m3u8 一致）
        if ad_block is not None:
            text = self._filter_ad_segments(text, target, ad_block)
        key = None
        if cache_on:
            try:
                key = cache.key_of(target)
                cache.playlist_write(key, text.encode("utf-8", "replace"))
                self._register_cache_ctx(key, target, req_headers, ad_block,
                                         force_proxy)
            except Exception:  # noqa: BLE001 —— 缓存写失败不阻断播放
                key = None
        rewritten = self._rewrite_m3u8(text, target, req_headers, ad_block,
                                       cache_on=bool(key), cache_key=key,
                                       force_proxy=force_proxy)
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
                      force_proxy: bool = False) -> None:
        """转发 m3u8 播放列表：整读 → 重写内部 URL（含广告过滤/缓存）。"""
        # 连接池复用：requests.Session 保持到 CDN 的 keep-alive 连接，
        # HLS 分片逐个转发时不再每次重新握手（见 _get_session 注释）。
        # stream=True：只读头，body 手动流式透传（避免整段载入内存/拖慢首帧）。
        # 默认直连优先，失败后回退系统代理。
        resp = _fetch_upstream(target, req_headers, force_proxy=force_proxy)
        try:
            # 上游错误（403/404/5xx）不发 body 给播放器：原 urllib 会抛
            # HTTPError，这里等价处理（播放器收到 502 会提示换线路/重试，
            # 而不是把错误页当媒体流播放黑屏）。
            if resp.status_code >= 400:
                handler.send_error(resp.status_code, "upstream error")
                return
            # m3u8 播放列表必须整读且自动解压：stream=True 时 resp.raw 返回
            # gzip 原始字节（Content-Encoding: gzip 不解压），直接重写会乱码/
            # 截断（542B gzip vs 8871B 明文）。m3u8 是小文本，用 resp.content
            # 完整读取 + 自动解压；媒体流（mp4/ts 大文件）才用 resp.raw 流式。
            body = resp.content
            if not body.lstrip().startswith(b"#EXTM3U") and not (
                (resp.headers.get("Content-Type") or "")).find("mpegurl") >= 0:
                # URL 是 m3u8 但内容不是（可能重定向/错误页）→ 透传原始内容
                self._send_body(handler, resp, body)
                return
            text = body.decode("utf-8", "replace")
            self._serve_m3u8_text(handler, text, target, req_headers, ad_block,
                                  force_proxy)
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    def _forward_media(self, handler: "_ProxyHandler", target: str,
                       req_headers: dict, ad_block: dict | None = None,
                       force_proxy: bool = False) -> None:
        """转发普通媒体（mp4/mp3/ts/audio…）：优先本地缓存命中，否则流式转发。

        - mp4 已完整缓存 → 直接 serve 本地文件（支持 Range/206/416）
        - 否则直连/代理上游流式转发，同时 tee 落盘（写满置 complete；
          播放被中断 / 拖动越界 → 弃用 .part，不做半截文件）
        - 内容实际是 HLS（短链/参数化 URL 未含 .m3u8）→ 转 m3u8 重写路径
        """
        cache = self._ensure_cache()
        cache_on = bool(cache is not None and cache.enabled)

        # 本地缓存命中：直接 serve。命中条件 = 索引标记 complete + 文件存在。
        if cache_on:
            try:
                key = cache.key_of(target)
                entry = cache.entry(key)
                if entry and entry.get("complete") and entry.get("kind") == "mp4":
                    p = cache.mp4_final(key)
                    if p.is_file():
                        cache.touch(key)
                        self._serve_local_file(handler, p)
                        return
            except Exception:  # noqa: BLE001 —— 缓存不可用退化为直传
                cache_on = False

        # 连接池复用：requests.Session 保持到 CDN 的 keep-alive 连接，
        # HLS 分片逐个转发时不再每次重新握手（见 _get_session 注释）。
        # stream=True：只读头，body 手动流式透传（避免整段载入内存/拖慢首帧）。
        # 默认直连优先，失败后回退系统代理。
        resp = _fetch_upstream(target, req_headers, force_proxy=force_proxy)
        try:
            if resp.status_code >= 400:
                handler.send_error(resp.status_code, "upstream error")
                return
            # 判断内容是不是 HLS 播放列表（URL 未含 .m3u8 但内容是的，如
            # 短链/参数化 m3u8）。只嗅探 _SNIFF_BYTES 字节（够覆盖 #EXTM3U）：
            # 预读多了会把每个分片的首字节延后一整个读取量的到达时间。
            # 压缩流必须整读解压（无法边读边判），此分支保留整读。
            # 普通媒体：透传响应头 + 流式转发（已拦截 >=400，这里透传上游状态码）
            whole = False
            if (resp.headers.get("Content-Encoding") or "").lower() in ("gzip", "deflate", "br"):
                first = resp.content
                whole = True  # 已整读解压：上游 Content-Length 是压缩态长度
            else:
                first = resp.raw.read(_SNIFF_BYTES)
            is_m3u8 = first.startswith(b"#EXTM3U") or (
                resp.headers.get("Content-Type") or "").find("mpegurl") >= 0

            if is_m3u8:
                # 读完整文本，重写内部 URL（分片/KEY/变体）为本地代理。
                # gzip 压缩时 first 已是完整解压内容（上方分支），rest 为空。
                rest = resp.raw.read()
                text = (first + rest).decode("utf-8", "replace")
                self._serve_m3u8_text(handler, text, target, req_headers,
                                      ad_block, force_proxy)
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
                        total = _content_total(resp)
                        if total:
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
                    chunk = resp.raw.read(_READ_CHUNK)
                    if not chunk:
                        break
                    handler.wfile.write(_frame(chunk, chunked))
                    sent += len(chunk)
                    self._touch()  # 流式期间持续刷新看门狗（暂停/拖动不误杀）
                    if tee:
                        tee.write(chunk)
                if chunked:
                    handler.wfile.write(b"0\r\n\r\n")
                elif declared is not None and sent < declared:
                    # 上游提前断流：声明了长度却没写满 → 该连接已无法定界，
                    # 必须关闭，否则复用它的下一个请求会读到错位的残留字节
                    handler.close_connection = True
                if tee:
                    tee.finish()
                # 结束即从进行中字典移除：finish/abort 后残留对象会令
                # 后续请求「already is None」为假 → 断流的 mp4 再也不落盘。
                if tee:
                    self._drop_mp4_tee(key, tee)
            except (BrokenPipeError, ConnectionResetError):
                # 播放器提前关闭连接（拖动/停止）属正常；未完成的落盘弃用
                handler.close_connection = True
                if tee:
                    tee.abort()
                    self._drop_mp4_tee(key, tee)
        finally:
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

    # ------------------------------------------------------------------ #
    def _serve_cache(self, handler: "_ProxyHandler", path: str) -> None:
        """/c/<key>/<segment> 路由：HLS 分片磁盘缓存。

        命中（本地 final 文件已完整存在）→ 直接 serve 本地文件（支持 Range）；
        未命中 → 回落上游代理转发，同时 tee 落盘（写满 commit，下次命中）。
        若内容是 m3u8（变体/嵌套播放列表）→ 转 m3u8 重写路径。
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
        force_proxy = _tuple_force_proxy(ctx)
        target = urljoin(base_url, seg_name)
        # 分片级预取钩子：取到第 seg_name 片后把后续 depth 片排队落盘。
        # 放锁外：预取自身会取同名 path_lock，锁内触发会自死锁。
        if not seg_name.endswith(".m3u8"):
            self._maybe_prefetch(cache_key, target)

        # 定位到本段在缓存里的路径；已知总长才 tee（见 _content_total）。
        lock = cache.path_lock(seg_name)
        with lock:
            if seg_name.endswith(".m3u8"):
                # 嵌套/变体播放列表：按自身 URL 的独立 key 缓存放过滤文本；
                # 命中且已缓存 → 复用过滤结果，只重写（免上游往返/免重复过滤）
                sub_key = cache.key_of(target)
                p = cache.playlist_file(sub_key)
                if p.is_file():
                    cache.touch(sub_key)
                    cached = p.read_text(encoding="utf-8", errors="replace")
                    rewritten = self._rewrite_m3u8(
                        cached, target, base_headers, ad_block,
                        cache_on=True, cache_key=sub_key,
                        force_proxy=force_proxy)
                    self._emit_m3u8(handler, rewritten)
                    return
                # 未缓存 → 走下路（fetch + 嗅探 + _serve_m3u8_text 会登记 sub_key）
            else:
                final = cache.hls_segment_final(cache_key, seg_name)
                entry = cache.entry(cache_key)
                if entry and entry.get("complete") and final.is_file():
                    cache.touch(cache_key)
                    self._serve_local_file(handler, final)
                    return

            req_headers = dict(base_headers)
            req_headers["Accept-Encoding"] = "identity"
            rng = handler.headers.get("Range")
            if rng and not seg_name.endswith(".m3u8"):
                req_headers["Range"] = rng
            resp = _fetch_upstream(target, req_headers, force_proxy=force_proxy)
            try:
                if resp.status_code >= 400:
                    handler.send_error(resp.status_code, "upstream error")
                    return
                # 可能是「URL 不带 .m3u8 的嵌套播放列表」→ 先嗅探首块
                whole = False
                if (resp.headers.get("Content-Encoding") or "").lower() in ("gzip", "deflate", "br"):
                    first = resp.content
                    whole = True  # 已整读解压：上游 Content-Length 是压缩态长度
                else:
                    first = resp.raw.read(_SNIFF_BYTES)
                is_m3u8 = first.startswith(b"#EXTM3U") or (
                    resp.headers.get("Content-Type") or "").find("mpegurl") >= 0
                if is_m3u8:
                    rest = resp.raw.read()
                    text = (first + (rest or b"")).decode("utf-8", "replace")
                    self._serve_m3u8_text(handler, text, target, req_headers,
                                          ad_block, force_proxy)
                    return
                if seg_name.endswith(".m3u8"):
                    # 上游非 m3u8（动态校验失败/错误页）→ 原样回给播放器
                    self._send_body(handler, resp, first + (resp.raw.read() or b""))
                    return
                chunked, declared = _send_stream_headers(
                    handler, resp, override_len=len(first) if whole else None)

                total = _content_total(resp)
                tee = None
                # /c/ 分片：只对「从头起」的请求落盘（拖动产生的非 0 Range
                # 不满足 _safe_keys 顺序写前提），与 mp4 tee 规则一致
                if total and (_parse_range_start(handler.headers.get("Range")) in (None, 0)):
                    final = cache.hls_segment_final(cache_key, seg_name)
                    st = _FileTee(cache, cache_key, cache.hls_segment_part(cache_key, seg_name),
                                  final, total, is_mp4=False)
                    if st.start():
                        tee = st
                sent = 0
                try:
                    if first:
                        handler.wfile.write(_frame(first, chunked))
                        sent += len(first)
                        if tee:
                            tee.write(first)
                    while True:
                        chunk = resp.raw.read(_READ_CHUNK)
                        if not chunk:
                            break
                        handler.wfile.write(_frame(chunk, chunked))
                        sent += len(chunk)
                        self._touch()
                        if tee:
                            tee.write(chunk)
                    if chunked:
                        handler.wfile.write(b"0\r\n\r\n")
                    elif declared is not None and sent < declared:
                        handler.close_connection = True  # 提前断流，连接不可复用
                    if tee:
                        tee.finish()
                except (BrokenPipeError, ConnectionResetError):
                    handler.close_connection = True
                    if tee:
                        tee.abort()
            finally:
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
                            ad_block: dict | None = None) -> str:
        """剔除 m3u8 中的广告段：交给 framework.adblock 的启发式规则。

        ad_block：源 ad_block 配置（naming / m3u8 自定义规则）。内置规则
        （CUE-OUT/CUE-IN 等）默认开启；ad_block 显式 enabled:false 才停用。
        任何异常都返回原文（播放优先，过滤失败不影响播放）。
        """
        try:
            from framework.adblock import AdblockEngine  # noqa: PLC0415
            engine = AdblockEngine()
            if ad_block:
                engine.configure(type("S", (), {"raw": {"ad_block": ad_block}})())
            if engine.enabled:
                return engine.filter_m3u8(m3u8_text, base_url)
        except Exception:  # noqa: BLE001 —— 过滤引擎异常不阻断播放
            pass
        return m3u8_text

    # ------------------------------------------------------------------ #
    def _rewrite_m3u8(self, text: str, base: str, headers: dict,
                      ad_block: dict | None = None,
                      cache_on: bool = False,
                      cache_key: str | None = None,
                      force_proxy: bool = False) -> str:
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
                                              ad_block, force_proxy)
                    out.append(line.replace(f'URI="{m.group(1)}"', f'URI="{new_uri}"'))
                    continue
                uri = m.group(1)
                out.append(line.replace(f'URI="{uri}"',
                                        f'URI="{self._rewrite_cache_or_token(uri, base, headers, ad_block, cache_on, cache_key, force_proxy)}"'))
                self._note_segment(segs, uri, base, cache_on, cache_key)
                continue
            uri = line
            out.append(self._rewrite_cache_or_token(
                uri, base, headers, ad_block, cache_on, cache_key, force_proxy))
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
                                force_proxy: bool = False) -> str:
        """单个资源重写：cache_on 且已知缓存 key → /c/<key>/<full>，否则 /s/。

/c/ 资源名 = quote 解析后的完整上游 URL：_serve_cache 反解时无需再次
urljoin 猜测上下文（分片可能是绝对路径 /hls/x.ts 或相对 hls/x.ts），
unquote 即得上游原 URL，直接取回/落盘（文件存名由 _safe_seg_name 压平）。
"""
        full = urljoin(base, uri)
        cache = self._ensure_cache() if cache_on else None
        if cache_on and cache is not None and cache_key is not None:
            port = self._server.server_address[1] if self._server else 0
            name = quote(full, safe="/:@")
            return f"http://127.0.0.1:{port}/c/{cache_key}/{name}"
        return self._proxy_url(full, base, headers, ad_block, force_proxy)


def proxy_url_for(url: str, headers: dict | None = None,
                  ad_block: dict | None = None,
                  force_proxy: bool = False) -> str:
    """external_player 调用的入口：把目标 URL 打包成本地代理 URL。

    headers 为空时直接返回原 URL（无防盗链头则无需代理，避免无谓起进程）。
    force_proxy：True 时该 URL 的所有上游转发一律走系统代理会话（跳过
    直连探测）；默认策略为直连优先、失败回退代理。
    """
    if not headers:
        return url
    return MediaProxy.instance().build_url(url, headers, ad_block, force_proxy)