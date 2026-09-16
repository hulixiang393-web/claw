# -*- coding: utf-8 -*-
"""背景图合成 + 透明度语义测试（gui/app.py）。

覆盖：
- _bg_opacity：用户设 0.0 不被 `or` 篡改；None/异常回退 0.6；正常值原样。
- 后台合成：_bg_image_qss_block 主线程立即返回；后台线程合成后经信号注入
  _central_area；缓存 key 随透明度更新。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from PySide6.QtGui import QImage, QColor
from PySide6.QtWidgets import QApplication

from gui.app import MainWindow, _app_base_dir
from framework.settings_manager import SettingsManager
from framework.theme_manager import ThemeManager


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _Area:
    def __init__(self) -> None:
        self._bg_pixmap = None

    def set_bg_pixmap(self, pixmap) -> None:
        self._bg_pixmap = pixmap


class _Fake:
    """模拟 MainWindow 最小切片（后台合成/透明度链路）。"""

    _bg_opacity = MainWindow._bg_opacity
    _bg_compose_async = MainWindow._bg_compose_async
    _on_bg_composed = MainWindow._on_bg_composed
    _load_bg_pixmap = MainWindow._load_bg_pixmap

    def __init__(self, settings) -> None:
        self.settings = settings
        self.theme_manager = ThemeManager(settings)
        self._central_area = _Area()

    def width(self) -> int:
        return 1100

    def height(self) -> int:
        return 760

    def _bg_overlay_qss(self) -> str:
        return "/* overlay */"

    def _clear_bg_pixmap(self) -> None:
        try:
            self._central_area.set_bg_pixmap(None)
        except Exception:  # noqa: BLE001
            pass


def _make_settings(tmp_path) -> SettingsManager:
    return SettingsManager(tmp_path / "app_config.json")


def test_bg_opacity_zero_not_overridden(app, tmp_path):
    """用户把透明度设为 0.0：必须原样读取，不得被 `or 0.6` 篡改。"""
    sm = _make_settings(tmp_path)
    fake = _Fake(sm)
    sm.set("ui", "background_opacity", 0.0)
    assert fake._bg_opacity() == 0.0
    sm.set("ui", "background_opacity", 0.3)
    assert fake._bg_opacity() == 0.3
    sm.set("ui", "background_opacity", None)
    assert fake._bg_opacity() == 0.6
    sm.set("ui", "background_opacity", "bad")
    assert fake._bg_opacity() == 0.6


def test_bg_compose_async_runs_off_gui_thread(app, tmp_path):
    """_bg_image_qss_block 主线程立即返回；后台合成完成后注入中央区。"""
    sm = _make_settings(tmp_path)
    fake = _Fake(sm)
    img = QImage(2000, 1500, QImage.Format_RGB32)
    img.fill(QColor("#3366aa"))
    bg_path = str(tmp_path / "wall.jpg")
    img.save(bg_path, "JPG", 90)
    sm.set("ui", "background_image", bg_path)
    sm.set("ui", "background_opacity", 0.6)

    t0 = time.perf_counter()
    qss = MainWindow._bg_image_qss_block(fake)
    gui_wait = time.perf_counter() - t0
    assert gui_wait < 0.05  # 主线程不再阻塞做整张大图合成

    deadline = time.time() + 5
    while time.time() < deadline:
        app.processEvents()
        if fake._central_area._bg_pixmap is not None:
            break
        time.sleep(0.02)
    pm = fake._central_area._bg_pixmap
    assert pm is not None and not pm.isNull()
    assert pm.toImage().size().width() == 1100
    assert pm.toImage().size().height() == 760


def _wait_key(app, key_file, needle, timeout=5.0) -> str:
    """轮询等待后台合成把缓存 key 写成包含 needle 的内容。"""
    deadline = time.time() + timeout
    text = ""
    while time.time() < deadline:
        app.processEvents()
        try:
            text = key_file.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if needle in text:
            return text
        time.sleep(0.02)
    return text


def test_bg_compose_cache_key_tracks_opacity(app, tmp_path):
    """透明度变化 → 缓存 key 更新，重新合成产物随之变化。"""
    sm = _make_settings(tmp_path)
    fake = _Fake(sm)
    img = QImage(1500, 1000, QImage.Format_RGB32)
    img.fill(QColor("#886622"))
    bg_path = str(tmp_path / "wall2.jpg")
    img.save(bg_path, "JPG", 90)
    sm.set("ui", "background_image", bg_path)

    cache_dir = Path(_app_base_dir()) / "data" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key_file = cache_dir / "bg_composed.key"

    sm.set("ui", "background_opacity", 0.2)
    MainWindow._bg_image_qss_block(fake)
    assert "0.2" in _wait_key(app, key_file, "0.2")

    sm.set("ui", "background_opacity", 0.9)
    MainWindow._bg_image_qss_block(fake)
    assert "0.9" in _wait_key(app, key_file, "0.9")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])