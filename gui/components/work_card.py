"""作品卡片（WorkCard）。

发现列表 / 搜索结果共用的卡片：封面图 + 标题 + 来源/作者 + 更新。
封面通过 CoverLoader（全局共享+限流）异步加载，避免并发失控。
点击触发选中（信号）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
)

from .cover_loader import CoverLoader


class WorkCard(QFrame):
    """单张作品卡片。"""

    clicked = Signal(object)  # 发射 Work 对象
    checked = Signal(object, bool)  # (work, checked) 勾选状态变化（批量模式）

    def __init__(self, work, selectable: bool = False, parent=None):
        super().__init__(parent)
        self.work = work
        self.setObjectName("workCard")
        self.setCursor(Qt.PointingHandCursor)
        # 弹性宽度：由网格列数决定，不固定，避免窗口窄时横向溢出
        self.setMinimumWidth(120)
        # 固定卡片高度：封面 180 + 标题 + 来源，保证网格每行等高
        self.setFixedHeight(272)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        # 封面行：封面 + 可选勾选框（右上角）
        cover_row = QHBoxLayout()
        cover_row.setSpacing(4)

        self._checkbox = None
        if selectable:
            self._checkbox = QCheckBox()
            self._checkbox.setCursor(Qt.PointingHandCursor)
            self._checkbox.toggled.connect(self._on_check_toggled)
            # 放在封面右上角，避免挡住封面
            cover_row.addStretch(1)
            cover_row.addWidget(self._checkbox, alignment=Qt.AlignTop)

        # 封面（CoverLoader 全局限流加载）
        cover = QLabel("🖼")
        cover.setAlignment(Qt.AlignCenter)
        cover.setFixedSize(140, 180)
        cover.setStyleSheet("background: palette(midlight); border-radius: 8px; font-size: 36px;")
        cover_row.addWidget(cover)
        self._cover = cover
        layout.addLayout(cover_row)

        title = QLabel(work.title or "无标题")
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        title.setMinimumHeight(36)
        title.setMaximumHeight(54)  # 限 2 行，长标题截断，保持卡片等高
        layout.addWidget(title)
        self._title = title

        src = QLabel(work.source_name or "")
        src.setStyleSheet("color: palette(dark); font-size: 10px;")
        layout.addWidget(src)

        # 异步加载封面（全局限流，不会并发爆炸）
        self._pix = None
        if work.cover:
            if work.cover.startswith("data:"):
                # data URI（解密后的封面）直接解码显示，无需网络
                self._load_data_cover(work.cover)
            else:
                self._load_cover(work.cover)

    # ------------------------------------------------------------------ #
    def _load_data_cover(self, data_uri: str) -> None:
        """直接解码 data URI 封面（加密站解密结果）。"""
        try:
            _, b64 = data_uri.split(",", 1)
            import base64

            data = base64.b64decode(b64)
            pix = QPixmap()
            if pix.loadFromData(data) and not pix.isNull():
                self._on_cover_ready(pix)
        except Exception:  # noqa: BLE001
            pass

    def set_cover_data(self, data_uri: str) -> None:
        """用解密后的 data URI 刷新封面（加密站搜索结果封面后补）。

        搜索结果返回时 cover 是加密 URL，WorkCard 直接加载不出图；后台
        AES 解密完成后调用本方法，把 data URI 画上。同时回写 work.cover，
        便于后续复用（详情/加入书架）。
        """
        if not data_uri or not data_uri.startswith("data:"):
            return
        try:
            self.work.cover = data_uri
        except Exception:  # noqa: BLE001
            pass
        self._load_data_cover(data_uri)

    def _load_cover(self, url: str) -> None:
        """通过 CoverLoader 加载封面（全局最多 4 个并发）。"""
        CoverLoader.instance().load(url, self._on_cover_ready)

    def _on_check_toggled(self, checked: bool) -> None:
        self.checked.emit(self.work, checked)

    def is_checked(self) -> bool:
        return bool(self._checkbox and self._checkbox.isChecked())

    def set_checked(self, checked: bool) -> None:
        if self._checkbox is not None:
            self._checkbox.setChecked(checked)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        # 点勾选框不触发卡片点击（checkbox 自己处理）
        if self._checkbox is not None and self._checkbox.underMouse():
            return
        self.clicked.emit(self.work)
        super().mouseReleaseEvent(event)

    def _on_cover_ready(self, pixmap) -> None:
        """CoverLoader 回调：更新封面。

        统一裁剪到容器尺寸（140×180）：不同横竖比例的封面，
        用 KeepAspectRatioByExpanding 铺满后居中裁剪，保证视觉大小一致。

        注意：封面是异步加载，回调到达时卡片可能已被回收（刷新/切页），
        此时 self._cover 的 C++ 对象已删除，需先判断存活再访问。
        """
        if pixmap is None:
            return  # 保留占位符
        import shiboken6

        if not shiboken6.isValid(self._cover):
            return  # 卡片已销毁，跳过
        from PySide6.QtCore import Qt as _Qt
        # 先按比例铺满容器，再居中裁剪到容器尺寸
        scaled = pixmap.scaled(
            140, 180, _Qt.KeepAspectRatioByExpanding, _Qt.SmoothTransformation
        )
        # 居中裁剪到 140×180
        sx = max(0, (scaled.width() - 140) // 2)
        sy = max(0, (scaled.height() - 180) // 2)
        cropped = scaled.copy(sx, sy, min(140, scaled.width()), min(180, scaled.height()))
        self._cover.setPixmap(cropped)
        from gui.components.cover_loader import fade_in

        fade_in(self._cover)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit(self.work)
        super().mouseReleaseEvent(event)
