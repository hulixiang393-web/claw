# -*- coding: utf-8 -*-
"""media_proxy 本地代理验证：防盗链 m3u8 重写 / mp4 Range / 无头直连。"""
import io, sys, pathlib, threading, json, urllib.request
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 1) 本地模拟「防盗链站」：不带正确 Referer/UA 返回 403，带则返回数据
hits = {"reqs": 0, "referers": [], "uas": []}
M3U8_TEMPLATE = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-KEY:METHOD=AES-128,URI="{cdn}/key.bin"
#EXTINF:10.0,
/seg/1001.ts
#EXTINF:10.0,
{cdn}/seg/1002.ts
#EXT-X-STREAM-INF:BANDWIDTH=1527000,RESOLUTION=1280x720
/variant/index.m3u8
#EXT-X-ENDLIST
"""

class FakeCDN(BaseHTTPRequestHandler):
    def do_GET(self):
        hits["reqs"] += 1
        hits["referers"].append(self.headers.get("Referer"))
        hits["uas"].append(self.headers.get("User-Agent"))
        if self.headers.get("Referer") != "https://site.com/":
            self.send_error(403); return
        if self.path.startswith("/playlist.m3u8"):
            body = M3U8; ct = "application/vnd.apple.mpegurl"
        elif self.path.startswith("/key.bin"):
            body = b"\x00" * 16; ct = "application/octet-stream"
        elif self.path.endswith(".ts"):
            body = b"\x47" * 1024; ct = "video/mp2t"
        elif self.path.startswith("/variant"):
            body = b"#EXTM3U\n#EXTINF:10.0,\n/seg/1003.ts\n#EXT-X-ENDLIST\n"; ct = "application/vnd.apple.mpegurl"
        elif self.path.startswith("/movie.mp4"):
            body = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100000; ct = "video/mp4"
        else:
            self.send_error(404); return
        rng = self.headers.get("Range")
        if rng:
            # 模拟真实 CDN：按 Range 截断响应体
            import re as _re
            m = _re.match(r"bytes=(\d*)-(\d*)", rng)
            start = int(m.group(1) or 0)
            end = int(m.group(2)) if m.group(2) else len(body) - 1
            end = min(end, len(body) - 1)
            body = body[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeCDN)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
CDN = f"http://127.0.0.1:{port}"
M3U8 = M3U8_TEMPLATE.replace("{cdn}", CDN).encode()

from framework.media_proxy import MediaProxy, proxy_url_for

mp = MediaProxy()
hdrs = {"Referer": "https://site.com/", "User-Agent": "Mozilla/5.0 Chrome"}

ok = 0
def check(name, cond, extra=""):
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if cond: ok += 1

# 2) m3u8 经代理
print("=== m3u8 代理 ===")
local = mp.build_url(f"{CDN}/playlist.m3u8", hdrs)
print("  本地 URL:", local[:60])
data = urllib.request.urlopen(local, timeout=10).read().decode()
check("m3u8 拉取成功(带header)", "EXTM3U" in data and "EXT-X-KEY" in data)
check("KEY URI 被代理", 'URI="http://127.0.0.1' in data, [l for l in data.splitlines() if "KEY" in l][0][:70])
check("相对分片被代理", "http://127.0.0.1:" in data and "/s/" in data)
check("绝对分片被代理", data.count("/s/") >= 4)
print("  重写后:\n" + "\n".join("   " + l for l in data.splitlines()[:7]))

# 3) 通过代理请求分片（key / ts / variant / 绝对分片）
print("=== 分片/KEY 代理 ===")
key_url = [l for l in data.splitlines() if "KEY" in l][0].split('URI="')[1].split('"')[0]
k = urllib.request.urlopen(key_url, timeout=10).read()
check("KEY 通过代理拿到 16 字节", len(k) == 16)
seg_lines = [l for l in data.splitlines() if l.startswith("http://127.0.0.1")]
s1 = urllib.request.urlopen(seg_lines[0], timeout=10).read()
check("分片1 通过代理 1024B", len(s1) == 1024, f"{len(s1)}B")
s2 = urllib.request.urlopen(seg_lines[1], timeout=10).read()
check("绝对分片2 通过代理", len(s2) == 1024)
var = urllib.request.urlopen(seg_lines[2], timeout=10).read().decode()
check("变体 m3u8 再次重写", "/s/" in var, [l for l in var.splitlines() if not l.startswith("#")][0][:50])

# 4) mp4 Range
print("=== mp4 Range ===")
murl = mp.build_url(f"{CDN}/movie.mp4", hdrs)
req = urllib.request.Request(murl, headers={"Range": "bytes=0-99"})
resp = urllib.request.urlopen(req, timeout=10)
body = resp.read()
check("Range 透传返回 206", resp.status == 206, f"status={resp.status}")
check("Range 截取 100B", len(body) == 100, f"{len(body)}B")

# 5) 源站 header 正确性
print("=== 源站收到 header ===")
check("Referer 已带", all(r == "https://site.com/" for r in hits["referers"][1:]))
check("UA 已带(浏览器)", all("Chrome" in (u or "") for u in hits["uas"][1:]))

# 6) 无防盗链头直连
print("=== 无头直连 ===")
plain = proxy_url_for(f"{CDN}/movie.mp4", None)
check("无头时原样返回 URL", plain == f"{CDN}/movie.mp4")

mp.stop()
print(f"\n=== media_proxy 验证: {ok} 项通过 ===")
