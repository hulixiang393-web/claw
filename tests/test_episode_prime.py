# -*- coding: utf-8 -*-
"""下一集「分片预热」回归（2026-10-29）。

背景：提前 4 分钟预取原先只预热「解析结果」这一个字符串，切到下一集时
VLC 仍要现拉 master/variant 清单 + 首片。omofuna 实测这段是 3.1s + 2.5s，
占切集卡顿的绝大部分——于是「预取明明做了，下一集还是卡」。

`prime_episode_segments` 把首几个分片提前落进代理磁盘缓存；实测首片
回源 3.56~3.88s → 本地 0.3~0.7ms。

本文件锁住三件事：主清单能下降到变体、媒体清单能取到分片、预热真的落盘。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from framework.media_proxy import MediaProxy  # noqa: E402

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=800000,RESOLUTION=1080x608
hi/index.m3u8
#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=400000,RESOLUTION=720x405
lo/index.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
000.ts
#EXTINF:10.0,
001.ts
#EXTINF:10.0,
002.ts
#EXT-X-ENDLIST
"""


def _proxy():
    return MediaProxy.instance()


def test_first_variant_picks_highest_bandwidth():
    p = _proxy()
    assert p._m3u8_first_variant(MASTER, "https://cdn.test/v/index.m3u8") == (
        "https://cdn.test/v/hi/index.m3u8")


def test_first_variant_empty_for_media_playlist():
    p = _proxy()
    assert p._m3u8_first_variant(MEDIA, "https://cdn.test/v/index.m3u8") == ""


def test_media_segments_resolved_absolute():
    p = _proxy()
    segs = p._m3u8_media_segments(MEDIA, "https://cdn.test/v/hi/index.m3u8", None)
    assert segs == [
        "https://cdn.test/v/hi/000.ts",
        "https://cdn.test/v/hi/001.ts",
        "https://cdn.test/v/hi/002.ts",
    ]


def test_media_segments_empty_for_master():
    p = _proxy()
    assert p._m3u8_media_segments(MASTER, "https://cdn.test/v/index.m3u8", None) == []


def test_prime_skips_non_hls():
    p = _proxy()
    assert p.prime_episode_segments("https://cdn.test/a.mp4") is False
    assert p.prime_episode_segments("") is False


def test_prime_skips_when_prefetch_disabled():
    p = _proxy()
    p.configure_prefetch(False, depth=0, workers=0)
    try:
        assert p.prime_episode_segments("https://cdn.test/a.m3u8") is False
    finally:
        p.configure_prefetch(True, depth=10, workers=3)


def test_prime_walks_master_then_schedules_segments(monkeypatch):
    """主清单 → 变体 → 分片，并按 segments 数量调度预取任务。"""
    p = _proxy()
    p.configure_prefetch(True, depth=10, workers=3)
    fetched = []
    scheduled = []

    def fake_fetch(url, headers, force_proxy=False, **kw):
        fetched.append(url)
        # master → 主清单；hi/index.m3u8（变体）→ 媒体清单
        return MEDIA if url.endswith("/hi/index.m3u8") else MASTER

    monkeypatch.setattr(p, "_fetch_text_upstream",
                        lambda url, h, fp: fake_fetch(url, h, fp))

    class _Cache:
        enabled = True

        def key_of(self, u):
            return "k:" + u

        class _Final:
            def is_file(self):
                return False

        def hls_segment_final(self, key, u):
            return self._Final()

    monkeypatch.setattr(p, "_cache_obj", lambda: _Cache())
    monkeypatch.setattr(p, "_prefetch_enabled", lambda: True)

    class _Pool:
        def submit(self, fn, *a, **kw):
            scheduled.append(a)

            class F:
                def add_done_callback(self, cb):
                    pass
            return F()

    monkeypatch.setattr(p, "_ensure_pf_pool", lambda: _Pool())

    ok = p.prime_episode_segments("https://cdn.test/v/master.m3u8",
                                  {"User-Agent": "x"}, None, segments=2)
    assert ok is True, (fetched, scheduled)
    assert len(scheduled) == 2, scheduled
    assert fetched[0] == "https://cdn.test/v/master.m3u8"
    # 假 Future 进了全局集合，退出时要清掉（否则 atexit 的 shutdown 会碰它）
    with p._pf_lock:
        p._pf_futures.clear()
        p._pf_scheduled.clear()


def test_prime_returns_false_when_playlist_unavailable(monkeypatch):
    p = _proxy()
    p.configure_prefetch(True, depth=10, workers=3)
    monkeypatch.setattr("framework.media_proxy._fetch_upstream",
                        lambda *a, **kw: None)
    assert p.prime_episode_segments("https://cdn.test/v/x.m3u8", {}, None) is False


def test_prime_is_wired_into_prefetch_completion():
    """预取完成回调必须调用 prime，否则切集仍要现拉首片。"""
    import inspect

    from gui.pages.reader.video_view import VideoView

    src = inspect.getsource(VideoView._warm_next_episode)
    assert "prime_episode_segments" in src
    assert "PREFETCH_PRIME_SEGMENTS" in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
