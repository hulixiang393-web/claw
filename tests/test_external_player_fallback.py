# -*- coding: utf-8 -*-
"""external_player 的「解析站挂了怎么办」：落盘兜底、出口切换、主机冷却。

现场（2026-11-02，omofuna 精品线 sid=1）：解析站 `art.v2player.top:8989` 与站点
主域同一台机器，直连 TCP 超时、经系统代理 502/RemoteDisconnected，而前一天同一
域名直连 200。旧实现有三个后果：

1. 报错被截断成「请求失败 GET <很长 URL>」，**看不出原因**；
2. `prefer_direct` 把主机钉死在直连出口，另一个出口（代理）永远不会再试；
3. 解析结果只存在内存里，进程重启后前台缓存全丢 → 解析站一挂，精品线整条不可用，
   哪怕一小时前刚成功解析出可播的 CDN 地址。

这里逐条钉住修好的行为。
"""
import json
import time

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
    "stale_ttl_s": 21600,
    "attempts": 1,
}
PLAYER = {"from": "mp4", "url": "%66%64%65%38", "sid": 1, "nid": 1}
EPISODE = "https://www.omofuna.com/anime/x/play/1/1.html?sid=1"
PLAY_URL = "https://player_host/player/?url=fde8&sid=1&nid=1"
STREAM = "https://cdn.example.com/ok/index.mp4?mime_type=video_mp4"

PAGE = "<script>var u=playData('AAA','BBB');</script>"
SCRIPTS = {
    "crypto-js.min.js": "var CryptoJS = {};",
    "common.js": (
        "function playData(cipher, key) {"
        "  return 'https://cdn.example.com/ok/index.mp4?mime_type=video_mp4';"
        "}"
    ),
}


class FakeHttp:
    """按需要「页面取不到 / 字节探测失败」，并记录每次请求的出口参数。"""

    def __init__(self, page=PAGE, page_fails=False, bytes_ok=True, fail_first_page_only=False):
        self.page = page
        self.page_fails = page_fails
        self.bytes_ok = bytes_ok
        self.fail_first_page_only = fail_first_page_only
        self.calls = []
        self.page_attempts = []

    def _is_page(self, url):
        return "player_host" in url

    def get_text(self, url, headers=None, **kwargs):
        self.calls.append((url, dict(kwargs)))
        if self._is_page(url):
            self.page_attempts.append(dict(kwargs))
            fail = self.page_fails
            if self.fail_first_page_only:
                fail = len(self.page_attempts) == 1
            if fail:
                raise RuntimeError("Connection reset by peer")
            return self.page
        name = url.rsplit("/", 1)[-1]
        if name in SCRIPTS:
            return SCRIPTS[name]
        raise AssertionError("unexpected url: %s" % url)

    def get_bytes(self, url, headers=None, **kwargs):
        self.calls.append((url, dict(kwargs)))
        if not self.bytes_ok:
            raise RuntimeError("403 Forbidden")
        return b"ID3\x04\x00"

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    ep._STREAM_CACHE.clear()
    ep._HOST_DOWN.clear()
    ep._HOST_ROUTE.clear()
    monkeypatch.setattr(ep, "_cache_dir", lambda: str(tmp_path))
    monkeypatch.setattr(ep.http_mod, "system_proxy_url", lambda: None)
    yield
    ep._STREAM_CACHE.clear()
    ep._HOST_DOWN.clear()
    ep._HOST_ROUTE.clear()


def _write_disk(tmp_path, url, age_s):
    """写一条「已经解析过、但已超出新鲜期」的落盘记录。"""
    data = {PLAY_URL: {"url": url, "ts": time.time() - age_s}}
    (tmp_path / ep._DISK_CACHE_NAME).write_text(
        json.dumps(data), encoding="utf-8")


def test_transport_failure_message_keeps_the_cause():
    """报错必须能看出原因（旧实现按 90 字截断，只剩半条 URL）。"""
    http = FakeHttp(page_fails=True)
    with pytest.raises(ep.ContentMissingError) as excinfo:
        ep.ExternalPlayer(http).resolve(dict(CFG), PLAYER, EPISODE)
    text = str(excinfo.value)
    assert "页面获取失败" in text
    assert "RuntimeError" in text and "Connection reset by peer" in text
    assert len(text) < 400, text


