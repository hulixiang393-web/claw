"""作品卡片（WorkCard）。

发现列表 / 搜索结果共用的卡片：封面（上）→ 标题 → 作者 → 来源/更新（下）。
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

# 封面容器高度：固定高 180，保证卡片上半区统一
COVER_HEIGHT = 180
# 标题区高度：限 2 行。13px 字 2 行约需 40px，38 会裁掉第 2 行下半截（标题被遮住）
TITLE_HEIGHT = 44
# 封面与标题之间的留白：图文分层的视觉间隔，让标题不再贴着封面
COVER_TITLE_GAP = 8
# 整卡固定高度：封面 180 + 标题 2 行 + 作者 + 来源 + 边距/间距
CARD_HEIGHT = 292


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
        # 固定卡片高度：封面 180 + 标题 2 行 + 作者 + 来源，保证网格每行等高
        self.setFixedHeight(CARD_HEIGHT)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        # 封面行：封面（铺满卡片宽度，固定高 180）+ 可选勾选框（右上角）
        cover_row = QHBoxLayout()
        cover_row.setSpacing(4)

        cover = QLabel("🖼")
        cover.setAlignment(Qt.AlignCenter)
        cover.setFixedHeight(COVER_HEIGHT)
        cover.setStyleSheet("background: palette(midlight); border-radius: 8px; font-size: 36px;")
        cover_row.addWidget(cover, stretch=1)  # 铺满剩余宽度，封面更大更整齐
        self._cover = cover

        self._checkbox = None
        if selectable:
            self._checkbox = QCheckBox()
            self._checkbox.setCursor(Qt.PointingHandCursor)
            self._checkbox.toggled.connect(self._on_check_toggled)
            # 放封面右上角，避免挡住封面
            cover_row.addWidget(self._checkbox, alignment=Qt.AlignTop | Qt.AlignRight)
        layout.addLayout(cover_row)
        # 封面与标题间的留白：拉大图文间隔，标题不再贴着封面
        layout.addSpacing(COVER_TITLE_GAP)

        # 标题：限 2 行，超出截断；固定高度保证卡片等高
        title = QLabel(work.title or "无标题")
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        title.setFixedHeight(TITLE_HEIGHT)
        title.setStyleSheet("font-size: 13px; font-weight: bold; color: palette(text);")
        layout.addWidget(title)
        self._title = title

        # 作者：一行省略；无作者不渲染空标签
        self._author_label = QLabel(work.author or "")
        self._author_label.setStyleSheet("font-size: 11px; color: palette(mid);")
        if not work.author:
            self._author_label.hide()
        layout.addWidget(self._author_label)

        # 底部：来源 / 更新小字；两者都没有则不渲染空占位
        layout.addStretch(1)
        self._meta_label = QLabel("")
        self._meta_label.setStyleSheet("color: palette(dark); font-size: 10px;")
        meta_text = " · ".join(p for p in (work.source_name, work.update) if p)
        if meta_text:
            self._meta_label.setText(meta_text)
        else:
            self._meta_label.hide()
        layout.addWidget(self._meta_label)

        # 异步加载封面（全局限流，不会并发爆炸）
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

    def set_cover_pixmap(self, pixmap) -> None:
        """按当前封面容器尺寸居中裁剪后显示（封面异步/后补共用）。

        不同横竖比例的封面，用 KeepAspectRatioByExpanding 铺满后居中裁剪，
        保证视觉大小一致（封面铺满卡片宽度，高度固定 COVER_HEIGHT）。

        注意：封面是异步加载，回调到达时卡片可能已被回收（刷新/切页），
        此时 self._cover 的 C++ 对象已删除，需先判断存活再访问。
        """
        if pixmap is None:
            return  # 保留占位符
        import shiboken6

        if not shiboken6.isValid(self._cover):
            return  # 卡片已销毁，跳过
        from PySide6.QtCore import Qt as _Qt

        w = self._cover.width()
        h = self._cover.height()
        if w <= 0 or h <= 0:
            w, h = 140, COVER_HEIGHT  # 兜底：容器尚未布局时用默认尺寸
        # 先按比例铺满容器，再居中裁剪到容器尺寸
        scaled = pixmap.scaled(
            w, h, _Qt.KeepAspectRatioByExpanding, _Qt.SmoothTransformation
        )
        # 居中裁剪到容器尺寸
        sx = max(0, (scaled.width() - w) // 2)
        sy = max(0, (scaled.height() - h) // 2)
        cropped = scaled.copy(sx, sy, min(w, scaled.width()), min(h, scaled.height()))
        self._cover.setPixmap(cropped)
        from gui.components.cover_loader import fade_in

        fade_in(self._cover)

    def _on_cover_ready(self, pixmap) -> None:
        """CoverLoader 回调：统一走 set_cover_pixmap 裁剪显示。"""
        self.set_cover_pixmap(pixmap)
