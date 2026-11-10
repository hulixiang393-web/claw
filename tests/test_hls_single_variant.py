# -*- coding: utf-8 -*-
"""HLS 主清单折叠回归（2026-11-01）。

现场："全部视频载入都慢"，一集片子起播要等十几秒。用本地合成上游 + 真 mpv
（tools/_probe_startup.py）测出根因之一：**ffmpeg/mpv 会把主清单里每个变体
都打开，并从每个变体各读头部若干片**。3 个变体就是 3 路并发抢同一条出口带宽，
外加上代理自己的起播预取也按变体各跑一遍（＝再乘一遍），慢 CDN 上首帧被推后
好几秒；每个变体还各占一个磁盘缓存目录（max_videos=5 时一部片子吃掉 3 个槽）。

修法：主清单只下发一个变体（最高码率，与播放器本来就会选的那路一致），
`media.hls.single_variant=false` 可恢复原行为。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from framework import media_proxy as mp  # noqa: E402
from framework.settings_manager import SettingsManager, media_hls_settings  # noqa: E402

MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="cn",URI="audio/cn.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,AUDIO="aud"
v360/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1920x1080,AUDIO="aud"
v1080/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,AUDIO="aud"
v720/index.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
000.ts
#EXT-X-ENDLIST
"""


def _uris(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.startswith("#")]


# --------------------------------------------------------------------------- #
# 1. 折叠本身
# --------------------------------------------------------------------------- #
def test_keeps_only_highest_bandwidth_variant():
    out = mp.collapse_master_variants(MASTER)
    assert _uris(out) == ["v1080/index.m3u8"]
    assert out.count("#EXT-X-STREAM-INF") == 1


def test_keeps_audio_group_and_original_position():
    out = mp.collapse_master_variants(MASTER)
    # 独立音轨组引用必须保留，否则有独立音轨的源会没声音
    assert "#EXT-X-MEDIA:TYPE=AUDIO" in out
    assert 'AUDIO="aud"' in out
    # 保留下来的还是原来那一行（顺序不乱：STREAM-INF 紧跟自己的 URI）
    lines = out.splitlines()
    idx = next(i for i, ln in enumerate(lines) if ln.startswith("#EXT-X-STREAM-INF"))
    assert lines[idx + 1].strip() == "v1080/index.m3u8"


def test_media_playlist_untouched():
    assert mp.collapse_master_variants(MEDIA) == MEDIA


def test_single_variant_master_untouched():
    one = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nonly.m3u8\n"
    assert mp.collapse_master_variants(one) == one


def test_unknown_shape_is_returned_unchanged():
    """STREAM-INF 后面不是 URI 行 → 看不懂就不猜，宁可多请求也别改坏清单。"""
    weird = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n#comment\n"
             "#EXT-X-STREAM-INF:BANDWIDTH=2\nb.m3u8\n")
    assert mp.collapse_master_variants(weird) == weird


def test_tie_on_bandwidth_keeps_first():
    tie = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100,RESOLUTION=1280x720\nA.m3u8\n"
           "#EXT-X-STREAM-INF:BANDWIDTH=100,RESOLUTION=640x360\nB.m3u8\n")
    assert _uris(mp.collapse_master_variants(tie)) == ["A.m3u8"]


# --------------------------------------------------------------------------- #
# 2. 真实代理路径：下发的清单里只剩一路
# --------------------------------------------------------------------------- #
class _StubHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.body = b""

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.headers[name] = value

    def end_headers(self):
        pass

    class _WFile:
        def __init__(self, outer):
            self._outer = outer

        def write(self, data):
            self._outer.body += data

    @property
    def wfile(self):
        return _StubHandler._WFile(self)


@pytest.fixture
def proxy(tmp_path, monkeypatch):
    from framework.media_cache import MediaCache

    monkeypatch.setattr(mp, "_SINGLE_VARIANT_STATE",
                        [time.monotonic() + 3600, True])
    cache = MediaCache(root=tmp_path / "v", max_videos=5, enabled=True)
    p = mp.MediaProxy(cache=cache, prefetch={})
    yield p
    p.stop()


def test_served_playlist_has_single_variant(proxy, monkeypatch):
    monkeypatch.setattr(proxy, "_filter_ad_segments",
                        lambda text, *a, **kw: text)
    handler = _StubHandler()
    proxy._serve_m3u8_text(handler, MASTER, "https://cdn.test/v/index.m3u8",
                           {"User-Agent": "t"})
    body = handler.body.decode()
    assert handler.status == 200
    assert body.count("#EXT-X-STREAM-INF") == 1
    assert "v1080/index.m3u8" in body
    assert "v360/index.m3u8" not in body and "v720/index.m3u8" not in body


def test_opt_out_keeps_all_variants(proxy, monkeypatch):
    """single_variant=false → 一个变体都不删（回到多画质探测）。"""
    monkeypatch.setattr(mp, "_SINGLE_VARIANT_STATE",
                        [time.monotonic() + 3600, False])
    monkeypatch.setattr(proxy, "_filter_ad_segments",
                        lambda text, *a, **kw: text)
    handler = _StubHandler()
    proxy._serve_m3u8_text(handler, MASTER, "https://cdn.test/v/index.m3u8",
                           {"User-Agent": "t"})
    body = handler.body.decode()
    assert body.count("#EXT-X-STREAM-INF") == 3


# --------------------------------------------------------------------------- #
# 3. 配置读取
# --------------------------------------------------------------------------- #
def test_media_hls_settings_default_is_single_variant(tmp_path):
    sm = SettingsManager(tmp_path / "app_config.json")
    assert media_hls_settings(sm) == {"single_variant": True}


def test_media_hls_settings_can_opt_out(tmp_path):
    sm = SettingsManager(tmp_path / "app_config.json")
    sm.set("media", "hls", {"single_variant": False})
    assert media_hls_settings(sm) == {"single_variant": False}


def test_media_hls_settings_survives_garbage(tmp_path):
    sm = SettingsManager(tmp_path / "app_config.json")
    sm.set("media", "hls", "not-a-dict")
    assert media_hls_settings(sm) == {"single_variant": True}
