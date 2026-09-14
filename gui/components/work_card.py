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
from .hover_title import HoverTitle

# 封面容器高度：固定高 180，保证卡片上半区统一
COVER_HEIGHT = 180
# 标题区高度：固定单行。超长省略，悬停卡片时浮层滑出完整标题
TITLE_HEIGHT = 20
# 封面与标题之间的留白：图文分层的视觉间隔，让标题不再贴着封面
COVER_TITLE_GAP = 8
# 整卡固定高度：封面 180 + 标题单行 + 作者 + 来源 + 边距/间距，富余给底部 stretch
CARD_HEIGHT = 292


def _make_text_cover(title: str, w: int, h: int) -> QPixmap:
    """生成文字占位封面：标题首字 + 按标题散列的主题色渐变。

    用于源站无封面（列表纯文本）或封面加载失败（如 quanben 的死图床）时，
    避免卡片长期空白/占位图标，视觉上仍有可辨识的「封面」。
    """
    import hashlib

    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter

    w = max(1, int(w))
    h = max(1, int(h))
    pm = QPixmap(w, h)
    pm.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pm)
    try:
        text = (title or "?").strip() or "?"
        seed = int(hashlib.md5(text.encode("utf-8")).hexdigest()[:8], 16)
        hue = seed % 360
        c1 = QColor.fromHsv(hue, 110, 214)
        c2 = QColor.fromHsv((hue + 28) % 360, 150, 158)
        grad = QLinearGradient(0, 0, w, h)
        grad.setColorAt(0.0, c1)
        grad.setColorAt(1.0, c2)
        painter.fillRect(0, 0, w, h, grad)
        ch = text[0]
        font = QFont()
        font.setPointSize(max(16, h // 4))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255, 235))
        painter.drawText(pm.rect(), _Qt.AlignCenter, ch)
    finally:
        painter.end()
    return pm


class WorkCard(QFrame):
    """单张作品卡片。"""

    clicked = Signal(object)  # 发射 Work 对象
    checked = Signal(object, bool)  # (work, checked) 勾选状态变化（批量模式）
    source_clicked = Signal(str)  # 来源名被点击时发射 source_id（仅 source_filterable 启用时）

    def __init__(self, work, selectable: bool = False, parent=None,
                 defer_cover: bool = False, source_filterable: bool = False):
        super().__init__(parent)
        self.work = work
        self.setObjectName("workCard")
        self.setCursor(Qt.PointingHandCursor)
        # 弹性宽度：由网格列数决定，不固定，避免窗口窄时横向溢出
        self.setMinimumWidth(120)
        # 固定卡片高度：封面 180 + 标题 2 行 + 作者 + 来源，保证网格每行等高
        self.setFixedHeight(CARD_HEIGHT)
        # 封面延迟加载：宿主页懒加载封面时传 defer_cover=True，创建时不立即
        # 拉封面（首屏一批卡片不挤占 CoverLoader 并发），进入视口后由宿主
        # 调用 load_cover() 触发。
        self._defer_cover = defer_cover
        self._cover_loaded = False
        self._placeholder_active = False
        self._source_filterable = source_filterable

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

        # 标题：固定单行省略；一行放不下时悬停卡片滑出完整标题（HoverTitle）
        title = HoverTitle(work.title or "无标题", parent_card=self)
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
        self._source_label = None
        self._update_label = None
        self._meta_label = QLabel("")
        self._meta_label.setStyleSheet("color: palette(dark); font-size: 10px;")

        source_name = work.source_name or ""
        update_text = work.update or ""

        if source_filterable and source_name:
            self._source_label = QLabel(source_name)
            self._source_label.setStyleSheet(
                "color: palette(dark); font-size: 10px;"
            )
            self._source_label.setCursor(Qt.PointingHandCursor)
            self._source_label.installEventFilter(self)
            layout.addWidget(self._source_label)
            if update_text:
                self._update_label = QLabel(f" · {update_text}")
                self._update_label.setStyleSheet("color: palette(dark); font-size: 10px;")
                layout.addWidget(self._update_label)
        else:
            meta_text = " · ".join(p for p in (source_name, update_text) if p)
            if meta_text:
                self._meta_label.setText(meta_text)
            else:
                self._meta_label.hide()
            layout.addWidget(self._meta_label)

        # 异步加载封面（全局限流，不会并发爆炸）。
        # defer_cover=True 时不立即拉（等宿主进入视口再 load_cover），
        # 避免首屏一批卡片同时挤占 CoverLoader 并发。
        if work.cover:
            if not self._defer_cover:
                self._cover_loaded = True
                if work.cover.startswith("data:"):
                    # data URI（解密后的封面）直接解码显示，无需网络
                    self._load_data_cover(work.cover)
                else:
                    self._load_cover(work.cover)
        else:
            # 无封面（列表纯文本源）→ 直接上文字占位封面，不留空白/图标
            self._apply_placeholder()

    # ------------------------------------------------------------------ #
    def _apply_placeholder(self) -> None:
        """生成/刷新文字占位封面（标题首字 + 散列主题色）。"""
        import shiboken6

        if not shiboken6.isValid(self._cover):
            return
        w = self._cover.width()
        h = self._cover.height()
        if w <= 0 or h <= 0:
            w, h = 140, COVER_HEIGHT
        self._cover.setPixmap(_make_text_cover(self.work.title or "?", w, h))
        self._placeholder_active = True

    def resizeEvent(self, event) -> None:  # noqa: N802
        """宽度变化时按新尺寸重绘文字占位封面（标题首字居中）。"""
        super().resizeEvent(event)
        if getattr(self, "_placeholder_active", False):
            self._apply_placeholder()
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

    def load_cover(self) -> None:
        """触发封面加载（defer_cover 卡片进入视口时由宿主调用）。

        幂等：已加载/无封面/重复触发都直接返回。data URI 即时解码。
        """
        if self._cover_loaded:
            return
        if not self.work or not self.work.cover:
            return
        self._cover_loaded = True
        if self.work.cover.startswith("data:"):
            self._load_data_cover(self.work.cover)
        else:
            self._load_cover(self.work.cover)

    def _load_cover(self, url: str) -> None:
        """通过 CoverLoader 加载封面（全局最多 16 个并发）。"""
        CoverLoader.instance().load(
            url, self._on_cover_ready, source_id=self.work.source_id
        )

    def _on_check_toggled(self, checked: bool) -> None:
        self.checked.emit(self.work, checked)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        # 防御：残留过期事件可能在半销毁态（_source_label 尚未初始化）派发进来
        src_label = getattr(self, "_source_label", None)
        if src_label is not None and obj is src_label:
            if event.type() == event.Type.Enter:
                self._source_label.setStyleSheet(
                    "color: palette(highlight); font-size: 10px; font-weight: bold;"
                )
            elif event.type() == event.Type.Leave:
                self._source_label.setStyleSheet(
                    "color: palette(dark); font-size: 10px;"
                )
            elif event.type() == event.Type.MouseButtonPress:
                self.source_clicked.emit(self.work.source_id)
                return True
        return super().eventFilter(obj, event)

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

    def set_source_filterable(self, enabled: bool) -> None:
        """启用/禁用来源名可点击筛选（宿主按需调用，默认关闭）。"""
        if enabled == self._source_filterable:
            return
        self._source_filterable = enabled
        source_name = self.work.source_name or ""
        update_text = self.work.update or ""
        layout = self.layout()
        if enabled and source_name:
            # 移除旧 _meta_label
            if self._meta_label:
                layout.removeWidget(self._meta_label)
                self._meta_label.hide()
            self._source_label = QLabel(source_name)
            self._source_label.setStyleSheet(
                "color: palette(dark); font-size: 10px;"
            )
            self._source_label.setCursor(Qt.PointingHandCursor)
            self._source_label.installEventFilter(self)
            layout.addWidget(self._source_label)
            if update_text:
                self._update_label = QLabel(f" · {update_text}")
                self._update_label.setStyleSheet("color: palette(dark); font-size: 10px;")
                layout.addWidget(self._update_label)
        elif not enabled:
            # 恢复原有 _meta_label
            for w in (self._source_label, self._update_label):
                if w:
                    layout.removeWidget(w)
                    w.hide()
            self._source_label = None
            self._update_label = None
            meta_text = " · ".join(p for p in (source_name, update_text) if p)
            if meta_text:
                self._meta_label.setText(meta_text)
                self._meta_label.show()
                layout.addWidget(self._meta_label)

    def set_cover_pixmap(self, pixmap) -> None:
        """按当前封面容器尺寸居中裁剪后显示（封面异步/后补共用）。

        不同横竖比例的封面，用 KeepAspectRatioByExpanding 铺满后居中裁剪，
        保证视觉大小一致（封面铺满卡片宽度，高度固定 COVER_HEIGHT）。

        注意：封面是异步加载，回调到达时卡片可能已被回收（刷新/切页），
        此时 self._cover 的 C++ 对象已删除，需先判断存活再访问。
        """
        if pixmap is None:
            # 加载失败（图床失效/防盗链）→ 回退文字占位封面，不留空白
            self._apply_placeholder()
            return
        import shiboken6

        if not shiboken6.isValid(self._cover):
            return  # 卡片已销毁，跳过
        from PySide6.QtCore import Qt as _Qt

        self._placeholder_active = False
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

        if not CoverLoader.instance().busy():
            fade_in(self._cover)

    def _on_cover_ready(self, pixmap) -> None:
        """CoverLoader 回调：统一走 set_cover_pixmap 裁剪显示。"""
        self.set_cover_pixmap(pixmap)
