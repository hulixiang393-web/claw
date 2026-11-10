# -*- coding: utf-8 -*-
"""第三方播放器解析（external_player）单元测试。

真实链路（omofuna 精品线路）：播放页 `player_aaaa` 只给 32 位不透明 id，
第三方播放器页面里 `playData('<密文>','<key>')` 才是真直链，解密要跑站点自己的
crypto-js + 混淆 common.js。这里用假页面/假脚验证接线与降级，真实站点行为由
现场验证覆盖。
"""
import json
import os
from types import SimpleNamespace

import pytest

from framework import external_player as ep


class FakeHttp:
    def __init__(self, page='<script>var u=playData(\'AAA\',\'BBB\');</script>',
                 scripts=None):
        self.page = page
        self.scripts = scripts or {
            "crypto-js.min.js": "var CryptoJS = {};",
            "common.js": (
                "function playData(cipher, key) {"
                "  return 'https://cdn.example.com/ok/index.mp4?mime_type=video_mp4';"
                "}"
            ),
        }
        self.calls = []

    def get_text(self, url, headers=None, **kwargs):
        self.calls.append(url)
        if "player_host" in url or "v2player" in url:
            return self.page
        name = url.rsplit("/", 1)[-1]
        if name in self.scripts:
            return self.scripts[name]
        raise AssertionError(f"unexpected url: {url}")


CFG = {
    "from": ["mp4"],
    "player_url": "https://player_host/player/?url={enc_url}&sid={sid}&nid={nid}",
    "cipher_regex": r"playData\('([^']+)','([^']+)'\)",
    "scripts": [
        "https://cdn.example.com/js/crypto-js.min.js",
        "https://cdn.example.com/js/common.js",
    ],
}

PLAYER = {"from": "mp4", "url": "%66%64%65%38", "sid": 1, "nid": 1}
EPISODE = "https://www.omofuna.com/anime/x/play/1/1.html?sid=1"


@pytest.fixture(autouse=True)
def _tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ep, "_cache_dir", lambda: str(tmp_path))
    # 解析结果缓存是进程级状态：不清会跨用例命中，失败用例就测不到异常路径。
    ep._STREAM_CACHE.clear()
    ep._HOST_DOWN.clear()
    ep._HOST_ROUTE.clear()
    # 测试不依赖本机代理出口
    monkeypatch.setattr(ep.http_mod, "system_proxy_url", lambda: None)
    yield
    ep._STREAM_CACHE.clear()
    ep._HOST_DOWN.clear()
    ep._HOST_ROUTE.clear()


@pytest.mark.skipif(not ep.node_available(), reason="需要 Node.js")
def test_resolve_returns_decrypted_stream_url():
    http = FakeHttp()
    url = ep.ExternalPlayer(http).resolve(CFG, PLAYER, EPISODE)
    assert url == "https://cdn.example.com/ok/index.mp4?mime_type=video_mp4"
    # 播放器请求里的 url 参数只解一层 percent 编码（不能二次编码成 %2566）
    player_call = http.calls[0]
    assert "url=fde8&" in player_call, player_call


def test_from_mismatch_is_ignored():
    http = FakeHttp()
    assert ep.ExternalPlayer(http).resolve(CFG, {"from": "bfzym3u8", "url": "x"}, EPISODE) == ""
    assert http.calls == []


def test_direct_url_passthrough():
    http = FakeHttp()
    player = {"from": "mp4", "url": "https://cdn.example.com/a.m3u8"}
    assert ep.ExternalPlayer(http).resolve(CFG, player, EPISODE) == (
        "https://cdn.example.com/a.m3u8"
    )
    assert http.calls == []


def test_missing_playdata_raises_content_missing():
    http = FakeHttp(page="<html>nothing</html>")
    with pytest.raises(ep.ContentMissingError):
        ep.ExternalPlayer(http).resolve(CFG, PLAYER, EPISODE)


def test_empty_config_returns_empty():
    assert ep.ExternalPlayer(FakeHttp()).resolve({}, PLAYER, EPISODE) == ""


@pytest.mark.skipif(not ep.node_available(), reason="需要 Node.js")
def test_script_warning_still_fails_cleanly(tmp_path):
    http = FakeHttp(scripts={"crypto-js.min.js": "<html>oops</html>",
                             "common.js": "<html>oops</html>"})
    with pytest.raises(ep.ContentMissingError):
        ep.ExternalPlayer(http).resolve(CFG, PLAYER, EPISODE)


def test_omofuna_config_declares_external_player():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "sources", "omofuna.json")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    play_url = data["endpoints"]["content"]["episode"]["play_url"]
    external = play_url["external_player"]
    assert external["from"] == ["mp4"]
    assert "{enc_url}" in external["player_url"]
    assert all(url.startswith("https://") for url in external["scripts"])
    # 解析失败要立刻换线，不能空等渲染
    assert "mp4" in play_url["render_skip_from"]


def test_tiktok_style_mp4_url_is_playable_direct():
    from framework.content.video import VideoMixin

    url = ("https://v16-tiktokcdn-com.akamaized.net/xxx/video/tos/alisg/o8I3/?a=1233"
           "&bt=2074&mime_type=video_mp4&vid=v14033g5")
    assert VideoMixin._is_playable_direct(url) is True
    assert VideoMixin._is_playable_direct("fde8170e5f755156d1133a77b297de5f") is False
