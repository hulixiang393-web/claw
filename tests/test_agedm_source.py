import json
from pathlib import Path

import pytest

from framework.config import SourceConfig
from framework.content import Content
from framework.errors import ContentMissingError
from framework.parser import Parser


@pytest.fixture
def source():
    path = Path(__file__).resolve().parents[1] / "sources" / "agedm.json"
    return SourceConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))


@pytest.fixture
def detail_html():
    tabs = "".join(
        f'<button data-bs-target="#playlist-source-{name}">{name}</button>'
        for name in ("xigua", "bfzym3u8", "hnm3u8")
    )
    panels = "".join(
        f'<div class="tab-pane" id="playlist-source-{name}">'
        + "".join(
            f'<a href="/play/20190042/{sid}/{ep}">第{ep:02d}集</a>'
            for ep in (1, 2)
        ) + "</div>"
        for sid, name in enumerate(("xigua", "bfzym3u8", "hnm3u8"), 1)
    )
    return f'<div class="video_detail_playlist_wrapper">{tabs}<div class="tab-content">{panels}</div></div>'


@pytest.mark.parametrize("sid", ["1", "2", "3"])
def test_chapters_keep_selected_line_and_episode(source, detail_html, sid):
    content = Content(None, Parser(), None)
    chapters = content._fetch_chapters(
        source, content._parser.parse(detail_html),
        detail_url=f"https://www.agedm.io/detail/20190042?sid={sid}",
    )
    assert [(ch.title, ch.url, ch.source_id) for ch in chapters] == [
        (f"第{ep:02d}集", f"https://www.agedm.io/play/20190042/{sid}/{ep}", sid)
        for ep in (1, 2)
    ]
    assert [s["sid"] for s in content._parse_source_list(source, detail_html)] == ["1", "2", "3"]


@pytest.mark.parametrize("locked", [False, True])
def test_age_failure_never_automatically_changes_line(source, monkeypatch, locked):
    content = Content(None, Parser(), None)
    attempts = []

    def fail(source, url, **kwargs):
        attempts.append(url)
        raise ContentMissingError("unavailable")

    monkeypatch.setattr(content, "_fetch_play_url_once", fail)
    monkeypatch.setattr(content, "_fetch_play_url_auto_switch", lambda *a, **k: pytest.fail("automatic switch"))
    with pytest.raises(ContentMissingError):
        content.fetch_video_episode(source, "/play/20190042/2/1", source_lock=locked)
    assert attempts == ["/play/20190042/2/1"]


@pytest.mark.parametrize("html", ["<html></html>", '<iframe id="iframeForVideo" src="javascript:alert(1)"></iframe>'])
def test_age_missing_or_invalid_iframe_never_opens_browser(source, monkeypatch, html):
    content = Content(None, Parser(), None)
    monkeypatch.setattr(content, "_get", lambda *a, **k: html)
    monkeypatch.setattr("framework.playwright_helper.fetch_rendered_video_sync", lambda *a, **k: pytest.fail("invalid iframe"))
    with pytest.raises(ContentMissingError):
        content.fetch_video_episode(source, "/play/20190042/2/1", source_lock=True)


def test_age_iframe_fetch_cancellation_never_starts_render(source, monkeypatch):
    from framework.stream_resolver import StreamBudget, StreamCancelledError

    content = Content(None, Parser(), None)
    context = StreamBudget()

    def fetch(*args, context=None, **kwargs):
        context.cancel()
        context.check()

    monkeypatch.setattr(content, "_get", fetch)
    monkeypatch.setattr("framework.playwright_helper.fetch_rendered_video_sync", lambda *a, **k: pytest.fail("cancelled iframe"))
    with pytest.raises(StreamCancelledError):
        content.fetch_video_streams(source, "/play/20190042/2/1", context=context, source_lock=True)


@pytest.mark.parametrize("source_id,opt_in", [("agedm", True), ("agedm", False), ("demo", True), ("demo", False)])
def test_render_recovery_is_age_opt_in(source, monkeypatch, source_id, opt_in):
    source.source_id = source_id
    rc = source.raw["endpoints"]["content"]["episode"]["render_config"]
    rc["recover_navigation_timeout"] = opt_in
    content = Content(None, Parser(), None)
    calls = []
    monkeypatch.setattr(content, "_get", lambda *a, **k: '<iframe id="iframeForVideo" src="https://player.example/parse"></iframe>')
    monkeypatch.setattr("framework.playwright_helper.fetch_rendered_video_sync",
                        lambda *a, **k: calls.append(k) or "https://cdn/video.m3u8")
    assert content.fetch_video_episode(source, "/play/20190042/2/1", source_lock=True) == "https://cdn/video.m3u8"
    assert len(calls) == 1
    if source_id == "agedm" and opt_in:
        assert calls[0]["recover_navigation_timeout"] is True
    else:
        assert "recover_navigation_timeout" not in calls[0]


def test_age_config_opts_into_pipeline_budget(source):
    candidate, = Content(None, Parser(), None).resolve_video_stream_candidates(source, "/play/20190042/2/1")
    assert source.source_id == "agedm"
    assert candidate["budget"] == "pipeline"
    assert callable(candidate["resolver"])


def test_age_render_uses_bounded_commit_wait(source, monkeypatch):
    content = Content(None, Parser(), None)
    calls = []

    def render(url, **kwargs):
        calls.append((url, kwargs))
        return "https://cdn/video.m3u8"

    monkeypatch.setattr(content, "_get", lambda *a, **k: '<iframe id="iframeForVideo" src="https://player.example/parse"></iframe>')
    monkeypatch.setattr("framework.playwright_helper.fetch_rendered_video_sync", render)
    assert content.fetch_video_episode(source, "/play/20190042/2/1", source_lock=True) == "https://cdn/video.m3u8"
    render_url, options = calls[0]
    assert render_url == "https://player.example/parse"
    assert options["wait_until"] == "commit"
    assert options["timeout_ms"] == 10000
    assert options["extra_delay_ms"] == 12000
    assert options["slow_fallback_rounds"] == 0
    assert options["recover_navigation_timeout"] is True
