"""内核基础模块冒烟测试（离线，不依赖 GUI）。"""
import os
import sys
import tempfile
import pathlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from framework.errors import *
from framework.events import EventBus, Event, EVENT_SEARCH_STARTED
from framework.config import SourceConfig
from framework.source_manager import SourceManager
from framework.settings_manager import SettingsManager
from framework.search_history import SearchHistory
from framework.theme_manager import ThemeManager


def test_eventbus():
    bus = EventBus()
    got = []
    bus.subscribe(lambda e: got.append(e.type))
    bus.emit(Event(EVENT_SEARCH_STARTED, {"source_id": "x"}))
    assert got == [EVENT_SEARCH_STARTED], got
    print("EventBus OK")


def test_themes():
    p = pathlib.Path(tempfile.mkdtemp()) / "app_config.json"
    sm = SettingsManager(p)
    tm = ThemeManager(sm)
    assert tm.current_key() == "sakura"
    assert sm.get("ui", "theme") == "sakura"
    tm.switch_to("midnight")
    assert sm.get("ui", "theme") == "midnight"
    # 非法主题回退 sakura
    tm.switch_to("bad_key")
    assert tm.current_key() == "sakura"
    # QSS 生成
    qss = tm.generate_qss("midnight")
    assert "{bg}" not in qss  # 模板已替换
    assert "#1E1B26" in qss  # midnight bg
    print("Theme OK")


def test_search_history():
    sh = SearchHistory(pathlib.Path(tempfile.mkdtemp()) / "h.json")
    for i in range(25):
        sh.push(f"word{i}")
    assert len(sh.recent()) == 20, len(sh.recent())
    assert sh.recent()[0] == "word24"
    sh.push("word5")  # 去重移前
    assert sh.recent()[0] == "word5"
    print("SearchHistory OK (20条约束+去重)")


def test_source_manager():
    cfg = SourceConfig.from_dict(
        {
            "$schema_version": 2,
            "$id": "demo_a",
            "$type": "novel",
            "$name": "站点A",
            "transports": {"base_url": "http://x"},
            "$enabled": True,
            "$weight": 1.0,
        }
    )
    assert cfg.source_id == "demo_a"
    assert cfg.content_type == "novel"

    mgr = SourceManager()
    mgr.add(cfg)
    n = SourceConfig.from_dict(
        {
            "$schema_version": 2,
            "$id": "demo_b",
            "$type": "video",
            "$name": "站点B",
            "transports": {"base_url": "http://y"},
            # 缺省 $enabled 默认 True
        }
    )
    mgr.add(n)
    assert mgr.count_by_type() == {"novel": 1, "comic": 0, "video": 1}
    assert mgr.count_enabled() == 2
    mgr.set_enabled("demo_a", False)
    assert mgr.count_enabled() == 1
    mgr.update_health("demo_a", "broken", "结构变更")
    assert mgr.count_broken() == 1
    h = mgr.get_health("demo_a")
    assert h.state == "broken"
    assert h.history[-1] == "broken"
    print("SourceManager OK (统计+健康)")


def test_config_validation():
    # 缺 $id 报错
    try:
        SourceConfig.from_dict({"$type": "novel", "$name": "x"})
        assert False, "应报错"
    except ConfigError as e:
        assert "$id" in e.message
    # 非法 $type
    try:
        SourceConfig.from_dict({"$id": "a", "$type": "music", "$name": "x"})
        assert False, "应报错"
    except ConfigError:
        pass
    print("Config 校验 OK")


if __name__ == "__main__":
    test_eventbus()
    test_themes()
    test_search_history()
    test_source_manager()
    test_config_validation()
    print("\n=== 内核基础模块冒烟测试全部通过 ===")
