# -*- coding: utf-8 -*-
"""播放链路探测：清单 200 ≠ 可播放（2026-11）。

现场：检测结果标「可播放」，切过去却是黑屏。原因是旧探测只读清单前 4KB，
拿 HTTP 200 就当可用。播放器真正需要的是清单里的**分片**：

- 分片 403/404（CDN 只放行浏览器 Referer、或防盗链 token 过期）
- 分片返回 HTML 错误页（200 + `<html>`）
- 清单只有 `#EXT-X-STREAM-INF`，变体清单本身 403
- 空清单（没有 `#EXTINF`）
- 本地媒体代理把清单改坏（广告段过滤/URI 重写失效）

本文件把 `_probe_playback_chain` 的这些判定逐条锁死，全部离线（假 requests）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import framework.content.video as video_module  # noqa: E402
from framework.content import Content  # noqa: E402


class _Checker:
    def __call__(self, *args, **kwargs):
        return True


class _FakeSource:
    source_id = "demo"
    base_url = "https://site/"
    content_type = "video"
    raw = {"ad_block": {"enabled": True}}

    def transports(self):
        return {"timeout": 10}

    def request_headers(self):
        return {"User-Agent": "test", "Referer": "https://site/"}

    def headers(self):
        return self.request_headers()


class _Resp:
    def __init__(self, status=200, body=b"", ctype="application/vnd.apple.mpegurl"):
        self.status_code = status
        self._body = body
        self.headers = {"Content-Type": ctype}
        self.reads = 0

        resp = self

        class _Raw:
            def __init__(self, data):
                self._data = data
                self._pos = 0

            def read(self, size):
                resp.reads += 1
                chunk = self._data[self._pos:self._pos + size]
                self._pos += len(chunk)
                return chunk

        self.raw = _Raw(body)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _content():
    return Content(None, None, _Checker())


def _patch_get(monkeypatch, table):
    """table: {substring: _Resp 或 异常}；按顺序匹配 URL。"""
    import requests

    seen = []

    def fake_get(url, headers=None, timeout=None, stream=False):
        seen.append(url)
        for needle, resp in table.items():
            if needle in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"未预期的请求：{url}")

    monkeypatch.setattr(requests, "get", fake_get)
    return seen


def _patch_proxy(monkeypatch, mark="http://127.0.0.1:9/s/token"):
    """把媒体代理换成固定前缀，避免测试里真的起本地服务。"""
    captured = {}

    def fake_proxy(url, headers=None, ad_block=None, force_proxy=False, source_id=None):
        captured["url"] = url
        captured["ad_block"] = ad_block
        captured["source_id"] = source_id
        return mark

    monkeypatch.setattr(video_module, "proxy_url_for", fake_proxy, raising=False)
    import framework.media_proxy as media_module

    monkeypatch.setattr(media_module, "proxy_url_for", fake_proxy)
    return captured


MEDIA = b"""#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXTINF:4.0,
seg-1.ts
#EXTINF:4.0,
seg-2.ts
#EXT-X-ENDLIST
"""

MASTER = b"""#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
720p/index.m3u8
"""


def test_chain_ok_when_manifest_and_segment_reachable(monkeypatch):
    proxy = _patch_proxy(monkeypatch)
    seen = _patch_get(monkeypatch, {
        "720p/index.m3u8": _Resp(body=MEDIA),
        "seg-1.ts": _Resp(body=b"G@\x11\x00", ctype="video/mp2t"),
        "/s/token": _Resp(body=MASTER),
    })
    result = _content()._probe_playback_chain(_FakeSource(), "https://cdn.test/index.m3u8")
    assert result["ok"] is True
    assert result["via"] == "proxy"
    assert result["segment"].endswith("seg-1.ts")
    # 探测确实走了代理（播放同路），且广告配置也带过去了
    assert proxy["url"] == "https://cdn.test/index.m3u8"
    assert proxy["ad_block"] == {"enabled": True}
    assert seen[0] == "http://127.0.0.1:9/s/token"


def test_chain_flags_segment_403_even_though_manifest_is_200(monkeypatch):
    _patch_proxy(monkeypatch)
    _patch_get(monkeypatch, {
        "/s/token": _Resp(body=MEDIA),
        "seg-1.ts": _Resp(status=403, body=b'{"code":"access_denied"}'),
    })
    result = _content()._probe_playback_chain(_FakeSource(), "https://cdn.test/index.m3u8")
    assert result["ok"] is False
    assert result["reason"] == "segment_http403"


def test_chain_flags_html_error_page(monkeypatch):
    _patch_proxy(monkeypatch)
    _patch_get(monkeypatch, {
        "/s/token": _Resp(body=b"<!DOCTYPE html><html><body>404</body></html>"),
    })
    result = _content()._probe_playback_chain(_FakeSource(), "https://cdn.test/index.m3u8")
    assert result["ok"] is False
    assert result["reason"] == "html"


def test_chain_flags_empty_media_playlist(monkeypatch):
    _patch_proxy(monkeypatch)
    _patch_get(monkeypatch, {
        "/s/token": _Resp(body=b"#EXTM3U\n#EXT-X-ENDLIST\n"),
    })
    result = _content()._probe_playback_chain(_FakeSource(), "https://cdn.test/index.m3u8")
    assert result["ok"] is False
    assert result["reason"] == "no_segment"


def test_chain_flags_dead_variant(monkeypatch):
    _patch_proxy(monkeypatch)
    _patch_get(monkeypatch, {
        "/s/token": _Resp(body=MASTER),
        "720p/index.m3u8": _Resp(status=403, body=b""),
    })
    result = _content()._probe_playback_chain(_FakeSource(), "https://cdn.test/index.m3u8")
    assert result["ok"] is False
    assert result["reason"] == "http403"


def test_chain_accepts_direct_mp4_when_not_hls(monkeypatch):
    _patch_proxy(monkeypatch)
    _patch_get(monkeypatch, {
        "/s/token": _Resp(body=b"\x00\x00\x00\x18ftypmp42", ctype="video/mp4"),
    })
    result = _content()._probe_playback_chain(_FakeSource(), "https://cdn.test/a.mp4")
    assert result["ok"] is True
    assert result["reason"] == ""


def test_chain_falls_back_to_direct_when_proxy_unavailable(monkeypatch):
    import framework.media_proxy as media_module

    def boom(*args, **kwargs):
        raise RuntimeError("proxy offline")

    monkeypatch.setattr(media_module, "proxy_url_for", boom)
    _patch_get(monkeypatch, {
        "index.m3u8": _Resp(body=MEDIA),
        "seg-1.ts": _Resp(body=b"G@\x11\x00"),
    })
    result = _content()._probe_playback_chain(_FakeSource(), "https://cdn.test/index.m3u8")
    assert result["ok"] is True
    assert result["via"] == "direct"


def test_chain_timeout_when_request_raises(monkeypatch):
    _patch_proxy(monkeypatch)

    def fake_get(url, headers=None, timeout=None, stream=False):
        raise TimeoutError("read timed out")

    import requests

    monkeypatch.setattr(requests, "get", fake_get)
    result = _content()._probe_playback_chain(_FakeSource(), "https://cdn.test/index.m3u8")
    assert result["ok"] is False
    assert result["reason"].startswith("error:")


def test_chain_reads_manifest_in_chunks_and_stops_early(monkeypatch):
    """大清单不整份读：读到首段 URI 就停（聚合站 2500 段 200KB+，整份读会超时）。"""
    _patch_proxy(monkeypatch)
    huge = b"#EXTM3U\n" + b"".join(
        b"#EXTINF:4.0,\nseg-%04d.ts\n" % i for i in range(3000)
    )
    manifest = _Resp(body=huge)
    _patch_get(monkeypatch, {
        "/s/token": manifest,
        "seg-0000.ts": _Resp(body=b"G@\x11\x00"),
    })
    result = _content()._probe_playback_chain(_FakeSource(), "https://cdn.test/index.m3u8")
    assert result["ok"] is True
    assert result["segment"].endswith("seg-0000.ts")
    # 读完第一块（8KB）就该判定完成，而不是把 200KB 全读完
    assert manifest.reads == 1


# --------------------------------------------------------------------------- #
# 严格模式：用 App 的播放器（mpv）真解几帧才算「可播放」
# --------------------------------------------------------------------------- #
def _patch_player(monkeypatch, exe="C:/mpv.exe", returncode=0, raises=None):
    import framework.desktop_player as desktop_player
    import framework.subprocess_no_window as sp

    monkeypatch.setattr(
        desktop_player, "resolve",
        lambda *a, **k: ("mpv", exe) if exe else (None, None),
    )
    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        if raises is not None:
            raise raises
        return type("Done", (), {"returncode": returncode, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(sp, "run", fake_run)
    return calls


def test_player_verify_runs_real_player_on_proxy_url(monkeypatch):
    _patch_proxy(monkeypatch)
    calls = _patch_player(monkeypatch)
    result = _content()._probe_player_playback(
        _FakeSource(), "https://cdn.test/index.m3u8"
    )
    assert result["ok"] is True
    # 解的是播放器能播的那个代理地址，不是原始 CDN 地址
    assert calls["cmd"][-1] == "http://127.0.0.1:9/s/token"
    assert "--frames=8" in calls["cmd"]


def test_player_verify_reports_player_failure(monkeypatch):
    _patch_proxy(monkeypatch)
    _patch_player(monkeypatch, returncode=2)
    result = _content()._probe_player_playback(
        _FakeSource(), "https://cdn.test/index.m3u8"
    )
    assert result["ok"] is False
    assert result["reason"] == "player_failed"


def test_player_verify_skips_when_no_player_installed(monkeypatch):
    _patch_proxy(monkeypatch)
    _patch_player(monkeypatch, exe="")
    result = _content()._probe_player_playback(
        _FakeSource(), "https://cdn.test/index.m3u8"
    )
    # 本机没装播放器不能把所有线路判死
    assert result["ok"] is None
    assert result["reason"] == "no_player"


def test_strict_mode_downgrades_when_player_cannot_play(monkeypatch):
    _patch_proxy(monkeypatch)
    _patch_player(monkeypatch, returncode=2)
    content = _content()
    content._settings = {"sources_runtime": {"verify_player_probe": True}}
    monkeypatch.setattr(
        content, "_probe_playback_chain",
        lambda *a, **k: {"ok": True, "reason": "", "via": "proxy", "segment": "s"},
    )
    monkeypatch.setattr(
        content, "fetch_detail",
        lambda *a, **k: type(
            "D", (), {"chapters": [type("C", (), {"title": "正片", "url": "https://cdn.test/a.m3u8"})()]}
        )(),
    )
    result = content.probe_line_playable(
        _FakeSource(), "https://site/detail", episode_index=0
    )
    assert result["ok"] is False
    assert result["reason"] == "player_failed"
