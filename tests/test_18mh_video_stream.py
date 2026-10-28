# -*- coding: utf-8 -*-
"""18mh 视频取流回归：签名包装地址必须整串使用且落在站点代理上。

2026-10 站点换版后播放器只给 `/media/m3u8?url=<encoded m3u8>&exp=<ts>&token=<sig>`
这类包装地址，实测：
- 丢掉 `&exp=`/`&token=`（只取 url= 里的裸 m3u8）→ 站点代理 403；
- 同一路径换到 CDN 域 `xv.dzuxta.cn/media/m3u8` → 403 / 挂起；
- 页面里还有拼接坏的一条 `…&token=…?auth_key=…`（第二个 `?` 后整段成了
  token 值），这条同样 403。

所以候选表必须：整串带签名 → 落在站点域 → 把接错的第二段查归并成合法参数。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.content import Content  # noqa: E402
from framework.content.video import VideoMixin  # noqa: E402
from framework.parser import Parser  # noqa: E402

EP = "https://18mh.net/mv/detail/70353"
INNER_M3U8 = (
    "https://xv.dzuxta.cn/videos5/7512f6276f2611711173c019a6a412f9/"
    "7512f6276f2611711173c019a6a412f9.m3u8?v=3&time=0&via=jmtt&via_bm=dx"
)
ENCODED_INNER = (
    "https%3A%2F%2Fxv.dzuxta.cn%2Fvideos5%2F7512f6276f2611711173c019a6a412f9%2F"
    "7512f6276f2611711173c019a6a412f9.m3u8%3Fv%3D3%26time%3D0%26via%3Djmtt%26via_bm%3Ddx"
)
SIGNED_TAIL = "exp=1793209866&token=2072b4e7a803917a232984b2f591d8f060d287ca10451222db9270885db01186"


def _content() -> Content:
    return Content(http=None, parser=Parser(), checker=None)


def _page(relative_clean: bool = False, absolute_cdn: bool = True) -> str:
    """构造与站点同构的详情页 HTML（JS 转义形态）。"""
    rel_clean = "\\/media\\/m3u8?url=" + ENCODED_INNER + "&" + SIGNED_TAIL
    rel_broken = rel_clean + "?auth_key=1793209866-0-0-ad4980b539d12ddd09e54eb052defe6b&via_m=jmtt"
    cdn = "https:\\/\\/xv.dzuxta.cn\\/media\\/m3u8?url=" + ENCODED_INNER
    parts = ['<script>window.__INITIAL_STATE__={"videoDetail":{"url":"%s"' % rel_clean]
    parts.append(',"view_url":"%s"' % rel_broken)
    if absolute_cdn:
        parts.append(',"backend_video":"%s"' % cdn)
    parts.append("}};</script>")
    return "".join(parts)


def _candidates(html: str) -> list[str]:
    cont = _content()
    return cont._extract_play_url_candidates(html, None, {}, {}, EP, EP)


def test_signed_site_proxy_candidate_comes_first():
    cands = _candidates(_page())
    assert cands, "应解析出候选"
    first = cands[0]
    assert first.startswith("https://18mh.net/media/m3u8?url="), first
    assert "exp=" in first and "token=" in first, "签名参数不能丢"


def test_cdn_proxy_is_rewritten_to_site_host():
    cands = _candidates(_page())
    cdn_wrapped = [c for c in cands if "/media/m3u8?" in c]
    assert cdn_wrapped, "包装地址应被收录"
    assert all("xv.dzuxta.cn/media/m3u8" not in c for c in cdn_wrapped)


def test_broken_second_query_string_is_merged():
    cands = _candidates(_page())
    merged = [c for c in cands if "token=" in c and "auth_key=" in c]
    assert merged, "拼接坏掉的包装地址应被修复后收录"
    for url in merged:
        # 第二个 '?' 之前应已改成 '&'：整串只保留开头一个查询分隔符
        assert url.count("?") == 1, url


def test_bare_m3u8_still_present_as_fallback():
    cands = _candidates(_page())
    bare = [c for c in cands if c.startswith("https://xv.dzuxta.cn/videos5/")]
    assert bare, "裸 m3u8 仍作为后备候选保留"
    assert INNER_M3U8.split("?")[0] in bare[0]


def test_relative_wrapper_is_absolutised_against_episode_host():
    html = _page(absolute_cdn=False)
    cands = _candidates(html)
    assert cands[0].startswith("https://18mh.net/media/m3u8?url=")


def test_is_playable_direct_accepts_signed_proxy():
    """包装地址虽不以 .m3u8 结尾，但路径含 m3u8 → 视为可直接播放。"""
    url = _candidates(_page())[0]
    assert VideoMixin._is_playable_direct(url) is True


def test_source_declares_hls_prefetch_tuning():
    """18mh 分片约 2MB、上游偶发数秒抖动，源级预取必须显式开够深度。

    实测：默认 4 片/2 路时连续播放 3.5MB/s、单片最慢 4.7s（明显卡顿）；
    深度 12/4 路后 13.7MB/s、最慢 0.6s。
    """
    import json

    cfg = json.loads(
        (Path(__file__).resolve().parents[1] / "sources" / "18mh-video.json").read_text(
            encoding="utf-8"
        )
    )
    hls = (cfg.get("media") or {}).get("hls") or {}
    assert (cfg.get("media") or {}).get("format") == "hls"
    prefetch = hls.get("prefetch") or {}
    assert prefetch.get("enabled") is True
    assert int(prefetch.get("depth") or 0) >= 10
    assert int(prefetch.get("workers") or 0) >= 3


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
