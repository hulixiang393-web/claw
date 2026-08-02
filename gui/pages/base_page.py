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

    def refresh(self) -> None:
        """从数据源刷新本页。"""
