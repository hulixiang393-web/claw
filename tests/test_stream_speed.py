# -*- coding: utf-8 -*-
"""取流提速回归（2026-10-28）。

三处改动都必须可验证、且不能改变原有语义：
1. 「纯 URL 解码」JS 走 Python 快路径（omofuna 实测省 1.26s/集）；
   其它任何脚本形状仍走真实 JS 执行。
2. 播放页 HTML 短 TTL 缓存 + 同 URL 并发合并（点集与提前预取撞同一集只发一次）。
3. 换线路候选并行取（默认线路失败时不再串行 3~6s）。
"""
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from framework.config import SourceConfig  # noqa: E402
from framework.decrypter import Decrypter  # noqa: E402


# ---------------------------------------------------------------------- #
# 1. URL 解码快路径
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("script,expected", [
    ("function decrypt(d) { return decodeURIComponent(d); }", True),
    ("function decrypt(d){return decodeURIComponent(d)}", True),
    ("d => decodeURIComponent(d)", True),
    ("(d) => decodeURIComponent(d);", True),
    ("var f = d => unescape(d)", True),
    # 非等价形状：必须走真实 JS
    ("function decrypt(d) { return decodeURIComponent(atob(d)); }", False),
    ("function decrypt(d) { return decodeURIComponent(d) + 'x'; }", False),
    ("function decrypt(d) { var x = d.split(''); return x.join(''); }", False),
    ("", False),
])
def test_unquote_only_script_detection(script, expected):
    assert Decrypter.unquote_only_script(script) is expected


def test_js_custom_unquote_fast_path_matches_js(monkeypatch):
    """快路径结果必须与 decodeURIComponent 一致，且不再调用 Playwright。"""
    calls = []
    monkeypatch.setattr("framework.decrypter._evaluate_js",
                        lambda *a, **kw: calls.append(a) or "WRONG")

    cfg = {"script": "function decrypt(d) { return decodeURIComponent(d); }"}
    raw = "%68%74%74%70%73%3A%2F%2Fvip.dytt-hot.com%2Fa%2Findex.m3u8%3Fx%3D1"
    out = Decrypter(None)._js_custom(cfg, raw)

    assert out == "https://vip.dytt-hot.com/a/index.m3u8?x=1"
    assert calls == [], "纯 URL 解码不应再走 Playwright"


def test_js_custom_falls_back_to_js_for_complex_script(monkeypatch):
    calls = []
    monkeypatch.setattr("framework.decrypter._evaluate_js",
                        lambda *a, **kw: calls.append(a) or "from-js")

    cfg = {"script": "function decrypt(d) { return decodeURIComponent(atob(d)); }"}
    out = Decrypter(None)._js_custom(cfg, "abc")
    assert out == "from-js"
    assert len(calls) == 1


def test_omofuna_source_script_hits_fast_path():
    """真实源配置的解密脚本必须命中快路径（否则等于没优化）。"""
    from framework.config import load_source

    root = Path(__file__).resolve().parents[1]
    src = load_source(root / "sources" / "omofuna.json")
    script = (((src.raw.get("decryption") or {}).get("targets") or {})
              .get("video_url") or {}).get("script")
    assert script, "omofuna 缺少 video_url 解密脚本"
    assert Decrypter.unquote_only_script(script), script


# ---------------------------------------------------------------------- #
# 2. 播放页 HTML 缓存 / 并发合并
# ---------------------------------------------------------------------- #
class _CountingContent:
    """只替换 _get 的最小 Content，统计真实抓取次数。"""

    def __getattr__(self, name):
        raise AttributeError(name)


def _make_video_content(fetch_delay=0.05):
    from framework.content import Content

    class Probe(Content):
        def __init__(self):
            self.fetches = []
            self.lock = threading.Lock()

        def _get(self, source, url, deadline=None, context=None):
            with self.lock:
                self.fetches.append(url)
            time.sleep(fetch_delay)
            return f"<html data-url='{url}'></html>"

    return Probe()


def _source():
    return SourceConfig.from_dict({
        "$schema_version": 2, "$id": "probe", "$type": "video", "$name": "probe",
        "transports": {"base_url": "https://example.test"},
    })


def test_play_page_html_cached_within_ttl():
    content = _make_video_content()
    source = _source()
    url = "https://example.test/play/1"
    first = content._get_play_page_html(source, url, url)
    second = content._get_play_page_html(source, url, url)
    assert first == second
    assert content.fetches == [url], "TTL 内应只抓一次"


def test_play_page_html_expires_after_ttl(monkeypatch):
    content = _make_video_content()
    source = _source()
    url = "https://example.test/play/2"
    monkeypatch.setattr(content, "_PLAY_PAGE_TTL", 0.01)
    content._get_play_page_html(source, url, url)
    time.sleep(0.05)
    content._get_play_page_html(source, url, url)
    assert len(content.fetches) == 2


