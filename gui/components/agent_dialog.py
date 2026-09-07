"""Agent 自动制源对话框（agent_dialog.py）。

源编辑器「🤖 AI 制源」按钮触发的对话框。
输入站点 URL + 内容类型 → 实时日志 → 成功自动加载进源编辑器。
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class _AgentWorker(QThread):
    """后台运行 SourceAgent，发射日志和完成信号。

    日志同时写盘（logs/agent-YYYYMMDD.log），制源失败后可从文件复查——
    UI 实时日志在对话框关闭即丢失，不落盘无据可查。
    """

    log_signal = Signal(str)
    finished_signal = Signal(object)  # AgentResult

    def __init__(self, agent, site_url, content_type, category=""):
        super().__init__()
        self._agent = agent
        self._site_url = site_url
        self._content_type = content_type
        self._category = category
        self._log_file = None

    def _open_log(self):
        # 日志目录沿用项目约定 logs/
        log_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "logs",
        )
        os.makedirs(log_dir, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        name = "agent-%s-%s.log" % (stamp, self._content_type)
        self._log_file = open(
            os.path.join(log_dir, name), "a", encoding="utf-8", errors="replace"
        )
        self._log_file.write(
            "# [%s] 制源 session site=%s type=%s category=%s\n"
            % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               self._site_url, self._content_type, self._category or "-")
        )
        self._log_file.flush()

    def _write_log(self, msg: str):
        if self._log_file is None:
            self._open_log()
        try:
            self._log_file.write(
                "[%s] %s\n"
                % (datetime.datetime.now().strftime("%H:%M:%S"), msg)
            )
            self._log_file.flush()
        except OSError:
            pass

    def _close_log(self):
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None

    def run(self):
        from framework.source_agent import AgentResult

        try:
            result = self._agent.make_source(
                self._site_url,
                self._content_type,
                self._category,
                on_log=lambda msg: (self._write_log(msg), self.log_signal.emit(msg)),
            )
        except Exception as exc:  # noqa: BLE001 —— 兜底：任何异常都必须发完成信号，
            # 否则对话框卡在「制源中…」，且 _agent._http.close()（_on_finished）
            # 永不执行 → HttpClient 泄漏
            result = AgentResult(
                ok=False,
                logs=[f"[Exception] 制源异常：{exc}"],
                suggestions=[f"制源过程出现未预期异常，请重试或检查网络：{exc}"],
            )
        self._write_log("=== 制源结束 ok=%s ===" % result.ok)
        self._close_log()
        self.finished_signal.emit(result)


class AgentDialog(QDialog):
    """Agent 自动制源对话框。"""

    source_added = Signal(object)  # 成功时发射新的 SourceConfig

    def __init__(self, preview=None, parent=None):
        super().__init__(parent)
        self._preview = preview
        self._agent = None
        self._worker = None

        self.setWindowTitle("🤖 AI 自动制源")
        self.resize(600, 500)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # 输入区
        input_group = QWidget()
        input_layout = QVBoxLayout(input_group)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(8)

        # 站点 URL
        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("站点 URL:"))
        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("https://example.com")
        url_row.addWidget(self._url_input, stretch=1)
        input_layout.addLayout(url_row)

        # 内容类型 + 类别
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("内容类型:"))
        self._type_combo = QComboBox()
        self._type_combo.addItems(["novel", "comic", "video"])
        self._type_combo.setFixedWidth(100)
        type_row.addWidget(self._type_combo)
        type_row.addWidget(QLabel("类别(可选):"))
        self._category_input = QLineEdit()
        self._category_input.setPlaceholderText("如：玄幻、恋爱、动作")
        self._category_input.setFixedWidth(160)
        type_row.addWidget(self._category_input)
        type_row.addStretch(1)
        input_layout.addLayout(type_row)

        # 模型选择
        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("模型:"))
        self._model_combo = QComboBox()
        self._model_combo.setEditable(True)
        self._model_combo.setPlaceholderText("选择或输入模型名")
        model_row.addWidget(self._model_combo, stretch=1)
        model_row.addStretch(1)
        input_layout.addLayout(model_row)

        layout.addWidget(input_group)

        # 按钮区
        btn_row = QHBoxLayout()
        self._start_btn = QPushButton("🚀 开始制源")
        self._start_btn.clicked.connect(self._on_start)
        btn_row.addWidget(self._start_btn)
        self._stop_btn = QPushButton("⏹ 停止")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._on_stop)
        btn_row.addWidget(self._stop_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        # 日志面板
        log_label = QLabel("实时日志:")
        log_label.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(log_label)

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(500)
        self._log_view.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 11px;"
        )
        layout.addWidget(self._log_view, stretch=1)

        # 状态栏
        self._status_label = QLabel("就绪")
        self._status_label.setStyleSheet("color: palette(mid); font-size: 11px;")
        layout.addWidget(self._status_label)

        # 底部按钮
        bottom_row = QHBoxLayout()
        self._close_btn = QPushButton("关闭")
        self._close_btn.clicked.connect(self.close)
        bottom_row.addStretch(1)
        bottom_row.addWidget(self._close_btn)
        layout.addLayout(bottom_row)

        # 加载可用模型列表
        self._load_models()

    def _load_models(self) -> None:
        """加载 LlmKeyStore 中的模型配置：云端模型 + 本地 LLAMA（.gguf）。"""
        try:
            from framework.llm import LlmKeyStore
            ks = LlmKeyStore()
            cloud = ks.cloud()
            local = ks.local()
            if cloud.get("model"):
                self._model_combo.addItem(cloud["model"])
            if local.get("model"):
                self._model_combo.addItem(local["model"])
            recent = local.get("recent") or []
            for e in recent:
                name = e.get("name") or Path(e.get("model_path", "")).name
                if name and self._model_combo.findText(name) < 0:
                    self._model_combo.addItem(name, e)
        except Exception:  # noqa: BLE001
            pass

    def _on_start(self) -> None:
        """开始制源。"""
        site_url = self._url_input.text().strip()
        if not site_url:
            self._status_label.setText("请输入站点 URL")
            return

        content_type = self._type_combo.currentText()
        category = self._category_input.text().strip()

        # 创建 LLM 客户端
        try:
            from framework.llm import LlmClient, LlmKeyStore, LlmError
            ks = LlmKeyStore()
            cloud = ks.cloud()
            local = ks.local()

            model_name = self._model_combo.currentText().strip()
            model_data = self._model_combo.currentData()
            if not model_name:
                self._status_label.setText("请选择或输入模型名")
                return

            # 判断是云端还是本地
            if cloud.get("model") == model_name and cloud.get("base_url"):
                llm = LlmClient(
                    base_url=cloud["base_url"],
                    api_key=cloud.get("api_key", ""),
                    model=model_name,
                )
            elif local.get("model") == model_name:
                llm = LlmClient(
                    base_url=local.get("base_url") or "http://127.0.0.1:11434",
                    model=Path(local.get("model_path") or model_name).name,
                )
            elif model_data and model_data.get("model_path"):
                llm = LlmClient(
                    base_url=model_data.get("base_url")
                    or local.get("base_url")
                    or "http://127.0.0.1:11434",
                    model=Path(model_data["model_path"]).name,
                )
            else:
                # 默认用云端
                llm = LlmClient(
                    base_url=cloud.get("base_url", "https://api.openai.com"),
                    api_key=cloud.get("api_key", ""),
                    model=model_name,
                )
        except LlmError as exc:
            self._status_label.setText(f"LLM 配置错误：{exc}")
            return

        # 创建 Agent
        from framework.http import HttpClient
        from framework.source_agent import SourceAgent

        http = HttpClient()
        agent = SourceAgent(
            llm=llm,
            http=http,
            preview=self._preview,
            max_attempts=5,
        )
        self._agent = agent

        # 启动后台线程
        self._worker = _AgentWorker(agent, site_url, content_type, category)
        self._worker.log_signal.connect(self._on_log)
        self._worker.finished_signal.connect(self._on_finished)

        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._status_label.setText("制源中...")
        self._log_view.clear()

        self._worker.start()

    def _on_stop(self) -> None:
        """停止制源。"""
        if self._agent:
            self._agent.cancel()
        if self._worker:
            self._worker.wait(3000)
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._status_label.setText("已停止")

    def _on_log(self, msg: str) -> None:
        """追加日志。"""
        self._log_view.appendPlainText(msg)
        # 自动滚动到底部
        sb = self._log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_finished(self, result) -> None:
        """制源完成。"""
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)

        if result.ok:
            self._status_label.setText(f"✅ 制源成功！source_id={result.source_id}")
            self._log_view.appendPlainText(f"\n=== 制源成功：{result.source_id} ===")
            # 发射信号让源编辑器加载新源
            self.source_added.emit(result)
        else:
            self._status_label.setText("❌ 制源失败，详见日志")
            self._log_view.appendPlainText("\n=== 制源失败 ===")
            if result.suggestions:
                self._log_view.appendPlainText("建议：")
                for s in result.suggestions:
                    self._log_view.appendPlainText(f"  • {s}")

        # 关闭 HttpClient
        if self._agent:
            self._agent._http.close()
