"""页面基类（BasePage）。

所有 Tab 页共用：统一页面生命周期 + 事件订阅接口。
对应 ui-home.md「页基类」。
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget


class BasePage(QWidget):
    """所有 Tab 页的基类。"""

    def __init__(self, parent=None):
        super().__init__(parent)

    def on_theme_changed(self, theme: str) -> None:
        """主题切换时回调（默认无操作，子类可覆写刷新颜色）。"""

    def on_event(self, event) -> None:
        """EventBus 事件回调（默认无操作，子类订阅后覆写）。"""

    def grid_columns(self, viewport_w) -> int:
        """按可视宽度计算作品网格列数（多列卡片流共用）。

        每列期望 190px（比旧 170px 更宽，卡片更舒适）；
        900px 最小窗口约 4 列，上限 8 列防列数过多卡片过小。
        调用方传入循环前算好的 viewport 宽度（不可用则 900 兜底）。
        """
        view_w = viewport_w or 900
        cols = max(2, view_w // 190)
        return min(cols, 8)

    def apply_column_stretch(self, grid_layout, cols: int) -> None:
        """让网格每列等宽，卡片均匀分布（避免某列标题长导致列宽参差）。"""
        for i in range(grid_layout.columnCount()):
            grid_layout.setColumnStretch(i, 0)
        for i in range(cols):
            grid_layout.setColumnStretch(i, 1)

    def refresh(self) -> None:
        """从数据源刷新本页。"""
