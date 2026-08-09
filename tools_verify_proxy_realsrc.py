# -*- coding: utf-8 -*-
"""真实源验证：bilibili mp4 / ttdm m3u8 经本地代理后可访问（带源 headers）。"""
import io, sys, pathlib, json, urllib.request
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from framework.config import SourceConfig
from framework.http import HttpClient
from framework.parser import Parser
from framework.selfcheck import StructureChecker
from framework.content import Content
from framework.decrypter import Decrypter
from framework.discovery import Discovery
from framework.media_proxy import MediaProxy

SRC_DIR = pathlib.Path(__file__).resolve().parent / "sources"
http = HttpClient(sleeper=lambda _: None)
parser = Parser()
checker = StructureChecker(http, parser, default_strategy="off")
content = Content(http, parser, checker, Decrypter(http))
disc = Discovery(http, parser, checker)

def get_src(sid):
    return SourceConfig.from_dict(json.loads((SRC_DIR / f"{sid}.json").read_text(encoding="utf-8")), SRC_DIR)

ok = 0
def check(name, cond, extra=""):
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if cond: ok += 1

mp = MediaProxy()

# bilibili：mp4 直链带 header 经代理
print("=== bilibili ===")
bili = get_src("bilibili")
try:
    works = disc.list_works(bili, "https://www.bilibili.com/v/popular/all", 1)
    d = content.fetch_detail(bili, works[0].url)
    v, a = content.fetch_video_streams(bili, d.chapters[0].url)
    hdrs = bili.request_headers()
    local = mp.build_url(v, hdrs)
    print(f"  原 URL: {v[:70]}")
    print(f"  本地: {local[:60]}")
    resp = urllib.request.urlopen(local, timeout=15)
    head = resp.read(100000)
    check("bili mp4 经代理 200", resp.status == 200, f"status={resp.status} len={len(head)}")
    check("mp4 魔数 ftyp", b"ftyp" in head[:16], head[:16].hex())
except Exception as e:
    print(f"  bili 异常: {type(e).__name__}: {str(e)[:120]}")

# ttdm：403 m3u8 带 header 经代理
print("=== ttdm ===")
ttdm = get_src("ttdm")
try:
    works = disc.list_works(ttdm, "https://ttdm.org.cn/", 1)
    d = content.fetch_detail(ttdm, works[0].url)
    v, a = content.fetch_video_streams(ttdm, d.chapters[0].url)
    hdrs = ttdm.request_headers()
    local = mp.build_url(v, hdrs)
    resp = urllib.request.urlopen(local, timeout=15)
    text = resp.read(3000).decode("utf-8", "replace")
    check("ttdm m3u8 经代理 200", resp.status == 200, f"status={resp.status}")
    check("ttdm m3u8 内容", "#EXTM3U" in text or "#EXT-X" in text, text[:50])
    # 如果 m3u8 被重写，分片也应可访问
    import re
    segs = [l for l in text.splitlines() if l.startswith("http://127.0.0.1")]
    if segs:
        try:
            s = urllib.request.urlopen(segs[0], timeout=15).read(500)
            check("ttdm 分片经代理可访问", len(s) > 0, f"{len(s)}B")
        except Exception as e:
            check("ttdm 分片经代理可访问", False, str(e)[:60])
except Exception as e:
    print(f"  ttdm 异常: {type(e).__name__}: {str(e)[:120]}")

mp.stop()
print(f"\n=== 真实源代理验证: {ok} 项通过 ===")
