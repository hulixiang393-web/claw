# -*- coding: utf-8 -*-
"""source_switch 线路自动切换验证脚本。

场景：
1. trtag：默认线路正常 → 取流成功（真机）。
2. trtag：模拟默认线路失效（probe 判定为死）→ 自动切到其他可用线路（mock）。
3. trtag：全部线路失效 → 抛 ContentMissingError（mock）。
4. ttdm：真实多线路失效（默认/第2线路 CDN 403）→ 自动切到可用线路，取流成功（真机）。

用法：python -u tools_verify_switch_auto.py
"""
from __future__ import annotations

import io
import json
import sys
import time
import types
import urllib.request

sys.path.insert(0, r"D:\code\claw")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from framework.config import SourceConfig
from framework.http import HttpClient
from framework.parser import Parser
from framework.selfcheck import StructureChecker
from framework.content import Content
from framework.decrypter import Decrypter

ROOT = r"D:\code\claw"


def load(sid):
    with open(rf"{ROOT}\sources\{sid}.json", encoding="utf-8") as f:
        return SourceConfig.from_dict(json.load(f), sid + ".json")


def http_ok(url: str, src) -> bool:
    """GET 播放 URL 是否可访问（200 且返回非空）。"""
    try:
        req = urllib.request.Request(url, headers=src.request_headers())
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200 and bool(resp.read(4096))
    except Exception:
        return False


def main() -> int:
    http = HttpClient(sleeper=lambda _: None)
    parser = Parser()
    checker = StructureChecker(http, parser, default_strategy="off")
    content = Content(http, parser, checker, Decrypter(http))
    fails = 0

    def check(name, cond, extra=""):
        nonlocal fails
        print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                               (" | " + extra) if extra else ""))
        if not cond:
            fails += 1

    # ------------------------------------------------------------------ #
    print("=" * 60)
    print("trtag：默认线路正常取流 + 模拟线路失效自动切换")
    print("=" * 60)
    tr = load("trtag")
    d = content.fetch_detail(tr, "https://jyoyuy.com/dy/duanjupian/406602/")
    check("trtag detail", bool(d.title) and len(d.chapters) > 0 and len(d.source_list) >= 2,
          f"chapters={len(d.chapters)} lines={len(d.source_list)}")
    ep = d.chapters[0].url if d.chapters else ""
    if ep:
        v, _a = content.fetch_video_streams(tr, ep)
        check("trtag 默认线路取流+m3u8可访问", bool(v) and http_ok(v, tr), str(v)[:70])
        time.sleep(1.2)

        # 模拟默认线路失效（probe 判定死）→ 应自动切到其他线路
        default_url = content._fetch_play_url_once(tr, ep)
        attempted = []
        real_once = content._fetch_play_url_once
        real_probe = content._play_url_probe

        def fake_probe(self, src, url):
            # 默认线路返回的播放 URL 判定为不可用，其余线路正常
            if url == default_url:
                return False
            return real_probe(src, url)

        def rec_once(self, src, url):
            attempted.append(url)
            return real_once(src, url)

        content._fetch_play_url_once = types.MethodType(rec_once, content)
        content._play_url_probe = types.MethodType(fake_probe, content)
        try:
            v2, _a2 = content.fetch_video_streams(tr, ep)
            switched = len(attempted) >= 2 and v2 != default_url
            check("trtag 模拟默认线路失效→自动切其他线路", bool(v2) and http_ok(v2, tr) and switched,
                  "尝试线路数=%d" % len(attempted))
        finally:
            content._fetch_play_url_once = real_once
            content._play_url_probe = real_probe
        time.sleep(1.2)

        # 全部线路失效 → 应抛 ContentMissingError
        content._play_url_probe = types.MethodType(
            lambda self, src, url: False, content
        )
        try:
            try:
                content.fetch_video_streams(tr, ep)
                check("trtag 全部线路失效→抛错", False)
            except Exception as exc:
                from framework.errors import ContentMissingError
                check("trtag 全部线路失效→抛错", isinstance(exc, ContentMissingError),
                      str(exc)[:60])
        finally:
            content._play_url_probe = real_probe
        time.sleep(1.2)
    else:
        check("trtag 无分集可测", False)

    # ------------------------------------------------------------------ #
    print("=" * 60)
    print("ttdm：真实线路失效自动切换（默认/第2线路 403 → 自动换可用线路）")
    print("=" * 60)
    tt = load("ttdm")
    d2 = content.fetch_detail(tt, "https://ttdm.org.cn/p/56671/")
    check("ttdm detail", bool(d2.title) and len(d2.chapters) > 0 and len(d2.source_list) >= 2,
          f"chapters={len(d2.chapters)} lines={len(d2.source_list)}")
    ep2 = d2.chapters[0].url if d2.chapters else ""
    if ep2:
        default_url2 = content._fetch_play_url_once(tt, ep2)
        check("ttdm 默认线路取流 URL（应存在）", bool(default_url2), str(default_url2)[:60])
        alive_default = http_ok(default_url2, tt)
        print("  ttdm 默认线路 m3u8 可达: %s（失效则触发自动切换）" % alive_default)

        attempted2 = []
        real_once2 = content._fetch_play_url_once
        real_probe2 = content._play_url_probe

        def rec_once2(self, src, url):
            attempted2.append(url)
            return real_once2(src, url)

        content._fetch_play_url_once = types.MethodType(rec_once2, content)
        content._play_url_probe = types.MethodType(
            lambda self, src, url: real_probe2(tt, url), content
        )
        try:
            v3, _a3 = content.fetch_video_streams(tt, ep2)
            multi = len(attempted2) >= 2
            ok3 = bool(v3) and http_ok(v3, tt)
            check("ttdm 自动换线路取流成功", ok3,
                  "尝试线路数=%d 结果=%s" % (len(attempted2), str(v3)[:60]))
            if ok3 and not alive_default:
                check("ttdm 返回线路与默认线路不同", v3 != default_url2, "默认已失效，自动换线")
        finally:
            content._fetch_play_url_once = real_once2
            content._play_url_probe = real_probe2
    else:
        check("ttdm 无分集可测", False)

    http.close()
    print()
    print("=== source_switch 自动切换验证: %s ===" % ("ALL PASS" if fails == 0 else "%d 项失败" % fails))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
