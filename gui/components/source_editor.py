"""源编辑器对话框（source_editor.py）。

对应 ui-editor.md 功能点：
- 字段分组表单（按 $type 动态显示）
- 保存不变量（SourceConfig.from_dict 严格校验）
- 允许残缺保存（草稿，$enabled=false）
- 新建 / 编辑切换
- JSON 导入/导出

表单为主 —— JSON 只做导入/导出与后端。实时选择器预览（preview.py）为另一独立任务。
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gui.components.editor_grids import KeyValueGrid, SelectorGrid
from gui.components import source_presets

from framework.config import SourceConfig, load_source, ConfigError

# 内容类型与对应正文块名
TYPE_CONTENT_BLOCK = {
    "novel": "chapter",
    "comic": "page",
    "video": "episode",
}


def _deep_get(d, path: str, default=None):
    """从嵌套 dict 按点分路径取值。path 如 "endpoints.search.keyword_param"。"""
    cur = d
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur if cur is not None else default


def _deep_set(d, path: str, value, remove_empty: bool = True):
    """把值写回嵌套 dict（按点分路径，自动建中间 dict）。

    remove_empty=True 时，字符串值为空则删除该键（避免草稿留空字符串污染 JSON）。
    """
    keys = path.split(".")
    cur = d
    for key in keys[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    leaf = keys[-1]
    if remove_empty and isinstance(value, str):
        value = value.strip()
        if not value:
            cur.pop(leaf, None)
            return
    if isinstance(value, str):
        value = value.strip()
    cur[leaf] = value


class _HelpBanner(QFrame):
    """帮助横幅：每 Tab 顶部的浅色说明条，一步步教怎么填。"""

    def __init__(self, title: str, lines: list, parent=None):
        super().__init__(parent)
        self.setObjectName("helpBanner")
        self.setStyleSheet(
            "QFrame#helpBanner { background: #FDF6EC; border: 1px solid #F0DFC8;"
            " border-radius: 8px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)

        head = QHBoxLayout()
        head.setSpacing(6)
        step = QLabel("💡")
        step.setStyleSheet("font-size: 14px;")
        head.addWidget(step)
        title_label = QLabel(title)
        title_label.setStyleSheet(
            "color: #8A6D3B; font-size: 12px; font-weight: bold;"
        )
        head.addWidget(title_label)
        head.addStretch(1)
        layout.addLayout(head)

        body = QLabel("\n".join(f"• {line}" for line in lines))
        body.setWordWrap(True)
        body.setStyleSheet("color: #A58A5F; font-size: 11px; line-height: 1.5;")
        layout.addWidget(body)


class _ExampleButton(QPushButton):
    """「填入示例」按钮：点击用真实源字段填充当前 Tab 空白控件。"""

    def __init__(self, text: str, parent=None):
        super().__init__(text)
        self.setFixedWidth(120)
        self.setStyleSheet(
            "background: transparent; color: palette(accent); border: 1px dashed"
            " palette(stroke); border-radius: 6px; font-size: 11px; padding: 3px 8px;"
        )


def _group_label(text: str) -> QLabel:
    """表单内的小节标题（用于 SelectorGrid 等非表单行控件前打标记）。"""
    lab = QLabel(text)
    lab.setStyleSheet("color: palette(accent); font-size: 11px; font-weight: bold;"
                      " margin-top: 6px;")
    return lab


class _FormSection(QWidget):
    """表单分区：供编辑器各标签页复用（label → 控件 表单布局）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.setSpacing(8)

        self._form = QFormLayout()
        self._form.setContentsMargins(16, 12, 16, 12)
        self._form.setSpacing(8)
        self._form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._outer.addLayout(self._form)
        self._path_map = {}  # 控件 → 点分路径（回读时用）

    # ---- 帮助横幅 / 示例 --------------------------------------------- #
    def add_help(self, title: str, lines: list) -> None:
        """在分区顶部插入帮助横幅。"""
        banner = _HelpBanner(title, lines)
        self._outer.insertWidget(0, banner)

    def add_example(self, text: str, callback) -> _ExampleButton:
        """在分区底部插入「填入示例」按钮。"""
        btn = _ExampleButton(text)
        btn.clicked.connect(callback)
        self._outer.addWidget(btn, alignment=Qt.AlignLeft)
        return btn

    # ---- 通用控件 ---------------------------------------------------- #
    def _line(self, label: str, path: str, hint: str = "") -> QLineEdit:
        w = QLineEdit()
        w.setProperty("path", path)
        self._form.addRow(label, self._hint_row(w, hint))
        self._path_map[id(w)] = path
        return w

    def _spin(self, label: str, path: str, lo: int, hi: int, suffix: str = "",
              hint: str = "") -> QSpinBox:
        w = QSpinBox()
        w.setRange(lo, hi)
        if suffix:
            w.setSuffix(suffix)
        w.setProperty("path", path)
        self._form.addRow(label, self._hint_row(w, hint))
        self._path_map[id(w)] = path
        return w

    def _double(self, label: str, path: str, lo: float, hi: float,
                hint: str = "") -> QDoubleSpinBox:
        w = QDoubleSpinBox()
        w.setRange(lo, hi)
        w.setDecimals(1)
        w.setSingleStep(0.1)
        w.setProperty("path", path)
        self._form.addRow(label, self._hint_row(w, hint))
        self._path_map[id(w)] = path
        return w

    def _check(self, label: str, path: str, hint: str = "") -> QCheckBox:
        w = QCheckBox()
        w.setProperty("path", path)
        self._form.addRow(label, self._hint_row(w, hint))
        self._path_map[id(w)] = path
        return w

    def _combo(self, label: str, path: str, items: list, hint: str = "") -> QComboBox:
        w = QComboBox()
        w.addItems(items)
        w.setProperty("path", path)
        self._form.addRow(label, self._hint_row(w, hint))
        self._path_map[id(w)] = path
        return w

    def _text(self, label: str, path: str, height: int = 120,
              hint: str = "") -> QPlainTextEdit:
        w = QPlainTextEdit()
        w.setProperty("path", path)
        w.setFixedHeight(height)
        w.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; font-size: 12px;"
        )
        self._form.addRow(label, self._hint_row(w, hint))
        self._path_map[id(w)] = path
        return w

    def _hint_row(self, widget, hint: str) -> QWidget:
        """把控件 + 行内灰色说明打包成一行（hint 空则只返回控件）。"""
        if not hint:
            return widget
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(widget)
        h = QLabel(hint)
        h.setStyleSheet("color: palette(mid); font-size: 11px;")
        lay.addWidget(h)
        lay.addStretch(1)
        return row


