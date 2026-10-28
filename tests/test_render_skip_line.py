# -*- coding: utf-8 -*-
"""线路解析器失效时快速失败并换线（2026-10-29）。

现场：omofuna 精品线路（sid=1，`player_aaaa.from = "mp4"`）只给出一个不透明
id（`26dc1dc6…_2`），真实地址要靠外部解析站 v2player 展开。该解析站现在
返回空壳 + 429，`playwright_fallback` 仍会去渲染 —— 实测 **47s** 才失败，
用户看到的就是「精品源获取不到播放流」，而且整次取流都被这条坏线路拖住。

修复：源用 `content.episode.play_url.render_skip_from` 声明已知失效的线路
类型；命中即不渲染、直接 ContentMissingError，让上层换线路（实测 sid=1
47.7s → 1.52s，最终自动切到可用线路出流）。
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from framework.config import SourceConfig, load_source  # noqa: E402
from framework.content import Content  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

PLAYER = """<script>var player_aaaa={"flag":"play","from":"%s",
"url":"%%32%%36","trysee":0}</script>"""


def _source(payload: dict) -> SourceConfig:
    return SourceConfig.from_dict({
        "$schema_version": 2, "$id": "probe", "$type": "video", "$name": "probe",
        "transports": {"base_url": "https://example.test"},
        **payload,
    })


def _content() -> Content:
    return Content(None, None, None, None)


def test_render_skipped_matches_from():
    c = _content()
    play_cfg = {"render_skip_from": ["mp4"]}
    assert c._render_skipped(None, play_cfg, {}, PLAYER % "mp4") is True
    assert c._render_skipped(None, play_cfg, {}, PLAYER % "bfzym3u8") is False


def test_render_skipped_case_insensitive_and_string_form():
    c = _content()
    assert c._render_skipped(
        None, {"render_skip_from": "MP4"}, {}, PLAYER % "Mp4") is True


def test_render_skipped_false_when_not_configured():
    c = _content()
    assert c._render_skipped(None, {}, {}, PLAYER % "mp4") is False
    assert c._render_skipped(None, {"render_skip_from": []}, {}, PLAYER % "mp4") is False


def test_render_skipped_false_without_player_config():
    c = _content()
    assert c._render_skipped(None, {"render_skip_from": ["mp4"]}, {}, "<html></html>") is False


def test_render_skipped_false_on_broken_json():
    c = _content()
    broken = "<script>var player_aaaa={not json}</script>"
    assert c._render_skipped(None, {"render_skip_from": ["mp4"]}, {}, broken) is False


def test_rendered_fetch_raises_fast_when_line_is_dead(monkeypatch):
    """命中 render_skip_from → 抛 ContentMissingError，且**不**进入 Playwright。"""
    from framework.content.video import ContentMissingError

    c = _content()
    renders = []

    def fake_get_html(source, url, abs_ep, deadline=None, context=None):
        return PLAYER % "mp4"

    monkeypatch.setattr(c, "_get_play_page_html", fake_get_html)
    monkeypatch.setattr("framework.playwright_helper.fetch_rendered_video_sync",
                        lambda *a, **kw: renders.append(1) or "")

    block = {"render_config": {}, "source_switch": {}}
    play_cfg = {"render": "playwright_fallback", "render_skip_from": ["mp4"]}
    source = _source({})

    with pytest.raises(ContentMissingError):
        c._fetch_play_url_rendered(source, "https://e/play/1", "https://e/play/1",
                                   block, play_cfg)
    assert renders == [], "已知失效线路不应该再进渲染"


def test_omofuna_declares_mp4_line_as_dead():
    """真实源配置必须声明 mp4 线路跳过渲染（否则 47s 卡死会回归）。"""
    src = load_source(ROOT / "sources" / "omofuna.json")
    play_cfg = src.raw["endpoints"]["content"]["episode"]["play_url"]
    assert "mp4" in [str(x).lower() for x in play_cfg.get("render_skip_from", [])]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
