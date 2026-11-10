# -*- coding: utf-8 -*-
"""起播耗时剖面（本地合成上游，不联外网）。

目的：把「点开一集到第一帧」拆成可测量的几段，判断慢在**本地链路**还是
**上游网络**：

    playlist(master) → playlist(variant) → 首片 → 播放器起播

做法：
1. 本地起一个假上游（master + 3 个变体 + 若干 300KB 分片），记录每个请求
   的开始/结束时间 → 代理与播放器各请求了什么、各花多久，一目了然；
2. 用 MediaProxy 把 master 包成本地代理 URL（与真实播放同一条链路：广告
   过滤、清单重写、/c/ 分片缓存、预取）；
3. 用 PlayerSession（mpv + JSON IPC）真播，测 spawn → file-loaded →
   首个 time-pos 的时间。

用法：
    python tools/_probe_startup.py            # 全流程
    python tools/_probe_startup.py proxy      # 只测代理链路（不起 mpv）
"""
from __future__ import annotations

import http.server
import os
import re
import socketserver
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SEG_BYTES = 300 * 1024
SEG_COUNT = 12
SEG_SECONDS = 4.0

# 模拟按连接限速的 CDN（KB/s）。设 0 表示不限速；慢速 CDN 实测 100~300KB/s，
# 用它验证"同一片被下载两次"这类浪费在真实带宽下的代价。
SEG_RATE_KBPS = float(os.environ.get("PROBE_SEG_RATE_KBPS") or 0)

REQUESTS: list[dict] = []
_LOCK = threading.Lock()

T0 = time.monotonic()


def _now() -> float:
    return time.monotonic() - T0


def _variant_line(name: str, bandwidth: int) -> str:
    return (f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION=1280x720\n"
            f"{name}.m3u8\n")


def _master() -> bytes:
    body = ("#EXTM3U\n#EXT-X-VERSION:3\n"
            + _variant_line("v1080", 4_000_000)
            + _variant_line("v720", 2_000_000)
            + _variant_line("v360", 800_000))
    return body.encode()


def _media_playlist(variant: str = "") -> bytes:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:4",
             "#EXT-X-MEDIA-SEQUENCE:0"]
    for i in range(SEG_COUNT):
        lines.append(f"#EXTINF:{SEG_SECONDS:.3f},")
        lines.append(f"{variant}seg{i:03d}.ts")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines).encode()


