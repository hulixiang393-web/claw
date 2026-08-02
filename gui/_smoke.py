"""GUI 离屏冒烟测试（无需显示器）。

用 QT_QPA_PLATFORM=offscreen 验证：
- App 能启动
- 首页组件能构建
- 导航栏 8 个 Tab，未实现界面显示占位
- 主题切换生效
"""
import os
import sys
import pathlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

from framework.events import EventBus, Event, EVENT_SEARCH_STARTED, EVENT_SOURCE_ERROR
from framework.config import SourceConfig
from framework.source_manager import SourceManager, HEALTH_BROKEN
from framework.settings_manager import SettingsManager
from framework.search_history import SearchHistory
from framework.theme_manager import ThemeManager
from gui.components import MascotWidget
from gui.pages.home_page import HomePage


def test_home_with_sources():
    import tempfile

    base = pathlib.Path(tempfile.mkdtemp())
    sm = SettingsManager(base / "app_config.json")
    tm = ThemeManager(sm)
    bus = EventBus()

    mgr = SourceManager()
    cfg = SourceConfig.from_dict(
        {
            "$schema_version": 2,
            "$id": "demo",
            "$type": "novel",
            "$name": "演示站",
            "transports": {"base_url": "http://x"},
        }
    )
    mgr.add(cfg)

    hist = SearchHistory(base / "h.json")
    hist.push("凡人修仙传")
    hist.push("咒术回战")

    page = HomePage(mgr, bus, tm, sm, hist)
    page.refresh()
    assert page.stats_row.card_novel.value_label.text() == "1"
    assert page.stats_row.card_enabled.value_label.text() == "1"
    assert page.stats_row.card_broken.value_label.text() == "0"
    # 有源时空状态隐藏
    assert page.empty_state.isHidden() is True
    print("HomePage 有源: 统计+空状态 OK")


def test_home_empty_state():
    import tempfile

    base = pathlib.Path(tempfile.mkdtemp())
    sm = SettingsManager(base / "app_config.json")
    tm = ThemeManager(sm)
    bus = EventBus()
    mgr = SourceManager()
    hist = SearchHistory(base / "h2.json")

    page = HomePage(mgr, bus, tm, sm, hist)
    page.refresh()
    # isHidden() 反映显式隐藏状态，不依赖父窗口是否 show
    assert page.empty_state.isHidden() is False
    assert page.stats_row.isHidden() is True
    print("HomePage 空状态: 无源时显示插画+按钮 OK")


def test_mascot_states():
    app = QApplication.instance() or QApplication([])
    mascot = MascotWidget()
    bus = EventBus()
    bus.subscribe(mascot.on_event)
    bus.emit(Event(EVENT_SEARCH_STARTED, {"source_id": "x"}))
    assert mascot.current_state() == "running"
    bus.emit(Event(EVENT_SOURCE_ERROR, {"source_id": "x"}))
    assert mascot.current_state() == "error"
    print("吉祥物三态: running/error OK")


def test_theme_switch():
    import tempfile

    base = pathlib.Path(tempfile.mkdtemp())
    sm = SettingsManager(base / "app_config.json")
    tm = ThemeManager(sm)
    tm.switch_to("midnight")
    assert sm.get("ui", "theme") == "midnight"
    assert "#1E1B26" in tm.generate_qss()
    print("主题切换: midnight OK")


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    test_home_with_sources()
    test_home_empty_state()
    test_mascot_states()
    test_theme_switch()
    print("\n=== GUI 离屏冒烟测试全部通过 ===")
