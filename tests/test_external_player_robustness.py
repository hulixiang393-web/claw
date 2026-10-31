# -*- coding: utf-8 -*-
"""external_player 的稳健性开关：缓存 / 重试次数 / 出口偏好 / 超时。

真实环境（art.v2player.top:8989）实测：同一个播放页 6~11s 才吐完、
TLS 握手时不时被重置；6s 上限 → 解析稳定失败。这些开关就是为了
「一次成功后复用、失败尽快重试、别把请求塞进不通的代理出口」。
"""
import pytest

from framework import external_player as ep

CFG = {
    "from": ["mp4"],
    "player_url": "https://player_host/player/?url={enc_url}&sid={sid}&nid={nid}",
    "cipher_regex": r"playData\('([^']+)','([^']+)'\)",
    "scripts": [
        "https://cdn.example.com/js/crypto-js.min.js",
        "https://cdn.example.com/js/common.js",
    ],
    "cache_ttl_s": 900,
}
PLAYER = {"from": "mp4", "url": "%66%64%65%38", "sid": 1, "nid": 1}
EPISODE = "https://www.omofuna.com/anime/x/play/1/1.html?sid=1"


class FakeHttp:
    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.calls = []
        self.scripts = {
            "crypto-js.min.js": "var CryptoJS = {};",
            "common.js": (
                "function playData(cipher, key) {"
                "  return 'https://cdn.example.com/ok/index.mp4?mime_type=video_mp4';"
                "}"
            ),
        }

    def get_text(self, url, headers=None, **kwargs):
        self.calls.append(url)
        if "player_host" in url:
            if self.fail_times > 0:
                self.fail_times -= 1
                raise RuntimeError("TLS handshake reset")
            return "<script>var u=playData('AAA','BBB');</script>"
        name = url.rsplit("/", 1)[-1]
        if name in self.scripts:
            return self.scripts[name]
        raise AssertionError(f"unexpected url: {url}")

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    ep._STREAM_CACHE.clear()
    monkeypatch.setattr(ep, "_cache_dir", lambda: str(tmp_path))
    yield
    ep._STREAM_CACHE.clear()


@pytest.mark.skipif(not ep.node_available(), reason="需要 Node.js")
def test_second_resolve_hits_cache():
    http = FakeHttp()
    first = ep.ExternalPlayer(http).resolve(dict(CFG), PLAYER, EPISODE)
    calls_after_first = len(http.calls)
    second = ep.ExternalPlayer(http).resolve(dict(CFG), PLAYER, EPISODE)
    assert first == second == "https://cdn.example.com/ok/index.mp4?mime_type=video_mp4"
    assert len(http.calls) == calls_after_first, "缓存命中时不应再请求播放页"


@pytest.mark.skipif(not ep.node_available(), reason="需要 Node.js")
def test_cache_ttl_zero_disables_cache():
    http = FakeHttp()
    cfg = dict(CFG, cache_ttl_s=0)
    ep.ExternalPlayer(http).resolve(cfg, PLAYER, EPISODE)
    calls = len(http.calls)
    ep.ExternalPlayer(http).resolve(cfg, PLAYER, EPISODE)
    assert len(http.calls) > calls


@pytest.mark.skipif(not ep.node_available(), reason="需要 Node.js")
def test_attempts_retries_transient_page_failure():
    http = FakeHttp(fail_times=1)
    cfg = dict(CFG, attempts=2)
    url = ep.ExternalPlayer(http).resolve(cfg, PLAYER, EPISODE)
    assert url.startswith("https://cdn.example.com/ok/index.mp4")
    assert sum(1 for call in http.calls if "player_host" in call) == 2


def test_attempts_exhausted_raises_content_missing():
    http = FakeHttp(fail_times=3)
    cfg = dict(CFG, attempts=2)
    with pytest.raises(ep.ContentMissingError) as excinfo:
        ep.ExternalPlayer(http).resolve(cfg, PLAYER, EPISODE)
    assert "页面获取失败" in str(excinfo.value)


@pytest.mark.skipif(not ep.node_available(), reason="需要 Node.js")
def test_http_timeout_and_budget_come_from_source_config():
    player = ep.ExternalPlayer(FakeHttp())
    player.resolve(dict(CFG, http_timeout_s=10, budget_s=16, attempts=1), PLAYER, EPISODE)
    assert player._http_timeout == 10.0
    assert player._budget == 16.0


def test_prefer_direct_marks_parser_host(monkeypatch):
    seen = []

    class _Note:
        @staticmethod
        def note_direct_preferred(url):
            seen.append(url)

    import framework.http as http_mod

    monkeypatch.setattr(http_mod, "note_direct_preferred", _Note.note_direct_preferred)
    http = FakeHttp()
    try:
        ep.ExternalPlayer(http).resolve(dict(CFG, prefer_direct=True), PLAYER, EPISODE)
    except ep.ContentMissingError:
        pass
    assert any("player_host" in url for url in seen), seen
