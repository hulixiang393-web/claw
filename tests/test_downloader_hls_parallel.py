# -*- coding: utf-8 -*-
"""HLS 分段并行下载测试（test_downloader_hls_parallel.py）。

背景：_download_hls 原本逐段串行下载，单连接 CDN 限速（ikanpp 实测单连接
只能跑几百 KB/s），源配置 media.hls.workers（ikanpp.json=4）此前从未被消费。
修复：分段下载改 ThreadPoolExecutor 并行，worker 数取 source.raw.media.hls.workers
（默认 4），限量在飞任务保证取消/暂停响应快；广告段跳过不下载。

覆盖（全部 mock，不联网）：并行度峰值受限 ≤workers 且非串行、无 hls 声明默认 4、
广告段不下载、取消信号能中断且限量在飞、master→variant 取流逻辑不受影响。
"""
import os
import sys
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import framework.adblock as adblock_mod  # noqa: E402
from framework.downloader import Downloader  # noqa: E402
from framework.ffmpeg_merger import MergeCancelled  # noqa: E402


def _m3u8(n_segs, ads=()):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:4", "#EXT-X-MEDIA-SEQUENCE:0"]
    for i in range(n_segs):
        lines.append("#EXTINF:4.000,")
        lines.append(f"http://cdn/ads/{i}.ts" if i in ads else f"http://cdn/seg{i}.ts")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines)


class _FakeHlsHttp:
    """按 URL 返回 m3u8/分段字节，并统计 get_bytes 并行度（活跃连接数峰值）。"""

    def __init__(self, m3u8_text, seg_bytes=b"SEG-DATA"):
        self.m3u8_text = m3u8_text
        self.seg_bytes = seg_bytes
        self.seg_calls = []  # url 列表
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def get_text(self, url, headers=None, **k):
        if "master" in url:
            return "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nhttp://cdn/variant.m3u8\n"
        return self.m3u8_text

    def get_bytes(self, url, headers=None, **k):
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            time.sleep(0.02)  # 放大重叠窗口，让并行度可观测
            with self._lock:
                self.seg_calls.append(url)
            return self.seg_bytes + str(url).encode()
        finally:
            with self._lock:
                self._active -= 1


class _FakeSettings:
    def get(self, section, key, default=None):
        return default


def _downloader(http):
    return Downloader(None, http, _FakeSettings())


def _source(raw_media=None):
    raw = {"media": {}}
    if raw_media:
        raw["media"]["hls"] = raw_media
    return SimpleNamespace(raw=raw)


def _stub_adblock(ad_indices=None, monkeypatch=None):
    """monkeypatch adblock_for → 固定广告段索引的桩引擎。"""
    if ad_indices is None:
        ad_indices = ()
    stub = SimpleNamespace(
        enabled=True,
        detect_m3u8_ads=lambda text, base: (list(ad_indices), text),
    )
    monkeypatch.setattr(adblock_mod, "adblock_for", lambda *a, **k: stub)


def _patch_remux(monkeypatch, out_path):
    """把 _remux_hls 替换为直接写输出文件（跳过真实 ffmpeg）。"""
    def _fake_remux(self, local_m3u8, path, progress_cb, cancel_evt, pause_evt):
        local_m3u8 = os.fspath(local_m3u8)
        if cancel_evt is not None and cancel_evt.is_set():
            raise MergeCancelled("合并已取消")
        with open(os.fspath(path), "wb") as f:
            f.write(b"MP4-REMUX")
    monkeypatch.setattr(Downloader, "_remux_hls", _fake_remux)


def _run(tmp_path, n_segs=8, workers=4, ads=(), monkeypatch=None):
    out = tmp_path / "out.mp4"
    _patch_remux(monkeypatch, out)
    _stub_adblock(ads, monkeypatch)
    http = _FakeHlsHttp(_m3u8(n_segs, ads=ads))
    d = _downloader(http)
    size = d._download_hls(
        "http://cdn/play.m3u8", {"Referer": "http://x/"}, str(out),
        progress_cb=None, cancel_evt=None, pause_evt=None,
        source=_source({"workers": workers}),
    )
    return http, size, out


def test_hls_parallel_bounded_by_workers(tmp_path, monkeypatch):
    """分段下载并行度峰值 ≤ workers；workers>1 时必有并发（非串行）。"""
    workers = 4
    http, size, out = _run(tmp_path, n_segs=16, workers=workers, monkeypatch=monkeypatch)
    assert http.max_active <= workers
    assert http.max_active >= 2  # 并行生效（旧的串行实现恒为 1）
    assert size > 0
    assert len(http.seg_calls) == 16
    assert out.is_file()


def test_hls_default_workers_4_when_unconfigured(tmp_path, monkeypatch):
    """源未声明 media.hls.workers → 默认 4。"""
    out = tmp_path / "out.mp4"
    _patch_remux(monkeypatch, out)
    _stub_adblock((), monkeypatch)
    http = _FakeHlsHttp(_m3u8(12))
    d = _downloader(http)
    size = d._download_hls(
        "http://cdn/play.m3u8", {}, str(out), source=_source(),
    )
    assert 2 <= http.max_active <= 4
    assert size > 0
    assert len(http.seg_calls) == 12


def test_hls_skip_ad_segments(tmp_path, monkeypatch):
    """广告段不下载、不进 get_bytes。"""
    http, size, out = _run(tmp_path, n_segs=8, workers=4, ads=(2, 4), monkeypatch=monkeypatch)
    assert len(http.seg_calls) == 6  # 8 - 2 个广告段
    assert all("/ads/" not in url for url in http.seg_calls)
    assert size > 0


def test_hls_parallel_respects_cancel(tmp_path, monkeypatch):
    """取消信号已登记时，并行下载抛 MergeCancelled，且限量在飞 ≤workers 段。"""
    cancel_evt = threading.Event()
    cancel_evt.set()
    out = tmp_path / "out.mp4"
    _patch_remux(monkeypatch, out)
    _stub_adblock((), monkeypatch)
    http = _FakeHlsHttp(_m3u8(8))
    d = _downloader(http)
    with pytest.raises(MergeCancelled):
        d._download_hls(
            "http://cdn/play.m3u8", {}, str(out),
            progress_cb=None, cancel_evt=cancel_evt, pause_evt=None,
            source=_source({"workers": 2}),
        )
    assert len(http.seg_calls) <= 2


def test_variant_master_playlist_still_works(tmp_path, monkeypatch):
    """master 多画质仍走 variant 子列表（并行化不改变取流逻辑）。"""
    out = tmp_path / "out.mp4"
    _patch_remux(monkeypatch, out)
    _stub_adblock((), monkeypatch)
    http = _FakeHlsHttp(_m3u8(5))
    d = _downloader(http)
    size = d._download_hls(
        "http://cdn/master.m3u8", {}, str(out), source=_source({"workers": 3}),
    )
    assert http.max_active <= 3
    assert len(http.seg_calls) == 5
    assert size > 0


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))