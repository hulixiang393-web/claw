"""设置页离屏测试（SettingsPage）。"""
import os
import sys
import pathlib
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

from framework.settings_manager import SettingsManager
from framework.theme_manager import ThemeManager
from gui.pages.settings_page import SettingsPage


def main():
    app = QApplication.instance() or QApplication([])
    base = pathlib.Path(tempfile.mkdtemp())
    sm = SettingsManager(base / "ac.json")
    tm = ThemeManager(sm)
    page = SettingsPage(sm, tm)
    page.resize(700, 600)
    page.show()
    app.processEvents()

    # 分区 Tab 数（网络/UI/下载/书架/诊断/广告 = 6）
    tabs = [page.tabs.tabText(i) for i in range(page.tabs.count())]
    print("分区:", tabs)
    assert tabs == ["网络", "UI", "下载", "书架", "诊断", "广告规则"], tabs

    # 字段载入（默认值）
    assert page._dl_concurrent.value() == 6, page._dl_concurrent.value()
    assert page._ui_theme.currentText() == "sakura", page._ui_theme.currentText()
    assert page._net_timeout.value() == 10, page._net_timeout.value()

    # 修改并应用 → 写回 settings + 持久化
    page._dl_concurrent.setValue(3)
    page._net_timeout.setValue(15)
    page._ui_theme.setCurrentText("midnight")
    page._on_apply()
    assert sm.get("download", "max_concurrent_downloads") == 3
    assert sm.get("network", "default_timeout") == 15
    assert sm.get("ui", "theme") == "midnight"
    print("应用保存 OK")

    # 主题实时切换
    got = []
    page.theme_changed.connect(lambda t: got.append(t))
    page._ui_theme.setCurrentText("mint")
    assert "mint" in got, got
    assert tm.current_key() == "mint", tm.current_key()
    print("主题切换 OK")

    # 新控件：背景图/透明度 保存 + 加载
    page._ui_bg_image.setText("C:/fake/bg.png")
    page._ui_bg_opacity.setValue(0.4)
    page._on_apply()
    assert sm.get("ui", "background_image") == "C:/fake/bg.png"
    assert abs(sm.get("ui", "background_opacity", 0.6) - 0.4) < 1e-9
    page._load_values()
    assert page._ui_bg_opacity.value() == 0.4
    print("背景图/透明度 保存加载 OK")

    # settings_applied 信号
    applied = []
    page.settings_applied.connect(lambda: applied.append(1))
    page._on_apply()
    assert applied, "settings_applied 未触发"
    print("settings_applied 信号 OK")

    # 清除缓存按钮存在且不崩
    from PySide6.QtWidgets import QMessageBox

    _orig_info = QMessageBox.information
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
    try:
        page._on_cache_clear()
    finally:
        QMessageBox.information = _orig_info
    print("清除缓存 OK")

    # 恢复默认 → 重置
    orig = QMessageBox.question
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    try:
        page._on_reset()
    finally:
        QMessageBox.question = orig
    assert sm.get("download", "max_concurrent_downloads") == 6
    assert page._dl_concurrent.value() == 6
    print("恢复默认 OK")

    print("\n=== 设置页离屏测试通过 ===")


if __name__ == "__main__":
    main()