def test_play_page_concurrent_callers_share_one_fetch():
    """点集与提前预取撞同一集：只发一次 HTTP。"""
    content = _make_video_content(fetch_delay=0.15)
    source = _source()
    url = "https://example.test/play/3"
    results = []

    def call():
        results.append(content._get_play_page_html(source, url, url))

    threads = [threading.Thread(target=call) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 4
    assert len(set(results)) == 1
    assert len(content.fetches) == 1


# ---------------------------------------------------------------------- #
# 3. 换线路候选并行
# ---------------------------------------------------------------------- #
def test_switch_candidates_fetched_in_parallel():
    from framework.content import Content

    class Probe(Content):
        def __init__(self):
            pass

        def switch_source(self, source, detail_url, sid, deadline=None, context=None):
            time.sleep(0.2)
            return (None, [SimpleNamespace(url=f"https://cdn/{sid}/ep1"),
                           SimpleNamespace(url=f"https://cdn/{sid}/ep2")])

    content = Probe()
    source = SourceConfig.from_dict({
        "$schema_version": 2, "$id": "p", "$type": "video", "$name": "p",
        "transports": {"base_url": "https://example.test"},
    })
    t0 = time.monotonic()
    urls = content._switch_candidate_episode_urls_parallel(
        source, "https://example.test/detail", 1, "1", ["1", "2", "3", "4"],
        max_parallel=3)
    elapsed = time.monotonic() - t0

    assert urls == ["https://cdn/2/ep2", "https://cdn/3/ep2", "https://cdn/4/ep2"]
    # 3 条线路各 0.2s：并行应明显小于串行的 0.6s
    assert elapsed < 0.55, f"候选仍是串行：{elapsed:.2f}s"


def test_switch_candidates_skip_current_and_failed_sids():
    from framework.content import Content

    class Probe(Content):
        def __init__(self):
            pass

        def switch_source(self, source, detail_url, sid, deadline=None, context=None):
            if sid == "3":
                raise RuntimeError("line down")
            return (None, [SimpleNamespace(url=f"https://cdn/{sid}/ep1")])

    content = Probe()
    source = SourceConfig.from_dict({
        "$schema_version": 2, "$id": "p", "$type": "video", "$name": "p",
        "transports": {"base_url": "https://example.test"},
    })
    urls = content._switch_candidate_episode_urls_parallel(
        source, "https://example.test/detail", 0, "1", ["1", "2", "3"], max_parallel=3)
    assert urls == ["https://cdn/2/ep1"]


# ---------------------------------------------------------------------- #
# 4. 渲染结果缓存（Playwright 单次要 8~15s，不能重来一遍）
# ---------------------------------------------------------------------- #
def _video_content():
    from framework.content import Content

    return Content(None, None, None, None)


def _probe_source():
    return SourceConfig.from_dict({
        "$schema_version": 2, "$id": "probe", "$type": "video", "$name": "probe",
        "transports": {"base_url": "https://example.test"},
    })


def test_rendered_play_cache_roundtrip():
    content = _video_content()
    source = _probe_source()
    content._rendered_play_cache_set(source, "https://e/1", "https://cdn/x.m3u8", {})
    assert content._rendered_play_cache_get(source, "https://e/1") == "https://cdn/x.m3u8"
    # 未缓存的集返回空（调用方据此走现场渲染）
    assert content._rendered_play_cache_get(source, "https://e/2") == ""


def test_rendered_play_cache_respects_signed_expiry():
    """签名快过期的地址不缓存，否则播放时会 403。"""
    import time as _t

    content = _video_content()
    source = _probe_source()
    soon = int(_t.time()) + 10
    content._rendered_play_cache_set(
        source, "https://e/2", f"https://cdn/x?auth_key={soon}-a-0-b", {})
    assert content._rendered_play_cache_get(source, "https://e/2") == ""

    later = int(_t.time()) + 3600
    content._rendered_play_cache_set(
        source, "https://e/3", f"https://cdn/y?auth_key={later}-a-0-b", {})
    assert content._rendered_play_cache_get(source, "https://e/3") != ""


def test_rendered_play_cache_expires():
    content = _video_content()
    source = _probe_source()
    content._rendered_play_cache_set(source, "https://e/4", "https://cdn/z", {"cache_ttl_s": 0.01})
    import time as _t

    _t.sleep(0.05)
    assert content._rendered_play_cache_get(source, "https://e/4") == ""


def test_render_uses_play_page_cache_instead_of_refetching():
    """转渲染兜底时不能为此再抓一遍播放页（omofuna 后半段集数每集多花 1~3s）。"""
    from framework.content import Content

    class Probe(Content):
        def __init__(self):
            self.fetches = []

        def _get(self, source, url, deadline=None, context=None):
            self.fetches.append(url)
            return "<html></html>"

    content = Probe()
    source = _probe_source()
    url = "https://e/play/1"
    content._get_play_page_html(source, url, url)          # 普通解析先抓
    content._get_play_page_html(source, url, url)          # 渲染兜底复用
    assert content.fetches == [url]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
