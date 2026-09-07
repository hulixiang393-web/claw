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
    base = pathlib.Path(tempfile.mkdtemp()) if "base" not in dir() else base
    sm = SettingsManager(base / "ac.json")
    tm = ThemeManager(sm)
    page = SettingsPage(sm, tm)
    page.resize(700, 600)
    page.show()
    app.processEvents()

    # 分区 Tab 数（网络/UI/下载/书架/诊断/广告/LLM = 7）
    tabs = [page.tabs.tabText(i) for i in range(page.tabs.count())]
    print("分区:", tabs)
    assert tabs == ["网络", "UI", "下载", "书架", "诊断", "广告规则", "LLM"], tabs

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

    # LLM 设置 Tab：字段存在 + 密码回显 + 保存（用临时 LlmKeyStore 路径隔离）
    import json as _json
    assert hasattr(page, "_llm_api_key"), "缺少 _llm_api_key"
    assert hasattr(page, "_llm_base_url"), "缺少 _llm_base_url"
    assert hasattr(page, "_llm_model"), "缺少 _llm_model"
    assert hasattr(page, "_llama_server_path"), "缺少 _llama_server_path"
    assert hasattr(page, "_llama_model_path"), "缺少 _llama_model_path"
    assert hasattr(page, "_llama_port"), "缺少 _llama_port"
    assert hasattr(page, "_llama_start_btn"), "缺少 LLAMA 启动按钮"
    assert hasattr(page, "_llm_test_btn"), "缺少云端测试按钮"
    assert hasattr(page, "_llm_prompt_edit"), "缺少提示词编辑区"
    # API Key 输入必须用 Password 回显（安全约束）
    from PySide6.QtWidgets import QLineEdit
    assert page._llm_api_key.echoMode() == QLineEdit.EchoMode.Password, "API Key 未用 Password 回显"

    # 用临时 LlmKeyStore 验证 Key 写 data/ 且不碰 app_config.json
    llm_data = base / "data" / "llm_keys.json"

    class _TmpStore:
        def __init__(self):
            self._data = {}
            if llm_data.exists():
                self._data = _json.loads(llm_data.read_text(encoding="utf-8"))

        def load(self):
            return self._data

        def cloud(self):
            return self._data.get("cloud") or {}

        def local(self):
            return self._data.get("local") or {}

        def save_cloud(self, api_key="", base_url="", model=""):
            self._data["cloud"] = {"api_key": api_key, "base_url": base_url, "model": model}
            self._flush()

        def save_local(self, base_url="", port=11434, server_path="", model_path="", model=""):
            self._data["local"] = {"base_url": base_url, "port": port,
                                   "server_path": server_path, "model_path": model_path,
                                   "model": model}
            self._flush()

        def _flush(self):
            llm_data.parent.mkdir(parents=True, exist_ok=True)
            llm_data.write_text(_json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")

    tmp_store = _TmpStore()
    import framework.llm as llm_mod
    _orig_store = llm_mod.LlmKeyStore
    llm_mod.LlmKeyStore = lambda *a, **k: tmp_store
    try:
        page._llm_api_key.setText("sk-super-secret-key")
        page._llm_base_url.setText("https://api.example.com/v1")
        page._llm_model.setText("test-model")
        page._llama_server_path.setText("C:/tools/llama-server.exe")
        page._llama_model_path.setText("C:/models/qwen.gguf")
        page._llama_port.setValue(11434)
        applied.clear()
        page._on_apply()
        # app_config 不应包含 Key
        ac_path = base / "ac.json"
        ac_content = ac_path.read_text(encoding="utf-8")
        assert "sk-super-secret-key" not in ac_content, "Key 泄入 app_config.json！"
        assert "sk-super-secret-key" in llm_data.read_text(encoding="utf-8"), "Key 未写入 llm_keys.json"
        assert "qwen.gguf" in llm_data.read_text(encoding="utf-8"), "本地模型未写入 llm_keys.json"
        print("LLM 配置保存 + Key 隔离 OK")
    finally:
        llm_mod.LlmKeyStore = _orig_store

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
