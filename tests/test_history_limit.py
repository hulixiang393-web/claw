# -*- coding: utf-8 -*-
"""播放历史保留上限：书架收藏永久保留且不占名额，其余最多 max_items 部。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework.reading_progress import ReadingProgress
from framework.settings_manager import SettingsManager, history_max_items


def _progress(tmp_path, max_items, shelf=()):
    return ReadingProgress(
        tmp_path / "progress.json",
        shelf_cb=lambda url: url in set(shelf),
        max_items=max_items,
    )


def test_cap_keeps_newest_non_shelf_records(tmp_path):
    progress = _progress(tmp_path, 3)
    for index in range(5):
        progress.save("demo", f"book{index}", "video", f"ep{index}",
                      f"第{index}集", title=f"作品{index}")
    assert set(progress.all()) == {"book2", "book3", "book4"}


def test_shelf_record_is_kept_and_does_not_take_a_slot(tmp_path):
    progress = _progress(tmp_path, 2, shelf={"fav"})
    progress.save("demo", "fav", "novel", "c1", "第一章", title="收藏")
    for index in range(4):
        progress.save("demo", f"book{index}", "video", f"ep{index}",
                      f"第{index}集", title=f"作品{index}")
    data = progress.all()
    assert "fav" in data
    assert {url for url in data if url != "fav"} == {"book2", "book3"}


def test_set_max_items_trim_immediately(tmp_path):
    progress = _progress(tmp_path, 20)
    for index in range(6):
        progress.save("demo", f"book{index}", "video", f"ep{index}",
                      f"第{index}集")
    progress.set_max_items(1)
    assert set(progress.all()) == {"book5"}
    progress.set_max_items(0)  # 0 = 不限制
    progress.save("demo", "bookX", "video", "epX", "第一集")
    assert len(progress.all()) == 2


def test_settings_helper_default_and_clamp(tmp_path):
    settings = SettingsManager(tmp_path / "app_config.json")
    assert history_max_items(settings) == 20
    settings.set("history", "max_items", 9999)
    assert history_max_items(settings) == 500
    settings.set("history", "max_items", -3)
    assert history_max_items(settings) == 0