class SourceEditor(QDialog):
    """源编辑器对话框。source_config 为 None 时进入新建（草稿）模式。"""

    source_saved = Signal(str)  # 保存成功后发射 source_id

    def __init__(self, source_config=None, sources_dir=None, parent=None, preview=None):
        super().__init__(parent)
        self._src = source_config
        self._sources_dir = Path(sources_dir) if sources_dir else None
        # self._raw：完整 JSON dict（含未暴露字段），保存时只覆盖已暴露字段
        self._raw = dict(source_config.raw) if source_config else self._default_template("novel")
        self._mode = "EDIT" if source_config else "NEW"
        self._preview = preview  # framework.preview.Preview（实时验证/测试搜索/测试详情）

        self.setWindowTitle("编辑源" if self._mode == "EDIT" else "添加源")
        self.resize(760, 680)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self._build_topbar()
        layout.addLayout(self._topbar)

        # 制作引导步骤条
        self._build_stepbar()
        layout.addLayout(self._stepbar)

        self.tabs = QTabWidget()
        self._build_basic()
        self._build_network()
        self._build_discovery()
        self._build_search()
        self._build_detail()
        self._build_content()
        self._build_constraints()
        self._build_diagnostics()
        self._build_adblock()
        self._build_api()
        self._build_decrypt()
        self._build_auth()
        self.tabs.currentChanged.connect(self._update_stepbar)
        layout.addWidget(self.tabs, stretch=1)

        # 预览面板（实时验证选择器 / 测试搜索 / 测试详情）
        self._build_preview_panel()
        layout.addWidget(self._preview_panel)

        # 底部按钮
        btns = QHBoxLayout()
        btns.addStretch(1)
        self.draft_btn = QPushButton("保存草稿(禁用)")
        self.draft_btn.clicked.connect(self._save_draft)
        btns.addWidget(self.draft_btn)
        self.export_btn = QPushButton("导出 JSON")
        self.export_btn.clicked.connect(self._export_json)
        btns.addWidget(self.export_btn)
        self.save_btn = QPushButton("校验并保存")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._validate_and_save)
        btns.addWidget(self.save_btn)
        layout.addLayout(btns)

        self._load_into_form()

    # ================================================================== #
    # 滚动包装
    # ================================================================== #
    @staticmethod
    def _wrap_scroll(widget: QWidget) -> QScrollArea:
        """把 Tab 表单包进滚动区：内容超过可视高度时可滚动查看。

        各 Tab 字段数不一（视频「正文」Tab 有媒体+换源两套子表单，最多），
        内容超出窗口时底部字段会被裁掉且无法看到，包一层滚动区解决。
        """
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(widget)
        return scroll

    # ================================================================== #
    # 顶部栏
    # ================================================================== #
    def _build_topbar(self) -> None:
        self._topbar = QHBoxLayout()
        self._topbar.setSpacing(8)

        self.id_label = QLabel("ID:")
        self._topbar.addWidget(self.id_label)
        self.id_edit = QLineEdit(self._raw.get("$id", ""))
        self.id_edit.setFixedWidth(160)
        self.id_edit.setPlaceholderText("sources id")
        self._topbar.addWidget(self.id_edit)

        self._topbar.addWidget(QLabel("类型:"))
        self.type_combo = QComboBox()
        self.type_combo.addItems(["novel", "comic", "video"])
        self.type_combo.setCurrentText(self._raw.get("$type", "novel"))
        self.type_combo.setFixedWidth(80)
        self.type_combo.currentTextChanged.connect(self._on_type_changed)
        self._topbar.addWidget(self.type_combo)

        self.status_label = QLabel("")
        self._update_status_label()
        self._topbar.addWidget(self.status_label)
        self._topbar.addStretch(1)

        # 🤖 AI 制源按钮（所有模式都显示）
        self._ai_btn = QPushButton("🤖 AI 制源")
        self._ai_btn.setToolTip("用 LLM 自动分析站点并生成源配置")
        self._ai_btn.clicked.connect(self._on_ai_source)
        self._topbar.addWidget(self._ai_btn)

        # 新建模式下提供「填入示例」：用同类型的参考源填充空白字段
        if self._mode == "NEW":
            self.example_btn = _ExampleButton("🎓 填入示例")
            self.example_btn.setToolTip("用同类型的现有源填充各 Tab 空白字段，便于参考")
            self.example_btn.clicked.connect(self._fill_example)
            self._topbar.addWidget(self.example_btn)

    # ================================================================== #
    # 预览面板（ui-editor #2/#3/#4）
    # ================================================================== #
    def _build_preview_panel(self) -> None:
        """底部预览面板：选择器实时验证 + 测试搜索 + 测试详情。

        无 preview 注入时整个面板隐藏（内核未接线的降级）。
        """
        panel = QFrame()
        panel.setObjectName("editorPreview")
        panel.setStyleSheet(
            "QFrame#editorPreview { background: palette(base); border: 1px solid palette(mid);"
            " border-radius: 8px; }"
        )
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)

        # 标题行：说明 + 折叠
        head = QHBoxLayout()
        title = QLabel("🔍 实时预览")
        title.setStyleSheet("font-weight: bold; font-size: 12px;")
        head.addWidget(title)
        head.addStretch(1)
        self.preview_toggle = QPushButton("收起")
        self.preview_toggle.setFixedWidth(60)
        self.preview_toggle.setStyleSheet("font-size: 11px; padding: 2px 8px;")
        self.preview_toggle.clicked.connect(self._toggle_preview)
        head.addWidget(self.preview_toggle)
        lay.addLayout(head)

        # 折叠容器
        self.preview_body = QWidget()
        body = QVBoxLayout(self.preview_body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(6)

        # 行1：选择器验证（选择器输入 + 验证按钮）
        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("选择器:"))
        self.preview_sel = QLineEdit()
        self.preview_sel.setPlaceholderText("CSS/XPath，如 a.title 或 //div[@class='item']")
        self.preview_sel.returnPressed.connect(self._do_validate_selector)
        # 自动防抖 500ms 触发（ui-editor #2）：停止输入 500ms 后自动验证
        from PySide6.QtCore import QTimer

        self._sel_debounce = QTimer(self)
        self._sel_debounce.setSingleShot(True)
        self._sel_debounce.setInterval(500)
        self._sel_debounce.timeout.connect(self._do_validate_selector)
        self.preview_sel.textChanged.connect(
            lambda _: self._sel_debounce.start() if self.preview_sel.text().strip() else None
        )
        sel_row.addWidget(self.preview_sel, stretch=1)
        self.preview_sel_url = QLineEdit()
        self.preview_sel_url.setPlaceholderText("验证 URL（留空=源 base_url）")
        self.preview_sel_url.setFixedWidth(220)
        sel_row.addWidget(self.preview_sel_url)
        self.preview_sel_btn = QPushButton("验证")
        self.preview_sel_btn.setFixedWidth(60)
        self.preview_sel_btn.clicked.connect(self._do_validate_selector)
        sel_row.addWidget(self.preview_sel_btn)
        body.addLayout(sel_row)

        # 行2：测试搜索 / 测试详情（关键词/URL 输入 + 按钮）
        test_row = QHBoxLayout()
        self.preview_kw = QLineEdit()
        self.preview_kw.setPlaceholderText("测试搜索关键词")
        self.preview_kw.returnPressed.connect(self._do_preview_search)
        test_row.addWidget(self.preview_kw, stretch=1)
        self.preview_search_btn = QPushButton("测试搜索")
        self.preview_search_btn.setFixedWidth(80)
        self.preview_search_btn.clicked.connect(self._do_preview_search)
        test_row.addWidget(self.preview_search_btn)
        test_row.addSpacing(8)
        self.preview_detail_url = QLineEdit()
        self.preview_detail_url.setPlaceholderText("测试详情 URL")
        self.preview_detail_url.returnPressed.connect(self._do_preview_detail)
        test_row.addWidget(self.preview_detail_url, stretch=1)
        self.preview_detail_btn = QPushButton("测试详情")
        self.preview_detail_btn.setFixedWidth(80)
        self.preview_detail_btn.clicked.connect(self._do_preview_detail)
        test_row.addWidget(self.preview_detail_btn)
        body.addLayout(test_row)

        # 结果区
        self.preview_result = QLabel("")
        self.preview_result.setWordWrap(True)
        self.preview_result.setStyleSheet(
            "color: palette(text2); font-size: 11px; font-family: Consolas, monospace;"
            " max-height: 90px;"
        )
        self.preview_result.setAlignment(Qt.AlignTop)
        self.preview_result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        body.addWidget(self.preview_result)

        lay.addWidget(self.preview_body)

        # 无 preview 注入 → 隐藏整个面板
        if self._preview is None:
            panel.setVisible(False)
        self._preview_panel = panel
        self._preview_visible = True

    def _toggle_preview(self) -> None:
        """折叠/展开预览面板。"""
        self._preview_visible = not self._preview_visible
        self.preview_body.setVisible(self._preview_visible)
        self.preview_toggle.setText("展开" if not self._preview_visible else "收起")

    def _current_source_config(self):
        """从表单构造当前 SourceConfig（供预览请求使用）。"""
        try:
            raw = self._build_dict()
            return SourceConfig.from_dict(raw)
        except Exception:  # noqa: BLE001
            return None

    def _do_validate_selector(self) -> None:
        """实时验证选择器：抓页面，显示命中项。"""
        if self._preview is None:
            self.preview_result.setText("预览内核未注入")
            return
        sel = self.preview_sel.text().strip()
        if not sel:
            return
        source = self._current_source_config()
        if source is None:
            self.preview_result.setText("⚠ 表单配置不完整，无法构造源")
            return
        url = self.preview_sel_url.text().strip() or source.base_url
        css = sel if not sel.startswith("//") else ""
        xpath = sel if sel.startswith("//") else ""
        hits = self._preview.validate_selector(source, url, css=css, xpath=xpath)
        if hits:
            self.preview_result.setText(
                f"✓ 命中 {len(hits)} 项：\n" + "\n".join(f"  · {h}" for h in hits[:8])
            )
        else:
            self.preview_result.setText("✗ 未命中任何元素，检查选择器或站点结构")

    def _do_preview_search(self) -> None:
        """一键测试搜索。"""
        if self._preview is None:
            self.preview_result.setText("预览内核未注入")
            return
        kw = self.preview_kw.text().strip()
        if not kw:
            return
        source = self._current_source_config()
        if source is None:
            self.preview_result.setText("⚠ 表单配置不完整，无法构造源")
            return
        self.preview_result.setText("搜索中...")
        results = self._preview.preview_search(source, kw)
        if results:
            lines = [f"· {r.title}（{r.source_name or r.source_id}）" for r in results[:8]]
            self.preview_result.setText(f"✓ 命中 {len(results)} 条：\n" + "\n".join(lines))
        else:
            self.preview_result.setText("✗ 未搜索到结果，检查搜索配置")

    def _do_preview_detail(self) -> None:
        """一键测试详情。"""
        if self._preview is None:
            self.preview_result.setText("预览内核未注入")
            return
        url = self.preview_detail_url.text().strip()
        if not url:
            return
        source = self._current_source_config()
        if source is None:
            self.preview_result.setText("⚠ 表单配置不完整，无法构造源")
            return
        self.preview_result.setText("抓取详情中...")
        detail = self._preview.preview_detail(source, url)
        if not detail:
            self.preview_result.setText("✗ 详情解析失败")
            return
        if detail.get("error"):
            self.preview_result.setText(f"✗ 详情失败：{detail['error']}")
            return
        lines = [
            f"标题: {detail.get('title')}",
            f"作者: {detail.get('author') or '无'}",
            f"状态: {detail.get('status') or '无'}",
            f"章节数: {detail.get('chapters') or 0}",
            f"简介: {detail.get('summary') or '无'}",
        ]
        self.preview_result.setText("\n".join(lines))

    # ================================================================== #
    # 制作引导步骤条
    # ================================================================== #
    _STEPS = [
        ("1 基本", 0, "先给源起个名字"),
        ("2 网络", 1, "填站点地址"),
        ("3 发现", 2, "分类页入口"),
        ("4 搜索", 3, "搜索功能"),
        ("5 详情", 4, "作品详情页"),
        ("6 正文", 5, "内容/图片/剧集"),
        ("7 限制", 6, "爬取上限"),
        ("8 完成", 7, "保存源"),
    ]

    def _build_stepbar(self) -> None:
        """横向步骤条：点击跳到对应 Tab，当前步骤高亮。"""
        self._stepbar = QHBoxLayout()
        self._stepbar.setContentsMargins(16, 0, 16, 0)
        self._stepbar.setSpacing(0)
        self._step_btns: list[QPushButton] = []
        for i, (label, tab_idx, _) in enumerate(self._STEPS):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(26)
            btn.setStyleSheet(
                "QPushButton { border: none; padding: 2px 10px; font-size: 11px;"
                " color: palette(mid); background: transparent; }"
                "QPushButton:checked { color: palette(accent); font-weight: bold;"
                " border-bottom: 2px solid palette(accent); background: transparent; }"
            )
            btn.clicked.connect(lambda _, idx=tab_idx: self.tabs.setCurrentIndex(idx))
            self._stepbar.addWidget(btn)
            self._step_btns.append(btn)
        self._stepbar.addStretch(1)
        self._update_stepbar(0)

    def _update_stepbar(self, idx: int) -> None:
        """高亮当前步骤按钮，灰色已完成步骤。"""
        for i, btn in enumerate(self._step_btns):
            btn.setChecked(i == idx)
            if i < idx:
                btn.setStyleSheet(
                    "QPushButton { border: none; padding: 2px 10px; font-size: 11px;"
                    " color: palette(success); background: transparent; }"
                    "QPushButton:checked { color: palette(accent); font-weight: bold;"
                    " border-bottom: 2px solid palette(accent); }"
                )
            elif i == idx:
                btn.setStyleSheet(
                    "QPushButton { border: none; padding: 2px 10px; font-size: 11px;"
                    " color: palette(accent); font-weight: bold;"
                    " border-bottom: 2px solid palette(accent); background: transparent; }"
                )
            else:
                btn.setStyleSheet(
                    "QPushButton { border: none; padding: 2px 10px; font-size: 11px;"
                    " color: palette(mid); background: transparent; }"
                    "QPushButton:checked { color: palette(accent); font-weight: bold;"
                    " border-bottom: 2px solid palette(accent); }"
                )

    # ================================================================== #
    # 各标签页表单
    # ================================================================== #
    def _build_basic(self) -> None:
        sec = _FormSection()
        sec.add_help(
            "第 1 步 · 基本资料",
            [
                "源名：UI 显示的名字，如「禁漫天堂」。",
                "官网：站点首页地址，源管理页「打开官网」用它。",
                "标签：逗号分隔，如 漫画, 汉化, 成人，用于筛选。",
                "ID 在顶部填：只能小写字母/数字/下划线，存成 sources/<ID>.json。",
            ],
        )
        self._f_name = sec._line("源名", "$name", "例：禁漫天堂")
        self._f_home = sec._line("官网", "$metadata.homepage", "例：https://18mh.net")
        self._f_desc = sec._line("简介", "$metadata.description", "站点一句话简介")
        self._f_tags = sec._line("标签", "$metadata.tags", "逗号分隔，例：漫画, 汉化")
        self._f_icon = sec._line("图标 URL", "$metadata.icon",
                                 "源管理页图标，留空用默认")
        self._f_lang = sec._line("语言", "$metadata.lang", "例：zh-CN")
        self._f_region = sec._line("地区", "$metadata.region", "例：cn、global")
        self._f_weight = sec._double("权重", "$weight", 0.0, 10.0,
                                     hint="搜索排序权重，默认 1.0")
        self.tabs.addTab(self._wrap_scroll(sec), "基本信息")

    def _build_network(self) -> None:
        sec = _FormSection()
        sec.add_help(
            "第 2 步 · 网络连接",
            [
                "Base URL：站点根地址，所有相对路径自动拼它。换域名只改这一处。",
                "User-Agent：模仿浏览器，多数站要求非空。从浏览器 F12 复制。",
                "Cookie：需要登录/过风控的站才填（如年龄验证、登录态）。",
                "超时/重试/间隔：默认即可，被反爬时适当加大间隔。",
            ],
        )
        self._f_base = sec._line("Base URL", "transports.base_url",
                                 "例：https://18mh.net")
        self._f_ua = sec._line("User-Agent", "transports.headers[User-Agent]",
                               "从浏览器 F12 复制")
        self._f_referer = sec._line("Referer", "transports.headers[Referer]",
                                    "多数站填首页，反爬站必填")
        self._f_accept = sec._line("Accept", "transports.headers[Accept]",
                                   "可留空用默认")
        self._f_accept_lang = sec._line("Accept-Language",
                                        "transports.headers[Accept-Language]",
                                        "例：zh-CN,zh;q=0.8")
        self._f_cookie = sec._line("Cookie", "transports.cookie",
                                   "登录/风控才需要，留空即可")
        self._f_follow = sec._check("跟随重定向", "transports.follow_redirects",
                                    "默认跟随，个别站取消")
        self._f_charset = sec._line("字符集", "transports.charset",
                                    "中文乱码时填 utf-8，留空自动探测")
        self._f_timeout = sec._spin("超时(秒)", "transports.timeout", 1, 300, hint="默认 10")
        self._f_retries = sec._spin("重试", "transports.retries", 0, 10, hint="默认 3")
        self._f_interval = sec._spin("请求间隔(ms)", "transports.interval_ms",
                                     0, 10000, " ms", hint="反爬站调大到 800+")
        self._f_rb_base = sec._double("重试退避基础(秒)", "transports.retry_backoff.base",
                                      0.0, 60.0, hint="默认 1")
        self._f_rb_max = sec._double("退避上限(秒)", "transports.retry_backoff.max",
                                     0.0, 120.0, hint="默认 10")
        self._f_rb_jitter = sec._double("退避抖动", "transports.retry_backoff.jitter",
                                        0.0, 2.0, hint="随机扰动，默认 0.3")
        auto_btn = QPushButton("一键补全默认值（Referer / 字符集 / 请求头）")
        auto_btn.setStyleSheet(
            "font-size: 11px; padding: 4px 10px; color: palette(accent);"
            " background: transparent; border: 1px solid palette(midlight);"
            " border-radius: 4px;"
        )
        auto_btn.clicked.connect(self._auto_fill_network)
        sec._outer.addWidget(auto_btn)
        self.tabs.addTab(self._wrap_scroll(sec), "网络")

    def _auto_fill_network(self) -> None:
        """按 Base URL 自动补齐 Referer / 字符集 / 常用请求头（仅补空项）。"""
        sug = source_presets.auto_network_defaults(
            base_url=self._f_base.text(),
            referer=self._f_referer.text(),
            charset=self._f_charset.text(),
            accept=self._f_accept.text(),
            accept_lang=self._f_accept_lang.text(),
        )
        if "referer" in sug:
            self._f_referer.setText(sug["referer"])
        if "charset" in sug:
            self._f_charset.setText(sug["charset"])
        if "accept" in sug:
            self._f_accept.setText(sug["accept"])
        if "accept_lang" in sug:
            self._f_accept_lang.setText(sug["accept_lang"])

    def _build_discovery(self) -> None:
        sec = _FormSection()
        sec.add_help(
            "第 3 步 · 发现（可选）",
            [
                "有「分类浏览页」的站才需要，勾选启用。",
                "列表入口 URL：分类页地址，填相对路径（自动拼 Base URL）。",
                "例：18mh 的分类页是 /comic/all，23qb 的是带 {page} 的地址。",
                "没有分类页的站 → 不勾选，用户手动输入作品链接使用。",
            ],
        )
        self._f_disc_enable = sec._check("启用发现", "_flag_discovery",
                                         "有分类浏览页才勾选")
        self._f_list_url = sec._line("列表入口 URL", "endpoints.discovery.list_url",
                                     "例：/comic/all")
        self._f_cat_url = sec._line("分类入口 URL", "endpoints.discovery.list_categories_url",
                                    "可选：分类页地址，例 /booklist?tag=NTR")
        self._f_pag_type = sec._combo("分页方式", "endpoints.discovery.list_paginator.type",
                                      ["increment"], hint="翻页拼法，多数 increment")
        self._f_pag_param = sec._line("分页参数名", "endpoints.discovery.list_paginator.param",
                                      "例 page；offset 类分页填 offset")
        self._f_pag_start = sec._spin("起始页", "endpoints.discovery.list_paginator.start",
                                      0, 1000, hint="默认 1")
        self._f_pag_step = sec._spin("每页条数", "endpoints.discovery.list_paginator.step",
                                     1, 1000, hint="默认 20")
        self._f_pag_placeholder = sec._line("页码占位符",
                                            "endpoints.discovery.list_paginator.page_placeholder",
                                            "模板类分页：URL 里页码的 {占位}")

        sec._outer.addWidget(_group_label("分类条目字段（页顶分类标签）"))
        self._disc_item_root = sec._line("分类项选择器",
                                         "endpoints.discovery.list_item.root_selector",
                                         "可选：分类标签元素")
        self._f_tag_tpl = sec._line("分类 URL 模板",
                                    "endpoints.discovery.list_item.tag_url_template",
                                    "例 /booklist?tag={tag}")
        self._disc_item_grid = SelectorGrid(None, ["title", "url", "data_val", "cover"])
        sec._outer.addWidget(self._disc_item_grid)

        sec._outer.addWidget(_group_label("作品列表条目字段"))
        self._f_work_root = sec._line("作品项选择器",
                                      "endpoints.discovery.works_list_item.root_selector",
                                      "例 a.card")
        self._f_cover_render = sec._combo(
            "封面渲染", "endpoints.discovery.works_list_item.cover_render",
            ["", "playwright"], hint="封面需 JS 渲染时选")
        self._disc_work_grid = SelectorGrid(
            None, ["title", "url", "cover", "author", "update", "status", "duration", "views"]
        )
        sec._outer.addWidget(self._disc_work_grid)
        self.tabs.addTab(self._wrap_scroll(sec), "发现")

    def _build_search(self) -> None:
        sec = _FormSection()
        sec.add_help(
            "第 4 步 · 搜索",
            [
                "搜索 URL：搜索页地址（如 /search 或 /comic/all）。",
                "关键词参数：看搜索页 URL 问号后的参数名（?keyword=xxx → keyword）。",
                "换页逻辑：分页时页码怎么拼 URL，不同源规则不同，默认自动 ?page=N。",
                "结果项选择器：搜索结果中每个结果卡片的外层元素（F12 右键检查）。",
                "提示：很多站搜索和发现用同一个入口，URL 可跟发现一样。",
            ],
        )
        self._f_search_enable = sec._check("启用搜索", "_flag_search")
        self._f_search_url = sec._line("搜索 URL", "endpoints.search.base_url",
                                       "例：/search 或 /comic/all")
        self._f_search_kw = sec._line("关键词参数", "endpoints.search.keyword_param",
                                      "看 ?keyword=xxx 里的 keyword")
        self._f_search_method = sec._combo("方法", "endpoints.search.method",
                                           ["", "GET", "POST"], hint="多数站 GET")
        self._f_search_page_tpl = sec._line(
            "换页逻辑", "endpoints.search.paginator.url_template",
            "分页 URL 模板：/search?q={keyword}&page={page}；留空自动 ?page=N"
        )
        self._f_search_root = sec._line("结果项选择器", "endpoints.search.item.root_selector",
                                        "例：.dx-novel-list li")
        self._f_search_body_fmt = sec._combo("正文格式", "endpoints.search.body_format",
                                             ["", "form"], hint="POST 搜索填 form")
        sec._outer.addWidget(_group_label("搜索结果条目字段"))
        self._search_item_grid = SelectorGrid(
            None, ["title", "url", "cover", "author", "update", "status", "tags"]
        )
        sec._outer.addWidget(self._search_item_grid)
        self._f_search_render, self._search_rc_specs = self._build_render_group(
            sec, "search"
        )
        self.tabs.addTab(self._wrap_scroll(sec), "搜索")

    def _build_detail(self) -> None:
        sec = _FormSection()
        sec.add_help(
            "第 5 步 · 详情页",
            [
                "打开任意一个作品详情页，右键元素 → 检查（F12），找对应选择器。",
                "标题（必填）：作品大标题元素，如 h1。",
                "封面：注意可能是懒加载（图片真实地址在 data-src 而非 src）。",
                "作者/简介：可选，有就填，没有留空。",
            ],
        )
        self._f_de_title = sec._line("标题选择器", "endpoints.detail.fields.title",
                                     "必填，例 h1")
        self._f_de_author = sec._line("作者选择器", "endpoints.detail.fields.author",
                                      "可选，例 .author")
        self._f_de_cover = sec._line("封面选择器", "endpoints.detail.fields.cover",
                                     "懒加载站注意 data-src")
        self._f_de_summary = sec._line("简介选择器", "endpoints.detail.fields.summary",
                                       "可选，例 .desc")
        self._f_de_status = sec._line("状态选择器", "endpoints.detail.fields.status",
                                      "连载/完结，可选")
        self._f_de_tags = sec._line("标签选择器", "endpoints.detail.fields.tags",
                                    "可选")
        self._f_de_bookname = sec._line("书名选择器", "endpoints.detail.fields.book_name",
                                        "标题在独立元素时填，可选")
        self._f_de_url = sec._line("详情 URL 模板", "endpoints.detail.url_pattern",
                                   "详情页地址规则，例 /book/{id}.html")
        self._f_de_url_sfx = sec._line("详情 URL 后缀", "endpoints.detail.url_suffix",
                                       "少用，留空即可")
        self._f_de_clean = sec._line("标题净化", "endpoints.detail.fields.title_clean",
                                     "去干扰字符，可选")
        self.tabs.addTab(self._wrap_scroll(sec), "详情")

    def _build_content(self) -> None:
        """正文块：按 $type 动态显示 chapter/page/episode。"""
        self.content_tab_widget = QWidget()
        self.content_tab_layout = QVBoxLayout(self.content_tab_widget)
        self.content_tab_layout.setContentsMargins(0, 0, 0, 0)
        self.tabs.insertTab(5, self._wrap_scroll(self.content_tab_widget), "正文")

    def _build_content_forms(self) -> None:
        """按当前 $type 重建正文表单。"""
        ctype = self.type_combo.currentText()
        block = TYPE_CONTENT_BLOCK.get(ctype, "chapter")

        # 清空旧表单
        while self.content_tab_layout.count():
            item = self.content_tab_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        sec = _FormSection()
        if ctype == "novel":
            sec.add_help(
                "第 6 步 · 正文（小说）",
                [
                    "章节项选择器：目录页每个章节的外层元素（F12 检查）。",
                    "章节标题 / URL 选择器：从章节项里取标题和链接。",
                    "正文选择器：点开一章后正文内容所在元素（如 #content p）。",
                ],
            )
            self._c_list_root = sec._line("章节项选择器", f"endpoints.content.{block}.list.root_selector",
                                          "例：a.module-row-text")
            self._c_list_title = sec._line("章节标题选择器", f"endpoints.content.{block}.list.fields.title",
                                           "例：span")
            self._c_list_url = sec._line("章节 URL 选择器", f"endpoints.content.{block}.list.fields.url",
                                         "attr:href")
            self._c_chapters_url = sec._line("目录页 URL", f"endpoints.content.{block}.list.chapters_url",
                                             "正文分页与目录分离时才填，可选")
            self._c_order = sec._combo("章节排序", f"endpoints.content.{block}.list.chapter_order",
                                       ["", "asc", "desc"], hint="默认 asc")
            self._c_title_clean = sec._line("标题净化", f"endpoints.content.{block}.list.title_clean",
                                            "去掉「第1章」等干扰文本，可选")
            self._c_body = sec._line("正文选择器", f"endpoints.content.{block}.body.selector",
                                     "例：#chaptercontent p")
            self._c_body_attr = sec._line("正文属性", f"endpoints.content.{block}.body.attr",
                                          "正文在同节点属性里时填属性名（如 text）")
            self._f_body_render, self._body_rc_specs = self._build_render_group(
                sec, "chapter"
            )
        elif ctype == "comic":
            sec.add_help(
                "第 6 步 · 正文（漫画）",
                [
                    "单图项选择器：图片元素（如 img）。",
                    "图片 URL 选择器：取图片地址；懒加载站真实地址在 data-src。",
                    "详情页即图片页：点开详情直接是图、没有章节列表的图集站要勾选。",
                ],
            )
            self._c_list_root = sec._line("单图项选择器", f"endpoints.content.{block}.list.root_selector",
                                          "例：.comicpage img")
            self._c_list_url = sec._line("图片 URL 选择器", f"endpoints.content.{block}.list.fields.url",
                                         "懒加载站填 data-src")
            self._c_chapters_url = sec._line("目录页 URL", f"endpoints.content.{block}.list.chapters_url",
                                             "图集列表分页独立时才填，可选")
            self._c_order = sec._combo("章节排序", f"endpoints.content.{block}.list.chapter_order",
                                       ["", "asc", "desc"], hint="默认 asc")
            self._c_title_clean = sec._line("标题净化", f"endpoints.content.{block}.list.title_clean",
                                            "去「第X话」干扰文本，可选")
            # 图集站：详情页即图片页（无独立章节列表）
            self._c_single = sec._check("详情页即图片页", f"endpoints.content.{block}.single_chapter",
                                        "图集站必勾")
            self._f_body_render, self._body_rc_specs = self._build_render_group(
                sec, "page"
            )
        else:  # video
            sec.add_help(
                "第 6 步 · 正文（视频）",
                [
                    "集项选择器：剧集列表中每集的外层元素。",
                    "集标题 / URL 选择器：从集项里取标题和播放页链接。",
                    "媒体格式 / 画质：多数站选 hls + best。yt-dlp 站由 api_endpoints 接管。",
                ],
            )
            self._c_list_root = sec._line("集项选择器", f"endpoints.content.{block}.list.root_selector",
                                          "例：.episode-item")
            self._c_list_title = sec._line("集标题选择器", f"endpoints.content.{block}.list.fields.title",
                                           "例：.ep-title")
            self._c_list_url = sec._line("集 URL 选择器", f"endpoints.content.{block}.list.fields.url",
                                         "attr:href")
            self._c_title_clean = sec._line("标题净化", f"endpoints.content.{block}.list.title_clean",
                                            "去「第1集」干扰文本，可选")
            sec._outer.addWidget(_group_label("播放页（取真实播放地址）"))
            self._c_play_sel = sec._line("播放地址选择器",
                                         f"endpoints.content.{block}.play_url.selector",
                                         "例：#play_ a")
            self._c_play_attr = sec._line("播放地址属性",
                                          f"endpoints.content.{block}.play_url.selector.attr",
                                          "默认 href")
            self._c_play_regex = sec._line("播放地址正则",
                                           f"endpoints.content.{block}.play_url.regex",
                                           "页内 JS 提取，例 (http[^\"']+\\.m3u8)")
            self._c_play_suffix = sec._line("地址后缀", f"endpoints.content.{block}.play_url.suffix",
                                            "例 /playlist.m3u8")
            self._f_ep_render, self._ep_rc_specs = self._build_render_group(
                sec, "episode"
            )
            self._c_single = sec._check("单集作品", f"endpoints.content.{block}.single_chapter",
                                        "无剧集列表的作品勾选")
            sec._outer.addWidget(_group_label("分季（series，可选）"))
            self._f_series_enable = sec._check("启用分季", f"endpoints.content.{block}.series.enabled",
                                               "多季作品（如番剧合集）才需要")
            self._f_series_min = sec._spin("最少季数", f"endpoints.content.{block}.series.min_parts",
                                           1, 20, hint="少于该数不算多季")
            self._series_part_map = KeyValueGrid(
                key_hint="季名（如 第一季）", value_hint="季播放页 URL 模板")
            sec._outer.addWidget(self._series_part_map)

            media_sec = _FormSection()
            self._f_media_format = media_sec._combo("媒体格式", "media.format",
                                                    ["hls", "dash", "mp4", "raw"],
                                                    hint="多数站 hls")
            self._f_media_quality = media_sec._combo(
                "选择画质", "media.select.video.quality", ["best", "1080p", "720p", "480p"],
                hint="推荐 best"
            )
            self._f_media_merge_tool = media_sec._combo(
                "合并工具", "media.merge.tool", ["", "ffmpeg"], hint="分段下载需合并")
            self._f_media_output = media_sec._combo(
                "输出格式", "media.merge.output_format", ["", "mp4", "mkv"],
                hint="合并输出容器，默认 mp4")
            media_wrap = QTabWidget()
            media_wrap.addTab(media_sec, "媒体")

            # 换源（source_switch）子页：多播放源站（如 MacCMS）可选配
            switch_sec = _FormSection()
            switch_sec.add_help(
                "换源（可选）",
                [
                    "多播放源站（如 MacCMS 的 ?sid= 切源）才需要填。",
                    "源列表选择器：播放源 tab 元素，含 data-sid 属性。",
                    "分集选择器：含 {sid} 占位，替换成当前源 ID 取该源分集。",
                    "播放配置正则：页面里 player_aaaa={...} 的提取正则。",
                ],
            )
            self._ss_enable = switch_sec._check(
                "启用换源", "_flag_source_switch", "多播放源站才需要"
            )
            self._ss_param = switch_sec._line(
                "换源参数名", "endpoints.content.episode.source_switch.param",
                "例：sid（URL 里 ?sid=）"
            )
            self._ss_list_sel = switch_sec._line(
                "源列表选择器", "endpoints.content.episode.source_switch.list_selector",
                "例：.player_name[data-sid]"
            )
            self._ss_list_attr = switch_sec._line(
                "源ID属性", "endpoints.content.episode.source_switch.list_attr",
                "例：data-sid"
            )
            self._ss_name_sel = switch_sec._line(
                "源名选择器", "endpoints.content.episode.source_switch.name_selector",
                "例：.player_name（可留空用默认）"
            )
            self._ss_ep_list = switch_sec._line(
                "分集选择器", "endpoints.content.episode.source_switch.ep_list_selector",
                "例：#playlist{sid} a[href*='/tv/']"
            )
            self._ss_play_regex = switch_sec._line(
                "播放配置正则", "endpoints.content.episode.source_switch.play_regex",
                "例：var player_aaaa=(\\\\{.*?\\\\})\\\\s*</script>"
            )
            self._ss_pc_url = switch_sec._line(
                "播放器配置URL", "endpoints.content.episode.source_switch.playerconfig_url",
                "可选：加载 ps/parse（如 /static/js/playerconfig.js）"
            )
            media_wrap.addTab(switch_sec, "换源")

            self.content_tab_layout.addWidget(sec)
            self.content_tab_layout.addWidget(media_wrap)
            self.content_media_tabs = media_wrap
            return

        self.content_tab_layout.addWidget(sec)

    def _build_constraints(self) -> None:
        sec = _FormSection()
        sec.add_help(
            "第 7 步 · 爬取限制",
            [
                "搜索/详情/正文的页数和条目上限，控制抓取量，防刷爆站点。",
                "默认值对多数站够用，一般不需要改。",
                "正文最大页/条：章节/图片/剧集数超大时可适当调小。",
            ],
        )
        self._f_search_pages = sec._spin("搜索最大页", "constraints.search.max_pages",
                                         1, 50, hint="默认 1")
        self._f_search_results = sec._spin("搜索最大结果", "constraints.search.max_results",
                                           1, 3000, hint="默认 20")
        self._f_detail_pages = sec._spin("详情最大页", "constraints.detail.max_pages",
                                         1, 50, hint="默认 1")
        self._f_content_pages = sec._spin("正文最大页", "_flag_content_pages",
                                          1, 10000, hint="默认 50")
        self._f_content_items = sec._spin("正文最大条", "_flag_content_items",
                                          1, 50000, hint="默认 500")
        self._f_max_cc = sec._spin("最大并发", "constraints.max_concurrency",
                                   1, 64, hint="留默认即可")
        self._f_detail_tmo = sec._spin("详情单页超时(秒)",
                                       "constraints.detail.timeout_per_page_sec",
                                       1, 300, hint="默认 30")
        self._f_global_tmo = sec._spin("全局抓取超时(秒)",
                                       "constraints.global.total_timeout_sec",
                                       1, 3600, hint="留默认即可")
        self.tabs.addTab(self._wrap_scroll(sec), "限制")

    def _build_diagnostics(self) -> None:
        sec = _FormSection()
        sec.add_help(
            "第 8 步 · 诊断与保存",
            [
                "自检策略：选 soft，站点改版时黄灯提醒，不影响使用。",
                "自检选择器：填列表页上一定存在的元素，用来判断站点有没有改版。",
                "例：.dx-novel-list li、li.gallary_item。",
                "点底部「校验并保存」完成。不确定就先「保存草稿」，不影响现有源。",
            ],
        )
        self._f_sc_strategy = sec._combo("自检策略", "diagnostics.selfcheck.strategy",
                                         ["off", "soft", "strict"], hint="建议 soft")
        self._f_sc_selector = sec._line("自检选择器", "diagnostics.selfcheck.selector",
                                        "例：li.gallary_item")
        self._f_sc_auto = sec._check("连续失败自动禁用",
                                     "diagnostics.selfcheck.auto_disable",
                                     "站点改版自动停用，慎用")
        self._f_sc_failures = sec._spin("失效判定次数",
                                        "diagnostics.selfcheck.max_failures",
                                        1, 100, hint="连续失败 N 次判定失效")
        self._f_sc_hours = sec._spin("自检间隔(小时)",
                                     "diagnostics.selfcheck.interval_hours",
                                     1, 720, hint="默认 24")
        self.tabs.addTab(self._wrap_scroll(sec), "诊断")

    def _build_adblock(self) -> None:
        """广告过滤（ad_block）：源级开关 + 补充规则。"""
        sec = _FormSection()
        sec.add_help(
            "广告过滤",
            [
                "开关：默认开启，框架用内置规则自动过滤视频/图片广告。",
                "URL 广告正则：额外拦截特征，逗号分隔（例：adserver, /gg/, banner2）",
                "广告域名：追加黑名单域名，支持 *.xxx.com（例：ads.xxx.com）",
            ],
        )
        self._f_ad_enable = sec._check("启用广告过滤", "ad_block.enabled", hint="默认开启")
        self._f_ad_regex = sec._line("URL 广告正则", "ad_block.block_url_regex",
                                     "逗号分隔，例：adserver,/gg/")
        self._f_ad_domains = sec._line("广告域名", "ad_block.block_domains",
                                       "逗号分隔，例：ads.xxx.com")
        self.tabs.addTab(self._wrap_scroll(sec), "广告过滤")

    # ================================================================== #
    # JS 渲染配置组（render / render_config）
    # ================================================================== #
    def _build_render_group(self, sec: "_FormSection", mode: str):
        """在 sec 末尾追加「JS 渲染」配置组；返回 (render_combo, specs)。

        render_combo：渲染方式开关（空 / playwright），控制配置组显隐。
        specs: list[[json_key, control, kind]]，供 _apply_render / _load_render 读写。
        mode: chapter / page / episode / search。
        """
        rc_combo = sec._combo(
            "渲染方式", f"_render/{mode}", ["", "playwright"],
            hint="页面由 JS 动态加载时选（Playwright 渲染后再抓）",
        )
        group = QWidget()
        form = QFormLayout(group)
        form.setContentsMargins(16, 4, 16, 8)
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        specs = []

        presets = [p for p in source_presets.RENDER_PRESETS
                   if mode in (p.get("modes") or [])]
        if presets:
            prow = QHBoxLayout()
            prow.setContentsMargins(0, 0, 0, 0)
            plbl = QLabel("常用预设")
            plbl.setStyleSheet("color: palette(text2); font-size: 11px;")
            pcombo = QComboBox()
            pcombo.addItem("")
            for p in presets:
                pcombo.addItem(p["name"])
            pcombo.setStyleSheet("font-size: 11px;")
            pbtn = QPushButton("填入")
            pbtn.setStyleSheet(
                "font-size: 11px; padding: 2px 10px; color: palette(accent);"
                " background: transparent; border: 1px solid palette(midlight);"
                " border-radius: 4px;"
            )

            def _apply_preset() -> None:
                name = pcombo.currentText()
                if not name:
                    return
                for p in presets:
                    if p["name"] == name:
                        self._load_cfg_widgets(specs, p.get("config") or {})
                        self._combo_select(rc_combo, "playwright")
                        break

            pbtn.clicked.connect(_apply_preset)
            prow.addWidget(plbl)
            prow.addWidget(pcombo, 1)
            prow.addWidget(pbtn)
            form.addRow("渲染预设", prow)

        def _line(label, key, hint=""):
            w = QLineEdit()
            w.setStyleSheet("font-size: 11px;")
            w.setPlaceholderText(hint)
            form.addRow(label, w)
            specs.append([key, w, "line"])

        def _spin(label, key, lo, hi, default, hint=""):
            w = QSpinBox()
            w.setRange(lo, hi)
            w.setValue(default)
            w.setSuffix(hint)
            w.setStyleSheet("font-size: 11px;")
            form.addRow(label, w)
            specs.append([key, w, "spin"])

        def _check(label, key, hint=""):
            w = QCheckBox()
            w.setToolTip(hint)
            form.addRow(label, w)
            specs.append([key, w, "check"])

        def _combo(label, key, items):
            w = QComboBox()
            w.addItems(items)
            w.setStyleSheet("font-size: 11px;")
            form.addRow(label, w)
            specs.append([key, w, "combo"])

        if mode == "search":
            _check("交互式搜索", "interact", "回主页自动输关键词提交（SPA 站）")
            _line("主页 URL", "home_url", "交互式搜索用的站点首页")
            _line("输入框选择器", "input_selector", "默认 input[name=wd]")
            _line("等待元素", "wait_for", "搜索结果出现的关键元素")
            _combo("就绪策略", "wait_until", ["", "networkidle", "domcontentloaded", "load"])
            _spin("超时(ms)", "timeout_ms", 1000, 120000, 30000, " ms")
            _spin("额外延迟(ms)", "extra_delay_ms", 0, 60000, 2500, " ms")
            _check("关键词过滤", "filter_keyword", "默认开启：结果须含关键词")
        elif mode == "episode":
            _combo("就绪策略", "wait_until", ["", "networkidle", "domcontentloaded", "load"])
            _spin("超时(ms)", "timeout_ms", 1000, 120000, 45000, " ms")
            _spin("额外延迟(ms)", "extra_delay_ms", 0, 60000, 8000, " ms")
        elif mode == "chapter":
            _line("等待元素", "wait_for", "正文容器出现的关键元素")
            _combo("就绪策略", "wait_until", ["", "networkidle", "domcontentloaded", "load"])
            _spin("超时(ms)", "timeout_ms", 1000, 120000, 30000, " ms")
            _spin("额外延迟(ms)", "extra_delay_ms", 0, 60000, 2000, " ms")
        else:  # page —— 漫画渲染参数最全
            _combo("提取方式", "extract_mode", ["", "canvas", "img", "text"])
            _line("图片选择器", "img_selector", "extract_mode=img 时限定")
            _line("图片 JS 路径", "img_js_path", "求值即得图片数组的 JS，如 window.imgs")
            _line("页面容器选择器", "page_container_selector", "长条图阅读器容器")
            _line("等待元素", "wait_for", "如 canvas；等它出现再抓")
            _combo("就绪策略", "wait_until", ["", "networkidle", "domcontentloaded", "load"])
            _spin("超时(ms)", "timeout_ms", 1000, 120000, 30000, " ms")
            _spin("额外延迟(ms)", "extra_delay_ms", 0, 60000, 2000, " ms")
            _check("滚动到底", "scroll_to_bottom", "长条图需逐段滚动加载")
            _check("滚轮滚动", "wheel_scroll", "用滚轮事件而非滚动条")
            _spin("滚动步长(px)", "scroll_step_px", 100, 30000, 2000, " px")
            _spin("滚动重试轮", "scroll_stale_rounds", 1, 50, 5, " 轮")

        sec._outer.addWidget(group)
        rc_combo.currentTextChanged.connect(
            lambda t, g=group: g.setVisible(bool(t))
        )
        group.setVisible(bool(rc_combo.currentText()))
        return rc_combo, specs

    # ================================================================== #
    # JSON API（api_endpoints）、解密（decryption）、登录（auth）Tab
    # ================================================================== #
    def _build_api(self) -> None:
        """JSON API 站：api_endpoints 的 discovery/search/detail/episode 四端点。"""
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        sec = _FormSection()
        sec.add_help(
            "JSON API · 发现",
            [
                "返回 JSON 而非 HTML 的站点，在这里配接口而不是选择器。",
                "响应路径：列表数据在返回 JSON 里的位置，如 data.result。",
                "字段映射：作品 JSON 里的字段→标准字段名（如 title/url/cover）。",
            ],
        )
        self._api_d_engine = sec._combo("引擎", "api_endpoints.discovery.engine",
                                        ["", "ytdlp"], hint="yt-dlp 接管填 ytdlp")
        self._api_d_url = sec._line("接口 URL", "api_endpoints.discovery.url")
        self._api_d_fb_url = sec._line("兜底 URL", "api_endpoints.discovery.fallback_url",
                                       "主接口失败时用拉取通道")
        self._api_d_rsp = sec._line("响应路径", "api_endpoints.discovery.response_path")
        self._api_d_fb_rsp = sec._line("兜底响应路径",
                                       "api_endpoints.discovery.fallback_response_path")
        sec._outer.addWidget(_group_label("请求参数"))
        self._api_d_params = KeyValueGrid()
        sec._outer.addWidget(_group_label("字段映射"))
        self._api_d_fields = KeyValueGrid(key_hint="标准字段", value_hint="JSON 路径")
        sec._outer.addWidget(_group_label("兜底字段映射"))
        self._api_d_fb_fields = KeyValueGrid(key_hint="标准字段", value_hint="JSON 路径")
        sec._outer.addWidget(_group_label("请求头"))
        self._api_d_headers = KeyValueGrid(key_hint="头名", value_hint="值")
        lay.addWidget(sec)

        sec = _FormSection()
        sec.add_help(
            "JSON API · 搜索",
            [
                "搜索接口返回 JSON 时使用。{keyword}/{page} 可在参数值里当占位。",
            ],
        )
        self._api_s_engine = sec._combo("引擎", "api_endpoints.search.engine",
                                        ["", "ytdlp"], hint="yt-dlp 接管填 ytdlp")
        self._api_s_url = sec._line("接口 URL", "api_endpoints.search.url")
        self._api_s_rsp = sec._line("响应路径", "api_endpoints.search.response_path")
        self._api_s_max = sec._spin("最多结果", "api_endpoints.search.max_results",
                                    0, 500, hint="默认 20")
        sec._outer.addWidget(_group_label("请求参数"))
        self._api_s_params = KeyValueGrid(value_hint="值，可用 {keyword}/{page}")
        sec._outer.addWidget(_group_label("字段映射"))
        self._api_s_fields = KeyValueGrid(key_hint="标准字段", value_hint="JSON 路径")
        lay.addWidget(sec)

        sec = _FormSection()
        sec.add_help(
            "JSON API · 详情",
            [
                "详情接口：返回元数据 + 章节列表。",
                "chapters：章节数组位置（items）与标题字段（title）。",
            ],
        )
        self._api_dt_engine = sec._combo("引擎", "api_endpoints.detail.engine",
                                         ["", "ytdlp"], hint="yt-dlp 接管填 ytdlp")
        self._api_dt_url = sec._line("接口 URL", "api_endpoints.detail.url")
        sec._outer.addWidget(_group_label("请求参数"))
        self._api_dt_params = KeyValueGrid()
        sec._outer.addWidget(_group_label("请求头"))
        self._api_dt_headers = KeyValueGrid(key_hint="头名", value_hint="值")
        sec._outer.addWidget(_group_label("字段提取"))
        self._api_dt_extract = KeyValueGrid(key_hint="标准字段",
                                            value_hint="JSON 路径，如 owner.name")
        sec._outer.addWidget(_group_label("章节列表（chapters）"))
        self._api_dt_ci = sec._line("章节数组路径", "api_endpoints.detail.chapters.items")
        self._api_dt_ct = sec._line("标题字段", "api_endpoints.detail.chapters.title")
        self._api_dt_cc = sec._line("子级路径", "api_endpoints.detail.chapters.children_path")
        self._api_dt_cu = sec._line("URL 模板", "api_endpoints.detail.chapters.url_template")
        lay.addWidget(sec)

        sec = _FormSection()
        sec.add_help(
            "JSON API · 播放",
            [
                "播放接口：返回可播放地址。{quality} 会替换成下方画质映射的实值。",
                "画质映射：参数值如 {quality} 占位时，best/720p 等显示值→接口实值。",
            ],
        )
        self._api_e_engine = sec._combo("引擎", "api_endpoints.episode.engine",
                                        ["", "ytdlp"], hint="yt-dlp 接管填 ytdlp")
        self._api_e_url = sec._line("接口 URL", "api_endpoints.episode.url")
        self._api_e_method = sec._combo("方法", "api_endpoints.episode.method",
                                        ["", "GET", "POST"])
        self._api_e_rsp = sec._line("响应路径", "api_endpoints.episode.response_path")
        sec._outer.addWidget(_group_label("请求参数"))
        self._api_e_params = KeyValueGrid(value_hint="值，可用 {cid}/{quality}")
        sec._outer.addWidget(_group_label("请求头"))
        self._api_e_headers = KeyValueGrid(key_hint="头名", value_hint="值")
        sec._outer.addWidget(_group_label("画质映射（quality）"))
        self._api_e_q_param = sec._line("画质参数名", "api_endpoints.episode.quality.param")
        self._api_e_q_default = sec._line("默认画质", "api_endpoints.episode.quality.default")
        self._api_e_q_map = KeyValueGrid(key_hint="显示画质", value_hint="接口实值")
        sec._outer.addWidget(self._api_e_q_map)
        lay.addWidget(sec)

        lay.addStretch(1)
        self.tabs.addTab(self._wrap_scroll(wrap), "API接口")

    def _build_decrypt(self) -> None:
        """解密配置：decryption.targets 的 image/content/chapter。"""
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        quick = _FormSection()
        quick.add_help(
            "内置解密逻辑（快捷方案）",
            [
                "从下拉选常用解密方案，一键填入下方对应位置；再按目标站密钥实际修改。",
                "「JS 参考」给出可借鉴/可比较的分析片段（不会写入 JSON）。",
            ],
        )
        self._dec_preset = quick._combo(
            "方案", "_preset_decrypt",
            [p["name"] for p in source_presets.DECRYPT_PRESETS],
            hint="选方案后点「填入方案」",
        )
        btnrow = QHBoxLayout()
        btnrow.setContentsMargins(0, 0, 0, 0)
        btnrow.setSpacing(8)
        apply_btn = QPushButton("填入方案")
        apply_btn.setStyleSheet(
            "font-size: 11px; padding: 4px 12px; color: palette(accent);"
            " background: transparent; border: 1px solid palette(midlight);"
            " border-radius: 4px;"
        )
        apply_btn.clicked.connect(lambda: self._action_decrypt_preset(False))
        js_btn = QPushButton("JS 参考")
        js_btn.setStyleSheet(
            "font-size: 11px; padding: 4px 12px; color: palette(mid);"
            " background: transparent; border: 1px solid palette(midlight);"
            " border-radius: 4px;"
        )
        js_btn.clicked.connect(lambda: self._action_decrypt_preset(True))
        btnrow.addWidget(apply_btn)
        btnrow.addWidget(js_btn)
        btnrow.addStretch(1)
        quick._outer.addLayout(btnrow)
        lay.addWidget(quick)

        sec = _FormSection()
        sec.add_help(
            "解密 · 图片（分片加密漫画）",
            [
                "图片字节加密的站（如 AES-CBC 分片）在这里配置。",
                "密钥/IV：UTF-8 明文或 base64；padding 默认 pkcs7。",
            ],
        )
        self._dec_img_strategy = sec._combo("策略", "decryption.targets.image.strategy",
                                            ["", "aes_cbc", "aes_ecb", "xor", "js_custom"])
        self._dec_img_key = sec._line("密钥", "decryption.targets.image.key")
        self._dec_img_iv = sec._line("IV", "decryption.targets.image.iv")
        self._dec_img_padding = sec._combo("填充", "decryption.targets.image.padding",
                                           ["", "pkcs7", "none"])
        self._dec_img_input = sec._line("输入来源", "decryption.targets.image.input")
        self._dec_img_output = sec._line("输出格式", "decryption.targets.image.output")
        self._dec_img_entry = sec._line("JS 入口", "decryption.targets.image.entry",
                                        "js_custom 入口函数，默认 decrypt")
        self._dec_img_script = sec._text("JS 脚本", "decryption.targets.image.script",
                                         hint="js_custom 函数源码")
        self._dec_img_params = sec._line("JS 参数", "decryption.targets.image.params",
                                         'js_custom 参数 JSON，如 {"key":"x"}')
        lay.addWidget(sec)

        sec = _FormSection()
        sec.add_help(
            "解密 · 正文（字符混淆 / 编码）",
            [
                "正文内容混淆的站（base64 / 外置解密接口 / 字库映射 / 自定义 JS）。",
                "custom_endpoint：把正文 POST 到解密接口换明文。",
                "js_custom：填 JS 函数源码，data 为正文串，返回明文。",
            ],
        )
        self._dec_c_strategy = sec._combo(
            "策略", "decryption.targets.content.strategy",
            ["", "base64_decode", "custom_endpoint", "translit", "js_custom"])
        self._dec_c_endpoint = sec._line("解密接口", "decryption.targets.content.endpoint")
        self._dec_c_req = sec._line("请求字段", "decryption.targets.content.request_field")
        self._dec_c_rsp = sec._line("响应字段", "decryption.targets.content.response_field")
        self._dec_c_map = sec._line("映射模块", "decryption.targets.content.map_module")
        self._dec_c_entry = sec._line("JS 入口", "decryption.targets.content.entry",
                                      "js_custom 入口函数，默认 decrypt")
        self._dec_c_script = sec._text("JS 脚本", "decryption.targets.content.script",
                                       hint="js_custom 函数源码")
        self._dec_c_params = sec._line("JS 参数", "decryption.targets.content.params",
                                       'js_custom 参数 JSON，如 {"key":"x"}')
        lay.addWidget(sec)

        sec = _FormSection()
        sec.add_help(
            "解密 · 章节标题（字库混淆 / JS）",
            [
                "章节标题字体混淆的站（如番茄小说字符映射 / 自定义 JS）。",
            ],
        )
        self._dec_ch_strategy = sec._combo("策略", "decryption.targets.chapter.strategy",
                                           ["", "translit", "base64_decode",
                                            "custom_endpoint", "js_custom"])
        self._dec_ch_map = sec._line("映射模块", "decryption.targets.chapter.map_module")
        self._dec_ch_entry = sec._line("JS 入口", "decryption.targets.chapter.entry",
                                       "js_custom 入口函数，默认 decrypt")
        self._dec_ch_script = sec._text("JS 脚本", "decryption.targets.chapter.script",
                                        hint="js_custom 函数源码")
        self._dec_ch_params = sec._line("JS 参数", "decryption.targets.chapter.params",
                                        'js_custom 参数 JSON，如 {"key":"x"}')
        lay.addWidget(sec)

        lay.addStretch(1)
        self.tabs.addTab(self._wrap_scroll(wrap), "解密")

    def _build_auth(self) -> None:
        """登录配置：auth（cookie 检测）。"""
        sec = _FormSection()
        sec.add_help(
            "登录（可选）",
            [
                "需要登录的站：勾选后应用内可打开登录弹窗保存 Cookie。",
                "登录检测：按 Cookie 里的特征字段判断是否已登录。",
            ],
        )
        self._f_auth_required = sec._check("需要登录", "auth.login_required")
        self._f_auth_note = sec._line("说明", "auth.note", "登录方式提示，可选")
        self._f_auth_field = sec._line("登录检测字段", "auth.cookie_check.field",
                                       "Cookie 里代表已登录的键")
        self._f_auth_on = sec._combo("过期处理", "auth.cookie_check.on_expired",
                                     ["", "warn"], hint="Cookie 失效时提醒")
        self._f_auth_min = sec._spin("Cookie 最小长度", "auth.cookie_check.min_length",
                                     0, 100000, hint="0 不校验长度")
        self.tabs.addTab(self._wrap_scroll(sec), "登录")

    # ================================================================== #
    # 渲染配置读写 helpers
    # ================================================================== #
    @staticmethod
    def _combo_select(combo, value: str) -> None:
        """设置 combo 当前值；值不在列表则先追加（兼容自定义值）。"""
        value = "" if value is None else str(value)
        if combo.findText(value) < 0:
            combo.addItem(value)
        combo.setCurrentText(value)

    def _action_decrypt_preset(self, show_js: bool) -> None:
        """解密 Tab 快捷方案按钮：填入参数或查看 JS 参考。"""
        name = self._dec_preset.currentText()
        if not name:
            return
        for p in source_presets.DECRYPT_PRESETS:
            if p["name"] == name:
                if show_js:
                    self._show_decrypt_js(p)
                else:
                    self._apply_decrypt_preset(p)
                break

    def _apply_decrypt_preset(self, preset: dict) -> None:
        """把内置解密方案填入对应 target 的控件（strategy + 参数）。"""
        target = preset.get("target")
        strategy = preset.get("strategy")
        vals = preset.get("values") or {}

        def put(prefix: str, keys) -> None:
            for k in keys:
                w = getattr(self, prefix + k, None)
                if w is None or k not in vals:
                    continue
                if isinstance(w, QComboBox):
                    self._combo_select(w, str(vals[k]))
                elif isinstance(w, QPlainTextEdit):
                    w.setPlainText(str(vals[k]))
                elif k == "params":
                    w.setText(self._fmt_json(vals[k]))
                elif isinstance(w, QLineEdit):
                    w.setText(str(vals[k]))

        if target == "image":
            self._combo_select(self._dec_img_strategy, strategy or "")
            put("_dec_img_", ("key", "iv", "padding", "input", "output",
                              "entry", "script", "params"))
        elif target == "chapter":
            self._combo_select(self._dec_ch_strategy, strategy or "")
            put("_dec_ch_", ("map_module", "entry", "script", "params"))
        else:
            self._combo_select(self._dec_c_strategy, strategy or "")
            put("_dec_c_", ("endpoint", "request_field", "response_field",
                            "map_module", "entry", "script", "params"))

    @staticmethod
    def _show_decrypt_js(preset: dict) -> None:
        """弹窗展示内置 JS 解密参考片段（仅提示，不写入 JSON）。"""
        js = preset.get("js")
        if not js:
            return
        box = QMessageBox()
        box.setWindowTitle(f"JS 参考：{preset.get('name', '')}")
        box.setText(preset.get("note", "") or "")
        box.setDetailedText(js)
        box.exec()

    @staticmethod
    def _collect_cfg_widgets(specs) -> dict:
        """从渲染配置控件收集非空值 dict。"""
        cfg = {}
        for key, w, kind in specs:
            if kind == "line":
                v = w.text().strip()
                if v:
                    cfg[key] = v
            elif kind == "combo":
                v = w.currentText().strip()
                if v:
                    cfg[key] = v
            elif kind in ("spin", "double"):
                cfg[key] = w.value()
            elif kind == "check":
                if w.isChecked():
                    cfg[key] = True
        return cfg

    @staticmethod
    def _load_cfg_widgets(specs, cfg: dict, defaults: dict | None = None) -> None:
        """把 render_config 回填到渲染控件。"""
        defaults = defaults or {}
        cfg = cfg or {}
        for key, w, kind in specs:
            if kind == "line":
                v = cfg.get(key, defaults.get(key, ""))
                w.setText("" if v is None else str(v))
            elif kind == "combo":
                v = cfg.get(key, defaults.get(key, ""))
                SourceEditor._combo_select(w, "" if v is None else str(v))
            elif kind == "spin":
                w.setValue(int(cfg.get(key, defaults.get(key, w.value()))))
            elif kind == "double":
                w.setValue(float(cfg.get(key, defaults.get(key, w.value()))))
            elif kind == "check":
                w.setChecked(bool(cfg.get(key, defaults.get(key, False))))

    @staticmethod
    def _is_deep_empty(value) -> bool:
        """递归判断值是否为空（None / '' / [] / 全空 dict）。"""
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        if isinstance(value, (list, tuple)):
            return all(SourceEditor._is_deep_empty(v) for v in value)
        if isinstance(value, dict):
            return all(SourceEditor._is_deep_empty(v) for v in value.values())
        return False

    def _apply_render(self, root_render: dict, root_cfg: dict, render_w, specs,
                      cfg_key: str = "render_config") -> None:
        """把渲染控件写回：render 写 root_render，render_config 写 root_cfg。

        render 与 config 不同层时（search.render / search.item.render_config、
        episode.play_url.render / episode.render_config）两个 root 分开传。
        未启用渲染则移除 render，且不触碰 render_config（保留原值）。
        """
        if render_w.currentText() == "playwright":
            root_render["render"] = "playwright"
            cfg = self._collect_cfg_widgets(specs)
            if cfg:
                merged = dict(root_cfg.get(cfg_key) or {})
                merged.update(cfg)
                root_cfg[cfg_key] = merged
            else:
                root_cfg.pop(cfg_key, None)
        else:
            root_render.pop("render", None)

    @staticmethod
    def _load_render(root_render: dict, root_cfg: dict, render_w, specs,
                     cfg_key: str = "render_config") -> None:
        """把 render / render_config 回填到渲染控件。"""
        SourceEditor._combo_select(render_w, root_render.get("render") or "")
        SourceEditor._load_cfg_widgets(specs, root_cfg.get(cfg_key) or {})

    # ================================================================== #
    # 示例填充
    # ================================================================== #
    # 各类型参考源（用真实存在的源作为示例模板）
    _EXAMPLE_SOURCE = {
        "novel": "23qb",
        "comic": "18mh",
        "video": "bilibili",
    }

    def _fill_example(self) -> None:
        """用同类型的参考源填充当前空白字段（仅新建模式）。

        成功不弹窗（按钮即反馈），失败才提示。
        """
        ctype = self.type_combo.currentText()
        sid = self._EXAMPLE_SOURCE.get(ctype)
        if not sid:
            return
        # 从 sources 目录找参考源
        directory = self._resolve_sources_dir()
        path = directory / f"{sid}.json"
        if not path.exists():
            self.status_label.setText(f"⚠ 参考源 {sid}.json 不存在")
            return
        try:
            example = load_source(path).raw
        except ConfigError as exc:
            QMessageBox.warning(self, "示例加载失败", str(exc))
            return
        # 只填当前为空的字段（不覆盖用户已填内容）
        self._fill_blank_from(example)
        self.status_label.setText(f"已填入示例「{sid}」，请按目标站点实际检查选择器")

    def _fill_blank_from(self, example: dict) -> None:
        """把 example 里对应路径的值填入当前为空/默认的控件。"""

        def as_str(val):
            """把值转成可填入 QLineEdit 的字符串。selector dict → css。"""
            if isinstance(val, dict):
                return val.get("css", "")
            return str(val) if val is not None else ""

        mappings = [
            (self._f_base, "transports.base_url"),
            (self._f_name, "$name"),
            (self._f_home, "$metadata.homepage"),
            (self._f_desc, "$metadata.description"),
            (self._f_list_url, "endpoints.discovery.list_url"),
            (self._f_search_url, "endpoints.search.base_url"),
            (self._f_search_kw, "endpoints.search.keyword_param"),
            (self._f_search_root, "endpoints.search.item.root_selector"),
            (self._f_de_title, "endpoints.detail.fields.title"),
            (self._f_de_author, "endpoints.detail.fields.author"),
            (self._f_de_cover, "endpoints.detail.fields.cover"),
            (self._f_de_summary, "endpoints.detail.fields.summary"),
            (self._f_sc_selector, "diagnostics.selfcheck.selector"),
            (self._f_icon, "$metadata.icon"),
            (self._f_lang, "$metadata.lang"),
            (self._f_region, "$metadata.region"),
            (self._f_cat_url, "endpoints.discovery.list_categories_url"),
            (self._f_pag_param, "endpoints.discovery.list_paginator.param"),
            (self._f_work_root, "endpoints.discovery.works_list_item.root_selector"),
        ]
        for widget, path in mappings:
            if not widget.text().strip():
                widget.setText(as_str(_deep_get(example, path, "")))
        # 启用勾选框：示例有对应块则勾选
        if not self._f_disc_enable.isChecked() and _deep_get(example, "endpoints.discovery", None):
            self._f_disc_enable.setChecked(True)
        if not self._f_search_enable.isChecked() and _deep_get(example, "endpoints.search", None):
            self._f_search_enable.setChecked(True)
        # 正文块（按当前类型）
        ctype = self.type_combo.currentText()
        block = TYPE_CONTENT_BLOCK.get(ctype, "chapter")
        prefix = f"endpoints.content.{block}"
        if hasattr(self, "_c_list_root") and not self._c_list_root.text().strip():
            self._c_list_root.setText(
                as_str(_deep_get(example, f"{prefix}.list.root_selector", ""))
            )
        if ctype == "novel" and hasattr(self, "_c_body") and not self._c_body.text().strip():
            self._c_body.setText(
                as_str(_deep_get(example, f"{prefix}.body.selector", ""))
            )
        if ctype == "comic" and hasattr(self, "_c_single"):
            if _deep_get(example, f"{prefix}.single_chapter", False):
                self._c_single.setChecked(True)
        # 网络
        if not self._f_ua.text().strip():
            self._f_ua.setText(
                _deep_get(example, "transports.headers", {}).get("User-Agent", "")
            )
        ex_headers = _deep_get(example, "transports.headers", {}) or {}
        for w, key in ((self._f_referer, "Referer"),
                       (self._f_accept, "Accept"),
                       (self._f_accept_lang, "Accept-Language")):
            if not w.text().strip():
                w.setText(ex_headers.get(key, ""))

    # ================================================================== #
    # 表单 → dict / 加载 / 保存
    # ================================================================== #
    def _load_into_form(self) -> None:
        """从 self._raw 填充到各表单控件。"""
        raw = self._raw
        # 基本信息
        self.id_edit.setText(raw.get("$id", ""))
        self._f_name.setText(_deep_get(raw, "$name", ""))
        self._f_home.setText(_deep_get(raw, "$metadata.homepage", ""))
        self._f_desc.setText(_deep_get(raw, "$metadata.description", ""))
        tags = _deep_get(raw, "$metadata.tags", []) or []
        self._f_tags.setText(", ".join(tags))
        self._f_icon.setText(_deep_get(raw, "$metadata.icon", ""))
        self._f_lang.setText(_deep_get(raw, "$metadata.lang", ""))
        self._f_region.setText(_deep_get(raw, "$metadata.region", ""))
        self._f_weight.setValue(float(_deep_get(raw, "$weight", 1.0)))

        # 网络
        self._f_base.setText(_deep_get(raw, "transports.base_url", ""))
        headers = _deep_get(raw, "transports.headers", {}) or {}
        self._f_ua.setText(headers.get("User-Agent", ""))
        self._f_referer.setText(headers.get("Referer", ""))
        self._f_accept.setText(headers.get("Accept", ""))
        self._f_accept_lang.setText(headers.get("Accept-Language", ""))
        self._f_cookie.setText(_deep_get(raw, "transports.cookie", ""))
        self._f_follow.setChecked(
            bool(_deep_get(raw, "transports.follow_redirects", True))
        )
        self._f_charset.setText(_deep_get(raw, "transports.charset", ""))
        self._f_timeout.setValue(int(_deep_get(raw, "transports.timeout", 10)))
        self._f_retries.setValue(int(_deep_get(raw, "transports.retries", 3)))
        self._f_interval.setValue(int(_deep_get(raw, "transports.interval_ms", 500)))
        rb = _deep_get(raw, "transports.retry_backoff", {}) or {}
        self._f_rb_base.setValue(float(rb.get("base_sec", rb.get("base", 0.0))))
        self._f_rb_max.setValue(float(rb.get("max_sec", rb.get("max", 0.0))))
        self._f_rb_jitter.setValue(float(rb.get("jitter", 0.0)))

        # 发现
        disc = _deep_get(raw, "endpoints.discovery", None)
        self._f_disc_enable.setChecked(disc is not None)
        self._f_list_url.setText(_deep_get(raw, "endpoints.discovery.list_url", ""))
        self._f_cat_url.setText(_deep_get(raw, "endpoints.discovery.list_categories_url", ""))
        pag = _deep_get(raw, "endpoints.discovery.list_paginator", {}) or {}
        self._combo_select(self._f_pag_type, pag.get("type") or "increment")
        self._f_pag_param.setText(pag.get("param", ""))
        self._f_pag_start.setValue(int(pag.get("start", 1)))
        self._f_pag_step.setValue(int(pag.get("step", 20)))
        self._f_pag_placeholder.setText(pag.get("page_placeholder", ""))
        li = _deep_get(raw, "endpoints.discovery.list_item", {}) or {}
        self._disc_item_root.setText(
            _deep_get(rootish(raw, "endpoints.discovery.list_item.root_selector"), "css", "")
        )
        self._f_tag_tpl.setText(li.get("tag_url_template", ""))
        self._disc_item_grid.set_entries(li.get("fields") or {})
        wl = _deep_get(raw, "endpoints.discovery.works_list_item", {}) or {}
        self._f_work_root.setText(
            _deep_get(rootish(raw, "endpoints.discovery.works_list_item.root_selector"), "css", "")
        )
        self._combo_select(self._f_cover_render, wl.get("cover_render") or "")
        self._disc_work_grid.set_entries(wl.get("fields") or {})

        # 搜索
        ssearch = _deep_get(raw, "endpoints.search", None)
        self._f_search_enable.setChecked(ssearch is not None)
        self._f_search_url.setText(_deep_get(raw, "endpoints.search.base_url", ""))
        self._f_search_kw.setText(_deep_get(raw, "endpoints.search.keyword_param", ""))
        self._f_search_method.setCurrentText(_deep_get(raw, "endpoints.search.method", ""))
        self._f_search_root.setText(_deep_get(rootish(raw, "endpoints.search.item.root_selector"), "css", ""))
        self._f_search_page_tpl.setText(
            _deep_get(raw, "endpoints.search.paginator.url_template", "")
        )
        self._combo_select(self._f_search_body_fmt,
                           _deep_get(raw, "endpoints.search.body_format", ""))
        s_item = (ssearch or {}).get("item") or {}
        self._search_item_grid.set_entries(s_item.get("fields") or {})
        self._load_render(ssearch or {}, s_item, self._f_search_render,
                          self._search_rc_specs)

        # 详情
        self._f_de_title.setText(_deep_get(rootish(raw, "endpoints.detail.fields.title"), "css", ""))
        self._f_de_author.setText(_deep_get(rootish(raw, "endpoints.detail.fields.author"), "css", ""))
        self._f_de_cover.setText(_deep_get(rootish(raw, "endpoints.detail.fields.cover"), "css", ""))
        self._f_de_summary.setText(_deep_get(rootish(raw, "endpoints.detail.fields.summary"), "css", ""))
        self._f_de_status.setText(_deep_get(rootish(raw, "endpoints.detail.fields.status"), "css", ""))
        self._f_de_tags.setText(_deep_get(rootish(raw, "endpoints.detail.fields.tags"), "css", ""))
        self._f_de_bookname.setText(_deep_get(rootish(raw, "endpoints.detail.fields.book_name"), "css", ""))
        self._f_de_url.setText(_deep_get(raw, "endpoints.detail.url_pattern", ""))
        self._f_de_url_sfx.setText(_deep_get(raw, "endpoints.detail.url_suffix", ""))
        tc = _deep_get(raw, "endpoints.detail.fields.title_clean", "")
        if isinstance(tc, dict):
            tc = tc.get("css", "")
        elif isinstance(tc, list):
            tc = json.dumps(tc, ensure_ascii=False)
        self._f_de_clean.setText("" if tc is None else str(tc))

        # 限制
        self._f_search_pages.setValue(int(_deep_get(raw, "constraints.search.max_pages", 1)))
        self._f_search_results.setValue(int(_deep_get(raw, "constraints.search.max_results", 20)))
        self._f_detail_pages.setValue(int(_deep_get(raw, "constraints.detail.max_pages", 1)))
        ctype = self.type_combo.currentText()
        block = TYPE_CONTENT_BLOCK.get(ctype, "chapter")
        self._f_content_pages.setValue(int(_deep_get(raw, f"constraints.{block}.max_pages", 50)))
        self._f_content_items.setValue(int(_deep_get(raw, f"constraints.{block}.max_items", 500)))
        self._f_max_cc.setValue(int(_deep_get(raw, "constraints.max_concurrency", 8)))
        self._f_detail_tmo.setValue(
            int(_deep_get(raw, "constraints.detail.timeout_per_page_sec", 30))
        )
        self._f_global_tmo.setValue(
            int(_deep_get(raw, "constraints.global.total_timeout_sec", 600))
        )

        # 诊断
        self._f_sc_strategy.setCurrentText(_deep_get(raw, "diagnostics.selfcheck.strategy", "soft"))
        self._f_sc_selector.setText(_deep_get(rootish(raw, "diagnostics.selfcheck.selector"), "css", ""))
        self._f_sc_auto.setChecked(
            bool(_deep_get(raw, "diagnostics.selfcheck.auto_disable", False))
        )
        self._f_sc_failures.setValue(
            int(_deep_get(raw, "diagnostics.selfcheck.max_failures", 3))
        )
        self._f_sc_hours.setValue(
            int(_deep_get(raw, "diagnostics.selfcheck.interval_hours", 24))
        )

        # 广告过滤
        ad_enabled = _deep_get(raw, "ad_block.enabled", False)
        self._f_ad_enable.setChecked(bool(ad_enabled))
        self._f_ad_regex.setText(", ".join(_deep_get(raw, "ad_block.block_url_regex", []) or []))
        self._f_ad_domains.setText(", ".join(_deep_get(raw, "ad_block.block_domains", []) or []))

        # 正文（按类型重建并填充）
        self._build_content_forms()
        self._load_content_fields(raw)

        # API接口 / 解密 / 登录
        self._load_api_fields(raw)
        self._load_decrypt_fields(raw)
        self._load_auth_fields(raw)

        self._update_status_label()

    def _build_dict(self) -> dict:
        """把表单值写回一个深拷贝，返回完整 dict。

        用深拷贝：不污染 self._raw（保证多次编辑/草稿保存取值稳定）。
        """
        import copy

        raw = copy.deepcopy(self._raw)

        # 基本信息
        raw["$id"] = self.id_edit.text().strip()
        _deep_set(raw, "$name", self._f_name.text())
        _deep_set(raw, "$metadata.homepage", self._f_home.text())
        _deep_set(raw, "$metadata.description", self._f_desc.text())
        tags = [t.strip() for t in self._f_tags.text().split(",") if t.strip()]
        metadata = raw.setdefault("$metadata", {})
        metadata["tags"] = tags
        raw["$weight"] = self._f_weight.value()
        raw["$type"] = self.type_combo.currentText()
        if not raw.get("$schema_version"):
            raw["$schema_version"] = 2

        # 网络
        _deep_set(raw, "transports.base_url", self._f_base.text())
        headers = raw.setdefault("transports", {}).setdefault("headers", {})
        ua = self._f_ua.text().strip()
        if ua:
            headers["User-Agent"] = ua
        elif "User-Agent" in headers:
            del headers["User-Agent"]
        self._apply_header(headers, "Referer", self._f_referer)
        self._apply_header(headers, "Accept", self._f_accept)
        self._apply_header(headers, "Accept-Language", self._f_accept_lang)
        _deep_set(raw, "transports.cookie", self._f_cookie.text())
        _deep_set(raw, "transports.timeout", self._f_timeout.value())
        _deep_set(raw, "transports.retries", self._f_retries.value())
        _deep_set(raw, "transports.interval_ms", self._f_interval.value())
        _deep_set(raw, "transports.follow_redirects", self._f_follow.isChecked())
        self._apply_text(raw.setdefault("transports", {}), "charset",
                         self._f_charset.text())
        rb = raw.setdefault("transports", {}).setdefault("retry_backoff", {})
        rb["base_sec"] = self._f_rb_base.value()
        rb["max_sec"] = self._f_rb_max.value()
        rb["jitter"] = round(self._f_rb_jitter.value(), 4)

        # 发现（未勾选则移除）
        if self._f_disc_enable.isChecked():
            endpoints = raw.setdefault("endpoints", {})
            disc = endpoints.setdefault("discovery", {})
            _deep_set(disc, "list_url", self._f_list_url.text())
            _deep_set(disc, "list_categories_url", self._f_cat_url.text())
            pag = disc.setdefault("list_paginator", {})
            pag["type"] = self._f_pag_type.currentText() or "increment"
            self._apply_text(pag, "param", self._f_pag_param.text())
            pag["start"] = self._f_pag_start.value()
            pag["step"] = self._f_pag_step.value()
            self._apply_text(pag, "page_placeholder", self._f_pag_placeholder.text())
            li = disc.setdefault("list_item", {})
            self._apply_css(li, "root_selector", self._disc_item_root)
            self._apply_text(li, "tag_url_template", self._f_tag_tpl.text())
            self._apply_grid_fields(li, self._disc_item_grid)
            wl = disc.setdefault("works_list_item", {})
            self._apply_css(wl, "root_selector", self._f_work_root)
            self._apply_text(wl, "cover_render", self._f_cover_render.currentText())
            self._apply_grid_fields(wl, self._disc_work_grid)
        else:
            if "endpoints" in raw:
                raw["endpoints"].pop("discovery", None)

        # 搜索
        if self._f_search_enable.isChecked():
            endpoints = raw.setdefault("endpoints", {})
            search = endpoints.setdefault("search", {})
            _deep_set(search, "base_url", self._f_search_url.text())
            _deep_set(search, "keyword_param", self._f_search_kw.text())
            _deep_set(search, "method", self._f_search_method.currentText())
            self._apply_text(search, "body_format", self._f_search_body_fmt.currentText())
            sitem = search.setdefault("item", {})
            root_sel = self._f_search_root.text().strip()
            if root_sel:
                sitem["root_selector"] = {"css": root_sel}
            elif "root_selector" in sitem:
                del sitem["root_selector"]
            self._apply_grid_fields(sitem, self._search_item_grid)
            self._apply_render(search, sitem, self._f_search_render,
                               self._search_rc_specs)
            # 换页逻辑（分页 URL 模板）：留空则删除，避免残留空 paginator
            tpl = self._f_search_page_tpl.text().strip()
            if tpl:
                search.setdefault("paginator", {})["url_template"] = tpl
            elif "paginator" in search:
                search["paginator"].pop("url_template", None)
                if not search["paginator"]:
                    del search["paginator"]
        else:
            if "endpoints" in raw:
                raw["endpoints"].pop("search", None)

        # 详情
        detail = raw.setdefault("endpoints", {}).setdefault("detail", {}).setdefault("fields", {})
        self._apply_selector(detail, "title", self._f_de_title.text())
        self._apply_selector(detail, "author", self._f_de_author.text())
        self._apply_selector(detail, "cover", self._f_de_cover.text())
        self._apply_selector(detail, "summary", self._f_de_summary.text())
        self._apply_selector(detail, "status", self._f_de_status.text())
        self._apply_selector(detail, "tags", self._f_de_tags.text())
        self._apply_selector(detail, "book_name", self._f_de_bookname.text())
        tco = detail.get("title_clean")
        if isinstance(tco, (bool, list)):
            pass  # 异构形态无表单表达，保留原值
        else:
            self._apply_selector(detail, "title_clean", self._f_de_clean.text())
        # URL 模板/后缀属于 detail 层（非 fields），是字符串模板而非 selector
        det_top = raw.setdefault("endpoints", {}).setdefault("detail", {})
        self._apply_text(det_top, "url_pattern", self._f_de_url.text())
        self._apply_text(det_top, "url_suffix", self._f_de_url_sfx.text())
        # 清理历史版本误写入 fields 的 url_pattern/url_suffix
        detail.pop("url_pattern", None)
        detail.pop("url_suffix", None)

        # 限制
        ctype = self.type_combo.currentText()
        block = TYPE_CONTENT_BLOCK.get(ctype, "chapter")
        _deep_set(raw, "constraints.search.max_pages", self._f_search_pages.value())
        _deep_set(raw, "constraints.search.max_results", self._f_search_results.value())
        _deep_set(raw, "constraints.detail.max_pages", self._f_detail_pages.value())
        _deep_set(raw, f"constraints.{block}.max_pages", self._f_content_pages.value())
        _deep_set(raw, f"constraints.{block}.max_items", self._f_content_items.value())
        _deep_set(raw, "constraints.max_concurrency", self._f_max_cc.value())
        _deep_set(raw, "constraints.detail.timeout_per_page_sec",
                  self._f_detail_tmo.value())
        _deep_set(raw, "constraints.global.total_timeout_sec", self._f_global_tmo.value())

        # 诊断（自检）
        # 仅当源已配置自检、或表单填入了策略/选择器时才保留块，
        # 避免每次保存给所有源注入默认诊断配置。
        old_sc = raw.get("diagnostics", {}).get("selfcheck")
        diag = raw.setdefault("diagnostics", {})
        sc = diag.setdefault("selfcheck", {})
        strategy = self._f_sc_strategy.currentText() or ""
        if old_sc and "strategy" in old_sc:
            sc["strategy"] = self._f_sc_strategy.currentText() or "soft"
        elif strategy:
            sc["strategy"] = strategy
        elif "strategy" in sc:
            sc.pop("strategy", None)
        self._apply_css(sc, "selector", self._f_sc_selector)
        if old_sc and "auto_disable" in old_sc:
            sc["auto_disable"] = self._f_sc_auto.isChecked()
        elif self._f_sc_auto.isChecked():
            sc["auto_disable"] = True
        else:
            sc.pop("auto_disable", None)
        if old_sc and "max_failures" in old_sc:
            sc["max_failures"] = self._f_sc_failures.value()
        elif self._f_sc_failures.value() != 3:
            sc["max_failures"] = self._f_sc_failures.value()
        else:
            sc.pop("max_failures", None)
        if old_sc and "interval_hours" in old_sc:
            sc["interval_hours"] = self._f_sc_hours.value()
        elif self._f_sc_hours.value() != 24:
            sc["interval_hours"] = self._f_sc_hours.value()
        else:
            sc.pop("interval_hours", None)
        if old_sc is None and not sc:
            diag.pop("selfcheck", None)
        if not diag and "diagnostics" in raw:
            raw.pop("diagnostics", None)

        # 正文（按类型）
        content = raw.setdefault("endpoints", {}).setdefault("content", {})
        # 只保留当前类型对应的块，避免切换类型后新旧块累积共存
        for k in [k for k in content if k != block]:
            content.pop(k)
        b = content.setdefault(block, {})
        self._apply_selector_list(b, self._c_list_root, self._c_list_url)
        lst = b.setdefault("list", {})
        if hasattr(self, "_c_chapters_url"):
            self._apply_text(lst, "chapters_url", self._c_chapters_url.text())
        if hasattr(self, "_c_order"):
            sfx = self._c_order.currentText().strip()
            if sfx:
                lst["chapter_order"] = sfx
            else:
                lst.pop("chapter_order", None)
        if hasattr(self, "_c_title_clean"):
            tco = lst.get("title_clean")
            if isinstance(tco, (bool, list)):
                pass  # 异构形态（布尔开关/排除列表）无表单表达，保留原值
            else:
                self._apply_text(lst, "title_clean", self._c_title_clean.text())
        if ctype == "novel":
            self._apply_selector(lst.setdefault("fields", {}), "title",
                                 self._c_list_title.text())
            body = b.setdefault("body", {})
            body_css = self._c_body.text().strip()
            if body_css:
                body["selector"] = {"css": body_css}
            elif "selector" in body:
                del body["selector"]
            if hasattr(self, "_c_body_attr"):
                self._apply_text(body, "attr", self._c_body_attr.text())
            self._apply_render(body, body, self._f_body_render, self._body_rc_specs)
        elif ctype == "comic":
            if hasattr(self, "_c_single"):
                if self._c_single.isChecked():
                    b["single_chapter"] = True
                else:
                    b.pop("single_chapter", None)
            body = b.setdefault("body", {})
            self._apply_render(body, body, self._f_body_render, self._body_rc_specs)
        elif ctype == "video":
            self._apply_selector(lst.setdefault("fields", {}), "title",
                                 self._c_list_title.text())
            _deep_set(raw, "media.format", self._f_media_format.currentText())
            _deep_set(raw, "media.select.video.quality",
                      self._f_media_quality.currentText())
            merge = raw.setdefault("media", {}).setdefault("merge", {})
            self._apply_text(merge, "tool", self._f_media_merge_tool.currentText())
            self._apply_text(merge, "output_format",
                             self._f_media_output.currentText())
            play = b.setdefault("play_url", {})
            psel = self._c_play_sel.text().strip()
            pattr = self._c_play_attr.text().strip()
            old_sel = play.get("selector")
            if psel or pattr:
                if isinstance(old_sel, dict):
                    sel = old_sel
                else:
                    sel = play["selector"] = {}
                if psel:
                    sel["css"] = psel
                else:
                    sel.pop("css", None)
                if pattr:
                    sel["attr"] = pattr
                else:
                    sel.pop("attr", None)
            elif isinstance(old_sel, dict):
                # 表单未填主选择器：selector 还带有 fallback 等键时保留，否则删除
                leftover = {k: v for k, v in old_sel.items() if k not in ("css", "attr")}
                if leftover:
                    old_sel.pop("css", None)
                    old_sel.pop("attr", None)
                else:
                    play.pop("selector", None)
            self._apply_text(play, "regex", self._c_play_regex.text())
            self._apply_text(play, "suffix", self._c_play_suffix.text())
            self._apply_render(play, b, self._f_ep_render, self._ep_rc_specs)
            if hasattr(self, "_c_single"):
                if self._c_single.isChecked():
                    b["single_chapter"] = True
                else:
                    b.pop("single_chapter", None)
            # 分季（series）
            if hasattr(self, "_f_series_enable"):
                if self._f_series_enable.isChecked():
                    ser = b.setdefault("series", {})
                    ser["min_parts"] = self._f_series_min.value()
                    pm = self._series_part_map.data()
                    if pm:
                        ser["part_map"] = pm
                    else:
                        ser.pop("part_map", None)
                else:
                    b.pop("series", None)
            # 换源（source_switch）：勾选且至少填了关键字段才写入
            ss = {}
            if hasattr(self, "_ss_enable") and self._ss_enable.isChecked():
                _deep_set(ss, "param", self._ss_param.text())
                _deep_set(ss, "list_selector", self._ss_list_sel.text())
                _deep_set(ss, "list_attr", self._ss_list_attr.text())
                _deep_set(ss, "name_selector", self._ss_name_sel.text())
                _deep_set(ss, "ep_list_selector", self._ss_ep_list.text())
                _deep_set(ss, "play_regex", self._ss_play_regex.text())
                _deep_set(ss, "playerconfig_url", self._ss_pc_url.text())
            ep_block = content.setdefault("episode", {})
            if ss and ss.get("list_selector") and ss.get("ep_list_selector"):
                ep_block["source_switch"] = self._keep_extra(
                    ep_block.get("source_switch"), ss
                )
            elif "source_switch" in ep_block:
                del ep_block["source_switch"]

        # 清理空壳：fields 无任何字段时删除，避免留下 {}
        if "fields" in lst and not lst["fields"]:
            del lst["fields"]

        # 广告过滤
        orig_ad = raw.get("ad_block")
        if self._f_ad_enable.isChecked():
            ad = raw.setdefault("ad_block", {})
            ad["enabled"] = True
            rex = [r.strip() for r in self._f_ad_regex.text().split(",") if r.strip()]
            if rex:
                ad["block_url_regex"] = rex
            else:
                ad.pop("block_url_regex", None)
            doms = [d.strip() for d in self._f_ad_domains.text().split(",") if d.strip()]
            if doms:
                ad["block_domains"] = doms
            else:
                ad.pop("block_domains", None)
        elif isinstance(orig_ad, dict):
            # 取消启用：保留原规则但置为关闭；无规则的纯 enabled 块直接移除
            if "block_url_regex" in orig_ad or "block_domains" in orig_ad:
                raw["ad_block"] = {"enabled": False}
            else:
                raw.pop("ad_block", None)
        else:
            raw.pop("ad_block", None)

        # API接口（api_endpoints）：空则移除；表单未覆盖键（如 categories）保留
        prev_api = raw.get("api_endpoints")
        api = {}
        d0 = {}
        self._apply_text(d0, "engine", self._api_d_engine.currentText())
        self._apply_text(d0, "url", self._api_d_url.text())
        self._apply_text(d0, "fallback_url", self._api_d_fb_url.text())
        self._apply_text(d0, "response_path", self._api_d_rsp.text())
        self._apply_text(d0, "fallback_response_path", self._api_d_fb_rsp.text())
        d0["params"] = self._api_d_params.data()
        d0["item_fields"] = self._api_d_fields.data()
        d0["fallback_item_fields"] = self._api_d_fb_fields.data()
        d0["headers"] = self._api_d_headers.data()
        d0 = self._keep_extra(
            prev_api.get("discovery") if isinstance(prev_api, dict) else None, d0
        )
        if not self._is_deep_empty(d0):
            api["discovery"] = d0
        s0 = {}
        self._apply_text(s0, "engine", self._api_s_engine.currentText())
        self._apply_text(s0, "url", self._api_s_url.text())
        self._apply_text(s0, "response_path", self._api_s_rsp.text())
        if self._api_s_max.value() > 0:
            s0["max_results"] = self._api_s_max.value()
        s0["params"] = self._api_s_params.data()
        s0["item_fields"] = self._api_s_fields.data()
        s0 = self._keep_extra(
            prev_api.get("search") if isinstance(prev_api, dict) else None, s0
        )
        if not self._is_deep_empty(s0):
            api["search"] = s0
        t0 = {}
        self._apply_text(t0, "engine", self._api_dt_engine.currentText())
        self._apply_text(t0, "url", self._api_dt_url.text())
        t0["params"] = self._api_dt_params.data()
        t0["headers"] = self._api_dt_headers.data()
        t0["field_extractors"] = self._api_dt_extract.data()
        ch0 = {}
        self._apply_text(ch0, "items", self._api_dt_ci.text())
        self._apply_text(ch0, "title", self._api_dt_ct.text())
        self._apply_text(ch0, "children_path", self._api_dt_cc.text())
        self._apply_text(ch0, "url_template", self._api_dt_cu.text())
        if not self._is_deep_empty(ch0):
            t0["chapters"] = ch0
        t0 = self._keep_extra(
            prev_api.get("detail") if isinstance(prev_api, dict) else None, t0
        )
        if not self._is_deep_empty(t0):
            api["detail"] = t0
        e0 = {}
        self._apply_text(e0, "engine", self._api_e_engine.currentText())
        self._apply_text(e0, "url", self._api_e_url.text())
        self._apply_text(e0, "method", self._api_e_method.currentText())
        self._apply_text(e0, "response_path", self._api_e_rsp.text())
        e0["params"] = self._api_e_params.data()
        e0["headers"] = self._api_e_headers.data()
        q0 = {}
        self._apply_text(q0, "param", self._api_e_q_param.text())
        self._apply_text(q0, "default", self._api_e_q_default.text())
        qm = self._api_e_q_map.data()
        if qm:
            q0["map"] = qm
        if not self._is_deep_empty(q0):
            e0["quality"] = q0
        e0 = self._keep_extra(
            prev_api.get("episode") if isinstance(prev_api, dict) else None, e0
        )
        if not self._is_deep_empty(e0):
            api["episode"] = e0
        if api:
            raw["api_endpoints"] = api
        else:
            raw.pop("api_endpoints", None)

        # 解密（decryption）
        prev_dec = raw.get("decryption")
        prev_t = prev_dec.get("targets") if isinstance(prev_dec, dict) else prev_dec
        targets = {}
        img0 = {}
        self._apply_text(img0, "strategy", self._dec_img_strategy.currentText())
        self._apply_text(img0, "key", self._dec_img_key.text())
        self._apply_text(img0, "iv", self._dec_img_iv.text())
        self._apply_text(img0, "padding", self._dec_img_padding.currentText())
        self._apply_text(img0, "input", self._dec_img_input.text())
        self._apply_text(img0, "output", self._dec_img_output.text())
        self._apply_text(img0, "entry", self._dec_img_entry.text())
        self._apply_script(img0, "script", self._dec_img_script)
        self._apply_json(img0, "params", self._dec_img_params.text())
        if not self._is_deep_empty(img0):
            targets["image"] = self._keep_extra(
                prev_t.get("image") if isinstance(prev_t, dict) else None, img0
            )
        c0 = {}
        self._apply_text(c0, "strategy", self._dec_c_strategy.currentText())
        self._apply_text(c0, "endpoint", self._dec_c_endpoint.text())
        self._apply_text(c0, "request_field", self._dec_c_req.text())
        self._apply_text(c0, "response_field", self._dec_c_rsp.text())
        self._apply_text(c0, "map_module", self._dec_c_map.text())
        self._apply_text(c0, "entry", self._dec_c_entry.text())
        self._apply_script(c0, "script", self._dec_c_script)
        self._apply_json(c0, "params", self._dec_c_params.text())
        if not self._is_deep_empty(c0):
            targets["content"] = self._keep_extra(
                prev_t.get("content") if isinstance(prev_t, dict) else None, c0
            )
        c0b = {}
        self._apply_text(c0b, "strategy", self._dec_ch_strategy.currentText())
        self._apply_text(c0b, "map_module", self._dec_ch_map.text())
        self._apply_text(c0b, "entry", self._dec_ch_entry.text())
        self._apply_script(c0b, "script", self._dec_ch_script)
        self._apply_json(c0b, "params", self._dec_ch_params.text())
        if not self._is_deep_empty(c0b):
            targets["chapter"] = self._keep_extra(
                prev_t.get("chapter") if isinstance(prev_t, dict) else None, c0b
            )
        if targets:
            raw["decryption"] = {"targets": targets}
        else:
            raw.pop("decryption", None)

        # 登录（auth）
        prev_auth = raw.get("auth")
        auth = {}
        auth["login_required"] = self._f_auth_required.isChecked()
        self._apply_text(auth, "note", self._f_auth_note.text())
        ck = {}
        self._apply_text(ck, "field", self._f_auth_field.text())
        self._apply_text(ck, "on_expired", self._f_auth_on.currentText())
        if self._f_auth_min.value() > 0:
            ck["min_length"] = self._f_auth_min.value()
        if ck:
            auth["cookie_check"] = ck
        auth = self._keep_extra(
            prev_auth if isinstance(prev_auth, dict) else None, auth
        )
        if not self._is_deep_empty(auth):
            raw["auth"] = auth
        else:
            raw.pop("auth", None)

        return raw

    # ------------------------------------------------------------------ #
    # 写回辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def _apply_text(d: dict, name: str, text: str) -> None:
        """写回纯文本字段；空白则移除。"""
        text = (text or "").strip()
        if text:
            d[name] = text
        else:
            d.pop(name, None)

    @staticmethod
    def _apply_script(d: dict, name: str, edit: QPlainTextEdit) -> None:
        """写回多行文本（JS 脚本）；空白则移除。"""
        text = (edit.toPlainText() or "").strip()
        if text:
            d[name] = text
        else:
            d.pop(name, None)

    @staticmethod
    def _apply_json(d: dict, name: str, text: str) -> None:
        """写回 JSON 字段（解析为 dict/list）；空白或非法则移除。"""
        text = (text or "").strip()
        if not text:
            d.pop(name, None)
            return
        try:
            d[name] = json.loads(text)
        except Exception:  # noqa: BLE001
            d.pop(name, None)

    @staticmethod
    def _apply_header(headers: dict, name: str, w) -> None:
        """写回额外 Header；空白则移除。"""
        v = w.text().strip()
        if v:
            headers[name] = v
        else:
            headers.pop(name, None)

    def _apply_css(self, d: dict, name: str, w) -> None:
        """写回选择器字段；只更新 css 键，保留原 spec 中的其它键（xpath/attr 等）。

        css 为空时仅移除与其并列的原 css 值，其余键不受影响。
        """
        css = w.text().strip()
        item = d.get(name)
        if css:
            if isinstance(item, dict):
                item["css"] = css
            else:
                d[name] = {"css": css}
        else:
            if isinstance(item, dict):
                item.pop("css", None)
                if not item:
                    del d[name]
            elif name in d:
                del d[name]

    @staticmethod
    def _apply_grid_fields(d: dict, grid) -> None:
        """写回字段表。

        只增改网格展示的固定字段名；网格行被清空则删除该字段；
        网格未展示的额外字段（如 ttdm 的 update/status）原样保留，避免整表覆盖丢数据。
        """
        entries = grid.entries()
        editable = {name for name, *_ in grid._entries}
        fields = d.setdefault("fields", {})
        for name in editable:
            if name in entries:
                fields[name] = entries[name]
            elif name in fields:
                del fields[name]
        if not fields:
            d.pop("fields", None)

    def _keep_extra(self, prev, cur: dict) -> dict:
        """把 prev 中 cur 没有的键补回 cur（保留表单未覆盖的配置，如 categories）。

        prev/cur 均为 dict 时递归合并。raw 在 _build_dict 里是深拷贝，可直接引用。
        """
        if not isinstance(prev, dict):
            return cur
        for k, v in prev.items():
            if k not in cur:
                cur[k] = v
            elif isinstance(v, dict) and isinstance(cur[k], dict):
                self._keep_extra(v, cur[k])
        return cur

    def _apply_selector(self, fields: dict, name: str, css: str) -> None:
        """写回单个 selector；只更新 css 键，保留原 spec 其它键（attr 等）。"""
        css = css.strip()
        item = fields.get(name)
        if css:
            if isinstance(item, dict):
                item["css"] = css
            else:
                fields[name] = {"css": css}
        else:
            if isinstance(item, dict):
                item.pop("css", None)
                if not item:
                    del fields[name]
            elif name in fields:
                del fields[name]

    def _apply_selector_list(self, block: dict, root_widget, url_widget) -> None:
        """写回 list.root_selector + list.fields.url（保留 dict 未知键）。"""
        lst = block.setdefault("list", {})
        self._apply_css(lst, "root_selector", root_widget)
        fields = lst.setdefault("fields", {})
        self._apply_css(fields, "url", url_widget)

    def _load_content_fields(self, raw: dict) -> None:
        """填充正文表单（编辑器主体重载后经 _build_content_forms 新建的控件）。"""
        ctype = self.type_combo.currentText()
        block = TYPE_CONTENT_BLOCK.get(ctype, "chapter")
        if not hasattr(self, "_c_list_root"):
            return
        ep = _deep_get(raw, f"endpoints.content.{block}", {}) or {}
        lst = ep.get("list") or {}
        body = ep.get("body") or {}
        self._c_list_root.setText(
            _deep_get(rootish(raw, f"endpoints.content.{block}.list.root_selector"), "css", "")
        )
        # 公共：目录页 / 排序 / 标题净化
        if hasattr(self, "_c_chapters_url"):
            self._c_chapters_url.setText(lst.get("chapters_url", ""))
        if hasattr(self, "_c_order"):
            self._combo_select(self._c_order, lst.get("chapter_order", ""))
        if hasattr(self, "_c_title_clean"):
            tclean = lst.get("title_clean", "")
            if isinstance(tclean, dict):
                tclean = tclean.get("css", "")
            elif isinstance(tclean, list):
                tclean = json.dumps(tclean, ensure_ascii=False)
            self._c_title_clean.setText("" if tclean is None else str(tclean))
        if ctype == "comic":
            self._c_list_url.setText(
                _deep_get(rootish(raw, f"endpoints.content.{block}.list.fields.url"), "css", "")
            )
            if hasattr(self, "_c_single"):
                self._c_single.setChecked(
                    bool(_deep_get(raw, f"endpoints.content.{block}.single_chapter", False))
                )
            self._load_render(body, body, self._f_body_render, self._body_rc_specs)
        elif ctype == "novel":
            self._c_list_title.setText(
                _deep_get(rootish(raw, f"endpoints.content.{block}.list.fields.title"), "css", "")
            )
            self._c_list_url.setText(
                _deep_get(rootish(raw, f"endpoints.content.{block}.list.fields.url"), "css", "")
            )
            self._c_body.setText(
                _deep_get(rootish(raw, f"endpoints.content.{block}.body.selector"), "css", "")
            )
            if hasattr(self, "_c_body_attr"):
                self._c_body_attr.setText(body.get("attr", ""))
            self._load_render(body, body, self._f_body_render, self._body_rc_specs)
        elif ctype == "video":
            self._c_list_title.setText(
                _deep_get(rootish(raw, f"endpoints.content.{block}.list.fields.title"), "css", "")
            )
            self._c_list_url.setText(
                _deep_get(rootish(raw, f"endpoints.content.{block}.list.fields.url"), "css", "")
            )
            self._combo_select(self._f_media_format, _deep_get(raw, "media.format", "hls"))
            self._combo_select(self._f_media_quality,
                               _deep_get(raw, "media.select.video.quality", "best"))
            self._combo_select(self._f_media_merge_tool,
                               _deep_get(raw, "media.merge.tool", ""))
            self._combo_select(self._f_media_output,
                               _deep_get(raw, "media.merge.output_format", ""))
            # 播放页
            play = ep.get("play_url") or {}
            if hasattr(self, "_c_play_sel"):
                psel = play.get("selector") or {}
                self._c_play_sel.setText(
                    psel.get("css", "") if isinstance(psel, dict) else str(psel)
                )
                self._c_play_attr.setText(
                    psel.get("attr", "") if isinstance(psel, dict) else ""
                )
                self._c_play_regex.setText(play.get("regex", ""))
                self._c_play_suffix.setText(play.get("suffix", ""))
            self._load_render(play or {}, ep, self._f_ep_render, self._ep_rc_specs)
            if hasattr(self, "_c_single"):
                self._c_single.setChecked(bool(ep.get("single_chapter", False)))
            # 分季
            if hasattr(self, "_f_series_enable"):
                ser = ep.get("series") or {}
                self._f_series_enable.setChecked(bool(ser))
                self._f_series_min.setValue(int(ser.get("min_parts", 2)))
                self._series_part_map.set_data(ser.get("part_map") or {})
            # 换源（source_switch）
            ss = ep.get("source_switch") or {}
            if hasattr(self, "_ss_enable"):
                self._ss_enable.setChecked(bool(ss))
                self._ss_param.setText(ss.get("param", ""))
                self._ss_list_sel.setText(ss.get("list_selector", ""))
                self._ss_list_attr.setText(ss.get("list_attr", ""))
                self._ss_name_sel.setText(ss.get("name_selector", ""))
                self._ss_ep_list.setText(ss.get("ep_list_selector", ""))
                self._ss_play_regex.setText(ss.get("play_regex", ""))
                self._ss_pc_url.setText(ss.get("playerconfig_url", ""))

    def _load_api_fields(self, raw: dict) -> None:
        """填充 JSON API（api_endpoints）表单控件。"""
        api = _deep_get(raw, "api_endpoints", {}) or {}
        d = api.get("discovery") or {}
        self._combo_select(self._api_d_engine, d.get("engine") or "")
        self._api_d_url.setText(d.get("url", ""))
        self._api_d_fb_url.setText(d.get("fallback_url", ""))
        self._api_d_rsp.setText(d.get("response_path", ""))
        self._api_d_fb_rsp.setText(d.get("fallback_response_path", ""))
        self._api_d_params.set_data(d.get("params") or {})
        self._api_d_fields.set_data(d.get("item_fields") or {})
        self._api_d_fb_fields.set_data(d.get("fallback_item_fields") or {})
        self._api_d_headers.set_data(d.get("headers") or {})
        s = api.get("search") or {}
        self._combo_select(self._api_s_engine, s.get("engine") or "")
        self._api_s_url.setText(s.get("url", ""))
        self._api_s_rsp.setText(s.get("response_path", ""))
        self._api_s_max.setValue(int(s.get("max_results", 0)))
        self._api_s_params.set_data(s.get("params") or {})
        self._api_s_fields.set_data(s.get("item_fields") or {})
        dt = api.get("detail") or {}
        self._combo_select(self._api_dt_engine, dt.get("engine") or "")
        self._api_dt_url.setText(dt.get("url", ""))
        self._api_dt_params.set_data(dt.get("params") or {})
        self._api_dt_headers.set_data(dt.get("headers") or {})
        self._api_dt_extract.set_data(dt.get("field_extractors") or {})
        ch = dt.get("chapters") or {}
        self._api_dt_ci.setText(ch.get("items", ""))
        self._api_dt_ct.setText(ch.get("title", ""))
        self._api_dt_cc.setText(ch.get("children_path", ""))
        self._api_dt_cu.setText(ch.get("url_template", ""))
        e = api.get("episode") or {}
        self._combo_select(self._api_e_engine, e.get("engine") or "")
        self._api_e_url.setText(e.get("url", ""))
        self._api_e_method.setCurrentText(e.get("method", ""))
        self._api_e_rsp.setText(e.get("response_path", ""))
        self._api_e_params.set_data(e.get("params") or {})
        self._api_e_headers.set_data(e.get("headers") or {})
        q = e.get("quality") or {}
        self._api_e_q_param.setText(q.get("param", ""))
        self._api_e_q_default.setText(q.get("default", ""))
        self._api_e_q_map.set_data(q.get("map") or {})

    def _load_decrypt_fields(self, raw: dict) -> None:
        """填充解密（decryption）表单控件。"""
        t = _deep_get(raw, "decryption.targets", {}) or {}
        img = t.get("image") or {}
        self._combo_select(self._dec_img_strategy, img.get("strategy") or "")
        self._dec_img_key.setText(img.get("key", ""))
        self._dec_img_iv.setText(img.get("iv", ""))
        self._combo_select(self._dec_img_padding, img.get("padding") or "")
        self._dec_img_input.setText(img.get("input", ""))
        self._dec_img_output.setText(img.get("output", ""))
        self._dec_img_entry.setText(img.get("entry", ""))
        self._dec_img_script.setPlainText(img.get("script", ""))
        self._dec_img_params.setText(self._fmt_json(img.get("params")))
        c = t.get("content") or {}
        self._combo_select(self._dec_c_strategy, c.get("strategy") or "")
        self._dec_c_endpoint.setText(c.get("endpoint", ""))
        self._dec_c_req.setText(c.get("request_field", ""))
        self._dec_c_rsp.setText(c.get("response_field", ""))
        self._dec_c_map.setText(c.get("map_module", ""))
        self._dec_c_entry.setText(c.get("entry", ""))
        self._dec_c_script.setPlainText(c.get("script", ""))
        self._dec_c_params.setText(self._fmt_json(c.get("params")))
        ch = t.get("chapter") or {}
        self._combo_select(self._dec_ch_strategy, ch.get("strategy") or "")
        self._dec_ch_map.setText(ch.get("map_module", ""))
        self._dec_ch_entry.setText(ch.get("entry", ""))
        self._dec_ch_script.setPlainText(ch.get("script", ""))
        self._dec_ch_params.setText(self._fmt_json(ch.get("params")))

    @staticmethod
    def _fmt_json(value) -> str:
        """dict/list → 紧凑 JSON 串（填入 JS 参数文本框）；其余原样。"""
        if isinstance(value, (dict, list)):
            try:
                return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            except Exception:  # noqa: BLE001
                return ""
        return "" if value is None else str(value)

    def _load_auth_fields(self, raw: dict) -> None:
        """填充登录（auth）表单控件。"""
        auth = _deep_get(raw, "auth", {}) or {}
        self._f_auth_required.setChecked(bool(auth.get("login_required", False)))
        self._f_auth_note.setText(auth.get("note", ""))
        ck = auth.get("cookie_check") or {}
        self._f_auth_field.setText(ck.get("field", ""))
        self._combo_select(self._f_auth_on, ck.get("on_expired") or "")
        self._f_auth_min.setValue(int(ck.get("min_length", 0)))

    # ================================================================== #
    # 保存 / 草稿 / 导出
    # ================================================================== #
    def _on_type_changed(self, ctype: str) -> None:
        """切换类型 → 重建正文表单 + 重填。"""
        self._build_content_forms()
        self._load_content_fields(self._raw)
        self._update_status_label()

    def _resolve_sources_dir(self) -> Path:
        if self._sources_dir:
            return self._sources_dir
        return Path(__file__).resolve().parent.parent.parent / "sources"

    def _require_id(self) -> bool:
        """校验 $id 非空且合法（小写蛇形）。"""
        sid = self.id_edit.text().strip()
        if not sid:
            QMessageBox.warning(self, "缺少 ID", "源 ID 不能为空。")
            return False
        if not sid.replace("_", "").isalnum() or sid != sid.lower():
            QMessageBox.warning(self, "ID 不合法", "源 ID 只能用小写字母、数字、下划线。")
            return False
        return True

    def _validate_and_save(self) -> None:
        """严格校验 + 落盘。校验失败禁止落盘。"""
        if not self._require_id():
            return
        try:
            raw = self._build_dict()
            SourceConfig.from_dict(raw, path="<editor>")
        except ConfigError as exc:
            QMessageBox.warning(self, "校验失败", f"配置不完整，无法保存：\n{exc.message}")
            return
        raw["$enabled"] = True
        if not self._write_raw(raw):
            return
        QMessageBox.information(self, "已保存", f"源「{raw.get('$name', raw.get('$id'))}」已保存并启用。")
        self.source_saved.emit(raw["$id"])

    def _save_draft(self) -> None:
        """残缺保存：跳过严格校验，强制 $enabled=false。"""
        if not self._require_id():
            return
        raw = self._build_dict()
        raw["$id"] = self.id_edit.text().strip()
        raw["$enabled"] = False
        if not self._write_raw(raw):
            return
        QMessageBox.information(self, "已存草稿", "已保存为草稿（源处于禁用状态）。")
        self.source_saved.emit(raw["$id"])

    def _write_raw(self, raw: dict) -> bool:
        """把 dict 序列化到 sources 目录。失败返回 False 并提示。"""
        sid = raw["$id"]
        try:
            directory = self._resolve_sources_dir()
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{sid}.json"
            path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "保存失败", f"写入失败：{exc}")
            return False
        self._raw = raw
        return True

    def _export_json(self) -> None:
        """导出当前表单为 JSON 文件（不影响 sources 目录）。"""
        raw = self._build_dict()
        default_name = (raw.get("$id") or "source") + ".json"
        path, _ = QFileDialog.getSaveFileName(self, "导出 JSON", default_name, "JSON (*.json)")
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            QMessageBox.information(self, "已导出", f"已导出到 {path}")
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", f"写入失败：{exc}")

    # ================================================================== #
    # 🤖 AI 制源
    # ================================================================== #
    def _on_ai_source(self) -> None:
        """打开 Agent 自动制源对话框。"""
        from gui.components.agent_dialog import AgentDialog

        dlg = AgentDialog(preview=self._preview, parent=self)
        dlg.source_added.connect(self._on_agent_source_added)
        dlg.exec()

    def _on_agent_source_added(self, result) -> None:
        """Agent 制源成功后，加载新源到编辑器表单或通知源列表刷新。"""
        from framework.source_agent import AgentResult

        if isinstance(result, AgentResult) and result.ok:
            if result.draft:
                # 有草稿数据 → 加载到表单供用户检查
                self._raw = result.draft
                self._load_into_form()
                self._update_status_label()
                QMessageBox.information(
                    self, "AI 制源成功",
                    f"源 {result.source_id} 已生成并加载到表单。\n"
                    "请检查各字段后点击「校验并保存」。",
                )
            else:
                # 源已由 SourceAgent 直接保存到 sources/ → 通知刷新
                QMessageBox.information(
                    self, "AI 制源成功",
                    f"源 {result.source_id} 已保存到源列表。\n"
                    "关闭此对话框后可在源列表中查看。",
                )
                self.source_saved.emit(result.source_id)

    # ================================================================== #
    def _update_status_label(self) -> None:
        enabled = bool(self._raw.get("$enabled", False))
        if self._mode == "NEW":
            self.status_label.setText("新源 · 草稿")
        else:
            self.status_label.setText("已启用" if enabled else "草稿(禁用)")

    @staticmethod
    def _default_template(ctype: str) -> dict:
        """新建源骨架 dict（默认配置来自 source_presets）。"""
        return source_presets.default_source_template(ctype)


def rootish(raw: dict, path: str, default=None):
    """返回 selector 对象本身（可能是 {css:...} 或字符串），供取 css。"""
    return _deep_get(raw, path, default)
