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
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

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
        self._f_weight = sec._double("权重", "$weight", 0.0, 10.0,
                                     hint="搜索排序权重，默认 1.0")
        self.tabs.addTab(sec, "基本信息")

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
        self._f_cookie = sec._line("Cookie", "transports.cookie",
                                   "登录/风控才需要，留空即可")
        self._f_timeout = sec._spin("超时(秒)", "transports.timeout", 1, 300, hint="默认 10")
        self._f_retries = sec._spin("重试", "transports.retries", 0, 10, hint="默认 3")
        self._f_interval = sec._spin("请求间隔(ms)", "transports.interval_ms",
                                     0, 10000, " ms", hint="反爬站调大到 800+")
        self.tabs.addTab(sec, "网络")

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
        self.tabs.addTab(sec, "发现")

    def _build_search(self) -> None:
        sec = _FormSection()
        sec.add_help(
            "第 4 步 · 搜索",
            [
                "搜索 URL：搜索页地址（如 /search 或 /comic/all）。",
                "关键词参数：看搜索页 URL 问号后的参数名（?keyword=xxx → keyword）。",
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
                                           ["GET", "POST"], hint="多数站 GET")
        self._f_search_root = sec._line("结果项选择器", "endpoints.search.item.root_selector",
                                        "例：.dx-novel-list li")
        self.tabs.addTab(sec, "搜索")

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
        self.tabs.addTab(sec, "详情")

    def _build_content(self) -> None:
        """正文块：按 $type 动态显示 chapter/page/episode。"""
        self.content_tab_widget = QWidget()
        self.content_tab_layout = QVBoxLayout(self.content_tab_widget)
        self.content_tab_layout.setContentsMargins(0, 0, 0, 0)
        self.tabs.insertTab(5, self.content_tab_widget, "正文")

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
            self._c_body = sec._line("正文选择器", f"endpoints.content.{block}.body.selector",
                                     "例：#chaptercontent p")
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
            # 图集站：详情页即图片页（无独立章节列表）
            self._c_single = sec._check("详情页即图片页", f"endpoints.content.{block}.single_chapter",
                                        "图集站必勾")
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
            media_sec = _FormSection()
            self._f_media_format = media_sec._combo("媒体格式", "media.format",
                                                    ["hls", "dash", "mp4", "raw"],
                                                    hint="多数站 hls")
            self._f_media_quality = media_sec._combo(
                "选择画质", "media.select.video.quality", ["best", "1080p", "720p", "480p"],
                hint="推荐 best"
            )
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
                                           1, 500, hint="默认 20")
        self._f_detail_pages = sec._spin("详情最大页", "constraints.detail.max_pages",
                                         1, 50, hint="默认 1")
        self._f_content_pages = sec._spin("正文最大页", "_flag_content_pages",
                                          1, 500, hint="默认 50")
        self._f_content_items = sec._spin("正文最大条", "_flag_content_items",
                                          1, 5000, hint="默认 500")
        self.tabs.addTab(sec, "限制")

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
        self.tabs.addTab(sec, "诊断")

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
        self.tabs.addTab(sec, "广告过滤")

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
        self._f_weight.setValue(float(_deep_get(raw, "$weight", 1.0)))

        # 网络
        self._f_base.setText(_deep_get(raw, "transports.base_url", ""))
        headers = _deep_get(raw, "transports.headers", {}) or {}
        self._f_ua.setText(headers.get("User-Agent", ""))
        self._f_cookie.setText(_deep_get(raw, "transports.cookie", ""))
        self._f_timeout.setValue(int(_deep_get(raw, "transports.timeout", 10)))
        self._f_retries.setValue(int(_deep_get(raw, "transports.retries", 3)))
        self._f_interval.setValue(int(_deep_get(raw, "transports.interval_ms", 500)))

        # 发现
        disc = _deep_get(raw, "endpoints.discovery", None)
        self._f_disc_enable.setChecked(disc is not None)
        self._f_list_url.setText(_deep_get(raw, "endpoints.discovery.list_url", ""))

        # 搜索
        ssearch = _deep_get(raw, "endpoints.search", None)
        self._f_search_enable.setChecked(ssearch is not None)
        self._f_search_url.setText(_deep_get(raw, "endpoints.search.base_url", ""))
        self._f_search_kw.setText(_deep_get(raw, "endpoints.search.keyword_param", ""))
        self._f_search_method.setCurrentText(_deep_get(raw, "endpoints.search.method", "GET"))
        self._f_search_root.setText(_deep_get(rootish(raw, "endpoints.search.item.root_selector"), "css", ""))

        # 详情
        self._f_de_title.setText(_deep_get(rootish(raw, "endpoints.detail.fields.title"), "css", ""))
        self._f_de_author.setText(_deep_get(rootish(raw, "endpoints.detail.fields.author"), "css", ""))
        self._f_de_cover.setText(_deep_get(rootish(raw, "endpoints.detail.fields.cover"), "css", ""))
        self._f_de_summary.setText(_deep_get(rootish(raw, "endpoints.detail.fields.summary"), "css", ""))

        # 限制
        self._f_search_pages.setValue(int(_deep_get(raw, "constraints.search.max_pages", 1)))
        self._f_search_results.setValue(int(_deep_get(raw, "constraints.search.max_results", 20)))
        self._f_detail_pages.setValue(int(_deep_get(raw, "constraints.detail.max_pages", 1)))
        ctype = self.type_combo.currentText()
        block = TYPE_CONTENT_BLOCK.get(ctype, "chapter")
        self._f_content_pages.setValue(int(_deep_get(raw, f"constraints.{block}.max_pages", 50)))
        self._f_content_items.setValue(int(_deep_get(raw, f"constraints.{block}.max_items", 500)))

        # 诊断
        self._f_sc_strategy.setCurrentText(_deep_get(raw, "diagnostics.selfcheck.strategy", "soft"))
        self._f_sc_selector.setText(_deep_get(rootish(raw, "diagnostics.selfcheck.selector"), "css", ""))

        # 广告过滤
        ad_enabled = _deep_get(raw, "ad_block.enabled", True)
        self._f_ad_enable.setChecked(bool(ad_enabled))
        self._f_ad_regex.setText(", ".join(_deep_get(raw, "ad_block.block_url_regex", []) or []))
        self._f_ad_domains.setText(", ".join(_deep_get(raw, "ad_block.block_domains", []) or []))

        # 正文（按类型重建并填充）
        self._build_content_forms()
        self._load_content_fields(raw)

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
        _deep_set(raw, "transports.cookie", self._f_cookie.text())
        _deep_set(raw, "transports.timeout", self._f_timeout.value())
        _deep_set(raw, "transports.retries", self._f_retries.value())
        _deep_set(raw, "transports.interval_ms", self._f_interval.value())

        # 发现（未勾选则移除）
        if self._f_disc_enable.isChecked():
            endpoints = raw.setdefault("endpoints", {})
            disc = endpoints.setdefault("discovery", {})
            _deep_set(disc, "list_url", self._f_list_url.text())
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
            root_sel = self._f_search_root.text().strip()
            if root_sel:
                search.setdefault("item", {})["root_selector"] = {"css": root_sel}
        else:
            if "endpoints" in raw:
                raw["endpoints"].pop("search", None)

        # 详情
        detail = raw.setdefault("endpoints", {}).setdefault("detail", {}).setdefault("fields", {})
        self._apply_selector(detail, "title", self._f_de_title.text())
        self._apply_selector(detail, "author", self._f_de_author.text())
        self._apply_selector(detail, "cover", self._f_de_cover.text())
        self._apply_selector(detail, "summary", self._f_de_summary.text())

        # 限制
        ctype = self.type_combo.currentText()
        block = TYPE_CONTENT_BLOCK.get(ctype, "chapter")
        _deep_set(raw, "constraints.search.max_pages", self._f_search_pages.value())
        _deep_set(raw, "constraints.search.max_results", self._f_search_results.value())
        _deep_set(raw, "constraints.detail.max_pages", self._f_detail_pages.value())
        _deep_set(raw, f"constraints.{block}.max_pages", self._f_content_pages.value())
        _deep_set(raw, f"constraints.{block}.max_items", self._f_content_items.value())

        # 正文（按类型）
        content = raw.setdefault("endpoints", {}).setdefault("content", {})
        b = content.setdefault(block, {})
        self._apply_selector_list(b, self._c_list_root, self._c_list_url)
        if ctype == "novel":
            self._apply_selector(b.get("list", {}).setdefault("fields", {}), "title", self._c_list_title.text())
            body_css = self._c_body.text().strip()
            if body_css:
                b["body"] = {"selector": {"css": body_css}}
        elif ctype == "comic":
            if hasattr(self, "_c_single"):
                if self._c_single.isChecked():
                    b["single_chapter"] = True
                else:
                    b.pop("single_chapter", None)
        elif ctype == "video":
            self._apply_selector(b.get("list", {}).setdefault("fields", {}), "title", self._c_list_title.text())
            _deep_set(raw, "media.format", self._f_media_format.currentText())
            _deep_set(raw, "media.select.video.quality", self._f_media_quality.currentText())
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
            ep_block = raw.setdefault("endpoints", {}).setdefault("content", {}).setdefault("episode", {})
            if ss and ss.get("list_selector") and ss.get("ep_list_selector"):
                ep_block["source_switch"] = ss
            elif "source_switch" in ep_block:
                del ep_block["source_switch"]

        # 广告过滤
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
        else:
            raw["ad_block"] = {"enabled": False}

        return raw

    def _apply_selector(self, fields: dict, name: str, css: str) -> None:
        """写回单个 selector（css 为空则移除）。"""
        css = css.strip()
        if css:
            fields[name] = {"css": css}
        elif name in fields:
            del fields[name]

    def _apply_selector_list(self, block: dict, root_widget, url_widget) -> None:
        """写回 list.root_selector + list.fields.url。"""
        lst = block.setdefault("list", {})
        root_css = root_widget.text().strip()
        if root_css:
            lst["root_selector"] = {"css": root_css}
        elif "root_selector" in lst:
            del lst["root_selector"]
        url_css = url_widget.text().strip()
        fields = lst.setdefault("fields", {})
        if url_css:
            fields["url"] = {"css": url_css}
        elif "url" in fields:
            del fields["url"]

    def _load_content_fields(self, raw: dict) -> None:
        """填充正文表单（编辑器主体重载后经 _build_content_forms 新建的控件）。"""
        ctype = self.type_combo.currentText()
        block = TYPE_CONTENT_BLOCK.get(ctype, "chapter")
        if not hasattr(self, "_c_list_root"):
            return
        self._c_list_root.setText(
            _deep_get(rootish(raw, f"endpoints.content.{block}.list.root_selector"), "css", "")
        )
        if ctype == "comic":
            self._c_list_url.setText(
                _deep_get(rootish(raw, f"endpoints.content.{block}.list.fields.url"), "css", "")
            )
            if hasattr(self, "_c_single"):
                self._c_single.setChecked(
                    bool(_deep_get(raw, f"endpoints.content.{block}.single_chapter", False))
                )
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
        elif ctype == "video":
            self._c_list_title.setText(
                _deep_get(rootish(raw, f"endpoints.content.{block}.list.fields.title"), "css", "")
            )
            self._c_list_url.setText(
                _deep_get(rootish(raw, f"endpoints.content.{block}.list.fields.url"), "css", "")
            )
            self._f_media_format.setCurrentText(_deep_get(raw, "media.format", "hls"))
            self._f_media_quality.setCurrentText(_deep_get(raw, "media.select.video.quality", "best"))
            # 换源（source_switch）
            ss = _deep_get(raw, f"endpoints.content.{block}.source_switch", None)
            if hasattr(self, "_ss_enable"):
                self._ss_enable.setChecked(bool(ss))
                self._ss_param.setText(_deep_get(ss, "param", "") if ss else "")
                self._ss_list_sel.setText(_deep_get(ss, "list_selector", "") if ss else "")
                self._ss_list_attr.setText(_deep_get(ss, "list_attr", "data-sid") if ss else "data-sid")
                self._ss_name_sel.setText(_deep_get(ss, "name_selector", "") if ss else "")
                self._ss_ep_list.setText(_deep_get(ss, "ep_list_selector", "") if ss else "")
                self._ss_play_regex.setText(_deep_get(ss, "play_regex", "") if ss else "")
                self._ss_pc_url.setText(_deep_get(ss, "playerconfig_url", "") if ss else "")

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
        try:
            raw = self._build_dict()
            SourceConfig.from_dict(raw, path="<editor>")
        except ConfigError as exc:
            QMessageBox.warning(self, "校验失败", f"配置不完整，无法保存：\n{exc.message}")
            return
        if not self._require_id():
            return
        raw["$enabled"] = True
        self._write_raw(raw)
        QMessageBox.information(self, "已保存", f"源「{raw.get('$name', raw.get('$id'))}」已保存并启用。")
        self.source_saved.emit(raw["$id"])

    def _save_draft(self) -> None:
        """残缺保存：跳过严格校验，强制 $enabled=false。"""
        if not self._require_id():
            return
        raw = self._build_dict()
        raw["$id"] = self.id_edit.text().strip()
        raw["$enabled"] = False
        self._write_raw(raw)
        QMessageBox.information(self, "已存草稿", "已保存为草稿（源处于禁用状态）。")
        self.source_saved.emit(raw["$id"])

    def _write_raw(self, raw: dict) -> None:
        """把 dict 序列化到 sources 目录。"""
        sid = raw["$id"]
        directory = self._resolve_sources_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{sid}.json"
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        self._raw = raw

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
    def _update_status_label(self) -> None:
        enabled = bool(self._raw.get("$enabled", False))
        if self._mode == "NEW":
            self.status_label.setText("新源 · 草稿")
        else:
            self.status_label.setText("已启用" if enabled else "草稿(禁用)")

    @staticmethod
    def _default_template(ctype: str) -> dict:
        """新建源骨架 dict。"""
        return {
            "$schema_version": 2,
            "$id": "",
            "$type": ctype,
            "$name": "",
            "$enabled": False,
            "$weight": 1.0,
            "$metadata": {"homepage": "", "description": "", "tags": []},
            "transports": {
                "base_url": "",
                "timeout": 10,
                "retries": 3,
                "interval_ms": 500,
            },
            "endpoints": {
                "detail": {"fields": {}},
                "content": {},
            },
            "constraints": {},
            "diagnostics": {"selfcheck": {"strategy": "soft"}},
        }


def rootish(raw: dict, path: str, default=None):
    """返回 selector 对象本身（可能是 {css:...} 或字符串），供取 css。"""
    return _deep_get(raw, path, default)