def test_transport_failure_puts_host_in_cooldown_then_fails_fast():
    """连不上过一次就短期冷却：下一集不再为一个挂掉的解析站白等。"""
    http = FakeHttp(page_fails=True)
    with pytest.raises(ep.ContentMissingError):
        ep.ExternalPlayer(http).resolve(dict(CFG), PLAYER, EPISODE)
    attempts = len(http.page_attempts)
    assert attempts >= 1

    with pytest.raises(ep.ContentMissingError) as excinfo:
        ep.ExternalPlayer(http).resolve(dict(CFG), PLAYER, EPISODE)
    assert "冷却" in str(excinfo.value)
    assert len(http.page_attempts) == attempts, "冷却期内不得再请求播放页"


def test_structure_change_does_not_cool_the_host_down():
    """页面能取到但解析不出密文 → 是结构问题，不许进冷却（否则永远看不到报错）。"""
    http = FakeHttp(page="<html>nothing</html>")
    with pytest.raises(ep.ContentMissingError) as excinfo:
        ep.ExternalPlayer(http).resolve(dict(CFG), PLAYER, EPISODE)
    assert "playData" in str(excinfo.value)
    assert ep._HOST_DOWN == {}

    with pytest.raises(ep.ContentMissingError):
        ep.ExternalPlayer(http).resolve(dict(CFG), PLAYER, EPISODE)
    assert len(http.page_attempts) >= 2


@pytest.mark.skipif(not ep.node_available(), reason="需要 Node.js")
def test_falls_back_to_the_other_exit(monkeypatch):
    """直连失败 → 立刻换代理出口再试一次（旧实现声明 prefer_direct 后就只走直连）。"""
    monkeypatch.setattr(ep.http_mod, "system_proxy_url", lambda: "http://127.0.0.1:65532")
    http = FakeHttp(fail_first_page_only=True)
    url = ep.ExternalPlayer(http).resolve(dict(CFG, prefer_direct=True), PLAYER, EPISODE)
    assert url == STREAM
    modes = [("proxy" in attempt) for attempt in http.page_attempts]
    assert modes == [False, True], http.page_attempts
    assert ep._HOST_ROUTE.get("player_host") == "proxy"


@pytest.mark.skipif(not ep.node_available(), reason="需要 Node.js")
def test_success_is_written_to_disk_and_survives_restart(tmp_path):
    http = FakeHttp()
    assert ep.ExternalPlayer(http).resolve(dict(CFG), PLAYER, EPISODE) == STREAM
    data = json.loads((tmp_path / ep._DISK_CACHE_NAME).read_text(encoding="utf-8"))
    assert data[PLAY_URL]["url"] == STREAM

    # 模拟重启：内存缓存清空、解析站同时挂掉 → 仍然能拿到上次的直链
    ep._STREAM_CACHE.clear()
    dead = FakeHttp(page_fails=True)
    assert ep.ExternalPlayer(dead).resolve(dict(CFG), PLAYER, EPISODE) == STREAM
    assert dead.page_attempts == [], "新鲜期内的落盘记录不该再打解析站"


def test_stale_link_rescues_when_parser_host_is_down(tmp_path):
    """超出新鲜期但仍在兜底窗口内 → 解析站挂了也能续播（先确认还能取到字节）。"""
    _write_disk(tmp_path, STREAM, 1800)
    http = FakeHttp(page_fails=True)
    assert ep.ExternalPlayer(http).resolve(dict(CFG), PLAYER, EPISODE) == STREAM
    assert any(url == STREAM for url, _ in http.calls), "兜底前必须先确认直链可用"


def test_stale_link_is_dropped_when_no_longer_playable(tmp_path):
    _write_disk(tmp_path, STREAM, 1800)
    http = FakeHttp(page_fails=True, bytes_ok=False)
    with pytest.raises(ep.ContentMissingError):
        ep.ExternalPlayer(http).resolve(dict(CFG), PLAYER, EPISODE)


def test_stale_link_outside_window_is_not_used(tmp_path):
    _write_disk(tmp_path, STREAM, 30000)   # 超过 stale_ttl_s(21600)
    http = FakeHttp(page_fails=True)
    with pytest.raises(ep.ContentMissingError):
        ep.ExternalPlayer(http).resolve(dict(CFG), PLAYER, EPISODE)
