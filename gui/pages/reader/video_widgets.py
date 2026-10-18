"""视频阅读界面小部件（B4 拆分）：播放控制面板与分集/推荐卡片网格。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from gui.components.hover_title import HoverTitle


class _PlayPanel(QWidget):
    """外部播放器方案的播放面板（占位几何区，不做视觉占位）。

    独立播放器进程接管画面，本区不显示任何内容——播放入口 =
    覆盖层中央「▶ 播放」按钮。这里只承接交互：
    - 单击/双击面板 → 拉起外部播放器；移动鼠标 → 唤出控制条
    - 保持布局占位与键盘焦点（快捷键仍生效）
    """

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self._view = view
        # 无视觉占位：透明，画面区只显示中央播放按钮
        self.setStyleSheet("background: transparent;")
        # 无按键移动也触发 mouseMoveEvent → 控制条隐藏后滑动即唤出
        self.setMouseTracking(True)

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        # 取流完成时用户在别的 Tab → 暂存的播放，回到阅读页补播
        self._view._flush_pending_play()

    def _click_timer(self) -> None:
        """单击/双击判定（双击后 300ms 无第二次点击才执行单击动作）。"""
        if self._view._click_pending:
            self._view._click_pending = False
            self._view._toggle_play_pause()

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._view._click_pending = True
            self._view._wake_controls()  # 点击面板 → 唤出控制条
            self.setFocus()  # 焦点收拢
            QTimer.singleShot(300, self._click_timer)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        self._view._click_pending = False
        if event.button() == Qt.LeftButton:
            # 外部播放器方案：双击面板 = 拉起外部播放器打开当前集
            self._view._open_external()
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        self._view._wake_controls()
        super().mouseMoveEvent(event)

    def enterEvent(self, event):  # noqa: N802
        self._view._wake_controls()
        super().enterEvent(event)

    def keyPressEvent(self, event):  # noqa: N802
        self._view._handle_key(event)


class _GridCard(QFrame):
    """块状卡片：封面（可选）在上 + 标题在下，点击触发。

    - 有封面：CoverLoader 异步回填缩略图（居中裁剪，尺寸与网格列对齐）
    - 无封面：隐藏封面区 → 纯文字块（palette midlight 圆角），观感如床头按钮
    - 有演员（detail.actor，源 detail.fields.actor 可选配置）：标题下增加
      一行小字号演员；无封面时演员行跟随标题居中
    - selected 属性：当前集高亮（亮色边框），供分集卡片选中态
    """

    clicked = Signal(object)  # 发射自身（host 按 p_data 区分分集/推荐）

    def __init__(self, title: str = "", cover_url: str = "", source_id: str = "",
                 actor: str = "", p_data=None, parent=None, source=None):
        super().__init__(parent)
        self.p_data = p_data
        self.cover_url = cover_url or ""
        self.source_id = source_id or ""
        self.actor = actor or ""
        self._source = source  # 所属源（供 CoverLoader 解密封面字节，私图床加密封面）
        self._selected = False
        self.setObjectName("mediaCard")
        self.setCursor(Qt.PointingHandCursor)
        card_h = 178 if self.actor else 158
        self.setFixedHeight(card_h)
        self.setStyleSheet(
            "QFrame#mediaCard { background: palette(midlight);"
            " border: 2px solid transparent; border-radius: 8px; }"
            "QFrame#mediaCard:hover { border-color: palette(highlight); }"
            "QFrame#mediaCard[mediaSelected=\"true\"] { border-color: palette(highlight); }"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(6)
        self._cover = QLabel(self)
        self._cover.setAlignment(Qt.AlignCenter)
        self._cover.setFixedHeight(100)
        self._cover.setStyleSheet(
            "background: palette(base); border-radius: 6px; color: palette(mid); font-size: 30px;"
        )
        self._cover.setText("▶")
        lay.addWidget(self._cover)

        self._title = HoverTitle(title or "", parent_card=self)
        self._title.setFixedHeight(20)
        self._title.setStyleSheet("font-size: 13px;")
        lay.addWidget(self._title, stretch=1)

        # 演员行（源 detail.fields.actor 配置；为空不显示，避免空行占位）
        self._actor_lbl = QLabel(self.actor, self)
        self._actor_lbl.setWordWrap(True)
        self._actor_lbl.setStyleSheet("color: palette(dark); font-size: 12px;")
        if self.actor:
            lay.addWidget(self._actor_lbl)
        else:
            self._actor_lbl.hide()

        if self.cover_url:
            self._load_cover()
        else:
            self._cover.hide()
            self._title.setAlignment(Qt.AlignCenter)
            self._actor_lbl.setAlignment(Qt.AlignCenter)

    def set_selected(self, on: bool) -> None:
        """刷新当前集选中态（属性驱动 QSS 边框）。"""
        on = bool(on)
        if on == self._selected:
            return
        self._selected = on
        self.setProperty("mediaSelected", on)
        style = self.style()
        style.unpolish(self)
        style.polish(self)

    def _load_cover(self) -> None:
        """异步加载卡片封面（CoverLoader 全局限流，居中裁剪显示）。"""
        from gui.components.cover_loader import CoverLoader

        def _on(pm):
            if pm is None or pm.isNull():
                return
            import shiboken6

            if not shiboken6.isValid(self._cover):
                return  # 卡片已销毁，跳过
            w = self._cover.width()
            h = self._cover.height()
            if w <= 0 or h <= 0:
                w, h = 160, 100  # 未布局兜底
            scaled = pm.scaled(
                w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
            )
            sx = max(0, (scaled.width() - w) // 2)
            sy = max(0, (scaled.height() - h) // 2)
            cropped = scaled.copy(sx, sy, min(w, scaled.width()), min(h, scaled.height()))
            self._cover.setPixmap(cropped)
            from gui.components.cover_loader import fade_in

            fade_in(self._cover)

        CoverLoader.instance().load(
            self.cover_url, _on, cache=True, persist=False,
            source_id=self.source_id, source=self._source,
        )

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self)
        super().mouseReleaseEvent(event)
