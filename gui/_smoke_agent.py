"""源编辑器 AI 制源入口离屏测试（AgentDialog / AI 按钮）。"""
import os
import sys
import pathlib
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication


def main():
    app = QApplication.instance() or QApplication([])

    # 1) AgentDialog 可实例化且含核心控件
    from gui.components.agent_dialog import AgentDialog

    dlg = AgentDialog()
    assert dlg._url_input is not None, "missing url input"
    assert dlg._model_combo is not None, "missing model combo"
    assert dlg._log_view is not None, "missing log view"
    assert dlg._start_btn is not None, "missing start button"
    assert dlg._stop_btn is not None, "missing stop button"
    dlg._log_view.appendPlainText("test log line")
    dlg._status_label.setText("ready")
    print("AgentDialog 实例化 OK")

    # 2) 空 URL 时点开始 → 提示（不崩溃）
    dlg._url_input.setText("")
    dlg._on_start()
    assert "URL" in dlg._status_label.text(), dlg._status_label.text()
    print("AgentDialog 空URL校验 OK")

    # 3) 无模型选择时点开始 → 提示
    dlg._url_input.setText("https://example.com")
    dlg._model_combo.clear()
    dlg._on_start()
    assert "模型" in dlg._status_label.text(), dlg._status_label.text()
    print("AgentDialog 无模型提示 OK")

    # 4) 日志追加 + 状态
    dlg._on_log("[Phase 1] 测试日志")
    txt = dlg._log_view.toPlainText()
    assert "Phase 1" in txt
    print("AgentDialog 日志追加 OK")

    # 5) 源编辑器含 AI 制源按钮
    from gui.components.source_editor import SourceEditor
    from framework.config import SourceConfig

    cfg = SourceConfig.from_dict({
        "$id": "smoke_ai_src", "$type": "novel", "$name": "Smoke",
        "transports": {"base_url": "https://example.com"},
    }, "<smoke>")
    editor = SourceEditor(source_config=cfg, parent=None, preview=None)
    assert hasattr(editor, "_ai_btn"), "缺少 AI 制源按钮"
    assert editor._ai_btn.text() == "🤖 AI 制源"
    editor.close()
    print("源编辑器 AI 制源按钮 OK")

    print("\n=== 源编辑器 AI 制源离屏测试通过 ===")


if __name__ == "__main__":
    main()