_SEGMENT = bytes(bytearray(range(256))) * (SEG_BYTES // 256)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        started = _now()
        record = {"path": path, "start": started, "end": None, "bytes": 0}
        with _LOCK:
            REQUESTS.append(record)
        try:
            if path.endswith("master.m3u8"):
                body = _master()
                ctype = "application/vnd.apple.mpegurl"
            elif path.endswith(".m3u8"):
                variant = path.rsplit("/", 1)[-1][: -len(".m3u8")]
                body = _media_playlist(variant)
                ctype = "application/vnd.apple.mpegurl"
            elif path.endswith(".ts"):
                body = _SEGMENT
                ctype = "video/mp2t"
            else:
                self.send_error(404)
                return
            # Range 支持（真实 CDN 的静态分片都支持）：代理"暂停预取 → 续传"
            # 的前提就是上游会给 206。别在这里偷懒，否则测出来的重复下载是假的。
            start = 0
            rng = self.headers.get("Range") or ""
            match = re.match(r"bytes=(\d+)-(\d*)", rng.strip())
            if match and path.endswith(".ts"):
                start = int(match.group(1))
                if start >= len(body):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(body)}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range",
                                 f"bytes {start}-{len(body) - 1}/{len(body)}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body) - start))
            self.end_headers()
            if SEG_RATE_KBPS and path.endswith(".ts"):
                # 按限速切片推流（每片 32KB，睡够时间）
                step = 32 * 1024
                for off in range(start, len(body), step):
                    self.wfile.write(body[off:off + step])
                    record["bytes"] += len(body[off:off + step])
                    time.sleep(step / (SEG_RATE_KBPS * 1024))
                return
            # 分四块慢慢写，模拟 CDN 边下边推（不是一次性灌完）
            chunk = max(1, (len(body) - start) // 4)
            for off in range(start, len(body), chunk):
                self.wfile.write(body[off:off + chunk])
                record["bytes"] += len(body[off:off + chunk])
                time.sleep(0.01)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            record["end"] = _now()


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def _start_upstream() -> tuple:
    server = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _make_proxy(tmp_root: Path):
    from framework.media_cache import MediaCache
    from framework.media_proxy import MediaProxy

    cache = MediaCache(root=tmp_root, max_videos=20, max_bytes=4096 * 1024 ** 2)
    proxy = MediaProxy(cache=cache,
                       prefetch={"enabled": True, "depth": 6, "workers": 3})
    return proxy


def _report(mark: str) -> None:
    print(f"\n--- upstream 请求（{mark}）---")
    with _LOCK:
        rows = sorted(REQUESTS, key=lambda r: r["start"])
        REQUESTS.clear()
    for row in rows:
        took = (row["end"] or _now()) - row["start"]
        print(f"  {row['start']:7.3f}s  {took:6.3f}s  {row['bytes']:>8}B  {row['path']}")
    variants = {r["path"] for r in rows if r["path"].endswith(".m3u8")}
    seg_rows = [r for r in rows if r["path"].endswith(".ts")]
    total = sum(r["bytes"] for r in seg_rows)
    uniq = {r["path"] for r in seg_rows}
    print(f"  清单请求数={len(variants)} 分片请求数={len(seg_rows)} "
          f"（不同分片 {len(uniq)}）")
    print(f"  分片上游总字节={total} "
          f"= 单份 {len(uniq) * SEG_BYTES} 的 {total / max(len(uniq) * SEG_BYTES, 1):.2f}×")


def run_proxy_only() -> None:
    """只测代理链路：播放器视角顺序拉 master → variant → 前 3 片。"""
    import requests

    server, base = _start_upstream()
    tmp = Path(tempfile.mkdtemp(prefix="claw-probe-"))
    try:
        proxy = _make_proxy(tmp)
        url = proxy.build_url(f"{base}/master.m3u8",
                              {"Referer": "https://example.test/"},
                              source_id="probe")
        session = requests.Session()
        t = time.monotonic()
        master = session.get(url, timeout=10)
        print(f"[proxy] master  -> {master.status_code} {(time.monotonic()-t)*1000:7.1f}ms"
              f"  {len(master.text)}B")
        # 走第一个变体（代理已把相对地址重写成 /c/ 本地路由）
        first_variant = next(
            ln.strip() for ln in master.text.splitlines()
            if ln.strip() and not ln.startswith("#"))
        t = time.monotonic()
        media = session.get(first_variant, timeout=10)
        print(f"[proxy] variant -> {media.status_code} {(time.monotonic()-t)*1000:7.1f}ms"
              f"  {len(media.text)}B")
        segs = [ln.strip() for ln in media.text.splitlines()
                if ln.strip() and not ln.startswith("#")][:3]
        for index, seg in enumerate(segs):
            t = time.monotonic()
            resp = session.get(seg, timeout=20)
            print(f"[proxy] seg{index}    -> {resp.status_code} "
                  f"{(time.monotonic()-t)*1000:7.1f}ms  {len(resp.content)}B")
        _report("proxy only")
        print(f"[cache] {tmp}")
    finally:
        server.shutdown()


def run_full() -> None:
    """全流程：代理 + mpv 真播，测 spawn → file-loaded → 首个 time-pos。"""
    from PySide6.QtCore import QCoreApplication, QTimer

    from framework.player_session import ST_ERROR, PlayerSession

    server, base = _start_upstream()
    tmp = Path(tempfile.mkdtemp(prefix="claw-probe-"))
    app = QCoreApplication(sys.argv)
    try:
        proxy = _make_proxy(tmp)
        url = proxy.build_url(f"{base}/master.m3u8",
                              {"Referer": "https://example.test/"},
                              source_id="probe")
        session = PlayerSession(referer="https://example.test/",
                                user_agent="claw-probe/1.0")
        marks: dict[str, float] = {}
        errors: list[str] = []

        def mark(name: str) -> None:
            marks.setdefault(name, _now())

        session.state_changed.connect(lambda st: mark(f"state{st}"))
        session.time_changed.connect(lambda ms: mark("first_pos"))
        session.error.connect(errors.append)

        t = time.monotonic()
        session.play(url, "", "probe", kind="hls", network_caching_ms=5000)
        mark("spawn_return")
        print(f"[mpv] play() 返回耗时 {(time.monotonic()-t)*1000:.1f}ms")
        QTimer.singleShot(12000, app.quit)
        app.exec()
        session.close()
        base_t = marks.get("spawn_return", 0.0)
        for name in ("state1", "state3", "first_pos"):
            if name in marks:
                print(f"[mpv] {name:12s} +{marks[name]-base_t:6.3f}s"
                      f"（绝对 {marks[name]:6.3f}s）")
        if "state3" not in marks and "first_pos" not in marks:
            print("[mpv] 12s 内没有起播", errors or "")
        _report("proxy + mpv")
    finally:
        server.shutdown()


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what == "proxy":
        run_proxy_only()
    else:
        run_full()
