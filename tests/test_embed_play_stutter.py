# -*- coding: utf-8 -*-
"""内嵌播放器点播卡顿的回归（2026-10-29）。

现场：点「播放」后有明显卡顿一下。根因是 `VlcPlayer` 的**直连看门狗**把
「本地代理首片慢」误判成「VLC 直连 CDN 被墙/TLS 失败」，于是走
`_play_via_proxy` 重播一遍 —— 双代理 + 双取流，用户体感就是必卡一下。

约束：本文件不创建 libvlc 实例，全部用桩。
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from framework import vlc_player as vp


# ---------------------------------------------------------------------- #
# 本机回环判定
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/s/abc",
    "http://127.0.0.1:1234/c/key/seg1.ts",
    "http://localhost:9/e/3",
    "http://[::1]:80/x",
])
def test_local_mrl_detected(url):
    assert vp._is_local_mrl(url) is True


@pytest.mark.parametrize("url", [
    "https://cdn.example.com/a.m3u8",
    "http://vip.dytt-hot.com/x/index.m3u8",
    "",
])
def test_remote_mrl_not_local(url):
    assert vp._is_local_mrl(url) is False


# ---------------------------------------------------------------------- #
# 是否装看门狗（纯函数，不实例化 QObject）
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/s/abc",
    "http://127.0.0.1:1234/c/key/seg1.ts",
    "http://localhost:9/e/3",
])
def test_watchdog_skipped_for_local_proxy_url(url):
    """本地代理 URL 不装看门狗：否则会重播一遍，点播必卡。"""
    assert vp._should_watch_play(url) is False


@pytest.mark.parametrize("url", [
    "https://cdn.example.com/a.m3u8",
    "http://vip.dytt-hot.com/x/index.m3u8",
])
def test_watchdog_kept_for_direct_cdn(url):
    """直连 CDN 仍要装：它才是 TLS/被墙救回的目标。"""
    assert vp._should_watch_play(url) is True


def test_kind_hint_selects_hls_buffer():
    """代理 URL 用 kind 提示拿到 HLS 档缓冲（5000ms），而不是 unknown 的 2500。"""
    from framework.media_tuner import classify

    assert classify("kind:hls").buffer_ms == 5000
    assert classify("http://127.0.0.1:8080/s/abc").buffer_ms == 2500


# ---------------------------------------------------------------------- #
# 调优档位一致性
# ---------------------------------------------------------------------- #
def test_video_view_passes_kind_for_hls(_qapp, monkeypatch):
    """VideoView 必须按**上游原始 URL** 判类型并按 kind 传给播放器。"""
    from tests.test_embedded_player import _FakePlayer, _view

    view = _view()
    captured = {}

    class _P(_FakePlayer):
        def play(self, url, audio="", title="", kind=""):
            captured["url"] = url
            captured["kind"] = kind
            super().play(url, audio, title)

    monkeypatch.setattr(view, "_ensure_player", lambda hdrs=None: _P())
    monkeypatch.setattr(view, "_embedded_play_url",
                        lambda v, a, h, ad: ("http://127.0.0.1:1/s/tok", ""))
    monkeypatch.setattr(view, "_start_prefetch_ticker", lambda: None)

    view._play("http://cdn/x/index.m3u8", "", "第1集")
    assert captured["url"].startswith("http://127.0.0.1")
    assert captured["kind"] == "hls"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
