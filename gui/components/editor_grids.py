"""通用表格编辑控件（editor_grids.py）。

供源编辑器表单复用两类结构化配置：
- KeyValueGrid：通用键值对编辑器（api_endpoints 的 params / item_fields /
  field_extractors / headers / quality.map 等键值型配置）。
- SelectorGrid：选择器字段编辑器（发现/搜索/详情里的多字段选择器集合，
  每字段一行 CSS + 属性(attr) 双列输入）。

两控件都不带主题硬编码，跟随 QSS palette，视觉与表单其余控件一致。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class KeyValueGrid(QWidget):
    """键值对表格：每行「键 + 值 + 删除」，底部「+ 添加一行」。

    set_data({key: value}) 载入；data() 返回 dict。
    载入的原值会被记住：值未改动的行按原类型回写（不把数字/布尔强转成字符串），
    新增或改动的行值以字符串写入。
    """

    def __init__(
        self,
        parent=None,
        key_hint: str = "键/字段名",
        value_hint: str = "值（支持 {占位}）",
    ):
        super().__init__(parent)
        self._key_hint = key_hint
        self._value_hint = value_hint
        self._rows: list = []  # [key_edit, value_edit] 元组
        self._orig: dict = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        self._grid = QGridLayout()
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(6)
        self._grid.setVerticalSpacing(4)

        head = QGridLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setHorizontalSpacing(6)
        hk = QLabel("键")
        hk.setStyleSheet("color: palette(mid); font-size: 11px;")
        hv = QLabel("值")
        hv.setStyleSheet("color: palette(mid); font-size: 11px;")
        head.addWidget(hk, 0, 0)
        head.addWidget(hv, 0, 1)
        head.setColumnStretch(1, 1)
        outer.addLayout(head)
        outer.addLayout(self._grid)

        add_btn = QPushButton("＋ 添加一行")
        add_btn.setStyleSheet(
            "font-size: 11px; padding: 2px 8px; color: palette(accent);"
            " background: transparent; border: none; text-align: left;"
        )
        add_btn.clicked.connect(lambda: self._append("", ""))
        outer.addWidget(add_btn, alignment=Qt.AlignLeft)

    def _append(self, key: str, value: str) -> None:
        ke = QLineEdit(self._key_hint)
        ke.setText(key)
        ke.setStyleSheet("font-size: 11px;")
        ve = QLineEdit(self._value_hint)
        ve.setText(value)
        ve.setStyleSheet("font-size: 11px;")
        self._rows.append((ke, ve))
        self._rebuild_grid()

    def _remove(self, idx: int) -> None:
        if not (0 <= idx < len(self._rows)):
            return
        self._rows.pop(idx)
        self._orig.clear()  # 行序变化后按原值回写不稳定，重置为 str 回写
        self._rebuild_grid()

    def _rebuild_grid(self) -> None:
        """按当前 rows 重建全部行（行数小，重建最简单可靠）。"""
        while self._grid.count():
            item = self._grid.itemAt(0)
            w = item.widget() if item is not None else None
            self._grid.removeItem(item)
            if w is not None:
                w.deleteLater()
        for row_idx, (ke, ve) in enumerate(self._rows):
            self._grid.addWidget(ke, row_idx, 0)
            self._grid.addWidget(ve, row_idx, 1)
            rm = QPushButton("✕")
            rm.setFixedSize(22, 22)
            rm.setStyleSheet(
                "font-size: 10px; color: palette(mid); background: transparent;"
                " border: none; border-radius: 4px;"
            )
            rm.setToolTip("删除该行")
            self._grid.addWidget(rm, row_idx, 2)
            rm.clicked.connect(lambda _=False, idx=row_idx: self._remove(idx))

    def set_data(self, pairs: dict) -> None:
        """清空并以 pairs 填充。"""
        self._clear()
        self._orig = dict(pairs or {})
        for k, v in (pairs or {}).items():
            self._append(str(k), "" if v is None else str(v))

    def data(self) -> dict:
        """收集当前键值对（忽略键为空的行）。"""
        out = {}
        for ke, ve in self._rows:
            k = ke.text().strip()
            if not k:
                continue
            v = ve.text()
            orig = self._orig.get(k)
            if orig is not None and str(orig) == v:
                out[k] = orig
            else:
                out[k] = v
        return out

    def is_empty(self) -> bool:
        return not any(ke.text().strip() for ke, _ in self._rows)

    def _clear(self) -> None:
        for ke, ve in list(self._rows):
            self._grid.removeWidget(ke)
            ke.deleteLater()
            self._grid.removeWidget(ve)
            ve.deleteLater()
        self._rows.clear()
        self._orig = {}


class SelectorGrid(QWidget):
    """选择器字段编辑器：字段名固定，每行「字段 + CSS 选择器 + 属性(attr)」三列。

    set_entries({name: {"css": ..., "attr": ...}}) 载入；entries() 返回同样结构，
    该字段 css 和 attr 都为空时不输出。
    """

    def __init__(self, parent=None, names: list | None = None,
                 labels: dict | None = None, hints: dict | None = None,
                 attr_hint: str = "attr"):
        super().__init__(parent)
        names = names or ["title", "url", "cover", "author", "update", "status"]
        labels = labels or {}
        hints = hints or {}
        self._entries: list = []  # (name, css_edit, attr_edit)
        self._orig_entries: dict = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        head = QGridLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setHorizontalSpacing(6)
        hk = QLabel("字段")
        hk.setStyleSheet("color: palette(mid); font-size: 11px;")
        hc = QLabel("CSS 选择器")
        hc.setStyleSheet("color: palette(mid); font-size: 11px;")
        ha = QLabel("属性(attr)")
        ha.setStyleSheet("color: palette(mid); font-size: 11px;")
        head.addWidget(hk, 0, 0)
        head.addWidget(hc, 0, 1)
        head.addWidget(ha, 0, 2)
        head.setColumnStretch(1, 1)
        outer.addLayout(head)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(1, 1)
        outer.addLayout(grid)

        for i, name in enumerate(names):
            lab = QLabel(labels.get(name, name))
            lab.setStyleSheet("color: palette(text2); font-size: 11px;")
            css_edit = QLineEdit()
            css_edit.setPlaceholderText(hints.get(name, ""))
            css_edit.setStyleSheet("font-size: 11px;")
            attr_edit = QLineEdit()
            attr_edit.setPlaceholderText(attr_hint)
            attr_edit.setStyleSheet("font-size: 11px;")
            grid.addWidget(lab, i, 0)
            grid.addWidget(css_edit, i, 1)
            grid.addWidget(attr_edit, i, 2)
            self._entries.append((name, css_edit, attr_edit))

    def set_entries(self, entries: dict) -> None:
        entries = entries or {}
        self._orig_entries = dict(entries)
        for name, css_edit, attr_edit in self._entries:
            spec = entries.get(name) or {}
            if isinstance(spec, str):
                css = spec
                attr = ""
            else:
                css = spec.get("css", "")
                attr = spec.get("attr", "")
            css_edit.setText(css)
            attr_edit.setText(attr)

    def entries(self) -> dict:
        out = {}
        for name, css_edit, attr_edit in self._entries:
            css = css_edit.text().strip()
            attr = attr_edit.text().strip()
            if not css and not attr:
                continue
            item = {}
            base = self._orig_entries.get(name)
            if isinstance(base, dict):
                # 保留 css/attr 之外的原始键（xpath / placeholder / regex / state 等）
                for k, v in base.items():
                    if k not in ("css", "attr"):
                        item[k] = v
            if css:
                item["css"] = css
            if attr:
                item["attr"] = attr
            out[name] = item
        return out