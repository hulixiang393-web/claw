# -*- coding: utf-8 -*-
"""「检测播放源」对同源采集源线路的支持（2026-11 洛雪TV）。

现场：聚合站的搜索分组把同一作品聚成「一条采集源一条线路」，每项都带
`detail_url`（换线路＝用该线路自己的 source/id 重取详情）。旧实现的检测按钮
只认「没有 detail_url 的其它源线路」：

    probeable = any(not item.get("detail_url") for item in source_list)

于是这类源上按钮直接隐藏——用户看到的就是「不能检测播放源」。而这类线路恰恰
最需要检测：洛雪TV 的 jl 详情有分集，但分集指向的 CDN 镜像主机
（`jimaoys94.com`）对非浏览器出口统一 403，只看详情会把死线路报成可用。

本文件锁住四件事：
1. 只有 detail_url 线路时，检测按钮可见可用；
2. 候选按「当前线路优先」排列，同 sid 的多条线路不被合并；
3. probe 结果按 detail_url 记账，界面标注（可播放）/（不可用）；
4. 同 sid 但 detail_url 不同的线路能被切换（旧逻辑点了没反应）。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402

from framework.content import Chapter, Detail  # noqa: E402
from framework.reading_progress import ReadingProgress  # noqa: E402
from gui.pages.reader_page import ReaderPage  # noqa: E402

BASE = "https://tv.lxyy.club/watch.php?title=%E7%AC%AC%E5%85%AD%E6%84%9F"

LINES = [
    {"source_id": "mj", "sid": "mj", "name": "4K60帧",
     "detail_url": f"{BASE}&source=mj&id=1"},
    {"source_id": "jl", "sid": "jl", "name": "自营·全22集",
     "detail_url": f"{BASE}&source=jl&id=2"},
    {"source_id": "jl", "sid": "jl", "name": "自营·HD中字",
     "detail_url": f"{BASE}&source=jl&id=3"},
    {"source_id": "ik", "sid": "ik", "name": "ikun·正片",
     "detail_url": f"{BASE}&source=ik&id=4"},
]


def _reader(tmp_path, monkeypatch):
    progress = ReadingProgress(tmp_path / "progress.json")
    manager = SimpleNamespace(get=lambda sid: SimpleNamespace(
        source_id=sid, source_name="洛雪TV", raw={}))
    reader = ReaderPage(manager, SimpleNamespace(), reading_progress=progress)
    reader.set_favorite_checker(lambda url: False)
    return reader


def _view(reader, monkeypatch, lines=None):
    view = reader.video_view
    detail = Detail(
        source_id="lxyy",
        content_type="video",
        title="第六感",
        url=(lines or LINES)[0]["detail_url"],
        chapters=[Chapter("HD中字", "https://cdn.test/a.m3u8")],
        source_list=list(lines or LINES),
    )
    monkeypatch.setattr(view, "_image_loader", None, raising=False)
    view._source = SimpleNamespace(source_id="lxyy", source_name="洛雪TV", raw={})
    view._detail = detail
    view._episodes = detail.chapters
    view._source_probe_settings = {"enabled": True, "max_sources": 12, "timeout_ms": 30000}
    view._populate_source_combo(detail)
    return view, detail


def test_probe_button_visible_for_same_source_lines(_qapp, tmp_path, monkeypatch):
    reader = _reader(tmp_path, monkeypatch)
    view, _ = _view(reader, monkeypatch)
    assert view.probe_btn.isVisible() or view.probe_btn.isVisibleTo(view)
    assert view.probe_btn.isEnabled()


def test_probe_candidates_keep_duplicate_sids_and_put_current_first(
    _qapp, tmp_path, monkeypatch
):
    reader = _reader(tmp_path, monkeypatch)
    view, detail = _view(reader, monkeypatch)
    detail.url = LINES[2]["detail_url"]  # 当前看的是第 2 条 jl 线路
    view._detail = detail
    view._episodes = [Chapter("HD中字", "https://cdn.test/a.m3u8")]
    view._current_idx = 0

    captured = {}
    monkeypatch.setattr(view, "_start_probe_task",
                        lambda config=None, generation=None: captured.update(
                            {"candidates": view._probe_candidates}))
    view._probe_sources()
    candidates = captured["candidates"]
    keys = [c["key"] for c in candidates]
    # 4 条线路都留（两条 jl 的 sid 相同但 key 不同）
    assert len(candidates) == 4
    assert keys[0] == LINES[2]["detail_url"]
    assert len(set(keys)) == 4
    assert all(c.get("mode") == "line" for c in candidates)
    assert all(c.get("episode_title") == "HD中字" for c in candidates)
    # 探测的分集必须与播放口径一致（换线路后按序号取同一集）：只测首集时
    # 「第 3 集」是死的线路也会被标成可播放。
    assert all(c.get("episode_index") == 0 for c in candidates)


def test_probe_result_marks_each_line_by_detail_url(_qapp, tmp_path, monkeypatch):
    reader = _reader(tmp_path, monkeypatch)
    view, _ = _view(reader, monkeypatch)
    view._probe_task = SimpleNamespace(reasons={})
    results = {
        LINES[0]["detail_url"]: "ok",
        LINES[1]["detail_url"]: "failed",
        LINES[2]["detail_url"]: "ok",
    }
    view._probe_task.reasons = {LINES[1]["detail_url"]: "segment_http403"}
    view._on_sources_probed(results, view._probe_generation, view._probe_task)
    texts = [view.source_combo.itemText(i) for i in range(view.source_combo.count())]
    assert texts[0].endswith("（可播放）")
    assert texts[1].endswith("（不可用）")
    assert texts[2].endswith("（可播放）")
    assert texts[3] == "ikun·正片"  # 没被测到的保持原名
    assert "2/3" in view.probe_btn.text()
    # 失败原因进 tooltip：用户能看出「清单能取但分片 403」这类假可用
    assert "segment_http403" in (view.source_combo.itemData(1, Qt.ToolTipRole) or "")


def test_player_error_downgrades_playable_mark(_qapp, tmp_path, monkeypatch):
    """真实播放失败 → 「可播放」改判「不可用」（探测结论错了让现实纠正）。"""
    reader = _reader(tmp_path, monkeypatch)
    view, detail = _view(reader, monkeypatch)
    view._probe_task = object()
    view._on_sources_probed(
        {LINES[0]["detail_url"]: "ok"}, view._probe_generation, view._probe_task
    )
    assert view.source_combo.itemText(0).endswith("（可播放）")
    view._current_sid = "mj"
    monkeypatch.setattr(view, "_sync_overlay_state", lambda *a, **k: None)
    monkeypatch.setattr(view, "_stop_progress_ticker", lambda: None)
    monkeypatch.setattr(view, "_show_status", lambda *a, **k: None)
    view._on_player_error("mpv 无法解码该流")
    assert view.source_combo.itemText(0).endswith("（不可用）")
    assert "mpv" in (view.source_combo.itemData(0, Qt.ToolTipRole) or "")
    assert "全部不可用" in view.probe_btn.text()


def test_switch_between_same_sid_lines(_qapp, tmp_path, monkeypatch):
    reader = _reader(tmp_path, monkeypatch)
    view, detail = _view(reader, monkeypatch)
    emitted = []
    view.source_changed.connect(lambda payload: emitted.append(payload))

    # 选第 3 条（sid 与当前第 2 条相同，仅 detail_url 不同）
    view.source_combo.setCurrentIndex(2)
    assert emitted, "同 sid 的另一条线路必须能切过去"
    _, sid, target_url = emitted[-1]
    assert sid == "jl"
    assert target_url == LINES[2]["detail_url"]


def test_no_switch_when_clicking_current_line(_qapp, tmp_path, monkeypatch):
    reader = _reader(tmp_path, monkeypatch)
    view, detail = _view(reader, monkeypatch)
    view._current_sid = "mj"
    detail.url = LINES[0]["detail_url"]
    emitted = []
    view.source_changed.connect(lambda payload: emitted.append(payload))
    view.source_combo.setCurrentIndex(0)
    assert emitted == []
